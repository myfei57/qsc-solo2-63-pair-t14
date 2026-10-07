"""Generation freshness policy and the stale-reference guard."""

from __future__ import annotations

from .guard import CHUTE_ASSIGNMENT, FLIGHT_SCHEDULE, GenerationGuard
from .policy import ADOPT, POLICIES, REJECT, StaleVerdict

__all__ = [
    "ADOPT",
    "CHUTE_ASSIGNMENT",
    "FLIGHT_SCHEDULE",
    "GenerationGuard",
    "POLICIES",
    "REJECT",
    "StaleVerdict",
]
