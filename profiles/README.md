# EM-Claw profiles

The launcher provides three Claude Code routes; omitting the route defaults to
`combined`:

| Route | Profile | MCP servers | Environment files |
| --- | --- | --- | --- |
| `bin/em-claw cryosparc` | `cryosparc` | CryoSPARC | CryoSPARC required |
| `bin/em-claw relion` | `relion` | RELION | RELION optional |
| `bin/em-claw` or `bin/em-claw combine` | `combined` | Both | CryoSPARC required; RELION optional |

The single-backend routes load only their own environment and MCP. The
`combined` profile enables `cryosparc` and `relion` over independent stdio
connections while retaining separate Python runtimes, MCP child environments,
and write boundaries.

The launcher imports only the core values required by the selected profile from
each trusted environment file. Each selected MCP bootstrap sources its own
complete file, so backend-specific runtime variables such as `PATH`,
`PYTHONPATH`, and `LD_LIBRARY_PATH` stay with the owning backend. On the
combined route, each bootstrap scrubs the other backend's prefixed variables.

Never store processing state in profile configuration directories. CryoSPARC
state belongs in its configured projects directory, and RELION state belongs
beneath an approved `RELION_PROJECT_ROOTS` entry. Keep credentials only in the
ignored backend `.env` files, never in MCP or Claude JSON configuration.

For `combine`, `profiles/combined/projects` is a read-write mount of the
external CryoSPARC source's `projects` directory. Stable CryoSPARC helpers are
exposed from the CryoSPARC profile without linking its `.env` or unrelated local
project trees into the combined profile.

Cross-backend transfer is not implicit. The two servers do not share workflow
state or call each other; any handoff must be an explicit, reviewed
export/conversion/import workflow.

When resuming with `-r` or `--resume`, choose the same route that originally
created the conversation. Every actual non-dry-run launch, including a resume,
starts new MCP children from the selected profile's current configuration and
environment; resuming conversation history does not reuse an old MCP process.
With no route, resume uses the combined profile, and `--resume` without a value
opens Claude's conversation picker.
