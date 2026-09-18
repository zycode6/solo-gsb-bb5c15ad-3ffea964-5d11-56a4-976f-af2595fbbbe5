"""Tests for GET /events/changes: effective-event diffs by chain root."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "events-changes-test.db"))
    db_module.init_db()
    return TestClient(app)


def _post(client, event_id, participant_id, points, correction_of=None):
    payload = {
        "event_id": event_id,
        "participant_id": participant_id,
        "points": points,
    }
    if correction_of is not None:
        payload["correction_of"] = correction_of
    r = client.post("/events", json=payload)
    assert r.status_code == 201
    return r.json()


def _changes(client, **params):
    r = client.get("/events/changes", params=params)
    assert r.status_code == 200, params
    return r.json()


def test_empty_to_first_revision_is_single_added_with_full_event(client):
    ev = _post(client, "e1", "alice", 10)
    body = _changes(client, from_revision=0, to_revision=1)
    assert body["from_revision"] == 0
    assert body["to_revision"] == 1
    assert body["changes"] == [
        {
            "change_type": "added",
            "chain_root_id": "e1",
            "before": None,
            "after": ev,
        }
    ]
    assert set(body["changes"][0]["after"]) == {
        "event_id",
        "participant_id",
        "points",
        "correction_of",
        "revision",
        "created_at",
    }


def test_single_correction_in_window_merges_into_replaced(client):
    e1 = _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)  # unrelated event inside the "before" snapshot
    e3 = _post(client, "e3", "carol", 12, correction_of="e1")

    body = _changes(client, from_revision=2, to_revision=3)
    assert body["changes"] == [
        {
            "change_type": "replaced",
            "chain_root_id": "e1",
            "before": e1,
            "after": e3,
        }
    ]


def test_multi_hop_chain_in_one_window_replaces_tip_with_latest_tip(client):
    e1 = _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    e3 = _post(client, "e3", "alice", 15, correction_of="e2")

    # Boundaries cut the chain: tip e1 at rev 1, tip e3 at rev 3.
    body = _changes(client, from_revision=1, to_revision=3)
    assert len(body["changes"]) == 1
    change = body["changes"][0]
    assert change["change_type"] == "replaced"
    assert change["chain_root_id"] == "e1"
    assert change["before"] == e1
    assert change["after"] == e3

    # From the empty snapshot there is nothing to cancel: latest tip is added.
    body = _changes(client, from_revision=0, to_revision=3)
    assert len(body["changes"]) == 1
    assert body["changes"][0]["change_type"] == "added"
    assert body["changes"][0]["after"]["event_id"] == "e3"


def test_events_effective_in_both_snapshots_cancel(client):
    e1 = _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    _post(client, "e3", "carol", 7)
    # Window 2 -> 3 only introduces e3; e2 stays effective and is cancelled.
    body = _changes(client, from_revision=2, to_revision=3)
    assert [c["chain_root_id"] for c in body["changes"]] == ["e3"]
    assert body["changes"][0]["change_type"] == "added"
    assert body["changes"][0]["before"] is None
    assert body["changes"][0]["after"]["event_id"] == "e3"


def test_tip_unchanged_outside_window_produces_no_change_for_chain(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "bob", 5)
    # At both revisions 2 and 3 the chain's effective tip is e2.
    body = _changes(client, from_revision=2, to_revision=3)
    assert [c["chain_root_id"] for c in body["changes"]] == ["e3"]


def test_one_in_one_out_under_different_roots_are_not_merged(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    _post(client, "e3", "carol", 12, correction_of="e1")
    # from=1 -> to=3: root e1 swaps e1 -> e3 (replaced); independent root e2
    # is a plain addition. Separate roots must never be merged.
    body = _changes(client, from_revision=1, to_revision=3)
    by_root = {c["chain_root_id"]: c for c in body["changes"]}
    assert set(by_root) == {"e1", "e2"}
    assert by_root["e1"]["change_type"] == "replaced"  # e3 traces to root e1
    assert by_root["e1"]["before"]["event_id"] == "e1"
    assert by_root["e1"]["after"]["event_id"] == "e3"
    assert by_root["e2"]["change_type"] == "added"
    assert by_root["e2"]["before"] is None
    assert by_root["e2"]["after"]["event_id"] == "e2"


def test_forked_corrections_in_one_window_are_removed_plus_two_added(client):
    e1 = _post(client, "e1", "alice", 10)
    e2 = _post(client, "e2", "alice", 11, correction_of="e1")
    e3 = _post(client, "e3", "alice", 12, correction_of="e1")

    # Both e2 and e3 correct e1; at rev 3 both are effective (two tips),
    # so the one-out/two-in remainder cannot merge.
    body = _changes(client, from_revision=1, to_revision=3)
    root_changes = [c for c in body["changes"] if c["chain_root_id"] == "e1"]
    assert len(root_changes) == 3
    kinds = {c["change_type"]: c for c in root_changes}
    assert set(kinds) == {"added", "removed"}
    assert kinds["removed"]["before"]["event_id"] == "e1"
    assert kinds["removed"]["after"] is None
    added_ids = sorted(
        c["after"]["event_id"] for c in root_changes if c["change_type"] == "added"
    )
    assert added_ids == ["e2", "e3"]
    assert all(c["before"] is None for c in root_changes if c["change_type"] == "added")
    # Sanity: all events carry full payloads and the same chain root.
    assert {c["chain_root_id"] for c in root_changes} == {"e1"}
    assert e1["revision"] == 1 and e2["revision"] == 2 and e3["revision"] == 3


def test_already_effective_fork_tip_cancels_when_second_branch_appears(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 11, correction_of="e1")
    _post(client, "e3", "alice", 12, correction_of="e1")
    # At rev 2 the effective tip is e2; at rev 3 both e2 and e3 are
    # effective: e2 cancels, leaving only e3 added.
    body = _changes(client, from_revision=2, to_revision=3)
    assert len(body["changes"]) == 1
    change = body["changes"][0]
    assert change["change_type"] == "added"
    assert change["chain_root_id"] == "e1"
    assert change["after"]["event_id"] == "e3"


def test_sorted_by_chain_root_then_after_or_before_revision_and_event_id(client):
    _post(client, "z1", "zoe", 10)
    _post(client, "a1", "amy", 5)
    _post(client, "z2", "zoe", 12, correction_of="z1")
    # from 0 -> 3: added a1 (root a1), replaced z1->z2 (root z1)
    body = _changes(client, from_revision=0, to_revision=3)
    keys = [
        (
            c["chain_root_id"],
            (c["after"] or c["before"])["revision"],
            (c["after"] or c["before"])["event_id"],
        )
        for c in body["changes"]
    ]
    assert keys == sorted(keys)
    assert [c["chain_root_id"] for c in body["changes"]] == ["a1", "z1"]


def test_full_events_match_the_events_endpoint(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    body = _changes(client, from_revision=1, to_revision=2)
    change = body["changes"][0]
    assert change["before"] == client.get("/events/e1").json()
    assert change["after"] == client.get("/events/e2").json()


def test_changes_route_does_not_shadow_event_by_id(client):
    _post(client, "changes", "alice", 10)
    # The literal /events/changes path resolves to the diff endpoint...
    r = client.get("/events/changes", params={"from_revision": 0, "to_revision": 1})
    assert r.status_code == 200
    assert r.json()["changes"][0]["after"]["event_id"] == "changes"
    # ...while unrelated ids still hit the single-event lookup (404 unknown).
    assert client.get("/events/does-not-exist").status_code == 404


def test_missing_params_422(client):
    assert client.get("/events/changes").status_code == 422
    assert client.get(
        "/events/changes", params={"from_revision": 0}
    ).status_code == 422
    assert client.get(
        "/events/changes", params={"to_revision": 1}
    ).status_code == 422


def test_non_integer_params_422(client):
    assert client.get(
        "/events/changes",
        params={"from_revision": "abc", "to_revision": 2},
    ).status_code == 422


def test_negative_from_revision_422(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/events/changes",
        params={"from_revision": -1, "to_revision": 1},
    ).status_code == 422


def test_equal_revisions_422(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/events/changes",
        params={"from_revision": 1, "to_revision": 1},
    ).status_code == 422


def test_reversed_order_422(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    assert client.get(
        "/events/changes",
        params={"from_revision": 2, "to_revision": 1},
    ).status_code == 422


def test_zero_from_revision_allowed(client):
    _post(client, "e1", "alice", 10)
    body = _changes(client, from_revision=0, to_revision=1)
    assert body["changes"][0]["change_type"] == "added"


def test_to_revision_beyond_current_404(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/events/changes",
        params={"from_revision": 0, "to_revision": 2},
    ).status_code == 404
    assert client.get(
        "/events/changes",
        params={"from_revision": 5, "to_revision": 6},
    ).status_code == 404


def test_ordering_checked_before_range(client):
    # Both out of range and wrongly ordered: ordering wins -> 422.
    assert client.get(
        "/events/changes",
        params={"from_revision": 9, "to_revision": 5},
    ).status_code == 422


def test_empty_board_any_pair_is_404(client):
    assert client.get(
        "/events/changes",
        params={"from_revision": 0, "to_revision": 1},
    ).status_code == 404
