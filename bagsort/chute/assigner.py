"""Chute assignment with a generation counter.

Re-assigning a flight to a different chute bumps the generation. A caller that
captured the old generation is refused, which is how a stale in-memory routing
cache is prevented from sending bags to a chute that has already been closed.

An assignment also stamps the flight schedule generation it was planned
against. The two generations therefore corroborate each other: a console hint
can show which schedule the chute plan belongs to, and a schedule revision
makes a plan visibly older without needing a chute reassignment to say so.

The stale decision itself lives in :class:`~bagsort.generations.GenerationGuard`
so the reject/adopt policy, the explanation and the audit entry are shared with
the flight schedule stream.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bagsort.clock import Clock
from bagsort.errors import ValidationError
from bagsort.model.entities import ChuteAssignment
from bagsort.model.errors import unknown_flight
from bagsort.store.store import Store

if TYPE_CHECKING:
    from bagsort.generations import GenerationGuard


class ChuteAssigner:
    """Assigns each flight's bags to a chute, versioned by generation."""

    def __init__(self, store: Store, clock: Clock, guard: "GenerationGuard") -> None:
        self._store = store
        self._clock = clock
        self._guard = guard

    def assign(self, flight_id: str, chute_id: str) -> ChuteAssignment:
        flight = self._store.state().flights.get(flight_id)
        if flight is None:
            raise unknown_flight(flight_id)
        identifier = (chute_id or "").strip()
        if not identifier:
            raise ValidationError("chute id must not be empty")
        previous = self._store.state().assignments.get(flight_id)
        generation = (previous.generation + 1) if previous else 1
        assignment = ChuteAssignment(
            flight_id=flight_id,
            chute_id=identifier,
            generation=generation,
            updated_at=self._clock.now_iso(),
            schedule_generation=flight.schedule_generation,
        )
        with self._store.session() as session:
            session.state.assignments[flight_id] = assignment
            session.record("chute.assign", assignment.as_dict(), self._clock.now_iso())
        return assignment

    def reassign(self, flight_id: str, chute_id: str) -> ChuteAssignment:
        return self.assign(flight_id, chute_id)

    def current(self, flight_id: str) -> ChuteAssignment | None:
        return self._store.state().assignments.get(flight_id)

    def require(self, flight_id: str) -> ChuteAssignment:
        assignment = self.current(flight_id)
        if assignment is None:
            raise ValidationError(f"flight {flight_id!r} has no chute assignment")
        return assignment

    def ensure_current(self, flight_id: str, generation: int):
        """Resolve a held assignment generation through the freshness guard.

        Returns ``(assignment, verdict)`` where ``verdict`` is ``None`` for a
        fresh reference and a :class:`StaleVerdict` when the policy adopted the
        current generation on the caller's behalf.
        """

        return self._guard.resolve_assignment(
            flight_id, generation, audit_stage="chute"
        )

    def by_chute(self) -> dict[str, list[str]]:
        grouped: dict[str, list[str]] = {}
        for flight_id, assignment in self._store.state().assignments.items():
            grouped.setdefault(assignment.chute_id, []).append(flight_id)
        return {chute: sorted(flights) for chute, flights in sorted(grouped.items())}

    def count(self) -> int:
        return len(self._store.state().assignments)
