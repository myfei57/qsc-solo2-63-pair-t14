"""One place that decides whether a held generation is still usable.

Both versioned streams -- chute assignments and flight schedules -- resolve
stale references through :class:`GenerationGuard`, so the explanation returned
to the caller, the audit trail left behind and the safety/availability policy
applied can never drift apart.

The decision is audited for *every* stale reference, including the ones that
are adopted: an automatic rewrite changes the request's semantics, so the fact
that it happened must survive an operator rotating the logs.
"""

from __future__ import annotations

from typing import Any

from bagsort.audit.chain import AuditChain
from bagsort.chute.history import AssignmentHistory
from bagsort.errors import StaleGenerationError
from bagsort.flight.history import ScheduleHistory
from bagsort.generations.policy import (
    ADOPT,
    AVAILABILITY,
    REJECT,
    StaleVerdict,
    require_policy,
)
from bagsort.store.store import Store

CHUTE_ASSIGNMENT = "chute_assignment"
FLIGHT_SCHEDULE = "flight_schedule"

_CHUTE_REFETCH = "/api/assignments/{flight}"
_SCHEDULE_REFETCH = "/api/flights/{id}/schedule"


class GenerationGuard:
    """Resolves held generations against current state under the active policy."""

    def __init__(
        self,
        store: Store,
        audit: AuditChain,
        assignments: AssignmentHistory,
        schedules: ScheduleHistory,
        mode: str = REJECT,
    ) -> None:
        self._store = store
        self._audit = audit
        self._assignments = assignments
        self._schedules = schedules
        self._mode = require_policy(mode)

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> str:
        self._mode = require_policy(mode)
        return self._mode

    def describe_policy(self) -> dict[str, Any]:
        return {
            "mode": self._mode,
            "available_modes": [REJECT, ADOPT],
            "availability_ceiling": AVAILABILITY[self._mode],
        }

    # -- entry points used by the two versioned subsystems -----------------

    def resolve_assignment(self, flight_id: str, generation: int, *, audit_stage: str):
        """Return the current assignment, rejecting or adopting per policy."""

        assignment = self._store.state().assignments.get(flight_id)
        if assignment is None or assignment.generation != generation:
            current_generation = assignment.generation if assignment else 0
            verdict = self._verdict(
                kind=CHUTE_ASSIGNMENT,
                flight_id=flight_id,
                held=generation,
                current=current_generation,
            )
            if assignment is None:
                # Adoption cannot conjure a plan that does not exist; this is a
                # hard rejection regardless of the configured policy.
                self._record(verdict, audit_stage, "rejected_missing")
                raise StaleGenerationError(verdict)
            if self._mode == ADOPT:
                self._record(verdict, audit_stage, "adopted")
                return assignment, verdict
            self._record(verdict, audit_stage, "rejected_stale")
            raise StaleGenerationError(verdict)
        return assignment, None

    def resolve_schedule(self, flight_id: str, generation: int, *, audit_stage: str):
        """Return the current schedule, rejecting or adopting per policy."""

        flight = self._store.state().flights.get(flight_id)
        if flight is None or flight.schedule_generation != generation:
            current_generation = flight.schedule_generation if flight else 0
            verdict = self._verdict(
                kind=FLIGHT_SCHEDULE,
                flight_id=flight_id,
                held=generation,
                current=current_generation,
            )
            if flight is None:
                self._record(verdict, audit_stage, "rejected_missing")
                raise StaleGenerationError(verdict)
            if self._mode == ADOPT:
                self._record(verdict, audit_stage, "adopted")
                return flight, verdict
            self._record(verdict, audit_stage, "rejected_stale")
            raise StaleGenerationError(verdict)
        return flight, None

    # -- internals ---------------------------------------------------------

    def _verdict(
        self,
        *,
        kind: str,
        flight_id: str,
        held: int,
        current: int,
    ) -> StaleVerdict:
        held_evidence, current_evidence = self._evidence(kind, flight_id, held, current)
        adopting = self._mode == ADOPT and current > 0
        if kind == CHUTE_ASSIGNMENT:
            refetch = {
                "endpoint": _CHUTE_REFETCH.format(flight=flight_id),
                "history_endpoint": f"/api/assignments/{flight_id}/history",
                "use_generation": current,
            }
            target = "chute assignment"
        else:
            refetch = {
                "endpoint": _SCHEDULE_REFETCH.format(id=flight_id),
                "history_endpoint": f"/api/flights/{flight_id}/schedule/history",
                "use_generation": current,
            }
            target = "flight schedule"
        if current == 0:
            reason = (
                f"{target} generation {held} for {flight_id!r} no longer exists; "
                f"the flight has no current {target.replace('flight ', '')}"
            )
        elif adopting:
            reason = (
                f"{target} generation {held} for {flight_id!r} is stale; current is {current}; "
                f"continuing against the current generation under the {ADOPT!r} policy"
            )
        else:
            reason = (
                f"{target} generation {held} for {flight_id!r} is stale; current is {current}"
            )
        notice = (
            f"request submitted against {target} generation {held}; it was executed "
            f"against the current generation {current} without changing the request"
            if adopting
            else "request rejected unchanged; re-fetch the generation named in refetch and retry"
        )
        return StaleVerdict(
            flight_id=flight_id,
            generation_of=kind,
            held_generation=held,
            current_generation=current,
            policy=self._mode,
            decision="adopted" if adopting else "rejected",
            reason=reason,
            notice=notice,
            availability=AVAILABILITY[self._mode],
            refetch=refetch,
            evidence={
                "held": held_evidence,
                "current": current_evidence,
                "journal": "/api/store/journal",
                "audit_endpoint": "/api/audit",
                "basis": (
                    "replay the journal commands named below and fold them in commit "
                    "order; the held and current generations must equal what this "
                    "verdict reports"
                ),
            },
            adopted=adopting,
        )

    def _evidence(self, kind: str, flight_id: str, held: int, current: int):
        if kind == CHUTE_ASSIGNMENT:
            held_row = self._assignments.locate(flight_id, held)
            current_row = self._assignments.locate(flight_id, current)
            held_evidence = self._assignment_evidence(held_row)
            current_evidence = self._assignment_evidence(current_row)
        else:
            held_row = self._schedules.locate(flight_id, held)
            current_row = self._schedules.locate(flight_id, current)
            held_evidence = self._schedule_evidence(held_row)
            current_evidence = self._schedule_evidence(current_row)
        return held_evidence, current_evidence

    @staticmethod
    def _assignment_evidence(row) -> dict[str, Any]:
        if row is None:
            return {"present": False}
        assignment = row.assignment
        return {
            "present": True,
            "journal_kind": row.journal_kind,
            "journal_sequence": row.journal_sequence,
            "generation": assignment.generation,
            "chute_id": assignment.chute_id,
            "schedule_generation": assignment.schedule_generation,
            "updated_at": assignment.updated_at,
        }

    @staticmethod
    def _schedule_evidence(row) -> dict[str, Any]:
        if row is None:
            return {"present": False}
        return {
            "present": True,
            "journal_kind": row.journal_kind,
            "journal_sequence": row.journal_sequence,
            "generation": row.generation,
            "cutoff_at": row.cutoff_at,
            "revised_at": row.revised_at,
        }

    def _record(self, verdict: StaleVerdict, stage: str, outcome: str) -> None:
        self._audit.record(
            "stale_reference",
            verdict.flight_id,
            stage,
            {
                "generation_of": verdict.generation_of,
                "held_generation": verdict.held_generation,
                "current_generation": verdict.current_generation,
                "policy": self._mode,
                "decision": verdict.decision,
                "outcome": outcome,
            },
        )
