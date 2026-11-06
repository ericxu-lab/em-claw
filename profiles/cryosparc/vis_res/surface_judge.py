#!/usr/bin/env python
"""
surface_judge.py — the SURFACE half of the vis_res resolution check, as one reusable script.

WHY THIS EXISTS
  Judging a map's resolution from flat slices alone sometimes UNDER-reads it, and the
  under-read may happen exactly where it matters (a real 3-4 A map, a membrane protein in a
  micelle). Two habits fix that, and this script encodes both:

    1. LADDER THE UNSHARPENED MAP FIRST to find the level range where the FOLD reads —
       smooth continuous tubes, not a bloated blob and not yet breaking up. On a membrane
       protein that range sits FAR deeper than the molecular-volume anchor suggests
       (~0.30x-0.15x), because the anchor counts protein only while the map also holds the
       detergent belt.
    2. THEN RENDER BOTH MAPS AT THE SAME ENCLOSED VOLUMES, side by side. Unsharpened gives
       the FOLD (are helices resolved, can you follow the backbone). Sharpened gives the
       DETAIL (side-chain knobs, helix pitch) plus amplified noise — which is readable only
       BECAUSE the unsharpened panel next to it vouches for the fold. Matching on enclosed
       volume (not sigma, not absolute level) is what makes the two columns comparable:
       sharpening changes the histogram.

  Read the FSC from the refine job — never recompute one. `fsc_resolution_from_job` returns
  every curve (nomask / loosemask / tightmask / noisesub); `mask_effect_A` = nomask minus
  corrected answers "is this number mask-inflated?" on its own.

  Calibration (hTA1 J548, 588k particles, box256 @1.069 A/px):
    mask effect 0.26 A (NOT inflated) · unsharp fold at 0.30x-0.16x · sharp knobs at the same
    volumes · vision ~3.8 A vs FSC 3.65 A -> CONSISTENT. Slice-only had called it ~5.5 A
    SUSPICIOUS; the paired surface is what corrected it.

USAGE
    # LOCAL / FOCUSED REFINE — pass the refinement's mask, or the read is not about the
    # region that was refined (added 2026-09-24):
    python surface_judge.py --project P5 --job J1363 --mass-kDa 144 \
           --mask <local_refine_mask>.mrc --mask-label 'receptor+Fv interface'

    # stage 1 — pull the FSC columns and ladder the unsharpened map
    python surface_judge.py --project P5 --job J548 --mass-kDa 144

    # READ the printed ladder sheet, pick the rows where the fold reads best, then:
    # stage 2 — both maps at those enclosed volumes, in one sheet
    python surface_judge.py --project P5 --job J548 --mass-kDa 144 --pair 0.30,0.22,0.16

Importable too:
    from surface_judge import surface_judge
    r = surface_judge(proj, 'J548', mass_kDa=144)                    # stage 1
    r = surface_judge(proj, 'J548', mass_kDa=144, pair=(0.30, 0.22, 0.16))   # stage 2
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
if str(HERE.parent) not in sys.path:
    sys.path.insert(0, str(HERE.parent))

import vis_res_utils as V


# Level search is COARSE-to-FINE. Guessing a narrow band and re-rendering it twice wastes
# renders and can miss the readable window entirely (J1078: two ~0.8x-0.1x ladders both landed
# in the wrong regime). Scan wide with a rough step FIRST -- one row per ~2x of enclosed volume
# across two orders of magnitude -- then tighten around whichever row looks closest.
COARSE_LADDER = (4.0, 2.0, 1.0, 0.5, 0.25, 0.12, 0.06, 0.03)


def fine_ladder(center: float, n: int = 5, spread: float = 2.2) -> tuple:
    """
    A TIGHT ladder bracketing `center` (a volume multiplier), geometrically spaced.
    spread = total factor covered (2.2 -> center/1.48 .. center*1.48 for n=5).
    Use after COARSE_LADDER has told you roughly where the fold reads.
    """
    import math
    lo = center / math.sqrt(spread)
    step = spread ** (1.0 / (n - 1))
    return tuple(round(lo * step ** i, 4) for i in range(n))


def final_volume_map(proj, job_uid: str, sharp: bool = True) -> Path:
    """Path to a refine job's FINAL-iteration volume map (sharpened by default)."""
    jdir = Path(proj.dir()) / job_uid
    pat = re.compile(rf'^{job_uid}_(\d+)_volume_map{"_sharp" if sharp else ""}\.mrc$')
    hits = [(int(m.group(1)), p) for p in jdir.iterdir()
            for m in [pat.match(p.name)] if m]
    if not hits:
        raise FileNotFoundError(f'no {"sharpened " if sharp else ""}volume map in {jdir}')
    return max(hits)[1]


def fsc_table(proj, job_uid: str) -> dict:
    """Every FSC curve the job stored, plus how much the mask bought. Never recomputed."""
    f = V.fsc_resolution_from_job(proj, job_uid)
    if not f:
        print(f'[surface_judge] no FSC asset for {job_uid} — read it from the job plot and pass by hand')
        return {}
    cols = f.get('res_by_column', {})
    print(f'\nFSC for {job_uid} (from {f["asset"]}):')
    for name in ('fsc_nomask', 'fsc_loosemask', 'fsc_tightmask', 'fsc_noisesub_raw', 'fsc_noisesub'):
        if name in cols:
            star = '   <- reported (corrected)' if name == 'fsc_noisesub' else ''
            print(f'   {name:18s} {cols[name]:6.2f} A{star}')
    if f.get('mask_effect_A') is not None:
        me = f['mask_effect_A']
        verdict = ('mask bought little -> the number is NOT mask-inflated'
                   if me <= 0.5 else
                   'the MASK did much of the work -> treat the reported number as inflated')
        print(f'   mask effect (nomask - corrected) = {me:+.2f} A  ->  {verdict}')
        print('   CAVEAT: on a FOCUSED/LOCAL refine, fsc_nomask covers density OUTSIDE the mask that '
              'the job never refined,\n           so the gap is inflated by construction — do not read '
              'it as "mask-inflated" there.')
    return f


def surface_judge(proj, job_uid: str, mass_kDa: float,
                  out_dir: str | Path | None = None,
                  ladder=None,
                  pair=None, size: int | None = None, datainfo_path: str | None = None,
                  render_host: str | None = None, render_ld_path: str | None = None,
                  mask=None, mask_label: str | None = None,
                  mask_threshold: float = 0.5) -> dict:
    """
    Stage 1 (pair=None): FSC table + UNSHARPENED ladder -> read the sheet, pick the range.
    Stage 2 (pair=(...)): both maps at those enclosed volumes in ONE sheet.

    mask / mask_label (ADDED 2026-09-24) — FOR A LOCAL REFINE, PASS THE REFINEMENT'S MASK.
    Only the masked region was refined; outside it the map is unrefined carry-over from the
    parent, so a whole-box surface read under-states the refinement and its display levels are
    chosen partly on density the job never touched. With a mask the default panel is cropped to
    the region and the verdict must be reported as belonging to THAT REGION. Applies to the
    default panel path; the --ladder/--pair search paths stay whole-box by design.
    """
    if datainfo_path:
        V.DATAINFO_PATH = datainfo_path
    # VIS_RES_RENDER_* live in ~/.bashrc, which non-interactive shells do NOT source, so pass
    # them through explicitly instead of hoping they are in the environment.
    import os
    render_host = render_host or os.environ.get('VIS_RES_RENDER_HOST')   # None = render locally
    render_ld_path = (render_ld_path or os.environ.get('VIS_RES_RENDER_LD_PATH')
                      or str(Path.home() / 'cxlibfix'))
    rkw = {'render_host': render_host, 'render_ld_path': render_ld_path}
    # Render size is per STAGE, and it is not cosmetic: at 600 px a broken-but-real fold reads as
    # "dust" (J1078 0.25x was dismissed that way, then read fine at 1400 px). Scan cheap, JUDGE BIG.
    if size is None:
        size = 1400 if pair is not None else (600 if ladder is None else 900)
    print(f'[surface_judge] rendering on host={render_host} (ld_path={render_ld_path})')
    mrc = final_volume_map(proj, job_uid, sharp=True)
    out = Path(out_dir) if out_dir else Path(proj.dir()) / job_uid / f'{job_uid}_surface_judge'
    out.mkdir(parents=True, exist_ok=True)
    print(f'[surface_judge] {job_uid}: {mrc.name}  (mass {mass_kDa:g} kDa -> '
          f'{V.volume_for_mass_kDa(mass_kDa):.0f} A^3 molecular volume)')

    f = fsc_table(proj, job_uid)

    if pair is None and not ladder:
        # DEFAULT: the whole surface read in one call -- crop, auto-pick the two display
        # levels from the connectivity knee, render UNSHARP|SHARP at matched enclosed
        # volume with a scale bar. The manual ladder path below is for when you want to
        # see the search itself (pass --ladder / --fine-around).
        r = V.surface_panel(mrc, out_dir=out / 'panel', size=size or 1200,
                            mask=mask, mask_label=mask_label,
                            mask_threshold=mask_threshold, **rkw)
        if r.get('scope') == 'region':
            print(f'\n   *** REGION-SCOPED READ: {r["region"]["name"]} '
                  f'({r["region"]["region_volume_A3"]:.0f} A^3). {r["scope_note"]}')
        print(f'\nSURFACE PANEL — READ: {r["sheet"]}\n'
              '   rows = the two auto-picked display levels; cols = UNSHARP | SHARP x front/side/top.\n'
              '   UNSHARP = the FOLD (continuous tubes = helices resolved, backbone followable).\n'
              '   SHARP at the SAME enclosed volume = the DETAIL (knobs ON those tubes = side chains).\n'
              '   MEASURE against the 10 A bar: ~5 A tubes + 2-3 A knobs + ~10 A spacing -> 3-3.5 A;\n'
              '   ~8 A smooth tubes with no knobs -> ~5 A.\n'
              '   Then close with vis_res_utils.assess(..., surface_checked=True, fsc_trustworthy=...).')
        return {'stage': 'panel', 'fsc': f, 'sheet': r['sheet'], 'mrc': str(mrc),
                'levels_unsharp': r['levels_unsharp'], 'levels_sharp': r['levels_sharp'],
                'enclosed_A3': r['enclosed_A3'], 'px_per_A': r['px_per_A']}

    if pair is None:
        ladder = tuple(ladder) if ladder else COARSE_LADDER
        tag = 'coarse' if ladder == COARSE_LADDER else 'fine'
        print(f'[surface_judge] {tag} ladder: {ladder}')
        r = V.render_map_surface(mrc, out_dir=out / f'ladder_unsharp_{tag}', mass_kDa=mass_kDa,
                                 volume_ladder=ladder, size=size, prefer_unsharpened=True,
                                 **rkw)
        print(f'\nSTEP 1 ({tag}, {size} px) — a SCAN, not a verdict: never call a level unreadable\n'
              f'   from these thumbnails — re-render the chosen row large (--pair does, at 1400 px).\n'
              f'   READ: {r["surface_sheet"]}\n'
              '   Find the row where the FOLD reads best: helices as smooth continuous tubes — not a\n'
              '   bloated blob (level too low), not yet breaking into fragments/coral (level too high).\n'
              '   A membrane protein reads DEEP: the mass anchor counts protein only, so the micelle\n'
              '   inflates every level above the readable window.\n'
              '   THEN TIGHTEN: re-run with --ladder $(fine_ladder(<best multiplier>)) to bracket it,\n'
              '   e.g. --ladder 0.20,0.26,0.35,0.46,0.61 around 0.35 — and only then --pair.')
        return {'stage': 'ladder', 'fsc': f, 'ladder_sheet': r['surface_sheet'], 'mrc': str(mrc)}

    r = V.render_surface_pair(mrc, out_dir=out / 'pair', mass_kDa=mass_kDa,
                              volume_ladder=tuple(pair), size=size, **rkw)
    print(f'\nSTEP 2 — READ: {r["sheet"]}\n'
          '   rows = enclosed volume, columns = UNSHARP | SHARP x front/side/top.\n'
          '   UNSHARP = the FOLD (helices resolved? backbone followable?).\n'
          '   SHARP at the SAME volume = the DETAIL (side-chain knobs along the tubes? helix pitch?\n'
          '   aromatic-ring holes?). Knobs count only where they sit ON the fold the unsharp panel shows.\n'
          '   Then estimate the resolution from BOTH and close with vis_res_utils.assess(\n'
          '        secondary_structure=..., vision_estimate_A=..., fsc_res_A=<corrected FSC>,\n'
          '        surface_checked=True, fsc_trustworthy=...).')
    return {'stage': 'pair', 'fsc': f, 'sheet': r['sheet'], 'mrc': str(mrc),
            'levels_unsharp': r['levels_unsharp'], 'levels_sharp': r['levels_sharp']}


def _floats(s):
    return tuple(float(x) for x in s.replace(' ', '').split(','))


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--project', required=True, help='CryoSPARC project UID, e.g. P5')
    ap.add_argument('--job', required=True, help='refine job UID, e.g. J548')
    ap.add_argument('--mass-kDa', type=float, required=True,
                    help='PROTEIN mass of the complex (exclude detergent/nanodisc)')
    ap.add_argument('--pair', type=_floats, default=None,
                    help='stage 2: volume multipliers to render on both maps, e.g. 0.30,0.22,0.16')
    ap.add_argument('--ladder', type=_floats, default=None,
                    help='stage-1 ladder multipliers (default: COARSE_LADDER, a wide rough scan)')
    ap.add_argument('--fine-around', type=float, default=None,
                    help='stage 1b: tight ladder bracketing this multiplier (from the coarse scan)')
    ap.add_argument('--out', default=None)
    ap.add_argument('--size', type=int, default=900)
    ap.add_argument('--mask', default=None,
                    help='FOR A LOCAL REFINE: the refinement mask (.mrc, same grid as the map). '
                         'Crops the panel to the refined REGION — outside it a local refine '
                         'leaves unrefined carry-over, so a whole-box read under-states it.')
    ap.add_argument('--mask-label', default=None,
                    help='name of the region the mask covers, e.g. "receptor+Fv interface"')
    ap.add_argument('--datainfo', default=None, help="active project's base_datainfo.yaml (ChimeraX path)")
    ap.add_argument('--host', default=None, help='render host (default $VIS_RES_RENDER_HOST; unset renders locally)')
    ap.add_argument('--ld-path', default=None, help='LD_LIBRARY_PATH prepended on the render host')
    a = ap.parse_args()

    from cs_connect import get_cs
    proj = get_cs().find_project(a.project)
    ladder = fine_ladder(a.fine_around) if a.fine_around else a.ladder
    surface_judge(proj, a.job, a.mass_kDa, out_dir=a.out, ladder=ladder,
                  pair=a.pair, size=a.size, datainfo_path=a.datainfo,
                  render_host=a.host, render_ld_path=a.ld_path,
                  mask=a.mask, mask_label=a.mask_label)
