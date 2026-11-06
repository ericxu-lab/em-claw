"""
cryoranker_utils.py — rank PARTICLES with CryoRanker (Cryo-IEF) and import the top fraction back
into CryoSPARC.

This is a PARTICLE-level quality ranker (scores individual particles, keeps the best fraction) —
distinct from the RELION class_ranker used for 2D-class selection (see 2d_select/). Use it to clean
an extracted particle stack (e.g. right after extract, before/after 2D) by keeping the top-scoring
particles.

Flow (extracted from the team's automate_cryosparc_pipeline.py):
  1. run_cryoranker(): run CryoRanker_inference.py via `conda run -n <env> accelerate launch ...`
     on a source job's particle stack, selecting the top `keep_fraction`. Writes
     `selected_particles_*.cs` + `scores_predicted_list.csv` into a result dir.
  2. import_ranked_particles(): cs.save_external_result() the ranked .cs back as a particles job,
     passthrough-linked to the source extract so downstream jobs can consume it.
  rank_and_import() does both and returns the new CryoSPARC job.

Config defaults are read from base_datainfo.yaml (`cryoranker_*` keys); override via the cfg arg.
Run from the project dir with the cryosparc-tools env (needs cryosparc.dataset).
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

PROJECT_DIR = Path(__file__).resolve().parent.parent          # the CryoSPARC profile directory
# ⚠ The REPO-ROOT yaml is the *next* project's staging file, NOT any running project's.
# It is only a last-resort fallback; every entry point below takes an explicit
# `datainfo_path` (or a pre-loaded `cfg`) and callers should ALWAYS pass
# projects/<name>/base_datainfo.yaml.  (fixed 2026-08-29, AT1R exp B)
BASE_DATAINFO = PROJECT_DIR / "base_datainfo.yaml"

# Site paths: set them per project in base_datainfo.yaml (cryoranker_*) or in the CryoSPARC .env.
CRYO_IEF_DIR = os.environ.get("EMCLAW_CRYO_IEF_DIR", "YOUR_CRYO_IEF_PATH")
DEFAULT_SCRIPT = os.path.join(CRYO_IEF_DIR, "CryoRanker_inference.py")
DEFAULT_WEIGHTS = os.path.join(CRYO_IEF_DIR, "cryo_ranker_v1.5_vit_b_model.safetensors")
DEFAULT_ENV = "cryo_ief"


class CryoRankerError(RuntimeError):
    pass


# --------------------------------------------------------------------------- config
def load_cryoranker_cfg(path: Optional[Path] = None) -> Dict[str, Any]:
    """Read the cryoranker_* keys from a project's base_datainfo.yaml (with sane defaults).

    `path` SHOULD be the active project's `projects/<name>/base_datainfo.yaml`. If omitted we
    fall back to the repo-root staging yaml and print a loud warning, because that file belongs
    to the NEXT project, not the running one.
    """
    if path is None:
        path = BASE_DATAINFO
        print(f"WARNING [cryoranker_utils]: no datainfo_path given; falling back to the REPO-ROOT "
              f"staging yaml {BASE_DATAINFO} -- pass projects/<name>/base_datainfo.yaml instead.")
    cfg: Dict[str, Any] = {}
    try:
        import yaml  # type: ignore
        with open(path) as fh:
            raw = yaml.safe_load(fh) or {}
        cfg = {k: v for k, v in raw.items() if str(k).startswith("cryoranker_")}
    except Exception:
        pass
    cfg["cryoranker_datainfo_path"] = str(path)
    cfg.setdefault("cryoranker_env", DEFAULT_ENV)
    cfg.setdefault("cryoranker_script", DEFAULT_SCRIPT)
    cfg.setdefault("cryoranker_weights", CRYO_IEF_DIR + "/")
    cfg.setdefault("cryoranker_num_processes", 16)
    cfg.setdefault("cryoranker_run_host", None)
    return cfg


# --------------------------------------------------------------------------- helpers
def _run_checked(cmd: Sequence[str], env: Optional[Dict[str, str]] = None) -> subprocess.CompletedProcess:
    cmd = list(map(str, cmd))
    print("$ " + shlex.join(cmd))
    proc = subprocess.run(cmd, env=env, text=True, capture_output=True)
    if proc.returncode != 0:
        raise CryoRankerError(
            f"Command failed ({proc.returncode}): {' '.join(cmd)}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    return proc


def _run_on_host(cmd, host=None, env=None) -> subprocess.CompletedProcess:
    """Local if host is None; else ssh, forwarding only a safe env whitelist."""
    cmd = list(map(str, cmd))
    if not host:
        return _run_checked(cmd, env=env)
    shell_cmd = " ".join(shlex.quote(x) for x in cmd)
    safe = {k: str(v) for k, v in (env or {}).items()
            if k in {"CUDA_VISIBLE_DEVICES", "PYTHONUNBUFFERED", "PYTHONIOENCODING"} and v is not None}
    if safe:
        shell_cmd = " ".join(f"{shlex.quote(k)}={shlex.quote(v)}" for k, v in safe.items()) + " " + shell_cmd
    proc = subprocess.run(["ssh", str(host), shell_cmd], text=True, capture_output=True)
    if proc.returncode != 0:
        raise CryoRankerError(f"Remote command failed on {host} ({proc.returncode})\n"
                              f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
    return proc


def _locate_cs(project_dir: str, job_uid: str, patterns: Sequence[str]) -> Path:
    jdir = Path(project_dir) / job_uid
    if not jdir.exists():
        raise CryoRankerError(f"Job directory not found: {jdir}")
    for pat in patterns:
        hits = sorted(jdir.glob(pat))
        if hits:
            return hits[0]
    raise CryoRankerError(f"No file in {jdir} matching any of {patterns}")


# --------------------------------------------------------------------------- input validation
# What the Cryo-IEF preprocessor actually needs (cryodata.data_preprocess.mrc_preprocess.
# raw_csdata_process_from_cryosparc_dir): a job dir that OWNS its particle stacks, i.e.
#   * a .cs whose name ends in one of PARTICLE_CS_SUFFIXES, and
#   * a subdirectory in MRC_SUBDIRS holding the .mrc particle stacks.
# If neither matches it silently falls back to `other_cs_path` -- an ARBITRARY .cs picked by
# os.listdir order -- and leaves mrc_dir = the job dir itself, which holds no stacks.  The result
# is a crash at best and scores computed on the wrong rows at worst.
PARTICLE_CS_SUFFIXES = ("extracted_particles.cs", "imported_particles.cs",
                        "restacked_particles.cs", "downsampled_particles.cs", "split_0000.cs")
MRC_SUBDIRS = ("restack", "extract", "imported", "downsample")


def validate_source_job(project_dir: str, source_job_uid: str) -> Dict[str, Any]:
    """Check that `source_job_uid`'s directory is something CryoRanker can actually read.

    Returns {'ok': bool, 'reason': str, 'particle_cs': str|None, 'mrc_dir': str|None}.
    `run_cryoranker` raises on ok=False unless `skip_validation=True`.
    """
    jdir = Path(project_dir) / source_job_uid
    if not jdir.is_dir():
        return {"ok": False, "reason": f"job directory not found: {jdir}",
                "particle_cs": None, "mrc_dir": None}
    names = [p.name for p in jdir.iterdir()]
    pcs = [n for n in names if any(n.endswith(s) for s in PARTICLE_CS_SUFFIXES)]
    sub = [d for d in MRC_SUBDIRS if (jdir / d).is_dir()]
    if not pcs or not sub:
        return {
            "ok": False,
            "reason": (
                f"{source_job_uid} is not a valid CryoRanker source. Found particle .cs "
                f"{pcs or 'NONE'} and stack subdir {sub or 'NONE'}; CryoRanker needs BOTH "
                f"(a *_{{extracted,imported,restacked,downsampled}}_particles.cs and one of "
                f"{MRC_SUBDIRS}). Job types that qualify: extract / restack / import particles / "
                f"downsample. Job types that DO NOT: select_2D, particle_sets, "
                f"remove_duplicate_particles, class_2D, refinements, external results -- these "
                f"only carry .cs REFERENCES to another job's stacks. "
                f"=> Rank the UPSTREAM EXTRACT job(s) instead and intersect the per-particle "
                f"scores with this job's particle uids afterwards (scores are per image, so they "
                f"are identical either way); or materialise the subset with a `restack_particles` "
                f"job first and rank that."
            ),
            "particle_cs": None, "mrc_dir": None,
        }
    return {"ok": True, "reason": "ok",
            "particle_cs": str(jdir / sorted(pcs)[0]), "mrc_dir": str(jdir / sub[0])}


# --------------------------------------------------------------------------- staging
# WHY STAGING EXISTS
#   cryodata.raw_csdata_process_from_cryosparc_dir() WRITES `new_particles.cs` back into
#   `raw_data_path` -- i.e. straight into the CryoSPARC JOB FOLDER. Two problems:
#     (a) CryoSPARC project dirs commonly belong to a different unix account -> PermissionError
#         before a single particle is scored;
#     (b) if it does succeed it pollutes the job dir, and EVERY later run silently re-loads that
#         stale cache instead of re-deriving it (`if not os.path.exists(new_csdata_path): ... else:
#         cs_data = Dataset.load(new_csdata_path)`).
#   So never hand the real job dir to CryoRanker. Hand it a SHADOW dir we own, containing symlinks.
#   The same mechanism makes an arbitrary particle SUBSET rankable (see stage_particle_subset),
#   which is what a select_2D / dedup output needs.
def stage_job_dir(project_dir: str, source_job_uid: str, stage_root: str) -> str:
    """Build a writable shadow of a valid CryoRanker source job (symlinks only). Returns its path."""
    v = validate_source_job(project_dir, source_job_uid)
    if not v["ok"]:
        raise CryoRankerError(v["reason"])
    jdir = Path(project_dir) / source_job_uid
    sdir = Path(stage_root) / f"stage_{source_job_uid}"
    sdir.mkdir(parents=True, exist_ok=True)
    for p in jdir.iterdir():
        if p.suffix == ".cs" or (p.is_dir() and p.name in MRC_SUBDIRS):
            link = sdir / p.name
            if not link.exists() and not link.is_symlink():
                link.symlink_to(p)
    return str(sdir)


def stage_particle_subset(particles, project_dir: str, stage_root: str,
                          tag: str = "subset") -> Dict[str, Any]:
    """Make an ARBITRARY particle set rankable (select_2D / dedup / merge outputs).

    CryoRanker resolves an image as os.path.join(<stack subdir>, basename(blob/path)), so a subset
    is rankable as long as we give it (i) a `*_extracted_particles.cs` holding the rows and
    (ii) a stack subdir holding the .mrc files those rows point at. Particles coming from SEVERAL
    extract jobs are split into one staging dir per source stack directory -- that, not "merged
    sets corrupt", is the real single-source constraint.

    `particles` is a cryosparc.dataset.Dataset with at least blob/path + blob/idx.
    Returns {'dirs': [paths], 'counts': [n per dir], 'sources': [stack dir per staging dir]}.
    """
    import numpy as np  # noqa
    paths = [str(p) for p in particles["blob/path"]]
    srcdirs = np.array([os.path.dirname(p).lstrip(">") for p in paths])
    out: Dict[str, Any] = {"dirs": [], "counts": [], "sources": []}
    for i, src in enumerate(sorted(set(srcdirs.tolist()))):
        sel = srcdirs == src
        sdir = Path(stage_root) / f"stage_{tag}_{i}"
        sdir.mkdir(parents=True, exist_ok=True)
        link = sdir / "extract"
        target = Path(project_dir) / src
        if not target.is_dir():
            raise CryoRankerError(f"stack directory not found for blob/path prefix {src}: {target}")
        if not link.exists() and not link.is_symlink():
            link.symlink_to(target)
        subset = particles.mask(sel) if hasattr(particles, "mask") else particles.take(np.where(sel)[0])
        subset.save(str(sdir / f"{tag}_{i}_extracted_particles.cs"))
        out["dirs"].append(str(sdir))
        out["counts"].append(int(sel.sum()))
        out["sources"].append(src)
    return out


# --------------------------------------------------------------------------- rank
def run_cryoranker(project_dir: str, source_job_uid: str, keep_fraction: float,
                   tag: str = "cryoranker", cfg: Optional[Dict[str, Any]] = None,
                   datainfo_path: Optional[str] = None,
                   results_dir: Optional[str] = None,
                   overwrite: bool = False,
                   skip_validation: bool = False,
                   extra_accelerate_args: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Rank the source job's particles; keep the top `keep_fraction`. Returns
    {csv, cs, star, keep_n, total, result_dir}.

    `datainfo_path` = the ACTIVE project's base_datainfo.yaml (projects/<name>/...).
    `results_dir`   = where to put the (very large) LMDB cache + scores. Defaults to
                      `cryoranker_results_dir` in the yaml, else <project_dir>/external_cryoranker_<tag>
                      -- but the CryoSPARC project dir is frequently owned by ANOTHER unix user, so
                      pass an explicitly writable path.
    `overwrite`     = False (NEW default) reuses an existing result dir, so an interrupted run
                      resumes from its LMDB cache instead of rebuilding ~100 GB. True restores the
                      old destroy-and-restart behaviour.
    `extra_accelerate_args` = e.g. ["--multi_gpu", "--num_processes", "4"] to actually use all the
                      GPUs; plain `accelerate launch` with no config file runs on ONE GPU.
    """
    cfg = cfg or load_cryoranker_cfg(Path(datainfo_path) if datainfo_path else None)
    script = str(cfg.get("cryoranker_script") or DEFAULT_SCRIPT)
    weights = str(cfg.get("cryoranker_weights") or DEFAULT_WEIGHTS)
    result_dir = Path(results_dir or cfg.get("cryoranker_results_dir")
                      or (Path(project_dir) / f"external_cryoranker_{tag}"))
    raw_data_path = str(Path(project_dir) / source_job_uid)

    # the result dir holds a ~50 KB/particle LMDB cache -- fail early on an unwritable parent
    parent = result_dir if result_dir.exists() else result_dir.parent
    if not os.access(str(parent), os.W_OK):
        raise CryoRankerError(
            f"CryoRanker results dir is not writable by this user: {result_dir}. "
            f"The CryoSPARC project dir often belongs to another unix account -- pass "
            f"results_dir=<somewhere you own> (or set cryoranker_results_dir in base_datainfo.yaml)."
        )

    if not skip_validation:
        v = validate_source_job(project_dir, source_job_uid)
        if not v["ok"]:
            raise CryoRankerError(v["reason"])
        # never let the preprocessor write new_particles.cs into the CryoSPARC job dir
        raw_data_path = stage_job_dir(project_dir, source_job_uid,
                                      str(result_dir.parent / "_stage"))

    wp = Path(weights)
    if wp.is_file():
        model_proj_dir = str(wp.parent)
    elif wp.is_dir():
        model_proj_dir = str(wp)
        if not (wp / Path(DEFAULT_WEIGHTS).name).exists():
            raise CryoRankerError(f"Weights dir missing {Path(DEFAULT_WEIGHTS).name}: {wp}")
    else:
        raise CryoRankerError(f"CryoRanker weights path does not exist: {weights}")

    # NOTE: order matters -- a bare "*particles*.cs" glob sorts "J####_passthrough_particles.cs"
    # BEFORE "extracted_particles.cs" (uppercase sorts first), so ask for the real particle files
    # first.  Passthrough is only an acceptable last resort for the row COUNT.
    input_cs = _locate_cs(project_dir, source_job_uid,
                          ["*extracted_particles.cs", "restacked_particles.cs",
                           "*imported_particles.cs", "*downsampled_particles.cs",
                           "particles.cs", "*particles*.cs"])
    try:
        from cryosparc.dataset import Dataset  # type: ignore
        total = len(Dataset.load(str(input_cs)))
    except Exception:
        total = 0
    if total <= 0:
        raise CryoRankerError(f"Could not determine particle count from {input_cs}")
    keep_n = max(1, int(total * keep_fraction))

    cmd = [
        "conda", "run", "--no-capture-output", "-n", str(cfg.get("cryoranker_env") or DEFAULT_ENV),
        "accelerate", "launch",
        "--main_process_port", str(cfg.get("cryoranker_main_process_port", 0)),
        *(list(extra_accelerate_args or cfg.get("cryoranker_accelerate_args") or [])),
        script,
        "--path_result_dir", str(result_dir),
        "--path_model_proj", model_proj_dir,
        "--raw_data_path", raw_data_path,
        "--num_select", str(keep_n),
        "--num_processes", str(int(cfg.get("cryoranker_num_processes") or 1)),
    ]
    env = os.environ.copy()
    if cfg.get("cryoranker_gpu_ids"):
        env["CUDA_VISIBLE_DEVICES"] = str(cfg["cryoranker_gpu_ids"])

    if result_dir.exists() and overwrite:
        print(f"Removing existing CryoRanker result dir: {result_dir}")
        shutil.rmtree(result_dir)
    elif result_dir.exists():
        print(f"Reusing existing CryoRanker result dir (overwrite=False): {result_dir}")
    result_dir.mkdir(parents=True, exist_ok=True)

    _run_on_host(cmd, host=cfg.get("cryoranker_run_host"), env=env)

    csv_path = result_dir / "scores_predicted_list.csv"
    cs_hits = sorted(result_dir.glob("selected_particles_*.cs"))
    star_hits = sorted(result_dir.glob("selected_particles_*.star"))
    if not csv_path.exists() or not cs_hits:
        raise CryoRankerError(f"CryoRanker output missing under {result_dir}")
    return {"csv": csv_path, "cs": cs_hits[0], "star": (star_hits[0] if star_hits else None),
            "keep_n": keep_n, "total": total, "result_dir": result_dir}


# --------------------------------------------------------------------------- import back
def import_ranked_particles(cs, project, workspace, particles_cs_path: str,
                            parent_job_uid: str, parent_output: str = "particles",
                            title: str = "Import CryoRanker Picks"):
    """Save the ranked .cs back into CryoSPARC as a particles job, passthrough-linked to the
    source job so downstream jobs see the full particle fields."""
    from cryosparc.dataset import Dataset  # type: ignore
    particles = Dataset.load(str(particles_cs_path))
    result = cs.save_external_result(
        project_uid=project.uid, workspace_uid=workspace.uid, dataset=particles,
        type="particle", name="particles",
        passthrough=(parent_job_uid, parent_output), title=title,
    )
    if hasattr(result, "uid"):
        return result
    if isinstance(result, str):
        return project.find_job(result)
    if isinstance(result, dict) and result.get("uid"):
        return project.find_job(result["uid"])
    raise CryoRankerError(f"Unexpected return from save_external_result: {type(result)}")


def rank_and_import(cs, project, workspace, source_job_uid: str, keep_fraction: float = 0.25,
                    tag: str = "cryoranker", parent_output: str = "particles",
                    title: Optional[str] = None, cfg: Optional[Dict[str, Any]] = None,
                    datainfo_path: Optional[str] = None, results_dir: Optional[str] = None,
                    overwrite: bool = False, skip_validation: bool = False,
                    extra_accelerate_args: Optional[Sequence[str]] = None):
    """Convenience: rank the source job's particles, keep top `keep_fraction`, import back.
    Returns (job, run_info_dict).

    ⚠ `source_job_uid` must be a job whose directory the Cryo-IEF preprocessor understands, i.e.
    one carrying its OWN particle stacks (extract / restack / import / downsample). Pointing it at
    a select_2D / merge / dedup job dir silently loads an arbitrary `.cs` and cannot resolve the
    mrc directory -- rank the EXTRACT job(s) instead and apply the per-particle scores to the
    downstream uid set yourself.
    """
    info = run_cryoranker(str(project.dir()), source_job_uid, keep_fraction, tag=tag, cfg=cfg,
                          datainfo_path=datainfo_path, results_dir=results_dir,
                          overwrite=overwrite, skip_validation=skip_validation,
                          extra_accelerate_args=extra_accelerate_args)
    job = import_ranked_particles(
        cs, project, workspace, str(info["cs"]), source_job_uid, parent_output,
        title or f"Import CryoRanker top {keep_fraction:g} ({info['keep_n']}/{info['total']})",
    )
    return job, info


if __name__ == "__main__":
    import json
    print(json.dumps(load_cryoranker_cfg(), indent=2, default=str))
