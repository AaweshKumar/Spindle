#!/usr/bin/env bash
# Spindle decision-chain verification script.
set -uo pipefail

REPO_ROOT="${1:-.}"
cd "$REPO_ROOT" || { echo "Repo root not found: $REPO_ROOT"; exit 1; }

# Package root: prefer src/spindle_core/ (current layout), fall back to repo root
# so this still works if the layout changes later.
if [ -d "src/spindle_core" ]; then
  SRC="src/spindle_core"
else
  SRC="."
fi

PASS=0
FAIL=0

section() { echo; echo "=== $1 ==="; }
ok()      { echo "PASS: $1"; PASS=$((PASS+1)); }
bad()     { echo "FAIL: $1"; FAIL=$((FAIL+1)); }

# ---------------------------------------------------------------
section "1. Naming collision check (decision 2 + 3: Tapestry / Loom)"
HITS=$(grep -rniE "tapestry[ _]?engine" \
  --include="*.py" --include="*.md" --include="*.rst" --include="*.txt" \
  --exclude-dir=".git" --exclude-dir="tapestry" . 2>/dev/null)
if [ -z "$HITS" ]; then
  ok "No literal 'Tapestry Engine' references found"
else
  bad "'Tapestry Engine' still referenced somewhere — will collide with new Tapestry config"
  echo "$HITS"
fi

HITS2=$(grep -rniw "tapestry" \
  --include="*.py" --include="*.md" --include="*.rst" --include="*.txt" \
  --exclude-dir=".git" --exclude-dir="tapestry" . 2>/dev/null)
if [ -z "$HITS2" ]; then
  ok "No stray 'Tapestry' references outside tapestry/"
else
  echo "INFO (manual check): 'Tapestry' mentions outside tapestry/ — confirm each is the NEW config sense, not a leftover Layer-0 reference:"
  echo "$HITS2"
fi

# ---------------------------------------------------------------
section "2. workers/ is unwired / unfinished (decision 4+5 premise)"
if [ -d "$SRC/workers" ]; then
  WIRED=$(grep -rniE "from (spindle\.)?workers|import (spindle\.)?workers" \
    "$SRC/engine" "$SRC/orchestrator" "$SRC/persistence" "$SRC/streams" \
    "$SRC/main.py" "$SRC/config.py" 2>/dev/null)
  if [ -z "$WIRED" ]; then
    ok "workers/ is not imported by engine/orchestrator/persistence/streams/main/config — confirms it's dead code"
  else
    bad "workers/ IS referenced by supposedly-frozen code — the 'unfinished/unwired' assumption is wrong"
    echo "$WIRED"
  fi
else
  echo "INFO: $SRC/workers not found — check folder name/location"
fi

# ---------------------------------------------------------------
section "3. Layer 0 (engine/) imports nothing from any layer above it"
BAD_IMPORTS=$(grep -rniE "^\s*(from|import)\s+(spindle\.)?(orchestrator|persistence|streams|workers|connectors|tapestry)" "$SRC/engine" 2>/dev/null)
if [ -z "$BAD_IMPORTS" ]; then
  ok "engine/ has zero imports from any other layer"
else
  bad "engine/ imports from a higher layer — layering violation"
  echo "$BAD_IMPORTS"
fi

# ---------------------------------------------------------------
section "4. Layer 1 (orchestrator/persistence/streams) imports nothing from Layer 2a+"
BAD_IMPORTS2=$(grep -rniE "^\s*(from|import)\s+(spindle\.)?(connectors|tapestry)" "$SRC/orchestrator" "$SRC/persistence" "$SRC/streams" 2>/dev/null)
if [ -z "$BAD_IMPORTS2" ]; then
  ok "orchestrator/persistence/streams have zero imports from connectors/ or tapestry/"
else
  bad "Layer 1 code imports from Layer 2a — layering violation"
  echo "$BAD_IMPORTS2"
fi

# ---------------------------------------------------------------
section "5. Sibling spindle-worker/ package is actually abandoned (decision 1)"
SIBLING_WORKER="../spindle-worker"
if [ -d "$SIBLING_WORKER" ]; then
  NON_TRIVIAL=$(find "$SIBLING_WORKER" -name "*.py" -not -name "__init__.py" 2>/dev/null)
  if [ -z "$NON_TRIVIAL" ]; then
    ok "spindle-worker/ exists but has no real .py content — consistent with 'abandoned, never built'"
  else
    bad "spindle-worker/ contains real Python files despite decision 1 marking this plan abandoned — check whether it's actually dead or was worked on before the pivot"
    echo "$NON_TRIVIAL"
  fi
else
  echo "INFO: no sibling spindle-worker/ found next to this repo — nothing to check"
fi

# ---------------------------------------------------------------
section "6. Frozen test suite still green"
if command -v uv >/dev/null 2>&1; then
  if uv run pytest -q; then
    ok "Full test suite passes"
  else
    bad "Test suite failing — do not hand off to the coder until this is green"
  fi
else
  echo "INFO: 'uv' not on PATH in this shell — run 'uv run pytest -q' manually and check it's still 31/31 (or current count)"
fi

# ---------------------------------------------------------------
echo
echo "================================"
echo "PASS: $PASS   FAIL: $FAIL"
[ "$FAIL" -eq 0 ] && exit 0 || exit 1
