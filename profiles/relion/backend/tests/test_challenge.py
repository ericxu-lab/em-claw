from __future__ import annotations

from relion_mcp.challenge import SMOKE_POSITIONS, VisualChallengeStore


def test_visual_challenge_success_and_no_reuse() -> None:
    tokens = iter(["ALPHA234", "BRAVO567", "CHARLIE8", "DELTA999"])
    store = VisualChallengeStore(
        clock=lambda: 100.0,
        token_factory=lambda: next(tokens),
        id_factory=lambda: "challenge-1",
    )
    challenge_id, labels = store.create()
    assert challenge_id == "challenge-1"
    assert tuple(labels) == SMOKE_POSITIONS
    result = store.verify(challenge_id, labels)
    assert result["passed"] is True
    assert all(result["matches"].values())
    assert store.verify(challenge_id, labels)["reason"] == "unknown_or_expired_challenge"


def test_visual_challenge_attempt_limit_and_expiry() -> None:
    now = [0.0]
    counter = iter(range(100))
    store = VisualChallengeStore(
        ttl_seconds=10,
        max_attempts=2,
        clock=lambda: now[0],
        token_factory=lambda: f"TOKEN{next(counter):03d}",
        id_factory=lambda: f"challenge-{next(counter)}",
    )
    challenge_id, _ = store.create()
    wrong = {position: "WRONG999" for position in SMOKE_POSITIONS}
    first = store.verify(challenge_id, wrong)
    assert first["passed"] is False
    assert first["attempts_remaining"] == 1
    second = store.verify(challenge_id, wrong)
    assert second["attempts_remaining"] == 0
    assert "visual_access=unavailable" in second["instruction"]
    assert "continue work independent of visual evidence" in second["instruction"]
    assert "Ask for help only if essential visual evidence" in second["instruction"]
    assert store.verify(challenge_id, wrong)["reason"] == "unknown_or_expired_challenge"

    challenge_id, labels = store.create()
    now[0] = 11.0
    expired = store.verify(challenge_id, labels)
    assert expired["reason"] == "unknown_or_expired_challenge"
