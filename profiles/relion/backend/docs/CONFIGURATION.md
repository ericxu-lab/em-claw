# Configuration

Runtime configuration is read once, when the MCP process imports
[`server.py`](../src/relion_mcp/server.py). Restart the server after changing
environment variables. [`settings.py`](../src/relion_mcp/settings.py) is the
authority for names, defaults, parsing, and path confinement.

## Environment variables

These are the complete 18 variables in `ENVIRONMENT_DEFAULTS`.

| Variable | Default | Meaning |
|---|---|---|
| `RELION_MCP_MODE` | `plan` | `plan` rejects every write; `run` permits a confirmed write within approved roots. Only these two values are accepted. |
| `RELION_PROJECT_ROOTS` | `""` (empty) | Approved absolute project roots, separated by the platform path separator (`:` on POSIX, `;` on Windows). Empty disables project access. |
| `RELION_CRYOSPARC_IMPORT_ROOTS` | `""` (empty) | Absolute, read-only CryoSPARC project roots, separated by the platform path separator. Required only when a standalone or RELION-only route uses the particle bridge; the combined route uses a startup snapshot of API-derived `{uid, project_dir}` identities plus directory device/inode values from the active CryoSPARC instance, with canonical directories resolved through `Project.dir()`. |
| `RELION_PYEM_PYTHON` | `""` (empty) | Absolute path to the Python interpreter for the external pyem installation. Empty makes CryoSPARC import unavailable. |
| `RELION_CSPARC2STAR` | `""` (empty) | Absolute path to a readable, non-symlink `csparc2star.py`. Empty makes CryoSPARC import unavailable. |
| `RELION_SCRATCH_DIR` | `""` (empty) | Optional absolute execution-host directory used as RELION's default particle scratch location. Empty disables the configured default. |
| `RELION_PIPELINE_NAME` | `default` | Pipeline used whenever a tool receives `pipeline_name=None`. The corresponding project file is `<name>_pipeline.star`. |
| `RELION_PIPELINER` | `relion_pipeliner` | Bare executable name or absolute path used to add jobs and launch schedules. |
| `RELION_IMAGE_HANDLER` | `relion_image_handler` | Bare executable name or absolute path used by direct map rescaling. |
| `RELION_STAR_HANDLER` | `relion_star_handler` | Executable hint whose resolved parent directory is added to child `PATH`, including native Select/JoinStar jobs. |
| `RELION_CLASS_SELECTOR` | `relion-mcp-class-select` | Fixed package-owned selector used by the preferred one-job GUI-equivalent class-selection path. A bare name or absolute executable path is accepted. If it cannot be resolved, `materialization_mode="auto"` falls back to Select/JoinStar. |
| `RELION_TOPAZ_EXECUTABLE` | `topaz` | Bare executable name or absolute path resolved by `relion_environment` for its Topaz availability report. Topaz training and picking jobs use their operation-level `topaz_executable` parameter instead. |
| `RELION_ADD_JOB_TIMEOUT` | `90` | Positive integer seconds allowed for one `relion_pipeliner` add-job call. |
| `RELION_UTILITY_TIMEOUT` | `900` | Positive integer seconds allowed for direct utilities, including map rescaling. |
| `RELION_SYMMETRY_ALIGNMENT_MAX_BOX_PX` | `512` | Positive integer maximum cubic input-map side for native symmetry alignment. Increase it only after accounting for the padded Fourier-memory cost and staging space on the execution host. |
| `RELION_VISUAL_MAX_SIDE_PX` | `2800` | Positive integer maximum width or height of a returned composite PNG. |
| `RELION_VISUAL_MAX_PNG_BYTES` | `6000000` | Positive integer maximum encoded PNG size before the renderer rejects it. |
| `RELION_VISUAL_MAX_PANELS` | `96` | Positive integer global panel cap; individual renderers may impose lower limits. |

Timeout, symmetry-alignment, and visual-limit values must parse as positive
integers. Invalid mode or integer values fail server startup with a
configuration error.

When a configured RELION executable resolves successfully, its parent
directory is prepended to the environment inherited by RELION subprocesses.
This is important because `relion_pipeliner` writes job commands such as
`relion_import`, `relion_star_handler`, and the class-selection helper; configuring only an absolute
pipeliner path would otherwise leave sibling executables undiscoverable.

### CryoSPARC particle-import configuration

The CryoSPARC-to-RELION Class3D handoff is fail-closed until the two pyem
settings and one source-authorization route are available. In the combined
EM-Claw route, the RELION bootstrap queries the authenticated active CryoSPARC
environment when the RELION MCP starts and snapshots the returned API-derived
inventory of `{uid, project_dir}` identities and pins each directory's
device/inode values. It obtains each canonical
`project_dir` through authoritative `Project.dir()`. The inventory is fixed for
that MCP process. A combined-route source must exactly match one inventory
`project_dir`; configured standalone roots do not replace this check. Relaunch
the combined route after the live project inventory changes.

For a named `P#`/`J#`, use `mcp__cryosparc__inspect_project_job` and pass its
`relion_handoff.source_project_dir`; choose the primary and passthrough inputs
only from `relion_handoff.allowed_cs_files`. Discovery identifies the requested
live job and its candidate CS outputs, while the RELION server independently
enforces startup-inventory membership. A standalone or RELION-only server has
no live inventory and instead requires `RELION_CRYOSPARC_IMPORT_ROOTS` as an
explicit read boundary only when this bridge is used. Configured roots are
resolved before containment is checked, are sufficient for standalone source
authorization, and do not grant RELION output rights.

Source CS files plus MRC/MRCS stacks must be regular, non-symlink files
selected by relative paths beneath the authorized source project. Keep every
source project separate from the destination selected under
`RELION_PROJECT_ROOTS`.

`RELION_PYEM_PYTHON` and `RELION_CSPARC2STAR` must be absolute paths. The
interpreter must resolve to an executable regular file. The script itself must
be a readable regular file and must not be a symbolic link. Site paths belong
in the private environment, not in tracked configuration. For example:

```sh
RELION_PYEM_PYTHON=/absolute/path/to/pyem-python
RELION_CSPARC2STAR=/absolute/path/to/csparc2star.py
# Required when standalone RELION uses this bridge:
# RELION_CRYOSPARC_IMPORT_ROOTS=/absolute/path/to/cryosparc-projects
```

`relion_environment` reports these settings under
`cryosparc_particle_import`, including resolved paths, the observed script
SHA-256, startup-inventory or configured-root authorization availability and
policy, output/copy policy, issues, and `ready`. The combined route's
path-only inventory transport is launcher-owned and must not be configured by
users. During conversion the server
sets the script directory as the child-only `PYTHONPATH`, disables user-site
packages and bytecode writes, removes `CRYOSPARC_*` and
`EMCLAW_CRYOSPARC_*` variables, and gives Numba an owner-only cache inside the
private conversion staging directory. That cache is removed before the bundle
is published. It invokes pyem as an argv list without a shell. The configured
`RELION_UTILITY_TIMEOUT` also bounds this subprocess.

These settings permit conversion but do not create a RELION project. A missing
destination may be previewed with
`relion_render_cryosparc_particle_import`; initialize it explicitly with the
returned `relion_initialize_project` request, render again, and only then call
`relion_import_cryosparc_particles`.

`RELION_SCRATCH_DIR` is validated lexically and then explicitly set in, or
removed from, the environment passed to RELION. It must be an absolute,
non-root POSIX path without traversal, whitespace, control characters, RELION
option delimiters, or shell metacharacters. The path is intentionally not
resolved and need not exist on the MCP host: a queued job may use node-local
storage that is visible only on its execution hosts. `relion_environment`
reports the configured value together with advisory MCP-host existence,
directory, and writability checks. For queued execution, verify separately
that the same path is usable on every assigned node. RELION creates and may
remove a `relion_volatile` child there, so use a dedicated scratch location and
do not share one physical location between concurrent jobs. Setting the
variable is the operator approval for that exact temporary-write base. Shallow
cluster mounts such as `/scratch` are intentionally accepted because the MCP
host cannot establish their ownership; never configure a shared or unrelated
directory whose `relion_volatile` child is not safe for RELION to delete.

For scratch-capable jobs, an explicit non-empty `scratch_directory` operation
parameter takes precedence over `RELION_SCRATCH_DIR`; it must equal that
operator-configured base or be a lexical descendant of it. Omitting the
parameter preserves the global RELION default, while an explicit empty string
disables scratch for that job. A non-empty per-job value is rejected when no
global scratch base is configured.
The descendant check is lexical, not filesystem or symlink containment: paths
may exist only on execution hosts and cannot be resolved reliably by the MCP
host. Operators must ensure the configured base and allowed descendants do not
traverse untrusted symlinks on any execution host.

## Codex connection and approval mode

Use and adapt the checked-in
[`examples/mcp-config.toml`](../examples/mcp-config.toml). It is the
canonical TOML example, so this document does not duplicate it. Replace its
two absolute path placeholders, place the configuration in the appropriate
Codex configuration file, restart Codex, and verify that the `relion` server is
available.

Keep `default_tools_approval_mode = "writes"`. This is the verified host-side
mode for this server: read-only planning and evidence tools remain available,
while tools annotated as writes receive host review. Host approval and server
authorization are separate controls. A write succeeds only when all of these
are true:

1. the host permits the write tool call;
2. `RELION_MCP_MODE=run`;
3. `RELION_PROJECT_ROOTS` contains the target project;
4. the tool call passes `confirm=true`.

The supported MCP configuration fields and approval-mode values are defined in
the [official Codex configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference).

Use the combined `relion_run_job`, `relion_run_workflow_step`, and
`relion_run_particle_class_selection` tools for ordinary execution. Each
registers and launches its bounded job or graph in one write intent. The
`relion_add_*` and `relion_start_jobs` calls are recovery/advanced interfaces,
not a way to split a normal run into extra approvals. See the canonical
[API reference](API.md) for exact signatures and `started` semantics.

## Pipeline and schedule names

Every public `pipeline_name: str | None = None` argument resolves to
`RELION_PIPELINE_NAME`. With the default configuration, tools inspect or update
`default_pipeline.star`. Passing a tool-level name overrides the configured
name for that call.

`schedule_name` identifies the RELION pipeliner schedule used to launch
already registered jobs. It is not a cluster queue name. If a combined run
tool receives no schedule name, the server derives a safe name from the job
alias or operation. Names and aliases accept 1–80 letters, digits, `.`, `_`,
or `-`.

A successful schedule launch returns a detached launcher PID and writes its
native scheduler log to `Log/pipeline_<schedule>.log`. Extra launcher console
output or launcher failure is retained in
`Log/pipeline_<schedule>.diagnostic.log`. Schedule launch is asynchronous; use
`relion_job_status` and the job's `run.out`/`run.err` to determine completion.

## GPU and queue semantics

GPU and queue choices are operation parameters, not MCP environment
variables. Inspect each operation with `relion_describe_operation` before
supplying overrides.

- `use_gpu` enables the native RELION job option where mapped.
- `gpu_ids` or `gpu_id` is passed to the corresponding native option. The MCP
  does not allocate GPUs, translate physical devices, or verify scheduler
  reservations.
- For a local job, use IDs valid in the MCP/RELION execution environment. For
  a queued job, use the logical device convention established by the site's
  RELION queue template and scheduler environment. Do not assume tutorial GPU
  strings match the current host or allocation.
- `submit_to_queue` toggles RELION's native queue option where that parameter
  is mapped. Queue name, submission command, template, resource requests, and
  site defaults remain RELION configuration concerns.
- The MCP accepts structured mapped parameters; it does not expose an argument
  for arbitrary queue shell snippets.

The pipeliner schedule and the compute queue are distinct layers. The schedule
orders registered RELION jobs. A job within that schedule may then run locally
or submit through the site queue according to its rendered native options.

## Project-root and path security

All tools that read or write a project validate `project_dir` against
`RELION_PROJECT_ROOTS`; this includes read-only visual, status, output, and log
tools. Configure the narrowest roots that contain the intended projects.

- `project_dir` must be absolute.
- Existing projects are resolved before containment is checked, so a symlink
  cannot escape an approved root.
- Project creation validates the resolved parent and then revalidates the new
  directory.
- RELION artifact inputs and job paths are project-relative. CryoSPARC import
  inputs are relative to an absolute source project authorized either by exact
  membership in the combined route's startup live-project inventory or by a
  configured standalone root.
  Absolute artifact paths and `..` traversal are rejected.
- Resolved artifact symlinks must remain inside the selected project.
- InitialModel acceptance records require no additional environment setting.
  They are confined, content-addressed project metadata under
  `.relion-mcp/initial-model-acceptances/`; only
  `relion_record_initial_model_acceptance` should create them. Class3D add/run
  calls require the returned project-relative path, and `relion_start_jobs`
  revalidates a matching record, every bound artifact, the MRC/PNG/particle-STAR
  structure, and reference geometry before launch.
- CryoSPARC import source files and stacks may not traverse symlinks. Import
  writes a new copy-only bundle below `External/CryoSPARC/`; it never writes
  into a source project or replaces an existing bundle.
- CryoSPARC `ctf/exp_group_id` is zero-based. The bridge records the source ID
  and validates pyem's corresponding one-based RELION `rlnOpticsGroup`.
- Publication prefers `renameat2(RENAME_NOREPLACE)`. On GPFS and other
  filesystems that reject that flag, it exclusively reserves the destination
  directory and atomically renames the completed private staging directory
  over only that empty reservation. A crash can leave an empty mode-000
  reservation that must be inspected and removed manually before retrying.
- RELION commands are executed as argv lists, without a shell.
- In agent deployments, do not expose direct Class3D-capable
  `relion_pipeliner` or scheduler-helper shell execution alongside this MCP;
  the acceptance interlock is enforced by the guarded MCP write paths.
- Direct map rescaling never overwrites its input. An existing distinct output
  is replaced only when `overwrite=true`.
- Abort is cooperative: the tool creates RELION's
  `RELION_JOB_ABORT_NOW` marker in the validated job directory.

An empty `RELION_PROJECT_ROOTS` is intentionally fail-closed. Catalog and
workflow tools that do not access a project still work, but project reads and
writes do not.

## First configuration check

After starting the MCP, call `relion_environment()` without a project. Check
the mode, approved roots, expected RELION version, resolved pipeliner, image
handler, star handler, class selector, reported particle-class selection modes,
`relion_refine`, symmetry-alignment limits, CryoSPARC particle-import readiness,
and execution path. Then call it
with an approved absolute `project_dir` to check the project marker and
configured pipeline file.

Before relying on scientific visual evidence, run the image challenge pair as
described in [`VISUAL_TOOLS.md`](VISUAL_TOOLS.md).

## Troubleshooting

### Server fails during startup

Check `RELION_MCP_MODE` and every timeout/visual-limit variable first. Mode must
be `plan` or `run`; numeric values must be positive integers. Because settings
are loaded at process import, restart the MCP after correcting them.

### `relion_environment` reports a missing executable

Use an executable name discoverable on the MCP process's inherited `PATH`, or
set the corresponding variable to an executable absolute path. If the
pipeliner is found but generated RELION job commands are not, configure the
RELION executables from the same installation so their parent `bin` directory
is added to the child path. External scientific programs such as CTFFIND and
Topaz are separate operation parameters and site dependencies.

### Execution is disabled

`RELION_MCP_MODE=plan` is the safe default. Review the rendered command, then
restart with `RELION_MCP_MODE=run` only when execution is intended. A run-mode
write still needs an approved root and `confirm=true`.

### Project access is rejected

Confirm that `project_dir` is absolute and resolves beneath one configured
root. Check for symlinks that resolve elsewhere. Multiple roots must use the
platform path separator, not commas.

### Project initialization cannot create a pipeline

The tool preserves an existing native pipeline without launching RELION. When
the pipeline is missing, it runs the RELION GUI executable `relion` directly if
the MCP process inherited a non-empty `DISPLAY`; otherwise it uses `xvfb-run`
as a headless fallback. Forward a working X11 display or install `xvfb-run` on
systems without one. Inspect the returned initializer stdout/stderr or the
actionable error if native creation fails.

### CryoSPARC particle import is not ready

Inspect `relion_environment()["cryosparc_particle_import"]`. Configure absolute
paths for `RELION_PYEM_PYTHON` and `RELION_CSPARC2STAR`, then restart the MCP.
On the combined route, confirm that the startup live-project inventory is
available, then resolve the source with `mcp__cryosparc__inspect_project_job`
and use its `relion_handoff` paths. Relaunch if the project was created after
the RELION MCP took its inventory snapshot. On a standalone or RELION-only
route, configure at least one `RELION_CRYOSPARC_IMPORT_ROOTS` entry when using
this bridge. If the destination does not exist yet, this readiness check is
independent of project creation: render the import, call the returned
`relion_initialize_project`
request, and render again. Do not run `csparc2star.py` directly to bypass a
missing-project or metadata validation error.

### A combined run registered jobs but did not start them

Do not add the jobs again. The start error reports the preserved job paths and
schedule name. Diagnose executable, schedule, and project logs, then call
`relion_start_jobs` with those existing jobs.

### Class3D is blocked by the InitialModel acceptance gate

Do not bypass the gate or create a JSON record manually. Inspect the returned
validation code, repeat any unresolved scientific assessment, and call
`relion_record_initial_model_acceptance` only after the exact reference,
Class3D particle population, particle diameter, evidence files, and audited
lineage satisfy the documented contract. A record becomes stale if any bound
file changes. For an add-only Class3D job, `relion_start_jobs` finds and
revalidates a matching record from project metadata; it intentionally has no
acceptance-path argument. See the
[API interlock contract](API.md#initialmodel-to-class3d-acceptance-interlock).

### `started=true` but the job is not complete

This is expected: `started` records successful asynchronous schedule launch,
not RELION completion. Poll `relion_job_status`; inspect `run.err`, `run.out`,
and scheduler logs when status is failed, absent, or ambiguous.

### A visual result is rejected for size or panel count

Request fewer classes, micrographs, or comparison branches first. Raise visual
limits only when the client and model can safely receive the larger image.
Individual tools retain their own stricter caps even if the global panel limit
is higher.

### Map rescaling refuses an output

Inputs and outputs must be project-relative, the input must exist, pixel sizes
and box size must be positive, and the output must differ from the input. Use a
new output name, or set `overwrite=true` only after explicitly deciding to
replace an existing distinct output.
