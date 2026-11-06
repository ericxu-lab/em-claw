from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Mapping
from threading import Lock
from typing import Any

SMOKE_POSITIONS = ("top_left", "top_right", "bottom_left", "bottom_right")
_SMOKE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _default_token(length: int = 8) -> str:
    return "".join(secrets.choice(_SMOKE_ALPHABET) for _ in range(length))


class VisualChallengeStore:
    """Short-lived, in-memory visual challenges used to prove model pixel access."""

    def __init__(
        self,
        *,
        ttl_seconds: int = 600,
        max_attempts: int = 3,
        max_active: int = 32,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] = _default_token,
        id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(18),
    ) -> None:
        if ttl_seconds <= 0 or max_attempts <= 0 or max_active <= 0:
            raise ValueError("Challenge limits must be positive.")
        self.ttl_seconds = ttl_seconds
        self.max_attempts = max_attempts
        self.max_active = max_active
        self._clock = clock
        self._token_factory = token_factory
        self._id_factory = id_factory
        self._lock = Lock()
        self._challenges: dict[str, dict[str, Any]] = {}

    def _prune_locked(self, now: float) -> None:
        expired = [
            challenge_id
            for challenge_id, challenge in self._challenges.items()
            if float(challenge["expires_at"]) <= now
        ]
        for challenge_id in expired:
            self._challenges.pop(challenge_id, None)
        while len(self._challenges) >= self.max_active:
            oldest = min(
                self._challenges,
                key=lambda key: float(self._challenges[key]["created_at"]),
            )
            self._challenges.pop(oldest, None)

    def create(self) -> tuple[str, dict[str, str]]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            challenge_id = self._id_factory()
            while challenge_id in self._challenges:
                challenge_id = self._id_factory()
            labels = {position: self._token_factory() for position in SMOKE_POSITIONS}
            self._challenges[challenge_id] = {
                "created_at": now,
                "expires_at": now + self.ttl_seconds,
                "attempts": 0,
                "labels": labels,
            }
        return challenge_id, dict(labels)

    def verify(self, challenge_id: str, labels: Mapping[str, str]) -> dict[str, Any]:
        now = self._clock()
        with self._lock:
            self._prune_locked(now)
            challenge = self._challenges.get(challenge_id)
            if challenge is None:
                return {
                    "passed": False,
                    "reason": "unknown_or_expired_challenge",
                    "required_keys": list(SMOKE_POSITIONS),
                }

            expected: dict[str, str] = challenge["labels"]
            provided = {
                str(key).strip().lower(): str(value).strip().upper()
                for key, value in labels.items()
            }
            matches = {
                position: provided.get(position) == expected[position]
                for position in SMOKE_POSITIONS
            }
            challenge["attempts"] = int(challenge["attempts"]) + 1
            passed = all(matches.values())
            attempts_remaining = max(0, self.max_attempts - int(challenge["attempts"]))
            if passed or attempts_remaining == 0:
                self._challenges.pop(challenge_id, None)

        return {
            "passed": passed,
            "matches": matches,
            "attempts_remaining": attempts_remaining,
            "reason": "image_content_verified" if passed else "one_or_more_tokens_incorrect",
            "instruction": (
                "Record visual_access=direct only when passed=true. "
                "Otherwise record visual_access=unavailable and inspect the image again while "
                "attempts remain. After attempts are exhausted, restore image access and obtain "
                "a new challenge; continue work independent of visual evidence. Ask for help "
                "only if essential visual evidence blocks all useful authorized progress."
            ),
        }
