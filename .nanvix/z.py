# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

"""Nanvix build script for XZ Utils.

Usage:
    ./z setup     # Resolve toolchain + download sysroot, prepare upstream tree
    ./z build    # Cross-compile liblzma.a
    ./z test     # Three-tier smoke + integration + functional ladder
    ./z release  # Stage sysroot/{lib,include} + emit dist/*.tar.bz2
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
    CFG_TOOLCHAIN,
    EXIT_BUILD_FAILURE,
    EXIT_MISSING_DEP,
    ZScript,
    log,
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

# Default toolchain prefix when CFG_TOOLCHAIN is not set.  Matches the
# default used by nanvix-zutil's docker layer and by sibling ports.
_DEFAULT_TOOLCHAIN = "/opt/nanvix"

IS_WINDOWS = sys.platform == "win32"


class XzBuild(ZScript):
    """Build script for nanvix/xz."""

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _toolchain_path(self) -> Path:
        """Return the resolved cross-toolchain prefix."""
        return Path(
            self.config.get(CFG_TOOLCHAIN, _DEFAULT_TOOLCHAIN) or _DEFAULT_TOOLCHAIN
        )

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
        toolchain = self.translate_path(self._toolchain_path())
        sysroot = self.translate_path(self._sysroot_path())
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
                    "Configure inputs changed since last build; "
                    "re-running ./configure."
                )
            env = dict(os.environ)
            env.update(overrides)
            # self.run() defaults to docker=True; when `./z setup --with-docker`
            # was used, the toolchain image persists and both ./configure and
            # make below run inside the nanvix/toolchain container.  No host
            # autotools or compiler is needed for the configure/build path.
            self.run(
                "./configure",
                *opts,
                cwd=self.repo_root,
                env=env,
            )
            marker.write_text(wanted)

        # Build.
        try:
            nproc = str(os.cpu_count() or 1)
        except Exception:
            nproc = "1"
        self.run("make", f"-j{nproc}", cwd=self.repo_root)

        # Stage a curated install image and copy the subset we ship into
        # build/ at the layout the schema/release path expects.
        self._stage_artefacts()
        # Build the smoke.elf test binary now, while we are inside the
        # docker-wrapped build context.  ``nanvix-zutil test`` is
        # deliberately host-only (script.py:730 — "test and benchmark
        # run on the host"), so the cross-compiler is unreachable from
        # the test step in CI when --with-docker was used during setup.
        # By producing build/smoke.elf here, the test tiers below only
        # need to *execute* a pre-built artefact on the host (via
        # nanvixd.elf, which is a Linux host binary in the sysroot).
        self._build_smoke()

    def _stage_artefacts(self) -> None:
        """Install into build/_install and copy outputs into build/."""
        repo = self.repo_root
        build_dir = repo / "build"
        stage = repo / _INSTALL_STAGE_REL

        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)

        self.run(
            "make",
            "install",
            f"DESTDIR={self.translate_path(stage)}",
            cwd=repo,
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
        """Build smoke.elf and confirm it links + is statically linked."""
        log.info("=== xz integration tests ===")
        smoke_elf = self._build_smoke()
        size = smoke_elf.stat().st_size
        if size < 50_000:
            log.fatal(
                f"integration: smoke.elf too small ({size} bytes)",
                code=EXIT_BUILD_FAILURE,
            )
        try:
            file_out = subprocess.run(
                ["file", str(smoke_elf)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.lower()
        except (FileNotFoundError, subprocess.CalledProcessError):
            file_out = ""
        if "elf" not in file_out:
            log.fatal(
                f"integration: smoke.elf is not an ELF binary ({file_out!r})",
                code=EXIT_BUILD_FAILURE,
            )
        if "statically" not in file_out:
            log.info("  WARN: file(1) did not report 'statically linked'")
        log.info(f"  OK: smoke.elf ({size} bytes, ELF)")
        log.info("  PASS: xz integration tests")

    def _test_functional(self) -> None:
        """Run smoke.elf inside nanvixd.elf and grep for XZ_SMOKE_OK.

        Mirrors the three-arm shape from sqlite's Makefile.nanvix:
        Linux+Docker (kvm-wrapped run via the toolchain image), Linux
        host (native nanvixd.elf out of the sysroot), and standalone
        ramfs build for any process mode (the kernel always boots from
        a ramfs; the difference between modes is the in-VM workload).
        """
        log.info("=== xz functional tests ===")
        smoke_elf = self._build_smoke()
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

        with tempfile.TemporaryDirectory(prefix="xz_smoke_") as tmp:
            tmp_path = Path(tmp)
            ramfs_dir = tmp_path / "ramfs"
            ramfs_dir.mkdir()
            (ramfs_dir / "tmp").mkdir()
            shutil.copy2(smoke_elf, ramfs_dir / "smoke.elf")
            ramfs_img = tmp_path / "rootfs.img"
            subprocess.run(
                [str(mkramfs), "-o", str(ramfs_img), str(ramfs_dir)],
                check=True,
                timeout=60,
            )
            cmd = [
                "timeout",
                "--foreground",
                "120",
                str(nanvixd),
                "-bin-dir",
                str(sysroot / "bin"),
                "-ramfs",
                str(ramfs_img),
                # nanvixd loads the user binary via host-side mmap()
                # rather than from inside the ramfs, so this must be an
                # absolute host path (the ramfs only carries auxiliary
                # state like /tmp).
                "--",
                str(smoke_elf.resolve()),
            ]
            log.info(f"$ {' '.join(cmd)}")
            result = subprocess.run(
                cmd,
                timeout=180,
                capture_output=True,
                text=True,
            )
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            if result.returncode != 0:
                log.fatal(
                    f"functional: nanvixd.elf exited {result.returncode}",
                    code=EXIT_BUILD_FAILURE,
                )
            if "XZ_SMOKE_OK" not in result.stdout:
                log.fatal(
                    "functional: smoke.elf did not print XZ_SMOKE_OK",
                    code=EXIT_BUILD_FAILURE,
                )
        log.info("  PASS: xz functional tests")

    def _build_smoke(self) -> Path:
        """Compile .nanvix/tests/smoke.c into build/smoke.elf.

        Statically links against ``liblzma.a`` plus the standard Nanvix
        triplet (``libposix``, ``libc``, ``libm``) using the canonical
        linker recipe (``-T <sysroot>/lib/user.ld``).  Idempotent: if
        ``smoke.elf`` is newer than ``smoke.c`` and ``liblzma.a`` it
        returns immediately.

        The cross-compiler invocation is routed through ``self.run`` so
        it transparently dispatches into the toolchain Docker container
        when ``./z setup --with-docker`` was used (the default in CI,
        where ``i686-nanvix-gcc`` exists only inside the image and not
        on the host runner).  All absolute paths handed to gcc are
        translated via ``self.translate_path`` so they resolve correctly
        on both sides of the docker boundary.
        """
        toolchain = self._toolchain_path()
        sysroot = self._sysroot_path()
        src = self.nanvix_dir / "tests" / "smoke.c"
        build_dir = self.repo_root / "build"
        liblzma = build_dir / "liblzma.a"
        smoke_elf = build_dir / "smoke.elf"
        include_dir = build_dir / "include"

        if not src.is_file():
            log.fatal(
                f"smoke source missing: {src}",
                code=EXIT_BUILD_FAILURE,
            )
        if not liblzma.is_file():
            log.fatal(
                f"liblzma.a missing at {liblzma}; run `./z build` first.",
                code=EXIT_BUILD_FAILURE,
            )
        if smoke_elf.is_file():
            inputs_mtime = max(src.stat().st_mtime, liblzma.stat().st_mtime)
            if smoke_elf.stat().st_mtime >= inputs_mtime:
                return smoke_elf

        log.info(f"Compiling smoke.elf -> {smoke_elf}")
        # translate_path() returns the host path unchanged when docker
        # mode is inactive, and the container mount-point when it is.
        # Same pattern as _configure_env_overrides() above.
        gcc_p = self.translate_path(toolchain / "bin" / "i686-nanvix-gcc")
        sysroot_p = self.translate_path(sysroot)
        include_p = self.translate_path(include_dir)
        src_p = self.translate_path(src)
        smoke_elf_p = self.translate_path(smoke_elf)
        liblzma_p = self.translate_path(liblzma)
        self.run(
            str(gcc_p),
            "-O2",
            "-Wall",
            f"-I{include_p}",
            "-static",
            f"-T{sysroot_p}/lib/user.ld",
            f"-L{sysroot_p}/lib",
            "-o",
            str(smoke_elf_p),
            str(src_p),
            "-Wl,--start-group",
            "-lposix",
            "-lc",
            "-lm",
            str(liblzma_p),
            "-Wl,--end-group",
            cwd=self.repo_root,
        )
        return smoke_elf

    def _run_tests_windows(self) -> None:
        """Run smoke.elf natively on Windows via nanvixd.exe.

        Only standalone mode is exercised on Windows; multi-process
        and single-process require linuxd which is Linux-only.  The
        binary discovery allowlist is restricted to ``smoke.elf`` so
        spurious ELFs in the tree are not booted.
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

        # On Windows we are a pure consumer of the Linux build job's artefact
        # transfer (`actions/upload-artifact: '**/*.elf'` in the reusable
        # workflow's build step).  liblzma.a, lzma.h, and liblzma.pc are not
        # transferred -- only ELFs are -- and the cross-toolchain isn't
        # installed on Windows runners.  The smoke and integration tiers
        # validate the *build environment* (artefact presence + sizes,
        # smoke.elf compile + ELF-magic sniff) and already ran on the Linux
        # build job before the artefact was uploaded.  Re-running them here
        # would only fail spuriously.  Mirror nanvix/zlib (`example.elf`)
        # and nanvix/sqlite (`sqlite3.elf`): discover the pre-built smoke.elf
        # via the allowlist and run the functional tier (boot via
        # nanvixd.exe + grep stdout for XZ_SMOKE_OK) only.

        test_allowlist = {"smoke.elf"}
        candidates: list[Path] = []
        for d in (self.repo_root / "build", self.repo_root):
            if d.is_dir():
                for p in sorted(d.glob("*.elf")):
                    if p.name in test_allowlist and p.name not in {
                        x.name for x in candidates
                    }:
                        candidates.append(p)
        if not candidates:
            log.fatal(
                f"No allowlisted test binaries found (expected: {sorted(test_allowlist)}).",
                code=EXIT_MISSING_DEP,
                hint="Build first via `./z build`.",
            )

        failed: list[str] = []
        for binary in candidates:
            name = binary.stem
            print(f"RUN  {name}...")
            with tempfile.TemporaryDirectory(
                prefix=f"nanvix_{name}_",
                ignore_cleanup_errors=True,
            ) as tmpdir:
                tmpdir_path = Path(tmpdir)
                ramfs_dir = tmpdir_path / "ramfs"
                ramfs_dir.mkdir()
                (ramfs_dir / "tmp").mkdir(exist_ok=True)
                shutil.copy2(binary, ramfs_dir / binary.name)
                ramfs_img = tmpdir_path / f"rootfs_{name}.img"
                try:
                    subprocess.run(
                        [str(mkramfs.resolve()), "-o", str(ramfs_img), str(ramfs_dir)],
                        check=True,
                        timeout=60,
                    )
                except subprocess.CalledProcessError as e:
                    print(f"FAIL {name} (mkramfs exit code {e.returncode})")
                    failed.append(name)
                    continue
                except subprocess.TimeoutExpired:
                    print(f"FAIL {name} (mkramfs timeout)")
                    failed.append(name)
                    continue
                try:
                    result = subprocess.run(
                        [
                            str(nanvixd.resolve()),
                            "-bin-dir",
                            str((sysroot_path / "bin").resolve()),
                            "-ramfs",
                            str(ramfs_img),
                            # Pass the absolute host path (matches
                            # nanvix/sqlite's working Windows pattern).
                            # Earlier versions used the in-ramfs relative
                            # path `./smoke.elf` (zlib's pattern) but that
                            # caused nanvixd.exe to exit 255 right after
                            # boot for this binary, while sqlite's host-path
                            # form works on the same Windows runner.
                            "--",
                            str(binary.resolve()),
                        ],
                        stdin=subprocess.DEVNULL,
                        timeout=180,
                    )
                    # Inherit stdout/stderr (matches nanvix/zlib +
                    # nanvix/sqlite). Capturing via anonymous pipes makes
                    # nanvixd.exe abort with exit 255 on Windows before the
                    # guest kernel boots, because its "interactive mode"
                    # console wiring requires real Windows console handles.
                    # smoke.c already returns non-zero on any liblzma error
                    # or round-trip mismatch, so the exit code is sufficient
                    # to gate pass/fail; the XZ_SMOKE_OK sentinel was only
                    # ever a belt-and-suspenders check.
                    if result.returncode != 0:
                        print(f"FAIL {name} (exit code {result.returncode})")
                        failed.append(name)
                    else:
                        print(f"OK   {name}")
                except subprocess.TimeoutExpired:
                    print(f"FAIL {name} (timeout)")
                    failed.append(name)

        if failed:
            raise RuntimeError(f"{len(failed)} test(s) failed: {' '.join(failed)}")
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
            dist/xz-<plat>-<mode>-<mem>.tar.bz2

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

        # Build the bzip2-compressed tarball; arcname strips the
        # staging dir prefix so paths inside the archive begin at
        # ``sysroot/``.
        tarball = dist_dir / f"{artifact}.tar.bz2"
        if tarball.exists():
            tarball.unlink()
        with tarfile.open(tarball, "w:bz2") as tf:
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
        with tarfile.open(tarball, "r:bz2") as tf:
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
