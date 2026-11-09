#!/usr/bin/env python3
"""
Extract a reusable CryoSPARC workflow template for the effective upstream jobs that lead
to reconstructions better than a resolution cutoff.

New default behavior:
- Search scope is exactly --project-dir (an exported CryoSPARC project directory).
- Automatically infer project UID/title/version from project.json or J*/job.json.
- Scan all jobs in that project, not only one workspace.
- Find completed reconstruction/refinement jobs whose best reported resolution is <=
  --resolution-cutoff (i.e. better/higher-resolution than cutoff in Å).
- If multiple reconstructions pass, reverse-trace all upstream input/parent/clone jobs
  and take the union.
- Export both graph files and a CryoSPARC-tools-style workflow JSON:
    {"_id": ..., "title": ..., "workflowVersion": "1.0.0", ..., "jobs": {"J1": ...}}

Typo-compatible alias is accepted: --reolution_cutoff.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import pathlib
import re
from collections import Counter, defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

DEFAULT_RECON_TYPES = {
    "homo_abinit", "hetero_refine", "nonuniform_refine_new", "class_3D",
    "new_local_refine", "naive_local_refine", "homo_refine_new", "homo_refine",
    "nonuniform_refine",
}
DEFAULT_RESOLUTION_FIELD_SUFFIXES = (
    "radwn_final_A", "radwn_tightmask_A", "radwn_noisesub_A", "res_0143_A",
)
STATUS_COLORS = {
    "completed": "#b7e4c7", "failed": "#ffccd5", "killed": "#ffd6a5",
    "building": "#d7e3fc", "queued": "#fff3b0", "launched": "#caffbf",
    "waiting": "#eeeeee",
}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Extract effective CryoSPARC workflow JSON by resolution cutoff.")
    p.add_argument("--project-dir", "--dir", required=True,
                   help="Directory to search; should contain project.json and/or J*/job.json")
    p.add_argument("--project-uid", default=None,
                   help="Optional override. By default inferred from project.json/job.json.")
    p.add_argument("--workspace-uid", default=None,
                   help="Optional filter. Default scans all jobs in the project.")
    p.add_argument("--resolution-cutoff", "--resolution_cutoff", dest="resolution_cutoff", type=float, default=None,
                   help="Cutoff in Å; selected seeds have best reported resolution <= cutoff.")
    p.add_argument("--reolution-cutoff", "--reolution_cutoff", dest="reolution_cutoff", type=float, default=None,
                   help=argparse.SUPPRESS)
    p.add_argument("--outdir", default=None,
                   help="Output directory. Default: <project-dir>/<project>_effective_workflow_le_<cutoff>A")
    p.add_argument("--include-deleted", action="store_true", help="Include deleted/trashed jobs in scan/traversal.")
    p.add_argument("--active-only-output", action="store_true", help="Drop deleted jobs from final output.")
    p.add_argument("--include-noncompleted-seeds", action="store_true",
                   help="Allow non-completed reconstruction jobs as seeds if they have resolution metadata.")
    p.add_argument("--seed-job", action="append", default=[], help="Force-add a seed UID. Repeatable.")
    p.add_argument("--exclude-job", action="append", default=[], help="Exclude a job UID from final graph. Repeatable.")
    p.add_argument("--recon-types", default=",".join(sorted(DEFAULT_RECON_TYPES)),
                   help="Comma-separated job_type values considered reconstruction/refinement seeds.")
    p.add_argument("--resolution-fields", default=",".join(DEFAULT_RESOLUTION_FIELD_SUFFIXES),
                   help="Comma-separated resolution field suffixes to consider.")
    p.add_argument("--no-parent-metadata", action="store_true",
                   help="Trace only input connections, not parents/cloned_from metadata.")
    p.add_argument("--print-seeds-only", action="store_true", help="Print candidates/seeds only and write no files.")
    p.add_argument("--workflow-title", default=None, help="Override title in exported CryoSPARC workflow JSON.")
    p.add_argument("--workflow-description", default=None, help="Override description in exported workflow JSON.")
    p.add_argument("--compact-parameters", action="store_true",
                   help="Write only params whose value is non-null or flagged/visible. Default writes all params_base entries.")
    args = p.parse_args()
    if args.resolution_cutoff is None:
        args.resolution_cutoff = args.reolution_cutoff
    if args.resolution_cutoff is None:
        p.error("--resolution-cutoff is required; typo alias --reolution_cutoff is accepted")
    return args


def load_json(path: pathlib.Path) -> Dict[str, Any]:
    return json.loads(path.read_text())


def clean(x: Any) -> Any:
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, dict):
        if set(x.keys()) == {"$date"}:
            return x["$date"]
        return {k: clean(v) for k, v in x.items()}
    if isinstance(x, list):
        return [clean(v) for v in x]
    return x


def date_val(d: Dict[str, Any], key: str) -> Any:
    v = d.get(key)
    if isinstance(v, dict) and "$date" in v:
        return v["$date"]
    return v


def jnum(uid: str) -> int:
    m = re.match(r"J(\d+)$", uid or "")
    return int(m.group(1)) if m else 10**9


def short(s: Any, n: int = 80) -> str:
    if s is None:
        return ""
    s = str(s).replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def esc_dot(s: Any) -> str:
    return str(s).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def walk(obj: Any, path: str = "") -> Iterable[Tuple[str, Any]]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk(v, f"{path}/{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk(v, f"{path}[{i}]")
    else:
        yield path, obj


def np_load_cs(project_dir: pathlib.Path, metafile: str) -> Optional[Any]:
    try:
        import numpy as np
    except Exception:
        return None
    p = project_dir / metafile
    if not p.exists():
        return None
    try:
        return np.load(str(p), allow_pickle=True)
    except Exception:
        return None


def collect_resolution_measurements(d: Dict[str, Any], project_dir: pathlib.Path, suffixes: Tuple[str, ...]) -> List[Dict[str, Any]]:
    vals: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str, int, float]] = set()

    for path, value in walk(d):
        if any(path.endswith(suf) for suf in suffixes) and isinstance(value, (int, float)):
            fv = float(value)
            if math.isfinite(fv) and fv > 0:
                key = (path, "job.json", -1, round(fv, 6))
                if key not in seen:
                    seen.add(key)
                    vals.append({"resolution_A": fv, "field_path": path, "source": "job.json", "index": None})

    for result in d.get("output_results") or []:
        for metafile in result.get("metafiles") or []:
            arr = np_load_cs(project_dir, metafile)
            if arr is None or not getattr(arr, "dtype", None) or not arr.dtype.names:
                continue
            for name in arr.dtype.names:
                if not any(name.endswith(suf) for suf in suffixes):
                    continue
                try:
                    series = arr[name]
                except Exception:
                    continue
                try:
                    iterator = list(enumerate(series))
                except TypeError:
                    iterator = [(0, series)]
                for idx, value in iterator:
                    try:
                        fv = float(value)
                    except Exception:
                        continue
                    if math.isfinite(fv) and fv > 0:
                        key = (name, metafile, idx, round(fv, 6))
                        if key not in seen:
                            seen.add(key)
                            vals.append({
                                "resolution_A": fv, "field_path": name, "source": metafile,
                                "index": idx, "group_name": result.get("group_name"),
                                "result_name": result.get("name"),
                            })
    vals.sort(key=lambda r: r["resolution_A"])
    return vals


def best_reconstruction_resolution(d: Dict[str, Any], project_dir: pathlib.Path,
                                   recon_types: Set[str], suffixes: Tuple[str, ...]) -> Optional[Dict[str, Any]]:
    if (d.get("job_type") or d.get("type")) not in recon_types:
        return None
    vals = collect_resolution_measurements(d, project_dir, suffixes)
    return vals[0] if vals else None


def find_connections(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    edges: List[Dict[str, Any]] = []
    for group in d.get("input_slot_groups") or []:
        target_input = group.get("name")
        target_type = group.get("type")
        for conn_idx, conn in enumerate(group.get("connections") or []):
            src_uid = conn.get("job_uid")
            src_group = conn.get("group_name")
            slot_records: List[Dict[str, Any]] = []
            seen: Set[Tuple[Tuple[str, Any], ...]] = set()
            for slot in conn.get("slots") or []:
                rec = {
                    "slot_name": slot.get("slot_name"),
                    "result_name": slot.get("result_name"),
                    "result_type": slot.get("result_type"),
                    "source_job_uid": slot.get("job_uid") or src_uid,
                    "source_group": slot.get("group_name") or src_group,
                    "version": slot.get("version"),
                }
                key = tuple(rec.items())
                if key not in seen:
                    seen.add(key)
                    slot_records.append(rec)
            by_src: Dict[Tuple[Optional[str], Optional[str]], List[Dict[str, Any]]] = defaultdict(list)
            if slot_records:
                for rec in slot_records:
                    by_src[(rec["source_job_uid"], rec["source_group"])].append(rec)
            else:
                by_src[(src_uid, src_group)] = []
            for (su, sg), slots in by_src.items():
                if not su:
                    continue
                edges.append({
                    "source": su, "source_group": sg, "target": d.get("uid"),
                    "target_input": target_input, "target_type": target_type,
                    "connection_index": conn_idx, "slots": slots, "edge_kind": "input_connection",
                })
    return edges


def simplify_parameters(params_base: Dict[str, Any], compact: bool = False) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for name, spec in (params_base or {}).items():
        if not isinstance(spec, dict):
            continue
        visible = not bool(spec.get("hidden", False))
        value = clean(spec.get("value"))
        flagged = bool(spec.get("flagged", False))
        notes = spec.get("notes", "") or ""
        if compact and value is None and not visible and not flagged and not notes:
            continue
        out[name] = {
            "value": value,
            "locked": bool(spec.get("locked", False)),
            "visible": visible,
            "flagged": flagged,
            "notes": notes,
        }
    return out


def workflow_groups_for_job(uid: str, conn_edges: List[Dict[str, Any]], node_set: Set[str]) -> List[List[str]]:
    groups: List[List[str]] = []
    seen: Set[Tuple[str, str]] = set()
    for e in sorted([x for x in conn_edges if x["target"] == uid], key=lambda x: (jnum(x["source"]), x.get("source_group") or "", x.get("target_input") or "")):
        if e["source"] not in node_set:
            continue
        source_group = e.get("source_group") or ""
        target_input = e.get("target_input") or ""
        if not source_group or not target_input:
            continue
        pair = (f"{e['source']}.{source_group}", target_input)
        if pair not in seen:
            seen.add(pair)
            groups.append([pair[0], pair[1]])
    return groups


def build_workflow_json(project_dir: pathlib.Path, project_meta: Dict[str, Any], project_uid: str,
                        nodes: List[Dict[str, Any]], jobs: Dict[str, Dict[str, Any]],
                        conn_edges: List[Dict[str, Any]], node_set: Set[str], args: argparse.Namespace,
                        seed_records: List[Dict[str, Any]]) -> Dict[str, Any]:
    title = args.workflow_title or f"{project_uid} effective workflow <= {args.resolution_cutoff:g} A"
    seed_lines = [f"- {s['uid']} {s.get('resolution_A'):.4f} A {s.get('job_type')} {s.get('title') or ''}" for s in seed_records if s.get("resolution_A") is not None]
    description = args.workflow_description
    if description is None:
        description = (
            f"**Effective workflow extracted from {project_dir}**\n"
            f"- Project: {project_uid}\n"
            f"- Resolution cutoff: <= {args.resolution_cutoff:g} Å\n"
            f"- Seeds:\n" + "\n".join(seed_lines) + "\n"
            f"- Jobs are the union of all upstream input/parent/clone ancestors of the selected seeds.\n"
            f"- Parameters were copied from each job's params_base; external file paths may need editing before replay."
        )
    created_at = date_val(project_meta, "created_at") or datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    cs_version = project_meta.get("created_at_version") or next((jobs[n["uid"]].get("version") for n in nodes if jobs[n["uid"]].get("version")), "unknown")
    workflow_id = hashlib.sha1((str(project_dir) + project_uid + str(args.resolution_cutoff) + ",".join(n["uid"] for n in nodes)).encode()).hexdigest()[:24]

    wf_jobs: Dict[str, Any] = {}
    for n in sorted(nodes, key=lambda x: jnum(x["uid"])):
        uid = n["uid"]
        d = jobs[uid]
        wf_jobs[uid] = {
            "title": d.get("title") or "",
            "description": d.get("description") or "",
            "jobType": d.get("job_type") or d.get("type") or "",
            "groups": workflow_groups_for_job(uid, conn_edges, node_set),
            "individualResults": [],
            "parameters": simplify_parameters(d.get("params_base") or {}, compact=args.compact_parameters),
        }
    return {
        "_id": workflow_id,
        "title": title,
        "workflowVersion": "1.0.0",
        "reference": True,
        "category": "Default",
        "createdAt": created_at,
        "csVersion": cs_version,
        "description": description,
        "jobs": wf_jobs,
    }


def infer_project_uid(jobs: Dict[str, Dict[str, Any]], project_meta: Dict[str, Any]) -> Optional[str]:
    if project_meta.get("uid"):
        return project_meta["uid"]
    counts = Counter(d.get("project_uid") for d in jobs.values() if d.get("project_uid"))
    return counts.most_common(1)[0][0] if counts else None


def summarize_outputs(d: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for g in d.get("output_result_groups") or []:
        out.append({
            "name": g.get("name"), "type": g.get("type"), "title": g.get("title"),
            "num_items": g.get("num_items"), "summary": clean(g.get("summary") or {}),
            "latest_summary_stats_keys": sorted((g.get("latest_summary_stats") or {}).keys()),
        })
    return out


def main() -> int:
    args = parse_args()
    project_dir = pathlib.Path(args.project_dir).expanduser().resolve()
    if not project_dir.exists():
        raise SystemExit(f"Directory not found: {project_dir}")

    project_meta: Dict[str, Any] = {}
    if (project_dir / "project.json").exists():
        project_meta = load_json(project_dir / "project.json")

    all_jobs: Dict[str, Dict[str, Any]] = {}
    for p in project_dir.glob("J*/job.json"):
        d = load_json(p)
        if d.get("uid"):
            all_jobs[d["uid"]] = d
    if not all_jobs:
        raise SystemExit(f"No J*/job.json found under {project_dir}")

    project_uid = args.project_uid or infer_project_uid(all_jobs, project_meta)
    if not project_uid:
        raise SystemExit("Could not infer project UID; pass --project-uid")
    jobs = {uid: d for uid, d in all_jobs.items() if d.get("project_uid") == project_uid}

    scope_uids = []
    for uid, d in jobs.items():
        if uid in set(args.exclude_job or []):
            continue
        if (not args.include_deleted) and d.get("deleted", False):
            continue
        if args.workspace_uid and args.workspace_uid not in (d.get("workspace_uids") or []):
            continue
        scope_uids.append(uid)
    scope_uids = sorted(scope_uids, key=jnum)
    if not scope_uids:
        raise SystemExit(f"No jobs in scan scope for project {project_uid}")

    recon_types = {x.strip() for x in args.recon_types.split(",") if x.strip()}
    suffixes = tuple(x.strip() for x in args.resolution_fields.split(",") if x.strip())
    exclude_jobs = set(args.exclude_job or [])

    all_edges: List[Dict[str, Any]] = []
    for uid in scope_uids:
        all_edges.extend(find_connections(jobs[uid]))

    parent_edges: List[Dict[str, Any]] = []
    if not args.no_parent_metadata:
        for uid in scope_uids:
            d = jobs[uid]
            for p in d.get("parents") or []:
                if p in jobs:
                    parent_edges.append({"source": p, "target": uid, "edge_kind": "parent_field"})
            c = d.get("cloned_from")
            if c in jobs:
                parent_edges.append({"source": c, "target": uid, "edge_kind": "cloned_from"})
    seen_pc = set(); dedup_pc = []
    for e in parent_edges:
        k = (e["source"], e["target"], e["edge_kind"])
        if k not in seen_pc:
            seen_pc.add(k); dedup_pc.append(e)
    parent_edges = dedup_pc

    forced = set(args.seed_job or [])
    candidate_records: List[Dict[str, Any]] = []
    seed_records: List[Dict[str, Any]] = []
    for uid in scope_uids:
        d = jobs[uid]
        if args.active_only_output and d.get("deleted"):
            continue
        if (not args.include_noncompleted_seeds) and d.get("status") != "completed" and uid not in forced:
            continue
        best = best_reconstruction_resolution(d, project_dir, recon_types, suffixes)
        if not best and uid not in forced:
            continue
        rec = {
            "uid": uid, "job_type": d.get("job_type") or d.get("type"), "title": d.get("title"),
            "status": d.get("status"), "deleted": d.get("deleted", False),
            "workspace_uids": d.get("workspace_uids") or [],
            "resolution_A": best["resolution_A"] if best else None,
            "resolution_field": best["field_path"] if best else "forced_seed",
            "resolution_source": best.get("source") if best else "forced_seed",
            "resolution_index": best.get("index") if best else None,
        }
        candidate_records.append(rec)
        # Better/higher resolution means smaller Å number.
        if uid in forced or (best and best["resolution_A"] <= args.resolution_cutoff):
            seed_records.append(rec)
    candidate_records.sort(key=lambda r: (float("inf") if r["resolution_A"] is None else r["resolution_A"], jnum(r["uid"])))
    seed_records.sort(key=lambda r: (float("inf") if r["resolution_A"] is None else r["resolution_A"], jnum(r["uid"])))

    if args.print_seeds_only:
        print(json.dumps(clean({
            "project_uid": project_uid, "project_dir": str(project_dir), "workspace_uid_filter": args.workspace_uid,
            "resolution_cutoff_A": args.resolution_cutoff,
            "seed_reconstructions": seed_records, "candidate_reconstructions": candidate_records,
        }), indent=2, ensure_ascii=False))
        return 0
    if not seed_records:
        print(json.dumps(clean({
            "project_uid": project_uid, "project_dir": str(project_dir), "resolution_cutoff_A": args.resolution_cutoff,
            "seed_reconstructions": [], "candidate_reconstructions": candidate_records,
            "message": "No reconstruction seed met cutoff. Remember: better than cutoff means resolution_A <= cutoff.",
        }), indent=2, ensure_ascii=False))
        return 2

    pred: Dict[str, Set[str]] = defaultdict(set)
    for e in all_edges:
        if e["source"] in jobs and e["target"] in jobs:
            pred[e["target"]].add(e["source"])
    for e in parent_edges:
        if e["source"] in jobs and e["target"] in jobs:
            pred[e["target"]].add(e["source"])

    seed_uids = {s["uid"] for s in seed_records}
    ancestor_uids = set(seed_uids)
    q = deque(seed_uids)
    while q:
        u = q.popleft()
        for p in pred.get(u, set()):
            if p in exclude_jobs:
                continue
            if p not in jobs:
                continue
            if args.active_only_output and jobs[p].get("deleted"):
                continue
            # Upstream jobs outside the initial scope are allowed, so cross-workspace project deps are preserved.
            if (not args.include_deleted) and jobs[p].get("deleted", False):
                continue
            if p not in ancestor_uids:
                ancestor_uids.add(p); q.append(p)

    node_uids = sorted(ancestor_uids, key=jnum)
    node_set = set(node_uids)
    nodes: List[Dict[str, Any]] = []
    for uid in node_uids:
        d = jobs[uid]
        best = best_reconstruction_resolution(d, project_dir, recon_types, suffixes)
        nodes.append({
            "uid": uid, "uid_num": d.get("uid_num") or jnum(uid), "project_uid": d.get("project_uid"),
            "workspace_uids": d.get("workspace_uids") or [],
            "deleted": d.get("deleted", False), "selected_seed_reconstruction": uid in seed_uids,
            "best_reconstruction_resolution_A": best["resolution_A"] if best else None,
            "best_reconstruction_resolution_field": best["field_path"] if best else None,
            "best_reconstruction_resolution_source": best.get("source") if best else None,
            "job_type": d.get("job_type") or d.get("type"), "section": d.get("section"),
            "title": d.get("title"), "description": d.get("description"), "status": d.get("status"),
            "status_num": d.get("status_num"), "has_error": d.get("has_error"), "has_warning": d.get("has_warning"),
            "created_at": date_val(d, "created_at"), "started_at": date_val(d, "started_at"),
            "launched_at": date_val(d, "launched_at"), "completed_at": date_val(d, "completed_at"),
            "failed_at": date_val(d, "failed_at"), "killed_at": date_val(d, "killed_at"),
            "parents_field": d.get("parents") or [], "children_field": d.get("children") or [],
            "cloned_from": d.get("cloned_from"), "queued_to_lane": d.get("queued_to_lane"),
            "queued_to_hostname": d.get("queued_to_hostname"), "queued_to_gpu": d.get("queued_to_gpu"),
            "output_result_groups": summarize_outputs(d), "version": d.get("version"),
        })

    # Include edges among the final node set. Some upstream deps outside initial scope can only have
    # outgoing edges if they were in scope; direct child connections are still captured from target jobs.
    conn_edges = sorted([e for e in all_edges if e["source"] in node_set and e["target"] in node_set],
                        key=lambda e: (jnum(e["target"]), jnum(e["source"]), e.get("target_input") or ""))
    pc_edges = sorted([e for e in parent_edges if e["source"] in node_set and e["target"] in node_set],
                      key=lambda e: (jnum(e["target"]), jnum(e["source"]), e["edge_kind"]))

    cutoff_tag = str(args.resolution_cutoff).replace(".", "p")
    outdir = pathlib.Path(args.outdir).expanduser().resolve() if args.outdir else project_dir / f"{project_uid}_effective_workflow_le_{cutoff_tag}A"
    outdir.mkdir(parents=True, exist_ok=True)
    prefix = f"{project_uid}_effective_workflow_le_{cutoff_tag}A"

    workflow_json = build_workflow_json(project_dir, project_meta, project_uid, nodes, jobs, conn_edges, node_set, args, seed_records)
    workflow_path = outdir / f"{prefix}_cryosparc_workflow.json"
    workflow_path.write_text(json.dumps(clean(workflow_json), indent=2, ensure_ascii=False))

    flat_edges = []
    for e in conn_edges:
        slot_names, result_names, result_types = [], [], []
        for s in e["slots"]:
            if s.get("slot_name"): slot_names.append(s["slot_name"])
            if s.get("result_name"): result_names.append(s["result_name"])
            if s.get("result_type"): result_types.append(s["result_type"])
        flat_edges.append({
            "source": e["source"], "source_group": e.get("source_group") or "", "target": e["target"],
            "target_input": e.get("target_input") or "", "target_type": e.get("target_type") or "",
            "slot_names": ",".join(dict.fromkeys(slot_names)),
            "result_names": ",".join(dict.fromkeys(result_names)),
            "result_types": ",".join(dict.fromkeys(result_types)), "edge_kind": e["edge_kind"],
        })
    for e in pc_edges:
        flat_edges.append({"source": e["source"], "source_group": "", "target": e["target"], "target_input": "",
                           "target_type": "", "slot_names": "", "result_names": "", "result_types": "", "edge_kind": e["edge_kind"]})

    payload = {
        "project_uid": project_uid, "project_title": project_meta.get("title"), "source_project_dir": str(project_dir),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "selection_rule": {
            "meaning": "union of all upstream jobs for recon/refine jobs whose best reported resolution_A <= cutoff",
            "resolution_cutoff_A": args.resolution_cutoff,
            "workspace_uid_filter": args.workspace_uid,
            "include_deleted_scan": args.include_deleted,
            "trace_parent_metadata": not args.no_parent_metadata,
            "resolution_fields_considered": list(suffixes),
            "reconstruction_job_types": sorted(recon_types),
        },
        "candidate_reconstructions": candidate_records,
        "seed_reconstructions": seed_records,
        "summary": {
            "candidate_reconstruction_count": len(candidate_records),
            "seed_reconstruction_count": len(seed_records),
            "ancestor_union_node_count": len(nodes),
            "deleted_nodes_in_union": sum(1 for n in nodes if n["deleted"]),
            "input_connection_edge_count": len(conn_edges),
            "parent_clone_edge_count": len(pc_edges),
            "status_counts": dict(Counter(n["status"] for n in nodes)),
            "job_type_counts": dict(Counter(n["job_type"] for n in nodes)),
            "cryosparc_workflow_job_count": len(workflow_json["jobs"]),
        },
        "nodes": nodes,
        "input_connection_edges": clean(conn_edges),
        "parent_clone_edges": pc_edges,
    }
    graph_path = outdir / f"{prefix}_graph.json"
    graph_path.write_text(json.dumps(clean(payload), indent=2, ensure_ascii=False))

    node_path = outdir / f"{prefix}_nodes.tsv"
    with node_path.open("w", newline="") as f:
        fields = ["uid", "workspace_uids", "deleted", "selected_seed_reconstruction", "best_reconstruction_resolution_A",
                  "best_reconstruction_resolution_field", "job_type", "status", "title", "created_at", "completed_at",
                  "cloned_from", "parents_field", "children_field", "has_error", "has_warning"]
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t", extrasaction="ignore"); w.writeheader()
        for n in nodes:
            r = n.copy(); r["workspace_uids"] = ",".join(map(str, r.get("workspace_uids") or []))
            r["parents_field"] = ",".join(map(str, r.get("parents_field") or [])); r["children_field"] = ",".join(map(str, r.get("children_field") or []))
            w.writerow(r)

    edge_path = outdir / f"{prefix}_edges.tsv"
    with edge_path.open("w", newline="") as f:
        fields = ["source", "source_group", "target", "target_input", "target_type", "slot_names", "result_names", "result_types", "edge_kind"]
        w = csv.DictWriter(f, fieldnames=fields, delimiter="\t"); w.writeheader(); w.writerows(flat_edges)

    dot_path = outdir / f"{prefix}_graph.dot"
    dl = [
        f"digraph {project_uid}_effective_workflow {{",
        f"  graph [rankdir=LR, overlap=false, splines=true, fontsize=10, label=\"{project_uid} effective workflow <= {args.resolution_cutoff:g} A\", labelloc=t];",
        "  node [shape=box, style=\"rounded,filled\", fontsize=9, fontname=\"Helvetica\"];",
        "  edge [fontsize=8, fontname=\"Helvetica\", color=\"#555555\"];",
    ]
    for n in nodes:
        fill = "#ffeb99" if n["selected_seed_reconstruction"] else STATUS_COLORS.get(n.get("status"), "#f8f9fa")
        style = "rounded,filled,bold" if n["selected_seed_reconstruction"] else ("rounded,filled,dashed" if n.get("deleted") else "rounded,filled")
        res = f"\\n{n['best_reconstruction_resolution_A']:.3f} A" if n.get("best_reconstruction_resolution_A") else ""
        label = f"{n['uid']}\\n{short(n.get('job_type'), 28)}\\n{short(n.get('title'), 42)}\\n{n.get('status') or ''}{res}"
        dl.append(f'  "{n["uid"]}" [label="{esc_dot(label)}", fillcolor="{fill}", style="{style}"];')
    for e in conn_edges:
        lab = (e.get("source_group") or "") + "→" + (e.get("target_input") or "")
        dl.append(f'  "{e["source"]}" -> "{e["target"]}" [label="{esc_dot(lab)}"];')
    for e in pc_edges:
        dl.append(f'  "{e["source"]}" -> "{e["target"]}" [style=dashed, color="#999999", label="{e["edge_kind"]}"];')
    dl.append("}"); dot_path.write_text("\n".join(dl) + "\n")

    mmd_path = outdir / f"{prefix}_graph.mmd"
    ml = ["flowchart LR"]
    for n in nodes:
        res = f"<br/>{n['best_reconstruction_resolution_A']:.3f} A" if n.get("best_reconstruction_resolution_A") else ""
        label = f"{n['uid']}<br/>{short(n.get('job_type'), 24)}<br/>{short(n.get('title'), 40)}<br/>{n.get('status') or ''}{res}".replace('"', "'")
        ml.append(f'  {n["uid"]}["{label}"]')
    for e in conn_edges:
        lab = ((e.get("source_group") or "") + "→" + (e.get("target_input") or "")).replace('"', "'")
        ml.append(f'  {e["source"]} -->|"{lab}"| {e["target"]}')
    for e in pc_edges:
        ml.append(f'  {e["source"]} -. {e["edge_kind"]} .-> {e["target"]}')
    mmd_path.write_text("\n".join(ml) + "\n")

    md_path = outdir / f"{prefix}_summary.md"
    with md_path.open("w") as f:
        f.write(f"# {project_uid} effective workflow <= {args.resolution_cutoff:g} Å\n\n")
        f.write(f"Source: `{project_dir}`\n\n")
        f.write(f"CryoSPARC workflow JSON: `{workflow_path}`\n\n")
        f.write("## Seed reconstructions\n\n| UID | resolution_A | type | status | title | field/source |\n|---|---:|---|---|---|---|\n")
        for s in seed_records:
            res = "" if s["resolution_A"] is None else f"{s['resolution_A']:.4f}"
            f.write(f"| {s['uid']} | {res} | {s['job_type']} | {s['status']} | {short(s['title'], 80)} | `{s['resolution_field']}` / `{s['resolution_source']}` |\n")
        f.write("\n## Summary\n\n")
        for k, v in payload["summary"].items(): f.write(f"- {k}: {v}\n")
        f.write("\n## Jobs in exported workflow\n\n| UID | seed | status | type | resolution_A | title |\n|---|---|---|---|---:|---|\n")
        for n in nodes:
            res = "" if n.get("best_reconstruction_resolution_A") is None else f"{n['best_reconstruction_resolution_A']:.4f}"
            f.write(f"| {n['uid']} | {n['selected_seed_reconstruction']} | {n.get('status') or ''} | {n.get('job_type') or ''} | {res} | {short(n.get('title'), 90)} |\n")

    result = {
        "outdir": str(outdir),
        "cryosparc_workflow_json": str(workflow_path),
        "files": [str(workflow_path), str(graph_path), str(node_path), str(edge_path), str(dot_path), str(mmd_path), str(md_path)],
        "summary": payload["summary"],
        "seed_reconstructions": seed_records,
    }
    print(json.dumps(clean(result), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
