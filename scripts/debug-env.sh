#!/usr/bin/env bash
echo "=== GIT env vars ==="
env | grep -E "^GIT_" | sort
echo "=== PRE_COMMIT env vars ==="
env | grep -E "^PRE_COMMIT" | sort
echo "=== pwd ==="
pwd
echo "=== git rev-parse --show-toplevel ==="
git rev-parse --show-toplevel
echo "=== git rev-parse --git-dir ==="
git rev-parse --git-dir
echo "=== git rev-parse --git-common-dir ==="
git rev-parse --git-common-dir
