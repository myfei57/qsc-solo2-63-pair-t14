"""Stale generation guard: explanation, audit trail and reject/adopt policy.

The guard is the single decision point shared by chute assignments and flight
schedules, so these tests pin down:

* a stale reference says which generation was held, which is current and what
  to re-fetch, with journal coordinates the verdict can be recomputed from;
* every stale decision -- including an adopted one -- lands in the hash linked
  audit chain;
* reject (the default) never serves stale traffic, while adopt's availability
  is capped by "a current version still exists".
"""

from __future__ import annotations

import pytest

from bagsort.errors import StaleGenerationError, ValidationError
from bagsort.generations import ADOPT, REJECT
from tests.support import register_flight


def _revised_flight(plant, cutoff: str = "2024-01-02T00:00:00Z"):
    flight = register_flight(plant)
    plant.chutes.assign(flight.id, "T1-CHA")
    plant.chutes.assign(flight.id, "T1-CHB")
    plant.revise_schedule(flight.id, cutoff)
    return flight


def test_reject_is_the_default_policy(plant):
    assert plant.generations.mode == REJECT
    policy = plant.generation_policy()
    assert policy["mode"] == REJECT
    assert policy["available_modes"] == [REJECT, ADOPT]
    assert "0%" in policy["availability_ceiling"]


def test_stale_assignment_verdict_names_both_generations_and_refetch(plant):
    flight = _revised_flight(plant)
    with pytest.raises(StaleGenerationError) as raised:
        plant.chutes.ensure_current(flight.id, 1)
    details = raised.value.details
    assert details["generation_of"] == "chute_assignment"
    assert details["held_generation"] == 1
    assert details["current_generation"] == 2
    assert details["policy"] == REJECT
    assert details["decision"] == "rejected"
    assert details["adopted"] is False
    assert details["refetch"]["use_generation"] == 2
    assert details["refetch"]["endpoint"] == f"/api/assignments/{flight.id}"
    assert "retry" in details["notice"]


def test_verdict_carries_recomputable_journal_evidence(plant):
    flight = _revised_flight(plant)
    with pytest.raises(StaleGenerationError) as raised:
        plant.chutes.ensure_current(flight.id, 1)
    evidence = raised.value.details["evidence"]
    held = evidence["held"]
    current = evidence["current"]
    assert held["present"] is True
    assert held["generation"] == 1
    assert held["chute_id"] == "T1-CHA"
    assert current["generation"] == 2
    assert current["chute_id"] == "T1-CHB"
    # The journal sequences must match what a replay over /api/store/journal sees.
    kinds = {
        entry.sequence: entry.kind
        for entry in plant.store.entries()
        if entry.kind == "chute.assign"
    }
    assert kinds[held["journal_sequence"]] == held["journal_kind"] == "chute.assign"
    assert current["journal_sequence"] > held["journal_sequence"]
    assert evidence["basis"]


def test_evidence_agrees_with_an_independent_journal_replay(plant):
    flight = _revised_flight(plant)
    with pytest.raises(StaleGenerationError) as raised:
        plant.chutes.ensure_current(flight.id, 1)
    evidence = raised.value.details["evidence"]
    from bagsort.chute.history import AssignmentHistory

    history = AssignmentHistory(plant.store).records(flight.id)
    by_generation = {row.assignment.generation: row for row in history}
    assert evidence["held"]["journal_sequence"] == by_generation[1].journal_sequence
    assert evidence["current"]["journal_sequence"] == by_generation[2].journal_sequence


def test_stale_decision_is_written_to_the_audit_chain(plant):
    flight = _revised_flight(plant)
    with pytest.raises(StaleGenerationError):
        plant.chutes.ensure_current(flight.id, 1)
    stale = [entry for entry in plant.audit.entries() if entry.kind == "stale_reference"]
    assert len(stale) == 1
    entry = stale[0]
    assert entry.subject == flight.id
    assert entry.detail["generation_of"] == "chute_assignment"
    assert entry.detail["held_generation"] == 1
    assert entry.detail["current_generation"] == 2
    assert entry.detail["outcome"] == "rejected_stale"
    assert plant.audit.verify() is True


def test_adopt_policy_serves_the_current_generation_and_audits_rewrite(plant):
    flight = _revised_flight(plant)
    plant.set_generation_policy(ADOPT)
    assignment, verdict = plant.chutes.ensure_current(flight.id, 1)
    assert assignment.generation == 2
    assert assignment.chute_id == "T1-CHB"
    assert verdict is not None and verdict.adopted is True
    assert verdict.decision == "adopted"
    assert verdict.current_generation == 2
    assert "current generation 2" in verdict.notice
    entry = [e for e in plant.audit.entries() if e.kind == "stale_reference"][0]
    assert entry.detail["outcome"] == "adopted"
    assert entry.detail["policy"] == ADOPT
    assert plant.audit.verify() is True


def test_adopt_cannot_resurrect_a_missing_assignment(plant):
    flight = register_flight(plant)
    plant.set_generation_policy(ADOPT)
    with pytest.raises(StaleGenerationError) as raised:
        plant.chutes.ensure_current(flight.id, 9)
    assert raised.value.details["current_generation"] == 0
    assert raised.value.details["decision"] == "rejected"
    assert "rejected_missing" == _last_outcome(plant)


def test_schedule_stream_shares_the_guard_and_its_evidence(plant):
    flight = register_flight(plant)
    plant.revise_schedule(flight.id, "2024-01-02T00:00:00Z")
    plant.revise_schedule(flight.id, "2024-01-03T00:00:00Z")
    with pytest.raises(StaleGenerationError) as raised:
        plant.schedule.ensure_current(flight.id, 2)
    details = raised.value.details
    assert details["generation_of"] == "flight_schedule"
    assert details["held_generation"] == 2
    assert details["current_generation"] == 3
    held = details["evidence"]["held"]
    assert held["journal_kind"] == "flight.schedule"
    assert held["cutoff_at"] == "2024-01-02T00:00:00Z"
    assert details["refetch"]["endpoint"] == f"/api/flights/{flight.id}/schedule"


def test_assignment_stamps_the_schedule_generation_it_was_planned_against(plant):
    flight = register_flight(plant)
    first = plant.assign_chute(flight.id, "T1-CHA")
    assert first.schedule_generation == 1
    plant.revise_schedule(flight.id, "2024-01-02T00:00:00Z")
    moved = plant.assign_chute(flight.id, "T1-CHB")
    assert moved.schedule_generation == 2
    # The old assignment still proves it was planned against schedule gen 1.
    rows = plant.chute_history.records(flight.id)
    assert rows[0].assignment.schedule_generation == 1
    assert rows[1].assignment.schedule_generation == 2


def test_switching_policy_is_validated(plant):
    with pytest.raises(ValidationError):
        plant.set_generation_policy("nope")
    # The guard itself speaks ValueError; Plant translates it for the console.
    with pytest.raises(ValueError):
        plant.generations.set_mode("nope")


def test_schedule_generations_survive_a_journal_replay(plant):
    from bagsort.store.replay import replay

    flight = register_flight(plant)
    plant.revise_schedule(flight.id, "2024-01-02T00:00:00Z")
    plant.revise_schedule(flight.id, "2024-01-03T00:00:00Z")

    rebuilt = replay(plant.store.entries())
    live = rebuilt.flights[flight.id]
    assert live.schedule_generation == 3
    assert live.cutoff_at == "2024-01-03T00:00:00Z"
    assert rebuilt.as_dict() == plant.store.state().as_dict()


def test_schedule_generations_survive_persist_and_restart(tmp_path):
    from bagsort.store.store import Store
    from bagsort.app import Plant
    from bagsort.flight.history import ScheduleHistory

    store = Store(str(tmp_path / "state.json"))
    plant = Plant.build(store)
    flight = register_flight(plant)
    plant.revise_schedule(flight.id, "2024-01-02T00:00:00Z")
    plant.set_generation_policy(ADOPT)
    plant.schedule.ensure_current(flight.id, 1)
    store.persist()

    reopened = Store.open(str(tmp_path / "state.json"))
    history = ScheduleHistory(reopened)
    assert [revision.generation for revision in history.records(flight.id)] == [1, 2]
    reopened_plant = Plant.build(reopened)
    # The stale decision audited before restart is still linked and intact.
    stale = [e for e in reopened_plant.audit.entries() if e.kind == "stale_reference"]
    assert stale and stale[0].detail["decision"] == "adopted"
    assert reopened_plant.audit.verify() is True
    # Policy resets to the safe default on restart; it is an operating choice.
    assert reopened_plant.generations.mode == REJECT


def _last_outcome(plant) -> str:
    return [
        entry
        for entry in plant.audit.entries()
        if entry.kind == "stale_reference"
    ][-1].detail["outcome"]
