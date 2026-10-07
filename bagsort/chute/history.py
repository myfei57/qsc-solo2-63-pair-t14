"""Assignment history rebuilt from the chute commands in the journal.

Every assignment a flight has ever had is kept. A new generation never removes
the old rows; the history is the recomputation basis a stale caller is pointed
at, so each row also carries the journal sequence it was committed at.
"""

from __future__ import annotations

from dataclasses import dataclass

from bagsort.model.entities import ChuteAssignment
from bagsort.store.store import Store


@dataclass(frozen=True)
class HistoricalAssignment:
    """One past assignment together with its journal coordinates."""

    assignment: ChuteAssignment
    journal_sequence: int
    journal_kind: str

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "journal_sequence": self.journal_sequence,
            "journal_kind": self.journal_kind,
        }
        payload.update(self.assignment.as_dict())
        return payload


class AssignmentHistory:
    """Every assignment a flight has ever had, in commit order."""

    def __init__(self, store: Store) -> None:
        self._store = store

    def records(self, flight_id: str) -> list[HistoricalAssignment]:
        rows: list[HistoricalAssignment] = []
        for entry in self._store.entries():
            if entry.kind != "chute.assign" or entry.payload.get("flight_id") != flight_id:
                continue
            rows.append(
                HistoricalAssignment(
                    assignment=ChuteAssignment.from_dict(entry.payload),
                    journal_sequence=entry.sequence,
                    journal_kind=entry.kind,
                )
            )
        return rows

    def for_flight(self, flight_id: str) -> list[ChuteAssignment]:
        return [record.assignment for record in self.records(flight_id)]

    def locate(self, flight_id: str, generation: int) -> HistoricalAssignment | None:
        """The committed row for a specific generation, or ``None`` if unknown."""

        for record in self.records(flight_id):
            if record.assignment.generation == generation:
                return record
        return None
