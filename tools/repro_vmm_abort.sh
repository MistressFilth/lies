#!/usr/bin/env bash
# Reproduce the CUDA VMM abort by reproducing what the integration test
# actually does: a COLD model cache, so every run builds a fresh CUDA
# context and attempts a fresh cuMemAddressReserve.
#
# The trigger was hidden by two confounds, both of which are measurement
# errors rather than facts about qmd:
#
#   * A reused sandbox keeps the model cached. After the first iteration
#     nothing is downloaded and no new context is created, so the
#     reservation is attempted once and the run reports 0 aborts. That
#     is a false all-clear -- it measured a warm cache, not the failure.
#
#   * The download itself moves peak VRAM by ~750 MiB. With one sample
#     per configuration that reads as a difference between settings when
#     it is a difference between "model already present" and not.
#
# Measured: tests/integration/test_tag_filter_end_to_end.py pulls 2.2 GB
# of GGUF per test into its own tmp_path, for 8 tests. This harness does
# the same thing deliberately, once per iteration.
#
# Usage: repro_vmm_abort.sh [iterations]
set -u

ITERS="${1:-12}"
KEEP_MODEL="${KEEP_MODEL:-0}"   # 1 = reuse one cache (the false all-clear)
WARM="${WARM:-0}"               # 1 = pre-seed the cache from the host
QMD_BIN="${QMD_BIN:-$(command -v qmd)}"
SHARED=/tmp/opencode/vmm-repro-shared

seeds() {
  local d="$1" c i
  for c in 1 2 3 4; do
    mkdir -p "$d/coll$c"
    for i in 1 2 3; do
      printf '# Doc %s\n\nProse long enough to be embedded as a real document rather than a one-line fixture.\n' "$i" \
        > "$d/coll$c/doc$i.md"
    done
  done
}

# Each of these spins up a fresh qmd process, and with a fresh cache that
# means a fresh model load and a fresh CUDA context. The integration
# test seeds FOUR collections and then queries, which additionally loads
# the 639 MB reranker and the 1.7 GB query-expansion model -- far heavier
# than the 300 MB embed model a bare `qmd embed` touches. Matching that
# shape is the whole point of the harness.
run_all() {
  local dir="$1" c out rc
  for c in 1 2 3 4; do
    env XDG_CACHE_HOME="$dir/cache" XDG_DATA_HOME="$dir/data" NO_COLOR=1 \
      "$QMD_BIN" collection add "$dir/coll$c" "r$c" --name "r$c" >/dev/null 2>&1
    out=$(env XDG_CACHE_HOME="$dir/cache" XDG_DATA_HOME="$dir/data" NO_COLOR=1 \
      "$QMD_BIN" embed -c "r$c" 2>&1); rc=$?
    if [ $rc -ne 0 ]; then printf 'embed%s:%s\n' "$c" "$out"; return 1; fi
  done
  # A query pulls in query-expansion (1.7 GB) + rerank (639 MB).
  out=$(env XDG_CACHE_HOME="$dir/cache" XDG_DATA_HOME="$dir/data" NO_COLOR=1 \
    "$QMD_BIN" query "pipelines and collections" --limit 2 2>&1); rc=$?
  if [ $rc -ne 0 ]; then printf 'query:%s\n' "$out"; return 1; fi
  return 0
}

aborts=0
fails=0
declare -a RESULTS

for i in $(seq 1 "$ITERS"); do
  # KEEP_MODEL=1 reuses the cache: the shape that produced 0/25 aborts.
  if [ "$KEEP_MODEL" = "1" ]; then
    dir="$SHARED"; rm -rf "$dir"; mkdir -p "$dir/cache" "$dir/data"
  else
    dir=$(mktemp -d "${TMPDIR:-/tmp}/vmm.XXXXXX")
    mkdir -p "$dir/cache" "$dir/data"
    if [ "$WARM" = "1" ] && [ -d "$HOME/.cache/qmd/models" ]; then
      mkdir -p "$dir/cache/qmd"
      cp -r "$HOME/.cache/qmd/models" "$dir/cache/qmd/models" 2>/dev/null || true
    fi
  fi
  seeds "$dir"

  if out=$(run_all "$dir"); then
    RESULTS+=("iter$i ok")
  else
    fails=$((fails+1))
    if printf '%s' "$out" | grep -q 'cuMemAddressReserve'; then
      aborts=$((aborts+1)); RESULTS+=("iter$i ABORT")
    else
      RESULTS+=("iter$i fail")
    fi
  fi
  [ "$KEEP_MODEL" = "1" ] || rm -rf "$dir"
done

echo "=== results (KEEP_MODEL=$KEEP_MODEL WARM=$WARM iters=$ITERS) ==="
printf '%s\n' "${RESULTS[@]}"
echo "aborts=$aborts nonzero_exits=$fails iters=$ITERS"