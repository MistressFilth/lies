#!/usr/bin/env bash
# A/B the CUDA backend under the protocol that reproduces the abort.
#
# The reproduction is not a plain embed loop. Every shape tried that did
# NOT touch the daemon returned zero aborts:
#
#   * 25 sequential embeds over one reused sandbox          -> 0/25
#   * fresh model cache per iteration, 1 collection         -> 0/12
#   * fresh model cache, 4 collections + query              -> 0/8
#
# What does reproduce it is a *cold qmd daemon*: recycle the daemon,
# then run the integration suite. The aborts cluster at session start
# and stop once the daemon is warm, which is why a long-lived daemon
# makes the failure look impossible to find.
#
# So this alternates arms on each cycle -- stock, patched, stock,
# patched -- rather than running all of one then all of the other. The
# abort is intermittent and the host drifts over an hour, so a
# block design would let slow drift masquerade as an arm difference.
#
# Usage: ab_backends.sh [cycles]      (each cycle = 1 daemon recycle + 1 suite run)
#
# Model paths are pinned to files already on the host. Left as `hf:` URIs,
# every one of the 8 tests re-downloads 2.2 GB into its own tmp_path
# because qmd resolves MODEL_CACHE_DIR from $XDG_CACHE_HOME and
# `_isolated_xdg` redirects that per test -- ~17.6 GB per suite run, every
# run. Pointing the three model variables at local files makes
# parseHfUri() return null (it only matches an "hf:" prefix) so there is
# nothing to download: ~12 min per suite run becomes ~5, and the abort
# still reproduces.
set -u

CYCLES="${1:-4}"
cd "$(dirname "$0")/.." || exit 1
SUITE=tests/integration/test_tag_filter_end_to_end.py
LOGDIR=/tmp/opencode/ab-backends
mkdir -p "$LOGDIR"

MODEL_DIR="${MODEL_DIR:-$HOME/.cache/qmd/models}"
EMBED_MODEL="${EMBED_MODEL:-$MODEL_DIR/hf_ggml-org_embeddinggemma-300M-Q8_0.gguf}"
GEN_MODEL="${GEN_MODEL:-$MODEL_DIR/hf_tobil_qmd-query-expansion-1.7B-q4_k_m.gguf}"
RERANK_MODEL="${RERANK_MODEL:-$MODEL_DIR/hf_ggml-org_qwen3-reranker-0.6b-q8_0.gguf}"
for m in "$EMBED_MODEL" "$GEN_MODEL" "$RERANK_MODEL"; do
  [ -f "$m" ] || { echo "missing model file: $m" >&2; exit 1; }
done
export QMD_EMBED_MODEL="$EMBED_MODEL" QMD_GENERATE_MODEL="$GEN_MODEL" QMD_RERANK_MODEL="$RERANK_MODEL"

printf '%-6s %-9s %-8s %-9s %s\n' CYCLE ARM LIB CUDA TIMEOUT RESULT
stock_aborts=0; patched_aborts=0
stock_runs=0; patched_runs=0

for i in $(seq 1 "$CYCLES"); do
  for arm in stock patched; do
    if [ "$arm" = stock ]; then
      ./tools/nlc_novmm.sh revert >/dev/null 2>&1
    else
      ./tools/nlc_novmm.sh install >/dev/null 2>&1
    fi
    vmm=$(strings "$(./tools/nlc_novmm.sh status | awk -F': *' '/^path/{print $2}')" 2>/dev/null | grep -c ggml_cuda_pool_vmm || echo '?')

    # Cold daemon: the variable under test.
    timeout 180 uv run lies qmd recycle >/dev/null 2>&1

    log="$LOGDIR/${arm}-${i}.log"
    INTEGRATION=1 uv run pytest "$SUITE" -p no:randomly -q --no-header > "$log" 2>&1
    cuda=$(grep -c cuMemAddressReserve "$log" || true)
    tmo=$(grep -c QmdTimeoutError "$log" || true)
    if grep -q '^FAILED\|^ERROR' "$log"; then res=fail; else res=pass; fi

    if [ "$arm" = stock ]; then
      stock_runs=$((stock_runs+1)); stock_aborts=$((stock_aborts+cuda))
    else
      patched_runs=$((patched_runs+1)); patched_aborts=$((patched_aborts+cuda))
    fi
    printf '%-6s %-9s %-8s %-9s %-7s %s\n' "$i" "$arm" "$vmm" "$cuda" "$tmo" "$res"
  done
done

# Leave the host patched.
./tools/nlc_novmm.sh install >/dev/null 2>&1

printf '\n=== totals ===\n'
printf 'stock   : %d cuda-abort event(s) across %d suite run(s)\n' "$stock_aborts" "$stock_runs"
printf 'patched : %d cuda-abort event(s) across %d suite run(s)\n' "$patched_aborts" "$patched_runs"
echo "(each suite run is 8 tests x 4 collections of fresh embeds)"