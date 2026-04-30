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
nanvix-version: 0.12.485
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
./z setup     # Resolve toolchain + sysroot, prepare upstream tree
./z build     # Cross-compile liblzma.a
./z test      # Smoke + integration + functional tiers
./z release   # Package release tarball under dist/
./z clean     # Remove build artefacts
```

Override the pinned `nanvix-zutil` version with
`NANVIX_ZUTIL_VERSION=<version>`.

---

## Prerequisites

| Component | Description | Default Location |
|-----------|-------------|------------------|
| **Nanvix Toolchain** | i686-nanvix cross-compiler | `/opt/nanvix` (or `$NANVIX_TOOLCHAIN`) |
| **Nanvix Sysroot** | System libraries and linker script | resolved by `./z setup` |
| **Python 3** | Required to bootstrap `nanvix-zutil` | on `PATH` |

XZ has no third-party dependencies.

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

Tests run in three tiers (mirrors the sibling ports):

1. **Smoke** — verify build artefacts exist and meet minimum sizes.
2. **Integration** — link `tests/smoke.c` against `liblzma.a` to
   produce `build/smoke.elf`.
3. **Functional** — run `smoke.elf` under `nanvixd.elf` and assert
   the `XZ_SMOKE_OK` sentinel on stdout.

```bash
./z test                       # all three tiers
./z test -- test-smoke         # single tier
```

---

## Source Changes from Upstream

The upstream XZ Utils 5.2.5 tree is preserved byte-identical outside
the Nanvix-specific surface (`.nanvix/`, `.github/workflows/nanvix-*`,
root wrappers `z`/`z.sh`/`z.ps1`, `NANVIX.md`, and `.gitignore`).

| Patch | Rationale | Hypothesis category |
|-------|-----------|---------------------|
| _none_ | **Initial port: zero source changes.** | n/a |

`build-aux/config.sub` is mutated at build time by `./z setup` (sed
injection of `i686-nanvix`); this file is autotools-generated and
falls under the "generated artefacts" exemption.

---

## Known Limitations

| Limitation | Impact |
|------------|--------|
| **Static linking only** | No shared `liblzma.so` (Nanvix does not support dynamic loading). |
| **`--enable-small`** | Built with the size-optimised codepath; multi-threaded encoder (`lzma_stream_encoder_mt`) is excluded. |
| **`--disable-threads`** | Single-threaded only (Nanvix consumers — primarily CPython — use single-threaded paths). |
| **No NLS / docs / scripts** | `--disable-nls --disable-doc --disable-scripts`. |
| **No `xzdec`/`lzmadec`/`lzmainfo`** | Disabled to keep the artefact surface minimal. |

---

## CI/CD

Workflow: `.github/workflows/nanvix-ci.yml` (added in a later commit).
Calls the reusable workflow at
`nanvix/workflows/.github/workflows/nanvix-ci.yml@v1.12.0` across the
full 2 × 3 × 2 matrix from `.nanvix/nanvix.toml`. Daily cron at
12:00 UTC; emits a `repository_dispatch` to `nanvix/cpython` on
release.
