"""Build the protein-FOCUSED core mask for the phase-2 local refinement.

Emits a HARD BINARY mask MRC on the input map's grid. That file is then imported with
`import_volumes(volume_out_name="mask", volume_psize=<grid psize>)` and softened with
`volume_tools(volume_out_type="mask", threshold=0.5, dilator=3, pad=<5|15>)` -- i.e. CryoSPARC's
own cosine padding does the soft edge, so the pad A/B is measured in the precedents' own units.

WHY NOT JUST THRESHOLD THE MAP
------------------------------
Measured on J1032 (`RESULT_threshold_sweep_J1032.md`): there is NO density threshold that isolates
the ordered core. At the level enclosing ~63 kDa, 61% of enclosed voxels are still beyond r=30 A and
the region breaks into 69 fragments; local texture inside never exceeds 0.49 against 0.73 for
ordered protein. Protein and detergent overlap in density MAGNITUDE -- they differ in TEXTURE.
Both precedents (P514/W2, P378) could hand-threshold because their maps were already at ~3.0-3.1 A.

So this script tries three methods in order and reports which one it used. The method actually used
MUST be recorded in the phase log -- methods 2 and 3 are deviations from the precedent recipe and
must not be reported as "the P514 mask".

  METHOD 1 (precedent)  density threshold, if one qualifies on BOTH texture and radius
  METHOD 2 (preferred fallback)  TEXTURE-derived: local sigma/mean is what physically separates
                        ordered protein from disordered detergent; threshold that volume instead
  METHOD 3 (last resort)  geometric ellipsoid fitted by PCA to the texture-selected core

USAGE
    python build_focus_mask.py <map.mrc> --out <mask_hard.mrc> [--core-radius-A 40]
"""
import argparse
import numpy as np
import mrcfile
from scipy import ndimage

PROTEIN_DENSITY_DA_PER_A3 = 0.81
TEXTURE_ORDERED = 0.50      # midpoint of measured 0.73 (protein) vs 0.30 (detergent)


def local_texture(vol, size=3, ref=None):
    """local sigma / local mean, with a FLOOR on the denominator.

    Without the floor this explodes wherever the local mean crosses zero -- i.e. all over the
    solvent boundary -- and the "ordered" selection then tracks the map's zero crossings instead
    of real structure. (Observed directly: mean texture 3.6 on J1032, against 0.73 for genuine
    ordered protein, and the mask ran out to the hard radius bound at 2x the target mass.)
    The floor is a fixed fraction of the map's own high-density scale, so texture stays on the
    same 0.30 (detergent) / 0.73 (protein) calibration the radial profile established.
    """
    v = vol.astype(np.float32)
    mean = ndimage.uniform_filter(v, size=size)
    sq = ndimage.uniform_filter(v * v, size=size)
    sigma = np.sqrt(np.maximum(sq - mean * mean, 0.0))
    scale = float(np.percentile(v, 99.9)) if ref is None else float(ref)
    floor = 0.10 * max(scale, 1e-6)
    return sigma / np.maximum(mean, floor)


def largest_component(mask):
    lab, n = ndimage.label(mask)
    if n == 0:
        return mask, 0, 0.0
    sizes = np.bincount(lab.ravel())[1:]
    keep = int(np.argmax(sizes)) + 1
    frac = sizes.max() / mask.sum()
    return lab == keep, n, float(frac)


def report(name, m, vx, rad_A, tex):
    nv = int(m.sum())
    if nv == 0:
        print(f"  {name}: EMPTY")
        return None
    v3 = nv * vx ** 3
    _, nfrag, frac = largest_component(m)
    print(f"  {name}: {v3:,.0f} A^3 = {v3*PROTEIN_DENSITY_DA_PER_A3/1000:.1f} kDa | "
          f"texture {tex[m].mean():.3f} | {100*np.mean(rad_A[m] > 30):.1f}% beyond r=30 A | "
          f"{nfrag} frags, {100*frac:.1f}% in largest | max radius {rad_A[m].max():.1f} A")
    return dict(vol=v3, tex=float(tex[m].mean()), out=float(np.mean(rad_A[m] > 30)),
                nfrag=nfrag, frac=frac)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mrc")
    ap.add_argument("--out", required=True)
    ap.add_argument("--core-radius-A", type=float, default=40.0,
                    help="hard outer bound: nothing beyond this radius can enter the mask")
    ap.add_argument("--expect-kda", type=float, default=53.0)
    args = ap.parse_args()

    with mrcfile.open(args.mrc, permissive=True) as m:
        vol = np.asarray(m.data, dtype=np.float32)
        vx = float(m.voxel_size.x)
    n = vol.shape[0]

    hi = vol > (vol.mean() + 3 * vol.std())
    zz, yy, xx = np.nonzero(hi)
    w = vol[hi]
    com = np.array([np.average(zz, weights=w), np.average(yy, weights=w), np.average(xx, weights=w)])
    gz, gy, gx = np.ogrid[:n, :n, :n]
    rad_A = np.sqrt((gz - com[0]) ** 2 + (gy - com[1]) ** 2 + (gx - com[2]) ** 2) * vx

    tex = local_texture(vol)
    solvent = vol > (vol.mean() + 1.0 * vol.std())
    within = rad_A <= args.core_radius_A
    target_A3 = args.expect_kda * 1000.0 / PROTEIN_DENSITY_DA_PER_A3

    print(f"map {args.mrc}\nbox {n}, voxel {vx:.4f} A, com ({com[0]:.1f},{com[1]:.1f},{com[2]:.1f})")
    print(f"target ordered mass {args.expect_kda:.0f} kDa = {target_A3:,.0f} A^3; "
          f"hard outer bound r={args.core_radius_A:.0f} A\n")

    chosen, method = None, None

    # ---- METHOD 1: density threshold (the precedent recipe) -------------------
    print("METHOD 1 - density threshold (precedent P514/P378):")
    vmax = float(vol.max())
    best1 = None
    for t in np.geomspace(max(vmax * 0.02, 1e-6), vmax * 0.9, 40):
        m = (vol > t) & within
        if m.sum() < 200:
            continue
        _, _, frac = largest_component(m)
        if (tex[m].mean() >= TEXTURE_ORDERED and np.mean(rad_A[m] > 30) < 0.35
                and frac >= 0.80):
            v3 = m.sum() * vx ** 3
            if best1 is None or abs(v3 - target_A3) < abs(best1[1] - target_A3):
                best1 = (t, v3, m)
    if best1 is not None:
        print(f"  QUALIFIES at threshold {best1[0]:.4f}")
        chosen = best1[2]
        report("method1", chosen, vx, rad_A, tex)
        method = f"METHOD 1 (density threshold {best1[0]:.4f}) - matches the precedent recipe"
    else:
        print("  no threshold qualifies on texture + radius + coherence -> falling through")

    # ---- METHOD 2: texture-derived -------------------------------------------
    if chosen is None:
        print("\nMETHOD 2 - texture-derived (local sigma/mean):")
        m2 = (tex >= TEXTURE_ORDERED) & solvent & within
        if m2.sum() > 200:
            m2 = ndimage.binary_closing(m2, np.ones((3, 3, 3)))
            m2, _, _ = largest_component(m2)
            m2 = ndimage.binary_fill_holes(m2)
            st = report("method2", m2, vx, rad_A, tex)
            if st and st["vol"] > 0.25 * target_A3 and st["frac"] >= 0.80:
                chosen, method = m2, "METHOD 2 (texture-derived) - DEVIATION from the precedent recipe"
            else:
                print("  texture mask too small or incoherent -> falling through")
        else:
            print("  too few ordered voxels -> falling through")

    # ---- METHOD 3: geometric ellipsoid ---------------------------------------
    if chosen is None:
        print("\nMETHOD 3 - geometric ellipsoid fitted to the ordered core:")
        seed = (tex >= TEXTURE_ORDERED) & solvent & within
        pts = np.array(np.nonzero(seed), dtype=np.float64).T
        if len(pts) < 100:
            seed = solvent & (rad_A <= 30.0)
            pts = np.array(np.nonzero(seed), dtype=np.float64).T
        c = pts.mean(axis=0)
        cov = np.cov((pts - c).T)
        evals, evecs = np.linalg.eigh(cov)
        semi = 2.0 * np.sqrt(np.maximum(evals, 1e-6))          # ~2 sigma along each principal axis
        semi = np.clip(semi, 8.0 / vx, args.core_radius_A / vx)
        print(f"  semi-axes (A): {semi[0]*vx:.1f} / {semi[1]*vx:.1f} / {semi[2]*vx:.1f}")
        grid = np.stack(np.meshgrid(np.arange(n), np.arange(n), np.arange(n), indexing="ij"), -1)
        d = (grid.reshape(-1, 3) - c) @ evecs
        ell = ((d / semi) ** 2).sum(axis=1).reshape(n, n, n) <= 1.0
        chosen = ell & within
        report("method3", chosen, vx, rad_A, tex)
        method = "METHOD 3 (geometric ellipsoid) - DEVIATION, no data-derived mask was possible"

    out = chosen.astype(np.float32)
    with mrcfile.new(args.out, overwrite=True) as f:
        f.set_data(out)
        f.voxel_size = vx
    print(f"\nUSED: {method}")
    print(f"wrote HARD BINARY mask -> {args.out}  (grid {n}^3 @ {vx:.4f} A/px)")
    print("next: import_volumes(volume_blob_path=<this>, volume_psize=%.4f, "
          "volume_out_name='mask')" % vx)
    print("      volume_tools(volume_out_type='mask', threshold=0.5, dilator=3, pad=5)   # arm 1")
    print("      volume_tools(volume_out_type='mask', threshold=0.5, dilator=3, pad=15)  # arm 2")
    print("\nRECORD THE METHOD USED IN THE PHASE LOG -- methods 2 and 3 are NOT the precedent recipe.")


if __name__ == "__main__":
    main()
