# Combined EM-Claw profile

@AGENTS.md

This is one EM-Claw session with two independent MCP servers: `cryosparc`
and `relion`. Use both when that helps the user's cryo-EM work, but keep their
process environments, credentials, project state, and write boundaries
separate.

Resume only a transcript that was started through this combined route. A
resumed transcript supplies conversational context; it does not restore an old
MCP process, environment, or runtime permission. Prior RELION task authorization
persists unless revoked or superseded. The currently selected
route, its two enabled MCPs, the effective RELION mode and project roots, and
all current write, confirmation, overwrite, and abort gates remain
authoritative. Reconnect to both backends as needed and reload durable project
state before acting. Start a new session instead of resuming a transcript from
an isolated CryoSPARC or RELION route.

## Route work to the owning backend

- Use `mcp__cryosparc__*` for CryoSPARC job-type, parameter, connection, and
  pipeline validation. When the user names an existing `P#`/`J#`, first call
  `mcp__cryosparc__inspect_project_job`; its active-environment connection,
  `relion_handoff.source_project_dir`, and
  `relion_handoff.allowed_cs_files` are authoritative for the handoff. Do not
  guess a site path, search the filesystem for the project, or select a `.cs`
  path outside that returned list. Treat
  `relion_handoff.allowed_cs_files_truncated=true` as an incomplete handoff
  inventory, not permission to guess an omitted path.
  Before planning or executing a CryoSPARC reconstruction, read
  `../cryosparc/CLAUDE.md` completely and follow its scientific phase and
  subagent protocol. Interpret references to its working directory through the
  compatibility links in this profile; keep project-specific files under
  `projects/<name>/`.
- Use `mcp__relion__*` for every operation covered by the RELION MCP. For any
  RELION SPA task, invoke the `relion-spa-orchestrator` skill and follow its
  applicable policy references. Start with `relion_environment` and
  `relion_validate_workflow`, and render each operation before executing it.
  InitialModel cannot feed Class3D until the hard `initial_model_accepted`
  checkpoint passes: create its exact hash-bound record with
  `relion_record_initial_model_acceptance`, pass the returned path to Class3D
  add/run calls, and let start-only recovery revalidate it. A refined consensus
  map imported from CryoSPARC is not an InitialModel seed: bind it with
  `relion_record_external_reference` and pass that returned path instead.
- Do not substitute a direct shell command for an operation covered by either
  MCP. Do not silently reinterpret a CryoSPARC request as RELION work or vice
  versa. If the intended backend is materially ambiguous, ask one focused
  question before a write.
- Scan CryoSPARC half-maps for symmetry with `vis_res/symmetry_scan.py`, which
  runs the RELION scanner on them in place; reserve `relion_scan_symmetry` for
  half-maps already inside an approved RELION project.

The two MCP processes do not communicate with each other. The conversation may
coordinate their results, but a cross-backend handoff is an explicit data
conversion task, not an automatic consequence of using both tools. Before a
handoff, establish input and output provenance, file format, pixel size, box
geometry, optics metadata, symmetry, handedness, and the destination project.
Create new artifacts inside approved project roots; never overwrite or mutate
the source artifact merely to make it consumable by the other backend.
For CryoSPARC particle stacks going into RELION Class3D, use the RELION-owned
`relion_render_cryosparc_particle_import` and
`relion_import_cryosparc_particles` tools. Pass the discovered
`relion_handoff.source_project_dir` and selected project-relative paths from
`relion_handoff.allowed_cs_files`. The RELION MCP authorizes the source when
its canonical directory exactly matches `project_dir` in the API-derived
`{uid, project_dir}` inventory and directory device/inode identities captured
from the authenticated CryoSPARC
environment at startup; each `project_dir` is resolved through authoritative
`Project.dir()`. Do not require `RELION_CRYOSPARC_IMPORT_ROOTS` on this route. A
prior RELION project is optional: when the
render reports `bootstrap_required=true`, call its returned
`relion_initialize_project` request and render again before conversion. Then
register the returned `import_particles` handoff; do not use the specialized
CryoSPARC-to-RELION 2D-selection script as this general bridge.

The CryoSPARC staging file deliberately is not linked into this directory:
read `../cryosparc/base_datainfo.yaml` when starting a project, copy it to
`projects/<name>/base_datainfo.yaml`, and then use only that project-local copy.
Do not create a root `base_datainfo.yaml` or `runs/` compatibility link; those
would reintroduce state collisions between concurrent projects.

## Environment and credential boundary

The launcher supplies each MCP from its own environment contract; the RELION
file may be explicitly disabled to use built-in defaults. Do not source, edit,
print, copy, or merge either `.env` file inside the session. Never put
credentials in commands, generated scripts, project state, reports, or MCP
configuration. The CryoSPARC connection-template tool is denied because its
output can contain credential-bearing code. If a connection prerequisite is
missing, report only the missing variable name, never another variable's value.

Do not globally activate or modify a Python/Conda environment and do not set a
session-wide `PYTHONPATH` for either backend. Their stdio bootstraps own separate
Python runtimes. Do not pass `CRYOSPARC_*` values to a RELION job or `RELION_*`
values to CryoSPARC code.

Run every Python command that imports `cryosparc` or creates, connects, or queues
CryoSPARC jobs through `../../libexec/em-claw-cryosparc-python`. It accepts normal
Python arguments (`-c ...` or a script path plus its arguments) and uses the
same isolated environment contract and pinned CryoSPARC client as the MCP.
Never use the `cryo_ief` Python for CryoSPARC API access; that environment is
only for the explicitly configured CryoRanker and model-mask utilities.

## Writes and durable state

RELION starts in `run` mode, but run mode is capability, not blanket approval.
A request to process, run, continue, or optimize with RELION authorizes the
routine additive work within that task. Record the scope and finite budgets in
`approval_envelope` without requesting separate segment approval. Follow the
orchestrator's autonomous continuation policy: monitor active jobs, assess
checkpoints internally, and dispatch the next ready stage without asking to
continue. Credible provisional outputs may feed downstream processing with
uncertainty recorded. Supported reversible scientific experiments are covered
within scope and budget; preserve their matched baselines. Rebalance stage
allocations within the campaign budget, finish with the best available result
at hard limits, and ask only for an essential unrecoverable input, unavoidable
user-constraint conflict, or necessary destructive/publication authority.
Every write must also satisfy the
server gates: an approved `RELION_PROJECT_ROOTS` destination and `confirm=true`.
Abort remains denied. An additive `relion_rescale_map` is allowed within an
approved boundary; `overwrite=true` is separately protected and must pass the
host confirmation hook.

Keep CryoSPARC state under the mounted `projects/` tree according to its phase
protocol. Keep `RELION_RUN_STATE.yaml` in the selected RELION project. Do not
store either backend's campaign state in this combined profile, and never use
one backend's approval boundary to authorize writes by the other.

Read-only inspection and validation may alternate freely between the servers.
For a mixed workflow, summarize which backend owns each artifact and next step
so provenance remains clear across the handoff.
