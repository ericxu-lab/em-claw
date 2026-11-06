# EM-Claw launcher guard

This directory is the EM-Claw packaging and dispatch root, not a cryo-EM
processing project. Do not create campaign state or run backend processing from
an unconfigured root session.

Exit this unconfigured root session and launch a route:

- `bin/em-claw` for the default combined route
- `bin/em-claw cryosparc` for the CryoSPARC MCP only
- `bin/em-claw relion` for the RELION MCP only
- `bin/em-claw combine` for both independent MCP servers in one session

Use `bin/em-claw --dry-run <route>` to preview and validate a route without
starting Claude or either MCP; omitting `<route>` previews `combine`. Use
`-r`/`--resume [ID_OR_SEARCH]` to resume a conversation, and always use the same
route that created it. A resume restores the conversation while starting fresh
MCP children from that route's current configuration and environment. Follow
the selected profile's instructions and keep state in the selected CryoSPARC or
approved RELION project. Put companion Claude options such as `--fork-session`
after the launcher's `--` separator.
