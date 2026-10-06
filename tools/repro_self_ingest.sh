#!/usr/bin/env bash
# Reproduce a self-ingest: `lies ingest --batch` pointed at a live
# collection directory, so source and mirror are the same path.
#
# Every XDG root is redirected -- data, cache and config -- so the qmd
# index is isolated too (the guard added in 981030c refuses otherwise).
# No --force: the idempotency check is on.
set -u

REPO="$(cd "$(dirname "$0")/.." && pwd)"
SANDBOX="$(mktemp -d /tmp/selfingest.XXXXXX)"
COLL="$SANDBOX/data/lies/library/collections/selfcoll"

mkdir -p "$SANDBOX/cache" "$SANDBOX/config" "$COLL"
cat > "$COLL/config.yaml" <<'YAML'
name: selfcoll
source: local:.
YAML

# Long enough to clear the thin-content quarantine guard, so the
# experiment tests idempotency and not the quarantine.
cat > "$COLL/page.md" <<'MD'
# A page

This body is deliberately long enough that the thin-content quarantine
guard lets it through, so that a failure here is a failure of the
idempotency contract rather than of a content-length filter that has
nothing to do with what is being tested.

Second paragraph, also padding, also present only to give the content
filter no excuse to fire before the hash comparison is ever reached.
MD

export XDG_DATA_HOME="$SANDBOX/data"
export XDG_CACHE_HOME="$SANDBOX/cache"
export XDG_CONFIG_HOME="$SANDBOX/config"
export NO_COLOR=1
export PYTHONPATH="$REPO/src"
LIES="$REPO/.venv/bin/lies"

echo "sandbox : $SANDBOX"
echo "collection dir: $COLL"
echo
echo "sha256 of the source file before any run:"
sha256sum "$COLL/page.md" | sed 's/^/  /'
echo

for run in 1 2 3; do
  echo "--- run $run ---"
  # PIPESTATUS, not $?: a pipeline's exit is the last command's, so
  # `lies ... | sed` would report sed's status and hide lies'.
  "$LIES" ingest --batch "$COLL" 2>&1 | sed 's/^/  /'
  echo "  exit=${PIPESTATUS[0]}"
  echo "  file hash now: $(sha256sum "$COLL/page.md" | cut -c1-16)…"
  if head -1 "$COLL/page.md" | grep -q '^---$'; then
    echo "  recorded source_hash: $(grep -m1 '^source_hash:' "$COLL/page.md" | cut -d' ' -f2 | cut -c1-16)…"
  else
    echo "  no frontmatter (file untouched)"
  fi
  echo
done

echo "--- full index of the collection after three runs ---"
ls -1 "$COLL" | sed 's/^/  /'
echo
echo "--- does the file agree with itself? ---"
REC=$(grep -m1 '^source_hash:' "$COLL/page.md" 2>/dev/null | cut -d' ' -f2)
ACT=$(sha256sum "$COLL/page.md" | cut -d' ' -f1)
echo "  recorded in its own frontmatter: $REC"
echo "  sha256sum of the file:          $ACT"
[ "$REC" = "$ACT" ] && echo "  AGREE" || echo "  DISAGREE"
echo
echo "--- quarantine records, if any ---"
find "$SANDBOX/data" -name '*.poison*' -o -name 'quarantine*' 2>/dev/null | head | sed 's/^/  /'
find "$SANDBOX/data" -path '*quarantine*' -type f 2>/dev/null -exec sh -c 'echo "  == $1"; head -20 "$1"' _ {} \;
echo
echo "sandbox retained at: $SANDBOX"
