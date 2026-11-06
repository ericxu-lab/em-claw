#!/usr/bin/env bash
# subV-as-GPT — run the subV plan-validation/ranking task on GPT via the Codex CLI.
#
# GPT reads .claude/agents/subV.md (the canonical subV spec) and uses the cryosparc MCP
# validator tools (registered in ~/.codex/config.toml as [mcp_servers.cryosparc]) for the
# technical check, exactly like the Claude subV did — only the model is GPT.
#
# Usage:
#   ./subV_gpt.sh PLANS.md          # plans read from a file
#   ./subV_gpt.sh < PLANS.md        # plans on stdin
#   echo "<3 plans>" | ./subV_gpt.sh
#
# Notes:
# - --dangerously-bypass-approvals-and-sandbox: required so Codex runs the cryosparc MCP tool
#   calls non-interactively (otherwise they are auto-cancelled with "user cancelled MCP tool
#   call"). This whole session is already the outer sandbox; subV only reads + validates.
# - Output is GPT's subV verdict (the subV.md output contract: RANKING / BEST / PER-PLAN /
#   PARAM FLAGS / RECOMMENDATION).
set -euo pipefail

# The CryoSPARC agent directory that holds .claude/agents/subV.md (bin/em-claw exports it).
PROJ="${EMCLAW_CRYOSPARC_SOURCE:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)}"
PLANS_SRC="${1:-/dev/stdin}"

# --- per-project context resolution (multi-project safety) --------------------------------
# The repo-root base_datainfo.yaml is the USER's staging file for the NEXT project and does NOT
# belong to any running project. Reading it silently fed subV the WRONG project's optics/sample
# (observed 2026-08-17: a cb1_redo validation read a different project's yaml). subV must always be
# pointed at an EXPLICIT projects/<name>/ directory.
#   ./subV_gpt.sh projects/<name>/runs/plans.md            # project inferred from the plans path
#   ./subV_gpt.sh plans.md projects/<name>                 # or given explicitly as arg 2
#   PROJECT_DIR=projects/<name> ./subV_gpt.sh < plans.md   # or via env (needed for stdin)
PROJECT_DIR="${2:-${PROJECT_DIR:-}}"
if [[ -z "$PROJECT_DIR" && "$PLANS_SRC" != "/dev/stdin" ]]; then
  # plans normally live at projects/<name>/runs/plans_*.md -> project dir is two levels up
  _cand="$(cd "$(dirname "$PLANS_SRC")/.." 2>/dev/null && pwd || true)"
  if [[ -n "$_cand" && -f "$_cand/base_datainfo.yaml" ]]; then
    PROJECT_DIR="$_cand"
  fi
fi
if [[ -z "$PROJECT_DIR" ]]; then
  echo "ERROR: cannot resolve the project directory." >&2
  echo "  subV must be given an explicit projects/<name>/ dir -- the repo-root base_datainfo.yaml" >&2
  echo "  belongs to a DIFFERENT (next) project and would silently mis-inform the validation." >&2
  echo "  Pass it as arg 2, or set PROJECT_DIR=projects/<name>." >&2
  exit 2
fi
# normalise to a path relative to $PROJ when possible, else absolute
case "$PROJECT_DIR" in /*) ;; *) PROJECT_DIR="$PROJ/$PROJECT_DIR" ;; esac
if [[ ! -f "$PROJECT_DIR/base_datainfo.yaml" ]]; then
  echo "ERROR: $PROJECT_DIR/base_datainfo.yaml not found -- wrong project dir?" >&2
  exit 2
fi
YAML_PATH="$PROJECT_DIR/base_datainfo.yaml"
STATE_PATH="$PROJECT_DIR/runs/phase_state.md"
echo "subV context: $YAML_PATH" >&2

read -r -d '' PROMPT <<EOF || true
You are subV, the cryo-EM reconstruction plan validator and judge.

1. Read .claude/agents/subV.md — that file is your full role, ranking priority (workflow FIRST,
   parameters as PARAM FLAGS), and exact OUTPUT CONTRACT. Follow it precisely.
2. Read the WORKING PROJECT's context from these EXACT paths — do NOT read the repo-root
   base_datainfo.yaml, which may belong to a different project and would mis-inform you:
     - $YAML_PATH        (this project's inputs: optics, particle size/character, symmetry)
     - $STATE_PATH       (jobs done, results, the human's problem, and the phase)
   Use them for context and to decide starting-vs-continuing phase behaviour.
3. Validate the candidate plan(s) given in the <stdin> block:
   - TECHNICAL: use the cryosparc MCP tools (validate_job_type, validate_params,
     validate_connection, validate_pipeline) — do not guess job-type/param/connection validity.
   - SCIENTIFIC + WORKFLOW: rank by workflow first; raise controversial parameters as PARAM FLAGS,
     do not demote a good workflow for them; reserve technical FAIL for run-breaking issues.
4. Output ONLY the subV output-contract block. Do NOT modify, create, or delete any files.
EOF

exec codex exec \
  --dangerously-bypass-approvals-and-sandbox \
  --skip-git-repo-check \
  -C "$PROJ" \
  "$PROMPT" < "$PLANS_SRC"
