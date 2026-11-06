#!/usr/bin/env python3
"""Multi-chain cryo-EM model prediction, processed-map rigid docking, refinement, and mask pipeline.

Input: multi-chain FASTA + cryo-EM MRC/MAP volume.

Pipeline:
  1. Split FASTA by chain and predict/download candidate model(s) for each chain.
  2. Make original + flipped scoring maps, then preprocess each real hand using the
     successful J164 protocol: low-density threshold + strong-density cap + normalize;
     auto-box the processed docking-only maps while preserving MRC origin.
  3. For every chain, translate the starting model to the docking-map center, then run
     ChimeraX fitmap random rigid docking on original and flip hand. Escalate random
     search levels until the acceptance gate passes or levels are exhausted. Optionally
     (--target-lobe-map) do this first as a CANDIDATE fit in a pre-cut target-lobe/segment
     submap, then locally re-fit that pose in the WHOLE map (two-stage global->local fit).
  4. Choose one real map hand for combined refinement.
  5. Optionally make a model-derived mask for selected chains.

============================================================================================
HANDEDNESS RULE (never violated anywhere in this tool): the atomic MODEL is never mirrored.
A folded protein is chirally fixed (L-amino acids, right-handed alpha helices); mirroring its
coordinates produces a chemically invalid molecule, not a "corrected" one. The ONLY handedness
operation performed anywhere in this pipeline is on the MAP: the same chiral model is rigid-
docked into BOTH the no-flip map and one axis-flipped map ("--dock-hands original,flip", the
default), each hand's fit is scored (fraction of atoms in density + mean-subtracted Phenix
CC_mask/CC_volume, never ChimeraX's own DC-inflated `correlation`), and the MAP hand that fits
better is KEPT; downstream outputs (refined model, masks) are emitted on that winning map's
grid. `check_model_chirality_L` / `assert_model_chirality_ok` are called after every rigid-body
transform as a structural guard against ever re-introducing a coordinate mirror. There used to
be a `mirror_model_coordinates()` helper that mapped a fitted model back onto the *original*
(possibly wrong-hand) grid -- it has been REMOVED, not merely deprecated, because mirroring a
model is never correct (see PORT_NOTES.md). If the flipped hand wins, the correct atomic model
IS the one fit directly in the flipped-map frame; that map file (not the original input file) is
the physically correct reference for everything downstream. See --legacy-flip-mask-to-original-
grid for the one place a caller can still ask for the OLD (map-back) behavior, which flips only
MASK/volume outputs (never the atomic model) back onto the original grid, for backward compat.
============================================================================================

Ported from the user's `complex-map-dock-refine` skill (see PORT_NOTES.md for the full mapping
of skill gate -> code):
  - Gate 1 (never mirror the model) -- see HANDEDNESS RULE above.
  - Gate 2/4 (target-lobe cut + two-stage global->local fit) -- `--target-lobe-map`,
    `dock_chain_two_stage()`, `chimerax_local_refit()`.
  - Gate 3 (accept by %atoms-in-density + lobe/whole CC + chirality, not CC alone) --
    `fraction_atoms_in_density()`, `select_best_hand()`, `--accept-mode multi_metric` (default).
  - Gate 5 (robust ChimeraX invocation) -- `--cmd "open <file>.cxc"`, never `--script <file>.cxc`.
  - Gate 6 (two-stage Phenix refine + geometry gates) -- `phenix_two_stage_refine()`,
    `--refine-mode two_stage` (default).
  - Vision QC gates (dock + refine self-verification via rendered model-in-density images,
    added per explicit user request alongside the skill port) -- `--vision-qc`,
    `render_dock_qc()` (build_model_mask/vision_qc.py).

Note on scripts/run_rigid_fit.py: that skill script calls StructAgent's own ChimeraX JSON-
result wrapper (`--wrapper .../StructAgent/skills/annika/chimerax/scripts/wrapper.py`). That
wrapper does not exist on this server (checked: no StructAgent tree, no matching wrapper.py
anywhere under /work or /home). This tool does NOT depend on it; it uses its own
`chimerax_fitmap()` / `chimerax_local_refit()`, made robust the same way (gate 5) rather than
adding a hard dependency on an unavailable external tool.

The processed map is used only for rigid search. Scoring uses real/unmodified maps. Final
refinement uses the selected real hand (original or flipped).

     Example final command for full pipeline with R mask:
       YOUR_MODEL_MASK_PYTHON_PATH
     build_model_mask/multichain_cryoem_autofit.py \
         --fasta
     YOUR_DATA_PATH/GHSR_Gprotein_Nb35_9UY3_chains_RABGN.fasta \
         --map YOUR_DATA_PATH/J172_006_volume_map_sharp.mrc \
         --out-dir /path/to/new_run \
         --resolution 3.2 \
         --skip-modeling \
         --models-dir YOUR_DATA_PATH/existing_models_from_9UY3 \
         --dock-hands flip \
         --search-levels 3000 \
         --cc-gate 0.50 \
         --mask-chains R

     Example with a FASTA/order sequence range:
       --mask-chains R \
       --mask-chain-ranges 'R:1-283'

     or:
       --mask-chains R \
       --mask-seq-from 1 \
       --mask-seq-to 283

     Example with a pre-cut target-lobe/segment submap for a chain in a multi-lobe complex
     (skill gate 2 -- an OPT-IN restriction, NOT a default recommendation as of 2026-08-19:
     PowerFit's own core-weighted CC + Laplace edge filter handles multi-component maps
     unaided -- the hTA1 benchmark placed a receptor alone inside a whole GPCR-Gs complex map
     correctly with no lobe guard at all. Use this when a whole-map search still mis-locks onto
     a brighter neighbouring component, and keep the crop comfortably larger than the model
     being docked -- see PORT_NOTES.md sec2b/sec2c for the measured evidence and the small-crop
     warning this tool logs automatically):
       --mask-chains R \
       --target-lobe-map /path/to/receptor_lobe_segment.mrc

"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import gemmi
    import mrcfile
    import numpy as np
    from scipy import ndimage
except Exception as e:
    raise SystemExit(
        "ERROR: this script needs gemmi, mrcfile, numpy, scipy. Run it with a Python that has them (YOUR_MODEL_MASK_PYTHON_PATH)"
    ) from e

# Optional: reusable render+assess vision-QC helpers (build_model_mask/vision_qc.py). Imported
# defensively so `import multichain_cryoem_autofit` always succeeds even if vision_qc.py or its
# soft PIL dependency is missing; callers only hit an error if they actually try to render.
# test_mask patch 2026-07-29: prefer the OpenGL-free slab-MIP renderer (qc_slices.py). ChimeraX
# cannot save images on this cluster (no OpenGL, no Xvfb anywhere), so vision_qc.render_dock_qc
# always raises LimitationError and NO QC image is ever produced -- and --vision-qc gate can
# never be satisfied. qc_slices renders the same model-in-density judgement from the map array.
# vision_qc stays as the fallback for hosts that do have working OpenGL.
try:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import qc_slices  # type: ignore
    from qc_slices import render_dock_qc as _render_dock_qc  # type: ignore
except Exception:
    try:
        from vision_qc import render_dock_qc as _render_dock_qc  # type: ignore
    except Exception:
        _render_dock_qc = None


def render_dock_qc(*args: Any, **kwargs: Any):
    if _render_dock_qc is None:
        raise PipelineError("vision_qc.render_dock_qc unavailable (vision_qc.py import failed on this host)")
    return _render_dock_qc(*args, **kwargs)


# ----------------------------- small utilities -----------------------------

class PipelineError(RuntimeError):
    pass


def q(x: Any) -> str:
    return shlex.quote(str(x))


def log(msg: str) -> None:
    print(msg, flush=True)


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def run_cmd(cmd: Sequence[str], *, cwd: Path, env: Optional[Dict[str, str]], log_path: Path, allow_fail: bool = False) -> Tuple[str, int]:
    cwd.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    command_line = "$ " + " ".join(q(c) for c in cmd)
    log(command_line)
    proc = subprocess.run(list(cmd), cwd=str(cwd), env=env, text=True, capture_output=True)
    text = command_line + "\n\n" + (proc.stdout or "")
    if proc.stderr:
        text += "\nSTDERR:\n" + proc.stderr
    log_path.write_text(text)
    if proc.returncode != 0 and not allow_fail:
        raise PipelineError(f"Command failed with exit code {proc.returncode}; see {log_path}")
    return text, proc.returncode


def make_env(phenix_bin: Optional[str]) -> Dict[str, str]:
    env = os.environ.copy()
    if phenix_bin:
        env["PATH"] = str(Path(phenix_bin).expanduser().resolve()) + os.pathsep + env.get("PATH", "")
    return env


def require_exe(name_or_path: str, env: Optional[Dict[str, str]] = None) -> str:
    p = Path(name_or_path).expanduser()
    if p.exists() and os.access(p, os.X_OK):
        return str(p.resolve())
    found = shutil.which(name_or_path, path=None if env is None else env.get("PATH"))
    if not found:
        raise PipelineError(f"Required executable not found: {name_or_path}")
    return found


def safe_id(text: str, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())
    text = text.strip("._-")
    return text[:80] or fallback


# ----------------------------- FASTA/model IO -----------------------------

@dataclass
class ChainSeq:
    chain_id: str
    name: str
    sequence: str
    fasta_path: Optional[str] = None


@dataclass
class DockResult:
    chain_id: str
    chain_name: str
    model_path: str
    provider: str
    hand: str
    search: int
    dock_model: str
    score_map: str
    dock_map: str
    cc: Dict[str, Any]
    passed: bool
    model_original_hand: Optional[str] = None


def clean_sequence(seq: str) -> str:
    seq = re.sub(r"[^A-Za-z]", "", seq).upper()
    if not seq:
        raise PipelineError("Empty FASTA sequence")
    return seq


def parse_fasta_multi(fasta_path: Path) -> List[ChainSeq]:
    text = fasta_path.read_text()
    records: List[ChainSeq] = []
    header: Optional[str] = None
    parts: List[str] = []

    def flush() -> None:
        nonlocal header, parts
        if header is None:
            return
        seq = clean_sequence("".join(parts))
        tokens = header.split()
        raw_id = tokens[0] if tokens else f"chain{len(records)+1}"
        # Common headers: >A, >chainA, >A|protein_name, >sp|Q...|NAME
        if "|" in raw_id:
            first = raw_id.split("|")[0]
            raw_chain = first if first else f"chain{len(records)+1}"
        else:
            raw_chain = raw_id
        chain_id = safe_id(raw_chain, f"chain{len(records)+1}")
        if len(chain_id) > 2 and chain_id.lower().startswith("chain"):
            chain_id = chain_id[5:] or chain_id
        # PDB chain IDs are most compatible as one character. Keep full name separately.
        if len(chain_id) > 1:
            chain_id = chain_id[0]
        used = {r.chain_id for r in records}
        if chain_id in used:
            alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
            for c in alphabet:
                if c not in used:
                    chain_id = c
                    break
        records.append(ChainSeq(chain_id=chain_id, name=header, sequence=seq))
        header, parts = None, []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            flush()
            header = line[1:].strip() or f"chain{len(records)+1}"
        else:
            parts.append(line)
    flush()
    if not records:
        raise PipelineError(f"No FASTA records found in {fasta_path}")
    return records


def write_chain_fastas(chains: List[ChainSeq], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for ch in chains:
        path = out_dir / f"chain_{ch.chain_id}.fasta"
        path.write_text(f">{ch.chain_id} {ch.name}\n{ch.sequence}\n")
        ch.fasta_path = str(path.resolve())


def load_model_report(report_path: Path) -> Dict[str, Any]:
    if not report_path.exists():
        raise PipelineError(f"Missing model report: {report_path}")
    return json.loads(report_path.read_text())


def predict_models_for_chain(
    chain: ChainSeq,
    *,
    out_dir: Path,
    python_exe: str,
    modeling_script: Path,
    provider: str,
    continue_on_error: bool,
    api_url: Optional[str],
    api_token_env: Optional[str],
    skip_modeling: bool,
    models_dir: Optional[Path],
    model_state: str = "",
    target_res_from: Optional[float] = None,
    target_res_to: Optional[float] = None,
) -> List[Dict[str, Any]]:
    chain_dir = out_dir / f"chain_{chain.chain_id}"
    chain_dir.mkdir(parents=True, exist_ok=True)

    if skip_modeling:
        if not models_dir:
            raise PipelineError("--skip-modeling requires --models-dir")
        # Prefer exact chain-ID model filenames.  Do NOT start with a loose
        # '*{chain_id}*' search: one-letter IDs such as G or N can match words in
        # other chain filenames (e.g. 'Gprotein'), silently assigning the wrong
        # model and creating overlapping duplicate chains in combined refinement.
        suffixes = {".pdb", ".cif", ".mmcif"}
        exact_patterns = [
            f"chain_{chain.chain_id}_*.pdb", f"chain_{chain.chain_id}_*.cif", f"chain_{chain.chain_id}_*.mmcif",
            f"chain-{chain.chain_id}-*.pdb", f"chain-{chain.chain_id}-*.cif", f"chain-{chain.chain_id}-*.mmcif",
            f"{chain.chain_id}_*.pdb", f"{chain.chain_id}_*.cif", f"{chain.chain_id}_*.mmcif",
            f"{chain.chain_id}.*",
        ]
        candidates: List[Path] = []
        seen: set[Path] = set()
        for pat in exact_patterns:
            for p in sorted(models_dir.glob(pat)):
                if p.suffix.lower() in suffixes and p.resolve() not in seen:
                    candidates.append(p)
                    seen.add(p.resolve())
        if not candidates:
            # Last-resort loose search, but require the chain ID as a filename token.
            token_re = re.compile(rf"(^|[_\\-.])chain[_\\-]?{re.escape(chain.chain_id)}([_\\-.]|$)|(^|[_\\-.]){re.escape(chain.chain_id)}([_\\-.]|$)", re.I)
            for p in sorted(models_dir.iterdir()):
                if p.suffix.lower() in suffixes and token_re.search(p.stem):
                    candidates.append(p)
        if not candidates:
            raise PipelineError(f"No exact model for chain {chain.chain_id} in {models_dir}")
        return [{"provider": "existing", "model_path": str(candidates[0].resolve()), "chain_id": chain.chain_id}]

    cmd = [
        python_exe,
        str(modeling_script),
        "--fasta", str(chain.fasta_path),
        "--provider", provider,
        "--out-dir", str(chain_dir),
    ]
    if continue_on_error:
        cmd.append("--continue-on-error")
    if api_url:
        cmd += ["--api-url", api_url]
    if api_token_env:
        cmd += ["--api-token-env", api_token_env]
    if model_state:
        cmd += ["--model-state", model_state]
    if target_res_from is not None:
        cmd += ["--target-res-from", str(target_res_from)]
    if target_res_to is not None:
        cmd += ["--target-res-to", str(target_res_to)]
    run_cmd(cmd, cwd=chain_dir, env=os.environ.copy(), log_path=chain_dir / "modeling.log", allow_fail=continue_on_error)

    report = load_model_report(chain_dir / "model_api_report.json")
    models: List[Dict[str, Any]] = []
    for r in report.get("results", []):
        mp = r.get("model_path")
        if mp and Path(mp).exists():
            rr = dict(r)
            rr["chain_id"] = chain.chain_id
            models.append(rr)
    if not models:
        best = report.get("best_model")
        if best and Path(best).exists():
            models.append({"provider": "best", "model_path": best, "chain_id": chain.chain_id})
    if not models:
        raise PipelineError(f"No predicted model found for chain {chain.chain_id}; see {chain_dir}")
    return models


def choose_models(models: List[Dict[str, Any]], dock_all_models: bool, max_models: int) -> List[Dict[str, Any]]:
    if dock_all_models:
        return models[:max_models]
    priority = ["alphafold", "swissmodel", "esmfold", "generic", "rcsb_template", "existing", "best"]
    for p in priority:
        for m in models:
            if str(m.get("provider")) == p:
                return [m]
    return [models[0]]


# ----------------------------- map operations -----------------------------

def read_map_header(path: Path) -> Dict[str, Any]:
    with mrcfile.open(path, permissive=True) as m:
        voxel = m.voxel_size
        origin = m.header.origin
        return {
            "shape_zyx": [int(x) for x in m.data.shape],
            "voxel_size_xyz": [float(voxel.x), float(voxel.y), float(voxel.z)],
            "origin_xyz": [float(origin.x), float(origin.y), float(origin.z)],
        }


def mirror_map(input_map: Path, output_map: Path, axis: str) -> Path:
    axis = axis.lower()
    if axis not in {"x", "y", "z"}:
        raise PipelineError("mirror axis must be x, y, or z")
    output_map.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.open(input_map, permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32).copy()
        voxel = (float(m.voxel_size.x), float(m.voxel_size.y), float(m.voxel_size.z))
        origin = (float(m.header.origin.x), float(m.header.origin.y), float(m.header.origin.z))
    if axis == "x":
        out_data = data[:, :, ::-1]
    elif axis == "y":
        out_data = data[:, ::-1, :]
    else:
        out_data = data[::-1, :, :]
    with mrcfile.new(output_map, overwrite=True) as out:
        out.set_data(np.asarray(out_data, dtype=np.float32))
        out.voxel_size = voxel
        out.header.origin.x, out.header.origin.y, out.header.origin.z = origin
        out.header.nxstart = 0
        out.header.nystart = 0
        out.header.nzstart = 0
        out.update_header_stats()
    return output_map.resolve()


def preprocess_docking_map(input_map: Path, output_map: Path, lower_percentile: float, upper_percentile: float, normalize: bool) -> Dict[str, Any]:
    output_map.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.open(input_map, permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32).copy()
        voxel = (float(m.voxel_size.x), float(m.voxel_size.y), float(m.voxel_size.z))
        origin = (float(m.header.origin.x), float(m.header.origin.y), float(m.header.origin.z))
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        raise PipelineError(f"Map has no finite voxels: {input_map}")
    # Percentile basis: normally computed over the WHOLE box (fine for a dense/full map, where
    # background genuinely is most of the volume). But if the input has already been pre-masked to
    # a sub-region (e.g. one lobe of a multi-domain complex, to keep ChimeraX/Phenix from locking
    # onto the wrong domain), a large chunk of the box is now exact zero from that masking -- taking
    # percentiles over the WHOLE box then makes lower/upper collapse near zero (both percentiles
    # land inside the masked-out zero bulk), so `upper_percentile` clips almost the ENTIRE real
    # signal down to one plateau value near zero. That plateau is what breaks ChimeraX's fitmap
    # (auto contour level lands on/above the plateau -> 0 fits) and destroys contrast for docking.
    # Fix: if a large fraction of the box is exactly zero (a strong sign of prior masking), compute
    # the percentiles on the NONZERO population instead, so the floor/cap stay calibrated to the
    # actual signal region. Behavior for a normal dense/full map (few exact zeros) is unchanged.
    exact_zero_frac = float(np.count_nonzero(finite == 0.0)) / finite.size
    percentile_basis = finite
    if exact_zero_frac > 0.2:
        nz = finite[finite != 0.0]
        if nz.size > 0:
            percentile_basis = nz
    work = data.copy()
    low = float(np.percentile(percentile_basis, lower_percentile))
    high = float(np.percentile(percentile_basis, upper_percentile))
    work[work < low] = 0.0
    work = np.minimum(work, high)
    if normalize:
        pos = work[work > 0]
        if pos.size:
            mean = float(np.mean(pos))
            std = float(np.std(pos))
            if std > 0:
                work = (work - mean) / std
                work[work < 0] = 0.0
    with mrcfile.new(output_map, overwrite=True) as out:
        out.set_data(np.asarray(work, dtype=np.float32))
        out.voxel_size = voxel
        out.header.origin.x, out.header.origin.y, out.header.origin.z = origin
        out.header.nxstart = 0
        out.header.nystart = 0
        out.header.nzstart = 0
        out.update_header_stats()
    report = {
        "input_map": str(input_map.resolve()),
        "output_map": str(output_map.resolve()),
        "purpose": "docking_only_threshold_clip_map",
        "lower_percentile": lower_percentile,
        "lower_value": low,
        "upper_percentile": upper_percentile,
        "upper_value": high,
        "normalize_positive_density": normalize,
        "input_exact_zero_fraction": exact_zero_frac,
        "percentile_basis": "nonzero_only" if percentile_basis is not finite else "whole_box",
        "nonzero_voxel_fraction": float(np.count_nonzero(work) / work.size),
        "input_min": float(np.nanmin(data)),
        "input_max": float(np.nanmax(data)),
        "output_min": float(np.nanmin(work)),
        "output_max": float(np.nanmax(work)),
        "voxel_size_xyz": list(voxel),
        "origin_xyz": list(origin),
    }
    output_map.with_suffix(output_map.suffix + ".preprocess_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def _expand_to_multiple(start: int, stop: int, limit: int, multiple: int) -> Tuple[int, int]:
    if multiple <= 1:
        return start, stop
    size = stop - start
    target = int(math.ceil(size / multiple) * multiple)
    extra = max(0, target - size)
    left = extra // 2
    right = extra - left
    start = max(0, start - left)
    stop = min(limit, stop + right)
    size = stop - start
    missing = target - size
    if missing > 0 and start == 0:
        stop = min(limit, stop + missing)
    elif missing > 0 and stop == limit:
        start = max(0, start - missing)
    return start, stop


def autobox_map(input_map: Path, output_map: Path, percentile: float, margin_A: float, multiple: int) -> Dict[str, Any]:
    """Crop empty borders from a docking map while preserving physical coordinates.

    The MRC origin is shifted by the crop start, so docked atomic coordinates remain
    compatible with the corresponding full real map hand.
    """
    output_map.parent.mkdir(parents=True, exist_ok=True)
    with mrcfile.open(input_map, permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32).copy()
        voxel = (float(m.voxel_size.x), float(m.voxel_size.y), float(m.voxel_size.z))
        origin = (float(m.header.origin.x), float(m.header.origin.y), float(m.header.origin.z))
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        raise PipelineError(f"Map has no finite voxels: {input_map}")
    thr = float(np.percentile(finite, percentile))
    mask = np.isfinite(data) & (data > thr)
    if not np.any(mask):
        raise PipelineError(f"Autobox threshold selected no voxels for {input_map}")
    labels, nlab = ndimage.label(mask)
    if nlab > 1:
        counts = np.bincount(labels.ravel())
        counts[0] = 0
        mask = labels == int(np.argmax(counts))
    coords = np.argwhere(mask)
    z0, y0, x0 = coords.min(axis=0)
    z1, y1, x1 = coords.max(axis=0) + 1
    mz = int(math.ceil(margin_A / voxel[2]))
    my = int(math.ceil(margin_A / voxel[1]))
    mx = int(math.ceil(margin_A / voxel[0]))
    nz, ny, nx = data.shape
    z0, z1 = max(0, z0 - mz), min(nz, z1 + mz)
    y0, y1 = max(0, y0 - my), min(ny, y1 + my)
    x0, x1 = max(0, x0 - mx), min(nx, x1 + mx)
    z0, z1 = _expand_to_multiple(z0, z1, nz, multiple)
    y0, y1 = _expand_to_multiple(y0, y1, ny, multiple)
    x0, x1 = _expand_to_multiple(x0, x1, nx, multiple)
    cropped = data[z0:z1, y0:y1, x0:x1]
    new_origin = (origin[0] + x0 * voxel[0], origin[1] + y0 * voxel[1], origin[2] + z0 * voxel[2])
    with mrcfile.new(output_map, overwrite=True) as out:
        out.set_data(np.asarray(cropped, dtype=np.float32))
        out.voxel_size = voxel
        out.header.origin.x, out.header.origin.y, out.header.origin.z = new_origin
        out.header.nxstart = 0
        out.header.nystart = 0
        out.header.nzstart = 0
        out.update_header_stats()
    report = {
        "input_map": str(input_map.resolve()),
        "output_map": str(output_map.resolve()),
        "purpose": "autoboxed_docking_only_map_preserve_origin",
        "threshold_percentile": percentile,
        "threshold_value": thr,
        "margin_A": margin_A,
        "multiple": multiple,
        "input_shape_zyx": [int(v) for v in data.shape],
        "output_shape_zyx": [int(v) for v in cropped.shape],
        "bbox_zyx_start_stop": {"z": [int(z0), int(z1)], "y": [int(y0), int(y1)], "x": [int(x0), int(x1)]},
        "old_origin_xyz": list(origin),
        "new_origin_xyz": list(new_origin),
        "voxel_size_xyz": list(voxel),
        "volume_fraction_kept": float(cropped.size / data.size),
    }
    output_map.with_suffix(output_map.suffix + ".autobox_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def prepare_maps(
    input_map: Path,
    out_dir: Path,
    axis: str,
    lower: float,
    upper: float,
    normalize: bool,
    autobox: bool,
    autobox_percentile: float,
    autobox_margin_A: float,
    autobox_multiple: int,
) -> Dict[str, Any]:
    maps_dir = out_dir / "00_maps"
    maps_dir.mkdir(parents=True, exist_ok=True)
    real_original = input_map.resolve()
    real_flip = maps_dir / f"{input_map.stem}_flip_{axis}.mrc"
    if not real_flip.exists():
        mirror_map(real_original, real_flip, axis)

    # Requested order: first make real hand maps, then preprocess each hand separately.
    proc_original = maps_dir / f"{input_map.stem}_original_dock_threshold_clip_p{lower:g}_cap{upper:g}.mrc"
    proc_flip = maps_dir / f"{input_map.stem}_flip_{axis}_dock_threshold_clip_p{lower:g}_cap{upper:g}.mrc"
    preprocess_original = preprocess_docking_map(real_original, proc_original, lower, upper, normalize)
    preprocess_flip = preprocess_docking_map(real_flip, proc_flip, lower, upper, normalize)

    dock_original = proc_original
    dock_flip = proc_flip
    autobox_reports: List[Dict[str, Any]] = []
    if autobox:
        dock_original = maps_dir / f"{proc_original.stem}_autobox_p{autobox_percentile:g}_margin{autobox_margin_A:g}.mrc"
        dock_flip = maps_dir / f"{proc_flip.stem}_autobox_p{autobox_percentile:g}_margin{autobox_margin_A:g}.mrc"
        autobox_reports.append(autobox_map(proc_original, dock_original, autobox_percentile, autobox_margin_A, autobox_multiple))
        autobox_reports.append(autobox_map(proc_flip, dock_flip, autobox_percentile, autobox_margin_A, autobox_multiple))

    return {
        "real_original": str(real_original),
        "real_flip": str(real_flip.resolve()),
        "processed_original_full_grid": str(proc_original.resolve()),
        "processed_flip_full_grid": str(proc_flip.resolve()),
        "dock_original": str(dock_original.resolve()),
        "dock_flip": str(dock_flip.resolve()),
        "preprocess_reports": [preprocess_original, preprocess_flip],
        "autobox_docking_maps": autobox,
        "autobox_reports": autobox_reports,
        "map_header": read_map_header(real_original),
    }


# ----------------------------- docking/scoring -----------------------------

def map_center_xyz(map_path: Path) -> Tuple[float, float, float]:
    """Return the physical center of an MRC/MAP grid in XYZ Angstrom coordinates."""
    header = read_map_header(map_path)
    nz, ny, nx = header["shape_zyx"]
    ax, ay, az = header["voxel_size_xyz"]
    ox, oy, oz = header["origin_xyz"]
    return (
        ox + 0.5 * (nx - 1) * ax,
        oy + 0.5 * (ny - 1) * ay,
        oz + 0.5 * (nz - 1) * az,
    )


def model_center_xyz(model_path: Path) -> Tuple[float, float, float]:
    """Return centroid of all atoms in a PDB/mmCIF model."""
    st = gemmi.read_structure(str(model_path))
    xs: List[float] = []
    ys: List[float] = []
    zs: List[float] = []
    for model in st:
        for chain in model:
            for res in chain:
                for atom in res:
                    xs.append(float(atom.pos.x))
                    ys.append(float(atom.pos.y))
                    zs.append(float(atom.pos.z))
    if not xs:
        raise PipelineError(f"No atoms found in model: {model_path}")
    return (float(sum(xs) / len(xs)), float(sum(ys) / len(ys)), float(sum(zs) / len(zs)))


def count_model_residues(model_path: Path) -> int:
    """Total polymer residue count across all chains of a PDB/mmCIF model.

    Used by `check_lobe_crop_size()` to estimate the ACTUAL candidate model's volume -- e.g. a
    272-residue receptor-only model or a 224-residue paired-Fv model, not the (possibly much
    longer, BRIL/fusion-partner-inclusive) source FASTA record length.
    """
    st = gemmi.read_structure(str(model_path))
    n = 0
    for model in st:
        for chain in model:
            for res in chain:
                if any(a.name == "CA" for a in res):
                    n += 1
    return n


def translate_model_to_map_center(input_model: Path, dock_map: Path, output_model: Path) -> Dict[str, Any]:
    """Translate model centroid to docking-map center before random fit search.

    This keeps the model inside the searched map box and avoids wasting random starts on
    an initial coordinate frame far away from the map. It applies only a rigid-body
    translation and does not change hand/chirality or internal geometry.
    """
    st = gemmi.read_structure(str(input_model))
    before = model_center_xyz(input_model)
    target = map_center_xyz(dock_map)
    shift = (target[0] - before[0], target[1] - before[1], target[2] - before[2])
    for model in st:
        for chain in model:
            for res in chain:
                for atom in res:
                    atom.pos.x += shift[0]
                    atom.pos.y += shift[1]
                    atom.pos.z += shift[2]
    output_model.parent.mkdir(parents=True, exist_ok=True)
    st.write_pdb(str(output_model))
    report = {
        "input_model": str(input_model.resolve()),
        "dock_map": str(dock_map.resolve()),
        "output_model": str(output_model.resolve()),
        "model_center_before_xyz": list(before),
        "dock_map_center_xyz": list(target),
        "translation_xyz": list(shift),
        "purpose": "center_starting_model_in_docking_map_before_chimerax_random_search",
    }
    output_model.with_suffix(output_model.suffix + ".center_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def estimate_expected_voxels(n_residues: int, voxel_size_A: float, avg_residue_da: float = 110.0, specific_volume_A3_per_da: float = 1.212) -> float:
    """Rough expected voxel count for a protein chain's ordered density, from residue count.

    MW ~= n_residues * avg_residue_da (average amino-acid residue mass in a chain, water-loss
    corrected). Volume ~= MW * specific_volume_A3_per_da (~1.2 A^3/Da is a standard protein
    partial-specific-volume conversion). Divided by the docking map's voxel volume to get a voxel
    count. This is only used to pick a SENSIBLE ChimeraX contour level for fitmap (see
    `estimate_contour_level_for_mass`), not for any scientific measurement.
    """
    mw_da = max(1, n_residues) * avg_residue_da
    volume_A3 = mw_da * specific_volume_A3_per_da
    voxel_volume_A3 = max(voxel_size_A, 1e-6) ** 3
    return max(1.0, volume_A3 / voxel_volume_A3)


def count_voxels_above_level(map_path: Path, level: float) -> int:
    """Count voxels strictly above `level` -- the map's own above-threshold enclosed volume."""
    with mrcfile.open(map_path, permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32)
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return 0
    return int(np.count_nonzero(finite > level))


def _absolute_quantile_level(map_path: Path, box_fraction: float) -> float:
    """Absolute density level enclosing ~box_fraction of THIS map's own box, computed directly
    from the histogram -- deliberately NOT `map_envelope_contour_level()` and NOT reading the
    module-level `DOCK_CONTOUR_BOX_FRACTION`/`DOCK_CONTOUR_LEVEL` globals, so this helper's
    result is independent of whatever --dock-contour-* the CLI run is using elsewhere.
    """
    with mrcfile.open(map_path, permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32)
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return 0.0
    frac = min(max(box_fraction, 1e-6), 0.5)
    return float(np.quantile(finite, 1.0 - frac))


def check_lobe_crop_size(lobe_map_path: Path, whole_map_path: Path, n_residues: int,
                          min_ratio: float = 3.0, level_box_fraction: float = 0.005) -> Dict[str, Any]:
    """WARN when a --target-lobe-map crop is small relative to the model being docked into it.

    PowerFit's advantage over the old ChimeraX ladder is a Laplace EDGE filter, which needs
    surrounding context to work with -- a lobe cropped tightly around a small domain starves it.
    Measured on cb1_redo (2026-08-19): the same paired-Fv model (~25 kDa) scored CC_mask 0.08
    (hand_ratio 1.2-1.3, REJECT) against a tight Fv-only sub-lobe, but 0.56 (hand_ratio 5.6,
    ACCEPT) against the WHOLE map -- same model, same hand, only the target-map SCOPE differed.

    IMPORTANT (found while validating this check): the level MUST be an ABSOLUTE density value
    derived from the WHOLE map, not a level re-derived from the lobe crop itself via
    `map_envelope_contour_level(box_fraction=...)`. That function is normalized to always enclose
    the SAME box_fraction of whatever grid it is given -- since every lobe/whole-map variant here
    shares the identical 416^3 grid (lobes are built by zeroing outside a half-space/slab, not by
    physically cropping the array), a box_fraction level recomputed per-map returns ~identical
    "enclosed volume" for a huge fab_lobe and a tiny fv_sublobe, making the check blind to the
    actual crop size by construction. Deriving the level ONCE from the WHOLE map instead, at a
    stringent `level_box_fraction` that isolates only the highest-density core (default 0.005,
    tighter than the 0.05/0.016 used elsewhere for the full particle envelope), and applying that
    SAME absolute level to the lobe crop, correctly separates the two real cb1_redo outcomes:
    receptor_lobe (272-residue model, WORKED: CC_mask 0.5725 ACCEPT) -> ratio 3.53x, above the
    default 3x floor; fv_sublobe AND the whole fab_lobe (224-residue paired-Fv model, both
    FAILED: CC_mask 0.08 and 0.0688, both REJECT) -> ratio 1.11x / 1.43x, below it.

    Below `min_ratio` (default ~3x -- "comfortably larger" than the model, not merely bigger),
    logs a LOUD warning naming the ratio and recommending the whole map instead. Purely
    advisory -- never raises, never blocks the run.
    """
    lobe_map_path = Path(lobe_map_path)
    whole_map_path = Path(whole_map_path)
    header = read_map_header(lobe_map_path)
    voxel_size_A = float(np.mean(header["voxel_size_xyz"]))
    expected_voxels = estimate_expected_voxels(max(1, int(n_residues)), voxel_size_A)
    level = _absolute_quantile_level(whole_map_path, level_box_fraction)
    enclosed_voxels = count_voxels_above_level(lobe_map_path, level)
    ratio = (enclosed_voxels / expected_voxels) if expected_voxels > 0 else float("inf")
    result = {
        "lobe_map": str(lobe_map_path),
        "whole_map": str(whole_map_path),
        "n_residues": int(n_residues),
        "voxel_size_A": voxel_size_A,
        "expected_model_voxels": expected_voxels,
        "lobe_enclosed_voxels_at_level": enclosed_voxels,
        "level_box_fraction": level_box_fraction,
        "contour_level": level,
        "crop_to_model_ratio": ratio,
        "min_ratio_recommended": min_ratio,
    }
    if not math.isfinite(ratio) or ratio < min_ratio:
        log(f"WARNING: --target-lobe-map crop {lobe_map_path.name} looks SMALL relative to the "
            f"{n_residues}-residue model about to be docked into it: enclosed volume / expected "
            f"model volume = {ratio:.2f}x (recommended >= {min_ratio:g}x). PowerFit's Laplace "
            f"edge filter needs surrounding context -- a tight crop starves it. Measured on "
            f"cb1_redo: the SAME model scored CC_mask 0.08 (hand_ratio 1.2-1.3, REJECT) in a "
            f"tight sub-lobe crop vs 0.56 (hand_ratio 5.6, ACCEPT) on the WHOLE map. Consider "
            f"dropping --target-lobe-map and docking against the whole map instead (see "
            f"PORT_NOTES.md sec2/sec3 -- PowerFit handles multi-component maps unaided).")
    else:
        log(f"--target-lobe-map crop {lobe_map_path.name}: enclosed/expected volume ratio "
            f"{ratio:.2f}x (>= {min_ratio:g}x) -- comfortably larger than the {n_residues}-residue "
            f"model, PowerFit's edge filter should have adequate context.")
    return result


def estimate_contour_level_for_mass(map_path: Path, expected_voxels: float) -> float:
    """Pick a ChimeraX contour level so the enclosed volume matches ~expected_voxels.

    Root-cause fix for a regression where ChimeraX's OWN auto-level heuristic lands at/above the
    map's plateau/cap value on the tool's floor+cap-clipped docking maps (`preprocess_docking_map`
    zeros below `lower_percentile` and caps at `upper_percentile`, producing a large flat plateau of
    tied values at the cap) -> auto-level picks a level with ~0 voxels above it -> `fitmap search`
    finds 0 fits and silently leaves the model at its untouched start pose (confirmed empirically:
    "Found 0 unique fits from N random placements" in the ChimeraX log, docked.pdb == start pose).
    Instead of trusting ChimeraX's auto-level, explicitly set a level in `make_chimerax_script`
    derived from the chain's own expected molecular volume (mass-consistent, not an arbitrary
    percentile), which is robust to how aggressively the docking map was pre-clipped.
    """
    with mrcfile.open(map_path, permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32)
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return 0.0
    frac = min(max(expected_voxels / finite.size, 1e-6), 0.5)
    level = float(np.quantile(finite, 1.0 - frac))
    pos = finite[finite > 0]
    # Safety clamp: on a very sparse/skewed docking map (most mass concentrated in a thin high tail
    # -- e.g. a small chain's mass-quantile landing inside a much bigger complex's box, or any map
    # whose positive tail is itself heavily right-skewed), the raw mass-quantile can still land at
    # or near the map's own maximum/plateau, reproducing the exact "0 fits" failure this function
    # exists to prevent. Never let the level exceed the 90th percentile of the POSITIVE population,
    # so there is always genuine density both above and below the chosen contour.
    if pos.size:
        cap = float(np.quantile(pos, 0.90))
        if not np.isfinite(level) or level <= 0.0 or level > cap:
            level = cap
    else:
        level = 0.0
    return level


# --- test_mask patch 2026-07-29: contour level from the MAP alone ---------------------------
# The contour level is a property of the MAP, not of the chain being docked. Deriving it from a
# residue count assumed the map holds exactly one copy of that chain, which is false for every
# symmetric oligomer (the "mask one subunit of a C3/C4 complex" case): the level came out 3-4x
# too high and ChimeraX fitmap returned "0 of N fits". Residue counts are gone; the level is now
# read off the map's own histogram as "the value above which DOCK_CONTOUR_BOX_FRACTION of the box
# lies", which reproduces the empirically working levels (TRPV1 1.33, HA 2.28) without any
# knowledge of what is in the map.
DOCK_CONTOUR_BOX_FRACTION: float = 0.05
DOCK_CONTOUR_LEVEL: Optional[float] = None


def map_envelope_contour_level(map_path: Path, box_fraction: Optional[float] = None) -> float:
    """Contour level enclosing ~box_fraction of the box, from the map histogram alone."""
    if DOCK_CONTOUR_LEVEL is not None:
        return float(DOCK_CONTOUR_LEVEL)
    frac = float(box_fraction if box_fraction is not None else DOCK_CONTOUR_BOX_FRACTION)
    frac = min(max(frac, 1e-4), 0.5)
    with mrcfile.open(map_path, permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32)
    finite = data[np.isfinite(data)]
    if finite.size == 0:
        return 0.0
    top = float(np.max(finite))
    level = float(np.quantile(finite, 1.0 - frac))
    # Plateau guard: preprocess_docking_map caps the map, so a large tied population can sit at
    # the maximum. A level at/above that plateau leaves nothing above the contour -> 0 fits.
    tries = 0
    while level >= top * 0.999 and tries < 6:
        frac = min(frac * 2.0, 0.5)
        level = float(np.quantile(finite, 1.0 - frac))
        tries += 1
    if not np.isfinite(level) or level <= 0.0:
        pos = finite[finite > 0]
        level = float(np.quantile(pos, 0.5)) if pos.size else 0.0
    return level


def contour_level_for_chain(map_path: Path, n_residues: Optional[int] = None) -> float:
    """Contour level for a map. `n_residues` is ignored (kept for call compatibility)."""
    return map_envelope_contour_level(map_path)


def make_chimerax_script(map_path: Path, model_path: Path, out_model: Path, search: int, resolution: float, level: Optional[float] = None) -> str:
    level_line = f"volume #1 level {level}\n" if level is not None else ""
    return f"""open {map_path}
open {model_path}
{level_line}fitmap #2 inMap #1 search {search} metric correlation resolution {resolution}
save {out_model} #2
exit
"""


def chimerax_fitmap(
    *,
    chimerax_exe: str,
    model: Path,
    dock_map: Path,
    out_dir: Path,
    search: int,
    resolution: float,
    n_residues: Optional[int] = None,
) -> Tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_model = out_dir / "docked.pdb"
    centered_model = out_dir / "centered_start_model.pdb"
    center_report = translate_model_to_map_center(model, dock_map, centered_model)
    cxc = out_dir / "chimerax_fitmap.cxc"
    log_path = out_dir / "chimerax_fitmap.log"
    level = map_envelope_contour_level(dock_map)
    (out_dir / "contour_level_report.json").write_text(json.dumps(
        {"method": "map_envelope", "box_fraction": DOCK_CONTOUR_BOX_FRACTION,
         "explicit_level": DOCK_CONTOUR_LEVEL, "level": level}, indent=2) + "\n")
    cxc.write_text(make_chimerax_script(dock_map, centered_model, out_model, search, resolution, level=level))
    # Gate 5 (robust ChimeraX invocation): run the .cxc command file via `--cmd "open <file>"`,
    # never `--script <file>.cxc` -- some ChimeraX builds route --script through `runscript`,
    # which expects a PYTHON file and raises a SyntaxError on a plain command script (this bit
    # vis_res_utils.render_map_surface; see PORT_NOTES.md).
    cmd = [chimerax_exe, "--nogui", "--exit", "--cmd", f"open {cxc}"]
    run_cmd(cmd, cwd=out_dir, env=os.environ.copy(), log_path=log_path, allow_fail=False)
    if not out_model.exists():
        raise PipelineError(f"ChimeraX did not write docked model: {out_model}")
    fitmap_log_text = log_path.read_text(errors="replace") if log_path.exists() else ""
    m = re.search(r"Found (\d+) unique fits from (\d+) random placements", fitmap_log_text)
    if m and int(m.group(1)) == 0:
        raise PipelineError(
            f"ChimeraX fitmap found 0 of {m.group(2)} fits (level={level!r}) -- docked model was "
            f"NOT moved from its centered start pose; see {log_path}"
        )
    return out_model.resolve(), log_path.resolve()


def make_chimerax_local_fit_script(map_path: Path, model_path: Path, out_model: Path, resolution: float, max_steps: int, level: Optional[float]) -> str:
    level_line = f"volume #1 level {level}\n" if level is not None else ""
    return f"""open {map_path}
open {model_path}
{level_line}fitmap #2 inMap #1 metric correlation resolution {resolution} maxSteps {max_steps}
save {out_model} #2
exit
"""


def chimerax_local_refit(
    *,
    chimerax_exe: str,
    model: Path,
    whole_map: Path,
    out_dir: Path,
    resolution: float,
    max_steps: int = 150,
    n_residues: Optional[int] = None,
) -> Tuple[Path, Path]:
    """Local-only fitmap refinement of an ALREADY-PLACED pose in the whole (accepted-hand) map.

    Skill gate 2/4, stage 2 of the two-stage global(lobe)->local(whole) fit: unlike
    `chimerax_fitmap`, this performs NO random search and does NOT recenter the model to the
    map center -- the input pose is assumed to already be physically correct (e.g. the winning
    target-lobe-cut candidate from stage 1), and this step only locally optimizes it against
    the full/whole map. A lobe-only fit is a CANDIDATE, never returned as the final pose.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    out_model = out_dir / "docked_local.pdb"
    cxc = out_dir / "chimerax_local_refit.cxc"
    log_path = out_dir / "chimerax_local_refit.log"
    level: Optional[float] = None
    if n_residues:
        level = contour_level_for_chain(whole_map, n_residues)
    cxc.write_text(make_chimerax_local_fit_script(whole_map, model, out_model, resolution, max_steps, level))
    cmd = [chimerax_exe, "--nogui", "--exit", "--cmd", f"open {cxc}"]
    run_cmd(cmd, cwd=out_dir, env=os.environ.copy(), log_path=log_path, allow_fail=False)
    if not out_model.exists():
        raise PipelineError(f"ChimeraX local refit did not write model: {out_model}")
    return out_model.resolve(), log_path.resolve()


def parse_map_correlations(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for key in ["CC_mask", "CC_volume", "CC_peaks", "CC_box"]:
        m = re.search(rf"{key}\s*[:=]\s*([-+]?\d*\.\d+|[-+]?\d+)", text)
        if m:
            out[key] = float(m.group(1))
    # Phenix 2.0 local tables.
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.search(r"chain\s+ID\s+CC", line, re.I):
            for j in range(i + 1, min(i + 8, len(lines))):
                m = re.match(r"\s*([A-Za-z0-9])\s+([-+]?\d*\.\d+|[-+]?\d+)", lines[j])
                if m:
                    out.setdefault("chain_cc", {})[m.group(1)] = float(m.group(2))
                    out.setdefault("first_chain_CC", float(m.group(2)))
    for label, key in [("Main chain", "main_chain_CC"), ("Side chain", "side_chain_CC")]:
        for i, line in enumerate(lines):
            if label.lower() in line.lower():
                window = "\n".join(lines[i:i+3])
                m = re.search(r"([-+]?\d*\.\d+|[-+]?\d+)\s+[-+]?\d", window)
                if m:
                    out[key] = float(m.group(1))
                break
    return out


def phenix_map_correlations(
    *,
    env: Dict[str, str],
    model: Path,
    score_map: Path,
    out_dir: Path,
    resolution: float,
) -> Tuple[Dict[str, Any], Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "model_vs_map"
    log_path = out_dir / "map_correlations.log"
    cmd = [
        "phenix.map_model_cc",
        str(model),
        str(score_map),
        f"resolution={resolution}",
        f"output.prefix={prefix}",
        "output.overwrite=True",
    ]
    text, code = run_cmd(cmd, cwd=out_dir, env=env, log_path=log_path, allow_fail=True)
    cc = parse_map_correlations(text)
    cc["returncode"] = code
    cc["log_path"] = str(log_path.resolve())
    return cc, log_path.resolve()


def fraction_atoms_in_density(model_path: Path, map_path: Path, level: float) -> Dict[str, Any]:
    """Fraction of model atoms whose (nearest-voxel) map density is >= level.

    Skill gate 3's primary acceptance metric (target: >=60% atoms in density), used ALONGSIDE
    Phenix CC_mask/CC_volume (mean-subtracted map-model correlation via `phenix_map_correlations`)
    rather than ChimeraX's own reported `correlation`, which includes the DC (mean) term and can
    read misleadingly high for a poor fit (see references/hta1-j616-worked-example.md: the wrong
    hand read a ChimeraX correlation of 0.386 but only 33% atoms in density and lobe CC_mask 0.10,
    a false-PASS trap this dual metric is designed to catch). Nearest-voxel lookup is a coarse but
    fast/robust choice; it is only ever used to COMPARE two hands under identical conditions, not
    as an absolute physical measurement.
    """
    with mrcfile.open(map_path, permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32)
        voxel = (float(m.voxel_size.x), float(m.voxel_size.y), float(m.voxel_size.z))
        origin = (float(m.header.origin.x), float(m.header.origin.y), float(m.header.origin.z))
    nz, ny, nx = data.shape
    st = gemmi.read_structure(str(model_path))
    n_in = 0
    n_total = 0
    n_oob = 0
    for model in st:
        for chain in model:
            for res in chain:
                for atom in res:
                    n_total += 1
                    ix = int(round((atom.pos.x - origin[0]) / voxel[0])) if voxel[0] else 0
                    iy = int(round((atom.pos.y - origin[1]) / voxel[1])) if voxel[1] else 0
                    iz = int(round((atom.pos.z - origin[2]) / voxel[2])) if voxel[2] else 0
                    if 0 <= ix < nx and 0 <= iy < ny and 0 <= iz < nz:
                        if float(data[iz, iy, ix]) >= level:
                            n_in += 1
                    else:
                        n_oob += 1
    frac = (n_in / n_total) if n_total else 0.0
    return {
        "fraction_in_density": frac,
        "n_atoms_in_density": n_in,
        "n_atoms_total": n_total,
        "n_atoms_out_of_bounds": n_oob,
        "level": level,
        "map": str(map_path.resolve()),
    }


# ============================================================================================
# test_mask 2026-07-29: RIGID DOCKING = PowerFit (rough global) -> fitmap (precise local)
#
# Replaces the ChimeraX `fitmap search N` random-restart global search, which is stochastic
# (identical HA input gave CC_mask 0.724 / 0.058 / 0.046 on three runs) and slow (20-60 min per
# hand per search level). Measured on this cluster:
#
#   case            PowerFit    +fitmap polish   old fitmap-search      mirrored-map control
#   HA protomer     0.493/29s   0.7245/+35s      0.724 (or 0.046) /1h   0.044
#   TRPV1 subunit   0.110/46s   0.3502           0.013 (never placed)   0.041
#   hTA1 receptor   0.237/46s   0.3977           n/a                    0.062  (J616 wrong hand)
#
# PowerFit finds the right basin fast (Laplace pre-filter + core-weighted local CC, which is what
# keeps it off the bright G-protein density when only the receptor is being placed); its 10-degree
# rotation grid leaves a few degrees on the table, which the LOCAL fitmap recovers.
#
# CHIRALITY: the atomic model is never mirrored -- only the MAP hand is switched (see the module
# HANDEDNESS RULE). assert_model_chirality_ok() is re-checked after every transform here.
# ============================================================================================


def trim_model_to_density(model: Path, map_path: Path, out_path: Path, extend_residues: int,
                          level: Optional[float] = None, min_atom_fraction: float = 0.5) -> Dict[str, Any]:
    """Drop residues that sit OUTSIDE the map envelope, keeping a margin at both ends.

    A predicted model routinely covers more than the map resolves -- a flexible domain, a tail, a
    region the reconstruction averaged away. Those residues have no density to occupy, so they drag
    the masked CC down and, left in place, they enlarge the mask around empty space. Trimming them
    makes the CC a statement about the part of the model the map can actually support.

    A residue is IN density when at least `min_atom_fraction` of its atoms fall on voxels at or
    above the map envelope level. Retained stretches are then extended by `extend_residues` along
    the sequence in both directions, so the cut never bites into the ordered core and each kept
    segment carries a margin.
    """
    if level is None:
        level = map_envelope_contour_level(map_path)
    with mrcfile.open(str(map_path), permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32)
        apix = np.array([float(m.voxel_size.x), float(m.voxel_size.y), float(m.voxel_size.z)])
        org = np.array([float(m.header.origin.x), float(m.header.origin.y), float(m.header.origin.z)])
        nstart = np.array([int(m.header.nxstart), int(m.header.nystart), int(m.header.nzstart)])
    origin_A = org + nstart * apix
    nz, ny, nx = data.shape

    st = gemmi.read_structure(str(model))
    st.setup_entities()
    out_st = gemmi.Structure()
    out_st.cell = gemmi.UnitCell(1, 1, 1, 90, 90, 90)
    out_model = gemmi.Model("1")
    stats: Dict[str, Any] = {"level": float(level), "extend_residues": int(extend_residues), "chains": {}}
    for ch in st[0]:
        residues = list(ch)
        inside = []
        for res in residues:
            vals = []
            for at in res:
                v = (np.array([at.pos.x, at.pos.y, at.pos.z]) - origin_A) / apix
                ix, iy, iz = int(round(v[0])), int(round(v[1])), int(round(v[2]))
                if 0 <= ix < nx and 0 <= iy < ny and 0 <= iz < nz:
                    vals.append(float(data[iz, iy, ix]) >= float(level))
                else:
                    vals.append(False)
            inside.append(bool(vals) and (sum(vals) / len(vals)) >= min_atom_fraction)
        keep = list(inside)
        for i, ok in enumerate(inside):          # extend every in-density stretch along the sequence
            if ok:
                for j in range(max(0, i - extend_residues), min(len(inside), i + extend_residues + 1)):
                    keep[j] = True
        new_ch = gemmi.Chain(ch.name)
        for res, k in zip(residues, keep):
            if k:
                new_ch.add_residue(res.clone())
        stats["chains"][ch.name] = {"residues_in": len(residues), "in_density": int(sum(inside)),
                                    "kept": len(new_ch), "dropped": len(residues) - len(new_ch)}
        if len(new_ch):
            out_model.add_chain(new_ch)
    if len(out_model) == 0:
        raise PipelineError(f"Density trim removed every residue of {model} at level {level:.3f}")
    out_st.add_model(out_model)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_st.write_pdb(str(out_path))
    stats["output"] = str(out_path.resolve())
    stats["kept_total"] = sum(c["kept"] for c in stats["chains"].values())
    stats["dropped_total"] = sum(c["dropped"] for c in stats["chains"].values())
    return stats


def sanitize_model_for_docking(src: Path, dst: Path, keep_hetero: bool = False) -> Path:
    """Protein-only copy of a candidate model, so Phenix can score the docked pose.

    `rcsb_template` downloads a WHOLE PDB entry (ligands, lipids, metals, modified residues), and
    phenix.map_model_cc then dies with "Unknown scattering type" -- so every pose from that rung
    was docked but never scored, and the provider looked like it produced nothing. Strip to
    standard protein unless --keep-hetero.
    """
    st = gemmi.read_structure(str(src))
    st.setup_entities()
    st.remove_alternative_conformations()
    st.remove_hydrogens()
    if not keep_hetero:
        out = gemmi.Structure()
        out.cell = gemmi.UnitCell(1, 1, 1, 90, 90, 90)
        model = gemmi.Model("1")
        for ch in st[0]:
            keep = gemmi.Chain(ch.name)
            for res in ch:
                if res.name.strip().upper() in STANDARD_PROTEIN_RESNAMES:
                    keep.add_residue(res.clone())
            if len(keep):
                model.add_chain(keep)
        if len(model) == 0:
            raise PipelineError(f"No standard protein residues left in {src}")
        out.add_model(model)
        st = out
    dst.parent.mkdir(parents=True, exist_ok=True)
    st.write_pdb(str(dst))
    return dst.resolve()


def normalize_model_columns(src: Path, dst: Path) -> Path:
    """Rewrite a PDB through gemmi so Phenix accepts it.

    PowerFit writes the element LEFT-justified in columns 77-78 ("N " instead of " N"), and
    phenix.map_model_cc then aborts with `Unknown chemical element type`. A gemmi round trip
    normalizes the columns without touching coordinates.
    """
    st = gemmi.read_structure(str(src))
    st.setup_entities()
    dst.parent.mkdir(parents=True, exist_ok=True)
    st.write_pdb(str(dst))
    return dst.resolve()


def powerfit_search(
    *,
    powerfit_exe: str,
    model: Path,
    map_path: Path,
    out_dir: Path,
    resolution: float,
    angle: float,
    n_solutions: int,
    procs: int,
) -> List[Path]:
    """Rough global 6D fit: rotation grid x FFT translation scan. Returns solution models."""
    out_dir.mkdir(parents=True, exist_ok=True)
    run_dir = out_dir / "powerfit_run"
    cmd = [
        powerfit_exe, str(map_path), str(resolution), str(model),
        "-p", str(procs), "-a", str(angle), "-n", str(n_solutions), "-d", str(run_dir),
    ]
    run_cmd(cmd, cwd=out_dir, env=os.environ.copy(), log_path=out_dir / "powerfit.log", allow_fail=False)
    fits = sorted(run_dir.glob("fit_*.pdb"), key=lambda q: int(re.sub(r"\D", "", q.stem) or 0))
    fits = [f for f in fits if "_norm" not in f.stem]
    if not fits:
        raise PipelineError(f"PowerFit produced no solutions in {run_dir}; see {out_dir}/powerfit.log")
    return [normalize_model_columns(f, out_dir / f"{f.stem}_norm.pdb") for f in fits]


def dock_chain_powerfit(
    *,
    chain: ChainSeq,
    model_info: Dict[str, Any],
    maps: Dict[str, Any],
    out_dir: Path,
    cc_gate: float,
    cc_review: float,
    score_key: str,
    resolution: float,
    chimerax_exe: str,
    phenix_env: Dict[str, str],
    dock_hands: List[str],
    powerfit_exe: str,
    powerfit_angle: float,
    powerfit_solutions: int,
    powerfit_procs: int,
    local_max_steps: int,
    hand_ratio_min: float,
    keep_hetero: bool = False,
    trim_extend: Optional[int] = 10,
) -> Optional[DockResult]:
    """Rough PowerFit global search then precise local fitmap, scored by masked CC.

    Runs on the REAL map of each requested hand -- PowerFit does its own Laplace filtering,
    resampling and trimming, so the floor/cap-clipped `preprocess_docking_map` products (and the
    contour level they needed) are not used here at all.
    """
    raw_model = Path(model_info["model_path"]).resolve()
    provider = str(model_info.get("provider", "unknown"))
    model_path = sanitize_model_for_docking(
        raw_model, out_dir / f"{raw_model.stem}_protein_only.pdb", keep_hetero=keep_hetero)
    hand_real = {"original": "real_original", "flip": "real_flip"}
    per_hand: Dict[str, DockResult] = {}

    for hand in dock_hands:
        if hand not in hand_real:
            raise PipelineError(f"Unknown dock hand {hand!r}; expected original and/or flip")
        real_map = Path(maps[hand_real[hand]])
        hand_dir = out_dir / hand
        try:
            solutions = powerfit_search(
                powerfit_exe=powerfit_exe, model=model_path, map_path=real_map,
                out_dir=hand_dir / "global_powerfit", resolution=resolution,
                angle=powerfit_angle, n_solutions=powerfit_solutions, procs=powerfit_procs,
            )
        except Exception as e:
            (hand_dir).mkdir(parents=True, exist_ok=True)
            (hand_dir / "ERROR.txt").write_text(f"PowerFit global search failed: {e}\n")
            log(f"WARNING: chain {chain.chain_id} hand {hand}: PowerFit failed: {e}")
            continue

        best_here: Optional[DockResult] = None
        for rank, sol in enumerate(solutions, start=1):
            polish_dir = hand_dir / f"rank_{rank}_local_fitmap"
            try:
                assert_model_chirality_ok(sol, f"chain {chain.chain_id} hand={hand} powerfit rank={rank}")
                polished, _ = chimerax_local_refit(
                    chimerax_exe=chimerax_exe, model=sol, whole_map=real_map,
                    out_dir=polish_dir, resolution=resolution, max_steps=local_max_steps,
                )
                assert_model_chirality_ok(polished, f"chain {chain.chain_id} hand={hand} local fitmap rank={rank}")
                cc, _ = phenix_map_correlations(
                    env=phenix_env, model=polished, score_map=real_map,
                    out_dir=polish_dir / "cc_vs_real", resolution=resolution,
                )
                cc_rough, _ = phenix_map_correlations(
                    env=phenix_env, model=sol, score_map=real_map,
                    out_dir=polish_dir / "cc_rough", resolution=resolution,
                )
            except Exception as e:
                polish_dir.mkdir(parents=True, exist_ok=True)
                (polish_dir / "ERROR.txt").write_text(str(e) + "\n")
                log(f"WARNING: chain {chain.chain_id} hand {hand} rank {rank}: {e}")
                continue

            cc["powerfit_rank"] = rank
            cc["cc_before_polish"] = cc_rough.get(score_key)
            cc["gate_score_key"] = score_key
            cc["gate_score_value"] = float(cc.get(score_key, float("nan")))
            try:
                level = map_envelope_contour_level(real_map)
                cc["atoms_in_density_fraction"] = fraction_atoms_in_density(
                    polished, real_map, level)["fraction_in_density"]
            except Exception:
                cc["atoms_in_density_fraction"] = float("nan")

            r = DockResult(
                chain_id=chain.chain_id, chain_name=chain.name, model_path=str(model_path),
                provider=provider, hand=hand, search=f"powerfit_rank{rank}",
                dock_model=str(polished), score_map=str(real_map.resolve()),
                dock_map=str(real_map.resolve()), cc=cc, passed=False,
            )
            log(f"chain {chain.chain_id} hand {hand} powerfit rank {rank}: "
                f"{score_key} {cc_rough.get(score_key)} -> {cc.get(score_key)} after local fitmap")
            if best_here is None or float(r.cc.get(score_key, float("-inf"))) > float(best_here.cc.get(score_key, float("-inf"))):
                best_here = r
        if best_here is not None:
            per_hand[hand] = best_here

    if not per_hand:
        return None

    ranked = sorted(per_hand.values(), key=lambda r: float(r.cc.get(score_key, float("-inf"))), reverse=True)
    winner = ranked[0]

    # Trim residues the map cannot support, then re-score: the gate is a statement about the part
    # of the model the density actually covers. A predicted model routinely extends past what the
    # reconstruction resolves (a flexible domain, a tail), and those residues drag the masked CC
    # down while contributing nothing. Margin is generous here (default 10 residues) because this
    # pose has not been refined yet.
    if trim_extend is not None:
        try:
            win_map = Path(winner.score_map)
            trimmed = Path(winner.dock_model).with_name(Path(winner.dock_model).stem + "_trimmed.pdb")
            tstat = trim_model_to_density(Path(winner.dock_model), win_map, trimmed,
                                          extend_residues=trim_extend)
            cc_t, _ = phenix_map_correlations(
                env=phenix_env, model=trimmed, score_map=win_map,
                out_dir=trimmed.parent / "cc_after_trim", resolution=resolution)
            winner.cc["cc_before_density_trim"] = winner.cc.get(score_key)
            winner.cc["density_trim"] = tstat
            winner.cc[score_key] = cc_t.get(score_key)
            winner.cc["gate_score_value"] = float(cc_t.get(score_key, float("nan")))
            winner.dock_model = str(trimmed)
            log(f"chain {chain.chain_id}: density trim (extend {trim_extend} res) kept "
                f"{tstat['kept_total']}, dropped {tstat['dropped_total']}; "
                f"{score_key} {winner.cc['cc_before_density_trim']} -> {cc_t.get(score_key)}")
        except Exception as e:
            log(f"WARNING: chain {chain.chain_id}: density trim failed ({e}); keeping untrimmed pose")

    win_cc = float(winner.cc.get(score_key, float("nan")))
    other_cc = float(ranked[1].cc.get(score_key, float("nan"))) if len(ranked) > 1 else float("nan")
    ratio = (win_cc / other_cc) if (math.isfinite(other_cc) and other_cc > 0) else float("inf")
    winner.cc["opposite_hand_score"] = other_cc if math.isfinite(other_cc) else None
    winner.cc["hand_ratio"] = ratio if math.isfinite(ratio) else None

    if win_cc >= cc_gate:
        verdict, winner.passed = "ACCEPT", True
    elif win_cc >= cc_review:
        verdict, winner.passed = "REVIEW", True
    else:
        verdict, winner.passed = "REJECT", False
    if len(dock_hands) > 1 and math.isfinite(ratio) and ratio < hand_ratio_min and verdict != "REJECT":
        verdict += f" (HAND AMBIGUOUS: winning/opposite {score_key} ratio {ratio:.1f} < {hand_ratio_min})"
    winner.cc["verdict"] = verdict
    log(f"chain {chain.chain_id}: {verdict} hand={winner.hand} {score_key}={win_cc:.4f} "
        f"(opposite hand {other_cc if math.isfinite(other_cc) else 'n/a'}, ratio "
        f"{'inf' if not math.isfinite(ratio) else format(ratio, '.1f')}); "
        f"gate accept>={cc_gate:.2f} review>={cc_review:.2f}")
    if verdict.startswith("REVIEW"):
        log(f"chain {chain.chain_id}: REVIEW means the chain is placed on the right component but "
            f"the model or the local density is imperfect -- READ the QC image before trusting the mask.")
    return winner


def select_best_hand(results: List[DockResult], score_key: str, cc_gate: float,
                     atoms_gate: Optional[float] = None) -> Tuple[Optional[DockResult], bool, str]:
    """Pick the best-fitting map hand among candidates docked under IDENTICAL conditions.

    A fit is SUCCESSFUL when the mean-subtracted map-model correlation inside the model's own
    mask (Phenix CC_mask, `--score-key`) reaches `--cc-gate`. That is a direct physical statement
    about the fit and needs no knowledge of what else the map contains. %atoms-in-density is still
    computed and reported as a diagnostic, but no longer gates: it depends on an absolute contour
    level, which is exactly the quantity this pipeline could not estimate reliably.

    Among hands clearing the gate the higher-CC hand wins; if NO hand clears it, the best is
    returned flagged NOT CONFIDENT rather than silently accepted.
    """
    if not results:
        return None, False, "no candidates"
    ok = [
        r for r in results
        if math.isfinite(float(r.cc.get(score_key, float("nan"))))
        and float(r.cc.get(score_key, float("-inf"))) >= cc_gate
    ]
    pool = ok if ok else results
    chosen = max(pool, key=lambda r: float(r.cc.get(score_key, float("-inf"))))
    atoms = chosen.cc.get("atoms_in_density_fraction")
    atoms_txt = f"{atoms:.3f}" if isinstance(atoms, float) and math.isfinite(atoms) else str(atoms)
    if ok:
        reason = (f"{len(ok)}/{len(results)} hand(s) reached {score_key}>={cc_gate:.2f}; "
                  f"chose hand={chosen.hand} by {score_key}={chosen.cc.get(score_key)} "
                  f"(atoms_in_density={atoms_txt}, diagnostic only)")
    else:
        reason = (f"NO hand reached {score_key}>={cc_gate:.2f} "
                  f"(best={chosen.hand} {score_key}={chosen.cc.get(score_key)}, "
                  f"atoms_in_density={atoms_txt}); NOT CONFIDENT")
    return chosen, bool(ok), reason


def dock_chain_model(
    *,
    chain: ChainSeq,
    model_info: Dict[str, Any],
    maps: Dict[str, Any],
    out_dir: Path,
    search_levels: List[int],
    cc_gate: float,
    score_key: str,
    resolution: float,
    chimerax_exe: str,
    phenix_env: Dict[str, str],
    dock_hands: List[str],
    atoms_gate: float = 0.60,
    accept_mode: str = "multi_metric",
) -> Optional[DockResult]:
    """Rigid-dock one chain using the validated processed+autobox+centered protocol.

    The dock maps in `maps` are expected to be the processed docking-only maps
    (p90/cap99.7/normalized by default) and, unless explicitly disabled, the
    autoboxed versions preserving the MRC origin.  Each search recenters the
    starting model to that docking map's physical center before ChimeraX random
    search.  Independent Phenix scoring is always against the corresponding
    real full map hand, never the artificial docking map.

    Acceptance (skill gate 3): all hands requested for a given search level are docked under
    IDENTICAL conditions and compared TOGETHER via `select_best_hand` (--accept-mode
    multi_metric, the default) -- %atoms-in-density plus mean-subtracted Phenix CC, not either
    hand's CC judged against an absolute threshold alone. --accept-mode cc_only reproduces the
    pre-port behavior (each hand judged independently against --cc-gate) for legacy callers.
    """
    model_path = Path(model_info["model_path"]).resolve()
    provider = str(model_info.get("provider", "unknown"))
    best: Optional[DockResult] = None
    hand_specs = {
        "original": ("dock_original", "real_original"),
        "flip": ("dock_flip", "real_flip"),
    }

    for search in search_levels:
        level_dir = out_dir / f"search_{search}"
        level_results: List[DockResult] = []
        for hand in dock_hands:
            if hand not in hand_specs:
                raise PipelineError(f"Unknown dock hand {hand!r}; expected original and/or flip")
            dock_key, score_key_map = hand_specs[hand]
            fit_dir = level_dir / hand / "dock"
            try:
                docked, _ = chimerax_fitmap(
                    chimerax_exe=chimerax_exe,
                    model=model_path,
                    dock_map=Path(maps[dock_key]),
                    out_dir=fit_dir,
                    search=search,
                    resolution=resolution,
                    n_residues=None,
                )
                assert_model_chirality_ok(docked, f"chain {chain.chain_id} hand={hand} search={search} rigid dock")
                real_score_map = Path(maps[score_key_map])
                cc, _ = phenix_map_correlations(
                    env=phenix_env,
                    model=docked,
                    score_map=real_score_map,
                    out_dir=level_dir / hand / "cc_vs_real",
                    resolution=resolution,
                )
                try:
                    level_real = contour_level_for_chain(real_score_map)
                    atoms_report = fraction_atoms_in_density(docked, real_score_map, level_real)
                    cc["atoms_in_density_fraction"] = atoms_report["fraction_in_density"]
                    cc["atoms_in_density_report"] = atoms_report
                except Exception as ae:
                    cc["atoms_in_density_fraction"] = float("nan")
                    cc["atoms_in_density_error"] = str(ae)
            except Exception as e:
                (level_dir / hand).mkdir(parents=True, exist_ok=True)
                (level_dir / hand / "ERROR.txt").write_text(str(e) + "\n")
                continue

            value = float(cc.get(score_key, float("nan")))
            cc["gate_score_key"] = score_key
            cc["gate_score_value"] = value
            result = DockResult(
                chain_id=chain.chain_id,
                chain_name=chain.name,
                model_path=str(model_path),
                provider=provider,
                hand=hand,
                search=search,
                dock_model=str(docked),
                score_map=str(real_score_map.resolve()),
                dock_map=str(Path(maps[dock_key]).resolve()),
                cc=cc,
                passed=False,
            )
            level_results.append(result)
            log(f"chain {chain.chain_id} provider {provider} hand {hand} search {search}: "
                f"{score_key}={value:.4f} atoms_in_density={cc.get('atoms_in_density_fraction')}")

        if not level_results:
            continue

        if accept_mode == "cc_only":
            for r in level_results:
                v = float(r.cc.get(score_key, float("nan")))
                r.passed = math.isfinite(v) and v >= cc_gate
            level_best = max(level_results, key=lambda r: float(r.cc.get(score_key, float("-inf"))))
        else:
            level_best, multi_passed, reason = select_best_hand(level_results, score_key, cc_gate, atoms_gate)
            if level_best is not None:
                level_best.passed = multi_passed
                level_best.cc["multi_metric_reason"] = reason
                log(f"chain {chain.chain_id} search={search}: {reason}")

        if level_best is not None and (
            best is None or float(level_best.cc.get(score_key, float("-inf"))) > float(best.cc.get(score_key, float("-inf")))
        ):
            best = level_best

        if level_best is not None and level_best.passed:
            log(f"chain {chain.chain_id}: passed at search={search} (accept_mode={accept_mode})")
            return level_best

    return best


def dock_chain_two_stage(
    *,
    chain: ChainSeq,
    model_info: Dict[str, Any],
    whole_maps: Dict[str, Any],
    lobe_maps: Dict[str, Any],
    out_dir: Path,
    cc_gate: float,
    cc_review: float,
    score_key: str,
    resolution: float,
    chimerax_exe: str,
    phenix_env: Dict[str, str],
    dock_hands: List[str],
    local_max_steps: int,
    powerfit_exe: str,
    powerfit_angle: float,
    powerfit_solutions: int,
    powerfit_procs: int,
    hand_ratio_min: float,
    keep_hetero: bool = False,
) -> Optional[DockResult]:
    """Skill gates 2 and 4: candidate identification in a target-lobe CUT, then a LOCAL refit
    in the WHOLE map. A blind whole-map global hit is a candidate, not proof; in a complex, the
    target-lobe cut (independent of the test model -- from segmentation/architecture) is used
    for hand-testing and component identification. Only after that is the pose trusted enough
    to locally re-optimize directly against the whole (accepted-hand) real map.

    FIXED 2026-08-19 (cb1_redo evidence): stage 1 now runs `dock_chain_powerfit()` -- PowerFit's
    FFT 6D global search + local ChimeraX polish -- against the LOBE maps, not the old
    `dock_chain_model()` ChimeraX random-restart ladder. The ladder was silently unconditional
    here regardless of `--dock-method`, and measured at the mirror-control noise floor on this
    project's receptor lobe (CC_mask 0.03-0.06) while PowerFit-on-the-same-lobe scored 0.5725
    (hand_ratio 7.26, ACCEPT) -- the search algorithm, not the model or the map, was the limiting
    factor. Stage 1's own local-fitmap polish (against the lobe) and any density trim are
    intentionally NOT the final answer -- it is a CANDIDATE only, never returned as final;
    stage 2 (unchanged) re-optimizes and rescores it against the WHOLE map, and the standard
    post-refine density trim (main(), after flexible refinement) is what actually trims the
    model. `trim_extend=None` is passed to stage 1's PowerFit call for exactly this reason: do
    not truncate the candidate to what the LOBE crop happens to cover before stage 2 has even
    seen the whole map.
    """
    stage1_dir = out_dir / "stage1_lobe_candidate"
    stage1 = dock_chain_powerfit(
        chain=chain, model_info=model_info, maps=lobe_maps, out_dir=stage1_dir,
        cc_gate=cc_gate, cc_review=cc_review, score_key=score_key, resolution=resolution,
        chimerax_exe=chimerax_exe, phenix_env=phenix_env, dock_hands=dock_hands,
        powerfit_exe=powerfit_exe, powerfit_angle=powerfit_angle,
        powerfit_solutions=powerfit_solutions, powerfit_procs=powerfit_procs,
        local_max_steps=local_max_steps, hand_ratio_min=hand_ratio_min, keep_hetero=keep_hetero,
        trim_extend=None,
    )
    if stage1 is None:
        return None
    log(f"chain {chain.chain_id}: stage-1 TARGET-LOBE candidate (PowerFit) hand={stage1.hand} "
        f"{score_key}={stage1.cc.get(score_key)} atoms={stage1.cc.get('atoms_in_density_fraction')} "
        f"passed={stage1.passed} (candidate only -- now verifying with a LOCAL fit in the WHOLE map)")

    whole_key = "real_original" if stage1.hand == "original" else "real_flip"
    whole_map = Path(whole_maps[whole_key])
    stage2_dir = out_dir / "stage2_local_whole_map"
    local_model, _ = chimerax_local_refit(
        chimerax_exe=chimerax_exe, model=Path(stage1.dock_model), whole_map=whole_map,
        out_dir=stage2_dir, resolution=resolution, max_steps=local_max_steps, n_residues=None,
    )
    assert_model_chirality_ok(local_model, f"chain {chain.chain_id} stage-2 local whole-map refit")
    cc_whole, _ = phenix_map_correlations(
        env=phenix_env, model=local_model, score_map=whole_map,
        out_dir=stage2_dir / "cc_vs_whole_real_map", resolution=resolution,
    )
    level_whole = contour_level_for_chain(whole_map)
    atoms_whole = fraction_atoms_in_density(local_model, whole_map, level_whole)
    cc_whole["atoms_in_density_fraction"] = atoms_whole["fraction_in_density"]
    cc_whole["atoms_in_density_report"] = atoms_whole
    cc_whole["gate_score_key"] = score_key
    cc_whole["gate_score_value"] = float(cc_whole.get(score_key, float("nan")))
    cc_whole["stage1_target_lobe_cc"] = stage1.cc

    # Acceptance is the masked-area correlation in both modes now (atoms-in-density is reported
    # as a diagnostic but never gates -- it needs an absolute contour level).
    v = cc_whole["gate_score_value"]
    passed = math.isfinite(v) and v >= cc_gate

    result = DockResult(
        chain_id=chain.chain_id, chain_name=chain.name, model_path=stage1.model_path,
        provider=stage1.provider, hand=stage1.hand, search=stage1.search,
        dock_model=str(local_model), score_map=str(whole_map.resolve()), dock_map=stage1.dock_map,
        cc=cc_whole, passed=passed,
    )
    log(f"chain {chain.chain_id}: stage-2 LOCAL whole-map fit hand={result.hand} "
        f"{score_key}={cc_whole['gate_score_value']:.4f} atoms={atoms_whole['fraction_in_density']:.3f} passed={passed}")
    return result


# ----------------------------- model transforms/combine/refine/mask -----------------------------
#
# NOTE: there used to be a `mirror_model_coordinates()` helper here that mapped a fitted model
# from flipped-map coordinates back onto the ORIGINAL grid. It has been REMOVED (not merely
# deprecated/gated) per the HANDEDNESS RULE in the module docstring: mirroring an atomic model
# is never a valid operation on a real (chirally-fixed) protein. If the flipped hand wins, the
# chemically valid model is the one fit directly in the flipped-map frame, and that map file is
# the correct downstream reference. A MASK is not a chiral biological object -- it is a shape --
# so, unlike the model, a mask MAY be mirrored to match whatever frame a caller will actually
# refine in; see `--emit-mask-frame` for the (backward-compatible) mask-only mirror-back-to-
# original-grid capability.

def check_model_chirality_L(model_path: Path) -> Dict[str, Any]:
    """Sanity guard: confirm a model's amino-acid backbone chirality is the canonical L-form.

    This tool NEVER mirrors an atomic model (see module docstring HANDEDNESS RULE) -- the only
    handedness operation performed anywhere in this pipeline is on the MAP. This check is a
    cheap regression guard, run after every rigid-body transform that must be chirality-
    preserving (translation/rotation only), so a future bug that accidentally introduces a
    coordinate mirror is caught immediately instead of silently producing an invalid protein.

    Method: for each residue with a CB atom (excludes GLY, and silently no-ops on CA-only /
    backbone-only models), compute the signed volume of the tetrahedron (N, CA, C, CB). A
    canonical L-amino acid gives a consistent sign; if a large fraction of residues have the
    opposite sign, the model has been mirrored.
    """
    st = gemmi.read_structure(str(model_path))
    n_checked = 0
    n_l = 0
    for model in st:
        for chain in model:
            for res in chain:
                atoms = {a.name: a.pos for a in res}
                if not {"N", "CA", "C", "CB"} <= set(atoms):
                    continue
                n = np.array([atoms["N"].x, atoms["N"].y, atoms["N"].z])
                ca = np.array([atoms["CA"].x, atoms["CA"].y, atoms["CA"].z])
                c = np.array([atoms["C"].x, atoms["C"].y, atoms["C"].z])
                cb = np.array([atoms["CB"].x, atoms["CB"].y, atoms["CB"].z])
                v1, v2, v3 = n - ca, c - ca, cb - ca
                signed_vol = float(np.dot(v1, np.cross(v2, v3)))
                n_checked += 1
                if signed_vol > 0:  # canonical L sign under this (N,CA,C,CB) ordering
                    # NOTE (test_mask patch 2026-07-29): the upstream tool used `< 0` here, which
                    # is inverted. Verified on experimental 5IRX (1614/1614 CB residues > 0),
                    # AlphaFold AF-O35433 (791/791 > 0) and SWISS-MODEL Q91MA7 (1371/1371 > 0):
                    # a canonical L-amino acid gives a POSITIVE signed volume for
                    # (N-CA) . [(C-CA) x (CB-CA)]. With `< 0` every valid L-form model scores
                    # fraction_l_form = 0.00 and assert_model_chirality_ok() aborts every dock.
                    n_l += 1
    frac_l = (n_l / n_checked) if n_checked else float("nan")
    return {"n_checked": n_checked, "n_l_form": n_l, "fraction_l_form": frac_l}


def assert_model_chirality_ok(model_path: Path, context: str, min_fraction_l: float = 0.85) -> Dict[str, Any]:
    result = check_model_chirality_L(model_path)
    frac = result.get("fraction_l_form", float("nan"))
    if result["n_checked"] >= 5 and math.isfinite(frac):
        if frac < (1.0 - min_fraction_l):
            raise PipelineError(
                f"CHIRALITY GUARD FAILED for {context} ({model_path}): fraction_l_form={frac:.2f} "
                f"looks MIRRORED (D-amino-acid pattern). This tool never mirrors atomic models; "
                f"this indicates a bug in this pipeline or a pre-mirrored input model."
            )
        if min_fraction_l > frac >= (1.0 - min_fraction_l):
            log(f"WARNING: ambiguous chirality check for {context} ({model_path}): "
                f"fraction_l_form={frac:.2f} (n_checked={result['n_checked']})")
    return result


CHAIN_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def rename_model_chain(input_model: Path, output_model: Path, chain_id: str,
                       reserved: Optional[Iterable[str]] = None) -> Path:
    """Give the model's FIRST chain this FASTA record's ID; every further chain a DISTINCT one.

    Previously every chain was renamed to the same ID, and gemmi then MERGES same-named chains on
    write: an oligomeric provider model was silently truncated to its first chain with no warning
    (SWISS-MODEL HA trimer 1488 residues -> 496, two protomers gone). SWISS-MODEL routinely returns
    oligomers and `rcsb_template` always downloads whole entries, so this was a silent data loss on
    a common input.

    Keeping the extra chains is also what we want scientifically: they are the neighbouring subunits,
    so flexible refinement sees them as context (same reason --map-symmetry fills the map), while the
    mask still comes from --mask-chains only.
    """
    st = gemmi.read_structure(str(input_model))
    st.setup_entities()
    used = {chain_id} | {str(c) for c in (reserved or [])}
    assigned: List[Tuple[str, str]] = []
    for model in st:
        for i, chain in enumerate(model):
            if i == 0:
                new_name = chain_id
            else:
                new_name = next((c for c in CHAIN_ID_ALPHABET if c not in used), None)
                if new_name is None:
                    raise PipelineError(
                        f"Ran out of chain IDs renaming {input_model} (model has {len(model)} chains)")
            assigned.append((chain.name, new_name))
            used.add(new_name)
            chain.name = new_name
    if len(assigned) > 1:
        log(f"{input_model.name}: multi-chain model kept in full, chains renamed "
            + ", ".join(f"{a}->{b}" for a, b in assigned)
            + f" (only '{chain_id}' is this FASTA record's chain; the rest are context)")
    output_model.parent.mkdir(parents=True, exist_ok=True)
    st.write_pdb(str(output_model))
    return output_model.resolve()


def prepare_selected_model_for_refine_hand(result: DockResult, out_dir: Path,
                                          reserved: Optional[Iterable[str]] = None) -> DockResult:
    """Copy/rename the selected docked model without mirroring atomic coordinates.

    Important: if the winning hand is a flipped map, the chemically valid atomic model is
    the model fitted/refined in that flipped-map coordinate frame. Mirroring the atomic
    model back to the original map would invert protein chirality and is not suitable for
    Phenix refinement. Only masks/volumes may optionally be flipped back to the original grid
    (see --emit-mask-frame); the model itself is NEVER mirrored, in either direction.
    """
    src = Path(result.dock_model)
    renamed = out_dir / f"chain_{result.chain_id}_{result.hand}_hand_prepared.pdb"
    rename_model_chain(src, renamed, result.chain_id, reserved=reserved)
    result.model_original_hand = str(renamed.resolve())
    return result


STANDARD_PROTEIN_RESNAMES = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
    "MSE", "SEC", "PYL",
}


# ============================================================================================
# test_mask 2026-07-29: fill the map by point-group symmetry BEFORE flexible refinement.
#
# Refining ONE chain inside a map that holds the whole complex leaves every neighbouring subunit's
# density unoccupied, so the chain is free to drift into it and MolProbity (which only sees the
# isolated chain) cannot detect the resulting inter-chain clashes. When the map was reconstructed
# with symmetry imposed, the neighbours are exact copies of what is already docked -- so generate
# them, refine the filled assembly, and build the mask from the requested chain(s) only.
#
# CryoSPARC convention: the point-group axis is +Z through the box centre.
# ============================================================================================

SYM_RE = re.compile(r"^([CD])(\d+)$", re.I)


def symmetry_operators(symmetry: str) -> List[np.ndarray]:
    """Rotation matrices for Cn / Dn about +Z (identity first). Dn adds a 2-fold about +X."""
    sym = (symmetry or "C1").strip().upper()
    if sym in ("", "C1"):
        return [np.eye(3)]
    m = SYM_RE.match(sym)
    if not m:
        raise PipelineError(f"Unsupported --map-symmetry {symmetry!r}; expected C1/Cn/Dn")
    kind, n = m.group(1).upper(), int(m.group(2))
    if n < 1:
        raise PipelineError(f"Bad symmetry order in {symmetry!r}")
    ops = []
    for i in range(n):
        a = 2.0 * math.pi * i / n
        ca, sa = math.cos(a), math.sin(a)
        ops.append(np.array([[ca, -sa, 0.0], [sa, ca, 0.0], [0.0, 0.0, 1.0]]))
    if kind == "D":
        flip = np.diag([1.0, -1.0, -1.0])          # 2-fold about X
        ops = ops + [o @ flip for o in ops]
    return ops


def map_box_centre_A(map_path: Path) -> np.ndarray:
    with mrcfile.open(str(map_path), permissive=True) as m:
        shape = np.array(m.data.shape[::-1], dtype=float)          # (x, y, z)
        apix = np.array([float(m.voxel_size.x), float(m.voxel_size.y), float(m.voxel_size.z)])
        org = np.array([float(m.header.origin.x), float(m.header.origin.y), float(m.header.origin.z)])
        nstart = np.array([int(m.header.nxstart), int(m.header.nystart), int(m.header.nzstart)])
    return org + nstart * apix + 0.5 * shape * apix


def fill_map_by_symmetry(model_path: Path, map_path: Path, symmetry: str, output_model: Path) -> Dict[str, Any]:
    """Add point-group copies of `model_path` so the map's other subunits are occupied.

    Returns {output, target_chains, added_chains, n_copies}. The ORIGINAL chain IDs are preserved,
    so --mask-chains still selects exactly the subunit that was docked; copies get fresh IDs.
    """
    ops = symmetry_operators(symmetry)
    st = gemmi.read_structure(str(model_path))
    st.setup_entities()
    target_chains = [c.name for c in st[0]]
    if len(ops) <= 1:
        return {"output": str(model_path.resolve()), "target_chains": target_chains,
                "added_chains": [], "n_copies": 1, "symmetry": "C1"}
    centre = map_box_centre_A(map_path)

    # Does the model ALREADY span this symmetry? A template-based model is often the complete
    # oligomer (SWISS-MODEL 5IRX is the whole TRPV1 tetramer), and generating Cn copies of a model
    # that already fills the map stacks n assemblies on top of each other -- Phenix then aborts with
    # `pdb_interpretation.clash_guard failure`. Detect it by rotating the CA positions and checking
    # whether they land on atoms that already exist.
    st_probe = gemmi.read_structure(str(model_path))
    st_probe.setup_entities()
    ca = np.array([[a.pos.x, a.pos.y, a.pos.z] for ch in st_probe[0] for r in ch
                   for a in r if a.name == "CA"])
    if len(ca) >= 10:
        from scipy.spatial import cKDTree
        tree = cKDTree(ca)
        worst = 0.0
        for op in ops[1:]:
            moved = (op @ (ca - centre).T).T + centre
            hit = tree.query_ball_point(moved, r=3.0)
            worst = max(worst, float(np.mean([1.0 if h else 0.0 for h in hit])))
        if worst > 0.30:
            log(f"map symmetry {symmetry}: model ALREADY spans this symmetry "
                f"({100*worst:.0f}% of rotated CA land on existing atoms) -- NOT adding copies. "
                f"Filling here would stack {len(ops)} assemblies on top of each other and Phenix "
                f"would abort with a clash_guard failure.")
            return {"output": str(model_path.resolve()), "target_chains": target_chains,
                    "added_chains": [], "n_copies": 1, "symmetry": symmetry,
                    "skipped_reason": f"model already symmetric (self-overlap {worst:.2f})"}

    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    used = set(target_chains)
    out_st = gemmi.Structure()
    out_st.cell = gemmi.UnitCell(1, 1, 1, 90, 90, 90)
    out_model = gemmi.Model("1")
    added: List[str] = []
    for op_i, op in enumerate(ops):
        for ch in st[0]:
            if op_i == 0:
                name = ch.name
            else:
                name = next((c for c in alphabet if c not in used), None)
                if name is None:
                    raise PipelineError("Ran out of chain IDs while filling the map by symmetry")
                added.append(name)
            used.add(name)
            new_ch = gemmi.Chain(name)
            for res in ch:
                r = res.clone()
                for at in r:
                    v = np.array([at.pos.x, at.pos.y, at.pos.z]) - centre
                    w = op @ v + centre
                    at.pos = gemmi.Position(float(w[0]), float(w[1]), float(w[2]))
                new_ch.add_residue(r)
            out_model.add_chain(new_ch)
    out_st.add_model(out_model)
    output_model.parent.mkdir(parents=True, exist_ok=True)
    out_st.write_pdb(str(output_model))
    return {"output": str(output_model.resolve()), "target_chains": target_chains,
            "added_chains": added, "n_copies": len(ops), "symmetry": symmetry,
            "rotation_centre_A": [float(x) for x in centre]}


def measure_ca_drift(before: Path, after: Path) -> Dict[str, Any]:
    """CA shift between the rigid-docked pose and the refined model, matched by chain+residue."""
    def cas(path: Path) -> Dict[Tuple[str, int], np.ndarray]:
        st = gemmi.read_structure(str(path))
        st.setup_entities()
        out: Dict[Tuple[str, int], np.ndarray] = {}
        for ch in st[0]:
            for r in ch:
                a = r.find_atom("CA", "*")
                if a is not None:
                    out[(ch.name, r.seqid.num)] = np.array([a.pos.x, a.pos.y, a.pos.z])
        return out
    A, B = cas(before), cas(after)
    keys = sorted(set(A) & set(B))
    if not keys:
        return {"n_ca": 0}
    d = np.array([float(np.linalg.norm(A[k] - B[k])) for k in keys])
    return {"n_ca": len(keys), "rmsd_A": float(np.sqrt((d ** 2).mean())),
            "max_shift_A": float(d.max()), "frac_over_2A": float((d > 2.0).mean())}


def combine_models(model_paths: List[Path], output_model: Path, *, protein_only: bool = True) -> Path:
    if not model_paths:
        raise PipelineError("No models to combine")
    out_st = gemmi.Structure()
    out_st.cell = gemmi.UnitCell(1, 1, 1, 90, 90, 90)
    out_model = gemmi.Model("1")
    seen: set[str] = set()
    for mp in model_paths:
        st = gemmi.read_structure(str(mp))
        if len(st) == 0:
            continue
        for chain in st[0]:
            ch = gemmi.Chain(chain.name)
            for res in chain:
                if protein_only and res.name.strip().upper() not in STANDARD_PROTEIN_RESNAMES:
                    continue
                ch.add_residue(res.clone())
            if len(ch) == 0:
                continue
            if ch.name in seen:
                # Pick another compatible one-character chain ID.
                for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789":
                    if c not in seen:
                        ch.name = c
                        break
            seen.add(ch.name)
            out_model.add_chain(ch)
    if len(out_model) == 0:
        raise PipelineError("No residues remained after combining models; check input models or --keep-hetero")
    out_st.add_model(out_model)
    output_model.parent.mkdir(parents=True, exist_ok=True)
    out_st.write_pdb(str(output_model))
    return output_model.resolve()


def phenix_real_space_refine(
    *,
    env: Dict[str, str],
    model: Path,
    map_path: Path,
    out_dir: Path,
    resolution: float,
    extra: str,
) -> Tuple[Path, Path]:
    """Legacy single-call refine (pre-port behavior). Kept for --refine-mode legacy."""
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = out_dir / "combined_refined"
    cmd = [
        "phenix.real_space_refine",
        str(model),
        str(map_path),
        f"resolution={resolution}",
        "refinement.run=minimization_global+local_grid_search+adp",
        "pdb_interpretation.secondary_structure.protein.enabled=True",
        "reference_model.enabled=True",
        "reference_model.use_starting_model_as_reference=True",
        f"output.file_name_prefix={prefix}",
    ]
    if extra:
        cmd.extend(shlex.split(extra))
    log_path = out_dir / "phenix_real_space_refine.log"
    run_cmd(cmd, cwd=out_dir, env=env, log_path=log_path, allow_fail=False)
    candidates = sorted(out_dir.glob("combined_refined*real_space_refined*.pdb")) + sorted(out_dir.glob("combined_refined*real_space_refined*.cif"))
    if not candidates:
        candidates = sorted(out_dir.glob("combined_refined*.pdb")) + sorted(out_dir.glob("combined_refined*.cif"))
    if not candidates:
        raise PipelineError(f"Phenix refinement finished but no refined model found in {out_dir}")
    return candidates[-1].resolve(), log_path.resolve()


def phenix_secondary_structure_restraints(env: Dict[str, str], model: Path, out_dir: Path) -> Optional[Path]:
    """Generate SS restraints (ss.eff) from the model.

    Ports the historical `helix_type` toggle fix from scripts/run_phenix_two_stage.sh: some
    phenix.secondary_structure_restraints versions default `helix_type` PHIL to `*unknown`
    instead of `*alpha`, which silently drops alpha-helix restraints; the sed-equivalent below
    flips the default star back onto `alpha`.  Returns None (never raises) if the executable is
    unavailable, so callers can gracefully fall back to --refine-mode legacy.
    """
    exe = shutil.which("phenix.secondary_structure_restraints", path=env.get("PATH"))
    if not exe:
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "ss_restraints.log"
    cmd = [exe, str(model), "format=phenix"]
    proc = subprocess.run(cmd, cwd=str(out_dir), env=env, text=True, capture_output=True)
    log_path.write_text((proc.stdout or "") + ("\nSTDERR:\n" + proc.stderr if proc.stderr else ""))
    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return None
    fixed_text = (proc.stdout or "").replace(
        "helix_type = alpha pi 3_10 *unknown", "helix_type = *alpha pi 3_10 unknown"
    )
    ss_eff = out_dir / "ss.eff"
    ss_eff.write_text(fixed_text)
    return ss_eff


GEOMETRY_DEFAULT_GATES: Dict[str, float] = {
    "clashscore_max": 10.0,
    "rama_favored_min": 96.0,
    "rama_outliers_max": 0.5,
    "rotamer_outliers_max": 2.0,
}


def parse_molprobity_text(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    m = re.search(r"clashscore[^0-9\-]*([-+]?\d*\.?\d+)", text, re.I)
    if m:
        out["clashscore"] = float(m.group(1))
    m = re.search(r"favored[^0-9\-]*([-+]?\d*\.?\d+)\s*%", text, re.I)
    if m:
        out["rama_favored_pct"] = float(m.group(1))
    m = re.search(r"rama\w*\s*outliers[^0-9\-]*([-+]?\d*\.?\d+)\s*%", text, re.I)
    if m:
        out["rama_outliers_pct"] = float(m.group(1))
    m = re.search(r"rotamer\s*outliers[^0-9\-]*([-+]?\d*\.?\d+)\s*%", text, re.I)
    if m:
        out["rotamer_outliers_pct"] = float(m.group(1))
    return out


def phenix_molprobity(env: Dict[str, str], model: Path, out_dir: Path) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = out_dir / "molprobity.log"
    exe = shutil.which("phenix.molprobity", path=env.get("PATH"))
    if not exe:
        return {"error": "phenix.molprobity not found"}
    cmd = [exe, str(model)]
    proc = subprocess.run(cmd, cwd=str(out_dir), env=env, text=True, capture_output=True)
    text = (proc.stdout or "") + ("\nSTDERR:\n" + proc.stderr if proc.stderr else "")
    log_path.write_text(text)
    geom = parse_molprobity_text(text)
    geom["returncode"] = proc.returncode
    geom["log_path"] = str(log_path.resolve())
    return geom


def geometry_passes(geom: Dict[str, Any], gates: Dict[str, float]) -> Tuple[bool, str]:
    problems = []
    cs = geom.get("clashscore")
    if cs is None or not math.isfinite(cs) or cs > gates["clashscore_max"]:
        problems.append(f"clashscore={cs} > {gates['clashscore_max']}")
    fav = geom.get("rama_favored_pct")
    if fav is None or not math.isfinite(fav) or fav < gates["rama_favored_min"]:
        problems.append(f"rama_favored={fav}% < {gates['rama_favored_min']}%")
    rout = geom.get("rama_outliers_pct")
    if rout is None or not math.isfinite(rout) or rout > gates["rama_outliers_max"]:
        problems.append(f"rama_outliers={rout}% > {gates['rama_outliers_max']}%")
    rot = geom.get("rotamer_outliers_pct")
    if rot is None or not math.isfinite(rot) or rot > gates["rotamer_outliers_max"]:
        problems.append(f"rotamer_outliers={rot}% > {gates['rotamer_outliers_max']}%")
    return (len(problems) == 0), "; ".join(problems)


def phenix_two_stage_refine(
    *,
    env: Dict[str, str],
    model: Path,
    map_path: Path,
    out_dir: Path,
    resolution: float,
    cleanup_cycles_to_test: Sequence[int] = (1, 2, 3),
    fit_score_key: str = "CC_mask",
    fit_floor: Optional[float] = None,
    geometry_gates: Optional[Dict[str, float]] = None,
    r1_extra: str = "",
    r2_extra: str = "",
) -> Dict[str, Any]:
    """Skill gate 6: SS-restrained density search (R1) -> gentle cleanup (R2), tested at
    1/2/3 cleanup macro_cycles and select the FIRST endpoint that passes BOTH a fit floor and
    geometry gates (clashscore<10, Rama favored>96%, Rama outliers<0.5%, rotamer outliers<2%
    by default). Ported from scripts/run_phenix_two_stage.sh + the hTA1 worked example, which
    rejected a 3rd cleanup cycle for raising CC while breaking geometry.

    Raises PipelineError if phenix.secondary_structure_restraints is unavailable/fails; the
    caller (main()) catches this and falls back to --refine-mode legacy automatically.
    """
    geometry_gates = geometry_gates or GEOMETRY_DEFAULT_GATES
    out_dir.mkdir(parents=True, exist_ok=True)
    ss_dir = out_dir / "00_ss_restraints"
    ss_eff = phenix_secondary_structure_restraints(env, model, ss_dir)
    if ss_eff is None:
        raise PipelineError(
            "phenix.secondary_structure_restraints unavailable/failed; "
            "caller should fall back to --refine-mode legacy"
        )

    r1_dir = out_dir / "r1_search"
    r1_dir.mkdir(parents=True, exist_ok=True)
    r1_prefix = r1_dir / "r1"
    cmd = [
        "phenix.real_space_refine", str(model), str(map_path), f"resolution={resolution}",
        "scattering_table=electron", "refinement.macro_cycles=3",
        "refinement.run=minimization_global+local_grid_search",
        "pdb_interpretation.secondary_structure.enabled=True", str(ss_eff),
        f"output.file_name_prefix={r1_prefix}",
    ]
    if r1_extra:
        cmd.extend(shlex.split(r1_extra))
    run_cmd(cmd, cwd=r1_dir, env=env, log_path=r1_dir / "phenix_r1.log", allow_fail=False)
    r1_candidates = sorted(r1_dir.glob("r1*real_space_refined*.pdb")) + sorted(r1_dir.glob("r1*real_space_refined*.cif"))
    if not r1_candidates:
        raise PipelineError(f"Phenix R1 restrained density search produced no refined model in {r1_dir}")
    r1_model = r1_candidates[-1].resolve()
    assert_model_chirality_ok(r1_model, "phenix R1 restrained density search")

    if fit_floor is None:
        cc0, _ = phenix_map_correlations(env=env, model=model, score_map=map_path, out_dir=out_dir / "cc_start", resolution=resolution)
        fit_floor = float(cc0.get(fit_score_key, 0.0))

    candidates: List[Dict[str, Any]] = []
    selected: Optional[Dict[str, Any]] = None
    for n in cleanup_cycles_to_test:
        r2_dir = out_dir / f"r2_cleanup_{n}"
        r2_dir.mkdir(parents=True, exist_ok=True)
        r2_prefix = r2_dir / f"r2_{n}"
        cmd = [
            "phenix.real_space_refine", str(r1_model), str(map_path), f"resolution={resolution}",
            "scattering_table=electron", f"refinement.macro_cycles={n}",
            "refinement.run=minimization_global",
            "pdb_interpretation.secondary_structure.enabled=True", str(ss_eff),
            f"output.file_name_prefix={r2_prefix}",
        ]
        if r2_extra:
            cmd.extend(shlex.split(r2_extra))
        run_cmd(cmd, cwd=r2_dir, env=env, log_path=r2_dir / f"phenix_r2_{n}.log", allow_fail=False)
        r2_candidates = sorted(r2_dir.glob(f"r2_{n}*real_space_refined*.pdb")) + sorted(r2_dir.glob(f"r2_{n}*real_space_refined*.cif"))
        if not r2_candidates:
            candidates.append({"cycle": n, "error": "no refined model produced"})
            continue
        r2_model = r2_candidates[-1].resolve()
        assert_model_chirality_ok(r2_model, f"phenix R2 cleanup cycle {n}")
        cc, _ = phenix_map_correlations(env=env, model=r2_model, score_map=map_path, out_dir=r2_dir / "cc", resolution=resolution)
        geom = phenix_molprobity(env, r2_model, r2_dir / "molprobity")
        geom_ok, geom_reason = geometry_passes(geom, geometry_gates)
        fit_val = float(cc.get(fit_score_key, float("nan")))
        fit_ok = math.isfinite(fit_val) and fit_val >= fit_floor
        cand = {
            "cycle": n, "model": str(r2_model), "cc": cc, "geometry": geom,
            "fit_ok": fit_ok, "geometry_ok": geom_ok, "geometry_problems": geom_reason,
            "passed": bool(fit_ok and geom_ok),
        }
        candidates.append(cand)
        log(f"phenix two-stage cleanup cycle={n}: {fit_score_key}={fit_val:.4f} (floor {fit_floor:.4f}) "
            f"geometry_ok={geom_ok} ({geom_reason or 'within gates'})")
        if cand["passed"] and selected is None:
            selected = cand
            break  # first acceptable endpoint, per skill gate 6

    if selected is None:
        # None of the tested cycles passed both gates. Never silently prefer a higher-CC/worse-
        # geometry cycle (gate 6) -- fall back to the best-available geometry, else R1 itself.
        ok_candidates = [c for c in candidates if "error" not in c]
        if ok_candidates:
            selected = min(ok_candidates, key=lambda c: (not c["geometry_ok"], c["geometry"].get("clashscore", 1e9)))
            log(f"WARNING: no cleanup cycle passed BOTH fit and geometry gates; falling back to "
                f"cycle={selected['cycle']} (best available geometry) -- inspect before trusting")
        else:
            selected = {"cycle": 0, "model": str(r1_model), "cc": None, "geometry": None,
                        "passed": False, "note": "all cleanup cycles failed to produce output; using R1 model"}
            log("WARNING: all cleanup cycles failed to produce output; falling back to the R1 restrained-search model")

    return {
        "ss_eff": str(ss_eff),
        "r1_model": str(r1_model),
        "r1_log": str((r1_dir / "phenix_r1.log").resolve()),
        "fit_floor": fit_floor,
        "geometry_gates": geometry_gates,
        "candidates": candidates,
        "selected": selected,
        "selected_model": selected.get("model", str(r1_model)),
    }


def choose_refinement_hand(successful: List[DockResult], score_key: str) -> str:
    """Choose one global map hand for combined refinement.

    Atomic models from original and flipped maps cannot be mixed in one refinement because
    mirroring atomic coordinates would invert molecular chirality. Therefore we keep the
    hand with the largest summed Phenix score among passed chains and refine only those
    chains in that same hand.
    """
    sums: Dict[str, float] = {}
    for r in successful:
        sums[r.hand] = sums.get(r.hand, 0.0) + float(r.cc.get(score_key, 0.0))
    if not sums:
        raise PipelineError("No successful chain results to choose refinement hand")
    return max(sums, key=sums.get)


def flip_mask_outputs_to_original_grid(mask_report: Dict[str, Any], out_dir: Path, axis: str) -> Dict[str, Any]:
    """Flip fit-hand MASK/volume outputs back onto the original input-map grid.

    A mask is a shape, not a chiral biological object, so mirroring it is valid -- unlike the
    atomic model (see the HANDEDNESS RULE in the module docstring), which is NEVER mirrored in
    either direction. This supports the fast local-refine workflow: dock the (never-flipped)
    chiral model into the CORRECT-HAND map, build the model-derived mask in that winning frame,
    then mirror JUST the mask back to the ORIGINAL input-map frame so a caller can locally
    refine their existing original-frame particles/volume directly with a frame-matched mask
    (and only mirror the final deliverable map at the very end, if ever). Controlled by
    `--emit-mask-frame` (default "both": keep the winning-hand mask AND emit this mirrored-back
    copy, so a caller can pick whichever frame matches what they are refining).
    """
    flipped: Dict[str, str] = {}
    for key in ["output_soft_mask", "output_hard_mask"]:
        p = mask_report.get(key)
        if p and Path(p).exists():
            src = Path(p)
            dst = out_dir / f"{src.stem}_original_grid{src.suffix}"
            mirror_map(src, dst, axis)
            flipped[key + "_original_grid"] = str(dst.resolve())
    mask_report["flipped_back_to_original_grid"] = flipped
    return mask_report


def parse_mask_chain_ranges(text: str, mask_chains: str, seq_from: Optional[int], seq_to: Optional[int]) -> Dict[str, Tuple[Optional[int], Optional[int]]]:
    """Parse FASTA-position mask ranges.

    Syntax: --mask-chain-ranges 'R:1-300,N:1-120'.  Positions are 1-based
    residue order within the selected chain/model sequence, not PDB residue numbers.
    If no chain-specific range is supplied, --mask-seq-from/--mask-seq-to applies to
    all --mask-chains.  Empty range means all residues.
    """
    ranges: Dict[str, Tuple[Optional[int], Optional[int]]] = {}
    if text.strip():
        for item in text.split(","):
            item = item.strip()
            if not item:
                continue
            if ":" not in item:
                raise PipelineError(f"Bad --mask-chain-ranges item {item!r}; expected chain:start-end")
            chain_id, rng = item.split(":", 1)
            chain_id = chain_id.strip()
            rng = rng.strip()
            if not chain_id:
                raise PipelineError(f"Bad --mask-chain-ranges item {item!r}; empty chain ID")
            if not rng or rng.lower() == "all":
                ranges[chain_id] = (None, None)
            elif "-" in rng:
                a, b = rng.split("-", 1)
                ranges[chain_id] = (int(a) if a else None, int(b) if b else None)
            else:
                pos = int(rng)
                ranges[chain_id] = (pos, pos)
    else:
        requested = [c.strip() for c in mask_chains.split(",") if c.strip()]
        for c in requested:
            ranges[c] = (seq_from, seq_to)
    return ranges


def filter_model_by_fasta_ranges(input_model: Path, output_model: Path, chain_ranges: Dict[str, Tuple[Optional[int], Optional[int]]]) -> Dict[str, Any]:
    """Write a selected model using 1-based FASTA/order positions for each chain.

    This avoids relying on PDB residue numbering, which may be offset/truncated relative
    to the input FASTA.  It assumes each chain model preserves sequence order.
    """
    st = gemmi.read_structure(str(input_model))
    out_st = gemmi.Structure()
    out_st.cell = st.cell
    out_model = gemmi.Model("1")
    requested = set(chain_ranges)
    selected_counts: Dict[str, int] = {}
    total_counts: Dict[str, int] = {}
    for model in st:
        for chain in model:
            if chain.name not in requested:
                continue
            start, end = chain_ranges[chain.name]
            out_chain = gemmi.Chain(chain.name)
            fasta_pos = 0
            for res in chain:
                if res.name.strip().upper() in STANDARD_PROTEIN_RESNAMES:
                    fasta_pos += 1
                    total_counts[chain.name] = fasta_pos
                    if start is not None and fasta_pos < start:
                        continue
                    if end is not None and fasta_pos > end:
                        continue
                    out_chain.add_residue(res.clone())
            if len(out_chain) > 0:
                selected_counts[chain.name] = len(out_chain)
                out_model.add_chain(out_chain)
    if len(out_model) == 0:
        raise PipelineError(f"Mask FASTA/order selection selected no residues from {input_model}; ranges={chain_ranges}")
    out_st.add_model(out_model)
    output_model.parent.mkdir(parents=True, exist_ok=True)
    out_st.write_pdb(str(output_model))
    return {
        "selected_model": str(output_model.resolve()),
        "chain_ranges_fasta_positions_1_based": {k: list(v) for k, v in chain_ranges.items()},
        "selected_residue_counts": selected_counts,
        "observed_chain_lengths_in_model": total_counts,
    }


def make_mask(
    *,
    python_exe: str,
    mask_script: Path,
    map_path: Path,
    model: Path,
    out_dir: Path,
    chains: str,
    chain_ranges: Dict[str, Tuple[Optional[int], Optional[int]]],
    radius: float,
    dilate: float,
    soften: float,
    mask_prefix: str = "selected_chains_mask",
) -> Dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    selected_model = out_dir / "selected_chains_fasta_range_model.pdb"
    selection_report = filter_model_by_fasta_ranges(model, selected_model, chain_ranges)
    cmd = [
        python_exe,
        str(mask_script),
        "--map", str(map_path),
        "--model", str(selected_model),
        "--out-dir", str(out_dir),
        "--out-prefix", mask_prefix,
        "--chains", "all",
        "--mask-radius-A", str(radius),
        "--mask-dilate-A", str(dilate),
        "--mask-soften-A", str(soften),
        "--write-hard-mask",
        "--overwrite",
    ]
    run_cmd(cmd, cwd=out_dir, env=os.environ.copy(), log_path=out_dir / "make_mask.log", allow_fail=False)
    report_path = out_dir / f"{mask_prefix}.report.json"
    report = json.loads(report_path.read_text()) if report_path.exists() else {}
    report["mask_dir"] = str(out_dir.resolve())
    report["requested_mask_chains"] = chains
    report["fasta_range_selection"] = selection_report
    return report


# ----------------------------- vision QC gating -----------------------------

def parse_vision_qc_dock_verdicts(text: str) -> Dict[str, str]:
    """Parse --vision-qc-verdict-dock 'R=pass,N=fail' into {chain_id: 'pass'|'fail'}."""
    out: Dict[str, str] = {}
    for item in text.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise PipelineError(f"Bad --vision-qc-verdict-dock item {item!r}; expected chain=pass|fail")
        k, v = item.split("=", 1)
        v = v.strip().lower()
        if v not in {"pass", "fail"}:
            raise PipelineError(f"Bad --vision-qc-verdict-dock value {item!r}; expected pass or fail")
        out[k.strip()] = v
    return out


# ----------------------------- reporting/main -----------------------------

def parse_search_levels(text: str) -> List[int]:
    vals = [int(x) for x in re.split(r"[,\s]+", text.strip()) if x]
    if not vals:
        raise PipelineError("--search-levels is empty")
    return vals


def parse_dock_hands(text: str) -> List[str]:
    hands = [h.strip().lower() for h in text.split(",") if h.strip()]
    if not hands:
        raise PipelineError("--dock-hands cannot be empty")
    for h in hands:
        if h not in {"original", "flip"}:
            raise PipelineError(f"Invalid --dock-hands value {h!r}; use original, flip, or original,flip")
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(hands))


def parse_int_list(text: str) -> List[int]:
    vals = [int(x) for x in re.split(r"[,\s]+", text.strip()) if x]
    if not vals:
        raise PipelineError("Expected a non-empty comma-separated list of integers")
    return vals


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Multi-chain FASTA -> model prediction -> processed-map ChimeraX rigid docking -> Phenix refinement -> optional mask."
    )
    # Required / core parameters kept short.
    p.add_argument("--fasta", required=True, help="Multi-chain FASTA; each record is treated as one chain")
    p.add_argument("--map", required=True, help="Input original MRC/MAP volume. Real scoring uses original and flipped real maps; final refinement uses the selected winning hand")
    p.add_argument("--out-dir", required=True, help="Output run directory")
    p.add_argument("--resolution", type=float, required=True, help="Map resolution in Angstrom for fitmap/Phenix scoring/refinement")

    # Model generation choices.
    p.add_argument("--model-provider", default="swissmodel_templates", choices=["alphafold", "swissmodel", "swissmodel_templates", "esmfold", "rcsb_template", "generic", "all"], help="Per-chain modeling provider [default: alphafold]")
    p.add_argument("--target-res-from", type=float, default=None, help="First residue the MAP resolves (from the project datainfo / a published model of this sample). Template providers prefer templates whose modelled range matches this window over merely high-coverage ones.")
    p.add_argument("--target-res-to", type=float, default=None, help="Last residue the MAP resolves.")
    p.add_argument("--model-fallback-providers", default="alphafold,swissmodel,rcsb_template,esmfold", help="If the rigid fit does not reach --cc-gate, re-predict the model a different way and retry, in this order [default: alphafold,swissmodel,rcsb_template,esmfold, i.e. AlphaFold is the SECOND option after the state-aware template route]. swissmodel_templates fetches SEVERAL models on different templates, oligomers first -- the usual cure, since a failed fit is normally the wrong oligomeric state. A failed fit usually means a bad STARTING MODEL, not a bad search: an AlphaFold monomer of a domain-swapped channel subunit never fit our TRPV1 map at any CC, while a SWISS-MODEL tetramer of the same sequence docked straight away. Ignored with --skip-modeling.")
    p.add_argument("--no-model-fallback", action="store_true", help="Do not try other model providers when the rigid fit fails; fail immediately instead.")
    p.add_argument("--dock-all-models", action="store_true", help="Dock all successful provider models per chain, not only the priority model")
    p.add_argument("--max-models-per-chain", type=int, default=4, help="Limit when --dock-all-models is used [default: 4]")
    p.add_argument("--skip-modeling", action="store_true", help="Use existing models from --models-dir instead of predicting/downloading")
    p.add_argument("--models-dir", default=None, help="Directory with existing chain models when --skip-modeling is used")
    p.add_argument("--api-url", default=None, help="Optional generic modeling API URL")
    p.add_argument("--api-token-env", default=None, help="Optional env var name containing generic API token")

    # Successful J164-style processed docking-map protocol.
    p.add_argument("--cc-gate", type=float, default=0.40, help="Masked-area correlation (--score-key, Phenix CC_mask) that a dock must reach to count as SUCCESSFUL, in BOTH accept modes; also the fit floor for --refine-mode two_stage [default: 0.40]")
    p.add_argument("--score-key", default="CC_mask", help="Phenix score key for gate/selection [default: CC_mask]. This is Phenix's mean-subtracted map-model correlation, never ChimeraX's own DC-inflated `correlation` (skill gate 3)")
    p.add_argument("--search-levels", default="300,1000,3000", help="Comma-separated ChimeraX random-pose search ladder [default: 300,1000,3000]")
    p.add_argument("--dock-hands", default="original,flip", help="Map hands to rigid-dock, comma-separated: original, flip, or original,flip [default: original,flip]. The atomic model is NEVER mirrored (see module docstring); both hands test the same chiral model against different map orientations. For a known flip-hand case use --dock-hands flip to skip the redundant original-hand pass.")
    p.add_argument("--rigid-only", action="store_true", help="Stop after per-chain rigid docking/scoring; do not combine/refine or make masks")
    p.add_argument("--mirror-axis", default="x", choices=["x", "y", "z"], help="Map hand flip axis [default: x]")
    p.add_argument("--lower-percentile", type=float, default=90.0, help="Zero density below this percentile for docking map [default: 90]")
    p.add_argument("--upper-percentile", type=float, default=99.7, help="Cap density above this percentile for docking map [default: 99.7]")
    p.add_argument("--no-normalize", action="store_true", help="Disable positive-density normalization of docking map")
    p.add_argument("--no-autobox-dock-map", action="store_true", help="Disable auto-boxing of processed docking-only maps")
    p.add_argument("--autobox-percentile", type=float, default=99.5, help="Percentile threshold for docking-map autobox [default: 99.5]")
    p.add_argument("--autobox-margin-A", type=float, default=30.0, help="Margin around autoboxed docking map in Angstrom [default: 30]")
    p.add_argument("--autobox-multiple", type=int, default=4, help="Expand docking box dimensions to a multiple of N [default: 4]")

    # Skill gate 2/4: target-lobe cut + two-stage global(lobe)->local(whole) fit.
    p.add_argument("--target-lobe-map", default=None, help="OPT-IN restriction, NOT the recommended default (see --dock-method): a pre-cut target-lobe/segment submap (same grid/hand convention as --map; both original and one axis-flip are derived from it exactly like --map) used for hand-testing + global rigid docking + target-lobe scoring (skill gate 2). The accepted pose is then LOCALLY re-fit in the WHOLE map (skill gate 4) before being trusted for combined refinement. PowerFit's own core-weighted CC + Laplace edge filter handles multi-component maps unaided (hTA1 benchmark: a lone receptor chain placed correctly inside a whole GPCR-Gs complex map) -- use this flag when a whole-map search still mis-locks onto a brighter neighbouring component, and make sure the crop is comfortably larger than the model being docked (a tool warning fires if it is not; a tight crop starves PowerFit's edge filter -- measured on cb1_redo: CC_mask 0.08 in a tight sub-lobe vs 0.56 on the whole map, same model/hand).")
    p.add_argument("--local-refit-max-steps", type=int, default=150, help="ChimeraX local fitmap maxSteps for the stage-2 whole-map local refit when --target-lobe-map is used [default: 150]")
    p.add_argument("--lobe-crop-min-ratio", type=float, default=3.0, help="Minimum (lobe enclosed volume / model expected volume) ratio before the small-crop WARNING fires for --target-lobe-map [default: 3.0 -- 'comfortably larger', not merely bigger]. Advisory only; never blocks.")
    p.add_argument("--lobe-crop-level-box-fraction", type=float, default=0.005, help="Absolute density level for the small-crop check, taken from the WHOLE --map (not the lobe crop -- see check_lobe_crop_size docstring for why) as the value above which this fraction of the WHOLE map's box lies [default: 0.005, tighter than --dock-contour-box-fraction because this isolates the density CORE, not the full particle envelope]. Independent of --dock-contour-box-fraction/--dock-contour-level.")

    # Skill gate 3: multi-metric acceptance (%atoms-in-density + CC + chirality), not CC alone.
    p.add_argument("--accept-mode", default="multi_metric", choices=["multi_metric", "cc_only"], help="Dock acceptance rule [default: multi_metric]. multi_metric (skill gate 3): among hands docked under IDENTICAL conditions, prefer the hand with the highest fraction of atoms in density (>=--atoms-in-density-gate) and, among those, the highest --score-key; if no hand reaches the atoms gate the run is flagged NOT CONFIDENT rather than silently accepting the better of two bad fits. cc_only reproduces the pre-port behavior: --score-key >= --cc-gate, judged independently per hand. NOTE: this only governs the retired `fitmap_search` path (`dock_chain_model`); PowerFit-based docking (the default, and now the --target-lobe-map path too) always gates on --score-key alone, per PORT_NOTES.md sec4.")
    p.add_argument("--dock-method", default="powerfit", help="Rigid docking strategy [default: powerfit]. PowerFit (FFT 6D global search) + a PRECISE local ChimeraX fitmap, scored by masked CC, is now the ONLY global search method -- this also applies on the --target-lobe-map path (fixed 2026-08-19; it used to silently force the ladder below regardless of this flag). 'fitmap_search' -- the old ChimeraX random-restart ladder -- is RETIRED: measured STOCHASTIC on identical input (CC_mask 0.72/0.06/0.05, same HA case) and it got WORSE with more restarts on cb1_redo's receptor lobe (300->1000->3000: 0.057/0.037/0.029, the signature of local-minimum trapping), while PowerFit on the identical model/lobe/level scored 0.5725 (ACCEPT). Passing 'fitmap_search' is still ACCEPTED for backward compatibility: it logs a loud deprecation WARNING and is silently routed to 'powerfit'. Any other value raises an error.")
    p.add_argument("--powerfit-exe", default=os.environ.get("EMCLAW_POWERFIT_EXE", "YOUR_POWERFIT_PATH"), help="PowerFit executable (haddocking/powerfit).")
    p.add_argument("--powerfit-angle", type=float, default=10.0, help="PowerFit rotational sampling in degrees [default: 10]. Finer costs ~8x per halving.")
    p.add_argument("--powerfit-solutions", type=int, default=5, help="PowerFit solutions to polish and score per hand [default: 5]. For a Cn oligomer the top n are usually the n symmetry copies of one pose.")
    p.add_argument("--powerfit-procs", type=int, default=16, help="Processors for PowerFit [default: 16]")
    p.add_argument("--cc-review", type=float, default=0.25, help="Masked-CC REVIEW floor [default: 0.25]. >=--cc-gate accepts outright; between this and --cc-gate the chain is placed on the right component but the model/local density is imperfect -- accepted but flagged REVIEW so the QC image is read; below this is rejected (mirrored-map controls measured 0.04-0.06).")
    p.add_argument("--hand-ratio-min", type=float, default=3.0, help="Winning/opposite hand masked-CC ratio below which the handedness call is flagged AMBIGUOUS [default: 3.0; measured 6-17x on correct calls].")
    p.add_argument("--dock-contour-box-fraction", type=float, default=0.05, help="Contour level for docking/atoms-in-density is taken from the MAP histogram as the value above which this fraction of the box lies [default: 0.05]. No residue counts are involved.")
    p.add_argument("--dock-contour-level", type=float, default=None, help="Explicit contour level, overriding --dock-contour-box-fraction.")
    p.add_argument("--atoms-in-density-gate", type=float, default=0.60, help="Minimum fraction of model atoms inside the map contour for --accept-mode multi_metric [default: 0.60, per the skill's hTA1 worked example]")

    # Optional mask. No --mask-chains means no mask.
    p.add_argument("--mask-label", default="", help="SHORT slug describing WHAT is masked, e.g. 'receptor_only' or 'TRPV1_1copy_DkTx_RTX'. It becomes the mask FILENAME prefix and is written into the mask report and a MASK_README.txt, so that months later -- and in the CryoSPARC job list -- it is obvious which part of the complex this mask covers. Use it as the title/comment of the import_volumes and local-refine jobs too. Defaults to the chain IDs.")
    p.add_argument("--mask-description", default="", help="One-line human description of the masked region and the sample, e.g. 'one TRPV1 protomer (res 335-751, TM+pore) of the DkTx/RTX-bound open-state tetramer; ARD and nanodisc excluded'. Recorded alongside the mask.")
    p.add_argument("--sample-description", default="", help="What the SAMPLE is, from the project datainfo: components, ligands, activation state. Used to pick a template in the matching conformational state and recorded with the mask.")
    p.add_argument("--model-state", default="", help="Conformational/ligand STATE the model must represent, e.g. 'open, DkTx and RTX bound' or 'apo closed'. Template-based providers rank candidate templates against this -- a wrong-state template is the single biggest cause of a failed fit (TRPV1: apo templates gave masked CC 0.23-0.32, the matching open-state template gave 0.74 on the same map).")
    p.add_argument("--mask-chains", default="", help="Comma-separated final chain IDs to mask. Empty = no mask")
    p.add_argument("--mask-seq-from", type=int, default=None, help="1-based FASTA/order start position for mask selection, applied to all --mask-chains if --mask-chain-ranges is not set")
    p.add_argument("--mask-seq-to", type=int, default=None, help="1-based FASTA/order end position for mask selection, applied to all --mask-chains if --mask-chain-ranges is not set")
    p.add_argument("--mask-chain-ranges", default="", help="Per-chain FASTA/order ranges, e.g. 'R:1-300,N:1-120'. Overrides --mask-seq-from/--mask-seq-to")
    p.add_argument("--mask-radius-A", type=float, default=3.0, help="Radius of the ball painted around EVERY selected atom to build the hard atom mask [default: 3.0 A]. Flat, not element-specific. With --mask-dilate-pix this forms one combined hard-boundary budget (~6-7 A); 3.0 still merges along the backbone (C-C = 1.5 A) while a 4.0 ball is >2x a carbon vdW radius and alone reaches 1.7x the protein volume.")
    p.add_argument("--mask-dilate-A", type=float, default=None, help="Hard dilation beyond the per-atom --mask-radius-A. Default: --mask-dilate-pix x the map pixel size.")
    p.add_argument("--mask-dilate-pix", type=float, default=3.0, help="Default hard dilation in PIXELS [default: 3]. Overridden by --mask-dilate-A if given.")
    p.add_argument("--mask-soften-A", type=float, default=None, help="Cosine soft-edge width. Default: --mask-soften-x-res x --resolution (never below 3 px).")
    p.add_argument("--mask-soften-x-res", type=float, default=2.0, help="Default soft edge as a multiple of map RESOLUTION [default: 2.0]. A soft edge below ~2x resolution causes Fourier ringing and inflates masked FSC. Overridden by --mask-soften-A.")
    p.add_argument("--emit-mask-frame", default="both", choices=["winning", "input", "both"], help="Which grid(s) to emit mask/volume outputs on when the flipped map hand wins [default: both, backward compatible with pre-port behavior which always emitted the original-input-grid mask]. A MASK (a shape) may be mirrored even though the MODEL (chiral) never is. 'winning': only the mask built directly in the winning/corrected-hand frame (matches the model and the flipped map). 'input': only a copy mirrored back onto the ORIGINAL --map file's grid (matches particles/volumes still in the original frame, e.g. for a direct local-refine there). 'both' (default): emit and clearly label both, so a caller picks whichever frame matches what they are refining. No effect when the original hand wins (the two frames are the same file).")

    # Skill gate 6: two-stage Phenix refine + geometry gates.
    p.add_argument("--map-symmetry", default="C1", help="Point group ALREADY IMPOSED on --map (C1, Cn, Dn) [default: C1]. When not C1, the docked component(s) are copied by symmetry to FILL the map before flexible refinement, so every neighbouring subunit's density is occupied: a lone chain refined in a complex map drifts into the empty neighbour density and MolProbity, seeing only the isolated chain, cannot detect the resulting inter-chain clashes. The mask is still built from --mask-chains only. Axis is +Z through the box centre (CryoSPARC convention).")
    p.add_argument("--max-drift-warn-A", type=float, default=2.0, help="Warn if the CA RMSD between the rigid-docked and the refined model exceeds this [default: 2.0 A].")
    p.add_argument("--max-drift-fail-A", type=float, default=5.0, help="Flag the refinement as REJECTED-DRIFT if max CA shift from the docked pose exceeds this [default: 5.0 A].")
    p.add_argument("--trim-extend-dock", type=int, default=10, help="After rigid fitting, drop residues outside the map envelope keeping this many residues of margin at both ends of every retained stretch, then re-score the masked CC and apply --cc-gate to THAT [default: 10].")
    p.add_argument("--trim-extend-refine", type=int, default=5, help="After flexible refinement, trim to density again with this margin; the final masked CC and the MASK are both computed from the trimmed model [default: 5].")
    p.add_argument("--no-density-trim", action="store_true", help="Disable both density trims; score and mask the full model.")
    p.add_argument("--post-refine-cc-warn", type=float, default=0.50, help="Warn loudly if the FINAL masked CC after flexible refinement is below this [default: 0.50]. Below it, refinement has not produced a model that explains the density: read the QC image before trusting the mask.")
    p.add_argument("--refine-mode", default="two_stage", choices=["two_stage", "legacy"], help="Combined-model refinement strategy [default: two_stage]. two_stage (skill gate 6): SS-restrained density search (macro_cycles=3, minimization_global+local_grid_search) then gentle cleanup, testing --refine-cleanup-cycles in order and keeping the FIRST endpoint that passes both a fit floor (>= --cc-gate on --score-key, unless it regresses below the starting model) and geometry gates (clashscore<10, Rama favored>96%%, Rama outliers<0.5%%, rotamer outliers<2%% by default); automatically falls back to legacy if phenix.secondary_structure_restraints/phenix.molprobity are unavailable. legacy reproduces the pre-port single-call minimization_global+local_grid_search+adp refine.")
    p.add_argument("--refine-cleanup-cycles", default="1,2,3", help="Comma-separated cleanup macro_cycles counts to test in --refine-mode two_stage, tried IN ORDER, first pass wins [default: 1,2,3]")
    p.add_argument("--geometry-clashscore-max", type=float, default=GEOMETRY_DEFAULT_GATES["clashscore_max"])
    p.add_argument("--geometry-rama-favored-min", type=float, default=GEOMETRY_DEFAULT_GATES["rama_favored_min"])
    p.add_argument("--geometry-rama-outliers-max", type=float, default=GEOMETRY_DEFAULT_GATES["rama_outliers_max"])
    p.add_argument("--geometry-rotamer-outliers-max", type=float, default=GEOMETRY_DEFAULT_GATES["rotamer_outliers_max"])

    # Vision QC gates (render+assess model-in-density at end-of-docking and end-of-refinement).
    p.add_argument("--vision-qc", default="render", choices=["off", "render", "gate"], help="Vision QC image gate at end-of-docking and end-of-refinement [default: render]. render: always best-effort render a model-in-density montage at both points and record its path in the report; never blocks/fails the run even if rendering itself errors. gate: additionally STOP after rendering and require --vision-qc-verdict-dock / --vision-qc-verdict-refine on a re-invocation before proceeding, for an agent/human to Read() the PNG and judge helices threading into density vs residues floating outside. off: skip rendering entirely.")
    p.add_argument("--vision-qc-verdict-dock", default="", help="--vision-qc gate only: per-chain verdicts after reading the dock QC image, e.g. 'R=pass,N=fail'")
    p.add_argument("--vision-qc-verdict-refine", default=None, choices=["pass", "fail"], help="--vision-qc gate only: verdict after reading the final refine QC image")

    # Tool paths and debug knobs.
    p.add_argument("--python", default=sys.executable, help="Python executable for helper scripts [default: current Python]")
    p.add_argument("--modeling-script", default=str(Path(__file__).resolve().parent / "make_homology_model_api.py"))
    p.add_argument("--mask-script", default=str(Path(__file__).resolve().parent / "make_model_soft_mask.py"))
    p.add_argument("--chimerax", default=os.environ.get("EMCLAW_CHIMERAX", "chimerax"))
    p.add_argument("--phenix-bin", default=os.environ.get("EMCLAW_PHENIX_BIN", "YOUR_PHENIX_BIN_PATH"))
    p.add_argument("--phenix-refine-extra", default="", help="Extra quoted Phenix real_space_refine PHIL args (legacy refine, or R1 search stage of two_stage)")
    p.add_argument("--keep-hetero", action="store_true", help="Keep HETATM/nonstandard residues in combined model before Phenix refinement. Default strips them to avoid missing-restraint failures")
    p.add_argument("--continue-on-model-error", action="store_true", help="Continue if one provider fails during model generation")
    p.add_argument("--dry-run", action="store_true", help="Only parse inputs and write normalized FASTA/config; do not run modeling/docking")
    return p.parse_args()


def write_summary(report: Dict[str, Any], out_dir: Path) -> None:
    lines: List[str] = []
    lines.append("Multi-chain cryo-EM autofit summary")
    lines.append("===================================")
    lines.append(f"Run time: {report.get('started_at')} -> {report.get('finished_at')}")
    lines.append(f"Input FASTA: {report.get('input_fasta')}")
    lines.append(f"Input map: {report.get('input_map')}")
    if report.get("target_lobe_map"):
        lines.append(f"Target-lobe map (skill gate 2): {report.get('target_lobe_map')}")
    lines.append(f"Dock hands: {','.join(report.get('dock_hands', [])) or 'original,flip'}")
    lines.append(f"Accept mode: {report.get('accept_mode')} (atoms-in-density gate: {report.get('atoms_in_density_gate')})")
    lines.append(f"Search levels: {report.get('search_levels')}")
    lines.append(f"Processed docking maps: {report.get('maps', {}).get('dock_original')} ; {report.get('maps', {}).get('dock_flip')}")
    lines.append("")
    lines.append("Per-chain best rigid results:")
    for r in report.get("chain_results", []):
        cc = r.get("cc", {})
        val = cc.get("gate_score_value", cc.get(report.get("score_key", "CC_mask")))
        lines.append(
            f"  chain {r.get('chain_id')}: hand={r.get('hand')} search={r.get('search')} "
            f"provider={r.get('provider')} {report.get('score_key')}={val} "
            f"atoms_in_density={cc.get('atoms_in_density_fraction')} passed={r.get('passed')}"
        )
        lines.append(f"    docked: {r.get('dock_model')}")
        if r.get("model_original_hand"):
            lines.append(f"    prepared fit-hand model: {r.get('model_original_hand')}")
        if r.get("dock_vision_qc_png"):
            lines.append(f"    dock vision QC image: {r.get('dock_vision_qc_png')}")
        if r.get("dock_vision_qc_verdict"):
            lines.append(f"    dock vision QC verdict: {r.get('dock_vision_qc_verdict')}")
    lines.append("")
    lines.append(f"Successful chains: {','.join(report.get('successful_chains', [])) or 'none'}")
    if report.get("refinement_hand"):
        lines.append(f"Refinement hand: {report.get('refinement_hand')}")
        lines.append(f"Refinement map: {report.get('refinement_map')}")
    if report.get("input_map_hand_corrected"):
        lines.append("NOTE: input --map was WRONG-HAND. All outputs are on the FLIPPED grid "
                      f"({report.get('output_grid_reference_map')}), not the original --map file. "
                      "The atomic model was never mirrored -- only the map hand was chosen.")
    if report.get("combined_model"):
        lines.append(f"Combined model before refine: {report.get('combined_model')}")
    if report.get("refine_mode"):
        lines.append(f"Refine mode: {report.get('refine_mode')}")
    if report.get("refined_model"):
        lines.append(f"Refined model: {report.get('refined_model')}")
        lines.append(f"Final CC: {report.get('final_cc')}")
    if report.get("refine_vision_qc_png"):
        lines.append(f"Refine vision QC image: {report.get('refine_vision_qc_png')}")
    if report.get("refine_vision_qc_verdict"):
        lines.append(f"Refine vision QC verdict: {report.get('refine_vision_qc_verdict')}")
    if report.get("mask_report"):
        lines.append(f"Mask report: {report.get('mask_report')}")
    lines.append("")
    lines.append("Important: processed maps are for rigid search only. Final CC/refinement use the selected real map hand. The atomic model is NEVER mirrored -- only the map hand is chosen (see module docstring HANDEDNESS RULE). A MASK is a shape, not a chiral object, so it MAY be mirrored: --emit-mask-frame controls whether mask outputs are on the winning/corrected-hand grid, mirrored back to the original --map grid, or both (default: both).")
    (out_dir / "multichain_autofit_summary.txt").write_text("\n".join(lines) + "\n")


def main() -> int:
    args = parse_args()

    # --dock-method: PowerFit is now the only global search (2026-08-19, cb1_redo evidence).
    # 'fitmap_search' is RETIRED, not removed outright from the CLI -- an old caller passing it
    # must not crash obscurely; log why and route to PowerFit instead.
    if args.dock_method == "fitmap_search":
        log("WARNING: --dock-method fitmap_search is RETIRED. It was the ChimeraX random-restart "
            "global ladder and measured STOCHASTIC on identical input (CC_mask 0.72/0.06/0.05 on "
            "the same HA case), and on cb1_redo's receptor lobe it got WORSE with MORE restarts "
            "(search 300/1000/3000 -> CC_mask 0.057/0.037/0.029), the signature of local-minimum "
            "trapping -- more search time did not help. PowerFit on the identical model/lobe/level "
            "scored 0.5725 (hand_ratio 7.26, ACCEPT). Routing this run to --dock-method powerfit; "
            "chimerax_local_refit() (the LOCAL polish step) is unaffected -- only the retired "
            "GLOBAL search ladder is being replaced. See PORT_NOTES.md sec2.")
        args.dock_method = "powerfit"
    elif args.dock_method != "powerfit":
        raise PipelineError(
            f"Unknown --dock-method {args.dock_method!r}. Only 'powerfit' is supported; "
            "'fitmap_search' is accepted for backward compatibility and auto-routed to powerfit "
            "with a warning (see PORT_NOTES.md sec2)."
        )

    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    report: Dict[str, Any] = {
        "started_at": now(),
        "input_fasta": str(Path(args.fasta).expanduser().resolve()),
        "input_map": str(Path(args.map).expanduser().resolve()),
        "resolution": args.resolution,
        "cc_gate": args.cc_gate,
        "score_key": args.score_key,
        "accept_mode": args.accept_mode,
        "dock_method": args.dock_method,
        "cc_review": args.cc_review,
        "atoms_in_density_gate": args.atoms_in_density_gate,
        "search_levels": parse_search_levels(args.search_levels),
        "dock_hands": parse_dock_hands(args.dock_hands),
        "refine_mode": args.refine_mode,
        "vision_qc": args.vision_qc,
        "chain_results": [],
        "successful_chains": [],
    }

    global DOCK_CONTOUR_BOX_FRACTION, DOCK_CONTOUR_LEVEL
    DOCK_CONTOUR_BOX_FRACTION = args.dock_contour_box_fraction
    DOCK_CONTOUR_LEVEL = args.dock_contour_level
    phenix_env = make_env(args.phenix_bin)
    chimerax_exe = require_exe(args.chimerax)
    require_exe("phenix.map_model_cc", phenix_env)
    require_exe("phenix.real_space_refine", phenix_env)
    modeling_script = Path(args.modeling_script).expanduser().resolve()
    mask_script = Path(args.mask_script).expanduser().resolve()
    if not modeling_script.exists() and not args.skip_modeling:
        raise PipelineError(f"Missing modeling script: {modeling_script}")
    if args.mask_chains and not mask_script.exists():
        raise PipelineError(f"Missing mask script: {mask_script}")

    geometry_gates = {
        "clashscore_max": args.geometry_clashscore_max,
        "rama_favored_min": args.geometry_rama_favored_min,
        "rama_outliers_max": args.geometry_rama_outliers_max,
        "rotamer_outliers_max": args.geometry_rotamer_outliers_max,
    }
    cleanup_cycles = parse_int_list(args.refine_cleanup_cycles)

    chains = parse_fasta_multi(Path(args.fasta).expanduser().resolve())
    fasta_dir = out_dir / "01_chain_fastas"
    write_chain_fastas(chains, fasta_dir)
    report["chains"] = [asdict(c) for c in chains]

    # STATE BEATS SEQUENCE IDENTITY (PORT_NOTES.md, top of file) -- the single biggest cause of a
    # failed/misleading rigid fit is a starting model in the wrong conformational/ligand state, and
    # sequence identity alone does not detect this (TRPV1 5IRX-open-matching=0.7384 ACCEPT vs
    # apo/wrong-species templates at 0.23-0.32; cb1_redo's own state-matched 5U09=0.5725 ACCEPT vs
    # wrong-state 6KPG (identity 0.791, active/Gi-coupled) = 0.3187 REVIEW, hand_ratio 7.48 -> 3.52 --
    # a real, costly drop, though NOT always a collapse to the pure noise floor: shape/fold overlap
    # between states can still buy a REVIEW-band score, which is exactly why it must not be trusted
    # on CC_mask alone -- see PORT_NOTES.md sec 0).
    # Both routes below that can silently skip the tool's own state-aware template ranking are
    # flagged LOUDLY, once, up front -- not buried in a per-chain log line.
    if args.skip_modeling:
        log("WARNING: template state-aware ranking is bypassed under --skip-modeling; you are "
            "responsible for having chosen a STATE-MATCHED model -- sequence identity alone is "
            "NOT sufficient (see PORT_NOTES.md 'state beats identity'). A wrong-state template can "
            "score well below a state-matched one (measured on cb1_redo: state-matched 5U09 "
            "CC_mask=0.5725 ACCEPT/hand_ratio=7.48 vs wrong-state 6KPG, identity 0.791, "
            "CC_mask=0.3187 REVIEW/hand_ratio=3.52 -- barely above the ambiguity floor) even with "
            "HIGH sequence identity, or, conversely, LOOK plausible while the pose is structurally "
            "wrong -- always sanity-check the docked model against the map by eye (qc_slices.py), "
            "not by CC_mask alone.")
        report["skip_modeling_state_warning"] = True
    if not args.model_state:
        log("WARNING: --model-state is empty/absent; template state-aware ranking has nothing to "
            "rank on and silently degrades to coverage/identity ordering (the exact failure mode "
            "that buried the correct TRPV1 template behind apo templates padded to higher coverage "
            "before this was fixed -- see PORT_NOTES.md sec 8/top). Pass --model-state describing "
            "the conformational/ligand state this map represents (e.g. 'inactive, antagonist-bound' "
            "/ 'open, agonist-bound') whenever a provider ladder (not --skip-modeling) is in play.")
        report["model_state_missing_warning"] = True

    if args.dry_run:
        report["finished_at"] = now()
        (out_dir / "multichain_autofit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        write_summary(report, out_dir)
        log(f"Dry run done: {out_dir}")
        return 0

    maps = prepare_maps(
        Path(args.map).expanduser().resolve(),
        out_dir,
        args.mirror_axis,
        args.lower_percentile,
        args.upper_percentile,
        normalize=not args.no_normalize,
        autobox=not args.no_autobox_dock_map,
        autobox_percentile=args.autobox_percentile,
        autobox_margin_A=args.autobox_margin_A,
        autobox_multiple=args.autobox_multiple,
    )
    report["maps"] = maps

    lobe_maps: Optional[Dict[str, Any]] = None
    if args.target_lobe_map:
        lobe_input = Path(args.target_lobe_map).expanduser().resolve()
        if not lobe_input.exists():
            raise PipelineError(f"--target-lobe-map does not exist: {lobe_input}")
        lobe_maps = prepare_maps(
            lobe_input, out_dir / "00_lobe", args.mirror_axis, args.lower_percentile,
            args.upper_percentile, normalize=not args.no_normalize, autobox=not args.no_autobox_dock_map,
            autobox_percentile=args.autobox_percentile, autobox_margin_A=args.autobox_margin_A,
            autobox_multiple=args.autobox_multiple,
        )
        report["target_lobe_map"] = str(lobe_input)
        report["lobe_maps"] = lobe_maps

    if lobe_maps is None:
        # 2026-08-19: reversed from a WARNING recommending --target-lobe-map. Measurement now
        # contradicts that advice -- PowerFit's own core-weighted CC + Laplace edge filter
        # handles multi-component maps unaided (the hTA1 benchmark placed a receptor chain
        # correctly inside the WHOLE GPCR-Gs complex map, CC_mask 0.3977). The whole map is the
        # VALIDATED DEFAULT; --target-lobe-map is an explicit opt-in for when a whole-map search
        # still mis-locks onto a brighter neighbouring component (see PORT_NOTES.md sec2/sec3).
        log("NOTE: docking against the WHOLE --map (no --target-lobe-map). This is the validated "
            "default for PowerFit -- proceeding.")
    else:
        log("NOTE: --target-lobe-map supplied: the whole-map PowerFit search is the validated "
            "default (see PORT_NOTES.md sec2/sec3); the lobe restricts stage-1 CANDIDATE "
            "identification only, and stage 2 always re-fits + rescores against the WHOLE map. "
            "Measured cb1_redo cases: receptor CC_mask 0.5725 on BOTH the lobe (ratio 7.26) and "
            "the whole map (ratio 7.48); Fv CC_mask 0.08 (ratio 1.2-1.3, REJECT) on a tight lobe "
            "vs 0.56 (ratio 5.6, ACCEPT) on the whole map -- a small lobe crop can starve "
            "PowerFit's edge filter (see the crop-size check above).")

    all_best: List[DockResult] = []
    successful: List[DockResult] = []
    models_root = out_dir / "02_models"
    dock_root = out_dir / "03_rigid_docking"
    prepared_root = out_dir / "04_selected_fit_hand"

    for chain in chains:
        # Provider ladder: if the rigid fit fails, the starting MODEL is the usual suspect (an
        # AlphaFold monomer of a domain-swapped channel subunit never fit our TRPV1 map at any
        # CC, while a SWISS-MODEL tetramer of the same sequence docked immediately). So predict
        # the model a different way and try again, rather than accepting a bad pose.
        provider_ladder = [args.model_provider]
        if not args.skip_modeling and not args.no_model_fallback:
            for fb in [q.strip() for q in args.model_fallback_providers.split(",") if q.strip()]:
                if fb not in provider_ladder:
                    provider_ladder.append(fb)
        chain_best: Optional[DockResult] = None
        tried_providers: List[str] = []
        def _accepted(r: Optional[DockResult]) -> bool:
            # The ladder is driven by the ACCEPT gate, not by "did not crash": a REVIEW-band fit
            # (right component, imperfect model) is exactly the case a different PREDICTION may
            # fix. TRPV1 from-start scraped 0.2506 on the AlphaFold monomer and would have
            # proceeded to a 4.78x-volume mask around a geometry-broken model, while a
            # SWISS-MODEL tetramer of the same sequence docks properly.
            return bool(r) and math.isfinite(float(r.cc.get(args.score_key, float("nan")))) \
                and float(r.cc.get(args.score_key, float("-inf"))) >= args.cc_gate

        for prov_i, provider in enumerate(provider_ladder):
            if _accepted(chain_best):
                break
            if prov_i > 0:
                best_so_far = (float(chain_best.cc.get(args.score_key, float("nan")))
                               if chain_best else float("nan"))
                log(f"=== chain {chain.chain_id}: best {args.score_key} so far {best_so_far:.4f} < "
                    f"ACCEPT gate {args.cc_gate} with provider(s) {tried_providers} -- switching "
                    f"model prediction to '{provider}' and retrying ===")
            log(f"=== Modeling chain {chain.chain_id} (provider {provider}): {chain.name} ===")
            tried_providers.append(provider)
            try:
                model_infos = predict_models_for_chain(
                    chain,
                    out_dir=models_root / f"provider_{safe_id(provider, 'p')}" if prov_i else models_root,
                    python_exe=args.python,
                    modeling_script=modeling_script,
                    provider=provider,
                    continue_on_error=args.continue_on_model_error or provider == "all",
                    api_url=args.api_url,
                    api_token_env=args.api_token_env,
                    skip_modeling=args.skip_modeling,
                    models_dir=Path(args.models_dir).expanduser().resolve() if args.models_dir else None,
                    model_state=args.model_state,
                    target_res_from=args.target_res_from, target_res_to=args.target_res_to,
                )
            except Exception as e:
                log(f"WARNING: chain {chain.chain_id}: provider {provider} produced no model ({e})")
                continue
            selected_models = choose_models(model_infos, args.dock_all_models or prov_i > 0,
                                            args.max_models_per_chain)
            for idx, model_info in enumerate(selected_models, start=1):
                # Surface the chosen template's RCSB title (when a provider supplied one) so a
                # wrong-state template is visible to whoever reads the log/report later, not just
                # to the ranking logic that (for swissmodel_templates) already used it.
                title_str = str(model_info.get("template_title") or "").strip()
                title_note = f" template_title={title_str[:80]!r}" if title_str else ""
                log(f"=== Docking chain {chain.chain_id}, candidate {idx}/{len(selected_models)} provider={model_info.get('provider')}{title_note} ===")
                if title_str:
                    report.setdefault("template_titles", {})[
                        f"{chain.chain_id}_candidate{idx}_{model_info.get('provider')}"] = title_str
                candidate_out_dir = dock_root / f"chain_{chain.chain_id}" / f"model_{idx}_{safe_id(str(model_info.get('provider')), 'provider')}"

                # Small-crop check (2026-08-19): PowerFit needs surrounding context for its
                # Laplace edge filter, so a --target-lobe-map cropped tightly around a small
                # domain can starve it -- the actual, measured failure condition for this flag,
                # not the wrong-component risk the flag itself exists to prevent. Uses the
                # ACTUAL candidate model's own residue count (not the source FASTA record, which
                # can be much longer, e.g. a BRIL-fusion construct behind a receptor-only model).
                # Advisory only -- logs and records in the report, never blocks.
                if lobe_maps is not None:
                    try:
                        n_res = count_model_residues(Path(model_info["model_path"]))
                        crop_check = check_lobe_crop_size(
                            Path(lobe_maps["real_original"]), Path(args.map).expanduser().resolve(),
                            n_res, min_ratio=args.lobe_crop_min_ratio,
                            level_box_fraction=args.lobe_crop_level_box_fraction,
                        )
                        report.setdefault("lobe_crop_size_checks", {})[
                            f"{chain.chain_id}_candidate{idx}"] = crop_check
                    except Exception as _e:
                        log(f"WARNING: lobe crop-size check failed for chain {chain.chain_id} "
                            f"candidate {idx}: {_e}")

                # PowerFit is the only global search method (fixed 2026-08-19): args.dock_method is
                # guaranteed "powerfit" by this point (see the deprecation routing at the top of
                # main()). The old `elif args.dock_method == "powerfit" / else: dock_chain_model(...)`
                # three-way split -- which is exactly the bug that let a --target-lobe-map run
                # silently skip PowerFit regardless of --dock-method -- is gone: `dock_chain_model()`
                # (the retired ChimeraX ladder) is no longer reachable from this dispatch at all; it
                # is kept, unused, only in case some other script imports and calls it as a library.
                if lobe_maps is not None:
                    result = dock_chain_two_stage(
                        chain=chain, model_info=model_info, whole_maps=maps, lobe_maps=lobe_maps,
                        out_dir=candidate_out_dir, cc_gate=args.cc_gate, cc_review=args.cc_review,
                        score_key=args.score_key, resolution=args.resolution,
                        chimerax_exe=chimerax_exe, phenix_env=phenix_env, dock_hands=report["dock_hands"],
                        local_max_steps=args.local_refit_max_steps, powerfit_exe=args.powerfit_exe,
                        powerfit_angle=args.powerfit_angle, powerfit_solutions=args.powerfit_solutions,
                        powerfit_procs=args.powerfit_procs, hand_ratio_min=args.hand_ratio_min,
                        keep_hetero=args.keep_hetero,
                    )
                else:
                    result = dock_chain_powerfit(
                        chain=chain, model_info=model_info, maps=maps, out_dir=candidate_out_dir,
                        cc_gate=args.cc_gate, cc_review=args.cc_review, score_key=args.score_key,
                        resolution=args.resolution, chimerax_exe=chimerax_exe, phenix_env=phenix_env,
                        dock_hands=report["dock_hands"], powerfit_exe=args.powerfit_exe,
                        powerfit_angle=args.powerfit_angle, powerfit_solutions=args.powerfit_solutions,
                        powerfit_procs=args.powerfit_procs, local_max_steps=args.local_refit_max_steps,
                        hand_ratio_min=args.hand_ratio_min, keep_hetero=args.keep_hetero,
                        trim_extend=(None if args.no_density_trim else args.trim_extend_dock),
                    )
                if result and (chain_best is None or float(result.cc.get(args.score_key, float("-inf"))) > float(chain_best.cc.get(args.score_key, float("-inf")))):
                    chain_best = result
                if _accepted(result):
                    break

        if chain_best:
            all_best.append(chain_best)
            prepared = prepare_selected_model_for_refine_hand(
                chain_best, prepared_root,
                reserved={c.chain_id for c in chains} | {r.chain_id for r in all_best})
            chain_result = asdict(prepared)

            if args.vision_qc != "off":
                dock_qc_dir = prepared_root / f"vision_qc_dock_chain_{chain.chain_id}"
                try:
                    png, qc_log = render_dock_qc(
                        model_pdb=Path(prepared.model_original_hand), map_mrc=Path(prepared.score_map),
                        out_dir=dock_qc_dir, chimerax_exe=chimerax_exe,
                    )
                    chain_result["dock_vision_qc_png"] = str(png)
                    chain_result["dock_vision_qc_log"] = str(qc_log)
                except Exception as e:
                    chain_result["dock_vision_qc_error"] = str(e)
                    log(f"WARNING: dock vision QC render failed for chain {chain.chain_id}: {e}")

                if args.vision_qc == "gate":
                    verdicts = parse_vision_qc_dock_verdicts(args.vision_qc_verdict_dock)
                    v = verdicts.get(chain.chain_id)
                    if v is None:
                        report["chain_results"].append(chain_result)
                        (out_dir / "multichain_autofit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
                        write_summary(report, out_dir)
                        raise PipelineError(
                            f"AWAITING_VISION_QC(dock, chain {chain.chain_id}): read "
                            f"{chain_result.get('dock_vision_qc_png')} (helices threading into density? "
                            f"residues floating outside?) then re-run with --vision-qc-verdict-dock "
                            f"'{chain.chain_id}=pass' (or fail)."
                        )
                    chain_result["dock_vision_qc_verdict"] = v
                    if v == "fail":
                        log(f"chain {chain.chain_id}: dock vision QC verdict=FAIL; excluding from combined refinement")
                        report["chain_results"].append(chain_result)
                        (out_dir / "multichain_autofit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
                        write_summary(report, out_dir)
                        continue

            report["chain_results"].append(chain_result)
            if prepared.passed:
                successful.append(prepared)
                report["successful_chains"].append(prepared.chain_id)
        else:
            report["chain_results"].append({"chain_id": chain.chain_id, "error": "no docking result"})

        report.setdefault("providers_tried", {})[chain.chain_id] = tried_providers
        (out_dir / "multichain_autofit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        write_summary(report, out_dir)

    if args.rigid_only:
        report["finished_at"] = now()
        report["status"] = "completed_rigid_only"
        report["rigid_only_note"] = "Stopped after per-chain rigid docking/scoring by --rigid-only; no combined refinement or mask generated."
        (out_dir / "multichain_autofit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        write_summary(report, out_dir)
        log(f"DONE rigid-only: {out_dir}")
        log(f"Summary: {out_dir / 'multichain_autofit_summary.txt'}")
        return 0

    if not successful:
        report["finished_at"] = now()
        report["status"] = "failed_no_chain_passed_accept_gate"
        (out_dir / "multichain_autofit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
        write_summary(report, out_dir)
        raise PipelineError(
            "RIGID FIT FAILED -- STOPPING, and NOT building a mask.\n"
            f"  No chain reached {args.score_key} >= {args.cc_review} (REVIEW floor); accept gate is "
            f"{args.cc_gate}. Best per chain: "
            + "; ".join(f"{r.chain_id}={r.cc.get(args.score_key)}" for r in all_best) + "\n"
            f"  Model providers tried: {report.get('providers_tried')}\n"
            "  A mask built from a fit this bad is meaningless -- it would put a soft mask around "
            "empty space or around the WRONG component, and every downstream local refine would "
            "inherit that. So this stops here on purpose.\n"
            "  ASK THE HUMAN for one of: (a) a better starting model -- the right oligomeric state "
            "matters, an isolated subunit of a domain-swapped or intertwined assembly often will not "
            "fit as a rigid body; (b) confirmation that this component is actually present in this "
            "map; (c) the correct map hand or symmetry; (d) a --target-lobe-map cut around the "
            "expected component. Read the dock QC images first -- they show where each attempt "
            "landed."
        )

    refinement_hand = choose_refinement_hand(successful, args.score_key)
    successful_for_refine = [r for r in successful if r.hand == refinement_hand]
    dropped_for_hand = [r.chain_id for r in successful if r.hand != refinement_hand]
    refinement_map = Path(maps["real_original"] if refinement_hand == "original" else maps["real_flip"]).resolve()
    report["refinement_hand"] = refinement_hand
    report["refinement_map"] = str(refinement_map)
    report["output_grid_reference_map"] = str(refinement_map)
    report["dropped_successful_chains_from_other_hand"] = dropped_for_hand
    report["input_map_hand_corrected"] = (refinement_hand == "flip")
    if refinement_hand == "flip":
        log(f"WARNING: input --map is WRONG-HAND (mirror of the true chirality); all outputs "
            f"(refined model, masks) are expressed on the {args.mirror_axis.upper()}-FLIPPED grid "
            f"({refinement_map}), NOT the original --map file's grid. The atomic MODEL was never "
            f"mirrored -- only the MAP hand was chosen (see PORT_NOTES.md).")

    # Mask geometry defaults: dilation in PIXELS, soft edge as a multiple of RESOLUTION.
    with mrcfile.open(str(refinement_map), permissive=True) as _m:
        _apix = float(_m.voxel_size.x)
    mask_dilate_A = (args.mask_dilate_A if args.mask_dilate_A is not None
                     else args.mask_dilate_pix * _apix)
    mask_soften_A = (args.mask_soften_A if args.mask_soften_A is not None
                     else max(args.mask_soften_x_res * args.resolution, 3.0 * _apix))
    report["mask_geometry"] = {
        "pixel_size_A": _apix, "resolution_A": args.resolution,
        "mask_radius_A": args.mask_radius_A,
        "mask_dilate_A": mask_dilate_A, "mask_dilate_pix": mask_dilate_A / _apix,
        "mask_soften_A": mask_soften_A, "mask_soften_pix": mask_soften_A / _apix,
        "mask_soften_x_resolution": mask_soften_A / args.resolution,
    }
    log(f"mask geometry: radius {args.mask_radius_A:.1f} A + dilate {mask_dilate_A:.2f} A "
        f"({mask_dilate_A/_apix:.1f} px) + soft edge {mask_soften_A:.2f} A "
        f"({mask_soften_A/_apix:.1f} px = {mask_soften_A/args.resolution:.1f} x resolution)")

    if "sharp" not in Path(args.map).name.lower():
        log(f"WARNING: flexible refinement will run against {Path(args.map).name}, whose name does "
            f"not look sharpened. Refinement (and the masked CC) MUST use the SHARPENED map -- "
            f"pass the *_volume_map_sharp.mrc, not the unsharpened volume.")
    report["refinement_map_is_sharpened_by_name"] = "sharp" in Path(args.map).name.lower()

    combine_dir = out_dir / "05_combined_refine"
    combined = combine_models(
        [Path(r.model_original_hand) for r in successful_for_refine if r.model_original_hand],
        combine_dir / f"combined_successful_chains_{refinement_hand}_hand_before_refine.pdb",
        protein_only=not args.keep_hetero,
    )
    report["combined_model"] = str(combined)
    report["combined_model_protein_only"] = not args.keep_hetero
    assert_model_chirality_ok(combined, "combined model before refine")

    # Fill the map by the point group already imposed on it, so flexible refinement sees the
    # neighbouring subunits instead of empty density it can drift into.
    sym_info = fill_map_by_symmetry(
        combined, refinement_map, args.map_symmetry,
        combine_dir / f"combined_symmetry_filled_{refinement_hand}_hand_before_refine.pdb")
    report["symmetry_fill"] = sym_info
    if sym_info["n_copies"] > 1:
        combined = Path(sym_info["output"])
        assert_model_chirality_ok(combined, "symmetry-filled model before refine")
        log(f"map symmetry {args.map_symmetry}: filled the map with {sym_info['n_copies']} copies "
            f"(target chains {sym_info['target_chains']}, added {sym_info['added_chains']}); "
            f"flexible refinement now sees the neighbours, the mask still uses --mask-chains only")
    else:
        log(f"map symmetry {args.map_symmetry}: no copies added. NOTE: if this map really does "
            f"contain other components, a lone chain can drift into their unoccupied density and "
            f"MolProbity cannot see the inter-chain clashes -- pass --map-symmetry, or supply every "
            f"component as its own FASTA record so all of them are docked first.")

    refine_log: Path
    if args.refine_mode == "two_stage":
        try:
            two_stage = phenix_two_stage_refine(
                env=phenix_env, model=combined, map_path=refinement_map,
                out_dir=combine_dir / f"refine_two_stage_on_{refinement_hand}_real_map",
                resolution=args.resolution, cleanup_cycles_to_test=cleanup_cycles,
                fit_score_key=args.score_key, fit_floor=args.cc_gate, geometry_gates=geometry_gates,
                r1_extra=args.phenix_refine_extra,
            )
            refined = Path(two_stage["selected_model"]).resolve()
            refine_log = Path(two_stage["r1_log"])
            report["refine_two_stage"] = two_stage
        except PipelineError as e:
            log(f"WARNING: --refine-mode two_stage unavailable/failed ({e}); falling back to legacy single-call refine")
            report["refine_mode"] = "legacy_fallback"
            refined, refine_log = phenix_real_space_refine(
                env=phenix_env, model=combined, map_path=refinement_map,
                out_dir=combine_dir / f"refine_on_{refinement_hand}_real_map",
                resolution=args.resolution, extra=args.phenix_refine_extra,
            )
    else:
        refined, refine_log = phenix_real_space_refine(
            env=phenix_env, model=combined, map_path=refinement_map,
            out_dir=combine_dir / f"refine_on_{refinement_hand}_real_map",
            resolution=args.resolution, extra=args.phenix_refine_extra,
        )
    assert_model_chirality_ok(refined, "final refined model")
    report["refined_model"] = str(refined)
    report["refine_log"] = str(refine_log)
    final_cc, final_cc_log = phenix_map_correlations(
        env=phenix_env,
        model=refined,
        score_map=refinement_map,
        out_dir=combine_dir / f"final_cc_vs_{refinement_hand}_real_map",
        resolution=args.resolution,
    )
    report["final_cc"] = final_cc
    report["final_cc_log"] = str(final_cc_log)
    _final = final_cc.get(args.score_key)
    if _final is not None and float(_final) < args.post_refine_cc_warn:
        msg = (f"WARNING: masked CC after flexible refinement is {float(_final):.3f}, below "
               f"{args.post_refine_cc_warn:.2f}. The refined model does NOT convincingly explain "
               f"the density -- the placement may be right while the model conformation is wrong. "
               f"READ the refine and mask QC images before using this mask, and check whether the "
               f"geometry gates passed (CC can rise while stereochemistry degrades).")
        log(msg)
        report["post_refine_cc_warning"] = msg
    report["post_refine_cc_warn_threshold"] = args.post_refine_cc_warn

    # Trim to density again after refinement (tighter margin now that the model has settled), and
    # report the masked CC of what will actually become the mask.
    mask_source_model = refined
    if not args.no_density_trim:
        try:
            trimmed_final = combine_dir / "refined_trimmed_to_density.pdb"
            tstat2 = trim_model_to_density(Path(refined), refinement_map, trimmed_final,
                                           extend_residues=args.trim_extend_refine)
            cc_tr, _ = phenix_map_correlations(
                env=phenix_env, model=trimmed_final, score_map=refinement_map,
                out_dir=combine_dir / "final_cc_after_trim", resolution=args.resolution)
            report["post_refine_density_trim"] = tstat2
            report["final_cc_trimmed"] = cc_tr
            mask_source_model = trimmed_final
            log(f"post-refine density trim (extend {args.trim_extend_refine} res): kept "
                f"{tstat2['kept_total']}, dropped {tstat2['dropped_total']}; masked CC "
                f"{final_cc.get(args.score_key)} -> {cc_tr.get(args.score_key)}  "
                f"(this trimmed model is what the MASK is built from)")
            _fv = cc_tr.get(args.score_key)
            if _fv is not None and float(_fv) < args.post_refine_cc_warn:
                log(f"WARNING: trimmed post-refine masked CC {float(_fv):.3f} < "
                    f"{args.post_refine_cc_warn:.2f} -- read the QC images before using this mask.")
        except Exception as e:
            log(f"WARNING: post-refine density trim failed ({e}); masking the untrimmed model")
    report["mask_source_model"] = str(mask_source_model)

    # Drift guard: flexible refinement must not walk away from the rigid pose that was validated.
    try:
        drift = measure_ca_drift(Path(report["combined_model"]), Path(refined))
        report["refine_drift"] = drift
        if drift.get("n_ca"):
            msg = (f"flexible-refine drift from the docked pose: CA RMSD {drift['rmsd_A']:.2f} A, "
                   f"max {drift['max_shift_A']:.2f} A ({100*drift['frac_over_2A']:.0f}% of residues "
                   f"moved >2 A)")
            if drift["max_shift_A"] > args.max_drift_fail_A:
                report["refine_drift_verdict"] = "REJECTED-DRIFT"
                log(f"WARNING: {msg} -- EXCEEDS --max-drift-fail-A {args.max_drift_fail_A} A. The "
                    f"refined model has walked off the validated rigid pose; do NOT trust the mask "
                    f"without reading the refine QC image.")
            elif drift["rmsd_A"] > args.max_drift_warn_A:
                report["refine_drift_verdict"] = "HIGH-DRIFT"
                log(f"WARNING: {msg} -- above --max-drift-warn-A {args.max_drift_warn_A} A.")
            else:
                report["refine_drift_verdict"] = "OK"
                log(msg)
    except Exception as e:
        log(f"WARNING: could not measure refine drift: {e}")

    if args.vision_qc != "off":
        refine_qc_dir = combine_dir / "vision_qc_refine"
        try:
            png, qc_log = render_dock_qc(model_pdb=refined, map_mrc=refinement_map, out_dir=refine_qc_dir, chimerax_exe=chimerax_exe)
            report["refine_vision_qc_png"] = str(png)
            report["refine_vision_qc_log"] = str(qc_log)
        except Exception as e:
            report["refine_vision_qc_error"] = str(e)
            log(f"WARNING: refine vision QC render failed: {e}")

        if args.vision_qc == "gate":
            if args.vision_qc_verdict_refine is None:
                report["finished_at"] = now()
                report["status"] = "awaiting_vision_qc_refine"
                (out_dir / "multichain_autofit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
                write_summary(report, out_dir)
                raise PipelineError(
                    f"AWAITING_VISION_QC(refine): read {report.get('refine_vision_qc_png')} (helices "
                    "threading into density? residues floating outside?) then re-run with "
                    "--vision-qc-verdict-refine pass (or fail)."
                )
            report["refine_vision_qc_verdict"] = args.vision_qc_verdict_refine
            if args.vision_qc_verdict_refine == "fail":
                report["finished_at"] = now()
                report["status"] = "refine_vision_qc_failed"
                (out_dir / "multichain_autofit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
                write_summary(report, out_dir)
                log("Refine vision QC verdict=FAIL; stopping before mask generation.")
                return 3

    if args.mask_chains.strip():
        mask_dir = out_dir / ("06_mask_on_original_grid" if refinement_hand == "original" else "06_mask_on_flip_grid")
        mask_chain_ranges = parse_mask_chain_ranges(args.mask_chain_ranges, args.mask_chains, args.mask_seq_from, args.mask_seq_to)
        mask_prefix = safe_id(args.mask_label, "") or ("mask_chain" + args.mask_chains.replace(",", ""))
        mask_prefix = mask_prefix if mask_prefix.endswith("mask") else mask_prefix + "_mask"
        mask_report = make_mask(
            python_exe=args.python,
            mask_script=mask_script,
            map_path=refinement_map,
            model=mask_source_model,
            out_dir=mask_dir,
            mask_prefix=mask_prefix,
            chains=args.mask_chains,
            chain_ranges=mask_chain_ranges,
            radius=args.mask_radius_A,
            dilate=mask_dilate_A,
            soften=mask_soften_A,
        )
        # Record WHAT was masked, in the report and as a README next to the .mrc, so the mask is
        # still self-explanatory when it turns up in a CryoSPARC job list months later.
        mask_report["mask_label"] = args.mask_label or mask_prefix
        mask_report["mask_description"] = args.mask_description
        mask_report["sample_description"] = args.sample_description
        mask_report["model_state"] = args.model_state
        try:
            soft_for_readme = mask_report.get("output_soft_mask", "")
            readme = mask_dir / "MASK_README.txt"
            readme.write_text(
                f"MASK: {args.mask_label or mask_prefix}\n"
                f"what is masked : {args.mask_description or ('chain(s) ' + args.mask_chains)}\n"
                f"sample         : {args.sample_description or '(not supplied -- see project datainfo)'}\n"
                f"model state    : {args.model_state or '(not specified)'}\n"
                f"chains masked  : {args.mask_chains}   (of the docked/refined model)\n"
                f"map            : {refinement_map}\n"
                f"resolution     : {args.resolution} A\n"
                f"geometry       : atom radius {args.mask_radius_A} A + dilate {mask_dilate_A:.2f} A "
                f"({mask_dilate_A/_apix:.1f} px) + soft edge {mask_soften_A:.2f} A "
                f"({mask_soften_A/args.resolution:.1f} x resolution)\n"
                f"masked CC      : rigid {report.get('chain_results', [{}])[0].get('cc', {}).get(args.score_key)}"
                f" -> refined {(report.get('final_cc_trimmed') or report.get('final_cc') or {}).get(args.score_key)}\n"
                f"soft mask      : {soft_for_readme}\n"
                f"QC image       : {mask_dir / 'mask_qc_slabs.png'}\n\n"
                "WHEN IMPORTING THIS MASK INTO CRYOSPARC: put the label and the 'what is masked' line\n"
                "into the import_volumes job TITLE and the local-refine job COMMENT, so anyone\n"
                "reviewing the processing can see which part of the complex was masked.\n")
            mask_report["mask_readme"] = str(readme)
            log(f"mask label: '{args.mask_label or mask_prefix}' -- {args.mask_description or 'chain(s) ' + args.mask_chains}")
            log(f"  -> use this as the CryoSPARC import_volumes TITLE / local-refine COMMENT; see {readme}")
        except Exception as e:
            log(f"WARNING: could not write mask README: {e}")

        # QC image of the MASK itself: does it wrap the target subunit, and how far past it?
        if args.vision_qc != "off":
            try:
                soft = mask_report.get("output_soft_mask")
                sel_model = mask_report.get("input_model") or str(refined)
                if soft:
                    png = qc_slices.render_mask_qc(
                        map_mrc=refinement_map, mask_mrc=Path(soft), model_pdb=Path(sel_model),
                        out_png=mask_dir / "mask_qc_slabs.png",
                        title=(f"mask chains={args.mask_chains} radius {args.mask_radius_A:.1f} + "
                               f"dilate {mask_dilate_A:.1f} A ({mask_dilate_A/_apix:.1f} px) + soft "
                               f"{mask_soften_A:.1f} A ({mask_soften_A/args.resolution:.1f}x res)"))
                    mask_report["mask_qc_png"] = str(png)
                    log(f"mask QC image: {png}")
                    mult = (mask_report.get("volumes_A3", {}) or {}).get(
                        "soft_mask_multiplier_vs_residue_estimate")
                    if mult is not None:
                        note = (f"soft mask is {float(mult):.2f}x the estimated protein volume "
                                f"(2-3x is a healthy focus mask; >4x means the mask is swallowing "
                                f"neighbouring subunits)")
                        log(("WARNING: " if float(mult) > 4.0 else "") + note)
                        mask_report["mask_volume_multiplier_note"] = note
            except Exception as e:
                mask_report["mask_qc_error"] = str(e)
                log(f"WARNING: mask QC render failed: {e}")

        if refinement_hand == "flip":
            # The MASK (a shape, not a chiral object) may be emitted on either/both frames --
            # the MODEL never is (HANDEDNESS RULE). `make_mask` above always builds the mask
            # directly in the winning/corrected-hand frame (matches the never-flipped model and
            # the flipped map); that is preserved verbatim as the "winning-hand" mask. Mirroring
            # a COPY back onto the original --map grid (fast local-refine workflow: refine your
            # existing original-frame particles/volume directly with a frame-matched mask, and
            # only mirror the final deliverable map, if ever) is additive/optional.
            winning_hand_mask = {
                k: mask_report.get(k) for k in ("output_soft_mask", "output_hard_mask") if mask_report.get(k)
            }
            mask_report["winning_hand_mask"] = winning_hand_mask
            mask_report["winning_hand_mask_note"] = (
                f"Mask built directly in the WINNING/corrected-hand frame ({refinement_map}), "
                "matching the never-flipped model and the flipped map."
            )
            if args.emit_mask_frame in {"input", "both"}:
                mask_report = flip_mask_outputs_to_original_grid(mask_report, mask_dir, args.mirror_axis)
                mask_report["input_grid_mask"] = mask_report.get("flipped_back_to_original_grid", {})
                mask_report["input_grid_mask_note"] = (
                    "Input map was WRONG-HAND; this copy is the mask MIRRORED BACK onto the ORIGINAL "
                    f"--map file's grid (input map: {args.map}), for a caller refining original-frame "
                    "particles/volumes directly. The atomic model itself was never mirrored either way."
                )
            if args.emit_mask_frame == "winning":
                mask_report["mask_grid"] = "winning_corrected_hand_grid_only"
            elif args.emit_mask_frame == "input":
                mask_report["mask_grid"] = "original_input_grid_only"
            else:
                mask_report["mask_grid"] = "both_winning_hand_and_original_input_grid"
        else:
            mask_report["mask_grid"] = "original_input_grid"
        report["mask_report"] = mask_report

    report["finished_at"] = now()
    report["status"] = "completed"
    (out_dir / "multichain_autofit_report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    write_summary(report, out_dir)
    log(f"DONE: {out_dir}")
    log(f"Summary: {out_dir / 'multichain_autofit_summary.txt'}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PipelineError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(2)
