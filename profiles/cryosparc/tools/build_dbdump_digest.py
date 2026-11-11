#!/usr/bin/env python3
"""Distil ONE USER's CryoSPARC history out of a raw `cryosparcm mongo` dump into
history-bank digests — the same read surface `build_history_digest.py` gives subR/agentM.

Why this exists
---------------
`build_history_digest.py` reads a curated bank group (graph.json + workflow.json) and
`build_live_digest.py` reads the LIVE server. Neither can see a project that was DELETED
from the working instance. A raw mongo dump (produced by the repository's
`helper/export_cryosparc_database_history.sh`) still holds those documents — deleted projects AND
jobs deleted inside live projects — so this builder recovers that history and emits digests
in the identical schema.

What it produces
----------------
One digest per SAMPLE, not per workspace. A sample is a connected component of the job
graph over DATA edges only (`particle`, `exposure`): the same specimen is routinely spread
over many workspaces, so workspaces are recorded, never used to split. Edges that carry a
model rather than data — `volume`, `mask`, `template`, `ml_model` — do NOT join components,
so two branches that merely trade a reference volume stay two samples (this is the
"only exchange of volume => separate samples" rule). Such jobs are still kept in the digest
when a milestone descends from them, flagged `external: true` with `external_via`.

MILESTONE = a completed single-volume refinement whose final gold-standard resolution is
<= --cutoff (default 4.0 A). Every milestone's full ancestry (parents, their parents, all
edge types) is what the digest keeps; the rest of the project is dropped.

Resolution
----------
The dump carries no resolution field and no gridfs payload, so resolution is read from the
event log: a refinement prints, per iteration, after it has recomputed the FSC,
    `  Using Filter Radius 99.348 (2.645A) | Previous: 97.149 (2.705A)`
and lowpass-filters its output to exactly that value. The LAST such line of a job is its
final gold-standard FSC resolution. Stage 1 (`index-res`) greps the event dumps for those
lines and caches a {project/job -> res} index; stage 2 (`digest`) uses it.

NOTE on the dump's events files: the exporter's events query is
`{$or:[{project_uid:...},{job_uid:{$in: all_job_uids}}]}`, and the second term is not
project-scoped — so every project's events.json is a large CROSS-project dump. A project's
own events are always in its own file; other files may or may not have them, and each was
taken at a different time. `index-res` therefore greps a SET of files and unions the result,
deduplicating events by timestamp.

Coverage limit (measured on one site's dump, 2026-09-02): every SURVIVING milestone-
eligible job has a resolution; the only ones missing are jobs flagged `deleted: true`
(2636/2636 of the misses). CryoSPARC purges a job's event log when the job is deleted, so
a deleted job's resolution is not recoverable from the dump - its parameters and wiring
still are. A deleted job therefore never becomes a milestone, but stays visible as lineage.

Usage
-----
  # --dump-root is the export script's output folder. --home-root is the directory that
  # holds the users' home folders (default $EMCLAW_CRYOSPARC_HOME_ROOT, else /home): a
  # project belongs to <user> when its project_dir sits under <home-root>/<user>/.

  # stage 1 — build the resolution index (slow, cached; ~1 GB/s per file, run once)
  python tools/build_dbdump_digest.py index-res --user YOUR_CRYOSPARC_USER \
      --dump-root YOUR_DB_EXPORT_PATH --home-root /home \
      --out ~/dbdump_cache/res_index.json --jobs 12

  # stage 2 — build the digests
  python tools/build_dbdump_digest.py digest --user YOUR_CRYOSPARC_USER \
      --dump-root YOUR_DB_EXPORT_PATH --home-root /home \
      --res-index ~/dbdump_cache/res_index.json \
      --out-root history_bank --cutoff 4.0

  python tools/build_dbdump_digest.py list-users --dump-root YOUR_DB_EXPORT_PATH | head -30
"""
import argparse
import collections
import glob
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from build_history_digest import KEY_TYPES, _is_env_field  # same backbone types, same env policy

# ---------------------------------------------------------------- constants

# Bookkeeping / UI jobs that add nothing to a recipe.
SKIP_TYPES = {"workspace", "instance_launch", "worker_gpu_test", "instance_testing"}

# Input-slot types that carry the SAMPLE itself. Only these join two jobs into one sample.
# Everything else (volume, mask, template, ml_model, denoise_model, hyperparameter) is a
# model/reference that can legitimately be borrowed between samples.
LINK_SLOT_TYPES = {"particle", "exposure"}

# Single-volume refinements: their last "Using Filter Radius" IS the job's reported
# gold-standard resolution, so only these are eligible to be milestones. hetero_refine /
# class_3D interleave several classes per iteration and homo_abinit prints no FSC at all,
# so they are lineage, never milestones.
MILESTONE_TYPES = {
    "nonuniform_refine_new", "nonuniform_refine", "homo_refine_new", "homo_refine",
    "new_local_refine", "local_refine", "naive_local_refine",
}

PICKER_DIAM_KEYS = ("diameter", "diameter_max", "par_diam")
OPTICS_KEYS = ("psize_A", "accel_kv", "cs_mm", "total_dose_e_per_A2")

# Directory holding the users' home folders; set from --home-root in main().
HOME_ROOT = os.environ.get("EMCLAW_CRYOSPARC_HOME_ROOT", "/home")

FILTER_RADIUS_PATTERN = '"text": "  Using Filter Radius '
_RES_RE = re.compile(r"Using Filter Radius [\d.]+ \(([\d.]+)A\)")
_PU_RE = re.compile(r'"project_uid": "(P\d+)"')
_JU_RE = re.compile(r'"job_uid": "(J\d+)"')
_TS_RE = re.compile(r'"created_at": "([^"]+)"')


# ---------------------------------------------------------------- dump access

def project_dirs(dump_root):
    """{project_uid: dir} for every project in the dump."""
    out = {}
    for p in glob.glob(os.path.join(dump_root, "project_metadata_work", "*", "*", "project.json")):
        out[os.path.basename(os.path.dirname(p))] = os.path.dirname(p)
    return dict(sorted(out.items(), key=lambda kv: int(kv[0][1:])))


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path) as fh:
            return json.load(fh)
    except Exception as ex:  # noqa: BLE001 - a truncated section must not kill the run
        print(f"[warn] unreadable {path}: {ex!r}", file=sys.stderr)
        return default


def home_user(project_doc):
    """The unix account under whose <HOME_ROOT>/<user>/ the project directory sits."""
    root = "/" + HOME_ROOT.strip("/") + "/"
    m = re.match("^" + re.escape(root) + r"([^/]+)/", project_doc.get("project_dir") or "")
    return m.group(1) if m else None


def owner_ids_for(projects, user):
    """Map a username to owner_user_id(s) using the projects that are BOTH in that user's
    home tree and last touched by them — the only unambiguous evidence in the dump."""
    ids = set()
    for doc in projects.values():
        if home_user(doc) == user and (doc.get("last_accessed") or {}).get("name") == user:
            if doc.get("owner_user_id"):
                ids.add(doc["owner_user_id"])
    return ids


def relation_of(doc, user, owner_ids):
    """How this project belongs to the user: they own it, it lives in their home tree, or
    they were merely the last person to open someone else's project."""
    if doc.get("owner_user_id") in owner_ids:
        return "owner"
    if home_user(doc) == user:
        return "home_dir"
    if (doc.get("last_accessed") or {}).get("name") == user:
        return "accessed"
    return None


def select_projects(dump_root, user, relations):
    """{project_uid: (dir, project_doc, relation)} for the user's projects."""
    dirs = project_dirs(dump_root)
    docs = {}
    for uid, d in dirs.items():
        doc = load_json(os.path.join(d, "project.json"))
        if doc:
            docs[uid] = doc
    oids = owner_ids_for(docs, user)
    out = {}
    for uid, doc in docs.items():
        rel = relation_of(doc, user, oids)
        if rel and rel in relations:
            out[uid] = (dirs[uid], doc, rel)
    return out, oids


# ---------------------------------------------------------------- stage 1: resolution index

def _grep_one(events_path, out_path):
    """Pull the filter-radius events (plus enough trailing context to identify the job)."""
    with open(out_path, "wb") as fh:
        subprocess.run(["grep", "-F", "-A", "14", FILTER_RADIUS_PATTERN, events_path],
                       stdout=fh, stderr=subprocess.DEVNULL, check=False)
    return out_path, os.path.getsize(out_path)


def _parse_grep(path, sink):
    """sink[(project, job)][created_at] = res_A  — timestamp-keyed so the same event seen in
    several cross-project dumps is counted once."""
    res = proj = job = None
    with open(path, errors="replace") as fh:
        for line in fh:
            m = _RES_RE.search(line)
            if m:
                res, proj = float(m.group(1)), None
                continue
            if res is None:
                continue
            m = _PU_RE.search(line)
            if m:
                proj = m.group(1)
                continue
            m = _JU_RE.search(line)
            if m and proj:
                job = m.group(1)
                continue
            m = _TS_RE.search(line)
            if m and proj and job:
                sink[(proj, job)][m.group(1)] = res
                res = proj = job = None


def cmd_index_res(a):
    projects, _ = select_projects(a.dump_root, a.user, set(a.relations.split(",")))
    files = [os.path.join(d, "events.json") for d, _doc, _rel in projects.values()]
    files = [f for f in files if os.path.exists(f) and os.path.getsize(f) > 1000]
    if a.extra_events:
        files += [f for f in a.extra_events.split(",") if os.path.exists(f)]
    tmp = a.out + ".grep.d"
    os.makedirs(tmp, exist_ok=True)
    total = sum(os.path.getsize(f) for f in files)
    print(f"{len(files)} event dumps, {total/1e12:.2f} TB to scan with {a.jobs} parallel greps",
          flush=True)

    tasks = [(f, os.path.join(tmp, f"{f.split('/')[-2]}.txt")) for f in files]
    todo = [t for t in tasks if not (a.reuse and os.path.exists(t[1]))]
    done = 0
    with ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for out, sz in ex.map(lambda t: _grep_one(*t), todo):
            done += 1
            print(f"  [{done}/{len(todo)}] {os.path.basename(out):14s} {sz/1e6:8.1f} MB", flush=True)

    sink = collections.defaultdict(dict)
    for _f, out in tasks:
        if os.path.exists(out):
            _parse_grep(out, sink)

    index = {}
    for (p, j), by_ts in sink.items():
        seq = [v for _ts, v in sorted(by_ts.items())]
        index[f"{p}/{j}"] = {"res_A": seq[-1], "n_iter": len(seq), "best_A": min(seq)}
    os.makedirs(os.path.dirname(os.path.abspath(a.out)) or ".", exist_ok=True)
    with open(a.out, "w") as fh:
        json.dump(index, fh, separators=(",", ":"))
    projs = len({k.split("/")[0] for k in index})
    print(f"wrote {a.out}: {len(index)} jobs with resolution over {projs} projects")


# ---------------------------------------------------------------- job graph

def _inputs(doc):
    """{slot_name: {"type": slot_type, "src": [job_uid, ...]}} — typed data-flow wiring."""
    out = {}
    for g in doc.get("input_slot_groups") or []:
        name = g.get("name") or g.get("type") or "in"
        srcs = []
        for c in g.get("connections") or []:
            if c.get("job_uid") and c["job_uid"] not in srcs:
                srcs.append(c["job_uid"])
        if srcs:
            out[name] = {"type": g.get("type"), "src": srcs}
    return out


def _particle_count(doc):
    n = 0
    for o in doc.get("output_result_groups") or []:
        if o.get("type") == "particle":
            n = max(n, o.get("num_items") or 0)
    return n or None


def _psize(doc):
    """Pixel size (A) of the job's own output — sets the Nyquist limit of its FSC."""
    for o in doc.get("output_result_groups") or []:
        s = o.get("summary") or {}
        for k in ("blob/psize_A", "alignments3D/psize_A", "alignments2D/psize_A"):
            if s.get(k):
                return float(s[k])
    return None


def _box(doc):
    for o in doc.get("output_result_groups") or []:
        shp = ((o.get("summary") or {}).get("blob/shape"))
        if isinstance(shp, list) and shp:
            return shp[0]
    return None


class DSU:
    def __init__(self):
        self.p = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra


def _uidnum(u):
    return int(u[1:]) if u[1:].isdigit() else 0


# ---------------------------------------------------------------- stage 2: digest

def build_project_samples(pdir, doc, relation, res_index, cutoff, link_types,
                          min_res=1.8, nyquist_margin=1.05):
    """Return (samples, meta). One sample per connected component that holds a milestone."""
    jobs_list = load_json(os.path.join(pdir, "jobs.json"), []) or []
    ws_docs = load_json(os.path.join(pdir, "workspaces.json"), []) or []
    ws_title = {w["uid"]: w.get("title") for w in ws_docs if w.get("uid")}
    puid = doc["uid"]

    jobs = {j["uid"]: j for j in jobs_list
            if j.get("uid") and j.get("job_type") not in SKIP_TYPES}

    wiring = {u: _inputs(j) for u, j in jobs.items()}
    parents = {u: sorted({s for w in wiring[u].values() for s in w["src"] if s in jobs},
                         key=_uidnum) for u in jobs}

    dsu = DSU()
    for u in jobs:
        dsu.find(u)
    for u, w in wiring.items():
        for slot in w.values():
            if slot["type"] in link_types:
                for s in slot["src"]:
                    if s in jobs:
                        dsu.union(u, s)

    # ---- milestones ----
    # A refinement whose FSC never falls below 0.143 inside its own box reports the box
    # limit, not a measurement: `Using Filter Radius` then pins at ~Nyquist (2 x psize).
    # Such a number is an artefact of the extraction, so it cannot define a milestone.
    # A number below --min-res is likewise not a real single-particle result (in practice
    # correlated half-sets, e.g. duplicate particles split across A/B).
    milestones, rejected = [], []
    for u, j in jobs.items():
        if j.get("job_type") not in MILESTONE_TYPES or j.get("status") != "completed":
            continue
        r = res_index.get(f"{puid}/{u}")
        if not r or r["res_A"] > cutoff:
            continue
        ps = _psize(j)
        nyq = round(2 * ps, 4) if ps else None
        why = None
        if nyq and r["res_A"] <= nyq * nyquist_margin:
            why = f"NYQUIST_LIMITED (res {r['res_A']} A vs box Nyquist {nyq} A)"
        elif r["res_A"] < min_res:
            why = f"IMPLAUSIBLE (res {r['res_A']} A < floor {min_res} A)"
        if why:
            rejected.append({"uid": u, "type": j.get("job_type"), "res_A": r["res_A"],
                             "nyquist_A": nyq, "reason": why})
        else:
            milestones.append((u, r))
    if not milestones:
        return [], {"n_jobs": len(jobs), "n_milestones": 0, "n_rejected": len(rejected)}

    by_comp = collections.defaultdict(list)
    for u, r in milestones:
        by_comp[dsu.find(u)].append(u)

    samples = []
    for comp, ms in by_comp.items():
        keep, stack = set(ms), list(ms)
        while stack:
            for par in parents[stack.pop()]:
                if par not in keep:
                    keep.add(par)
                    stack.append(par)

        # Jobs in `keep` that are NOT part of this specimen: they were reached only across a
        # model edge (volume / mask / template / ml_model). Tag each with the slot type the
        # sample entered it through, and propagate that tag up its own ancestry, so the whole
        # borrowed sub-branch reads as borrowed rather than just its entry point.
        own = {u for u in keep if dsu.find(u) == comp}
        ext = keep - own
        via = collections.defaultdict(set)
        frontier = []
        for c in own:
            for slot in wiring[c].values():
                for s_uid in slot["src"]:
                    if s_uid in ext:
                        via[s_uid].add(slot["type"])
                        frontier.append(s_uid)
        seen = set(frontier)
        while frontier:
            x = frontier.pop()
            for slot in wiring[x].values():
                for s_uid in slot["src"]:
                    if s_uid in ext:
                        via[s_uid] |= via.get(x, set())
                        if s_uid not in seen:
                            seen.add(s_uid)
                            frontier.append(s_uid)

        steps = {}
        versions = collections.Counter()
        env_seen, type_counts = set(), collections.Counter()
        status_counts = collections.Counter()
        for u in sorted(keep, key=_uidnum):
            j = jobs[u]
            jt = j.get("job_type")
            type_counts[jt] += 1
            status_counts[j.get("status")] += 1
            if j.get("version"):
                versions[j["version"]] += 1
            rec = {"type": jt, "status": j.get("status")}
            wss = j.get("workspace_uids") or []
            if wss:
                rec["workspace"] = wss[0] if len(wss) == 1 else sorted(wss, key=_uidnum)
            title = (j.get("title") or "").strip()
            if title and title != jt and not title.startswith("New Job"):
                rec["title"] = title            # the human label: states the job's INTENT
            if j.get("deleted"):
                rec["deleted"] = True           # gone from the live instance, kept here
            params = {k: v.get("value") for k, v in (j.get("params_spec") or {}).items()}
            if params:
                rec["params"] = params
                ef = [k for k, v in params.items() if _is_env_field(k, v)]
                if ef:
                    rec["env_fields"] = ef
                    env_seen.update(ef)
            if wiring[u]:
                rec["in"] = {k: v["src"] for k, v in wiring[u].items()}
                rec["in_types"] = {k: v["type"] for k, v in wiring[u].items()}
            pc = _particle_count(j)
            if pc:
                rec["n_particle"] = pc
            box = _box(j)
            if box:
                rec["box_pix"] = box
            r = res_index.get(f"{puid}/{u}")
            if r:
                rec["res_A"] = r["res_A"]
                rec["n_iter"] = r["n_iter"]
                ps = _psize(j)
                if ps:
                    rec["nyquist_A"] = round(2 * ps, 4)
                    if r["res_A"] <= rec["nyquist_A"] * nyquist_margin:
                        rec["res_flag"] = "NYQUIST_LIMITED"
                    elif r["res_A"] < min_res:
                        rec["res_flag"] = "IMPLAUSIBLE"
            if dsu.find(u) != comp:
                # reached only through a volume/mask/template edge: a reference borrowed
                # from ANOTHER sample, kept for the recipe but not part of this specimen.
                rec["external"] = True
                rec["external_via"] = sorted(via.get(u, ()))
            steps[u] = rec

        ms_sorted = sorted(ms, key=lambda u: steps[u]["res_A"])
        best = ms_sorted[0]

        # ---- backbone: key jobs on the ancestry of the best map, chronological ----
        anc, stack = {best}, [best]
        while stack:
            for par in parents[stack.pop()]:
                if par not in anc:
                    anc.add(par)
                    stack.append(par)
        backbone, prev = [], None
        for u in sorted((x for x in anc if steps[x]["type"] in KEY_TYPES), key=_uidnum):
            s = steps[u]
            row = {"uid": u, "type": s["type"]}
            if s.get("res_A") is not None:
                row["res_A"] = s["res_A"]
                if prev is not None:
                    row["delta_A"] = round(s["res_A"] - prev, 3)
                prev = s["res_A"]
            for k in ("n_particle", "title", "params"):
                if s.get(k):
                    row[k] = s[k]
            backbone.append(row)

        optics = {}
        for u in sorted(keep, key=_uidnum):
            if steps[u]["type"] in ("import_micrographs", "import_movies"):
                for k in OPTICS_KEYS:
                    v = (steps[u].get("params") or {}).get(k)
                    if v is not None:
                        optics[k] = round(v, 4) if isinstance(v, float) else v
                break
        if steps[best].get("box_pix"):
            optics["final_box_pix"] = steps[best]["box_pix"]

        picker_d = sorted({v for s in steps.values() if "picker" in s["type"]
                           for k, v in (s.get("params") or {}).items()
                           if k in PICKER_DIAM_KEYS and v})

        wss = sorted({w for s in steps.values() for w in
                      ([s["workspace"]] if isinstance(s.get("workspace"), str)
                       else (s.get("workspace") or []))}, key=_uidnum)

        # the raw data this specimen actually came from (borrowed branches excluded)
        data_sources = [{"uid": u, "type": steps[u]["type"],
                         "path": (steps[u].get("params") or {}).get("blob_paths")
                                 or (steps[u].get("params") or {}).get("particle_meta_path")}
                        for u in sorted(own, key=_uidnum)
                        if steps[u]["type"].startswith("import_")
                        and steps[u]["type"] != "import_volumes"]

        samples.append({
            "component": comp,
            "data_sources": data_sources,
            "rejected": [r for r in rejected if dsu.find(r["uid"]) == comp],
            "workspaces": wss,
            "workspace_titles": {w: ws_title.get(w) for w in wss},
            "steps": steps,
            "backbone": backbone,
            "optics": optics,
            "picker_diameter_A": picker_d,
            "milestones": [{"uid": u, "type": steps[u]["type"], "res_A": steps[u]["res_A"],
                            "n_particle": steps[u].get("n_particle"),
                            "workspace": steps[u].get("workspace"),
                            "deleted": steps[u].get("deleted", False)} for u in ms_sorted],
            "best": best,
            "cs_version": versions.most_common(1)[0][0] if versions else None,
            "type_counts": dict(type_counts.most_common()),
            "status_counts": dict(status_counts),
            "env_seen": sorted(env_seen),
            "n_deleted_steps": sum(1 for s in steps.values() if s.get("deleted")),
            "n_external_steps": len(ext),
        })

    samples.sort(key=lambda s: s["steps"][s["best"]]["res_A"])
    return samples, {"n_jobs": len(jobs), "n_milestones": len(milestones)}


def slug_of(doc):
    """`home__<user>__<dirs>__<project dir name>`, the history-bank naming convention."""
    d = (doc.get("project_dir") or "").strip("/")
    root = HOME_ROOT.strip("/") + "/"
    if d.startswith(root):
        d = "home/" + d[len(root):]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", d.replace("/", "__")) or doc["uid"]


def cmd_digest(a):
    res_index = load_json(a.res_index, {}) or {}
    if not res_index:
        sys.exit(f"no resolution index at {a.res_index}; run `index-res` first")
    link_types = set(a.link_types.split(","))
    projects, oids = select_projects(a.dump_root, a.user, set(a.relations.split(",")))
    print(f"user {a.user}: owner_ids={sorted(oids)}; {len(projects)} projects "
          f"({a.relations}); link slot types={sorted(link_types)}", flush=True)

    index_rows, n_written = [], 0
    for puid, (pdir, doc, relation) in projects.items():
        samples, meta = build_project_samples(pdir, doc, relation, res_index, a.cutoff,
                                             link_types, a.min_res, a.nyquist_margin)
        if not samples:
            continue
        slug = slug_of(doc)
        short = slug[len("home__"):] if slug.startswith("home__") else slug
        for i, s in enumerate(samples, 1):
            gid = f"G{i:03d}"
            wtag = "_".join(s["workspaces"]) or "W0"
            gdir = os.path.join(a.out_root, short, f"{gid}__{wtag}")
            os.makedirs(gdir, exist_ok=True)
            best = s["steps"][s["best"]]
            digest = {
                "schema": "cryoem-history-digest/v1",
                "source": "dbdump",
                "dump_root": a.dump_root,
                "project_uid": puid,
                "project_name": (doc.get("project_dir") or "").rstrip("/").split("/")[-1],
                "project_title": doc.get("title"),
                "project_slug": slug,
                "project_deleted": bool(doc.get("deleted")),
                "user": a.user,
                "user_relation": relation,
                "group": gid,
                "workspaces": s["workspaces"],
                "workspace_titles": s["workspace_titles"],
                "cs_version": s["cs_version"],
                "resolution_cutoff_A": a.cutoff,
                "sample": {
                    "note": "one sample = one connected component of the job graph over DATA "
                            "edges only; volume/mask/template/ml_model edges do NOT merge "
                            "samples, so branches that only trade a reference volume stay "
                            "separate. Steps flagged `external` are such borrowed references.",
                    "link_slot_types": sorted(link_types),
                    "component_root": s["component"],
                    "n_milestones": len(s["milestones"]),
                    "n_rejected_milestones": len(s["rejected"]),
                    "data_sources": s["data_sources"],
                    "n_steps": len(s["steps"]),
                    "n_deleted_steps": s["n_deleted_steps"],
                    "n_external_steps": s["n_external_steps"],
                    "project_jobs_total": meta["n_jobs"],
                },
                "pdf": {"present": False,
                        "note": "the mongo dump carries no gridfs payload; this digest is "
                                "numeric + parameter only (no 2D montages / FSC plots)."},
                "optics": s["optics"],
                "particle": {"title": doc.get("title"),
                             "picker_diameter_A": s["picker_diameter_A"] or None},
                "result": {
                    "best_uid": s["best"],
                    "best_job_type": best["type"],
                    "best_res_A": best["res_A"],
                    "n_particles": best.get("n_particle"),
                    "n_seeds": len(s["milestones"]),
                    "note": "best_res_A is the job's own final gold-standard FSC, recovered "
                            "from the event log ('Using Filter Radius ... (X A)' of the last "
                            "iteration). It is NOT validated — run vis_res on a map before "
                            "trusting a number (CLAUDE.md step 9). Jobs flagged `deleted` "
                            "carry no res_A: CryoSPARC purges the event log on delete, "
                            "so their resolution is unrecoverable and they can never be "
                            "a milestone - they remain as lineage.",
                },
                "milestones": s["milestones"],
                "rejected_milestones": s["rejected"],
                "job_type_counts": s["type_counts"],
                "status_counts": s["status_counts"],
                "param_policy": {
                    "note": "steps[].params keeps ALL non-default knobs verbatim (learn from "
                            "these). Names in steps[].env_fields are environment-specific paths "
                            "from the SOURCE project: re-fill from the working environment, "
                            "never copy or omit. Confirm the full required-param set per job "
                            "via MCP validate_params before running.",
                    "env_fields_seen": s["env_seen"],
                },
                "backbone": s["backbone"],
                "steps": s["steps"],
            }
            fname = f"{short}__{puid}__{gid}__{wtag}__le_{a.cutoff:g}A_digest.json"
            out = os.path.join(gdir, fname)
            with open(out, "w") as fh:
                json.dump(digest, fh, separators=(",", ":"))
            n_written += 1
            index_rows.append({
                "path": out, "project_uid": puid, "group": gid, "slug": short,
                "project_title": doc.get("title"), "relation": relation,
                "project_deleted": bool(doc.get("deleted")),
                "workspaces": s["workspaces"],
                "best_uid": s["best"], "best_type": best["type"], "best_res_A": best["res_A"],
                "n_particles": best.get("n_particle"), "n_milestones": len(s["milestones"]),
                "n_steps": len(s["steps"]), "n_deleted_steps": s["n_deleted_steps"],
                "size_kb": round(os.path.getsize(out) / 1024, 1),
            })
            print(f"  {puid} {gid} {wtag:22s} best {s['best']} {best['res_A']:.2f} A "
                  f"({len(s['milestones'])} milestones, {len(s['steps'])} steps, "
                  f"{s['n_deleted_steps']} deleted) -> {os.path.basename(out)}", flush=True)

    index_rows.sort(key=lambda r: r["best_res_A"])
    os.makedirs(a.out_root, exist_ok=True)
    with open(os.path.join(a.out_root, "index.json"), "w") as fh:
        json.dump({"user": a.user, "dump_root": a.dump_root, "cutoff_A": a.cutoff,
                   "link_slot_types": sorted(link_types), "samples": index_rows}, fh, indent=1)
    with open(os.path.join(a.out_root, "index.tsv"), "w") as fh:
        cols = ["best_res_A", "project_uid", "group", "best_uid", "best_type", "n_particles",
                "n_milestones", "n_steps", "n_deleted_steps", "project_deleted", "relation",
                "project_title", "workspaces", "slug"]
        fh.write("\t".join(cols) + "\n")
        for r in index_rows:
            r = dict(r, workspaces=",".join(r["workspaces"]))
            fh.write("\t".join(str(r.get(c, "")) for c in cols) + "\n")
    print(f"\n{n_written} sample digests -> {a.out_root} (index.json / index.tsv)")


def cmd_list_users(a):
    dirs = project_dirs(a.dump_root)
    c = collections.Counter()
    for d in dirs.values():
        doc = load_json(os.path.join(d, "project.json"))
        if not doc:
            continue
        for who in {home_user(doc), (doc.get("last_accessed") or {}).get("name")}:
            if who:
                c[who] += 1
    for who, n in c.most_common(a.limit):
        print(f"{n:5d}  {who}")


def main():
    global HOME_ROOT
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--dump-root", required=True,
                       help="output folder of helper/export_cryosparc_database_history.sh")
        p.add_argument("--home-root", default=HOME_ROOT,
                       help="directory holding the users' home folders "
                            "(default $EMCLAW_CRYOSPARC_HOME_ROOT, else /home)")
        p.add_argument("--user", required=True)
        p.add_argument("--relations", default="owner,home_dir",
                       help="which projects count as the user's: owner,home_dir,accessed "
                            "(default owner,home_dir)")

    p = sub.add_parser("index-res", help="grep the event dumps for per-job final resolution")
    common(p)
    p.add_argument("--out", required=True)
    p.add_argument("--jobs", type=int, default=8, help="parallel greps")
    p.add_argument("--reuse", action="store_true", help="keep existing per-file grep output")
    p.add_argument("--extra-events", help="comma-separated extra events.json files to scan")
    p.set_defaults(func=cmd_index_res)

    p = sub.add_parser("digest", help="write one digest per sample")
    common(p)
    p.add_argument("--res-index", required=True)
    p.add_argument("--out-root", required=True)
    p.add_argument("--cutoff", type=float, default=4.0, help="milestone resolution cutoff (A)")
    p.add_argument("--min-res", type=float, default=1.8,
                   help="reject a 'milestone' better than this as not a real result "
                        "(default 1.8 A); it is still recorded under rejected_milestones")
    p.add_argument("--nyquist-margin", type=float, default=1.05,
                   help="reject a milestone within this factor of its own box Nyquist "
                        "(2 x pixel size) — the FSC was truncated by the box, not measured")
    p.add_argument("--link-types", default=",".join(sorted(LINK_SLOT_TYPES)),
                   help="input-slot types that join jobs into ONE sample "
                        "(default particle,exposure — volume/mask/template never join)")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("list-users", help="who appears in the dump")
    p.add_argument("--dump-root", required=True,
                   help="output folder of helper/export_cryosparc_database_history.sh")
    p.add_argument("--home-root", default=HOME_ROOT,
                   help="directory holding the users' home folders")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(func=cmd_list_users)

    a = ap.parse_args()
    HOME_ROOT = a.home_root
    a.func(a)


if __name__ == "__main__":
    main()
