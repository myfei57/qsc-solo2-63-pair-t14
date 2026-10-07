"""Shared construction of the structured stale-generation error.

The domain layer (``ensure_current``) and the cross-checking
:class:`~bagsort.generations.guard.GenerationGuard` build the same refusal
shape from one place, so the message an in-process caller receives cannot
drift away from the message the console returns.
"""

from __future__ import annotations

from typing import Any

from bagsort.errors import StaleError
from . import policy as policies

CHUTE_KIND = "chute_assignment"
SCHEDULE_KIND = "flight_schedule"


def stale_generation(
    *,
    kind: str,
    flight_id: str,
    presented: int,
    current_generation: int | None,
    refetch_path: str | None,
    what: str,
    qualifier: str,
    reason: str = "generation_advanced",
    held: dict[str, Any] | None = None,
    current: dict[str, Any] | None = None,
    with_ceiling: bool = False,
) -> StaleError:
    """Refusal naming both generations, the current target and the refetch.

    ``with_ceiling`` adds the availability ceiling of ``adopt_current`` so a
    request-facing refusal always states how far auto-adoption could have
    helped. The domain-level ``ensure_current`` omits it; the console guard
    always includes it.
    """

    if current_generation is None:
        message = (
            f"{what} for {flight_id!r} has no current generation to fetch "
            f"({reason}); re-fetch the flight before retrying."
        )
        refetch = None
    else:
        message = (
            f"{what} generation {presented} for {flight_id!r} is stale; current is "
            f"generation {current_generation} ({qualifier}). Re-fetch "
            f"GET {refetch_path} and retry."
        )
        refetch = {"method": "GET", "path": refetch_path}
    details: dict[str, Any] = {
        "stale": True,
        "reference_kind": kind,
        "flight_id": flight_id,
        "presented_generation": presented,
        "current_generation": current_generation,
        "policy": policies.REJECT,
        "reason": reason,
        "refetch": refetch,
        "held": held,
        "current": current,
    }
    if with_ceiling:
        details["availability_ceiling"] = policies.describe(policies.ADOPT_CURRENT)
    return StaleError(message, details)
