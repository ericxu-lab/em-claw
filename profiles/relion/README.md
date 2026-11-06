# RELION profile

`bin/em-claw relion` launches this directory as the supported RELION-only
Claude Code profile and exposes the `relion` MCP over stdio. The
`bin/em-claw combine` route—or `bin/em-claw` with no route—opens
`profiles/combined`, where the same RELION adapter is available next to an
independent `cryosparc` MCP. This profile retains the canonical RELION policy,
skill, implementation, and documentation through links.

Claude-side permissions are defense in depth; the RELION server's plan/run
mode, approved-root validation, path-containment checks, and `confirm=true`
requirement remain authoritative for writes.

Class3D has an additional fail-closed runtime interlock. The agent must first
use `relion_record_initial_model_acceptance` to publish a high-confidence,
hash-bound acceptance for the exact InitialModel map, final particles,
geometry, completed source artifacts, and visual evidence, then pass the
returned path to the Class3D add/run tool. Start-only recovery revalidates the
same live inputs before scheduler launch. Native Class3D shell commands are
outside this protection and are prohibited by the profile policy.

## Launcher environment

Keep RELION settings in the ignored, owner-only `profiles/relion/.env` (mode
`0600`). This trusted shell file is optional. The launcher evaluates it in a
subshell for either RELION-containing route and imports its routing values into
the selected Claude session; the RELION bootstrap separately sources the full
file. Backend runtime settings such as `PATH`, `PYTHONPATH`, and
`LD_LIBRARY_PATH` therefore remain private to the RELION MCP child. On
`combine`, the CryoSPARC bootstrap also removes inherited `RELION_*` values
before starting its server. An already exported RELION variable takes
precedence over an imported routing-file value. Do not put secrets or site paths
in an `.mcp.json`, Claude settings, or documentation. The default file may be
overridden with an absolute `EMCLAW_RELION_ENV_FILE` path for controlled
machine-local use; an explicitly empty value disables default-file discovery.

The supported launcher resolves this profile's real `backend` directory and
sets `RELION_MCP_REPO` to its absolute path. It preserves an explicit
`RELION_MCP_MODE=plan|run`, defaults an unset or empty mode to `run`, and rejects
other values before Claude starts. Supply one or more approved absolute RELION
project roots in `RELION_PROJECT_ROOTS`, separated by the platform path
separator, to enable project access. Do not use the EM-Claw root, combined
profile, or this component profile as a project root. An empty roots value
allows the session to start, but all RELION project access and every write fail
closed. `RELION_SCRATCH_DIR` is optional; leave it unset or empty to disable the
configured native scratch default.

A minimal local file is:

```sh
RELION_PROJECT_ROOTS=/absolute/path/to/approved/relion-projects
RELION_SCRATCH_DIR=/fast/local/relion-scratch
# Optional CryoSPARC-particle bridge for RELION Class3D:
RELION_PYEM_PYTHON=/absolute/path/to/pyem-python
RELION_CSPARC2STAR=/absolute/path/to/csparc2star.py
# Required only when the RELION-only route uses this bridge:
# RELION_CRYOSPARC_IMPORT_ROOTS=/absolute/path/to/cryosparc-projects
```

Then select a RELION-containing route from the repository root:

```sh
bin/em-claw relion
bin/em-claw
bin/em-claw combine
```

The `relion` route uses this directory as Claude's working directory; `combine`
uses `profiles/combined`. Both preserve forwarded `DISPLAY` and `XAUTHORITY`
values for RELION. Project initialization leaves an existing
`<pipeline>_pipeline.star` untouched; when the file is missing, the server runs
`relion --pipeline <pipeline> --do_projdir --refresh 1 --idle 0` through the
forwarded display, or falls back to `xvfb-run` when `DISPLAY` is unset.

The server environment follows the canonical RELION configuration exactly:

| Variable | RELION behavior |
| --- | --- |
| `RELION_MCP_MODE` | Unset/empty defaults to `run`; explicit `plan` or `run` is preserved; other values fail launch. |
| `RELION_PROJECT_ROOTS` | Defaults to empty, which blocks project access; use a path-separator-delimited list of explicit approved roots. |
| `RELION_CRYOSPARC_IMPORT_ROOTS` | Read-only source roots required only when the RELION-only route uses the Class3D particle bridge. The combined route instead uses its startup snapshot of API-derived `{uid, project_dir}` identities plus directory device/inode values from the active CryoSPARC instance, with canonical directories resolved through `Project.dir()`. |
| `RELION_PYEM_PYTHON` | Absolute Python executable for the separately installed pyem runtime. |
| `RELION_CSPARC2STAR` | Absolute readable `csparc2star.py` path; the observed script hash is recorded in each conversion manifest. |
| `RELION_SCRATCH_DIR` | Optional native RELION scratch default. Empty disables it; a nonempty value must be a lexically safe absolute, non-root path. |
| `RELION_PIPELINE_NAME` | Defaults to `default`. |
| `RELION_PIPELINER` | Defaults to `relion_pipeliner`. |
| `RELION_IMAGE_HANDLER` | Defaults to `relion_image_handler`. |
| `RELION_STAR_HANDLER` | Defaults to `relion_star_handler`. |
| `RELION_CLASS_SELECTOR` | The default `relion-mcp-class-select` name is bound to the profile-owned `bin/relion-mcp-class-select` shim. Explicit alternate bare names and absolute paths are preserved. |
| `RELION_TOPAZ_EXECUTABLE` | Defaults to `topaz`; used by `relion_environment` for its availability report, while Topaz jobs use their operation-level parameter. |
| `RELION_ADD_JOB_TIMEOUT` | Defaults to `90` seconds. |
| `RELION_UTILITY_TIMEOUT` | Defaults to `900` seconds. |
| `RELION_SYMMETRY_ALIGNMENT_MAX_BOX_PX` | Defaults to `512`. |
| `RELION_VISUAL_MAX_SIDE_PX` | Defaults to `2800`. |
| `RELION_VISUAL_MAX_PNG_BYTES` | Defaults to `6000000`. |
| `RELION_VISUAL_MAX_PANELS` | Defaults to `96`. |

`RELION_MCP_REPO` is a launcher variable identifying the vendored source
snapshot; it is not a user-set RELION server setting. Site paths, credentials,
and secrets do not belong in `.mcp.json` or tracked repository files.

For a CryoSPARC particle stack, first call
`relion_render_cryosparc_particle_import`. A RELION project need not exist yet:
use the returned `relion_initialize_project` arguments when
`bootstrap_required=true`, render again, and then call
`relion_import_cryosparc_particles`. The conversion creates a new
`External/CryoSPARC/<export_name>/` bundle and returns a mapped
`import_particles` handoff for registration before Class3D. It does not use the
specialized 2D-selection pipeline. On the combined route, first resolve the
named `P#`/`J#` with `mcp__cryosparc__inspect_project_job`. Use its
`relion_handoff.source_project_dir` and select the relative `.cs` inputs only
from `relion_handoff.allowed_cs_files`; do not guess an omitted input when
`relion_handoff.allowed_cs_files_truncated=true`. The RELION MCP accepts that
source only when its canonical directory exactly matches `project_dir` in the
API-derived `{uid, project_dir}` inventory, with directory device/inode values,
captured from the authenticated
active CryoSPARC environment at combined-route startup. Each inventory
`project_dir` is resolved through authoritative `Project.dir()`. A RELION-only
route has no live CryoSPARC inventory and therefore uses an explicitly
configured `RELION_CRYOSPARC_IMPORT_ROOTS` boundary when it uses this bridge.

Scratch configuration is read when the MCP process starts. Exit and launch a
new invocation after changing `RELION_SCRATCH_DIR` in the environment file or
caller environment. Resuming the same route also starts a fresh RELION MCP child
with the current value. The launcher checks the path lexically without requiring
it to exist locally because scratch may be compute-node-local; the configured
path must be visible and writable on every node that executes the local or
queued job. RELION stages particles below it in
`relion_volatile`, deleting an existing directory of that name before staging
and normally deleting it after a successful job. Setting the variable
authorizes that exact temporary-write base. Use a dedicated directory whose
`relion_volatile` child may safely be deleted; shallow cluster mounts such as
`/scratch` are accepted because their ownership cannot be established from the
launcher, so do not select a shared or unrelated base.

The scratch-capable 2D classification, initial-model, 3D classification, and
Refine3D operations also accept `scratch_directory`. Omit it to inherit the
session default, pass `""` to disable scratch for that job, or select the base
or a lexical descendant for a unique per-job location. Since compute-local
paths cannot be resolved on the MCP host, this lexical check is not a guarantee
against symlink redirection. The base and descendants must be trusted paths on
every execution host. Use distinct scratch locations for concurrent jobs so
they cannot remove or replace each other's staged particles.

The class-selection shim remains outside the provenance-locked `backend`
snapshot. The bootstrap gives the MCP its absolute path so RELION External jobs
do not depend on a package entry point inside a transient `uv` environment. A
configured Python or the `uv` runtime and cache must be visible on every host
that executes those local External jobs. If `EMCLAW_RELION_MCP_PYTHON` is set,
it must be an absolute executable path visible on those hosts.

The RELION and combined profiles start
`../../libexec/em-claw-relion-mcp`. That bootstrap defaults to the vendored
`backend` directory and may be overridden with an explicit `RELION_MCP_REPO`
only for controlled development validation.

## Safe session flow

1. Put explicit approved project roots in `profiles/relion/.env`.
2. Run `bin/em-claw relion`, or use `bin/em-claw combine` when the same session
   also needs CryoSPARC. RELION starts in `run` mode by default on either route.
3. Ask the agent to process, continue, or optimize the dataset, giving any
   resource or scientific limits. It discovers the environment, validates the
   workflow, and renders commands before execution.
4. The agent records the request's scope, input branch, parameter ranges,
   finite budgets, and stop conditions in `RELION_RUN_STATE.yaml`, then
   proceeds without a separate approval for each processing segment.

To force RELION into plan-only mode, set the following in its environment file
and restart or resume the same `relion` or combined route:

```sh
RELION_MCP_MODE=plan
```

Run mode removes only the server's plan-mode write veto. Each RELION-containing
Claude profile statically pre-allows read-only, additive, and idempotent tools
so the host does not prompt again for every job. That static permission is not
write authorization; the user's processing request supplies that authority
within its scope. The agent sets `confirm=true` for covered actions and keeps
prior authorization on resume unless revoked or superseded. Planning-only
requests remain read-only. The server still requires an
approved root and `confirm=true`; the mode is fixed for that MCP process.

The default is continuous autonomous processing: the agent monitors jobs,
assesses checkpoints, and launches the next ready stage through the requested
outcome. It does not ask whether to continue after each job. A credible
medium-confidence candidate may feed successive downstream stages while
alternatives and uncertainty remain recorded. `defer` records an unresolved
scientific conclusion without requesting human input; final scientific
acceptance and rejection still require high confidence.

Supported symmetry comparisons and focused/no-alignment experiments run under
the processing request when evidence, matched baselines, explicit user choices,
and the overall budget permit them. Agent-chosen stage allocations can be
rebalanced within that budget. Optional failures end the affected branch; at
diminishing returns or a hard limit the agent returns the best available result
with limitations. Questions are reserved for essential unrecoverable input,
unavoidable user-constraint conflicts, or necessary destructive/publication
authority. Planning requests remain read-only.

`relion_rescale_map` is handled conditionally instead of being statically
allowlisted: the selected profile's hook allows it when `overwrite` is omitted
or `false` and asks for confirmation when `overwrite=true`. Replacement
therefore remains a separately protected action, while a hook failure falls
back to normal permission handling. Invalid overwrite values are blocked, and
`relion_abort_job` remains denied on both routes.

Duplicate-particle removal is an additive direct command within an approved
segment. Render it first with `relion_render_remove_duplicate_particles` to
inspect the coordinate scale, native command, and both destination paths.
`rlnMicrographPixelSize` is authoritative; if it is absent, the renderer
requires an explicit verified coordinate scale even when
`rlnImagePixelSize` and `rlnMicrographOriginalPixelSize` agree. Inputs must
contain the coordinate, micrograph, optics-group, and X/Y origin columns
required by native RELION, with finite coordinate/origin values, non-null names,
SPA rather than tomography metadata, and strict native-compatible STAR
encoding, loop structure, tokenization, typed values, and
one-row-per-physical-line layout. `relion_remove_duplicate_particles` privately
stages and validates the retained output and `_removed.star` sidecar, proves a
type-aware particle-row partition and unchanged retained optics metadata, then
publishes both as new files; neither output may already exist and the input is
never modified. The sidecar validator accepts both the named `data_particles`
block from newer RELION builds and the deployed beta's legal unnamed `data_` block;
that legacy exception does not apply to inputs or retained outputs.

Campaign files and persistent runtime state belong inside an approved RELION
project, never in the dispatcher, either Claude profile, or backend snapshot.
The explicitly configured scratch path is a temporary native RELION write
location outside the project boundary. Processing must not modify `backend`.

The `combine` route does not create an automatic CryoSPARC-to-RELION bridge.
The MCP processes do not call each other or share workflow state; any handoff
must explicitly identify and review its export, conversion, import, and
destination-project steps.

Resume a RELION conversation with the same route that created it—for example,
`bin/em-claw relion --resume <conversation>` for a RELION-only conversation or
`bin/em-claw combine --resume <conversation>` for a combined one. The resumed
invocation starts a new RELION MCP child from the selected route's current
configuration and environment; it does not reconnect to the former child.

## Vendored backend

`backend` is a real directory derived from a clean `git archive` export of the
pinned RELION MCP source, with profile-local feature changes applied.
`backend.provenance.json` records its base source revision and Git tree, package
version, license, export method, explicit exclusions, and a digest of every
final exported file. The directory intentionally contains no nested Git
repository, virtual environment, cache, or built distribution.
