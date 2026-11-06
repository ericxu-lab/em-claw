#!/usr/bin/env python3
"""Escalate only explicit overwrite requests for RELION map rescaling."""

import json
import sys


TOOL_NAME = "mcp__relion__relion_rescale_map"


def decide(permission, reason):
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": permission,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    sys.stdout.write("\n")
    return 0


def block(message):
    print(message, file=sys.stderr)
    return 2


def main():
    try:
        event = json.load(sys.stdin)
    except (TypeError, ValueError):
        return block("Blocked rescale request: malformed PreToolUse input.")

    if (
        not isinstance(event, dict)
        or event.get("hook_event_name") != "PreToolUse"
        or event.get("tool_name") != TOOL_NAME
    ):
        return block("Blocked rescale request: unexpected PreToolUse event.")

    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return block("Blocked rescale request: tool_input must be an object.")

    overwrite = tool_input.get("overwrite", False)
    if overwrite is False:
        return decide(
            "allow",
            "This rescale does not request replacement of an existing output map.",
        )
    if overwrite is not True:
        return block("Blocked rescale request: overwrite must be a boolean.")

    return decide(
        "ask",
        "This rescale explicitly replaces an existing output map.",
    )


if __name__ == "__main__":
    sys.exit(main())
