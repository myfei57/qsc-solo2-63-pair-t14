"""Flight schedule revision history rebuilt from the schedule journal.

Each revision is its own committed command (``flight.schedule``), so the
sequence of schedule generations is independently replayable and a revision
never erases the cutoff a caller may still hold.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bagsort.store.store import Store


@dataclass(frozen=True)
class ScheduleRevision:
    """One published cutoff together with the generation that produced it."""

    flight_id: str
    cutoff_at: str
    generation: int
    journal_sequence: int
    journal_kind: str = "flight.schedule"
    revised_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "flight_id": self.flight_id,
            "cutoff_at": self.cutoff_at,
            "generation": self.generation,
            "journal_sequence": self.journal_sequence,
            "journal_kind": self.journal_kind,
            "revised_at": self.revised_at,
        }


class ScheduleHistory:
    """Every schedule revision a flight has ever published, in commit order."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def records(self, flight_id: str) -> list[ScheduleRevision]:
        revisions: list[ScheduleRevision] = []
        # Generation 1 is established implicitly at registration: the first
        # flight.put carries the initial cutoff and schedule_generation, so the
        # history starts there rather than at the first explicit revision.
        for entry in self._store.entries():
            if entry.kind == "flight.put" and entry.payload.get("flight", {}).get("id") == flight_id:
                flight = entry.payload["flight"]
                revisions.append(
                    ScheduleRevision(
                        flight_id=flight_id,
                        cutoff_at=str(flight.get("cutoff_at", "")),
                        generation=int(flight.get("schedule_generation", 1)),
                        journal_sequence=entry.sequence,
                        journal_kind="flight.put",
                        revised_at=entry.at,
                    )
                )
                break
        revisions.extend(
            ScheduleRevision(
                flight_id=str(entry.payload["flight_id"]),
                cutoff_at=str(entry.payload.get("cutoff_at", "")),
                generation=int(entry.payload.get("generation", 0)),
                journal_sequence=entry.sequence,
                revised_at=entry.at,
            )
            for entry in self._store.entries()
            if entry.kind == "flight.schedule" and entry.payload.get("flight_id") == flight_id
        )
        return revisions

    def locate(self, flight_id: str, generation: int) -> ScheduleRevision | None:
        """The committed revision for a specific generation, or ``None``."""

        for revision in self.records(flight_id):
            if revision.generation == generation:
                return revision
        return None
