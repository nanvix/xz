# Copyright(c) The Maintainers of Nanvix.
# Licensed under the MIT License.

"""Nanvix build script for XZ Utils.

Usage:
    ./z setup     # Resolve toolchain + download sysroot, prepare upstream tree
    ./z build    # Cross-compile liblzma.a
    ./z clean    # Remove build artefacts
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
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
        return Path(self.config.get(CFG_TOOLCHAIN, _DEFAULT_TOOLCHAIN))

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
            "AR":     f"{bin_}/i686-nanvix-ar",
            "AS":     f"{bin_}/i686-nanvix-as",
            "CC":     f"{bin_}/i686-nanvix-gcc",
            "CXX":    f"{bin_}/i686-nanvix-g++",
            "CPP":    f"{bin_}/i686-nanvix-gcc -E",
            "LD":     f"{bin_}/i686-nanvix-ld",
            "RANLIB": f"{bin_}/i686-nanvix-ranlib",
            "STRIP":  f"{bin_}/i686-nanvix-strip",
            "NM":     f"{bin_}/i686-nanvix-nm",
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
            "LIBS":   "",
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
        """Generate ./configure via autogen.sh if upstream did not ship it.

        Tarball releases of xz ship a pre-generated configure; git
        checkouts do not.  Probe for ./configure and run autogen.sh
        only when missing, so checked-out trees Just Work.
        """
        configure = self.repo_root / "configure"
        if configure.exists():
            return
        autogen = self.repo_root / "autogen.sh"
        if not autogen.exists():
            log.fatal(
                "Neither ./configure nor ./autogen.sh present.",
                code=EXIT_BUILD_FAILURE,
                hint="The upstream tree appears incomplete.",
            )
        log.info("./configure missing -- running autogen.sh --no-po4a")
        self.run("sh", "./autogen.sh", "--no-po4a", docker=False)

    def _patch_config_sub(self) -> None:
        """Idempotently teach build-aux/config.sub about i686-nanvix.

        Mirrors the libffi port's Makefile.nanvix sed recipe.  config.sub
        is autotools-generated and excluded from the upstream-byte-identity
        invariant, so this in-tree mutation does not warrant a row in
        NANVIX.md's source-changes table.
        """
        cs = self.repo_root / "build-aux" / "config.sub"
        if not cs.exists():
            return  # autogen.sh hasn't run yet; will be retried after configure
        text = cs.read_text()
        if "nanvix" in text:
            return
        # libffi precedent: extend the existing fiwix* arm of the OS table.
        new_text, n = re.subn(
            r"(\| fiwix\* )(\))",
            r"\1| nanvix* \2",
            text,
            count=1,
        )
        if n == 0:
            log.fatal(
                "Failed to patch build-aux/config.sub: fiwix* anchor not found.",
                code=EXIT_BUILD_FAILURE,
                hint="config.sub layout differs from the expected GNU template.",
            )
        cs.write_text(new_text)
        log.info("Patched build-aux/config.sub for i686-nanvix")

    # ------------------------------------------------------------------
    # ZScript hook overrides
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Resolve sysroot/toolchain and prepare the autotools tree."""
        super().setup()
        self._ensure_configure()
        self._patch_config_sub()

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
            self._patch_config_sub()  # re-run in case setup() was skipped
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

    def _stage_artefacts(self) -> None:
        """Install into build/_install and copy outputs into build/."""
        repo = self.repo_root
        build_dir = repo / "build"
        stage = repo / _INSTALL_STAGE_REL

        if stage.exists():
            shutil.rmtree(stage)
        stage.mkdir(parents=True)

        self.run(
            "make", "install", f"DESTDIR={self.translate_path(stage)}",
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
