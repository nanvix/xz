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
import shlex
import shutil
import subprocess
import sys
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
    load_manifest,
    package,
)
from nanvix_zutil.paths import (
    include_out,
    lib_out,
    repo_root,
    test_out,
    dist_dir,
    release_dir,
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
        # Since Nanvix 0.16.19, process startup (_start) lives in
        # libnvx_crt0.a (it drives __nanvix_libc_start_main in libposix.a)
        # and must be linked first so its strong _start overrides the
        # toolchain's weak no-op stub; otherwise the guest never reaches
        # main and hangs.  Probe the host sysroot so this is a no-op on
        # older Nanvix releases that do not ship the archive.
        crt0_host = os.path.join(self._sysroot_path(), "lib", "libnvx_crt0.a")
        crt0 = f"{sysroot}/lib/libnvx_crt0.a " if os.path.exists(crt0_host) else ""
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
                f"-Wl,--allow-multiple-definition "
                f"-Wl,--start-group {crt0}-lposix -lc -lm -Wl,--end-group"
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
        configure = repo_root() / "configure"
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
        """Cross-compile liblzma.a via the upstream autotools.

        Runs the entire autotools sequence (./configure -> make -> make
        install -> tests/check_PROGRAMS) inside a single ``docker run``
        invocation.  This is required for correctness under zutil's
        Windows tar-copy docker mode, where the workspace is staged
        into an ephemeral ``/tmp/build`` for each container start --
        splitting the sequence across multiple ``run()`` calls would
        lose ./configure's outputs (Makefile, config.h, libtool, ...)
        before ``make`` ever sees them.  Final artefacts persist
        because ``make install DESTDIR=...`` and the test-ELF copy-back
        write through the bind-mounted workspace.
        """
        repo = repo_root()
        opts = self._configure_opts()
        overrides = self._configure_env_overrides()

        try:
            nproc = str(os.cpu_count() or 1)
        except Exception:
            nproc = "1"

        # Stage path translated for the container: make install writes
        # through the workspace mount so build/_install/ lands on the
        # host filesystem regardless of docker copy mode.
        stage_host = repo / _INSTALL_STAGE_REL
        if stage_host.exists():
            shutil.rmtree(stage_host)
        stage_host.mkdir(parents=True)
        stage_container = (
            self.docker.translate_path(stage_host) if self.docker else stage_host
        )

        # Test-ELF destination directory, likewise translated.
        tests_dest_host = repo / "build" / "tests"
        tests_dest_host.mkdir(parents=True, exist_ok=True)
        tests_dest_container = (
            self.docker.translate_path(tests_dest_host)
            if self.docker
            else tests_dest_host
        )

        # See _build_upstream_tests' historical comment block: drop the
        # link group from LDFLAGS at make-time for the tests target and
        # carry it via LIBS so libtool preserves positional ordering.
        sysroot = (
            self.docker.translate_path(self._sysroot_path())
            if self.docker
            else self._sysroot_path()
        )
        # `-all-static`, not a bare `-static`: libtool treats `-static` as
        # "prefer static *libtool* libraries" and never forwards it to the
        # compiler driver, so the link silently goes dynamic now that the
        # 0.17.x sysroot ships a libc.so.  A dynamic executable gets an INTERP
        # and a PHDR-bearing first PT_LOAD that the linker maps one page
        # *below* user.ld's BASE_ADDR (0x40000000); nanvixd then rejects it at
        # load time with "do_elf32_load() invalid load address".  `-all-static`
        # makes libtool pass its link_static_flag (`-static`) through to gcc,
        # producing a fully static ELF whose first LOAD sits exactly at
        # BASE_ADDR.  Harmless pre-0.17 (no libc.so meant the link was already
        # static).
        tests_ldflags = f"-all-static -T{sysroot}/lib/user.ld -L{sysroot}/lib -Wl,--allow-multiple-definition"
        # See _configure_env_overrides: link libnvx_crt0.a first (guarded so
        # it is a no-op on Nanvix releases that predate the crt0 cutover).
        crt0_host = os.path.join(self._sysroot_path(), "lib", "libnvx_crt0.a")
        crt0 = f"{sysroot}/lib/libnvx_crt0.a," if os.path.exists(crt0_host) else ""
        # Link the cross-toolchain's newlib libc/libm explicitly instead of a
        # bare -lc/-lm.  Since Nanvix 0.17.x the release sysroot ships its own
        # libc.a/libm.a (the Rust "nanvix_libc"), and because the link passes
        # -L{sysroot}/lib first a plain -lc would bind there.  That libc
        # dropped newlib's global reentrancy pointer `_impure_ptr`, while the
        # upstream xz tests are compiled against the toolchain's newlib
        # <stdio.h> (the sysroot ships no headers) where `stderr` expands to
        # `_impure_ptr` -- so the test ELFs fail to link with
        # "undefined reference to `_impure_ptr'".  Pinning libc.a/libm.a to the
        # toolchain keeps the whole stdio stack on newlib (a self-consistent
        # FILE model); newlib's stream cookies call POSIX write/read/lseek,
        # which nanvix_libc/libposix still exports, so runtime I/O still works.
        # Stopgap until nanvix/nanvix#2683 restores `_impure_ptr` (or the
        # sysroot ships matching headers).  No-op on pre-0.17 sysroots that
        # never shipped their own libc and where -lc already resolved here.
        # $NEWLIB_LIBC/$NEWLIB_LIBM are resolved by the build script below; the
        # `-print-file-name` probe must run WITHOUT -L{sysroot}/lib so it
        # returns the toolchain newlib path, not the sysroot's libc.a.
        cc = f"{TOOLCHAIN_CONTAINER_PATH}/bin/i686-nanvix-gcc"
        tests_libs = (
            f"-Wl,--start-group,{crt0}-lposix,$NEWLIB_LIBC,$NEWLIB_LIBM,--end-group"
        )

        configure_cmd = " ".join(["./configure", *(shlex.quote(o) for o in opts)])
        tests_targets = " ".join(_UPSTREAM_TEST_NAMES)

        # Single shell script: configure -> make -> install ->
        # tests-build -> copy test ELFs out to the mounted workspace.
        script = "\n".join(
            [
                "set -e",
                configure_cmd,
                f"make -j{nproc}",
                f"make install DESTDIR={shlex.quote(str(stage_container))}",
                # Resolve the toolchain's newlib libc/libm (see tests_libs).
                # No -L flags here: -print-file-name must ignore the sysroot.
                f"NEWLIB_LIBC=$({shlex.quote(cc)} -print-file-name=libc.a)",
                f"NEWLIB_LIBM=$({shlex.quote(cc)} -print-file-name=libm.a)",
                (
                    "make -C tests "
                    f"LDFLAGS={shlex.quote(tests_ldflags)} "
                    f'LIBS="{tests_libs}" '
                    f"{tests_targets} -j{nproc}"
                ),
                f"mkdir -p {shlex.quote(str(tests_dest_container))}",
                # libtool drops the unwrapped ELF under tests/.libs/<name>;
                # in the rare case it inlines the binary directly into
                # tests/<name> (no shared-library wrapper needed) fall
                # back to the flat path.
                "for name in " + tests_targets + "; do",
                "  src=tests/.libs/$name",
                '  [ -f "$src" ] || src=tests/$name',
                '  if [ ! -f "$src" ]; then',
                '    echo "upstream test binary missing: $name" >&2',
                "    exit 1",
                "  fi",
                f'  cp -f "$src" {shlex.quote(str(tests_dest_container))}/$name.elf',
                "done",
            ]
        )

        env = dict(os.environ)
        env.update(overrides)
        run(
            "sh",
            "-c",
            script,
            cwd=repo,
            env=env,
            docker=self.docker,
        )

        # Refresh the marker now that configure/make/install all
        # succeeded in lockstep.  Kept for diagnostic value (`cat
        # .nanvix-configured` shows the configure inputs of the last
        # successful build); no longer gates re-execution.
        marker = repo / _CONFIGURED_MARKER
        marker.write_text(self._configured_marker_contents(opts, overrides))

        # Host-only: shuffle the install image into the layout the
        # release/packaging step expects under build/.
        self._stage_artefacts()

    def _stage_artefacts(self) -> None:
        """Copy install image outputs from build/_install into build/.

        Host-only: ``make install DESTDIR=<build/_install>`` is driven
        from :meth:`build` as part of the single docker invocation, so
        by the time we get here ``build/_install/sysroot/`` already
        exists on the host filesystem (written through the bind mount).
        """
        repo = repo_root()
        build_dir = repo / "build"
        stage = repo / _INSTALL_STAGE_REL

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

        # Also stage into .nanvix/out/release/{lib,include} and
        # .nanvix/out/test/ so the inherited ZScript.release() packages
        # them and `./z test` consumers can find the test ELFs in the
        # canonical location.  These are pure host-side copies; the
        # files in build/ are read-only inputs to the staging step.
        self._stage_release_outputs()

    def _stage_release_outputs(self) -> None:
        """Mirror build/{lib,include} into lib_out()/include_out() and
        build/tests/*.elf into test_out() for the inherited release().
        """
        build_dir = repo_root() / "build"
        lib_dir = build_dir / "lib"
        include_dir = build_dir / "include"
        tests_dir = build_dir / "tests"

        lib_o = lib_out()
        inc_o = include_out()
        tst_o = test_out()
        (lib_o / "pkgconfig").mkdir(parents=True, exist_ok=True)
        (inc_o / "lzma").mkdir(parents=True, exist_ok=True)
        tst_o.mkdir(parents=True, exist_ok=True)

        shutil.copy2(lib_dir / "liblzma.a", lib_o / "liblzma.a")
        shutil.copy2(
            lib_dir / "pkgconfig" / "liblzma.pc",
            lib_o / "pkgconfig" / "liblzma.pc",
        )
        shutil.copy2(include_dir / "lzma.h", inc_o / "lzma.h")
        lzma_dst = inc_o / "lzma"
        if lzma_dst.exists():
            shutil.rmtree(lzma_dst)
        shutil.copytree(include_dir / "lzma", lzma_dst)

        for elf in sorted(tests_dir.glob("*.elf")):
            shutil.copy2(elf, tst_o / elf.name)

        log.info(f"Staged release outputs under {lib_o.parent}")

    def _expected_test_paths(self) -> list[Path]:
        """Return the expected ``build/tests/<name>.elf`` paths.

        Pure path computation -- does not touch the filesystem and
        does not invoke any subprocess.  Used by both the build-time
        cross-compile and the host-side test runner to agree on where
        the upstream test ELFs live.
        """
        dest_dir = repo_root() / "build" / "tests"
        return [dest_dir / f"{name}.elf" for name in _UPSTREAM_TEST_NAMES]

    def _locate_upstream_tests(self) -> list[Path]:
        """Return the cached upstream test ELFs (host-side; no docker).

        Asserts every expected ELF exists; if any are missing, fails
        with a hint to run ``./z build`` first.  Used by the test
        runner, which must never reach the cross-toolchain.
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

    # ------------------------------------------------------------------
    # Tests
    # ------------------------------------------------------------------

    def test(self) -> None:
        """Run the upstream ``check_PROGRAMS`` functional tests.

        Functional tests boot each upstream test binary under
        ``nanvixd`` and therefore exercise the full stack (artefact
        presence, ELF validity, and runtime behaviour), so no
        separate smoke or integration tier is provided.

        Any positional arguments (formerly used to select tiers via
        ``./z test -- <tier>``) are rejected loudly so legacy callers
        that still pass ``test-smoke`` / ``test-integration`` /
        ``test-functional`` fail fast with an actionable message
        instead of silently running the full suite.
        """
        if self.targets:
            log.fatal(
                f"./z test takes no arguments (got: {' '.join(self.targets)})",
                code=EXIT_BUILD_FAILURE,
                hint=(
                    "The smoke and integration tiers were removed; "
                    "`./z test` now always runs the functional tier. "
                    "Drop any `test-smoke`/`test-integration`/"
                    "`test-functional` arguments from your invocation."
                ),
            )
        if IS_WINDOWS:
            self._run_tests_windows()
            return
        self._test_functional()

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
            repo_elf = repo_root() / elf.name
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
                initrd = make_initrd(self, elf.name, test=True)
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
            repo_root() / "build" / "tests",
            repo_root() / "build",
            repo_root(),
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
            repo_elf = repo_root() / binary.name
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
                initrd = make_initrd(self, binary.name, test=True)
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

    def release(self) -> None:
        """Package the release archive named per build configuration.

        The base :meth:`ZScript.release` packages ``release_dir()`` under the
        bare package name, so every matrix configuration emits an
        identically-named archive; in CI these collide and overwrite one
        another, leaving the published release with only generic assets.
        Dependents resolve assets by the pattern
        ``{name}-{machine}-{mode}-{mem}`` (e.g.
        ``{name}-microvm-multi-process-128mb``), so the archive must carry that
        name for dependency installation to succeed.
        """
        manifest = load_manifest()
        name = (
            f"{manifest.name}"
            f"-{self.config.machine}"
            f"-{self.config.deployment_mode}"
            f"-{self.config.memory_size}"
        )
        package([release_dir()], dist_dir(), name)

    def clean(self) -> None:
        """Remove build artefacts and the configure sentinel."""
        repo = repo_root()
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
