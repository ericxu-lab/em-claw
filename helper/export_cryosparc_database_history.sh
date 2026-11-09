#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# Export CryoSPARC project metadata + selected GridFS files
#
# Low-memory + pretty JSON + project input + ALL mode + parallel workers.
#
# Behavior:
# - PROJECT_UID=P123, or positional P123, exports only that project.
# - PROJECT_UID=ALL, positional ALL, or no project ID exports every project.
# - N / --parallel controls the number of concurrent worker processes.
# - Metadata is exported per project in parallel into isolated worker folders,
#   then assembled once after all workers complete. This avoids parallel writes
#   into the same final metadata tree.
# - Real GridFS PNG/PDF files, when EXPORT_REAL_FILES=1, are exported per job
#   in parallel.
#
# Safe behavior:
# - Does not delete or modify CryoSPARC database
# - Writes only under OUTROOT
# - Recreates generated metadata/summary/job-list output under OUTROOT each run
# - Streams Mongo records and GridFS chunks to avoid huge allocations
# - Saves JSON files in pretty/indented format and validates them after export
# ============================================================

usage() {
    cat <<'USAGE'
Usage:
  bash export_cryosparc_database_history.sh [OUTROOT] [PROJECT_UID|ALL] [N]
  bash export_cryosparc_database_history.sh --out OUTROOT --project PROJECT_UID|ALL --parallel N

Examples:
  # One project, metadata only
  bash export_cryosparc_database_history.sh ./result P123

  # All projects, metadata only, with 4 parallel project workers
  bash export_cryosparc_database_history.sh ./result_ALL ALL 4

  # All projects, metadata + PNG/PDF GridFS files, with 4 parallel workers
  EXPORT_REAL_FILES=1 bash export_cryosparc_database_history.sh ./result_ALL ALL 4

  # Named options
  EXPORT_REAL_FILES=1 bash export_cryosparc_database_history.sh --out ./result_ALL --project ALL --parallel 4

  # Environment variables are also supported
  PROJECT_UID=P123 PARALLEL_JOBS=4 EXPORT_REAL_FILES=1 bash export_cryosparc_database_history.sh ./result

Arguments:
  OUTROOT      Output directory. Default: $HOME/test/cryosparc_selected_export
  PROJECT_UID CryoSPARC project ID/UID to export, for example P123.
              Use ALL to explicitly export every project in the database.
              If omitted, all projects are exported.
  N            Number of parallel worker processes. Default: 1.
              Must be a positive integer.

Options:
  -o, --out OUTROOT
  -p, --project PROJECT_UID|ALL
  -j, --jobs, --parallel, --processes N
USAGE
}

OUTROOT=""
TARGET_PROJECT_UID="${PROJECT_UID:-}"
PARALLEL_JOBS="${PARALLEL_JOBS:-${N:-1}}"
PARALLEL_JOBS_FROM_OPTION=0

POSITIONAL=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)
            usage
            exit 0
            ;;
        -o|--out|--outroot)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: $1 requires an output directory." >&2
                usage >&2
                exit 2
            fi
            OUTROOT="$2"
            shift 2
            ;;
        -p|--project|--project-id|--project-uid)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: $1 requires a CryoSPARC project UID or ALL." >&2
                usage >&2
                exit 2
            fi
            TARGET_PROJECT_UID="$2"
            shift 2
            ;;
        -j|--jobs|--parallel|--processes)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: $1 requires a positive integer." >&2
                usage >&2
                exit 2
            fi
            PARALLEL_JOBS="$2"
            PARALLEL_JOBS_FROM_OPTION=1
            shift 2
            ;;
        --)
            shift
            while [[ $# -gt 0 ]]; do
                POSITIONAL+=("$1")
                shift
            done
            ;;
        -*)
            echo "ERROR: Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
        *)
            POSITIONAL+=("$1")
            shift
            ;;
    esac
done

if (( ${#POSITIONAL[@]} > 0 )); then
    OUTROOT="${OUTROOT:-${POSITIONAL[0]}}"
fi

if (( ${#POSITIONAL[@]} > 1 )); then
    TARGET_PROJECT_UID="${TARGET_PROJECT_UID:-${POSITIONAL[1]}}"
fi

if (( ${#POSITIONAL[@]} > 2 )); then
    if (( PARALLEL_JOBS_FROM_OPTION == 1 )); then
        echo "ERROR: Parallel worker count was supplied both positionally and with --parallel." >&2
        usage >&2
        exit 2
    fi
    PARALLEL_JOBS="${POSITIONAL[2]}"
fi

if (( ${#POSITIONAL[@]} > 3 )); then
    echo "ERROR: Too many positional arguments." >&2
    usage >&2
    exit 2
fi

OUTROOT="${OUTROOT:-$HOME/test/cryosparc_selected_export}"
EXPORT_REAL_FILES="${EXPORT_REAL_FILES:-0}"
KEEP_RAW="${KEEP_RAW:-0}"

case "$TARGET_PROJECT_UID" in
    ALL|all|All)
        TARGET_PROJECT_UID=""
        PROJECT_SELECTION_LABEL="all projects (PROJECT_UID=ALL)"
        ;;
    "")
        PROJECT_SELECTION_LABEL="all projects"
        ;;
    *)
        PROJECT_SELECTION_LABEL="$TARGET_PROJECT_UID"
        ;;
esac

if ! [[ "$PARALLEL_JOBS" =~ ^[1-9][0-9]*$ ]]; then
    echo "ERROR: Parallel worker count N must be a positive integer; got '$PARALLEL_JOBS'." >&2
    usage >&2
    exit 2
fi

json_escape() {
    python3 - "$1" <<'PY'
import json
import sys
value = sys.argv[1]
print("null" if value == "" else json.dumps(value))
PY
}

safe_name() {
    printf '%s' "$1" | sed 's/[^A-Za-z0-9._-]/_/g'
}

mkdir -p "$OUTROOT"

METADATA_DIR="$OUTROOT/project_metadata"
FILES_DIR="$OUTROOT/project_gridfs_files"
PROJECT_RAW_DIR="$OUTROOT/project_metadata_raw"
PROJECT_WORK_DIR="$OUTROOT/project_metadata_work"
JOB_RAW_DIR="$OUTROOT/job_raw_base64"
PROJECT_LIST_RAW="$OUTROOT/project_uids.raw.txt"
PROJECT_LIST="$OUTROOT/project_uids.txt"
JOB_LIST="$OUTROOT/selected_project_jobs.tsv"
JOB_PARTS_DIR="$OUTROOT/selected_project_jobs_parts"
SUMMARY_PARTS_DIR="$OUTROOT/export_summary_parts"
VALIDATION_REPORT="$OUTROOT/json_validation_errors.txt"

# Recreate generated metadata output every run so stale compact/corrupt JSON cannot survive.
rm -rf "$METADATA_DIR" "$PROJECT_WORK_DIR" "$JOB_PARTS_DIR" "$SUMMARY_PARTS_DIR"
mkdir -p "$METADATA_DIR" "$FILES_DIR" "$PROJECT_RAW_DIR" "$PROJECT_WORK_DIR" "$JOB_RAW_DIR" "$JOB_PARTS_DIR" "$SUMMARY_PARTS_DIR"
rm -f "$JOB_LIST" "$OUTROOT/export_summary.json" "$OUTROOT/export_summary.tsv" "$VALIDATION_REPORT"

printf 'Output root: %s\n' "$OUTROOT"
printf 'Target project UID: %s\n' "$PROJECT_SELECTION_LABEL"
printf 'Parallel workers: %s\n' "$PARALLEL_JOBS"
printf 'EXPORT_REAL_FILES=%s\n' "$EXPORT_REAL_FILES"
printf 'KEEP_RAW=%s\n\n' "$KEEP_RAW"

# ------------------------------------------------------------
# 1. Build project list
# ------------------------------------------------------------

if [[ -z "$TARGET_PROJECT_UID" ]]; then
    cat > "$OUTROOT/list_projects.js" <<'EOF'
function asString(x) {
    if (x === undefined || x === null) return null;
    return String(x);
}

db.projects.find({}).forEach(function(p) {
    var project_uid = p.uid || p.project_uid || p._id;
    project_uid = asString(project_uid);
    if (project_uid) print("###PROJECT_UID###" + project_uid);
});
EOF

    echo "[1/7] Listing all CryoSPARC projects..."
    cryosparcm mongo < "$OUTROOT/list_projects.js" > "$PROJECT_LIST_RAW"
    grep '^###PROJECT_UID###' "$PROJECT_LIST_RAW" | sed 's/^###PROJECT_UID###//' | awk 'NF && !seen[$0]++ {print}' > "$PROJECT_LIST" || true
else
    echo "[1/7] Preparing single-project export list..."
    printf '%s\n' "$TARGET_PROJECT_UID" > "$PROJECT_LIST"
fi

PROJECT_COUNT="$(awk 'NF {count += 1} END {print count + 0}' "$PROJECT_LIST")"
if [[ "$PROJECT_COUNT" == "0" ]]; then
    echo "ERROR: No CryoSPARC projects found to export." >&2
    exit 1
fi

printf 'Projects to process: %s\n\n' "$PROJECT_COUNT"

# ------------------------------------------------------------
# 2. Create metadata splitter for one project worker
# ------------------------------------------------------------

cat > "$OUTROOT/split_one_project_metadata_stream.py" <<'PY'
from pathlib import Path
import json
import re
import sys
from collections import defaultdict

raw = Path(sys.argv[1])
worker_metadata_root = Path(sys.argv[2])
job_part = Path(sys.argv[3])
summary_part = Path(sys.argv[4])

worker_metadata_root.mkdir(parents=True, exist_ok=True)
job_part.parent.mkdir(parents=True, exist_ok=True)
summary_part.parent.mkdir(parents=True, exist_ok=True)

array_sections = [
    "workspaces",
    "jobs",
    "selected_jobs_for_gridfs",
    "events",
    "workflows",
    "reports",
    "fs_files",
]

count_keys = {
    "jobs": "jobs",
    "selected_jobs_for_gridfs": "selected_jobs_for_gridfs",
    "workspaces": "workspaces",
    "events": "events",
    "workflows": "workflows",
    "reports": "reports",
    "fs_files": "fs_files",
}

writers = {}
first_item = {}
projects_seen = {}
initialized_projects = set()
counts = defaultdict(lambda: defaultdict(int))
fs_job_counts = defaultdict(int)
failed_lines = 0


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))


def project_dir_for(project_uid):
    safe_project_uid = safe_name(project_uid)
    project_dir = worker_metadata_root / safe_project_uid
    project_dir.mkdir(parents=True, exist_ok=True)

    if project_uid not in initialized_projects:
        # This worker directory is isolated, but clear known files anyway.
        for name in array_sections + ["project", "target_job_types", "counts"]:
            stale = project_dir / f"{name}.json"
            if stale.exists():
                stale.unlink()
        initialized_projects.add(project_uid)

    projects_seen[project_uid] = project_dir
    return project_dir


def get_writer(project_uid, section):
    key = (project_uid, section)
    if key in writers:
        return writers[key]
    project_dir = project_dir_for(project_uid)
    fh = (project_dir / f"{section}.json").open("w", encoding="utf-8")
    fh.write("[\n")
    writers[key] = fh
    first_item[key] = True
    return fh


def write_array_doc(project_uid, section, doc):
    fh = get_writer(project_uid, section)
    key = (project_uid, section)
    if not first_item[key]:
        fh.write(",\n")
    pretty = json.dumps(doc, indent=2, ensure_ascii=False, default=str)
    # Indent each object within the surrounding JSON array.
    fh.write("\n".join("  " + line if line else line for line in pretty.splitlines()))
    first_item[key] = False
    if section in count_keys:
        counts[project_uid][count_keys[section]] += 1


with raw.open("r", encoding="utf-8", errors="replace") as fh:
    for line in fh:
        if not line.startswith("###DOC###"):
            continue
        payload = line[len("###DOC###"):].strip()
        try:
            rec = json.loads(payload)
        except Exception:
            failed_lines += 1
            continue

        project_uid = rec.get("project_uid")
        section = rec.get("section")
        doc = rec.get("doc")
        if not project_uid or not section:
            failed_lines += 1
            continue

        project_dir = project_dir_for(project_uid)

        if section == "project":
            (project_dir / "project.json").write_text(
                json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
        elif section == "target_job_types":
            (project_dir / "target_job_types.json").write_text(
                json.dumps(doc, indent=2, ensure_ascii=False, default=str) + "\n",
                encoding="utf-8",
            )
        elif section in array_sections:
            write_array_doc(project_uid, section, doc)
            if section == "fs_files" and isinstance(doc, dict):
                job_uid = doc.get("job_uid")
                content_type = doc.get("contentType")
                if job_uid and content_type in ("image/png", "application/pdf"):
                    fs_job_counts[(project_uid, str(job_uid))] += 1
        else:
            failed_lines += 1

for (project_uid, section), fh in list(writers.items()):
    fh.write("\n]\n")
    fh.close()

for project_uid, project_dir in projects_seen.items():
    for section in array_sections:
        path = project_dir / f"{section}.json"
        if not path.exists():
            path.write_text("[]\n", encoding="utf-8")

    c = {k: int(counts[project_uid].get(k, 0)) for k in [
        "jobs",
        "selected_jobs_for_gridfs",
        "workspaces",
        "events",
        "workflows",
        "reports",
        "fs_files",
    ]}
    (project_dir / "counts.json").write_text(
        json.dumps(c, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

if not projects_seen:
    raise SystemExit(f"No project metadata records found in {raw}")

summary_rows = []
for project_uid in sorted(projects_seen):
    c = counts[project_uid]
    summary_rows.append({
        "project_uid": project_uid,
        "jobs": int(c.get("jobs", 0)),
        "selected_jobs_for_gridfs": int(c.get("selected_jobs_for_gridfs", 0)),
        "workspaces": int(c.get("workspaces", 0)),
        "events": int(c.get("events", 0)),
        "workflows": int(c.get("workflows", 0)),
        "reports": int(c.get("reports", 0)),
        "fs_files": int(c.get("fs_files", 0)),
        "metadata_parse_failed_lines": failed_lines,
    })

summary_part.write_text(
    json.dumps(summary_rows, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)

with job_part.open("w", encoding="utf-8") as f:
    for (project_uid, job_uid), nfiles in sorted(fs_job_counts.items()):
        f.write(f"{project_uid}\t{job_uid}\t{nfiles}\n")

print("metadata projects:", len(projects_seen))
print("metadata parse failed lines:", failed_lines)
print("metadata output:", worker_metadata_root)
print("json format: pretty/indented")
print("jobs with selected GridFS files:", len(fs_job_counts))
PY

# ------------------------------------------------------------
# 3. Create metadata worker for one project
# ------------------------------------------------------------

cat > "$OUTROOT/export_one_project_metadata_worker.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

OUTROOT="$1"
PROJECT_WORK_DIR="$2"
PROJECT_RAW_DIR="$3"
JOB_PARTS_DIR="$4"
SUMMARY_PARTS_DIR="$5"
PROJECT_UID="$6"

json_escape() {
    python3 - "$1" <<'PY'
import json
import sys
value = sys.argv[1]
print("null" if value == "" else json.dumps(value))
PY
}

safe_name() {
    printf '%s' "$1" | sed 's/[^A-Za-z0-9._-]/_/g'
}

SAFE_PROJECT="$(safe_name "$PROJECT_UID")"
WORKER_METADATA_ROOT="$PROJECT_WORK_DIR/$SAFE_PROJECT"
PROJECT_UID_JSON="$(json_escape "$PROJECT_UID")"
JS_PROJECT="$PROJECT_RAW_DIR/${SAFE_PROJECT}.metadata.js"
RAW_PROJECT="$PROJECT_RAW_DIR/${SAFE_PROJECT}.metadata.raw.txt"
LOG_PROJECT="$PROJECT_RAW_DIR/${SAFE_PROJECT}.metadata.log"
JOB_PART="$JOB_PARTS_DIR/${SAFE_PROJECT}.tsv"
SUMMARY_PART="$SUMMARY_PARTS_DIR/${SAFE_PROJECT}.json"

mkdir -p "$WORKER_METADATA_ROOT"
rm -rf "$WORKER_METADATA_ROOT"/*
rm -f "$JOB_PART" "$SUMMARY_PART"

cat > "$JS_PROJECT" <<EOF
var TARGET_PROJECT_UID = $PROJECT_UID_JSON;

var TARGET_JOB_TYPES = [
    "class_2D",
    "class_2D_new",
    "select_2D",
    "reference_select_2D",
    "homo_abinit",
    "hetero_refine",
    "homo_refine_new",
    "nonuniform_refine_new",
    "class_3D",
    "new_local_refine",
    "local_refine",
    "naive_local_refine",
    "align_3D_new",
    "homo_reconstruct"
];

function asString(x) {
    if (x === undefined || x === null) return null;
    return String(x);
}

function pushUnique(arr, value) {
    if (value === undefined || value === null) return;
    for (var i = 0; i < arr.length; i++) {
        if (arr[i] === value) return;
    }
    arr.push(value);
}

function emit(project_uid, section, doc) {
    print("###DOC###" + JSON.stringify({
        project_uid: asString(project_uid),
        section: section,
        doc: doc
    }));
}

var project_terms = [
    {uid: TARGET_PROJECT_UID},
    {project_uid: TARGET_PROJECT_UID},
    {_id: TARGET_PROJECT_UID}
];

try {
    project_terms.push({_id: ObjectId(TARGET_PROJECT_UID)});
} catch (e) {
    // Not a Mongo ObjectId; string matching above is enough.
}

var found_projects = 0;

db.projects.find({\$or: project_terms}).forEach(function(p) {
    found_projects += 1;

    var canonical_project_uid = asString(p.uid || p.project_uid || p._id);
    if (!canonical_project_uid) return;

    var project_query_values = [];
    pushUnique(project_query_values, p.uid);
    pushUnique(project_query_values, p.project_uid);
    pushUnique(project_query_values, p._id);
    pushUnique(project_query_values, canonical_project_uid);

    print("###PROJECT_START###" + canonical_project_uid);
    emit(canonical_project_uid, "target_job_types", TARGET_JOB_TYPES);
    emit(canonical_project_uid, "project", p);

    var all_job_uids = [];
    var selected_job_uids = [];

    db.jobs.find({project_uid: {\$in: project_query_values}}).forEach(function(j) {
        emit(canonical_project_uid, "jobs", j);
        var job_uid = j.uid || j.job_uid || j._id;
        if (job_uid) {
            pushUnique(all_job_uids, job_uid);
            pushUnique(all_job_uids, asString(job_uid));
        }

        var jt = j.job_type || j.type;
        if (TARGET_JOB_TYPES.indexOf(jt) >= 0) {
            emit(canonical_project_uid, "selected_jobs_for_gridfs", j);
            if (job_uid) {
                pushUnique(selected_job_uids, job_uid);
                pushUnique(selected_job_uids, asString(job_uid));
            }
        }
    });

    db.workspaces.find({project_uid: {\$in: project_query_values}}).forEach(function(w) {
        emit(canonical_project_uid, "workspaces", w);
    });

    db.workflows.find({project_uid: {\$in: project_query_values}}).forEach(function(wf) {
        emit(canonical_project_uid, "workflows", wf);
    });

    db.reports.find({project_uid: {\$in: project_query_values}}).forEach(function(r) {
        emit(canonical_project_uid, "reports", r);
    });

    db.events.find({
        \$or: [
            {project_uid: {\$in: project_query_values}},
            {job_uid: {\$in: all_job_uids}}
        ]
    }).forEach(function(ev) {
        emit(canonical_project_uid, "events", ev);
    });

    db.fs.files.find({
        project_uid: {\$in: project_query_values},
        job_uid: {\$in: selected_job_uids},
        contentType: {\$in: ["image/png", "application/pdf"]}
    }).forEach(function(f) {
        emit(canonical_project_uid, "fs_files", f);
    });

    print("###PROJECT_END###" + canonical_project_uid);
});

if (found_projects === 0) {
    print("###NO_PROJECT_FOUND###" + TARGET_PROJECT_UID);
}
EOF

printf 'Exporting metadata for project %s\n' "$PROJECT_UID"
cryosparcm mongo < "$JS_PROJECT" > "$RAW_PROJECT" 2> "$LOG_PROJECT"

if ! grep -q '^###PROJECT_START###' "$RAW_PROJECT"; then
    echo "ERROR: No CryoSPARC project matched PROJECT_UID='$PROJECT_UID'." >&2
    exit 1
fi

python3 "$OUTROOT/split_one_project_metadata_stream.py" "$RAW_PROJECT" "$WORKER_METADATA_ROOT" "$JOB_PART" "$SUMMARY_PART"

# Keep non-empty logs because they may contain useful allocator warnings or Mongo errors.
if [[ ! -s "$LOG_PROJECT" ]]; then
    rm -f "$LOG_PROJECT"
fi

if [[ "${KEEP_METADATA_JS:-0}" != "1" ]]; then
    rm -f "$JS_PROJECT"
fi
SH
chmod +x "$OUTROOT/export_one_project_metadata_worker.sh"

# ------------------------------------------------------------
# 4. Export metadata per project in parallel
# ------------------------------------------------------------

echo "[2/7] Exporting project metadata with $PARALLEL_JOBS parallel worker(s)..."
awk 'NF {print}' "$PROJECT_LIST" \
    | xargs -r -n 1 -P "$PARALLEL_JOBS" "$OUTROOT/export_one_project_metadata_worker.sh" \
        "$OUTROOT" "$PROJECT_WORK_DIR" "$PROJECT_RAW_DIR" "$JOB_PARTS_DIR" "$SUMMARY_PARTS_DIR"

# ------------------------------------------------------------
# 5. Assemble metadata, aggregate summary, and validate JSON
# ------------------------------------------------------------

cat > "$OUTROOT/assemble_and_validate_metadata.py" <<'PY'
from pathlib import Path
import json
import shutil
import sys

outroot = Path(sys.argv[1])
project_work_dir = Path(sys.argv[2])
metadata_dir = Path(sys.argv[3])
summary_parts_dir = Path(sys.argv[4])
job_parts_dir = Path(sys.argv[5])
job_list = Path(sys.argv[6])
validation_report = Path(sys.argv[7])

metadata_dir.mkdir(parents=True, exist_ok=True)

# Copy isolated worker project directories into the final metadata directory.
project_dirs = []
for worker_root in sorted(project_work_dir.iterdir() if project_work_dir.exists() else []):
    if not worker_root.is_dir():
        continue
    for project_dir in sorted(worker_root.iterdir()):
        if not project_dir.is_dir():
            continue
        dest = metadata_dir / project_dir.name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(project_dir, dest)
        project_dirs.append(dest)

summary = []
for path in sorted(summary_parts_dir.glob("*.json")):
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"WARNING: failed to parse summary part {path}: {e}", file=sys.stderr)
        continue
    if isinstance(rows, list):
        summary.extend(rows)

# Remove exact duplicate rows if a project was listed more than once.
seen_projects = set()
deduped = []
for row in sorted(summary, key=lambda row: str(row.get("project_uid", ""))):
    project_uid = str(row.get("project_uid", ""))
    if project_uid in seen_projects:
        continue
    seen_projects.add(project_uid)
    deduped.append(row)
summary = deduped

(outroot / "export_summary.json").write_text(
    json.dumps(summary, indent=2, ensure_ascii=False) + "\n",
    encoding="utf-8",
)

fields = [
    "project_uid",
    "jobs",
    "selected_jobs_for_gridfs",
    "workspaces",
    "events",
    "workflows",
    "reports",
    "fs_files",
]

with (outroot / "export_summary.tsv").open("w", encoding="utf-8") as f:
    f.write("\t".join(fields) + "\n")
    for row in summary:
        f.write("\t".join(str(row.get(k, "")) for k in fields) + "\n")

with job_list.open("w", encoding="utf-8") as f:
    f.write("project_uid\tjob_uid\tfs_files\n")
    seen_jobs = set()
    for part in sorted(job_parts_dir.glob("*.tsv")):
        for line in part.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            fields_line = line.rstrip("\n").split("\t")
            if len(fields_line) < 3:
                continue
            key = tuple(fields_line[:2])
            if key in seen_jobs:
                continue
            seen_jobs.add(key)
            f.write("\t".join(fields_line[:3]) + "\n")

# Validate every generated JSON file.
errors = []
json_files = sorted(metadata_dir.rglob("*.json")) + [outroot / "export_summary.json"]
for path in json_files:
    try:
        with path.open("r", encoding="utf-8") as fh:
            json.load(fh)
    except Exception as e:
        errors.append(f"{path}: {e}")

if errors:
    validation_report.write_text("\n".join(errors) + "\n", encoding="utf-8")
    print(f"ERROR: {len(errors)} JSON file(s) failed validation. See {validation_report}", file=sys.stderr)
    raise SystemExit(1)
else:
    validation_report.write_text("All generated JSON files parsed successfully.\n", encoding="utf-8")

print("metadata projects:", len(summary))
print("project metadata dirs:", len(project_dirs))
print("summary:", outroot / "export_summary.tsv")
print("selected job list:", job_list)
print("json validation:", validation_report)
PY

echo "[3/7] Assembling metadata, aggregating summary, and validating JSON..."
python3 "$OUTROOT/assemble_and_validate_metadata.py" \
    "$OUTROOT" "$PROJECT_WORK_DIR" "$METADATA_DIR" "$SUMMARY_PARTS_DIR" "$JOB_PARTS_DIR" "$JOB_LIST" "$VALIDATION_REPORT"

if [[ "$EXPORT_REAL_FILES" != "1" ]]; then
    echo
    echo "[4/7] Skipping real GridFS file export."
    echo "Set EXPORT_REAL_FILES=1 to export PNG/PDF files per job."
    echo
    echo "[5/7] Done."
    echo "Metadata directory:"
    echo "  $METADATA_DIR"
    echo "Summary:"
    echo "  $OUTROOT/export_summary.tsv"
    echo "Selected job list:"
    echo "  $JOB_LIST"
    echo "JSON validation report:"
    echo "  $VALIDATION_REPORT"
    exit 0
fi

# ------------------------------------------------------------
# 6. Create streaming decoder for one GridFS job
# ------------------------------------------------------------

cat > "$OUTROOT/decode_one_job_stream.py" <<'PY'
import base64
import json
import re
from pathlib import Path
import sys

files_dir = Path(sys.argv[1])
project_uid = sys.argv[2]
job_uid = sys.argv[3]


def safe_name(value):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value))


safe_project = safe_name(project_uid)
safe_job = safe_name(job_uid)
outdir = files_dir / safe_project / safe_job
outdir.mkdir(parents=True, exist_ok=True)

fh = None
count = 0


def unique_path(directory, filename):
    safe_filename = safe_name(filename or "file")
    candidate = directory / safe_filename
    if not candidate.exists():
        return candidate
    stem = candidate.stem
    suffix = candidate.suffix
    i = 1
    while True:
        candidate = directory / f"{stem}_{i}{suffix}"
        if not candidate.exists():
            return candidate
        i += 1


try:
    for raw_line in sys.stdin:
        line = raw_line.rstrip("\n")

        if line.startswith("###FILE_BEGIN###"):
            if fh is not None:
                fh.close()
                fh = None
            payload = line[len("###FILE_BEGIN###"):].strip()
            current = json.loads(payload)
            filename = current.get("filename") or f"file_{count}"
            outpath = unique_path(outdir, filename)
            fh = outpath.open("wb")
            count += 1

        elif line.startswith("###CHUNK###"):
            if fh is None:
                continue
            chunk_b64 = line[len("###CHUNK###"):].strip()
            if chunk_b64:
                fh.write(base64.b64decode(chunk_b64))

        elif line.startswith("###FILE_END###"):
            if fh is not None:
                fh.close()
                fh = None

        else:
            continue
finally:
    if fh is not None:
        fh.close()

print(f"decoded {count} files for {project_uid}/{job_uid} -> {outdir}")
PY

# ------------------------------------------------------------
# 7. Create GridFS worker for one job
# ------------------------------------------------------------

cat > "$OUTROOT/export_one_gridfs_job_worker.sh" <<'SH'
#!/usr/bin/env bash
set -euo pipefail

OUTROOT="$1"
FILES_DIR="$2"
JOB_RAW_DIR="$3"
KEEP_RAW="$4"
PROJECT_UID="$5"
JOB_UID="$6"
NFILES="$7"

json_escape() {
    python3 - "$1" <<'PY'
import json
import sys
value = sys.argv[1]
print("null" if value == "" else json.dumps(value))
PY
}

safe_name() {
    printf '%s' "$1" | sed 's/[^A-Za-z0-9._-]/_/g'
}

SAFE_PROJECT="$(safe_name "$PROJECT_UID")"
SAFE_JOB="$(safe_name "$JOB_UID")"

RAW_JOB="$JOB_RAW_DIR/${SAFE_PROJECT}__${SAFE_JOB}.raw.txt"
JS_JOB="$JOB_RAW_DIR/${SAFE_PROJECT}__${SAFE_JOB}.js"
LOG_JOB="$JOB_RAW_DIR/${SAFE_PROJECT}__${SAFE_JOB}.log"

PROJECT_UID_JSON="$(json_escape "$PROJECT_UID")"
JOB_UID_JSON="$(json_escape "$JOB_UID")"

printf 'Exporting GridFS files for %s / %s (%s files)\n' "$PROJECT_UID" "$JOB_UID" "$NFILES"

cat > "$JS_JOB" <<EOF
var project_uid = $PROJECT_UID_JSON;
var job_uid = $JOB_UID_JSON;

function emitFileBegin(f) {
    print("###FILE_BEGIN###" + JSON.stringify({
        filename: f.filename,
        contentType: f.contentType,
        length: f.length,
        uploadDate: f.uploadDate,
        job_uid: f.job_uid,
        project_uid: f.project_uid
    }));
}

db.fs.files.find({
    project_uid: project_uid,
    job_uid: job_uid,
    contentType: {\$in: ["image/png", "application/pdf"]}
}).forEach(function(f) {
    emitFileBegin(f);

    db.fs.chunks.find({files_id: f._id}).sort({n: 1}).forEach(function(c) {
        print("###CHUNK###" + c.data.base64());
    });

    print("###FILE_END###");
});
EOF

if [[ "$KEEP_RAW" == "1" ]]; then
    cryosparcm mongo < "$JS_JOB" \
        2> "$LOG_JOB" \
        | tee "$RAW_JOB" \
        | python3 "$OUTROOT/decode_one_job_stream.py" "$FILES_DIR" "$PROJECT_UID" "$JOB_UID"
else
    cryosparcm mongo < "$JS_JOB" \
        2> "$LOG_JOB" \
        | python3 "$OUTROOT/decode_one_job_stream.py" "$FILES_DIR" "$PROJECT_UID" "$JOB_UID"
    rm -f "$RAW_JOB"
fi

if [[ "$KEEP_RAW" != "1" ]]; then
    rm -f "$JS_JOB"
fi

if [[ ! -s "$LOG_JOB" ]]; then
    rm -f "$LOG_JOB"
fi
SH
chmod +x "$OUTROOT/export_one_gridfs_job_worker.sh"

# ------------------------------------------------------------
# 8. Export real GridFS files in parallel
# ------------------------------------------------------------

echo "[4/7] Exporting real GridFS PNG/PDF files with $PARALLEL_JOBS parallel worker(s)..."
echo "Raw per-job files directory: $JOB_RAW_DIR"
echo "Decoded files directory: $FILES_DIR"
echo

JOB_COUNT="$(tail -n +2 "$JOB_LIST" | awk -F '\t' 'NF >= 3 && $1 != "" && $2 != "" {count += 1} END {print count + 0}')"

if [[ "$JOB_COUNT" == "0" ]]; then
    echo "No selected GridFS PNG/PDF files found for export."
else
    echo "GridFS jobs to export: $JOB_COUNT"
    tail -n +2 "$JOB_LIST" \
        | awk -F '\t' 'NF >= 3 && $1 != "" && $2 != "" {print $1, $2, $3}' \
        | xargs -r -n 3 -P "$PARALLEL_JOBS" "$OUTROOT/export_one_gridfs_job_worker.sh" \
            "$OUTROOT" "$FILES_DIR" "$JOB_RAW_DIR" "$KEEP_RAW"
fi

# ------------------------------------------------------------
# 9. Final summary
# ------------------------------------------------------------

echo
echo "[5/7] Checking outputs..."
find "$FILES_DIR" -type f | head -n 20 || true

echo
echo "[6/7] JSON validation report:"
echo "  $VALIDATION_REPORT"

echo
echo "[7/7] Done."
echo
echo "Metadata directory:"
echo "  $METADATA_DIR"
echo
echo "Real GridFS files directory:"
echo "  $FILES_DIR"
echo
echo "Summary:"
echo "  $OUTROOT/export_summary.tsv"
echo
echo "Selected project/job list:"
echo "  $JOB_LIST"
echo
echo "Disk usage:"
du -sh "$OUTROOT" || true
