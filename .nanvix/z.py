# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

"""Nanvix build script for XZ Utils.

Usage:
    ./z setup     # Resolve toolchain + download sysroot, prepare upstream tree
    ./z build    # Cross-compile liblzma.a
    ./z test     # Three-tier smoke + integration + functional ladder
    ./z release  # Stage sysroot/{lib,include} + emit dist/*.tar.gz
    ./z clean    # Remove build artefacts
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

from nanvix_zutil import (
    CFG_SYSROOT,
    EXIT_BUILD_FAILURE,
    EXIT_MISSING_DEP,
    TOOLCHAIN_CONTAINER_PATH,
    ZScript,
    log,
    make_initrd,
    run,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Upstream xz uses GNU autoconf; we install with --prefix=/sysroot so that
# release tarballs encode the deployment-time path and not an ephemeral
# build runner path.  See sqlite's port for the same convention.
_INSTALL_PREFIX = "/sysroot"

# Sentinel honoured by `./configure` to make `./z build` idempotent.
_CONFIGURED_MARKER = ".nanvix-configured"

# Subdirectory under the upstream tree where `make install DESTDIR=...`
# stages the install image before we copy the curated subset into build/.
_INSTALL_STAGE_REL = Path("build") / "_install"


# Upstream tests/check_PROGRAMS list (tests/Makefile.am:39-46).  The order
# matches the upstream Makefile so iteration is stable and the names are
# trivially greppable across the port.
_UPSTREAM_TEST_NAMES = (
    "test_check",
    "test_stream_flags",
    "test_filter_flags",
    "test_block_header",
    "test_index",
    "test_bcj_exact_size",
)

# Names (sans .elf) of upstream tests known to exit 77 (automake's SKIP
# convention) under nanvixd.elf, mapped to a one-line reason.  An empty
# dict means every upstream test is expected to PASS (exit 0); the runner
# treats any 77 from a non-listed test as an unexpected SKIP and fails the
# tier.  Kept here (not a sidecar file) so a grep from the runner lands on
# both the data and the consumer in one place.
_UPSTREAM_TEST_SKIPLIST: dict[str, str] = {}

IS_WINDOWS = sys.platform == "win32"


class XzBuild(ZScript):
    """Build script for nanvix/xz."""

    # ------------------------------------------------------------------
    # Docker hooks
    # ------------------------------------------------------------------

    def docker_image(self) -> str:  # noqa: D102
        return "ghcr.io/nanvix/toolchain-gcc:sha-34a3641"

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _toolchain_path(self) -> Path:
        """Return the resolved cross-toolchain prefix."""
        return Path(str(TOOLCHAIN_CONTAINER_PATH))

    def _sysroot_path(self) -> Path:
        """Return the resolved Nanvix sysroot, or fail loudly."""
        sysroot = self.config.get(CFG_SYSROOT, "")
        if not sysroot:
            log.fatal(
                f"{CFG_SYSROOT} is not set.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first to download the sysroot.",
            )
        return Path(sysroot)

    def _configure_env_overrides(self) -> dict[str, str]:
        """Return only the env keys we explicitly set for ./configure.

        Kept separate from the full merged ``os.environ`` so the
        ``.nanvix-configured`` marker can fingerprint the configure
        inputs without being polluted by ambient ``$PWD`` / ``$SHELL``
        / ``$TERM`` / etc.

        Lifted from sqlite's Makefile.nanvix CONFIGURE_ENV block; LIBZ
        dropped (xz has no zlib dependency), -DSQLITE_OMIT_WAL dropped,
        -D_GNU_SOURCE added (zlib precedent for newlib feature gates).
        """
        # translate_path() returns the host path unchanged when docker mode
        # is inactive, and the container mount-point (e.g. /mnt/sysroot)
        # when --with-docker is in effect.  Without this remap, configure's
        # C compiler probe fails inside the container with "cannot create
        # executables" because it is told to link against host paths that
        # do not exist in the container's filesystem.
        toolchain = str(TOOLCHAIN_CONTAINER_PATH)
        sysroot = (
            self.docker.translate_path(self._sysroot_path())
            if self.docker
            else self._sysroot_path()
        )
        bin_ = f"{toolchain}/bin"
        return {
            "AR": f"{bin_}/i686-nanvix-ar",
            "AS": f"{bin_}/i686-nanvix-as",
            "CC": f"{bin_}/i686-nanvix-gcc",
            "CXX": f"{bin_}/i686-nanvix-g++",
            "CPP": f"{bin_}/i686-nanvix-gcc -E",
            "LD": f"{bin_}/i686-nanvix-ld",
            "RANLIB": f"{bin_}/i686-nanvix-ranlib",
            "STRIP": f"{bin_}/i686-nanvix-strip",
            "NM": f"{bin_}/i686-nanvix-nm",
            "CFLAGS": f"-O2 -D_GNU_SOURCE -I{sysroot}/include",
            "CPPFLAGS": f"-D_GNU_SOURCE -I{sysroot}/include",
            "LDFLAGS": (
                f"-static -T{sysroot}/lib/user.ld -L{sysroot}/lib "
                f"-Wl,--start-group -lposix -lc -lm -Wl,--end-group"
            ),
            # Intentionally empty: passing -Wl,--start-group via LIBS
            # breaks the sed pipeline that materialises liblzma.pc
            # (the commas inside -Wl,... collide with sed's `s,,,g`
            # delimiter).  All link inputs are carried in LDFLAGS.
            "LIBS": "",
        }

    def _configure_env(self) -> dict[str, str]:
        """Full env for ./configure: ``os.environ`` merged with overrides."""
        env: dict[str, str] = dict(os.environ)
        env.update(self._configure_env_overrides())
        return env

    def _configured_marker_contents(
        self,
        opts: list[str],
        overrides: dict[str, str],
    ) -> str:
        """Deterministic fingerprint of the configure inputs.

        Stored in ``.nanvix-configured`` so ``build()`` can detect when
        a re-``./z setup`` (or a manual ``nanvix.toml`` edit) has
        resolved a different sysroot or toolchain and force a clean
        re-``./configure`` instead of silently reusing the prior
        ``Makefile``.  Plain key=value text rather than a hash so a
        human can ``cat`` the marker and see what it pinned.
        """
        lines = [
            "# configured by .nanvix/z.py — do not edit; regenerated by build()",
            "[configure-opts]",
            *opts,
            "[configure-env]",
            *(f"{k}={overrides[k]}" for k in sorted(overrides)),
        ]
        return "\n".join(lines) + "\n"

    def _configure_opts(self) -> list[str]:
        """Configure flags: ship liblzma only; CLI not needed by cpython."""
        return [
            "--host=i686-nanvix",
            f"--prefix={_INSTALL_PREFIX}",
            "--disable-shared",
            "--enable-static",
            "--disable-nls",
            "--disable-doc",
            "--disable-scripts",
            "--disable-xz",
            "--disable-xzdec",
            "--disable-lzmadec",
            "--disable-lzmainfo",
            "--enable-small",
            "--disable-threads",
        ]

    # ------------------------------------------------------------------
    # ./configure / autotools preparation
    # ------------------------------------------------------------------

    def _ensure_configure(self) -> None:
        """Verify the vendored ./configure is present.

        The xz port vendors the autotools-generated outputs (configure,
        aclocal.m4, config.h.in, Makefile.in, build-aux/*, m4/*, po/*)
        directly into the port repo so CI does not need autoconf,
        autopoint, libtool, automake, or gettext inside the toolchain
        Docker image.  Refresh procedure: ``sh ./autogen.sh --no-po4a``
        on a host with autotools, then ``git add -f`` the regenerated
        files (see NANVIX.md / Refreshing vendored autotools outputs).
        """
        configure = self.repo_root / "configure"
        if not configure.exists():
            log.fatal(
                "./configure is missing from the port tree.",
                code=EXIT_BUILD_FAILURE,
                hint=(
                    "The autotools outputs are vendored; if they have "
                    "been deleted, regenerate with `sh ./autogen.sh "
                    "--no-po4a` on a host with autoconf+automake+"
                    "libtool+autopoint installed, then `git add -f` "
                    "the result."
                ),
            )

    # ------------------------------------------------------------------
    # ZScript hook overrides
    # ------------------------------------------------------------------

    def setup(self) -> bool:
        """Resolve sysroot/toolchain and prepare the autotools tree."""
        result = super().setup()
        self._ensure_configure()
        return result

    def build(self) -> None:
        """Cross-compile liblzma.a via the upstream autotools."""
        # Configure once per distinct configure-input set.  If the
        # sysroot, toolchain, or configure flags have changed since the
        # last successful ./configure (e.g. a re-./z setup resolved a
        # different sysroot), the marker contents will mismatch and we
        # re-run ./configure to avoid linking against stale Makefiles.
        marker = self.repo_root / _CONFIGURED_MARKER
        opts = self._configure_opts()
        overrides = self._configure_env_overrides()
        wanted = self._configured_marker_contents(opts, overrides)
        current = marker.read_text() if marker.exists() else None
        if current != wanted:
            if current is not None:
                log.info(
                    "Configure inputs changed since last build; re-running ./configure."
                )
            env = dict(os.environ)
            env.update(overrides)
            # ./configure and make both invoke the cross-toolchain, so
            # they must run inside the docker-wrapped build context
            # established by `./z setup --with-docker`.
            run(
                "./configure",
                *opts,
                cwd=self.repo_root,
                env=env,
                docker=self.docker,
            )
            marker.write_text(wanted)

        # Build.
        try:
            nproc = str(os.cpu_count() or 1)
        except Exception:
            nproc = "1"
        run("make", f"-j{nproc}", cwd=self.repo_root, docker=self.docker)

        # Stage a curated install image and copy the subset we ship into
        # build/ at the layout the schema/release path expects.
        self._stage_artefacts()
        # Cross-compile upstream's tests/check_PROGRAMS now, while we
        # are inside the docker-wrapped build context.  ``nanvix-zutil
        # test`` is deliberately host-only (script.py:730 — "test and
        # benchmark run on the host"), so the cross-compiler is
        # unreachable from the test step in CI when --with-docker was
        # used during setup.  By producing build/tests/test_*.elf here,
        # the test tiers below only need to *execute* the pre-built
        # artefacts on the host (via nanvixd.elf, which is a Linux host
        # binary in the sysroot).
        self._build_upstream_tests()

    def _stage_artefacts(self) -> None:
        """Install into build/_install and copy outputs into build/."""
        repo = self.repo_root
        build_dir = repo / "build"
        stage = repo / _INSTALL_STAGE_REL

        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)

        run(
            "make",
            "install",
            f"DESTDIR={(self.docker.translate_path(stage) if self.docker else stage)}",
            cwd=repo,
            docker=self.docker,
        )

        # The configure --prefix=/sysroot lands files under
        # <stage>/sysroot/{lib,bin,include}/...  Copy out into build/.
        src_root = stage / "sysroot"
        if not src_root.is_dir():
            log.fatal(
                f"Expected install staging at {src_root} not found.",
                code=EXIT_BUILD_FAILURE,
            )

        # Clear any prior staged outputs but keep _install/ in place.
        for name in ("liblzma.a", "include", "lib"):
            target = build_dir / name
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()

        # liblzma.a → build/liblzma.a (and also build/lib/liblzma.a for
        # the release packaging step in a later commit).
        lib_dir = build_dir / "lib"
        lib_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_root / "lib" / "liblzma.a", lib_dir / "liblzma.a")
        shutil.copy2(src_root / "lib" / "liblzma.a", build_dir / "liblzma.a")

        # liblzma.pc → build/lib/pkgconfig/liblzma.pc
        pc_src = src_root / "lib" / "pkgconfig" / "liblzma.pc"
        pc_dst_dir = lib_dir / "pkgconfig"
        pc_dst_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pc_src, pc_dst_dir / "liblzma.pc")

        # Headers → build/include/{lzma.h, lzma/*.h}
        include_dir = build_dir / "include"
        include_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_root / "include" / "lzma.h", include_dir / "lzma.h")
        shutil.copytree(
            src_root / "include" / "lzma",
            include_dir / "lzma",
        )

        log.info(f"Staged artefacts under {build_dir}")

    def _expected_test_paths(self) -> list[Path]:
        """Return the expected ``build/tests/<name>.elf`` paths.

        Pure path computation -- does not touch the filesystem and
        does not invoke any subprocess.  Used by both the build-time
        cross-compile and the host-side test tiers to agree on where
        the upstream test ELFs live.
        """
        dest_dir = self.repo_root / "build" / "tests"
        return [dest_dir / f"{name}.elf" for name in _UPSTREAM_TEST_NAMES]

    def _locate_upstream_tests(self) -> list[Path]:
        """Return the cached upstream test ELFs (host-side; no docker).

        Asserts every expected ELF exists; if any are missing, fails
        with a hint to run ``./z build`` first.  Used
        by the test tiers, which must never reach the cross-toolchain.
        """
        dests = self._expected_test_paths()
        missing = [d for d in dests if not d.is_file()]
        if missing:
            log.fatal(
                "upstream test ELFs missing: " + ", ".join(str(d) for d in missing),
                code=EXIT_BUILD_FAILURE,
                hint="Run `./z build` first.",
            )
        return dests

    def _build_upstream_tests(self) -> list[Path]:
        """Cross-compile upstream tests/check_PROGRAMS into build/tests/.

        Drives ``make -C tests check_PROGRAMS`` inside the same
        docker-wrapped build context as :meth:`build`, then copies the
        libtool-unwrapped ELFs into ``build/tests/test_*.elf`` and
        returns their paths in the order declared by upstream's
        ``tests/Makefile.am``.

        Idempotent: if every destination ELF is newer than its source
        ``tests/<name>.c`` and ``build/liblzma.a``, the rebuild is
        skipped and the existing destinations are returned.

        Only safe to call from :meth:`build` (cross-compile context).
        Test tiers must use :meth:`_locate_upstream_tests` instead.
        """
        repo = self.repo_root
        build_dir = repo / "build"
        liblzma = build_dir / "liblzma.a"
        if not liblzma.is_file():
            log.fatal(
                f"liblzma.a missing at {liblzma}; run `./z build` first.",
                code=EXIT_BUILD_FAILURE,
            )

        dests = self._expected_test_paths()
        dest_dir = build_dir / "tests"
        dest_dir.mkdir(parents=True, exist_ok=True)
        srcs = [repo / "tests" / f"{name}.c" for name in _UPSTREAM_TEST_NAMES]

        inputs_mtime = max(
            liblzma.stat().st_mtime,
            *(s.stat().st_mtime for s in srcs if s.exists()),
        )
        if all(d.is_file() and d.stat().st_mtime >= inputs_mtime for d in dests):
            return dests

        # Build the env on top of the canonical configure overrides so
        # CC/AR/etc. all point at the cross toolchain.  We additionally
        # need to override LDFLAGS+LIBS for the make-time link of the
        # check_PROGRAMS targets, but make will not honour env-set
        # values for variables that the generated Makefile assigns
        # unconditionally (LDFLAGS and LIBS are baked in at configure
        # time).  We therefore pass the override on the make command
        # line below, where it wins over the Makefile assignment.
        #
        # The override itself: libtool reorders anything in LDFLAGS
        # ahead of LDADD when composing the final link line for the
        # check_PROGRAMS targets, which would split the
        # --start-group/--end-group group across liblzma.la and break
        # cyclic resolution between liblzma/libposix/libc/libm.  We
        # therefore drop the link group from LDFLAGS and carry it in
        # LIBS as a single comma-joined -Wl, token, which libtool
        # preserves positionally (it appears after the convenience
        # library at link time).  The configure-time LIBS="" invariant
        # in _configure_env_overrides is unaffected — that recipe is
        # only consumed by ./configure, never by make.
        sysroot = (
            self.docker.translate_path(self._sysroot_path())
            if self.docker
            else self._sysroot_path()
        )
        env = dict(os.environ)
        env.update(self._configure_env_overrides())
        ldflags_override = f"-static -T{sysroot}/lib/user.ld -L{sysroot}/lib"
        libs_override = "-Wl,--start-group,-lposix,-lc,-lm,--end-group"

        try:
            nproc = str(os.cpu_count() or 1)
        except Exception:
            nproc = "1"

        log.info("Building upstream tests/check_PROGRAMS")
        # ``check_PROGRAMS`` is a Make variable, not a target.  Pass the
        # individual binary names as explicit targets so we build them
        # without running them (``make check`` would build *and* run
        # under upstream's test harness, which we deliberately bypass).
        run(
            "make",
            "-C",
            "tests",
            f"LDFLAGS={ldflags_override}",
            f"LIBS={libs_override}",
            *_UPSTREAM_TEST_NAMES,
            f"-j{nproc}",
            cwd=repo,
            env=env,
            docker=self.docker,
        )

        # libtool drops the unwrapped ELF under tests/.libs/<name>; in
        # the rare case it inlines the binary directly into tests/<name>
        # (e.g. when no shared-library wrapper is needed) fall back to
        # the flat path.
        for name, dest in zip(_UPSTREAM_TEST_NAMES, dests):
            src = repo / "tests" / ".libs" / name
            if not src.is_file():
                src = repo / "tests" / name
            if not src.is_file():
                log.fatal(
                    f"upstream test binary missing: {name} "
                    "(looked under tests/.libs/ and tests/)",
                    code=EXIT_BUILD_FAILURE,
                )
            shutil.copy2(src, dest)
        return dests

    # ------------------------------------------------------------------
    # Test ladder
    # ------------------------------------------------------------------

    def test(self) -> None:
        """Run the three-tier test ladder.

        Without arguments runs smoke -> integration -> functional in
        order.  When invoked as ``./z test -- <tier> [<tier> ...]`` the
        named tiers run in the order given (mirrors the reusable CI
        workflow's ``standalone-test-args`` knob so callers can ask for
        ``test-smoke test-integration`` only on slow standalone cells).
        """
        if IS_WINDOWS:
            self._run_tests_windows()
            return

        tier_map = {
            "smoke": self._test_smoke,
            "test-smoke": self._test_smoke,
            "integration": self._test_integration,
            "test-integration": self._test_integration,
            "functional": self._test_functional,
            "test-functional": self._test_functional,
        }
        if self.targets:
            unknown = [t for t in self.targets if t not in tier_map]
            if unknown:
                log.fatal(
                    f"Unknown test target(s): {', '.join(unknown)}",
                    code=EXIT_BUILD_FAILURE,
                    hint=f"Known: {', '.join(sorted(set(tier_map)))}",
                )
            tiers = [tier_map[t] for t in self.targets]
        else:
            tiers = [self._test_smoke, self._test_integration, self._test_functional]
        for tier in tiers:
            tier()

    def _test_smoke(self) -> None:
        """Verify build artefacts exist and look sane (no runtime)."""
        log.info("=== xz smoke tests ===")
        build_dir = self.repo_root / "build"
        liblzma = build_dir / "liblzma.a"
        header = build_dir / "include" / "lzma.h"
        pc = build_dir / "lib" / "pkgconfig" / "liblzma.pc"
        for path, floor in (
            (liblzma, 100_000),
            (header, 0),
            (pc, 0),
        ):
            if not path.is_file():
                log.fatal(
                    f"smoke: missing artefact {path}",
                    code=EXIT_BUILD_FAILURE,
                    hint="Run `./z build` first.",
                )
            size = path.stat().st_size
            if size < floor:
                log.fatal(
                    f"smoke: {path} too small ({size} < {floor})",
                    code=EXIT_BUILD_FAILURE,
                )
            log.info(f"  OK: {path.name} ({size} bytes)")
        log.info("  PASS: xz smoke tests")

    def _test_integration(self) -> None:
        """Confirm every upstream test binary is a static ELF."""
        log.info("=== xz integration tests ===")
        elfs = self._locate_upstream_tests()
        # Verify ELF format via magic bytes -- the source of truth
        # and dependency-free.  file(1), if present, is queried only
        # for the human-readable 'statically linked' confirmation; its
        # absence (e.g. on minimal Windows runners) must not fail the
        # tier.  No size floor: the six upstream ELFs vary widely;
        # presence + ELF magic is the contract.
        for elf in elfs:
            if not elf.is_file():
                log.fatal(
                    f"integration: missing {elf}",
                    code=EXIT_BUILD_FAILURE,
                )
            with elf.open("rb") as fh:
                magic = fh.read(4)
            if magic != b"\x7fELF":
                log.fatal(
                    f"integration: {elf.name} is not an ELF binary (magic={magic!r})",
                    code=EXIT_BUILD_FAILURE,
                )
            try:
                file_out = subprocess.run(
                    ["file", str(elf)],
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.lower()
            except (FileNotFoundError, subprocess.CalledProcessError):
                file_out = ""
            if file_out and "statically" not in file_out:
                log.info(
                    f"  WARN: file(1) did not report 'statically linked' for {elf.name}"
                )
            log.info(f"  OK: {elf.name} ({elf.stat().st_size} bytes, ELF)")
        log.info("  PASS: xz integration tests")

    def _test_functional(self) -> None:
        """Run every upstream check_PROGRAMS test under nanvixd.elf.

        Filters out names listed in :data:`_UPSTREAM_TEST_SKIPLIST`
        (logs one ``SKIP`` line per filtered entry) and runs the
        remainder.  In standalone mode, uses ``make_initrd`` to bundle
        each test binary with system daemons; in multi-process and
        single-process modes, invokes nanvixd directly with the ELF.
        """
        log.info("=== xz functional tests ===")
        all_elfs = self._locate_upstream_tests()
        elfs: list[Path] = []
        for elf in all_elfs:
            reason = _UPSTREAM_TEST_SKIPLIST.get(elf.stem)
            if reason is not None:
                log.info(f"  SKIP: {elf.stem} ({reason})")
            else:
                elfs.append(elf)
        if self.config.deployment_mode == "standalone":
            self._run_functional_standalone(elfs)
        else:
            self._run_functional_non_standalone(elfs)
        log.info("  PASS: xz functional tests")

    def _run_functional_standalone(self, elfs: list[Path]) -> None:
        """Run standalone functional tests using make_initrd.

        Creates an initrd bundling each test ELF with system daemons
        via make_initrd, and a ramfs providing /tmp for any test I/O.
        The ELF is temporarily copied to the repo root because
        make_initrd resolves binary paths relative to it.
        """
        sysroot = self._sysroot_path()
        mkramfs = sysroot / "bin" / "mkramfs.elf"
        nanvixd = sysroot / "bin" / "nanvixd.elf"
        for tool in (mkramfs, nanvixd):
            if not tool.is_file():
                log.fatal(
                    f"functional: {tool} not present",
                    code=EXIT_MISSING_DEP,
                    hint="Re-run `./z setup` to refresh the sysroot.",
                )

        failures: list[str] = []

        for elf in elfs:
            name = elf.stem
            log.info(f"  Running {name}...")
            # make_initrd resolves binaries relative to repo_root;
            # copy the ELF there temporarily unless it already lives there.
            repo_elf = self.repo_root / elf.name
            copied_elf = False
            initrd: Path | None = None
            try:
                if elf.resolve() != repo_elf.resolve():
                    if repo_elf.exists():
                        raise FileExistsError(
                            f"refusing to clobber existing {repo_elf}"
                        )
                    shutil.copy2(elf, repo_elf)
                    copied_elf = True
                initrd = make_initrd(self, elf.name)
                with tempfile.TemporaryDirectory(prefix=f"xz_test_{name}_") as tmp:
                    tmp_path = Path(tmp)
                    ramfs_dir = tmp_path / "ramfs"
                    ramfs_dir.mkdir()
                    (ramfs_dir / "tmp").mkdir()
                    ramfs_img = tmp_path / "rootfs.img"

                    run(
                        str(mkramfs),
                        "-o",
                        str(ramfs_img),
                        str(ramfs_dir),
                        timeout=60,
                    )

                    run(
                        str(sysroot / "bin" / "nanvixd.elf"),
                        "-bin-dir",
                        str(sysroot / "bin"),
                        "-ramfs",
                        str(ramfs_img),
                        "--",
                        str(initrd),
                        timeout=180,
                    )
                log.info(f"  PASS: {name}")
            except SystemExit:
                log.info(f"  FAIL: {name}")
                failures.append(name)
            finally:
                if initrd is not None and initrd.exists():
                    initrd.unlink()
                if copied_elf and repo_elf.exists():
                    repo_elf.unlink()

        if failures:
            log.fatal(
                "functional: FAIL: " + ", ".join(failures),
                code=EXIT_BUILD_FAILURE,
            )

    def _run_functional_non_standalone(self, elfs: list[Path]) -> None:
        """Run functional tests in multi-process or single-process mode.

        Uses nanvixd.elf directly with a ramfs providing /tmp for any
        test I/O.  No initrd is needed as daemons are managed by the
        hypervisor in these modes.
        """
        sysroot = self._sysroot_path()
        nanvixd = sysroot / "bin" / "nanvixd.elf"
        mkramfs = sysroot / "bin" / "mkramfs.elf"
        for tool in (nanvixd, mkramfs):
            if not tool.is_file():
                log.fatal(
                    f"functional: {tool} not present",
                    code=EXIT_MISSING_DEP,
                    hint="Re-run `./z setup` to refresh the sysroot.",
                )

        failures: list[str] = []

        for elf in elfs:
            name = elf.stem
            log.info(f"  Running {name}...")
            try:
                with tempfile.TemporaryDirectory(prefix=f"xz_test_{name}_") as tmp:
                    tmp_path = Path(tmp)
                    ramfs_dir = tmp_path / "ramfs"
                    ramfs_dir.mkdir()
                    (ramfs_dir / "tmp").mkdir()
                    ramfs_img = tmp_path / "rootfs.img"

                    run(
                        str(mkramfs),
                        "-o",
                        str(ramfs_img),
                        str(ramfs_dir),
                        timeout=60,
                    )

                    run(
                        str(nanvixd),
                        "-bin-dir",
                        str(sysroot / "bin"),
                        "-ramfs",
                        str(ramfs_img),
                        "--",
                        str(elf.resolve()),
                        timeout=180,
                    )
                log.info(f"  PASS: {name}")
            except SystemExit:
                log.info(f"  FAIL: {name}")
                failures.append(name)

        if failures:
            log.fatal(
                "functional: FAIL: " + ", ".join(failures),
                code=EXIT_BUILD_FAILURE,
            )

    def _run_tests_windows(self) -> None:
        """Run upstream check_PROGRAMS natively on Windows via nanvixd.exe.

        Only standalone mode is exercised on Windows; multi-process
        and single-process require linuxd which is Linux-only.  Uses
        make_initrd to bundle each test binary with system daemons,
        and a ramfs providing /tmp for any test I/O.
        """
        if self.config.deployment_mode != "standalone":
            print(
                f"Skipping tests on Windows for mode "
                f"'{self.config.deployment_mode}' (requires linuxd)."
            )
            return

        sysroot = self.config.get(CFG_SYSROOT, "")
        if not sysroot:
            log.fatal(
                f"{CFG_SYSROOT} is not set.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first.",
            )
        sysroot_path = Path(sysroot)
        nanvixd = sysroot_path / "bin" / "nanvixd.exe"
        mkramfs = sysroot_path / "bin" / "mkramfs.exe"
        if not nanvixd.is_file():
            log.fatal(
                "nanvixd.exe not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first.",
            )
        if not mkramfs.is_file():
            log.fatal(
                "mkramfs.exe not found.",
                code=EXIT_MISSING_DEP,
                hint="Run `./z setup` first.",
            )

        test_allowlist = {f"{n}.elf" for n in _UPSTREAM_TEST_NAMES}
        # Iterate the full allowlist minus anything in the skiplist;
        # skipped names are logged but not booted.
        iteration_set = test_allowlist - {f"{n}.elf" for n in _UPSTREAM_TEST_SKIPLIST}
        for skipped in sorted(test_allowlist - iteration_set):
            stem = skipped[: -len(".elf")]
            print(f"SKIP {stem} ({_UPSTREAM_TEST_SKIPLIST[stem]})")
        candidates: list[Path] = []
        seen: set[str] = set()
        for d in (
            self.repo_root / "build" / "tests",
            self.repo_root / "build",
            self.repo_root,
        ):
            if d.is_dir():
                for p in sorted(d.glob("*.elf")):
                    if (
                        p.name in test_allowlist
                        and p.name in iteration_set
                        and p.name not in seen
                    ):
                        candidates.append(p)
                        seen.add(p.name)
        if not candidates:
            log.fatal(
                f"No allowlisted test binaries found (expected: "
                f"{sorted(iteration_set)}).",
                code=EXIT_MISSING_DEP,
                hint="Build first via `./z build`.",
            )

        failed: list[str] = []
        for binary in candidates:
            name = binary.stem
            print(f"RUN  {name}...")
            # make_initrd resolves binaries relative to repo_root;
            # copy the ELF there temporarily unless it already lives there.
            repo_elf = self.repo_root / binary.name
            copied_elf = False
            initrd: Path | None = None
            try:
                if binary.resolve() != repo_elf.resolve():
                    if repo_elf.exists():
                        raise FileExistsError(
                            f"refusing to clobber existing {repo_elf}"
                        )
                    shutil.copy2(binary, repo_elf)
                    copied_elf = True
                initrd = make_initrd(self, binary.name)
                with tempfile.TemporaryDirectory(
                    prefix=f"nanvix_{name}_",
                    ignore_cleanup_errors=True,
                ) as tmpdir:
                    tmpdir_path = Path(tmpdir)
                    ramfs_dir = tmpdir_path / "ramfs"
                    ramfs_dir.mkdir()
                    (ramfs_dir / "tmp").mkdir(exist_ok=True)
                    ramfs_img = tmpdir_path / f"rootfs_{name}.img"

                    run(
                        str(mkramfs),
                        "-o",
                        str(ramfs_img),
                        str(ramfs_dir),
                        timeout=60,
                    )

                    run(
                        str(nanvixd),
                        "-bin-dir",
                        str(sysroot_path / "bin"),
                        "-ramfs",
                        str(ramfs_img),
                        "--",
                        str(initrd),
                        timeout=180,
                    )
                print(f"OK   {name}")
            except SystemExit:
                print(f"FAIL {name}")
                failed.append(name)
            finally:
                if initrd is not None and initrd.exists():
                    initrd.unlink()
                if copied_elf and repo_elf.exists():
                    repo_elf.unlink()

        if failed:
            raise RuntimeError(f"{len(failed)} failed: {' '.join(failed)}")
        print(f"\t\t*** All {len(candidates)} tests PASSED ***")

    # ------------------------------------------------------------------
    # Release packaging
    # ------------------------------------------------------------------

    def release(self) -> None:
        """Stage sysroot/{lib,include} into dist/ and tar it up.

        Output layout (mirrors sqlite's ``Makefile.nanvix:233-267``):

            dist/xz-<plat>-<mode>-<mem>/sysroot/lib/liblzma.a
            dist/xz-<plat>-<mode>-<mem>/sysroot/lib/pkgconfig/liblzma.pc
            dist/xz-<plat>-<mode>-<mem>/sysroot/include/lzma.h
            dist/xz-<plat>-<mode>-<mem>/sysroot/include/lzma/*.h
            dist/xz-<plat>-<mode>-<mem>.tar.gz

        Then re-opens the tarball and asserts the four expected paths
        are present (acceptance criterion #4).  Pure-Python tarfile is
        used so the release path has no external ``tar`` dependency on
        Windows runners.
        """
        repo = self.repo_root
        build_dir = repo / "build"
        if not (build_dir / "liblzma.a").is_file():
            log.fatal(
                "build/ artefacts missing; run `./z build` first.",
                code=EXIT_BUILD_FAILURE,
            )

        artifact = (
            f"xz-{self.config.machine}"
            f"-{self.config.deployment_mode}"
            f"-{self.config.memory_size}"
        )
        dist_dir = repo / "dist"
        staging = dist_dir / artifact
        sysroot = staging / "sysroot"

        # Fresh stage every time -- the tarball is the canonical output.
        if staging.exists():
            shutil.rmtree(staging)
        (sysroot / "lib" / "pkgconfig").mkdir(parents=True, exist_ok=True)
        (sysroot / "include" / "lzma").mkdir(parents=True, exist_ok=True)

        # Source -> dest pairs (file copies; no globs to keep the
        # contract explicit).
        copies: list[tuple[Path, Path]] = [
            (build_dir / "liblzma.a", sysroot / "lib" / "liblzma.a"),
            (
                build_dir / "lib" / "pkgconfig" / "liblzma.pc",
                sysroot / "lib" / "pkgconfig" / "liblzma.pc",
            ),
            (build_dir / "include" / "lzma.h", sysroot / "include" / "lzma.h"),
        ]
        for src, dst in copies:
            if not src.is_file():
                log.fatal(
                    f"release: missing input {src}",
                    code=EXIT_BUILD_FAILURE,
                )
            shutil.copy2(src, dst)

        # Header subdirectory (lzma/*.h) -- copytree is fine since the
        # destination was just created empty.
        lzma_subdir_src = build_dir / "include" / "lzma"
        if not lzma_subdir_src.is_dir():
            log.fatal(
                f"release: missing header dir {lzma_subdir_src}",
                code=EXIT_BUILD_FAILURE,
            )
        lzma_subdir_dst = sysroot / "include" / "lzma"
        if lzma_subdir_dst.exists():
            shutil.rmtree(lzma_subdir_dst)
        shutil.copytree(lzma_subdir_src, lzma_subdir_dst)

        # Build the gzip-compressed tarball; arcname strips the
        # staging dir prefix so paths inside the archive begin at
        # ``sysroot/``.
        tarball = dist_dir / f"{artifact}.tar.gz"
        if tarball.exists():
            tarball.unlink()
        with tarfile.open(tarball, "w:gz") as tf:
            tf.add(sysroot, arcname="sysroot")
        log.info(f"Wrote release tarball: {tarball}")

        self._verify_release(tarball)

    def _verify_release(self, tarball: Path) -> None:
        """Re-open the tarball and assert the four required paths exist.

        Mirrors sqlite's ``verify-package`` step.  Anything missing
        here fails CI before the artefact is uploaded, so a downstream
        port (cpython) never sees a half-empty release.
        """
        required = {
            "sysroot/lib/liblzma.a",
            "sysroot/lib/pkgconfig/liblzma.pc",
            "sysroot/include/lzma.h",
        }
        # The header subdirectory is enforced by membership: at least
        # one entry beneath sysroot/include/lzma/ must be present.
        with tarfile.open(tarball, "r:gz") as tf:
            members = tf.getnames()
        present = set(members)
        missing = sorted(required - present)
        if missing:
            log.fatal(
                f"release: tarball missing required paths: {missing}",
                code=EXIT_BUILD_FAILURE,
                hint=f"Tarball: {tarball}",
            )
        if not any(
            m.startswith("sysroot/include/lzma/") and m != "sysroot/include/lzma"
            for m in members
        ):
            log.fatal(
                "release: tarball has no entries under sysroot/include/lzma/",
                code=EXIT_BUILD_FAILURE,
                hint=f"Tarball: {tarball}",
            )
        log.info(f"Verified release tarball: {tarball}")

    def clean(self) -> None:
        """Remove build artefacts and the configure sentinel."""
        repo = self.repo_root
        marker = repo / _CONFIGURED_MARKER
        if marker.exists():
            marker.unlink()
        for name in ("build", "dist"):
            d = repo / name
            if d.is_dir():
                shutil.rmtree(d)
        # Best-effort upstream make clean; ignore failures (e.g., when
        # ./configure has never run, no Makefile exists yet).
        if (repo / "Makefile").exists():
            subprocess.run(
                ["make", "distclean"],
                cwd=repo,
                check=False,
            )


if __name__ == "__main__":
    XzBuild.main()
