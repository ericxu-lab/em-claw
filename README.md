# EM-Claw

EM-Claw starts or resumes a Claude Code conversation through a cryo-EM route.
For first-time setup, see [INSTALL.md](INSTALL.md).
With no route argument, it uses `combine`:

| Route | Claude profile | MCP servers | Environment inputs |
| --- | --- | --- | --- |
| `bin/em-claw cryosparc` | `profiles/cryosparc` | `cryosparc` | Required CryoSPARC `.env` |
| `bin/em-claw relion` | `profiles/relion` | `relion` | Optional RELION `.env` |
| `bin/em-claw` or `bin/em-claw combine` | `profiles/combined` | `cryosparc` and `relion` | Required CryoSPARC `.env`; optional RELION `.env` |

Use the default `combine` route when one conversation needs both toolsets; use
an explicit single-backend route when it does not. In the combined route, the
MCP servers are independent stdio child processes with separate Python runtimes,
backend environments, tool namespaces, and safety checks. Their tools appear as
`mcp__cryosparc__*` and `mcp__relion__*`.

The CryoSPARC subagents (subC, subP, subR, subS, subV) and the `/subP` skill
are in [`profiles/cryosparc-agent/.claude/`](profiles/cryosparc-agent/).

## Configure backend environments

Keep site-local settings in separate, ignored files:

- The CryoSPARC file defaults to `profiles/cryosparc/.env`. It must provide
  `EMCLAW_CRYOSPARC_SOURCE`, `CRYOSPARC_LICENSE_ID`,
  `CRYOSPARC_MASTER_HOSTNAME`, `CRYOSPARC_BASE_PORT`, `CRYOSPARC_EMAIL`, and
  `CRYOSPARC_PASSWORD`. Set `EMCLAW_CRYOSPARC_ENV_FILE` to an absolute path to
  use another machine-local file. This file is required only by `cryosparc` and
  `combine`.
- The optional RELION file defaults to `profiles/relion/.env`. It may contain
  `RELION_PROJECT_ROOTS` and optional `RELION_*` executable, pipeline, timeout,
  mode, or scratch settings. Set `EMCLAW_RELION_ENV_FILE` to an absolute path to
  use another file. It is consulted only by `relion` and `combine`; if the
  default file is absent, RELION starts with safe empty roots and scratch
  values. Set `EMCLAW_RELION_ENV_FILE` to an explicit empty value to disable
  auto-discovery of the default file.

Both files use trusted shell `NAME=value` or `export NAME=value` syntax. Quote
values as needed, make the files readable only by their owner (for example,
mode `0600`), and never commit them. Because they are shell input, use only
files you control and keep them to environment setup. Credential values must
not be copied into `.mcp.json`, Claude settings, documentation, command-line
arguments, or logs.

The launcher evaluates a selected file in a subshell and imports only the core
values needed by the selected Claude profile. The matching MCP bootstrap then
sources its complete file. Backend-specific runtime settings such as `PATH`,
`PYTHONPATH`, or `LD_LIBRARY_PATH` therefore stay with their owning backend.
Caller-exported core and routing variables take precedence over file defaults.

Python scripts that use the CryoSPARC API must run through
`libexec/em-claw-cryosparc-python`, not an arbitrary Conda interpreter. The
helper accepts ordinary Python arguments (`-c ...` or a script path and its
arguments), reuses the CryoSPARC MCP's isolated environment contract, removes
RELION variables, and pins `cryosparc-tools` 4.7.1 with NumPy 1.26.4. From the
combined or CryoSPARC profile directory, invoke it as
`../../libexec/em-claw-cryosparc-python ...`.

On the `combine` route, each MCP bootstrap removes the other backend's prefixed
variables before starting its server: the RELION child cannot read
`CRYOSPARC_*`, and the CryoSPARC child cannot read `RELION_*`. CryoSPARC
connection variables remain available to the combined Claude parent because
its CryoSPARC helper scripts need them, but they are excluded from the RELION
child and from non-secret launch output.

Set `EMCLAW_CRYOSPARC_SOURCE` to the absolute path of the CryoSPARC agent
directory: the bundled `profiles/cryosparc-agent`, or a site copy of it. EM-Claw mounts that
source read-only except for its `projects` directory, which remains read-write
for scientific state. Under `combine`, relative `projects/...` paths resolve
through `profiles/combined/projects` to that external directory. RELION uses
the pinned source snapshot in `profiles/relion/backend`.

## Launch a route

The no-argument form and the explicit `combine` form are equivalent:

```sh
bin/em-claw
bin/em-claw combine
```

Select a single-backend route explicitly when wanted:

```sh
bin/em-claw cryosparc
bin/em-claw relion
```

Claude arguments must follow `--`:

```sh
bin/em-claw combine -- --model opus
```

Use `EMCLAW_CLAUDE_BIN` to select one Claude executable without embedding shell
arguments; otherwise the launcher resolves `claude` on `PATH`.

An interactive launch prints an EM-Claw welcome banner, naming the selected
route, to the terminal before Claude Code starts: block letters under a UTF-8
locale, plain ASCII otherwise. It never appears in
`--dry-run` output or when stderr is not a terminal; set `EMCLAW_NO_BANNER=1` to
turn it off.

### Preflight without launching

Place `--dry-run` before the selected route:

```sh
bin/em-claw --dry-run
bin/em-claw --dry-run cryosparc
bin/em-claw --dry-run relion
bin/em-claw --dry-run combine
```

`--dry-run` validates only the selected profile, its required environment
inputs, paths, mode values, MCP bootstraps, and launch prerequisites. It prints
a non-secret route summary and the effective Claude command, but does not start
Claude or an MCP server and does not create runtime directories. A failed
invariant returns a nonzero status. The complete syntax is

```text
bin/em-claw [--dry-run] [cryosparc|relion|combine] [-r|--resume [ID_OR_SEARCH]] [-- CLAUDE_ARGUMENT ...]
```

Run `bin/em-claw --help` to display it. With no route, preflight targets
`combine`.

## Resume a conversation

EM-Claw accepts Claude's `-r`/`--resume` option. With no value it opens the
conversation picker; with a value it accepts a conversation ID or search text.
`--resume=VALUE` is also accepted, and an explicit route may appear before or
after the resume option when the arguments are unambiguous. For example:

```sh
bin/em-claw --resume
bin/em-claw combine --resume 01234567-89ab-cdef-0123-456789abcdef
bin/em-claw cryosparc --resume "particle cleanup"
bin/em-claw --resume="3D refinement" relion
```

Resume is a launcher option and must appear before the launcher's `--`
separator. The first form uses the default `combine` route. Always resume with
the same route that created the conversation. A resumed invocation restores the
conversation but starts fresh MCP child processes using the route's current
profile, MCP configuration, and environment files. EM-Claw also renders the
current system and profile instructions again instead of reusing Claude's
stored system-prompt snapshot. Changing the route does not carry the former
route's MCP tools or runtime state into the new children and can leave the
conversation's instructions and project assumptions mismatched.

`--dry-run` may be combined with resume to preview the selected route and
effective Claude command without opening the conversation or starting an MCP:

```sh
bin/em-claw --dry-run relion --resume "3D refinement"
```

Pass Claude options that accompany resume, such as `--fork-session`, after the
launcher separator:

```sh
bin/em-claw relion --resume 01234567-89ab-cdef-0123-456789abcdef -- --fork-session
```

## RELION execution authority

The `relion` and `combine` routes default an unset or empty
`RELION_MCP_MODE` to `run`. To make RELION read/plan-only, put
`RELION_MCP_MODE=plan` in `profiles/relion/.env` or export it in the caller, then
relaunch the same RELION-containing route, resuming the conversation if wanted.

Run mode does not start a job or grant unrestricted writes. A RELION write still
requires all of the following:

- its target is beneath an explicit root in `RELION_PROJECT_ROOTS`;
- the MCP call supplies `confirm=true`;
- the server's validation and path-containment checks pass; and
- the operator has approved a bounded processing segment in Claude, including
  its jobs, inputs, parameter ranges, budgets, and stop conditions.

An empty `RELION_PROJECT_ROOTS` lets the route start, but project access and
writes fail closed. The mode and roots are fixed when the RELION MCP child
starts, so exit and relaunch after changing them.

Before Class3D, `relion_compare_map_projections` can compare a selected
InitialModel map with explicit 2D class averages using independently fitted
synthetic projections.
InitialModel acceptance requires direct inspection of at least three selected
classes that are credible, distinct appearances from the seed population;
correlation scores and fitted directions are diagnostics, never an automatic
verdict. Central-slab or threshold-occupancy views from ordinary map reports are
not synthetic projections. After inspecting an eligible report,
`relion_publish_initial_model_evidence` can publish the exact PNG identified by
its `image.sha256` into the approved project through the normal run-mode,
`confirm=true`, and root-confinement gates. This byte-preserving publication
does not itself establish scientific acceptance.

To configure RELION's optional native scratch default, set a lexically safe
absolute, non-root path:

```sh
RELION_SCRATCH_DIR=/fast/local/relion-scratch
```

The path need not exist on the launch host, but it must be visible and writable
on every compute node that runs the job. RELION creates `relion_volatile` below
that path, removes an existing directory of that name when staging particles,
and normally removes it after successful completion. Use a dedicated directory
whose `relion_volatile` child may safely be deleted. An empty value disables the
configured default.

Scratch-capable classification and refinement operations may override the
session default with `scratch_directory`. Omit it to inherit the default, pass
`""` to disable scratch for that job, or choose the configured base or one of
its lexical descendants. This containment check is lexical rather than
symlink-aware because a compute-local path may not exist on the MCP host. Use
trusted paths and distinct scratch locations for concurrent jobs.

## Cross-backend work

The `combine` route makes both MCP toolsets available to Claude; it does not
make the two servers communicate directly. It also does not automatically
convert data, translate job parameters, or merge CryoSPARC and RELION workflow
state. Any handoff must be an explicit, reviewed export/conversion/import
workflow whose files, metadata, provenance, and destination project are named.
Native CryoSPARC operations must use the `cryosparc` MCP, and native RELION
operations must use the `relion` MCP.

For a particle handoff identified by CryoSPARC `P#`/`J#`, first call
`mcp__cryosparc__inspect_project_job`. It queries the live instance selected by
the active CryoSPARC environment. Use its
`relion_handoff.source_project_dir` and choose the project-relative inputs only
from `relion_handoff.allowed_cs_files`; do not guess a site path or scan the
filesystem, and do not guess an omitted input when
`relion_handoff.allowed_cs_files_truncated=true`. At combined-route RELION MCP
startup, EM-Claw independently snapshots the API-derived inventory of
`{uid, project_dir}` identities, plus pinned directory device/inode values,
from that same authenticated CryoSPARC
instance, resolving each canonical `project_dir` through authoritative
`Project.dir()`. A source is authorized only when its canonical directory
exactly matches an inventory `project_dir`. No duplicate CryoSPARC source-root
export is required on the combined route. A RELION-only route has no live
inventory and requires
`RELION_CRYOSPARC_IMPORT_ROOTS` only when that route uses the particle
bridge. The launcher also exposes each canonical `RELION_PROJECT_ROOTS` entry
to Claude as an additional working directory, so the agent and RELION MCP can
create approved project outputs there.

Persistent state belongs in the relevant CryoSPARC project or approved RELION
project. The launcher root, profile configuration, and vendored RELION backend
must not hold campaign files, datasets, credentials, caches, or processing
outputs.

## Vendored RELION backend

The RELION provenance record next to `profiles/relion/backend` identifies its
source revision, tree, package version, license, export method, and content
digest. The snapshot excludes checkout-only state such as `.git`, virtual
environments, caches, and built distributions.
