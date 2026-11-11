# Installing and using EM-Claw

EM-Claw runs Claude Code as a cryo-EM processing agent with CryoSPARC and/or
RELION tools. This guide takes a fresh Linux account from clone to first run.
[README.md](README.md) holds the full reference for routes, safety gates and
environment handling.

Throughout this guide, replace every `YOUR_..._PATH`, `YOUR_..._ACCOUNT` or
other `YOUR_...` placeholder with a value from your own site.

## 1. Requirements

| Component | Needed for | Notes |
| --- | --- | --- |
| Linux, `bash` 4.3+, `git` | all routes | Developed on CentOS 7 / x86_64 |
| [Claude Code](https://docs.claude.com/en/docs/claude-code) | all routes | `claude` on `PATH`, or set `EMCLAW_CLAUDE_BIN` |
| [uv](https://docs.astral.sh/uv/) 0.5+ | all routes | Builds the pinned Python 3.11 runtimes on first use |
| [bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) | `cryosparc`, `combine` | Keeps the CryoSPARC agent directory read-only |
| CryoSPARC 4.7.x master with a license and a user account | `cryosparc`, `combine` | `cryosparc-tools` 4.7.1 is installed automatically |
| RELION 5.0 | `relion`, `combine`, 2D class ranking | Plus `relion_class_ranker` model weights |
| [Codex CLI](https://github.com/openai/codex) | subV plan validation | Optional; `profiles/cryosparc/subV_gpt.sh` |
| pyem (`csparc2star.py`) | CryoSPARC → RELION particle import | Optional |
| Cryo-IEF / CryoRanker, Topaz, ChimeraX, PowerFit, Phenix | optional helper steps | See section 6 |

You do not need to install Python packages by hand: each launcher in
`libexec/` calls `uv run --no-project` with exact pins.

| Runtime | Launcher | Pinned packages |
| --- | --- | --- |
| CryoSPARC MCP server | `libexec/em-claw-cryosparc-mcp` | `mcp==1.29.1`, `cryosparc-tools==4.7.1`, `numpy==1.26.4` |
| CryoSPARC helper scripts | `libexec/em-claw-cryosparc-python` | `cryosparc-tools==4.7.1`, `numpy==1.26.4`, pillow, mrcfile, matplotlib, pyyaml, scipy |
| RELION MCP server | `libexec/em-claw-relion-mcp` | `mcp[cli]==2.0.0`, PyYAML, numpy 1.26.4, matplotlib 3.8.4, mrcfile, Pillow, scipy 1.11.4 |

## 2. Get the code and install the base tools

```sh
git clone YOUR_GITHUB_REPOSITORY_URL em-claw
cd em-claw

# uv and a Python 3.11 interpreter for it
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.11

# bubblewrap (use your distribution's package manager, or ask your admin)
sudo yum install bubblewrap        # or: sudo apt install bubblewrap

# Claude Code
npm install -g @anthropic-ai/claude-code
claude --version
```

## 3. Prepare the CryoSPARC agent directory

The CryoSPARC route mounts an agent directory, named by
`EMCLAW_CRYOSPARC_SOURCE`, read-only except for its `projects/` folder. The
repository ships one in `profiles/cryosparc-agent/`:

```text
profiles/cryosparc-agent/
├── .claude/
│   ├── agents/        subC.md  subP.md  subR.md  subS.md  subV.md
│   ├── skills/        subP/SKILL.md
│   └── settings.json  enables the Codex plugin for subV
└── projects/          per-project state (read-write, gitignored)
```

Use it directly by setting `EMCLAW_CRYOSPARC_SOURCE` to its absolute path in
section 4, or copy it elsewhere (for example to shared storage, so projects
live outside the code checkout) and point the variable at the copy.

Fill in the site placeholder in the agent prompts:

- `YOUR_GPU_RENDER_HOST` in `.claude/agents/subC.md`: a host with OpenGL and
  ChimeraX for surface rendering.

subR learns from the history bank in `profiles/cryosparc/history_bank/`. It
ships with six example digests, registered in
`profiles/cryosparc/Selected_collection.txt`. To add your site's own history,
collect it with one of the two scripts in [`helper/`](helper/README.md), ask
the agent to digest it into that bank, and list your best precedents in
`Selected_collection.txt`.

Add the reference papers subS may cite in phase 1 to
`profiles/cryosparc/solid_reference_pool`.

## 4. Configure the CryoSPARC environment

```sh
cp profiles/cryosparc/.env.example profiles/cryosparc/.env
chmod 600 profiles/cryosparc/.env
$EDITOR profiles/cryosparc/.env
```

| Variable | Value |
| --- | --- |
| `EMCLAW_CRYOSPARC_SOURCE` | Absolute path of the agent directory from section 3, e.g. `/absolute/path/to/em-claw/profiles/cryosparc-agent` |
| `CRYOSPARC_LICENSE_ID` | YOUR_CRYOSPARC_LICENSE_ID |
| `CRYOSPARC_MASTER_HOSTNAME` | YOUR_CRYOSPARC_MASTER_HOST |
| `CRYOSPARC_BASE_PORT` | Your master's base port (default `39000`) |
| `CRYOSPARC_EMAIL` / `CRYOSPARC_PASSWORD` | YOUR_CRYOSPARC_ACCOUNT login |

The same file takes the optional tool paths (`EMCLAW_RELION_CLASS_RANKER`,
`EMCLAW_CRYO_IEF_DIR`, `EMCLAW_POWERFIT_EXE`, ...) described in section 6.
Never copy these values into `.mcp.json`, Claude settings or committed files.

## 5. Configure the RELION environment (optional)

```sh
cp profiles/relion/.env.example profiles/relion/.env
chmod 600 profiles/relion/.env
$EDITOR profiles/relion/.env
```

Set at least `RELION_PROJECT_ROOTS` to the absolute RELION project directories
the agent may write to (colon-separated). With an empty value the route starts,
but every project access is blocked. If RELION comes from an environment
module, the file may also run `module load YOUR_RELION_MODULE_NAME` and extend
`PATH`. Set `RELION_MCP_MODE=plan` for a read/plan-only session.

## 6. Optional helper tools

Uncomment and set only the tools you use in `profiles/cryosparc/.env`. A
helper whose tool is left at its `YOUR_..._PATH` placeholder stops with an
error rather than running a wrong program.

| Helper | Variables |
| --- | --- |
| RELION class ranker (`2d_select/`) | `EMCLAW_RELION_CLASS_RANKER`, `EMCLAW_RELION_PYTHON`, `EMCLAW_RELION_TORCH_HOME`, optional `EMCLAW_RELION_MODULE` / `EMCLAW_RELION_MODULEPATH`, `EMCLAW_RELION_RUNS_DIR` |
| CryoRanker (`cryoemranker/`) | `EMCLAW_CRYO_IEF_DIR` (or `cryoranker_*` in `base_datainfo.yaml`) |
| Model-derived masks (`build_model_mask/`) | `EMCLAW_POWERFIT_EXE`, `EMCLAW_PHENIX_BIN`, `EMCLAW_CHIMERAX`; run with a Python that has gemmi, mrcfile, numpy, scipy and matplotlib |
| History digests (`tools/`) | `EMCLAW_HISTORY_PDF_ROOT` |
| CryoSPARC command_vis patch (`runs_infra/`) | `CRYOSPARC_MASTER_DIR`; run as the CryoSPARC admin account on the master host |

For subV on Codex, register the CryoSPARC MCP server in `~/.codex/config.toml`:

```toml
[mcp_servers.cryosparc]
command = "/absolute/path/to/em-claw/libexec/em-claw-cryosparc-mcp"
env = { EMCLAW_CRYOSPARC_ENV_FILE = "/absolute/path/to/em-claw/profiles/cryosparc/.env" }
```

## 7. Check the installation

```sh
bin/em-claw --dry-run cryosparc
bin/em-claw --dry-run relion
bin/em-claw --dry-run combine
```

A dry run validates the profile, environment files, paths and prerequisites,
then prints a non-secret summary without starting Claude or an MCP server. Fix
anything it reports before continuing.

On a cluster whose compute nodes have no internet access, run each route once
on a login node with network access. uv fills its cache under
`~/.cache/em-claw/uv-cache` (or `EMCLAW_RUNTIME_DIR`), and later launches reuse
it offline.

## 8. Run a CryoSPARC reconstruction project

1. Create the project in the CryoSPARC web UI and note its UID (e.g. `P8`).
2. Describe the dataset. Copy the template and replace the placeholders:

   ```sh
   cp profiles/cryosparc/base_datainfo.example.yaml profiles/cryosparc/base_datainfo.yaml
   $EDITOR profiles/cryosparc/base_datainfo.yaml
   ```

   This root file stages the **next** project. agentM copies it to
   `projects/<name>/base_datainfo.yaml` when the project starts.
3. Launch the agent and ask it to start the project:

   ```sh
   bin/em-claw cryosparc          # or: bin/em-claw   (combined CryoSPARC + RELION)
   ```

   ```text
   > Start a new project from base_datainfo.yaml and run phase 1.
   ```

4. agentM runs one phase at a time: it gathers precedents (subR) and guidance
   (subS), drafts three plans, has them validated (subV), executes the chosen
   plan (subC) and reports a validated reconstruction. Review the result and
   give feedback for the next phase.
5. Resume later with the same route:

   ```sh
   bin/em-claw cryosparc --resume
   ```

Each project's state lives in `projects/<name>/runs/` inside the agent
directory (`phase_state.md`, `RESUME.md`, decision logs).

## 9. Run RELION or a cross-backend handoff

```sh
bin/em-claw relion             # RELION only
bin/em-claw combine            # both toolsets in one session
```

In the combined route, hand particles from CryoSPARC to RELION explicitly:
name the CryoSPARC `P#`/`J#`, let the agent call
`mcp__cryosparc__inspect_project_job`, and approve the import into a project
under `RELION_PROJECT_ROOTS`. Every RELION write needs `confirm=true` and your
approval of the processing segment. See README.md, "Cross-backend work".

## 10. Run the tests

```sh
PYTHONDONTWRITEBYTECODE=1 uv run --no-project --python 3.11 --with pytest \
  python -m pytest tests/integration -q -p no:cacheprovider
```

Run them before every push. `test_publishable_tree.py` fails if a tracked file
contains a home-directory path, a private IP address, a real email address or
an `.env` file, and, when your `profiles/*/.env` exists, any of its CryoSPARC
license, host, account or password values.

## Troubleshooting

| Message | Fix |
| --- | --- |
| `bubblewrap is required to protect the original CryoSPARC source` | Install `bwrap` or set `EMCLAW_BWRAP_BIN` |
| `original CryoSPARC Claude agents` / `skills` missing | Point `EMCLAW_CRYOSPARC_SOURCE` at `profiles/cryosparc-agent` (section 3) |
| `... is missing from the CryoSPARC environment` | Fill every required variable in `profiles/cryosparc/.env` |
| `uv is unavailable ...` | Install uv, or set `EMCLAW_UV_BIN` to its absolute path |
| `Claude Code executable is unavailable` | Install Claude Code, or set `EMCLAW_CLAUDE_BIN` |
| RELION project access is blocked | Add the project directory to `RELION_PROJECT_ROOTS` and relaunch |
| A helper reports a `YOUR_..._PATH` value | Set the matching `EMCLAW_*` variable from section 6 |
