# cryoemranker — particle-level ranking via CryoRanker (Cryo-IEF)

Ranks **individual particles** by quality and keeps the top fraction, then imports the kept
particles back into CryoSPARC as a particles job. This is a **particle-level** cleaner — distinct
from the RELION **class_ranker** in `2d_select/` (which ranks 2D *classes*). Use it to thin an
extracted stack to its best particles, e.g. right after `extract`, before/after a 2D round, or to
enrich a noisy pool.

## API (`cryoranker_utils.py`)
Run from the project dir with the **cryosparc-tools** env (needs `cryosparc.dataset`).

```python
import sys; sys.path.insert(0, "cryoemranker")   # run from the CryoSPARC profile directory
from cryoranker_utils import rank_and_import, run_cryoranker, import_ranked_particles

# one-shot: rank source job's particles, keep top 25%, import back (passthrough-linked)
job, info = rank_and_import(cs, project, workspace, source_job_uid="J13", keep_fraction=0.25)
# info = {csv, cs, star, keep_n, total, result_dir}; `job` is the new CryoSPARC particles job
```

Or the two steps separately:
```python
info = run_cryoranker(str(project.dir()), "J13", keep_fraction=0.25, tag="round1")
job  = import_ranked_particles(cs, project, workspace, str(info["cs"]),
                               parent_job_uid="J13", parent_output="particles")
```

## How it works
1. `run_cryoranker` runs `CryoRanker_inference.py` via
   `conda run -n <cryoranker_env> accelerate launch ...` on the source job's particle `.cs`,
   selecting the top `keep_fraction` (`--num_select N`). GPUs via `CUDA_VISIBLE_DEVICES`
   (`cryoranker_gpu_ids`); optional remote run over ssh (`cryoranker_run_host`).
   Outputs `selected_particles_*.cs` + `scores_predicted_list.csv` in
   `external_cryoranker_<tag>/`.
2. `import_ranked_particles` calls `cs.save_external_result(..., type="particle",
   passthrough=(source_job, "particles"))` so downstream jobs see full particle fields.

## Config (read from `base_datainfo.yaml` `cryoranker_*`, override via `cfg=`)
`cryoranker_env` (cryo_ief) · `cryoranker_script` · `cryoranker_weights` (dir or file) ·
`cryoranker_num_processes` (16) · `cryoranker_run_host` (None = local) ·
optional `cryoranker_gpu_ids`, `cryoranker_main_process_port`, `cryoranker_results_dir`.

Deps (all present): `cryo_ief` env with `accelerate`, `CryoRanker_inference.py`, weights
`cryo_ranker_v1.5_vit_b_model.safetensors`.

## Notes
- `keep_fraction` is the runtime judgment call — log it + the kept/total counts in
  `runs/subC_decisions.md`, like other selection decisions.
- Extracted from the team's `automate_cryosparc_pipeline.py` (Pipeline 1/3 CryoRanker step).
