#!/usr/bin/env python
"""agentM's CLOCK. Emit one line whenever a CryoSPARC job leaves the active set.

    python tools/watch_jobs.py <PROJECT_UID> <WORKSPACE_UID> [POLL_SECONDS]
    python tools/watch_jobs.py P5 W28 60

Arm it through the Monitor tool, persistent, filtering to the event lines:

    Monitor(command="../../libexec/em-claw-cryosparc-python -u tools/watch_jobs.py P5 W28 2>&1 | "
                    "grep -E --line-buffered 'DONE|\\*\\*\\*|WATCHING|IDLE|WARN'",
            persistent=True, timeout_ms=3600000,
            description="<proj>/<ws> job completions and failures")

WHY THIS EXISTS -- read before deciding to skip it.
  agentM has no clock. subC parks after queueing a long job (correctly: holding the
  turn open burns the context the checkpoint exists to preserve), and agentM's own
  turn ends after it reports. CLAUDE.md assigns the polling to agentM -- but a duty
  assigned in prose with no mechanism is not performed. Measured 2026-09-02 on GCGR:
  five arms completed and one FAILED while agentM believed all six were running, and
  the human had to act as the clock. Every earlier instance was caught only by
  accident, when a sibling subC queried the job table for its own placement decision.

COVERAGE RULE: emit on EVERY terminal state, not just success. Silence must mean
"still running", never "died quietly". A watcher that greps only for completion is
indistinguishable from no watcher at all when a job crashes.
"""
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from cs_connect import get_cs

# ⚠ MEASURED 2026-09-22 (jyf1): "waiting" was MISSING and it cost a silent miss.
# The watcher snapshots the active set at ARM time and reports a UID only when it LEAVES that set.
# So any job sitting in an untracked state at the moment a watcher is armed NEVER ENTERS the set and
# its completion can NEVER be reported — and each re-arm rebuilds the set, so a long job re-armed
# around is permanently invisible to the new watcher. J2732 (class_2D_new) completed this way with
# ZERO events delivered in a full 30-minute window, and was only found by a live query.
# 🔒 Silence from this tool is NOT evidence a job is still running. Confirm live before concluding.
ACTIVE_STATES = ("running", "launched", "queued", "started", "waiting")

# ⚠ STILL OPEN, deliberately not changed here (escalated): a type in SKIP_TYPES never enters the
# active set, so it can never leave it — meaning a snowflake that FAILS is invisible, not merely
# unreported while alive. Reporting terminal states for skipped types needs separate tracking, which
# is a larger change than this one-line coverage fix.
SKIP_TYPES = {"snowflake"}          # stale interactive Live sessions; they never terminate


def active_uids(cs, proj, ws):
    """UIDs of non-snowflake jobs currently active in proj/ws."""
    found = set()
    for st in ACTIVE_STATES:
        try:
            rows = cs.cli.get_jobs_by_status(st) or []
        except Exception:
            continue                                    # one bad status must not blind the rest
        for r in rows:
            if not isinstance(r, dict) or r.get("project_uid") != proj:
                continue
            if r.get("type") in SKIP_TYPES:
                continue
            if ws in (r.get("workspace_uids") or []):
                found.add(r["uid"])
    return found


def finish_line(cs, proj, uid):
    try:
        d = cs.find_project(proj).find_job(uid).doc
        status, jtype = d.get("status", "?"), d.get("type", "?")
        if status == "completed":
            return f"DONE {uid} ({jtype}) completed"
        errs = d.get("errors_run") or []
        msg = ""
        if errs:
            e0 = errs[0]
            msg = str(e0.get("message", e0) if isinstance(e0, dict) else e0)[:160]
        return f"*** {status.upper()} *** {uid} ({jtype}) {msg}"
    except Exception as e:
        return f"*** UNKNOWN *** {uid} left the active set ({type(e).__name__})"


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    proj, ws = sys.argv[1], sys.argv[2]
    poll = int(sys.argv[3]) if len(sys.argv) > 3 else 60

    cs = get_cs()
    prev = active_uids(cs, proj, ws)
    print(f"WATCHING {proj}/{ws}: {', '.join(sorted(prev)) if prev else 'no active jobs'}", flush=True)

    while True:
        time.sleep(poll)
        try:
            cur = active_uids(cs, proj, ws)
        except Exception as e:                          # transient failure must not kill the watch
            print(f"WARN poll failed ({type(e).__name__}) - retrying", flush=True)
            try:
                cs = get_cs()
            except Exception:
                pass
            continue

        for uid in sorted(prev - cur):
            print(finish_line(cs, proj, uid), flush=True)

        if prev and not cur:
            # ⚠ MEASURED 2026-09-22: this used to read "ALL JOBS IDLE - nothing active", which CLAIMS
            # MORE THAN THE TOOL KNOWS. It fires when the TRACKED set empties — jobs queued since the
            # last poll are not in `cur` yet, so it fired while two refines were live and had just been
            # submitted. Silence and this line are both statements about the WATCHER, not the queue.
            print(f"WATCHED SET EMPTY in {proj}/{ws} - all tracked jobs finished "
                  f"(jobs queued since the last poll are NOT counted; confirm live)", flush=True)
        prev = cur


if __name__ == "__main__":
    main()
