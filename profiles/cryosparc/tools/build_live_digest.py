#!/usr/bin/env python3
"""Distill a LIVE CryoSPARC project (one or more workspaces) into the same compact digest
schema `build_history_digest.py` emits for the offline history bank.

Why this exists: subP is asked to mimic "a project we have run" (e.g. zll_260130), which lives
on the live server, not in the bank. Crawling a few-hundred-job DAG with an LLM is expensive and
lossy; this does it deterministically and hands subP the identical read surface it would get from
a bank digest — plus three live-only signals the bank cannot carry:

  * `status` per step        -> failed/killed jobs are visible, not silently mixed in
  * `workspace` per step     -> which arm of a multi-workspace project a job belongs to
  * `final`/`ancestor` flags -> CryoSPARC's own record of which jobs fed the final result
  * `title` per step         -> the human/subC label, which states the job's INTENT

Resolution comes from `vis_res.vis_res_utils.corrected_fsc_resolution` (the tested route: the
job doc does NOT carry the number; it is read from the job's gridfs FSC asset).

Usage:
    python tools/build_live_digest.py P5 --workspaces W3,W10,W11,W16,W19,W20,W22 \
        --out pipelines/_digests/zll_260130_digest.json --name zll_260130
    python tools/build_live_digest.py P5 --title-contains YOUR_WORKSPACE_TITLE --out ... --name ...
    python tools/build_live_digest.py P5 --workspaces W21 --no-fsc --out ...   # skip FSC reads
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, _ROOT)

from build_history_digest import KEY_TYPES, _is_env_field  # same env-path policy, same backbone types

# Job types that are pure bookkeeping / UI and add nothing to a pipeline recipe.
SKIP_TYPES = {"workspace", "instance_launch"}


def _particle_count(doc):
    n = 0
    for o in doc.get("output_result_groups") or []:
        if o.get("type") == "particle":
            n = max(n, o.get("num_items") or 0)
    return n or None


def _box_and_psize(doc):
    """Read box (pix) and pixel size (A) off any particle/volume output summary."""
    for o in doc.get("output_result_groups") or []:
        s = o.get("summary") or {}
        shp = s.get("blob/shape")
        box = shp[0] if isinstance(shp, list) and shp else None
        psize = s.get("blob/psize_A") or s.get("alignments3D/psize_A")
        if box or psize:
            return box, (round(psize, 4) if isinstance(psize, float) else psize)
    return None, None


def _inputs(doc):
    """{input_slot: [source_job_uid, ...]} — the data-flow wiring, same shape as the bank digest."""
    out = {}
    for g in doc.get("input_slot_groups") or []:
        slot = g.get("name") or g.get("type") or "in"
        for c in g.get("connections") or []:
            src = c.get("job_uid")
            if src:
                out.setdefault(slot, [])
                if src not in out[slot]:
                    out[slot].append(src)
    return {k: sorted(v, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0) for k, v in out.items()}


def _params(doc):
    """params_spec holds ONLY the knobs explicitly set away from default — exactly the
    'non-default params, verbatim' the bank digest keeps."""
    return {k: v.get("value") for k, v in (doc.get("params_spec") or {}).items()}


def _resolver(project_uid, enabled):
    """Return fn(job_uid) -> corrected gold-standard FSC resolution (A) or None."""
    if not enabled:
        return lambda uid: None
    try:
        from cs_connect import get_cs
        from vis_res import vis_res_utils as V
        proj = get_cs().find_project(project_uid)
    except Exception as ex:  # noqa: BLE001 - degrade to a numeric-free digest, never crash
        print(f"[warn] FSC reads unavailable ({ex!r}); continuing without res_A", file=sys.stderr)
        return lambda uid: None

    cache = {}

    def get(uid):
        if uid not in cache:
            try:
                r = V.corrected_fsc_resolution(proj, uid)
                cache[uid] = round(float(r), 3) if r else None
            except Exception:  # noqa: BLE001 - a job with no FSC asset is normal
                cache[uid] = None
        return cache[uid]

    return get


def build_live_digest(project_uid, workspaces=None, title_contains=None, want_fsc=True, name=None):
    from cs_connect import get_cs

    cs = get_cs()
    all_jobs = cs.cli.list_jobs(project_uid=project_uid)

    ws_titles = {w["uid"]: w.get("title") for w in cs.cli.list_workspaces(project_uid=project_uid)}
    keep_ws = set(workspaces or [])
    if title_contains:
        keep_ws |= {u for u, t in ws_titles.items() if t and title_contains in t}
    if not keep_ws:
        keep_ws = set(ws_titles)

    jobs = {}
    for j in all_jobs:
        if j.get("deleted") or j.get("job_type") in SKIP_TYPES:
            continue
        if keep_ws & set(j.get("workspace_uids") or []):
            jobs[j["uid"]] = j

    res_of = _resolver(project_uid, want_fsc)

    steps, type_counts, env_seen, parents = {}, {}, set(), {}
    for uid, doc in jobs.items():
        jt = doc.get("job_type")
        type_counts[jt] = type_counts.get(jt, 0) + 1
        rec = {"type": jt, "status": doc.get("status")}
        ws = [w for w in (doc.get("workspace_uids") or []) if w in keep_ws]
        if ws:
            rec["workspace"] = ws[0] if len(ws) == 1 else ws
        title = (doc.get("title") or "").strip()
        if title and title != jt:
            rec["title"] = title  # subC/human label: states the job's INTENT
        p = _params(doc)
        if p:
            rec["params"] = p
            ef = [k for k, v in p.items() if _is_env_field(k, v)]
            if ef:
                rec["env_fields"] = ef
                env_seen.update(ef)
        wiring = _inputs(doc)
        if wiring:
            rec["in"] = wiring
            parents[uid] = sorted({s for v in wiring.values() for s in v})
        pc = _particle_count(doc)
        if pc:
            rec["n_particle"] = pc
        box, psize = _box_and_psize(doc)
        if box:
            rec["box_pix"] = box
        if doc.get("is_final_result"):
            rec["final"] = True
        if doc.get("is_ancestor_of_final_result"):
            rec["ancestor_of_final"] = True
        if doc.get("status") == "completed" and jt in KEY_TYPES:
            r = res_of(uid)
            if r:
                rec["res_A"] = r
        steps[uid] = rec

    # ---- best result + backbone (lineage of the best map, key jobs, chronological) ----
    scored = [(s["res_A"], u) for u, s in steps.items() if s.get("res_A")]
    best_uid = min(scored)[1] if scored else None
    backbone = []
    if best_uid:
        seen, stack = {best_uid}, [best_uid]
        while stack:
            for par in parents.get(stack.pop(), []):
                if par in steps and par not in seen:
                    seen.add(par)
                    stack.append(par)
        chain = sorted((u for u in seen if steps[u]["type"] in KEY_TYPES),
                       key=lambda u: int(u[1:]))
        prev = None
        for u in chain:
            s = steps[u]
            row = {"uid": u, "type": s["type"]}
            if s.get("res_A") is not None:
                row["res_A"] = s["res_A"]
                if prev is not None:
                    row["delta_A"] = round(s["res_A"] - prev, 3)
                prev = s["res_A"]
            for k in ("n_particle", "params", "title"):
                if s.get(k):
                    row[k] = s[k]
            backbone.append(row)
        contributing = sorted(seen, key=lambda u: int(u[1:]))
    else:
        contributing = []

    # ---- optics from the import job ----
    optics = {}
    for uid, s in sorted(steps.items(), key=lambda x: int(x[0][1:])):
        if s["type"] in ("import_micrographs", "import_movies"):
            for k in ("psize_A", "accel_kv", "cs_mm", "total_dose_e_per_A2"):
                v = (s.get("params") or {}).get(k)
                if v is not None:
                    optics[k] = round(v, 4) if isinstance(v, float) else v
            break
    if best_uid and steps[best_uid].get("box_pix"):
        optics["final_box_pix"] = steps[best_uid]["box_pix"]

    picker_d = sorted({v for s in steps.values() if "picker" in s["type"]
                       for k, v in (s.get("params") or {}).items()
                       if k in ("diameter", "diameter_max", "par_diam") and v})

    return {
        "schema": "cryoem-live-digest/v1",
        "compatible_with": "cryoem-history-digest/v1",
        "source": "live",
        "name": name or (title_contains or project_uid),
        "project_uid": project_uid,
        "workspaces": {u: ws_titles.get(u) for u in sorted(keep_ws, key=lambda x: int(x[1:]))
                       if u in {w for j in jobs.values() for w in (j.get("workspace_uids") or [])}},
        "cs_version": next((j.get("version") for j in jobs.values() if j.get("version")), None),
        "optics": optics,
        "particle": {"picker_diameter_A": picker_d or None},
        "result": {
            "best_uid": best_uid,
            "best_job_type": steps[best_uid]["type"] if best_uid else None,
            "best_res_A": steps[best_uid]["res_A"] if best_uid else None,
            "n_particles": steps[best_uid].get("n_particle") if best_uid else None,
            "n_scored": len(scored),
            "note": "best_res_A is the job-REPORTED corrected gold-standard FSC. It is NOT "
                    "validated — run vis_res on the map before trusting it (see CLAUDE.md step 9).",
        },
        "contributing": contributing,
        "job_type_counts": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
        "status_counts": {k: sum(1 for s in steps.values() if s["status"] == k)
                          for k in sorted({s["status"] for s in steps.values() if s.get("status")})},
        "param_policy": {
            "note": "steps[].params keeps ALL non-default knobs verbatim (learn from these). "
                    "Names in steps[].env_fields are environment-specific paths from the SOURCE "
                    "project: re-fill from the working environment, never copy or omit. Confirm "
                    "the full required-param set per job via MCP validate_params before running.",
            "env_fields_seen": sorted(env_seen),
        },
        "backbone": backbone,
        "steps": steps,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("project_uid", help="e.g. P5")
    ap.add_argument("--workspaces", help="comma-separated, e.g. W3,W10,W11")
    ap.add_argument("--title-contains", help="select workspaces whose title contains this string")
    ap.add_argument("--name", help="short name for the digest (e.g. zll_260130)")
    ap.add_argument("--no-fsc", action="store_true", help="skip gridfs FSC reads (fast, no res_A)")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    d = build_live_digest(
        a.project_uid,
        workspaces=[w.strip() for w in a.workspaces.split(",")] if a.workspaces else None,
        title_contains=a.title_contains,
        want_fsc=not a.no_fsc,
        name=a.name,
    )
    os.makedirs(os.path.dirname(os.path.abspath(a.out)), exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(d, fh, separators=(",", ":"))
    r = d["result"]
    print(f"wrote {a.out} ({os.path.getsize(a.out)/1024:.1f} KB)")
    print(f"  workspaces : {d['workspaces']}")
    print(f"  jobs       : {len(d['steps'])}  ({d['status_counts']})")
    print(f"  best       : {r['best_uid']} {r['best_job_type']} {r['best_res_A']} A "
          f"({r['n_particles']} ptcls)")
    print(f"  contributing lineage: {len(d['contributing'])} jobs; backbone {len(d['backbone'])} steps")


if __name__ == "__main__":
    main()
