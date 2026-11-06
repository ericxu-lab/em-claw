---
name: subP
description: >-
  Turn a successful CryoSPARC project into a reusable, self-contained .ipynb pipeline for
  similar samples. Use when the user points at a project (live, e.g. "zll_260130" / "P5 W19",
  or a history-bank group) and asks to mimic it, distil it, or make a fixed/reusable script
  or notebook out of it. Runs outside the phase protocol.
---

# /subP — distil a successful project into a pipeline notebook

This is a **standalone function**, not part of the reconstruction phase protocol. It does not
touch `phase_state.md` and it queues no jobs.

## Steps

### 1. Resolve the reference project
From what the user named, get a **project uid + workspaces**:
- a project of ours → `projects/<name>/base_datainfo.yaml` (`project_uid`) and the workspace
  titles (usually `<name>`, `<name>_p1p2`, … — one project spans several);
- a bare `P<n>`/`W<n>` → use it directly;
- a history-bank group → skip to step 2 with its existing `*_digest.json`.

Ask the user only if the project is genuinely ambiguous.

### 2. Build the digest (deterministic — do NOT do this by reading jobs yourself)
```bash
# run from the CryoSPARC profile directory; every CryoSPARC script goes through the EM-Claw runtime
../../libexec/em-claw-cryosparc-python tools/build_live_digest.py <Puid> --title-contains <ws-prefix> \
    --name <name> --out pipelines/_digests/<name>_digest.json
```
(`--workspaces W3,W10,...` if the titles don't share a prefix; `--no-fsc` for a fast structural
pass with no resolutions.) Check the printed summary: workspaces, job/status counts, best job,
size of the contributing lineage. If `best_res_A` is empty or the lineage looks wrong, fix that
here — subP's whole output rests on it.

### 3. Spawn subP
One `subP` subagent, given: the digest path, the reference project's
`projects/<name>/runs/{phase_state.md,subC_decisions.md}` if ours, the user's final-result job if
they named one, the target sample class, and the output dir `pipelines/<name>/`.

### 4. Report
Relay subP's contract block. Then flag for the user, in your own words:
- what was **dropped** and why (this is where distillation can silently damage a pipeline);
- any **parameter changed** from the reference and its evidence;
- that the notebook is **unvalidated** until `validation_plan.md` is run.

### 5. Offer validation
Validating the notebook against the reference dataset is a normal agentM phase with subC (days of
GPU, long-job checkpointing) — **offer it, do not start it**.

## Layout
| Path | What |
|---|---|
| `pipelines/_digests/<name>_digest.json` | extracted reference (shared, regenerable) |
| `pipelines/<name>/recipe.json` | auditable straightening record |
| `pipelines/<name>/<name>_pipeline.ipynb` | the deliverable |
| `pipelines/<name>/workflow.md` | mermaid diagram |
| `pipelines/<name>/validation_plan.md` | how to prove it works |
| `pipelines/<name>/lib/` | pinned copies of the tools the notebook imports |

`pipelines/` is **shared-brain** material like `history_bank/` — not per-project state, never under
`projects/<name>/runs/`.
