"""The generation referee: explain, evidence, audit, and only then refuse.

A request carrying a generation is answered through this guard instead of a
bare comparison. On a mismatch the caller learns which generation it held,
which generation is current, what to re-fetch, and gets journal anchors the
duty engineer can replay. Every stale decision -- rejection *and* adoption --
is written to the tamper-evident audit chain, so an expired reference never
disappears without a trace.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bagsort.audit.chain import AuditChain
from bagsort.errors import ValidationError
from bagsort.model.errors import unknown_flight
from .evidence import (
    AssignmentRevision,
    GenerationEvidence,
    ScheduleRevision,
)
from . import policy as policies
from .stale import CHUTE_KIND, SCHEDULE_KIND, stale_generation


@dataclass(frozen=True)
class Adoption:
    """Result of continuing a stale request on the current generation."""

    kind: str
    flight_id: str
    presented_generation: int
    current_generation: int
    adopted_notice: str
    current: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "adopted": True,
            "kind": self.kind,
            "flight_id": self.flight_id,
            "presented_generation": self.presented_generation,
            "current_generation": self.current_generation,
            "adopted_notice": self.adopted_notice,
            "current": self.current,
        }


class GenerationGuard:
    """Decides whether a presented generation may act and records why not."""

    def __init__(self, evidence: GenerationEvidence, audit: AuditChain) -> None:
        self._evidence = evidence
        self._audit = audit

    # -- chute allocation generations -------------------------------------

    def check_assignment(
        self,
        flight_id: str,
        presented_generation: int,
        policy: str = policies.REJECT,
    ) -> AssignmentRevision | Adoption:
        """Validate a chute allocation generation for a request to act on."""

        _require_policy(policy)
        if not self._evidence.flight_live(flight_id):
            # A retired flight keeps its journal lineage for inspection, but no
            # request may act on it any more -- adoption cannot resurrect it.
            self._raise_unknown_flight(flight_id)
        current = self._evidence.current_assignment(flight_id)
        if current is None:
            # A live flight without an allocation cannot be helped either:
            # adoption must not invent an allocation, so it stays a refusal.
            self._record(
                flight_id,
                CHUTE_KIND,
                "rejected",
                policy,
                presented_generation,
                None,
                reason="no_current_assignment",
            )
            raise stale_generation(
                kind=CHUTE_KIND,
                flight_id=flight_id,
                presented=presented_generation,
                current_generation=None,
                refetch_path=None,
                what="chute allocation",
                qualifier="",
                reason="no_current_assignment",
                with_ceiling=True,
            )
        if current.assignment.generation == presented_generation:
            return current
        if policies.is_adoptive(policy):
            return self._adopt_assignment(flight_id, presented_generation, current)

        held = self._evidence.assignment_at(flight_id, presented_generation)
        reason = "generation_advanced" if held else "generation_unknown"
        assignment = current.assignment
        self._record(
            flight_id,
            CHUTE_KIND,
            "rejected",
            policy,
            presented_generation,
            assignment.generation,
            reason=reason,
            current_chute=assignment.chute_id,
            held_chute=held.assignment.chute_id if held else None,
        )
        raise stale_generation(
            kind=CHUTE_KIND,
            flight_id=flight_id,
            presented=presented_generation,
            current_generation=assignment.generation,
            refetch_path=f"/api/assignments/{flight_id}",
            what="chute allocation",
            qualifier=f"chute {assignment.chute_id!r}",
            reason=reason,
            held=held.as_dict() if held else None,
            current=current.as_dict(),
            with_ceiling=True,
        )

    def _adopt_assignment(
        self,
        flight_id: str,
        presented_generation: int,
        current: AssignmentRevision,
    ) -> Adoption:
        assignment = current.assignment
        notice = (
            f"request presented chute allocation generation {presented_generation} for "
            f"{flight_id!r}; it is stale and has been continued on current generation "
            f"{assignment.generation} (chute {assignment.chute_id!r})"
        )
        self._record(
            flight_id,
            CHUTE_KIND,
            "adopted",
            policies.ADOPT_CURRENT,
            presented_generation,
            assignment.generation,
            reason="adopted_current",
            current_chute=assignment.chute_id,
        )
        return Adoption(
            kind=CHUTE_KIND,
            flight_id=flight_id,
            presented_generation=presented_generation,
            current_generation=assignment.generation,
            adopted_notice=notice,
            current=current.as_dict(),
        )

    # -- flight schedule generations ---------------------------------------

    def check_schedule(
        self,
        flight_id: str,
        presented_generation: int,
        policy: str = policies.REJECT,
    ) -> ScheduleRevision | Adoption:
        """Validate a flight schedule generation for a request to act on."""

        _require_policy(policy)
        if not self._evidence.flight_live(flight_id):
            self._raise_unknown_flight(flight_id)
        current = self._evidence.current_schedule(flight_id)
        if current is None:
            self._raise_unknown_flight(flight_id)
        if current.generation == presented_generation:
            return current
        if policies.is_adoptive(policy):
            return self._adopt_schedule(flight_id, presented_generation, current)

        held = self._schedule_at(flight_id, presented_generation)
        reason = "generation_advanced" if held else "generation_unknown"
        self._record(
            flight_id,
            SCHEDULE_KIND,
            "rejected",
            policy,
            presented_generation,
            current.generation,
            reason=reason,
            current_cutoff=current.cutoff_at,
        )
        raise stale_generation(
            kind=SCHEDULE_KIND,
            flight_id=flight_id,
            presented=presented_generation,
            current_generation=current.generation,
            refetch_path=f"/api/flights/{flight_id}/schedule",
            what="flight schedule",
            qualifier=f"cutoff {current.cutoff_at!r}",
            reason=reason,
            held=held.as_dict() if held else None,
            current=current.as_dict(),
            with_ceiling=True,
        )

    def _adopt_schedule(
        self,
        flight_id: str,
        presented_generation: int,
        current: ScheduleRevision,
    ) -> Adoption:
        notice = (
            f"request presented flight schedule generation {presented_generation} for "
            f"{flight_id!r}; it is stale and has been continued on current generation "
            f"{current.generation} (cutoff {current.cutoff_at!r})"
        )
        self._record(
            flight_id,
            SCHEDULE_KIND,
            "adopted",
            policies.ADOPT_CURRENT,
            presented_generation,
            current.generation,
            reason="adopted_current",
            current_cutoff=current.cutoff_at,
        )
        return Adoption(
            kind=SCHEDULE_KIND,
            flight_id=flight_id,
            presented_generation=presented_generation,
            current_generation=current.generation,
            adopted_notice=notice,
            current=current.as_dict(),
        )

    def _schedule_at(self, flight_id: str, generation: int) -> ScheduleRevision | None:
        for revision in self._evidence.schedule_lineage(flight_id):
            if revision.generation == generation:
                return revision
        return None

    # -- corroborating view ------------------------------------------------

    def report(self, flight_id: str) -> dict[str, Any]:
        """Chute allocation, flight schedule and policy, with shared evidence.

        Historical generations stay readable even for a retired flight; the
        journal is the source of truth and liveness is reported, not enforced,
        here so old references can always be traced after retirement.
        """

        assignment = self._evidence.current_assignment(flight_id)
        schedule = self._evidence.current_schedule(flight_id)
        live = self._evidence.flight_live(flight_id)
        if assignment is None and schedule is None:
            raise unknown_flight(flight_id)
        return {
            "flight_id": flight_id,
            "live": live,
            "chute_assignment": _generation_view(
                CHUTE_KIND,
                current=assignment.as_dict() if assignment else None,
                lineage=[item.as_dict() for item in self._evidence.assignment_lineage(flight_id)],
                refetch_path=f"/api/assignments/{flight_id}",
            ),
            "flight_schedule": _generation_view(
                SCHEDULE_KIND,
                current=schedule.as_dict() if schedule else None,
                lineage=[item.as_dict() for item in self._evidence.schedule_lineage(flight_id)],
                refetch_path=f"/api/flights/{flight_id}/schedule",
            ),
            "policy": {
                "default": policies.REJECT,
                "reject": policies.describe(policies.REJECT),
                "adopt_current": policies.describe(policies.ADOPT_CURRENT),
            },
            "evidence": {
                "source": "committed journal entries",
                "watermark": self._evidence.watermark(),
                "recompute": (
                    "filter committed entries by kind (chute.assign / flight.put) "
                    "and flight_id, ordered by journal sequence"
                ),
            },
        }

    # -- internals ---------------------------------------------------------

    def _record(
        self,
        flight_id: str,
        kind: str,
        decision: str,
        policy: str,
        presented_generation: int,
        current_generation: int | None,
        reason: str,
        **extra: Any,
    ) -> None:
        detail: dict[str, Any] = {
            "reference_kind": kind,
            "decision": decision,
            "policy": policy,
            "presented_generation": presented_generation,
            "current_generation": current_generation,
            "reason": reason,
            "watermark": self._evidence.watermark(),
        }
        detail.update(extra)
        self._audit.record("generation.expired", flight_id, "chute", detail)

    def _raise_unknown_flight(self, flight_id: str) -> None:
        raise unknown_flight(flight_id)


def _generation_view(
    kind: str, current: dict[str, Any] | None, lineage: list[dict[str, Any]], refetch_path: str
) -> dict[str, Any]:
    return {
        "kind": kind,
        "current_generation": None if current is None else current["generation"],
        "current": current,
        "lineage": lineage,
        "history_depth": len(lineage),
        "refetch": {"method": "GET", "path": refetch_path},
    }


def _require_policy(policy: str) -> None:
    if policy not in policies.POLICIES:
        raise ValidationError(
            f"unknown stale policy {policy!r}; expected one of {', '.join(policies.POLICIES)}"
        )
