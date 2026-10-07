"""Flight schedule revisions and the expiring schedule generation.

A revision bumps the generation. Anything that captured the previous generation
is then refused, so a deadline or recheck plan computed against an old schedule
cannot be applied to the new one.

Each revision is committed as its own ``flight.schedule`` command (in addition
to the updated flight document), which keeps the full sequence of cutoffs and
generations replayable after a rotation instead of leaving only the latest
value. Stale resolution goes through
:class:`~bagsort.generations.GenerationGuard`, the same policy point chute
assignments use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from dataclasses import dataclass

from bagsort.clock import Clock
from bagsort.model.entities import Flight
from bagsort.model.errors import unknown_flight
from bagsort.store.store import Store

if TYPE_CHECKING:
    from bagsort.generations import GenerationGuard


@dataclass(frozen=True)
class Schedule:
    """A flight's cutoff time together with the generation that produced it."""

    flight_id: str
    cutoff_at: str
    generation: int

    def as_dict(self) -> dict[str, object]:
        return {
            "flight_id": self.flight_id,
            "cutoff_at": self.cutoff_at,
            "generation": self.generation,
        }


class ScheduleBoard:
    """Publishes and revises the schedule every downstream plan depends on."""

    def __init__(self, store: Store, clock: Clock, guard: "GenerationGuard") -> None:
        self._store = store
        self._clock = clock
        self._guard = guard

    def current(self, flight_id: str) -> Schedule:
        flight = self._flight(flight_id)
        return Schedule(
            flight_id=flight.id,
            cutoff_at=flight.cutoff_at,
            generation=flight.schedule_generation,
        )

    def revise(self, flight_id: str, cutoff_at: str) -> Schedule:
        """Publish a new cutoff and invalidate every older generation."""

        flight = self._flight(flight_id)
        cutoff = cutoff_at.strip()
        at = self._clock.now_iso()
        updated = flight.evolved(
            cutoff_at=cutoff,
            schedule_generation=flight.schedule_generation + 1,
        )
        with self._store.session() as session:
            session.state.flights[updated.id] = updated
            session.record("flight.put", {"flight": updated.as_dict()}, at)
            session.record(
                "flight.schedule",
                {
                    "flight_id": updated.id,
                    "cutoff_at": cutoff,
                    "generation": updated.schedule_generation,
                },
                at,
            )
        return Schedule(
            flight_id=updated.id,
            cutoff_at=updated.cutoff_at,
            generation=updated.schedule_generation,
        )

    def ensure_current(self, flight_id: str, generation: int):
        """Resolve a held schedule generation through the freshness guard."""

        return self._guard.resolve_schedule(
            flight_id, generation, audit_stage="audit"
        )

    def _flight(self, flight_id: str) -> Flight:
        flight = self._store.state().flights.get(flight_id)
        if flight is None:
            raise unknown_flight(flight_id)
        return flight
