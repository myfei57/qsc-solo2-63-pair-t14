"""Generation evidence assembled straight from the committed journal.

On-call needs more than "your generation is stale". They need enough material
to recompute the verdict themselves: the committed journal sequence each
allocation was recorded at, the full generation lineage of a flight, and both
of the generations that collide (chute allocation and flight schedule). Every
value here is read from committed journal entries, so the evidence survives a
restart and can be replayed independently of the materialised state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bagsort.model.entities import ChuteAssignment, Flight
from bagsort.store.store import Store


@dataclass(frozen=True)
class AssignmentRevision:
    """One committed chute allocation, with its journal anchor."""

    assignment: ChuteAssignment
    journal_sequence: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "journal_sequence": self.journal_sequence,
            "at": self.at,
            **self.assignment.as_dict(),
        }


@dataclass(frozen=True)
class ScheduleRevision:
    """One committed flight schedule revision, with its journal anchor."""

    generation: int
    cutoff_at: str
    journal_sequence: int
    at: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "generation": self.generation,
            "cutoff_at": self.cutoff_at,
            "journal_sequence": self.journal_sequence,
            "at": self.at,
        }


class GenerationEvidence:
    """Reads the generation lineage of a flight out of the committed log."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def assignment_lineage(self, flight_id: str) -> list[AssignmentRevision]:
        """Every ``chute.assign`` ever committed for the flight, oldest first."""

        return [
            AssignmentRevision(
                assignment=ChuteAssignment.from_dict(entry.payload),
                journal_sequence=entry.sequence,
                at=entry.at,
            )
            for entry in self._store.entries()
            if entry.kind == "chute.assign"
            and entry.payload.get("flight_id") == flight_id
        ]

    def current_assignment(self, flight_id: str) -> AssignmentRevision | None:
        lineage = self.assignment_lineage(flight_id)
        return lineage[-1] if lineage else None

    def assignment_at(self, flight_id: str, generation: int) -> AssignmentRevision | None:
        """The historical allocation a stale reference was holding, if kept."""

        for revision in self.assignment_lineage(flight_id):
            if revision.assignment.generation == generation:
                return revision
        return None

    def schedule_lineage(self, flight_id: str) -> list[ScheduleRevision]:
        """Every committed flight version carrying a schedule generation.

        A flight is born at schedule generation 1; each ``flight.put`` that
        changes the generation is one revision point. Both the registration and
        the revisions come from the same ``flight.put`` stream the state is
        rebuilt from, so this lineage cannot disagree with the schedule board.
        """

        revisions: list[ScheduleRevision] = []
        seen: set[int] = set()
        for entry in self._store.entries():
            if entry.kind != "flight.put":
                continue
            flight = Flight.from_dict(entry.payload["flight"])
            if flight.id != flight_id or flight.schedule_generation in seen:
                continue
            seen.add(flight.schedule_generation)
            revisions.append(
                ScheduleRevision(
                    generation=flight.schedule_generation,
                    cutoff_at=flight.cutoff_at,
                    journal_sequence=entry.sequence,
                    at=entry.at,
                )
            )
        return revisions

    def current_schedule(self, flight_id: str) -> ScheduleRevision | None:
        lineage = self.schedule_lineage(flight_id)
        return lineage[-1] if lineage else None

    def flight_live(self, flight_id: str) -> bool:
        """Whether the flight exists in the committed, materialised state.

        The journal keeps its full lineage regardless; liveness only decides
        whether a request may still act against it.
        """

        return flight_id in self._store.state().flights

    def watermark(self) -> int:
        """Commit watermark the verdict was computed against."""

        return self._store.watermark
