#!/usr/bin/env bash
# Rebuild node-llama-cpp's CUDA backend with the VMM pool compiled out.
#
# WHY THIS EXISTS
#
#   `qmd` runs llama.cpp **in-process** through `node-llama-cpp`, which
#   loads a CUDA backend plugin from its own `linux-x64-cuda/bins/`
#   directory. On WSL2 that backend intermittently hard-aborts:
#
#       [node-llama-cpp] CUDA error: out of memory
#       [node-llama-cpp]   current device: 0, in function alloc at
#         .../ggml/src/ggml-cuda/ggml-cuda.cu:492
#       [node-llama-cpp]   cuMemAddressReserve(&pool_addr,
#                                    CUDA_POOL_VMM_MAX_SIZE, 0, 0, 0)
#       ... ggml_abort -> SIGILL
#
#   "out of memory" is a misleading label. `cuMemAddressReserve` reserves
#   **virtual address space**, and `CUDA_POOL_VMM_MAX_SIZE` is hardcoded
#   at 32 GB. Measured on this host with a 24 GiB RTX 4090: peak embed
#   usage was 4775 MiB at 38% utilisation with ~20 GB free, so the
#   reservation is failing with memory to spare. It is an address-space
#   reservation failing under WSL2's WDDM-backed CUDA, not VRAM
#   exhaustion.
#
#   Upstream: withcatai/node-llama-cpp#580 (RTX 3090, same 32 GB
#   reservation, same hard abort, same qmd workload) with the workaround
#   `GGML_CUDA_NO_VMM=ON`. The runtime fallback requested there is
#   withcatai/node-llama-cpp#610, still open -- which is why a *build*
#   flag is still the only lever.
#
#   HONESTY NOTE ON VERIFICATION
#
#   This script is NOT the fix. It patches `linux-x64-cuda/bins/`, which
#   this host does not load; an A/B showed 4/5 suite runs aborting with
#   it against 3/4 with stock -- no difference, because none was
#   possible.
#
#   What DOES work is rebuilding with node-llama-cpp's own toolchain and
#   `GGML_CUDA_NO_VMM=ON`, then installing over BOTH `bin/` and
#   `Release/` in llama/localBuilds/. See AGENTS.md, "Fixing it", for
#   the exact commands -- the short version is that a hand-rolled build
#   `dlopen`s cleanly but never brings the GPU up, because it misses
#   `-DGGML_SHARED -DNAPI_VERSION=7` and a pinned `CUDAToolkit_ROOT`.
#
#   To find the library that actually carries the VMM pool:
#
#     find ~/.bun/install/global/node_modules -name '*.so*' -type f \
#       | while read -r f; do
#           n=$(strings "$f" 2>/dev/null | grep -c ggml_cuda_pool_vmm)
#           [ "$n" != 0 ] && echo "$n  $f"
#         done
#
#   And to find the one that is actually LOADED, read the backtrace --
#   it carries the full path, which is the thing worth doing first:
#
#     grep -oE '/[^ ]*libggml-cuda[^ )]*' <abort log> | sort -u
#
#   The general error, repeated throughout: the file that was rebuilt is
#   not the file that runs, and a successful `dlopen` is no evidence a
#   library works.
#
# WHY A SCRIPT AND NOT A CONFIG
#
#   `QMD_EMBED_PARALLELISM` is qmd's knob for how many embedding
#   contexts one process creates, and qmd already computes it from VRAM
#   (capped at 8). Pinning it to 1 does NOT fix this abort -- measured 2
#   aborts with it, 1 without, neither library patched. The failure is
#   one reservation's *size*, not a concurrency race.
#
#   `GGML_CUDA_NO_VMM` is a CMake option, so the fix lives in a compiled
#   artifact inside a **bun global install**. Any `bun install`, package
#   refresh, or reinstall of `@tobilu/qmd` overwrites
#   `libggml-cuda.so` and silently restores the abort. That is why this
#   recipe is checked in: re-run `install` and the fix is back.
#
# USAGE
#
#   tools/nlc_novmm.sh build     # clone + build the backend (slow, ~4 min)
#   tools/nlc_novmm.sh install   # back up stock, install NO_VMM build
#   tools/nlc_novmm.sh revert    # restore the stock (VMM-enabled) library
#   tools/nlc_novmm.sh status    # which backend is installed, and why
#
#   `build` output is cached under $CACHE, so `install` after a `bun`
#   wipe is fast -- no recompile, just re-copy.
set -euo pipefail

NLC_BINS="${NLC_BINS:-$HOME/.bun/install/global/node_modules/@node-llama-cpp/linux-x64-cuda/bins/linux-x64-cuda}"
BACKUP="${NLC_BACKUP:-$HOME/.local/share/lies/nlc-backup/libggml-cuda.so.vmm}"
CACHE="${NLC_CACHE:-$HOME/.local/cache/lies/nlc-novmm}"
SRC="$CACHE/llama.cpp"
TAG="${NLC_TAG:-b8390}"          # what @tobilu/qmd's node-llama-cpp 3.18.1 bundles
ARTIFACT="$SRC/build/bin/libggml-cuda.so.0"

# sm_89 = Ada / RTX 4090. Override for a different card:
#   NLC_CUDA_ARCH=86 NLC_CUDA_ARCH=120 tools/nlc_novmm.sh build
ARCH="${NLC_CUDA_ARCH:-89}"
JOBS="${NLC_JOBS:-6}"

has_vmm() { strings "$1" 2>/dev/null | grep -c 'ggml_cuda_pool_vmm' || true; }

die() { echo "error: $*" >&2; exit 1; }

require_bins() {
  [ -d "$NLC_BINS" ] || die "no node-llama-cpp CUDA backend at $NLC_BINS (is qmd installed?)"
  [ -f "$NLC_BINS/libggml-cuda.so" ] || die "missing libggml-cuda.so in $NLC_BINS"
}

do_build() {
  command -v cmake >/dev/null || die "cmake not on PATH"
  command -v nvcc  >/dev/null || die "nvcc not on PATH"
  mkdir -p "$CACHE"
  if [ ! -d "$SRC/.git" ]; then
    echo ">> cloning llama.cpp $TAG"
    git clone -q --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp.git "$SRC"
  else
    echo ">> reusing source at $SRC"
  fi
  echo ">> configuring (GGML_CUDA_NO_VMM=ON, arch=$ARCH)"
  cmake -S "$SRC" -B "$SRC/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_CUDA=ON \
    -DGGML_CUDA_NO_VMM=ON \
    -DCMAKE_CUDA_ARCHITECTURES="$ARCH" \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_SERVER=OFF -DLLAMA_BUILD_COMMON=OFF >/dev/null
  # Prove the flag reached nvcc rather than trusting the cache.
  grep -q -- '-DGGML_CUDA_NO_VMM' "$SRC/build/ggml/src/ggml-cuda/CMakeFiles/ggml-cuda.dir/flags.make" \
    || die "GGML_CUDA_NO_VMM is not in the CUDA defines; refusing to build a library that does not disable VMM"
  echo ">> building (-j$JOBS); this takes a few minutes"
  cmake --build "$SRC/build" --target ggml -j"$JOBS" >/dev/null
  [ -f "$ARTIFACT" ] || die "build produced no $ARTIFACT"
  [ "$(has_vmm "$ARTIFACT")" = "0" ] || die "built library still contains the VMM pool -- flag did not take effect"
  echo ">> built: $(stat -c %s "$ARTIFACT") bytes, 0 VMM symbols"
}

do_install() {
  require_bins
  [ -f "$ARTIFACT" ] || die "no build at $ARTIFACT -- run '$0 build' first"
  mkdir -p "$(dirname "$BACKUP")"
  if [ ! -f "$BACKUP" ]; then
    cp "$NLC_BINS/libggml-cuda.so" "$BACKUP"
    echo ">> backed up stock backend -> $BACKUP"
  fi
  cp "$ARTIFACT" "$NLC_BINS/libggml-cuda.so"
  chmod 755 "$NLC_BINS/libggml-cuda.so"
  # This build requests libggml-base.so.0; the shipped backend is
  # unversioned. glibc registers a library by its SONAME, so this symlink
  # satisfies both names from one file rather than loading two copies of
  # the backend registry.
  ln -sfn libggml-base.so "$NLC_BINS/libggml-base.so.0"
  echo ">> installed NO_VMM backend"
  do_status
}

do_revert() {
  require_bins
  [ -f "$BACKUP" ] || die "no backup at $BACKUP (cannot revert)"
  cp "$BACKUP" "$NLC_BINS/libggml-cuda.so"
  chmod 755 "$NLC_BINS/libggml-cuda.so"
  rm -f "$NLC_BINS/libggml-base.so.0"
  echo ">> restored stock (VMM-enabled) backend"
  do_status
}

do_status() {
  require_bins
  local cur; cur=$(has_vmm "$NLC_BINS/libggml-cuda.so")
  printf 'path      : %s\n' "$NLC_BINS/libggml-cuda.so"
  printf 'size      : %s bytes\n' "$(stat -c %s "$NLC_BINS/libggml-cuda.so")"
  printf 'VMM pool  : %s symbols\n' "$cur"
  printf 'soname link: %s\n' "$([ -L "$NLC_BINS/libggml-base.so.0" ] && echo present || echo absent)"
  if [ "$cur" = "0" ]; then
    echo 'state     : PATCHED - VMM pool absent; this backend cannot make the failing'
    echo '            cuMemAddressReserve call at all'
  else
    echo 'state     : STOCK - vulnerable to the WSL2 VMM reservation abort'
  fi
  printf 'backup    : %s\n' "$([ -f "$BACKUP" ] && echo "$BACKUP" || echo none)"
}

case "${1:-status}" in
  build)   do_build ;;
  install) do_install ;;
  revert)  do_revert ;;
  status)  do_status ;;
  *) die "usage: $0 {build|install|revert|status}" ;;
esac