#!/usr/bin/env bash
set -u

PROJECT=/home/liumingrui/lumen-arm64
cd "$PROJECT"
source .env
export LUMEN_SEMCODE_MCP="$PROJECT/Analysis-SKILL/tools/semcode/target/release/semcode-mcp"
ROOT="$PROJECT/runtime/arm64_e2e_v0.1"
RUNS="$ROOT/runs"
mkdir -p "$RUNS"

# The mixed remote import directory contains three arm64 maintenance cases.
# Keep this runner architecture-local; the x86_64 collaborator uses the
# corresponding v2.0-dev workflow and must not be pulled into this loop.
ids=(
  1459655b2bf1e798f9d0
  79883955da3a62652a80
  37ca7ae3e98cb65c3209
)

printf 'E2E_START arm64 %s\n' "$(date -Is)" | tee "$ROOT/run.log"
for id in "${ids[@]}"; do
  input="$ROOT/inputs/$id/input.txt"
  session="arm64_e2e_v0.1_$id"
  out="$RUNS/$id.stdout.log"
  printf 'CASE_START %s %s\n' "$id" "$(date -Is)" | tee -a "$ROOT/run.log"
  timeout --signal=TERM --kill-after=60s 1800s \
    venv/bin/python main.py "$input" --config config.json --session-id "$session" \
    >"$out" 2>&1
  rc=$?
  printf '%s\n' "$rc" > "$RUNS/$id.rc"
  printf 'CASE_END %s rc=%s %s\n' "$id" "$rc" "$(date -Is)" | tee -a "$ROOT/run.log"
done
printf 'E2E_END %s\n' "$(date -Is)" | tee -a "$ROOT/run.log"
