# Helper scripts: build the raw history for the history bank

EM-Claw's subR agent learns from an offline **history bank** of past CryoSPARC
projects (`YOUR_HISTORY_BANK_PATH` in `profiles/cryosparc-agent/.claude/agents/subR.md`).
These two scripts collect the raw history that the bank is built from. Use
**either** one, depending on what you can access:

| Script | Reads | Use it when |
| --- | --- | --- |
| `export_cryosparc_database_history.sh` | the CryoSPARC database, through `cryosparcm mongo` | you can run commands as the CryoSPARC owner on the master node |
| `extract_effective_cryosparc_flow.py` | CryoSPARC project folders (`CS-*`, with `project.json` and `J*/job.json`) | you have read access to the project folders |

## Why they run outside the agent

Collecting history needs access the agent deliberately does not have: the
CryoSPARC database, or many project folders across the site. For safety and
authority, a person runs these fixed, reviewable scripts once, by hand,
outside any EM-Claw session. Both scripts only read their sources and write
only to the output folder you give them. The agent then works from that output
alone.

## Option A: export from the CryoSPARC database

Run on the CryoSPARC master as the account that owns CryoSPARC, with
`cryosparcm` on `PATH` and `python3` available:

```sh
bash helper/export_cryosparc_database_history.sh ./cryosparc_history P123        # one project
bash helper/export_cryosparc_database_history.sh ./cryosparc_history ALL 4       # every project, 4 workers
EXPORT_REAL_FILES=1 bash helper/export_cryosparc_database_history.sh ./cryosparc_history ALL 4
```

For each project it writes `project_metadata/<P>/` with `project.json`,
`jobs.json`, `workspaces.json`, `events.json`, `workflows.json`, `reports.json`
and `fs_files.json`, plus `export_summary.tsv` and a JSON validation report.
`EXPORT_REAL_FILES=1` also saves the PNG/PDF plots of 2D, selection, ab-initio
and refinement jobs under `project_gridfs_files/<P>/<J>/`. Each run regenerates
the metadata it owns under the output folder. Run `--help` for all options.

## Option B: extract from project folders

Needs Python 3.7 or later; numpy is optional, and lets the script also read
resolutions from `.cs` files. Run it only on project folders you trust, because
it loads their `.cs` files with numpy.

```sh
python3 helper/extract_effective_cryosparc_flow.py \
    --project-dir /path/to/CS-myproject --resolution-cutoff 4.0 \
    --outdir YOUR_HISTORY_BANK_PATH/CS-myproject/G1
```

It finds every completed reconstruction or refinement at or better than the
cutoff, traces all jobs upstream of them, and writes the result as a CryoSPARC
workflow (`*_cryosparc_workflow.json`), a job graph (`*_graph.json`,
`*_nodes.tsv`, `*_edges.tsv`, `.dot`, `.mmd`) and a `*_summary.md`. Always pass
`--outdir`; without it the results are written inside the project folder. Use
`--print-seeds-only` to preview which reconstructions pass the cutoff.

Keep the layout `<bank>/<CS-project-folder>/G<n>/`, one `G<n>` folder per
extracted workflow: the digest step looks for `*CS*/G*` folders.

## Then: have the agent digest the raw history

Point subR at the bank by replacing `YOUR_HISTORY_BANK_PATH` in
`profiles/cryosparc-agent/.claude/agents/subR.md`, start EM-Claw on the
`cryosparc` route, and ask it to digest the raw history, for example:

```text
> Digest the raw CryoSPARC history in <output folder> into the history bank at <bank path>.
```

The agent digests each kind of raw history with its own tool in
`profiles/cryosparc/tools/`; both write compact `*_digest.json` files in the same
schema, and subR reads those digests:

| Raw history | Digest tool |
| --- | --- |
| Option B, `extract_effective_cryosparc_flow.py` | `build_history_digest.py --bank <bank path>` adds a digest to each `G<n>` folder |
| Option A, `export_cryosparc_database_history.sh` | `build_dbdump_digest.py`, in two stages per CryoSPARC user: `index-res` reads each job's final resolution from the exported event logs, then `digest` writes one digest per sample under `<bank path>/<user>__<project folder>/G<nnn>__<workspaces>/` |

`build_dbdump_digest.py` also recovers projects and jobs that were deleted from
CryoSPARC but are still in the database. It decides which projects belong to a
user from where their folders sit, so give it the directory that holds your
users' home folders with `--home-root` or `EMCLAW_CRYOSPARC_HOME_ROOT` (default
`/home`). Its digests carry numbers and parameters only, no 2D or FSC plots.

Finally, list your best precedents in `profiles/cryosparc/Selected_collection.txt`
so subR weights them higher.
