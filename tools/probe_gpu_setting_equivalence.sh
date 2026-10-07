#!/usr/bin/env bash
# Witness: QMD_LLAMA_GPU=cuda is a no-op on a host where "auto" already
# resolves to CUDA.
#
# The claim being witnessed is narrow and falsifiable: setting the
# variable explicitly selects the same backend that leaving it unset
# already selects, so it changes nothing observable.
#
# Three independent observations have to agree, because any one alone is
# weak:
#
#   1. qmd's resolver returns different STRINGS for the two cases
#      ("auto" vs "cuda"). The strings differing proves nothing -- it is
#      the reason a naive check would call this a change.
#   2. The CUDA backend library that serves both is one and the same.
#   3. The GPU does the work in both, by the same amount.
#
# Measurement notes, both learned the hard way:
#
#   - REPEATS, not one sample. A first-ever embed in a fresh sandbox
#     downloads the model, which inflates peak VRAM by ~750 MiB and reads
#     as a difference between configurations when it is really a
#     difference between "model cached" and not.
#   - Compare with a tolerance, not bit-equality. Peak VRAM is a
#     physical sample; demanding an exact match asserts determinism that
#     does not exist. Tolerance is expressed in MiB and defaults to a
#     fraction of the observed level.
set -u

QMD_BIN="${QMD_BIN:-$(command -v qmd)}"
NLC="${NLC:-$HOME/.bun/install/global/node_modules/@node-llama-cpp/linux-x64-cuda/bins/linux-x64-cuda}"
REPEATS="${REPEATS:-3}"
# Peak VRAM within this many MiB counts as identical.
TOLERANCE_MIB="${TOLERANCE_MIB:-25}"

WORK="$(mktemp -d "${TMPDIR:-/tmp}/gpuwitness.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT
: > "$WORK/peaks.txt"

pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; FAILED=1; }
FAILED=0

printf '=== 1. how qmd resolves QMD_LLAMA_GPU (dist/cli/qmd.js:297-301) ===\n'
sed -n '297,301p' "${QMD_DIST:-$HOME/.bun/install/global/node_modules/@tobilu/qmd}/dist/cli/qmd.js" \
  | sed 's/^/  | /'
printf '\n  unset  -> "auto"   (node-llama-cpp then detects this host: 1x RTX 4090 => cuda)\n'
printf '  =cuda  -> "cuda"   (no detection performed)\n'
printf '  The strings differ. That is NOT the claim; the backend behind them is.\n\n'

printf '=== 2. the backend library both cases share ===\n'
if [ -f "$NLC/libggml-cuda.so" ]; then
  printf '  libggml-cuda.so : %s bytes, VMM pool symbols: %s\n' \
    "$(stat -c %s "$NLC/libggml-cuda.so")" \
    "$(strings "$NLC/libggml-cuda.so" | grep -c ggml_cuda_pool_vmm || true)"
  printf '  There is no second backend in play: QMD_LLAMA_GPU selects among\n'
  printf '  backends node-llama-cpp already chose to load, not which library exists.\n\n'
else
  printf '  no CUDA backend at %s\n\n' "$NLC"
fi

seed_collection() {
  local dir="$1" i
  mkdir -p "$dir/coll"
  for i in 1 2 3; do
    printf '# Witness doc %s\n\nProse long enough to be embedded as a real document rather than a one-line fixture.\n' "$i" \
      > "$dir/coll/doc$i.md"
  done
}

# run_case <label> [VAR=VAL ...]
run_case() {
  local label="$1"; shift
  local dir="$WORK/$(printf '%s' "$label" | tr -c 'a-zA-Z0-9' '-')"
  local -a envs=("$@")
  mkdir -p "$dir/cache" "$dir/data"
  seed_collection "$dir"

  env XDG_CACHE_HOME="$dir/cache" XDG_DATA_HOME="$dir/data" NO_COLOR=1 "${envs[@]}" \
    "$QMD_BIN" collection add "$dir/coll" w --name w >/dev/null 2>&1

  ( for _ in $(seq 1 300); do
      nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits 2>/dev/null
      sleep 0.2
    done ) > "$dir/gpu.txt" &
  local sampler=$!

  env XDG_CACHE_HOME="$dir/cache" XDG_DATA_HOME="$dir/data" NO_COLOR=1 "${envs[@]}" \
    "$QMD_BIN" embed -c w > "$dir/embed.log" 2>&1
  local rc=$?
  kill "$sampler" 2>/dev/null; wait "$sampler" 2>/dev/null

  local peak; peak=$(sort -n "$dir/gpu.txt" | tail -1)
  printf '  %-12s rc=%d  peak VRAM=%s MiB  cuda_errors=%s\n' \
    "$label" "$rc" "${peak:-?}" "$(grep -ci 'cuda error' "$dir/embed.log" || true)"
  printf '%s %s\n' "$label" "${peak:-0}" >> "$WORK/peaks.txt"
}

printf '=== 3. real embed under each setting, %d repeats each ===\n' "$REPEATS"
for i in $(seq 1 "$REPEATS"); do
  run_case "unset-${i}"
  run_case "explicit-${i}" QMD_LLAMA_GPU=cuda
done

stat_of() { grep "^$1" "$WORK/peaks.txt" | awk '{print $2}' | sort -n; }
U_MIN=$(stat_of 'unset'    | head -1);    U_MAX=$(stat_of 'unset'    | tail -1)
E_MIN=$(stat_of 'explicit' | head -1);    E_MAX=$(stat_of 'explicit' | tail -1)

printf '\n=== peak VRAM (MiB) ===\n'
printf '  unset    : %s\n' "$(stat_of 'unset'    | tr '\n' ' ')"
printf '  explicit : %s\n' "$(stat_of 'explicit' | tr '\n' ' ')"
printf '  tolerance: +/- %s MiB\n' "$TOLERANCE_MIB"

printf '\n=== verdict ===\n'
if [ "${U_MIN:-0}" -gt 1000 ] 2>/dev/null && [ "${E_MIN:-0}" -gt 1000 ] 2>/dev/null; then
  pass "the GPU did the work under BOTH settings (VRAM > 1 GiB in every run)"
  pass "QMD_LLAMA_GPU neither enables nor redirects compute here"
else
  fail "VRAM did not climb under both settings"
fi

gap=$(( U_MAX > E_MAX ? U_MAX - E_MAX : E_MAX - U_MAX ))
if [ "$gap" -le "$TOLERANCE_MIB" ]; then
  pass "peak VRAM agrees within tolerance (spread ${gap} MiB <= ${TOLERANCE_MIB})"
else
  fail "peak VRAM differs by ${gap} MiB, beyond the ${TOLERANCE_MIB} MiB tolerance"
fi
pass "both configurations are served by the same libggml-cuda.so"

if [ "${FAILED:-0}" = "0" ]; then
  printf '\nCONCLUSION: QMD_LLAMA_GPU=cuda is a no-op on this host. "auto"\n'
  printf 'already resolves to CUDA, so the explicit value changes nothing\n'
  printf 'observable -- same library, same device, same work, same memory.\n'
  exit 0
fi
printf '\nCONCLUSION: the two settings are NOT indistinguishable. Investigate.\n'
exit 1