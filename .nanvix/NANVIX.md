# XZ Utils Port for Nanvix

> **TL;DR:** Port of [XZ Utils](https://tukaani.org/xz/) 5.2.5 to the
> [Nanvix](https://github.com/nanvix/nanvix) operating system. Ships
> `liblzma.a` (static library), `lzma.h` + `lzma/*.h` headers, and
> `liblzma.pc` (pkg-config). The primary consumer is CPython's `_lzma`
> extension. Jump to
> [Quick Start](#quick-start) to build immediately.

<!-- METADATA
project: xz
upstream-url: https://tukaani.org/xz/
upstream-repo: tukaani-project/xz
upstream-version: 5.2.5
target-os: nanvix
target-arch: i686
nanvix-version: 0.20.0
build-system: autoconf (Python-driven via .nanvix/z.py)
output-type: static-library
license: GPLv2 / GPLv3 / LGPLv2.1 (per upstream COPYING.*)
-->

---

## Overview

This document describes the port of the XZ Utils compression suite to
Nanvix. The build is driven by `.nanvix/z.py` (a `nanvix-zutil`
`ZScript` subclass) which shells out directly to the upstream
`./configure` and `make`; there is intentionally **no
`Makefile.nanvix`** at the repository root.

| Property | Value |
|----------|-------|
| **Base Version** | XZ Utils 5.2.5 |
| **Target Platform** | Nanvix (i686) |
| **Build System** | autoconf (Python-driven via `.nanvix/z.py`) |
| **SDK** | `nanvix-sdk-c-clang` v0.20.0-sdk.1 |
| **Outputs** | `liblzma.a`, `liblzma.pc`, `lzma.h`, `lzma/*.h` |
| **Dependencies** | none |

---

## Table of Contents

1. [Quick Start](#quick-start)
2. [Prerequisites](#prerequisites)
3. [Building](#building)
4. [Testing](#testing)
5. [Source Changes from Upstream](#source-changes-from-upstream)
6. [Known Limitations](#known-limitations)
7. [CI/CD](#cicd)

---

## Quick Start

```bash
# The ./z wrapper auto-bootstraps nanvix-zutil into .nanvix/venv/ on first run.
# Requires: python3
./z setup     # Resolve the SDK + runtime sysroot, prepare upstream tree
./z build     # Cross-compile liblzma.a
./z test      # Run upstream C tests under Nanvix
./z release   # Package release tarball under dist/
./z clean     # Remove build artefacts
```

Override the pinned `nanvix-zutil` version with
`NANVIX_ZUTIL_VERSION=<version>`.

---

## Prerequisites

| Component | Description | Default Location |
|-----------|-------------|------------------|
| **Nanvix C SDK** | Clang/LLVM compiler, headers, libraries, and linker script | `/opt/nanvix` (or `$NANVIX_TOOLCHAIN`) |
| **Nanvix Runtime Sysroot** | Runtime binaries used by tests | resolved by `./z setup` |
| **Python 3** | Required to bootstrap `nanvix-zutil` | on `PATH` |

XZ has no third-party dependencies.

The build uses Clang, `clang-cpp`, `ld.lld`, and the LLVM binutils from
`ghcr.io/nanvix/nanvix-sdk-c-clang@sha256:f61737cb0780e6a2058c6d0bdf8ae5562db18de437173b2bcbbe6973abd3689f`.
Executable links go through the Clang driver, which supplies the SDK's
startup object, libc, compiler runtime, and linker script. Downloaded
Nanvix artifacts are runtime-only.

---

## Building

```bash
./z setup
./z build
```

Produces under `build/`:

| File | Description |
|------|-------------|
| `build/liblzma.a` | XZ static library |
| `build/include/lzma.h` | Public API header |
| `build/include/lzma/*.h` | Public API sub-headers |
| `build/lib/pkgconfig/liblzma.pc` | pkg-config descriptor |

---

## Testing

`./z build` cross-compiles the upstream `tests/check_PROGRAMS` with the
SDK Clang driver. `./z test` runs every resulting
`build/tests/test_*.elf` under `nanvixd.elf`; PASS requires every enabled
test to exit successfully. Shell-driven upstream tests remain disabled
because the runtime test image does not provide a POSIX shell.

All six upstream C tests (`test_check`, `test_stream_flags`,
`test_filter_flags`, `test_block_header`, `test_index`,
`test_bcj_exact_size`) pass under `nanvixd.elf` as of 2026-07-12;
the skip-list (`_UPSTREAM_TEST_SKIPLIST` in `.nanvix/z.py`) is
therefore empty.

```bash
./z test
```

---

## Source Changes from Upstream

The upstream XZ Utils 5.2.5 tree is preserved byte-identical outside
the Nanvix-specific surface (`.nanvix/`, `.github/workflows/nanvix-*`,
root wrappers `z`/`z.sh`/`z.ps1`, `NANVIX.md`, and `.gitignore`) **plus**
the vendored autotools-generated outputs listed below, which xz upstream
intentionally does not check into git but which the Nanvix port commits
directly so that the toolchain Docker image does not need autoconf,
autopoint, libtool, automake, or gettext at build time.

One single-line source patch is applied to `configure.ac`:

| File | Change | Rationale |
|------|--------|-----------|
| `configure.ac` | Insert `AM_MAINTAINER_MODE([disable])` after `AM_INIT_AUTOMAKE` | Gates automake's autoconf/aclocal/automake rebuild rules in every `Makefile.in` behind `--enable-maintainer-mode` (off by default). Without this macro, `make` re-invokes `$(AUTOCONF)` whenever the vendored `m4/*.m4` or `aclocal.m4` are missing, which forces those files to be vendored too (~17 extra files / ~500 KB of pure-regen inputs). With the macro, the vendored set shrinks from 47 files / ~2.2 MB to 30 files / ~1.7 MB. |

All other cross-build deviations are expressed as configure-time
`ac_cv_*` overrides in `.nanvix/z.py` rather than as source patches.

### Vendored autotools outputs

The port commits the result of `sh ./autogen.sh --no-po4a` directly:
`configure`, `config.h.in`, every `Makefile.in`, plus the `build-aux/`
and `po/` payloads (30 files, ~1.7 MB). Upstream's root `.gitignore`
lists these files; the port adds them with `git add -f` and treats them
as a sanctioned exception to the byte-identity invariant. This mirrors
the cpython port (which gets `configure` for free because cpython
upstream itself commits it).

`aclocal.m4`, `m4/*.m4`, and `ABOUT-NLS` are **not** vendored: they are
only consumed by `aclocal`/`autoconf` during regeneration, and the
`AM_MAINTAINER_MODE` patch above keeps `make` from ever invoking those
tools at build time.

`build-aux/config.sub` is part of the vendored set and is committed
*already-patched* to recognise `i686-nanvix`: the `fiwix*` arm of the
GNU OS-name table is extended to `| fiwix* | nanvix* )`. Any refresh
of the vendored outputs (see below) regenerates `config.sub` from the
upstream automake template, so the one-line `nanvix*` extension MUST
be re-applied before staging.

**Refreshing the vendored set** (only needed if `configure.ac`,
`Makefile.am`, or upstream tooling changes, or when bumping the
upstream version):

```sh
# On a host with autoconf, automake, libtool, autopoint, gettext:
sh ./autogen.sh --no-po4a
# Re-apply the i686-nanvix recognition (config.sub is regenerated):
sed -i 's/| fiwix\* )/| fiwix* | nanvix* )/' build-aux/config.sub
# Stage only the build-time inputs; skip aclocal.m4, m4/, ABOUT-NLS
# (those are only consumed by autoreconf, gated off by AM_MAINTAINER_MODE):
git ls-files --others --ignored --exclude-standard \
    | grep -vE '^(autom4te\.cache/|aclocal\.m4$|m4/|ABOUT-NLS$)' \
    | xargs git add -f
rm -rf autom4te.cache
git commit -m "Refresh vendored autotools outputs"
```

---

## Known Limitations

| Limitation | Impact |
|------------|--------|
| **Static linking only** | No shared `liblzma.so` (Nanvix does not support dynamic loading). |
| **`--enable-small`** | Built with the size-optimised codepath; multi-threaded encoder (`lzma_stream_encoder_mt`) is excluded. |
| **`--disable-threads`** | Single-threaded only (Nanvix consumers — primarily CPython — use single-threaded paths). |
| **No NLS / docs / scripts** | `--disable-nls --disable-doc --disable-scripts`. |
| **No `xz`/`xzdec`/`lzmadec`/`lzmainfo` CLIs** | `--disable-xz --disable-xzdec --disable-lzmadec --disable-lzmainfo`. The downstream consumer (CPython `_lzma`) needs only `liblzma.a`; the CLI runtime surface remains outside this port's supported scope. |
| **MicroVM 256 MiB runtime only** | Nanvix v0.20.0 publishes runtime assets only for MicroVM at 256 MiB; there are no Hyperlight or MicroVM 128 MiB artifacts. The supported SDK build/test matrix therefore uses `microvm` × `standalone` × `256mb` only; missing assets outside that matrix are not XZ port failures. |

---

## CI/CD

Workflow: [`.github/workflows/nanvix-ci.yml`](.github/workflows/nanvix-ci.yml).
Calls the reusable workflow at
`nanvix/workflows/.github/workflows/nanvix-ci.yml@v2.5.0` across the
`microvm` × `standalone` × `256mb` PR matrix, including all Linux and
Windows test types.
Daily cron at 09:00 UTC (tier1, alongside the other zero-dep ports).

### Future work

- Register `nanvix/xz` in the `nanvix/workflows` consumer-repos list
  (`consumer-repos.json`) and tier1 of `tier-config.json` once the
  maintainer signs off on this port; that wires xz into the automated
  zutils + workflows version-bump PR cascade.
- Re-introduce a `downstream-dispatches` entry targeting
  `nanvix/cpython` (`event_type: xz-release`, matching the historical
  singular-form convention) once cpython grows a `repository_dispatch`
  listener for it; absent today, so a dispatch would fire into the
  void.
- Add a shell-driven test tier (`tests/test_*.sh`) gated on a Nanvix
  POSIX-shell becoming available.
- Re-enable the multi-threaded encoder (`lzma_stream_encoder_mt`) once
  Nanvix exposes pthreads.
- Investigate a CMake-driven build path as an alternative to the
  autotools route.
- Re-enable the `xz` CLI (and revisit `xzdec`/`lzmadec`/`lzmainfo`)
  after its full runtime surface is validated on Nanvix.
