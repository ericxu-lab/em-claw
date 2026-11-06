#!/usr/bin/env python3
"""Make a model-derived soft mask on the original cryo-EM map grid.

This is intended as the downstream mask/threshold step after rigid_flex_dock.py.
It reads a fitted PDB/mmCIF, selects optional chains and residue-number range,
projects an atom mask back onto the input MRC volume grid, softens/dilates it,
and estimates a local density threshold from the selected atom/model volume.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

import gemmi
import mrcfile
import numpy as np
from scipy import ndimage


VDW_RADII_A = {
    "H": 1.20,
    "C": 1.70,
    "N": 1.55,
    "O": 1.52,
    "S": 1.80,
    "P": 1.80,
    "F": 1.47,
    "CL": 1.75,
    "BR": 1.85,
    "I": 1.98,
    "MG": 1.73,
    "ZN": 1.39,
    "CA": 2.31,
    "NA": 2.27,
    "K": 2.75,
}

STANDARD_AA = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
    "MSE", "SEC", "PYL",
}


@dataclass
class AtomRec:
    x: float
    y: float
    z: float
    element: str
    chain: str
    resnum: int
    resname: str


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=(
            "Generate a fitted-model-derived soft mask projected onto the original "
            "MRC volume grid, and estimate a local density threshold from model/atom volume."
        )
    )
    p.add_argument("--map", required=True, help="Original/reference MRC map to define output grid and density values")
    p.add_argument("--model", required=True, help="Fitted PDB/mmCIF model in the same coordinates as --map")
    p.add_argument("--out-dir", required=True, help="Output directory")
    p.add_argument("--out-prefix", default="model_soft_mask", help="Output filename prefix")
    p.add_argument("--chains", default="all", help="Comma-separated chain IDs to include, or 'all' [default: all]")
    p.add_argument("--seq-from", type=int, default=None, help="First residue sequence number to include [default: all]")
    p.add_argument("--seq-to", type=int, default=None, help="Last residue sequence number to include [default: all]")
    p.add_argument("--include-hydrogen", action="store_true", help="Include hydrogens when building the atom mask")
    p.add_argument("--mask-radius-A", type=float, default=3.0, help="Hard atom-mask radius around each selected atom in Angstrom [default: 3.0]")
    p.add_argument("--mask-dilate-A", type=float, default=6.0, help="Additional full-weight dilation outside hard atom mask in Angstrom [default: 6.0]")
    p.add_argument("--mask-soften-A", type=float, default=6.0, help="Cosine soft edge width in Angstrom [default: 6.0]")
    p.add_argument("--mask-level-for-threshold", type=float, default=0.5, help="Use voxels with soft mask > this level as local threshold-search region [default: 0.5]")
    p.add_argument(
        "--threshold-volume-source",
        choices=["atom-vdw", "atom-mask", "residues"],
        default="atom-vdw",
        help=(
            "Volume used for density-threshold estimate. 'atom-vdw' sums selected atoms' "
            "van der Waals sphere volumes; 'atom-mask' uses voxels inside --mask-radius-A atom mask; "
            "'residues' uses residue_count*135 A^3 [default: atom-vdw]"
        ),
    )
    p.add_argument("--threshold-search", choices=["mask-core", "whole-map"], default="mask-core", help="Where to sort map voxels for threshold [default: mask-core]")
    p.add_argument("--write-hard-mask", action="store_true", help="Also write the hard atom mask MRC")
    p.add_argument("--overwrite", action="store_true", help="Overwrite existing output files")
    p.add_argument(
        "--reference-mask", default=None,
        help=(
            "OPTIONAL sanity-check-only MRC mask on the SAME grid (e.g. an independent "
            "density-segmented lobe/domain boundary). If given, this script reports what "
            "fraction of the new atom-derived mask's volume falls OUTSIDE it -- purely a "
            "DIAGNOSTIC printed to the report, never applied to the output. Do NOT "
            "auto-intersect the output with this reference: an accepted, refined model's "
            "own shape is the primary evidence for where the mask should be; a big "
            "reported fraction is a prompt to LOOK (is the excess a genuine, refined "
            "feature like an interface-proximal loop, or a real placement problem?), not "
            "an instruction to blindly clip. See complex-map-dock-refine skill's "
            "acceptance-gates.md 'Mask built from the ACCEPTED, refined model' row."
        ),
    )
    return p.parse_args()


def chain_allowed(chain_id: str, requested: Optional[set[str]]) -> bool:
    return requested is None or chain_id in requested


def load_atoms(model_path: Path, chains_arg: str, seq_from: Optional[int], seq_to: Optional[int], include_h: bool) -> tuple[list[AtomRec], set[tuple[str, int, str]], dict]:
    st = gemmi.read_structure(str(model_path))
    st.setup_entities()
    requested_chains = None if chains_arg.lower() == "all" else {c.strip() for c in chains_arg.split(",") if c.strip()}
    atoms: list[AtomRec] = []
    residues: set[tuple[str, int, str]] = set()
    chains_seen: set[str] = set()
    chains_used: set[str] = set()

    for model in st:
        for chain in model:
            chain_id = chain.name
            chains_seen.add(chain_id)
            if not chain_allowed(chain_id, requested_chains):
                continue
            for res in chain:
                resnum = int(res.seqid.num) if res.seqid.num is not None else 0
                if seq_from is not None and resnum < seq_from:
                    continue
                if seq_to is not None and resnum > seq_to:
                    continue
                resname = res.name.strip().upper()
                residue_has_atom = False
                for atom in res:
                    if atom.element.name is None:
                        element = atom.name.strip()[0].upper()
                    else:
                        element = atom.element.name.upper()
                    if not include_h and element == "H":
                        continue
                    pos = atom.pos
                    atoms.append(AtomRec(pos.x, pos.y, pos.z, element, chain_id, resnum, resname))
                    residue_has_atom = True
                    chains_used.add(chain_id)
                if residue_has_atom and resname in STANDARD_AA:
                    residues.add((chain_id, resnum, resname))
    meta = {
        "chains_seen": sorted(chains_seen),
        "chains_requested": "all" if requested_chains is None else sorted(requested_chains),
        "chains_used": sorted(chains_used),
        "seq_from": seq_from,
        "seq_to": seq_to,
    }
    if not atoms:
        raise SystemExit(f"No atoms selected from {model_path}; check --chains/--seq-from/--seq-to")
    return atoms, residues, meta


def read_map(map_path: Path):
    with mrcfile.open(map_path, permissive=True) as m:
        data = np.asarray(m.data, dtype=np.float32).copy()
        voxel = (float(m.voxel_size.x), float(m.voxel_size.y), float(m.voxel_size.z))
        origin = (float(m.header.origin.x), float(m.header.origin.y), float(m.header.origin.z))
        nxstart = int(m.header.nxstart)
        nystart = int(m.header.nystart)
        nzstart = int(m.header.nzstart)
        # If origin is zero but starts are non-zero, use start*voxel as a practical fallback.
        if origin == (0.0, 0.0, 0.0) and (nxstart, nystart, nzstart) != (0, 0, 0):
            origin = (nxstart * voxel[0], nystart * voxel[1], nzstart * voxel[2])
        header = m.header.copy()
    return data, voxel, origin, header


def write_mrc(path: Path, data: np.ndarray, voxel: tuple[float, float, float], origin: tuple[float, float, float], overwrite: bool):
    if path.exists() and not overwrite:
        raise SystemExit(f"Output exists: {path}; pass --overwrite")
    with mrcfile.new(path, overwrite=True) as m:
        m.set_data(np.asarray(data, dtype=np.float32))
        m.voxel_size = voxel
        m.header.origin.x = origin[0]
        m.header.origin.y = origin[1]
        m.header.origin.z = origin[2]
        m.update_header_stats()


def atom_to_grid(atom: AtomRec, origin: tuple[float, float, float], voxel: tuple[float, float, float]) -> tuple[float, float, float]:
    ix = (atom.x - origin[0]) / voxel[0]
    iy = (atom.y - origin[1]) / voxel[1]
    iz = (atom.z - origin[2]) / voxel[2]
    return iz, iy, ix  # numpy/MRC data order is z,y,x


def build_hard_atom_mask(shape: tuple[int, int, int], atoms: list[AtomRec], voxel: tuple[float, float, float], origin: tuple[float, float, float], radius_A: float) -> tuple[np.ndarray, int, int]:
    nz, ny, nx = shape
    vz, vy, vx = voxel[2], voxel[1], voxel[0]
    hard = np.zeros(shape, dtype=bool)
    atoms_inside = 0
    atoms_outside = 0

    rz = int(math.ceil(radius_A / vz))
    ry = int(math.ceil(radius_A / vy))
    rx = int(math.ceil(radius_A / vx))

    for atom in atoms:
        fz, fy, fx = atom_to_grid(atom, origin, voxel)
        cz, cy, cx = int(round(fz)), int(round(fy)), int(round(fx))
        if cx < -rx or cx >= nx + rx or cy < -ry or cy >= ny + ry or cz < -rz or cz >= nz + rz:
            atoms_outside += 1
            continue
        atoms_inside += 1
        z0, z1 = max(0, cz - rz), min(nz, cz + rz + 1)
        y0, y1 = max(0, cy - ry), min(ny, cy + ry + 1)
        x0, x1 = max(0, cx - rx), min(nx, cx + rx + 1)
        zz = (np.arange(z0, z1, dtype=np.float32) - fz) * vz
        yy = (np.arange(y0, y1, dtype=np.float32) - fy) * vy
        xx = (np.arange(x0, x1, dtype=np.float32) - fx) * vx
        dist2 = zz[:, None, None] ** 2 + yy[None, :, None] ** 2 + xx[None, None, :] ** 2
        hard[z0:z1, y0:y1, x0:x1] |= dist2 <= radius_A ** 2
    return hard, atoms_inside, atoms_outside


def soften_mask(hard: np.ndarray, voxel: tuple[float, float, float], dilate_A: float, soften_A: float) -> np.ndarray:
    # distance from each outside voxel to nearest hard atom-mask voxel; inside is 0.
    sampling = (voxel[2], voxel[1], voxel[0])
    dist = ndimage.distance_transform_edt(~hard, sampling=sampling).astype(np.float32)
    mask = np.zeros(hard.shape, dtype=np.float32)
    inner = dist <= dilate_A
    mask[inner] = 1.0
    if soften_A > 0:
        edge = (dist > dilate_A) & (dist < dilate_A + soften_A)
        t = (dist[edge] - dilate_A) / soften_A
        mask[edge] = 0.5 * (1.0 + np.cos(np.pi * t))
    return mask


def percentile_threshold(values: np.ndarray, target_voxels: int) -> Optional[float]:
    finite = values[np.isfinite(values)]
    if finite.size == 0 or target_voxels <= 0:
        return None
    target_voxels = min(int(target_voxels), finite.size)
    kth = finite.size - target_voxels
    return float(np.partition(finite, kth)[kth])


def main() -> int:
    args = parse_args()
    map_path = Path(args.map).expanduser().resolve()
    model_path = Path(args.model).expanduser().resolve()
    out_dir = Path(args.out_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    data, voxel, origin, _header = read_map(map_path)
    voxel_volume = voxel[0] * voxel[1] * voxel[2]
    atoms, residues, sel_meta = load_atoms(model_path, args.chains, args.seq_from, args.seq_to, args.include_hydrogen)

    hard, atoms_inside, atoms_outside = build_hard_atom_mask(data.shape, atoms, voxel, origin, args.mask_radius_A)
    if hard.sum() == 0:
        raise SystemExit("Selected atoms produced an empty mask on this map grid; check model/map coordinates and origin")
    soft = soften_mask(hard, voxel, args.mask_dilate_A, args.mask_soften_A)

    hard_volume_A3 = float(hard.sum() * voxel_volume)
    soft_volume_A3 = float(soft.sum() * voxel_volume)
    residue_volume_A3 = float(len(residues) * 135.0)
    atom_vdw_volume_A3 = float(sum((4.0 / 3.0) * math.pi * (VDW_RADII_A.get(a.element.upper(), 1.7) ** 3) for a in atoms))

    if args.threshold_volume_source == "atom-vdw":
        threshold_volume_A3 = atom_vdw_volume_A3
    elif args.threshold_volume_source == "atom-mask":
        threshold_volume_A3 = hard_volume_A3
    else:
        threshold_volume_A3 = residue_volume_A3
    target_voxels = max(1, int(round(threshold_volume_A3 / voxel_volume)))

    if args.threshold_search == "mask-core":
        search_region = soft > args.mask_level_for_threshold
        threshold_region_name = f"soft_mask>{args.mask_level_for_threshold:g}"
    else:
        search_region = np.isfinite(data)
        threshold_region_name = "whole_map"
    region_voxels = int(np.count_nonzero(search_region))
    threshold = percentile_threshold(data[search_region], target_voxels)

    prefix = args.out_prefix
    soft_path = out_dir / f"{prefix}.soft_mask.mrc"
    hard_path = out_dir / f"{prefix}.hard_atom_mask.mrc"
    report_json = out_dir / f"{prefix}.report.json"
    report_txt = out_dir / f"{prefix}.report.txt"

    write_mrc(soft_path, soft, voxel, origin, args.overwrite)
    if args.write_hard_mask:
        write_mrc(hard_path, hard.astype(np.float32), voxel, origin, args.overwrite)

    multiplier_vs_residue = soft_volume_A3 / residue_volume_A3 if residue_volume_A3 > 0 else None
    multiplier_vs_hard = soft_volume_A3 / hard_volume_A3 if hard_volume_A3 > 0 else None

    reference_mask_check = None
    if args.reference_mask:
        ref_data, ref_voxel, ref_origin, _ = read_map(Path(args.reference_mask).expanduser().resolve())
        if ref_data.shape != soft.shape:
            reference_mask_check = {
                "reference_mask": str(Path(args.reference_mask).expanduser().resolve()),
                "error": f"grid mismatch: mask shape {soft.shape} vs reference shape {ref_data.shape} -- skipped",
            }
        else:
            sel = soft > 0.5
            ref_sel = ref_data > 0.5
            outside = int(np.count_nonzero(sel & ~ref_sel))
            total = int(np.count_nonzero(sel))
            frac_outside = (outside / total) if total else None
            reference_mask_check = {
                "reference_mask": str(Path(args.reference_mask).expanduser().resolve()),
                "note": ("DIAGNOSTIC ONLY -- NOT applied to the output mask. A nonzero fraction "
                         "is a prompt to visually check WHETHER the excess is genuine refined "
                         "structure (e.g. an interface-proximal loop) or a placement problem -- "
                         "it is not by itself grounds to intersect/clip this mask against the "
                         "reference. See acceptance-gates.md 'Mask built from the ACCEPTED, "
                         "refined model' row."),
                "mask_voxels_at_0.5": total,
                "mask_voxels_outside_reference_at_0.5": outside,
                "fraction_of_mask_outside_reference": frac_outside,
            }
            if frac_outside is not None and frac_outside > 0.05:
                print(f"NOTE: {frac_outside:.1%} of the new mask (at the 0.5 level) falls outside "
                      f"--reference-mask. This is a DIAGNOSTIC, not an automatic clip -- inspect "
                      f"before deciding whether to trim (see acceptance-gates.md).", file=sys.stderr)

    report = {
        "input_map": str(map_path),
        "input_model": str(model_path),
        "output_soft_mask": str(soft_path),
        "output_hard_mask": str(hard_path) if args.write_hard_mask else None,
        "map_shape_zyx": list(map(int, data.shape)),
        "voxel_size_A_xyz": list(voxel),
        "origin_xyz": list(origin),
        "selection": sel_meta,
        "selected_atoms": len(atoms),
        "selected_atoms_inside_grid": atoms_inside,
        "selected_atoms_outside_or_near_edge": atoms_outside,
        "selected_standard_residues": len(residues),
        "mask_parameters": {
            "mask_radius_A": args.mask_radius_A,
            "mask_dilate_A": args.mask_dilate_A,
            "mask_soften_A": args.mask_soften_A,
        },
        "volumes_A3": {
            "hard_atom_mask": hard_volume_A3,
            "atom_vdw_sum_no_overlap_correction": atom_vdw_volume_A3,
            "residue_estimate_135A3_per_residue": residue_volume_A3,
            "soft_mask_integral": soft_volume_A3,
            "soft_mask_multiplier_vs_residue_estimate": multiplier_vs_residue,
            "soft_mask_multiplier_vs_hard_atom_mask": multiplier_vs_hard,
        },
        "threshold_estimate": {
            "threshold_volume_source": args.threshold_volume_source,
            "target_volume_A3": threshold_volume_A3,
            "target_voxels": target_voxels,
            "threshold_search": args.threshold_search,
            "threshold_region": threshold_region_name,
            "threshold_region_voxels": region_voxels,
            "estimated_density_threshold": threshold,
        },
        "reference_mask_check": reference_mask_check,
    }
    with open(report_json, "w") as f:
        json.dump(report, f, indent=2)
    with open(report_txt, "w") as f:
        f.write("Model-derived soft mask report\n")
        f.write(f"Map: {map_path}\n")
        f.write(f"Model: {model_path}\n")
        f.write(f"Soft mask: {soft_path}\n")
        if args.write_hard_mask:
            f.write(f"Hard atom mask: {hard_path}\n")
        f.write(f"Grid shape z,y,x: {data.shape}\n")
        f.write(f"Voxel size x,y,z: {voxel}\n")
        f.write(f"Origin x,y,z: {origin}\n")
        f.write(f"Chains used: {','.join(sel_meta['chains_used'])}\n")
        f.write(f"Sequence range: {args.seq_from if args.seq_from is not None else 'all'} to {args.seq_to if args.seq_to is not None else 'all'}\n")
        f.write(f"Selected atoms: {len(atoms)}; inside/usable: {atoms_inside}; outside/edge skipped: {atoms_outside}\n")
        f.write(f"Selected standard residues: {len(residues)}\n")
        f.write(f"Hard atom-mask volume A^3: {hard_volume_A3:.3f}\n")
        f.write(f"Atom VDW summed volume A^3 (no overlap correction): {atom_vdw_volume_A3:.3f}\n")
        f.write(f"Residue volume estimate A^3: {residue_volume_A3:.3f}\n")
        f.write(f"Soft mask integral volume A^3: {soft_volume_A3:.3f}\n")
        if multiplier_vs_residue is not None:
            f.write(f"Soft-mask multiplier vs residue estimate: {multiplier_vs_residue:.3f}\n")
        if multiplier_vs_hard is not None:
            f.write(f"Soft-mask multiplier vs hard atom mask: {multiplier_vs_hard:.3f}\n")
        f.write(f"Threshold volume source: {args.threshold_volume_source}\n")
        f.write(f"Threshold target volume A^3: {threshold_volume_A3:.3f}\n")
        f.write(f"Threshold target voxels: {target_voxels}\n")
        f.write(f"Threshold search region: {threshold_region_name} ({region_voxels} voxels)\n")
        f.write(f"Estimated density threshold: {threshold}\n")
        f.write("\nNote: inspect the mask and threshold in ChimeraX/Coot before using for final local refinement.\n")

    print(f"Wrote soft mask: {soft_path}")
    if args.write_hard_mask:
        print(f"Wrote hard atom mask: {hard_path}")
    print(f"Wrote report: {report_txt}")
    print(f"Wrote JSON: {report_json}")
    print(f"Estimated density threshold: {threshold}")
    print(f"Soft mask volume multiplier vs residue estimate: {multiplier_vs_residue}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
