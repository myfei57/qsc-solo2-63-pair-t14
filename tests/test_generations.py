"""Explaining, evidencing and auditing an expired generation reference."""

from __future__ import annotations

import pytest

from bagsort.app import Plant
from bagsort.errors import NotFoundError, StaleError, ValidationError
from bagsort.generations import Adoption
from bagsort.generations.policy import ADOPT_CURRENT, REJECT
from bagsort.store.store import Store


def _assign_twice(plant, flight):
    plant.chutes.assign(flight.id, "T1-CHA")
    plant.chutes.assign(flight.id, "T1-CHB")


def _stale(action):
    """Run an action expected to fail stale, returning the exception."""

    with pytest.raises(StaleError) as caught:
        action()
    return caught.value


# ---------------------------------------------------------------------------
# rejection explains exactly what expired
# ---------------------------------------------------------------------------


def test_stale_rejection_names_both_generations_and_the_refetch(plant, flight):
    _assign_twice(plant, flight)
    error = _stale(lambda: plant.generations.check_assignment(flight.id, 1))
    details = error.details
    assert details["stale"] is True
    assert details["reference_kind"] == "chute_assignment"
    assert details["presented_generation"] == 1
    assert details["current_generation"] == 2
    assert details["policy"] == REJECT
    assert details["refetch"] == {"method": "GET", "path": f"/api/assignments/{flight.id}"}
    message = str(error)
    assert "generation 1" in message and "generation 2" in message
    assert "T1-CHB" in message and "re-fetch" in message.lower()


def test_stale_details_carry_the_held_and_current_allocation(plant, flight):
    _assign_twice(plant, flight)
    details = _stale(lambda: plant.generations.check_assignment(flight.id, 1)).details
    assert details["held"]["chute_id"] == "T1-CHA"
    assert details["held"]["generation"] == 1
    assert details["held"]["journal_sequence"] >= 1
    assert details["current"]["chute_id"] == "T1-CHB"
    assert details["current"]["generation"] == 2


def test_an_unknown_generation_is_distinguished_from_an_advanced_one(plant, flight):
    _assign_twice(plant, flight)
    details = _stale(lambda: plant.generations.check_assignment(flight.id, 99)).details
    assert details["reason"] == "generation_unknown"
    assert details["held"] is None


def test_a_fresh_generation_is_returned_without_an_audit_entry(plant, flight):
    _assign_twice(plant, flight)
    current = plant.generations.check_assignment(flight.id, 2)
    assert current.assignment.chute_id == "T1-CHB"
    assert plant.audit.by_kind().get("generation.expired", 0) == 0


def test_unknown_policy_is_a_validation_error(plant, flight):
    _assign_twice(plant, flight)
    with pytest.raises(ValidationError):
        plant.generations.check_assignment(flight.id, 1, policy="guess")


# ---------------------------------------------------------------------------
# adopt_current: explicit, explained, bounded
# ---------------------------------------------------------------------------


def test_adopt_current_continues_on_the_current_generation(plant, flight):
    _assign_twice(plant, flight)
    verdict = plant.generations.check_assignment(flight.id, 1, policy=ADOPT_CURRENT)
    assert isinstance(verdict, Adoption)
    assert verdict.presented_generation == 1
    assert verdict.current_generation == 2
    payload = verdict.as_dict()
    assert payload["adopted"] is True
    assert payload["current"]["chute_id"] == "T1-CHB"
    assert payload["current"]["journal_sequence"] >= 1
    assert "generation 2" in verdict.adopted_notice


def test_adopt_cannot_revive_a_retired_flight(plant, flight):
    _assign_twice(plant, flight)
    plant.book.remove(flight.id)
    with pytest.raises(NotFoundError):
        plant.generations.check_assignment(flight.id, 2, policy=ADOPT_CURRENT)


def test_adopt_cannot_invent_a_missing_allocation(plant, flight):
    details = _stale(
        lambda: plant.generations.check_assignment(flight.id, 1, policy=ADOPT_CURRENT)
    ).details
    assert details["reason"] == "no_current_assignment"


# ---------------------------------------------------------------------------
# audit trail of every stale decision
# ---------------------------------------------------------------------------


def _expired_entries(plant):
    return [entry for entry in plant.audit.entries() if entry.kind == "generation.expired"]


def test_every_stale_decision_is_audited_and_chain_verifies(plant, flight):
    _assign_twice(plant, flight)
    _stale(lambda: plant.generations.check_assignment(flight.id, 1))
    plant.generations.check_assignment(flight.id, 1, ADOPT_CURRENT)

    entries = _expired_entries(plant)
    assert len(entries) == 2
    rejected, adopted = entries
    assert rejected.detail["decision"] == "rejected"
    assert rejected.detail["policy"] == REJECT
    assert rejected.detail["presented_generation"] == 1
    assert rejected.detail["current_generation"] == 2
    assert rejected.detail["reason"] == "generation_advanced"
    assert rejected.detail["watermark"] >= 1
    assert adopted.detail["decision"] == "adopted"
    assert adopted.detail["policy"] == ADOPT_CURRENT
    assert plant.audit.verify() is True


def test_schedule_staleness_is_audited_under_its_own_kind(plant, flight):
    plant.schedule.revise(flight.id, "2024-03-01T00:00:00Z")
    _stale(lambda: plant.generations.check_schedule(flight.id, 1))
    entry = _expired_entries(plant)[-1]
    assert entry.detail["reference_kind"] == "flight_schedule"
    assert entry.detail["current_generation"] == 2
    assert entry.detail["current_cutoff"] == "2024-03-01T00:00:00Z"


def test_unknown_schedule_generation_is_distinguished_from_advanced(plant, flight):
    details = _stale(lambda: plant.generations.check_schedule(flight.id, 42)).details
    assert details["reason"] == "generation_unknown"
    assert details["held"] is None
    assert details["current_generation"] == 1


def test_schedule_adoption_leaves_an_audit_decision(plant, flight):
    plant.schedule.revise(flight.id, "2024-03-01T00:00:00Z")
    verdict = plant.generations.check_schedule(flight.id, 1, ADOPT_CURRENT)
    assert isinstance(verdict, Adoption)
    entry = _expired_entries(plant)[-1]
    assert entry.detail["decision"] == "adopted"
    assert entry.detail["reference_kind"] == "flight_schedule"


# ---------------------------------------------------------------------------
# corroboration and historical record keeping
# ---------------------------------------------------------------------------


def test_report_corroborates_assignment_and_schedule_generations(plant, flight):
    _assign_twice(plant, flight)
    plant.schedule.revise(flight.id, "2024-03-01T00:00:00Z")
    report = plant.generations.report(flight.id)
    assert report["live"] is True
    assert report["chute_assignment"]["current_generation"] == 2
    assert report["flight_schedule"]["current_generation"] == 2
    chute_lineage = report["chute_assignment"]["lineage"]
    assert [item["generation"] for item in chute_lineage] == [1, 2]
    assert chute_lineage[0]["journal_sequence"] < report["evidence"]["watermark"]
    assert report["policy"]["default"] == REJECT
    assert report["policy"]["adopt_current"]["cannot_salvage"]


def test_report_handles_a_live_flight_without_an_allocation(plant, flight):
    report = plant.generations.report(flight.id)
    assert report["live"] is True
    assert report["chute_assignment"]["current_generation"] is None
    assert report["chute_assignment"]["history_depth"] == 0
    assert report["flight_schedule"]["current_generation"] == 1


def test_historical_generations_remain_queryable_after_a_new_one(plant, flight):
    _assign_twice(plant, flight)
    lineage = plant.chute_history.for_flight(flight.id)
    assert [item.generation for item in lineage] == [1, 2]
    assert [item.chute_id for item in lineage] == ["T1-CHA", "T1-CHB"]


def test_report_stays_readable_after_the_flight_is_retired(plant, flight):
    _assign_twice(plant, flight)
    plant.book.remove(flight.id)
    report = plant.generations.report(flight.id)
    assert report["live"] is False
    assert report["chute_assignment"]["history_depth"] == 2
    assert [item["generation"] for item in report["chute_assignment"]["lineage"]] == [1, 2]


def test_history_and_decisions_survive_a_journal_replay(plant, flight):
    _assign_twice(plant, flight)
    _stale(lambda: plant.generations.check_assignment(flight.id, 1))
    plant.store.persist()

    reloaded = Plant.build(Store.open(plant.store.path), clock=plant.clock)
    lineage = reloaded.chute_history.for_flight(flight.id)
    assert [item.generation for item in lineage] == [1, 2]
    assert len(_expired_entries(reloaded)) == 1
    assert reloaded.audit.verify() is True
