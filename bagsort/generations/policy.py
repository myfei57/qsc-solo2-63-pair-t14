"""Generation freshness: the stale-reference policy and its safety/availability trade.

Two references in the plant are versioned by a generation:

* a chute assignment (``ChuteAssignment.generation``), and
* a flight's schedule (``Flight.schedule_generation``).

A caller that captured an old generation must be told *why* it was refused and
*what to fetch* instead of receiving an opaque failure. The policy here decides
what happens to such a stale request:

``REJECT`` (default, safety first)
    The request is refused with the full :class:`StaleVerdict` payload. No bag
    is routed against a chute plan the caller did not agree to. The cost is
    availability: **every** stale request fails and must be retried with a fresh
    allocation, so availability for stale traffic is zero by construction.

``ADOPT`` (availability first)
    The request transparently continues against the current generation. This
    keeps traffic moving when the caller is happy to follow the newest plan, but
    it rewrites the request's original semantics. The availability ceiling is
    lower than it looks: adoption only succeeds when a current generation
    actually exists (the flight is registered and still has an assignment /
    schedule). A reference older than the retained history, or one for a retired
    flight, is still rejected -- adoption cannot resurrect a plan that no longer
    exists, so stale availability is capped at "a current version is retrievable".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

REJECT = "reject"
ADOPT = "adopt"
POLICIES: tuple[str, ...] = (REJECT, ADOPT)

# Human readable availability ceiling, surfaced verbatim by the console so the
# trade behind the chosen mode does not live only in source comments.
AVAILABILITY = {
    REJECT: (
        "stale requests are never served: 0% of stale traffic continues; every "
        "caller must re-fetch the current generation and retry"
    ),
    ADOPT: (
        "stale requests are served against the current generation only while a "
        "current generation still exists; references older than retained history "
        "or to retired flights are still rejected, so stale availability is "
        "bounded by 'current version retrievable' and never reaches 100%"
    ),
}


@dataclass(frozen=True)
class StaleVerdict:
    """The full explanation a stale caller needs, plus a recomputation basis.

    ``held_generation`` is what the caller believed in, ``current_generation``
    what the plant now holds, and ``generation_of`` names the versioned stream
    (``chute_assignment`` or ``flight_schedule``). ``evidence`` carries the
    journal coordinates (command kind and sequence) of both versions, so an
    operator can replay the journal and confirm every number independently.
    """

    flight_id: str
    generation_of: str
    held_generation: int
    current_generation: int
    policy: str
    decision: str
    reason: str
    notice: str
    availability: str
    refetch: dict[str, Any]
    evidence: dict[str, Any] = field(default_factory=dict)
    adopted: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "generation_of": self.generation_of,
            "held_generation": self.held_generation,
            "current_generation": self.current_generation,
            "policy": self.policy,
            "decision": self.decision,
            "reason": self.reason,
            "notice": self.notice,
            "availability_ceiling": self.availability,
            "refetch": self.refetch,
            "evidence": self.evidence,
            "adopted": self.adopted,
        }


def require_policy(mode: str) -> str:
    """Validate an operator supplied policy name."""

    if mode not in POLICIES:
        raise ValueError(f"unknown stale policy {mode!r}; expected one of {', '.join(POLICIES)}")
    return mode
