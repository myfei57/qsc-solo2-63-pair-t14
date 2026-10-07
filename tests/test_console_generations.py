"""Console answers for an expired generation reference."""

from __future__ import annotations

import json

from tests.support import register_flight


def _call(server, method, path, payload=None, query=None):
    response = server.call(method, path, payload, query)
    return response.status, json.loads(response.body)


def _flight_with_two_generations(server):
    flight = register_flight(server.plant, code="CA1", carousel="CR1")
    server.plant.assign_chute(flight.id, "T1-CHA")
    server.plant.assign_chute(flight.id, "T1-CHB")
    return flight


def test_stale_confirm_returns_structured_409(server):
    flight = _flight_with_two_generations(server)
    status, body = _call(
        server, "POST", f"/api/assignments/{flight.id}/confirm", {"generation": 1}
    )
    assert status == 409
    assert body["code"] == "stale_reference"
    details = body["details"]
    assert details["presented_generation"] == 1
    assert details["current_generation"] == 2
    assert details["held"]["chute_id"] == "T1-CHA"
    assert details["current"]["chute_id"] == "T1-CHB"
    assert details["refetch"]["path"] == f"/api/assignments/{flight.id}"
    assert details["availability_ceiling"]["policy"] == "adopt_current"
    assert details["availability_ceiling"]["cannot_salvage"]


def test_fresh_confirm_reports_the_matching_generation(server):
    flight = _flight_with_two_generations(server)
    status, body = _call(
        server, "POST", f"/api/assignments/{flight.id}/confirm", {"generation": 2}
    )
    assert status == 200
    assert body["confirmed"] is True
    assert body["adopted"] is False
    assert body["presented_generation"] == 2
    assert body["current_generation"] == 2


def test_adopt_policy_continues_and_notices_the_rewrite(server):
    flight = _flight_with_two_generations(server)
    status, body = _call(
        server,
        "POST",
        f"/api/assignments/{flight.id}/confirm",
        {"generation": 1, "policy": "adopt_current"},
    )
    assert status == 200
    assert body["adopted"] is True
    assert body["presented_generation"] == 1
    assert body["current_generation"] == 2
    assert "generation 2" in body["adopted_notice"]
    # the notice version is the true current generation
    _, current = _call(server, "GET", f"/api/assignments/{flight.id}")
    assert body["current_generation"] == current["generation"]
    assert body["current"]["chute_id"] == current["chute_id"]


def test_unknown_policy_is_a_400(server):
    flight = _flight_with_two_generations(server)
    status, body = _call(
        server,
        "POST",
        f"/api/assignments/{flight.id}/confirm",
        {"generation": 1, "policy": "guess"},
    )
    assert status == 400
    assert body["code"] == "validation_error"


def test_generations_endpoint_corroborates_both_kinds(server):
    flight = _flight_with_two_generations(server)
    server.plant.schedule.revise(flight.id, "2024-03-01T00:00:00Z")
    status, view = _call(server, "GET", f"/api/assignments/{flight.id}/generations")
    assert status == 200
    assert view["live"] is True
    assert view["chute_assignment"]["current_generation"] == 2
    assert [item["generation"] for item in view["chute_assignment"]["lineage"]] == [1, 2]
    assert view["flight_schedule"]["current_generation"] == 2
    assert view["flight_schedule"]["current"]["cutoff_at"] == "2024-03-01T00:00:00Z"
    assert view["evidence"]["watermark"] >= 1
    assert "journal" in view["evidence"]["recompute"]


def test_stale_decision_is_visible_in_the_audit_timeline(server):
    flight = _flight_with_two_generations(server)
    _call(server, "POST", f"/api/assignments/{flight.id}/confirm", {"generation": 1})
    status, entries = _call(server, "GET", "/api/audit")
    assert status == 200
    expired = [entry for entry in entries if entry["kind"] == "generation.expired"]
    assert len(expired) == 1
    detail = expired[0]["detail"]
    assert detail["presented_generation"] == 1
    assert detail["current_generation"] == 2
    assert detail["decision"] == "rejected"
    assert detail["stage"] == "chute"


def test_schedule_deadline_rejects_then_adopts_with_matching_version(server):
    flight = _flight_with_two_generations(server)
    server.plant.schedule.revise(flight.id, "2024-03-01T00:00:00Z")
    status, body = _call(
        server,
        "GET",
        f"/api/recheck/deadline/{flight.id}",
        query={"generation": ["1"]},
    )
    assert status == 409
    assert body["details"]["reference_kind"] == "flight_schedule"
    assert body["details"]["current_generation"] == 2

    status, body = _call(
        server,
        "GET",
        f"/api/recheck/deadline/{flight.id}",
        query={"generation": ["1"], "policy": ["adopt_current"]},
    )
    assert status == 200
    assert body["schedule"]["generation"] == 2
    assert body["presented_generation"] == 2
    assert "generation 2" in body["adopted_notice"]


def test_history_endpoint_keeps_old_generations(server):
    flight = _flight_with_two_generations(server)
    status, history = _call(server, "GET", f"/api/assignments/{flight.id}/history")
    assert status == 200
    assert [item["generation"] for item in history] == [1, 2]
