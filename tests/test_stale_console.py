"""Console behaviour around stale generations.

The HTTP layer must turn a guard verdict into a response an on-call operator
can act on without reading service source: both generations, the policy, the
availability ceiling, the re-fetch target and the audit/journal coordinates.
When the policy adopts, the notice and the served object must describe the same
generation -- the prompt can never advertise a version different from reality.
"""

from __future__ import annotations

from tests.support import make_server
from tests.test_console_api import bootstrap, call


def _move_once(server, flight_id, chute_id="T1-CHB"):
    # bootstrap already assigned generation 1, so one more reassign lands on 2.
    call(server, "POST", "/api/chutes/assign", {"flight_id": flight_id, "chute_id": chute_id})


def test_stale_confirm_explains_the_expiry_in_full(server):
    flight_id = bootstrap(server)
    _move_once(server, flight_id)
    status, body = call(
        server, "POST", f"/api/assignments/{flight_id}/confirm", {"generation": 1}
    )
    assert status == 409
    assert body["code"] == "stale_reference"
    assert body["details"]["held_generation"] == 1
    assert body["details"]["current_generation"] == 2
    assert body["details"]["generation_of"] == "chute_assignment"
    assert body["details"]["refetch"]["use_generation"] == 2
    assert body["details"]["refetch"]["history_endpoint"] == (
        f"/api/assignments/{flight_id}/history"
    )
    assert "0%" in body["details"]["availability_ceiling"]
    evidence = body["details"]["evidence"]
    assert evidence["held"]["generation"] == 1
    assert evidence["current"]["journal_kind"] == "chute.assign"
    assert evidence["audit_endpoint"] == "/api/audit"


def test_stale_rejection_returns_the_current_generation_for_refetch(server):
    flight_id = bootstrap(server)
    _move_once(server, flight_id)
    _, body = call(
        server, "POST", f"/api/assignments/{flight_id}/confirm", {"generation": 1}
    )
    current = call(server, "GET", f"/api/assignments/{flight_id}")[1]
    assert body["details"]["current_generation"] == current["generation"]


def test_reject_policy_does_not_serve_any_stale_traffic(server):
    flight_id = bootstrap(server)
    _move_once(server, flight_id)
    assert (
        call(
            server, "POST", f"/api/assignments/{flight_id}/confirm", {"generation": 1}
        )[0]
        == 409
    )


def test_adopt_policy_continues_and_the_notice_matches_the_served_version(server):
    flight_id = bootstrap(server)
    _move_once(server, flight_id)
    policy = call(server, "POST", "/api/assignments/policy", {"mode": "adopt"})[1]
    assert policy["mode"] == "adopt"
    assert "bounded" in policy["availability_ceiling"]

    status, body = call(
        server, "POST", f"/api/assignments/{flight_id}/confirm", {"generation": 1}
    )
    assert status == 200
    assert body["adopted"] is True
    assert body["generation"] == 2
    assert body["held_generation"] == 1
    assert body["current_generation"] == body["generation"]
    # The notice must state the rewrite and agree with the real current version.
    assert "current generation 2" in body["notice"]


def test_served_version_always_equals_live_state_even_after_another_bump(server):
    flight_id = bootstrap(server)
    _move_once(server, flight_id)
    call(server, "POST", "/api/assignments/policy", {"mode": "adopt"})
    call(server, "POST", "/api/chutes/assign", {"flight_id": flight_id, "chute_id": "T1-CHC"})
    _, body = call(
        server, "POST", f"/api/assignments/{flight_id}/confirm", {"generation": 1}
    )
    live = call(server, "GET", f"/api/assignments/{flight_id}")[1]
    assert body["generation"] == live["generation"] == 3
    assert body["current_generation"] == 3
    assert "current generation 3" in body["notice"]


def test_adopted_still_rejects_when_no_current_assignment_exists(server):
    # Register a fresh flight with no assignment at all (fixture, not bootstrap).
    call(server, "POST", "/api/flights", {"code": "NOCHUTE", "carousel": "CR1"})
    flight_id = "T1-NOCHUTE"
    call(server, "POST", "/api/assignments/policy", {"mode": "adopt"})
    status, body = call(
        server, "POST", f"/api/assignments/{flight_id}/confirm", {"generation": 42}
    )
    assert status == 409
    assert body["details"]["current_generation"] == 0
    assert body["details"]["decision"] == "rejected"


def test_assignment_history_keeps_every_generation_with_coordinates(server):
    flight_id = bootstrap(server)
    _move_once(server, flight_id)
    history = call(server, "GET", f"/api/assignments/{flight_id}/history")[1]
    assert [row["generation"] for row in history] == [1, 2]
    assert history[0]["chute_id"] == "T1-CHA"
    assert history[1]["chute_id"] == "T1-CHB"
    assert all(row["journal_kind"] == "chute.assign" for row in history)
    assert history[1]["journal_sequence"] > history[0]["journal_sequence"]


def test_schedule_history_keeps_every_revision(server):
    flight_id = bootstrap(server)
    call(server, "POST", f"/api/flights/{flight_id}/schedule", {"cutoff": "11:00"})
    call(server, "POST", f"/api/flights/{flight_id}/schedule", {"cutoff": "12:00"})
    history = call(server, "GET", f"/api/flights/{flight_id}/schedule/history")[1]
    assert [row["generation"] for row in history] == [1, 2, 3]
    assert [row["cutoff_at"] for row in history][1:] == ["11:00", "12:00"]
    assert history[0]["journal_kind"] == "flight.put"
    assert history[1]["journal_kind"] == "flight.schedule"


def test_generations_endpoint_cross_checks_both_streams(server):
    flight_id = bootstrap(server)
    call(server, "POST", f"/api/flights/{flight_id}/schedule", {"cutoff": "11:00"})
    call(server, "POST", "/api/chutes/assign", {"flight_id": flight_id, "chute_id": "T1-CHA"})
    body = call(server, "GET", f"/api/flights/{flight_id}/generations")[1]
    assert body["flight_schedule"]["generation"] == 2
    assert body["chute_assignment"]["generation"] == 2
    assert body["chute_assignment"]["schedule_generation"] == 2
    assert body["chute_generations"] == [1, 2]
    assert body["schedule_generations"] == [1, 2]


def test_recheck_deadline_rejects_then_adopts_stale_schedule(server):
    flight_id = bootstrap(server)
    call(server, "POST", f"/api/flights/{flight_id}/schedule", {"cutoff": "11:00"})
    status, body = call(
        server, "GET", f"/api/recheck/deadline/{flight_id}", query={"generation": ["1"]}
    )
    assert status == 409
    assert body["details"]["generation_of"] == "flight_schedule"
    assert body["details"]["current_generation"] == 2

    call(server, "POST", "/api/assignments/policy", {"mode": "adopt"})
    status, body = call(
        server, "GET", f"/api/recheck/deadline/{flight_id}", query={"generation": ["1"]}
    )
    assert status == 200
    assert body["adopted"] is True
    assert body["schedule"]["generation"] == 2
    assert body["current_generation"] == 2
    assert "current generation 2" in body["notice"]


def test_every_stale_decision_is_audited_including_adoptions(server):
    flight_id = bootstrap(server)
    _move_once(server, flight_id)
    call(server, "POST", f"/api/assignments/{flight_id}/confirm", {"generation": 1})
    call(server, "POST", "/api/assignments/policy", {"mode": "adopt"})
    call(server, "POST", f"/api/assignments/{flight_id}/confirm", {"generation": 1})
    audit = call(server, "GET", "/api/audit")[1]
    stale = [entry for entry in audit if entry["kind"] == "stale_reference"]
    assert [entry["detail"]["outcome"] for entry in stale] == [
        "rejected_stale",
        "adopted",
    ]
    assert all(entry["subject"] == flight_id for entry in stale)
    assert call(server, "GET", "/api/audit/verify")[1]["intact"] is True


def test_policy_endpoint_rejects_an_unknown_mode(server):
    status, body = call(server, "POST", "/api/assignments/policy", {"mode": "mystery"})
    assert status == 400
    assert body["code"] == "validation_error"
    # The invalid mode is a programmer/operator input error, not a silent fall back.
    assert "unknown stale policy" in body["error"]


def test_schedule_revision_is_its_own_journal_command(server):
    flight_id = bootstrap(server)
    call(server, "POST", f"/api/flights/{flight_id}/schedule", {"cutoff": "11:00"})
    kinds = call(server, "GET", "/api/store/watermark")[1]["kinds"]
    assert "flight.schedule" in kinds
