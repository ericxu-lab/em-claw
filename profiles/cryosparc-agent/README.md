# CryoSPARC agent directory

This directory is a ready-to-use value for `EMCLAW_CRYOSPARC_SOURCE`. `bin/em-claw`
loads it with `--plugin-dir` and mounts it read-only, except for `projects/`.

| Path | Purpose |
| --- | --- |
| `.claude/agents/subR.md` | Finds precedents in the offline history bank |
| `.claude/agents/subS.md` | Literature and manual guidance (`solid_reference_pool` only in phase 1) |
| `.claude/agents/subV.md` | Plan validation and ranking, run on GPT through `../cryosparc/subV_gpt.sh` |
| `.claude/agents/subC.md` | Executes and debugs plans on the live CryoSPARC server |
| `.claude/agents/subP.md`, `.claude/skills/subP/` | Distils a successful project into a reusable notebook (`/subP`) |
| `.claude/settings.json` | Enables the Codex plugin used by subV |
| `projects/` | Per-project state (`projects/<name>/base_datainfo.yaml`, `runs/`); contents are gitignored |

The agent prompts refer to the helper code by paths relative to `profiles/cryosparc`,
which is the working directory of the CryoSPARC and combined routes. Site values
appear as `YOUR_...` placeholders: set `YOUR_GPU_RENDER_HOST` (subC surface
rendering) for your site, or keep a private copy of this directory and point
`EMCLAW_CRYOSPARC_SOURCE` at it. subR reads the history bank in
`profiles/cryosparc/history_bank/`.
