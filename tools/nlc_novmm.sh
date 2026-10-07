#!/usr/bin/env bash
# Rebuild node-llama-cpp's CUDA backend with the VMM pool compiled out.
#
# THE ABORT
#
#   `qmd` runs llama.cpp in-process through `node-llama-cpp`. On WSL2 the
#   CUDA backend intermittently hard-aborts mid-embed:
#
#       [node-llama-cpp] CUDA error: out of memory
#       [node-llama-cpp]   current device: 0, in function alloc at
#         .../ggml/src/ggml-cuda/ggml-cuda.cu:492
#       [node-llama-cpp]   cuMemAddressReserve(&pool_addr,
#                                    CUDA_POOL_VMM_MAX_SIZE, 0, 0, 0)
#       ... ggml_abort -> SIGILL
#
#   "out of memory" is a false label. `cuMemAddressReserve` reserves
#   **virtual address space** and `CUDA_POOL_VMM_MAX_SIZE` is hardcoded at
#   32 GB. Measured on this host (RTX 4090, 24 GiB): peak embed usage was
#   4775 MiB at 38% utilisation with ~20 GB free. It is an address-space
#   reservation failing under WSL2's WDDM-backed CUDA, not VRAM
#   exhaustion -- no amount of free VRAM changes the outcome.
#
#   Upstream: withcatai/node-llama-cpp#580 (RTX 3090, same 32 GB
#   reservation, same hard abort, same qmd workload) with the workaround
#   `GGML_CUDA_NO_VMM=ON`. The runtime fallback requested there is
#   withcatai/node-llama-cpp#610, still open -- which is why a build flag
#   is still the only lever. There is NO runtime env var for it.
#
# RESULT
#
#   Rebuilt with this script, `tests/integration/test_tag_filter_end_to_end.py`
#   shows 0 CUDA aborts across 5 suite runs, against 4 of 5 at baseline,
#   with GPU acceleration confirmed on the same build:
#
#       qmd doctor  ->  device probe: GPU cuda; offloading enabled
#
#   That last check is the one that matters. Two earlier attempts "fixed"
#   the abort by leaving the GPU switched off, which surfaced as
#   `QMD Warning: no GPU acceleration` plus `QmdTimeoutError`. Always
#   confirm the GPU came up; an abort count of zero is also what
#   "nothing ran" and "ran on the CPU" look like.
#
# THE FOUR THINGS THAT MUST MATCH
#
#   A hand-rolled CMake build `dlopen`s cleanly and the GPU never comes
#   up. All four of these are required, and none are guessable:
#
#     1. build with node-llama-cpp's own toolchain (its `source download`
#        CLI), not a standalone cmake invocation;
#     2. `-DGGML_SHARED -DNAPI_VERSION=7` -- the node addon ABI;
#     3. `CUDAToolkit_ROOT` pinned -- otherwise CMake compiles with the
#        nvcc you name and links another toolkit's libcudart, because
#        /usr/local/cuda resolves through /etc/alternatives;
#     4. single-arch `GGML_NATIVE=ON`, not the fat binary a shipped
#        prebuilt uses.
#
#   Plus `cmake -DGGML_CUDA_NO_VMM=ON` is SILENTLY IGNORED here -- the
#   option is pinned by node-llama-cpp's configure -- so the cache has to
#   be edited directly.
#
# BOTH COPIES MUST BE PATCHED
#
#   localBuilds/<variant>/ holds `bin/libggml-cuda.so` AND
#   `Release/libggml-cuda.so`. Rebuilding updates `bin/`; the process
#   loads `Release/`. Patching only the rebuilt file leaves the abort
#   fully intact while every check on that file passes. The abort
#   backtrace names the loaded path in full -- read it:
#
#       grep -oE '/[^ ]*libggml-cuda[^ )]*' <abort log> | sort -u
#
#   That is the mistake this whole investigation kept making: the file
#   that was rebuilt is not the file that runs. A successful `dlopen` is
#   no evidence a library works.
#
# WHY THIS SCRIPT EXISTS RATHER THAN A CONFIG
#
#   The fix lives in a compiled artifact inside a bun global install.
#   Any `bun install`, package refresh, or reinstall of `@tobilu/qmd`
#   replaces `llama/localBuilds/` and the abort returns silently. That
#   is what this script is for: re-run `apply` and the fix is back. The
#   build is cached, so a reapply after a bun wipe is fast.
#
# USAGE
#
#   tools/nlc_novmm.sh status   # report backend state, change nothing
#   tools/nlc_novmm.sh verify   # abort VMM-free AND GPU-up, change nothing
#   tools/nlc_novmm.sh build    # fetch + build via node-llama-cpp (slow)
#   tools/nlc_novmm.sh apply    # build if needed, patch both copies, verify
#   tools/nlc_novmm.sh revert   # restore the unpatched backend
set -uo pipefail

NLC_PKG="${NLC_PKG:-$HOME/.bun/install/global/node_modules/node-llama-cpp}"
NLC_MAIN="${NLC_MAIN:-$HOME/.bun/install/global/node_modules/@node-llama-cpp}"
LOCAL_BUILDS="${LOCAL_BUILDS:-$NLC_PKG/llama/localBuilds}"
VARIANT="${VARIANT:-linux-x64-cuda}"
RELEASE="${NLC_PKG_REPO_RELEASE:-b8390}"
BACKUP="${NLC_BACKUP:-$HOME/.local/share/lies/nlc-backup}"

BUILD_DIR="$LOCAL_BUILDS/$VARIANT"
SRC_DIR="$NLC_PKG/llama"
BIN_LIB="$BUILD_DIR/bin/libggml-cuda.so"
REL_LIB="$BUILD_DIR/Release/libggml-cuda.so"
FALLBACK_LIB="$NLC_MAIN/$VARIANT-ext/bins/$VARIANT/fallback/libggml-cuda.so"

die() { echo "error: $*" >&2; exit 1; }
has_vmm() { strings "$1" 2>/dev/null | grep -c 'ggml_cuda_pool_vmm' || true; }

# --- status ------------------------------------------------------------
do_status() {
  for label_path in "bin:$BIN_LIB" "Release:$REL_LIB" "fallback-prebuilt:$FALLBACK_LIB"; do
    label=${label_path%%:*}; path=${label_path#*:}
    if [ -f "$path" ]; then
      printf '  %-20s %10s bytes   VMM symbols: %s\n' "$label" "$(stat -c %s "$path")" "$(has_vmm "$path")"
    else
      printf '  %-20s %s\n' "$label" "(absent)"
    fi
  done
  printf '  %-20s %s\n' "build dir" "$([ -d "$BUILD_DIR" ] && echo present || echo 'absent - run build')"
}

# --- verify: the two conditions that together mean "fixed" --------------
do_verify() {
  local rc=0 lib
  echo "== VMM removed from every backend this host may load =="
  for lib in "$BIN_LIB" "$REL_LIB"; do
    [ -f "$lib" ] || continue
    local n; n=$(has_vmm "$lib")
    if [ "$n" = "0" ]; then
      printf '  OK    %s\n' "$lib"
    else
      printf '  FAIL  %s still has %s VMM symbols\n' "$lib" "$n"; rc=1
    fi
  done
  echo
  echo "== GPU acceleration still up (an abort count of 0 also means 'no GPU') =="
  local probe
  probe=$(cd "$REPO_ROOT" 2>/dev/null && uv run qmd doctor 2>&1 | grep -i 'device probe' | head -1)
  if printf '%s' "$probe" | grep -qi 'GPU cuda'; then
    printf '  OK    %s\n' "${probe:0:110}"
  else
    printf '  FAIL  %s\n' "${probe:-no device probe output}"; rc=1
  fi
  return $rc
}

# --- build -------------------------------------------------------------
do_build() {
  command -v node >/dev/null || die "node not on PATH"
  command -v cmake >/dev/null || die "cmake not on PATH"
  [ -d "$SRC_DIR" ] || die "no llama.cpp source at $SRC_DIR (run: nlc source download)"

  echo ">> fetching + building llama.cpp via node-llama-cpp's own toolchain"
  echo ">> (this is what supplies -DGGML_SHARED -DNAPI_VERSION=7 and the right CUDAToolkit_ROOT)"
  node "$NLC_PKG/dist/cli/cli.js" source download \
      --release "$RELEASE" --gpu cuda --arch x64 || die "nlc source download failed"

  [ -d "$BUILD_DIR" ] || die "expected build dir $BUILD_DIR, absent after build"
}

# --- the flag, which -D will not set ------------------------------------
enable_novmm() {
  local cache="$BUILD_DIR/CMakeCache.txt"
  [ -f "$cache" ] || die "no CMakeCache.txt at $cache"
  echo ">> setting GGML_CUDA_NO_VMM=ON in CMakeCache.txt (a -D is silently ignored)"
  sed -i 's/^GGML_CUDA_NO_VVM:BOOL=OFF$/GGML_CUDA_NO_VMM:BOOL=ON/' "$cache"
  grep -q '^GGML_CUDA_NO_VVM:BOOL=ON$' "$cache" || die "could not set the flag in $cache"

  cmake -S "$SRC_DIR" -B "$BUILD_DIR" >/dev/null 2>&1 || die "reconfigure failed"

  # Prove the flag reached nvcc rather than trusting the cache.
  local fm
  fm=$(find "$BUILD_DIR" -path '*ggml-cuda*' -name flags.make 2>/dev/null | head -1)
  [ -n "$fm" ] || die "no flags.make found under $BUILD_DIR"
  grep -q -- '-DGGML_CUDA_NO_VMM' "$fm" || die "-DGGML_CUDA_NO_VMM is absent from CUDA_DEFINES; refusing to install a build that still uses VMM"
  echo ">> flag confirmed in CUDA_DEFINES"
}

# --- apply: patch BOTH copies, then verify ------------------------------
do_apply() {
  [ -d "$BUILD_DIR" ] || do_build
  enable_novmm

  echo ">> rebuilding ggml-cuda"
  cmake --build "$BUILD_DIR" --target ggml-cuda -j"${JOBS:-6}" >/dev/null 2>&1 \
    || die "ggml-cuda build failed"
  [ "$(has_vmm "$BIN_LIB")" = "0" ] || die "rebuilt bin/ still has the VMM pool"

  mkdir -p "$BACKUP"
  # bin/ is the rebuild output; Release/ is what the process loads. Missing
  # either is normal on a fresh build -- only patch what exists.
  for lib in "$REL_LIB" "$FALLBACK_LIB"; do
    if [ -f "$lib" ]; then
      [ -f "$BACKUP/$(basename "$(dirname "$lib")")-libggml-cuda.so" ] \
        || cp "$lib" "$BACKUP/$(basename "$(dirname "$lib")")-libggml-cuda.so"
      cp "$BIN_LIB" "$lib"
      echo ">> patched $lib"
    fi
  done

  echo
  do_status
  echo
  do_verify
}

do_revert() {
  local restored=0 lib bak
  for lib in "$REL_LIB" "$FALLBACK_LIB"; do
    [ -f "$lib" ] || continue
    bak="$BACKUP/$(basename "$(dirname "$lib")")-libggml-cuda.so"
    if [ -f "$bak" ]; then cp "$bak" "$lib"; echo ">> restored $lib"; restored=1
    else echo ">> no backup for $lib ($bak absent); leaving it alone"; fi
  done
  [ "$restored" = 1 ] || die "no backups found under $BACKUP -- cannot revert"
  do_status
}

REPO_ROOT="${REPO_ROOT:-$(cd "$(dirname "$0")/.." && pwd)}"

case "${1:-status}" in
  build)  do_build ;;
  apply)  do_apply ;;
  revert) do_revert ;;
  verify) do_verify ;;
  status) do_status ;;
  *) die "usage: $0 {status|verify|build|apply|revert}" ;;
esac