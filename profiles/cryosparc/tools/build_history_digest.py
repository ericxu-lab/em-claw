#!/usr/bin/env python3
"""Distill a heavy CryoSPARC history-bank group into a compact, analysis-friendly digest.

For one G* group it reads the heavy files (graph.json + workflow.json) and the PDF
manifest, and emits a single `<base>_digest.json` that captures, per step:
  job type -> crucial (non-default) params -> inputs -> outputs
  (particles kept, resolution, 2D selected/excluded class counts, result PDFs).
Plus a linearized `backbone` (the lineage of the best map, with resolution deltas) and
group-level metadata (optics, particle hint, best result) for similarity search.

The digest is ~1-2% the size of graph.json and contains everything subR/agentM need,
so graph.json/edges.tsv/nodes.tsv can be dropped afterward (workflow.json is kept as the
re-importable source of truth).

Usage:
    python build_history_digest.py <G_group_dir> [--manifest <all_projects_matching_pdfs.tsv>]
    python build_history_digest.py --bank <pipe_test_root> [--manifest ...]   # all groups
"""
import json, os, re, csv, sys, glob, argparse

csv.field_size_limit(1 << 24)

KEY_TYPES = {  # jobs whose resolution/role defines the backbone
    "homo_abinit", "homo_refine_new", "nonuniform_refine_new", "new_local_refine",
    "hetero_refine", "class_3D", "ctf_refine_global", "ctf_refine_local",
}
SELECT_RE = re.compile(r"(Selected|Excluded)\s+(\d+)\s+classes", re.I)
ITER_RE = re.compile(r"iteration_(\d+)")
PDF_ROOT = os.environ.get("EMCLAW_HISTORY_PDF_ROOT", "YOUR_HISTORY_PDF_PATH")

# Parameters that are filesystem paths tied to the ORIGINAL environment. Their history value
# must NOT be copied or omitted — agentM re-fills them from the working environment, else the
# job errors (missing binary / file-not-found / empty required field). Marked per step as
# `env_fields` so agentM treats them as fill-locally, not as learnable knobs.
ENV_PATH_PARAMS = {
    "exec_path", "blob_paths", "volume_blob_path", "particle_meta_path", "particle_blob_path",
    "deepemhancer_exec_path", "deepemhancer_model_path", "path", "mask_path",
    "initial_volume_path", "template_path", "query_templates_path", "recenter_path",
}

def _is_env_field(name, val):
    if name in ENV_PATH_PARAMS:
        return True
    return isinstance(val, str) and (val.startswith(("/", "~")) or "*" in val or "\\" in val)

# Map a manifest filename to a coarse diagnostic 'kind' so subR knows what a PDF shows
# and can pick the right image for a vision call.
def classify_pdf(name):
    n = name.lower()
    if "selected" in n and "class" in n:               return "2d_selected"
    if "excluded" in n and "class" in n:               return "2d_excluded"
    if re.search(r"\d+d_classes_for_iteration", n) or "2d_classes" in n: return "2d_classes"
    if "cfscs_half_angle" in n:                        return "fsc_conical"   # anisotropy
    if n.startswith(("fsc", "j")) and "fsc" in n:      return "fsc"
    if "viewing_direction" in n:                       return "viewing_direction"  # orientation
    if "guinier" in n:                                 return "guinier"        # B-factor
    if "per_particle_scale" in n:                      return "particle_scale"
    if "real_space_slices" in n or "structure_for_class" in n: return "volume_slices"
    if "noise_model" in n:                             return "noise_model"
    return "other"


def disk_name(job_uid, file_id, record_index, filename):
    """On-disk PDF name = J<num>__<file_id[:12]>__rec<record_index>__<filename>."""
    return f"{job_uid}__{file_id[:12]}__rec{record_index}__{filename}"


def _find(group_dir, suffix):
    hits = glob.glob(os.path.join(group_dir, f"*{suffix}"))
    return hits[0] if hits else None


def _particle_count(node):
    n = 0
    for o in node.get("output_result_groups", []):
        if o.get("type") == "particle":
            n = max(n, o.get("num_items") or 0)  # the 'All particles' group
    return n or None


def _final_box(node):
    for o in node.get("output_result_groups", []):
        shp = (o.get("summary") or {}).get("blob/shape")
        if shp:
            return shp[0]
    return None


def build_manifest_index(manifest_path, project_uid=None):
    """Index the PDF manifest. With project_uid -> {job_uid: {...}} for that project.
    Without (project_uid=None) -> {project_uid: {job_uid: {...}}} for the whole bank
    (one streaming pass, so --bank mode reads the 387 MB file only once).

    Per job: {'select': {selected,excluded}, 'pdf': {kind: on_disk_name}}. For multi-
    iteration kinds we keep the representative (highest iteration) so the index stays small
    and points at the final/best image for a vision call."""
    full = project_uid is None
    idx = {}
    if not manifest_path or not os.path.exists(manifest_path):
        return idx
    with open(manifest_path, newline="") as fh:
        r = csv.reader(fh, delimiter="\t")
        header = next(r)
        col = {name: i for i, name in enumerate(header)}
        pu, ju = col["project_uid"], col["job_uid"]
        fn, et = col["filename"], col["event_text"]
        fid, rec, slug = col["file_id"], col["record_index"], col["project_slug"]
        for row in r:
            if len(row) <= et:
                continue
            if not full and row[pu] != project_uid:
                continue
            bucket = idx.setdefault(row[pu], {}) if full else idx
            bucket["__slug__"] = row[slug]  # on-disk pdf dir name (carries home__ prefix)
            e = bucket.setdefault(row[ju], {"select": {}, "pdf": {}, "_iter": {}})
            fname = row[fn]
            if fname:
                kind = classify_pdf(fname)
                it = int(ITER_RE.search(fname).group(1)) if ITER_RE.search(fname) else 0
                if it >= e["_iter"].get(kind, -1):
                    e["_iter"][kind] = it
                    e["pdf"][kind] = disk_name(row[ju], row[fid], row[rec], fname)
            m = SELECT_RE.search(row[et] or "")
            if m:
                e["select"][m.group(1).lower()] = int(m.group(2))
    # drop per-job helper key (skip the reserved __slug__ string entry)
    def _clean(bucket):
        for k, e in bucket.items():
            if isinstance(e, dict):
                e.pop("_iter", None)
    if full:
        for proj in idx.values():
            _clean(proj)
    else:
        _clean(idx)
    return idx


def build_digest(group_dir, manifest_path=None, man_index=None):
    """man_index: preloaded {job_uid: {...}} for this project (skips streaming the
    manifest). Used by --bank so the manifest is read only once for the whole bank."""
    gj = _find(group_dir, "_graph.json")
    wf = _find(group_dir, "_workflow.json")
    if not gj or not wf:
        raise FileNotFoundError(f"missing graph/workflow json in {group_dir}")
    g = json.load(open(gj))
    w = json.load(open(wf))
    wjobs = w.get("jobs", {})

    nodes = {n["uid"]: n for n in g["nodes"]}
    # job-level parents from parent_clone_edges (compact, job-to-job)
    # Workflow graph from the authoritative data-flow edges: per job, which source job's
    # output feeds which input slot (particles / volume / mask / ...). This makes the
    # workflow reconstructable, not just a bare lineage.
    inputs = {}   # target -> {target_input: [source_uid, ...]}
    parents = {}  # target -> [source_uid, ...]  (for ancestor walk)
    for e in g.get("input_connection_edges", []):
        tgt, src, slot = e["target"], e["source"], e.get("target_input") or e.get("target_type") or "in"
        slot_map = inputs.setdefault(tgt, {})
        lst = slot_map.setdefault(slot, [])
        if src not in lst:
            lst.append(src)
        parents.setdefault(tgt, [])
        if src not in parents[tgt]:
            parents[tgt].append(src)

    # resolution per job (seed reconstructions carry the gold-standard FSC@0.143 number)
    res = {s["uid"]: round(s["resolution_A"], 3) for s in g.get("seed_reconstructions", [])
           if s.get("resolution_A")}
    # fall back to node best_reconstruction_resolution_A for non-seed refinements
    for u, n in nodes.items():
        if u not in res and n.get("best_reconstruction_resolution_A"):
            try:
                res[u] = round(float(n["best_reconstruction_resolution_A"]), 3)
            except (TypeError, ValueError):
                pass

    project_uid = g.get("project_uid")
    man = man_index if man_index is not None else build_manifest_index(manifest_path, project_uid)

    def nd_params(uid):
        # keep ALL non-default params (incl compute knobs) — the agent decides what is
        # crucial at run time, combined with subS/subV guidance.
        j = wjobs.get(uid, {})
        return {k: v.get("value") for k, v in j.get("parameters", {}).items()}

    # ---- per-step compact records (all jobs) ----
    steps = {}
    type_counts = {}
    env_fields_seen = set()
    for uid, n in nodes.items():
        jt = n.get("job_type")
        type_counts[jt] = type_counts.get(jt, 0) + 1
        rec = {"type": jt}
        p = nd_params(uid)
        if p:
            rec["params"] = p
            ef = [k for k, v in p.items() if _is_env_field(k, v)]
            if ef:
                rec["env_fields"] = ef          # re-fill locally; do not copy/omit
                env_fields_seen.update(ef)
        slot_map = inputs.get(uid)
        if slot_map:
            # {input_slot: [source_uid,...]} — e.g. {"particles":["J93"],"volume":["J196"]}
            rec["in"] = {k: sorted(v, key=lambda x: int(x[1:]) if x[1:].isdigit() else 0)
                         for k, v in slot_map.items()}
        pc = _particle_count(n)
        if pc:
            rec["n_particle"] = pc
        if uid in res:
            rec["res_A"] = res[uid]
        m = man.get(uid)
        if m:
            if m["select"]:
                rec["select"] = m["select"]
            if m.get("pdf"):
                rec["pdf"] = m["pdf"]  # {kind: on_disk_name}
        steps[uid] = rec

    # ---- backbone: lineage of the best map, key jobs only, chronological ----
    seeds = sorted(g.get("seed_reconstructions", []), key=lambda s: s.get("resolution_A", 9e9))
    best = seeds[0] if seeds else None
    backbone = []
    if best:
        # collect ancestors of best
        seen, stack = set(), [best["uid"]]
        while stack:
            x = stack.pop()
            for par in parents.get(x, []):
                if par not in seen:
                    seen.add(par); stack.append(par)
        seen.add(best["uid"])
        chain = sorted((u for u in seen if nodes[u]["job_type"] in KEY_TYPES),
                       key=lambda u: int(u[1:]))
        prev = None
        for u in chain:
            r = res.get(u)
            row = {"uid": u, "type": nodes[u]["job_type"]}
            if r is not None:
                row["res_A"] = r
                if prev is not None:
                    row["delta_A"] = round(r - prev, 3)
                prev = r
            pc = _particle_count(nodes[u])
            if pc:
                row["n_particle"] = pc
            p = nd_params(u)
            if p:
                row["params"] = p
            backbone.append(row)

    # ---- optics / particle hint ----
    optics = {}
    for uid, j in wjobs.items():
        if j.get("jobType") in ("import_micrographs", "import_movies"):
            pr = {k: v.get("value") for k, v in j.get("parameters", {}).items()}
            for src, dst in (("psize_A", "psize_A"), ("accel_kv", "accel_kv"), ("cs_mm", "cs_mm")):
                if src in pr:
                    optics[dst] = round(pr[src], 4) if isinstance(pr[src], float) else pr[src]
            break
    if best and _final_box(nodes[best["uid"]]):
        optics["final_box_pix"] = _final_box(nodes[best["uid"]])
    picker_d = []
    for j in wjobs.values():
        if j.get("jobType") in ("blob_picker_gpu", "template_picker_gpu"):
            for k in ("diameter", "diameter_max", "par_diam"):
                v = j.get("parameters", {}).get(k, {}).get("value")
                if v:
                    picker_d.append(v)

    # ---- PDF surface (vision): resolvable root + present flag + a representative
    #      clean 2D-average montage for quick particle-signal matching ----
    # On-disk PDF dir naming is inconsistent. Try candidates and pick the one whose
    # files/ actually exists (convention: home__<pipe_test_folder>, sometimes bare).
    folder = os.path.basename(os.path.dirname(group_dir))
    slug = man.get("__slug__") or g.get("project_slug", "")
    candidates = [f"home__{folder}", folder, slug, g.get("project_slug", "")]
    pdf_root = os.path.join(PDF_ROOT, candidates[0])  # default to dominant convention
    pdf_present = False
    for c in candidates:
        if c and os.path.isdir(os.path.join(PDF_ROOT, c, "files")):
            pdf_root, pdf_present = os.path.join(PDF_ROOT, c), True
            break

    # Collection is partial: keep only pdf entries whose image is actually on disk so subR
    # never chases a dead path. (2D select counts come from text and are kept regardless.)
    n_pdf_on_disk = 0
    if pdf_present:
        ondisk = set(os.listdir(os.path.join(pdf_root, "files")))
        for s in steps.values():
            if "pdf" in s:
                s["pdf"] = {k: v for k, v in s["pdf"].items() if v in ondisk}
                if s["pdf"]:
                    n_pdf_on_disk += len(s["pdf"])
                else:
                    del s["pdf"]
    else:
        for s in steps.values():
            s.pop("pdf", None)  # no images collected -> numeric-only digest

    signal_pdf = None
    sel_jobs = [u for u, s in steps.items()
                if s["type"] == "select_2D" and "pdf" in s and "2d_selected" in s["pdf"]]
    if sel_jobs:
        u = max(sel_jobs, key=lambda x: int(x[1:]))  # latest curated 2D set (image on disk)
        signal_pdf = {"uid": u, "file": steps[u]["pdf"]["2d_selected"]}

    digest = {
        "schema": "cryoem-history-digest/v1",
        "project_uid": project_uid,
        "project_name": slug.split("__")[-1],
        "project_slug": slug,
        "group": os.path.basename(group_dir).split("__")[0],
        "workspaces": g.get("workspace_group_workspaces"),
        "cs_version": next((n.get("version") for n in nodes.values() if n.get("version")), None),
        "resolution_cutoff_A": g.get("resolution_cutoff_A"),
        "pdf": {"root": pdf_root, "present": pdf_present, "images_on_disk": n_pdf_on_disk,
                "signal_2d": signal_pdf, "resolve": "<root>/files/<on_disk_name>"},
        "optics": optics,
        "particle": {"title": g.get("summary", {}).get("project_title")
                     if isinstance(g.get("summary"), dict) else None,
                     "picker_diameter_A": sorted(set(picker_d)) or None},
        "result": {
            "best_uid": best["uid"] if best else None,
            "best_job_type": best["job_type"] if best else None,
            "best_res_A": round(best["resolution_A"], 3) if best else None,
            "n_particles": _particle_count(nodes[best["uid"]]) if best else None,
            "n_seeds": len(seeds),
        },
        "job_type_counts": dict(sorted(type_counts.items(), key=lambda x: -x[1])),
        "param_policy": {
            "note": "steps[].params keeps ALL non-default knobs verbatim (learn from these). "
                    "Names in steps[].env_fields are environment-specific paths from the source "
                    "project: re-fill from the WORKING environment, never copy or omit. Confirm "
                    "the full required-param set per job via MCP validate_params before running.",
            "env_fields_seen": sorted(env_fields_seen),
        },
        "backbone": backbone,
        "steps": steps,
    }
    return digest


def write_digest(group_dir, manifest_path=None, man_index=None):
    d = build_digest(group_dir, manifest_path, man_index)
    wf = _find(group_dir, "_workflow.json")
    base = os.path.basename(wf).replace("_cryosparc_workflow.json", "").replace("_workflow.json", "")
    out = os.path.join(group_dir, base + "_digest.json")
    with open(out, "w") as fh:
        json.dump(d, fh, separators=(",", ":"))
    return out, os.path.getsize(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", nargs="?", help="a G* group dir")
    ap.add_argument("--bank", help="pipe_test root; process every G* group")
    ap.add_argument("--manifest", default=os.path.join(PDF_ROOT, "all_projects_matching_pdfs.tsv"))
    a = ap.parse_args()
    if a.bank:
        groups = sorted(glob.glob(os.path.join(a.bank, "*CS*", "G*")))
        print(f"{len(groups)} groups; indexing manifest once...", flush=True)
        full = build_manifest_index(a.manifest, project_uid=None)
        print(f"manifest indexed: {len(full)} projects", flush=True)
        ok = tot = 0
        for gdir in groups:
            try:
                g0 = json.load(open(_find(gdir, "_graph.json")))
                man = full.get(g0.get("project_uid"), {})
                out, sz = write_digest(gdir, man_index=man)
                ok += 1; tot += sz
                print(f"  ok  {os.path.basename(gdir):40s} {sz/1024:7.1f} KB")
            except Exception as ex:
                print(f"  ERR {os.path.basename(gdir):40s} {ex}")
        print(f"done: {ok}/{len(groups)} digests, {tot/1e6:.1f} MB total")
    elif a.target:
        out, sz = write_digest(a.target, a.manifest)
        print(f"wrote {out} ({sz/1024:.1f} KB)")
    else:
        ap.error("give a group dir or --bank")


if __name__ == "__main__":
    main()
