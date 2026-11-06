"""
negatives.py — read the advisory NEGATIVE-result ledger so agents know which jobs are negative
(human-indicated or bad-parameter) and don't mistake them for good results.

This is INFORMATIONAL, not a guard: it never raises and never blocks queueing. A negative result is
still a result — a bad reference volume or bad 2D classes can be reused on purpose as a junk-sink /
decoy to attract bad particles during cleaning. So we annotate; the agent decides.

The ledger lives in runs/phase_state.md between <!-- neg-list-begin --> / <!-- neg-list-end -->.
Each entry line:
    - NEG <token> [<token> ...] — why: <...>; possible-reuse: <...>; GOOD: <clean frontier>. [<src/date>]
where <token> is a workspace (W3), a job range (J3-J23), or a UID list (J5,J9).

Usage (advisory — print/log, do not gate on it):
    from negatives import annotate_inputs
    for uid, why in annotate_inputs(connections):
        print(f"NOTE: input {uid} is a negative result — {why}. Reuse only if intentional.")
"""
from __future__ import annotations
import re
from pathlib import Path

PHASE_STATE = Path(__file__).resolve().parent / 'runs' / 'phase_state.md'


def _expand_token(tok: str) -> tuple[set[str], set[str]]:
    tok = tok.strip().strip(',')
    if re.fullmatch(r'W\d+', tok):
        return {tok}, set()
    m = re.fullmatch(r'J(\d+)-J(\d+)', tok)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return set(), {f'J{i}' for i in range(a, b + 1)}
    if re.fullmatch(r'J\d+', tok):
        return set(), {tok}
    return set(), set()


def load_negatives(path: str | Path = PHASE_STATE):
    """Parse the ledger → (neg_workspaces:set, neg_uid_reasons:dict[uid->why])."""
    text = Path(path).read_text()
    m = re.search(r'<!-- neg-list-begin -->(.*?)<!-- neg-list-end -->', text, re.S)
    body = m.group(1) if m else ''
    wss: set[str] = set()
    uid_why: dict[str, str] = {}
    # entries may wrap across indented continuation lines; rejoin then split on "- NEG"
    joined = re.sub(r'\n\s+', ' ', body)
    for chunk in joined.splitlines():
        chunk = chunk.strip()
        if not chunk.startswith('- NEG'):
            continue
        head, _, tail = chunk[len('- NEG'):].partition('—')
        whym = re.search(r'why:\s*(.*?)(?:;|$)', tail)
        why = (whym.group(1).strip() if whym else tail.strip())[:120]
        for tok in re.split(r'\s+', head.strip()):
            for t in tok.split(','):
                w, u = _expand_token(t)
                wss |= w
                for x in u:
                    uid_why[x] = why
    return wss, uid_why


def _iter_sources(connections: dict):
    for _name, val in connections.items():
        items = val if isinstance(val, list) else [val]
        for item in items:
            if isinstance(item, (tuple, list)) and item:
                yield str(item[0])


def annotate_inputs(connections: dict, proj=None, path: str | Path = PHASE_STATE):
    """Return [(uid, why), ...] for inputs that touch a negative job — by UID, or by workspace if
    `proj` is given. Advisory only: caller prints/logs and decides. Never raises on a match."""
    wss, uid_why = load_negatives(path)
    out = []
    for uid in _iter_sources(connections):
        if uid in uid_why:
            out.append((uid, uid_why[uid]))
        elif proj is not None and wss:
            try:
                jws = proj.find_job(uid).doc.get('workspace_uids', [])
            except Exception:
                jws = []
            hit = wss.intersection(jws)
            if hit:
                out.append((uid, f'in negative workspace {sorted(hit)}'))
    return out


if __name__ == '__main__':
    wss, uid_why = load_negatives()
    print('negative workspaces:', sorted(wss))
    print('negative uids:', sorted(uid_why, key=lambda u: int(u[1:])))
