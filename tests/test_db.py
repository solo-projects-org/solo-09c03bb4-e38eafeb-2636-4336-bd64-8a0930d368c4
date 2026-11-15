"""Database 状态机与去重规则的单元测试（无网络）。"""
from __future__ import annotations

import pytest

from app.db import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_FINDING,
    STATE_MOVING,
    STATE_PENDING,
    Database,
)

CT = "1.2.840.10008.5.1.4.1.1.2"
TS = "1.2.840.10008.1.2.1"


@pytest.fixture()
def db(tmp_path):
    return Database(tmp_path / "test.db")


def _manifest(n: int):
    return [(f"1.2.3.{i}", CT, TS) for i in range(n)]


def test_lifecycle_and_terminal_immutability(db):
    db.create_batch("b1", "1.2.840.11.1", "")
    assert db.transition_state("b1", [STATE_PENDING], STATE_FINDING)
    db.set_manifest("b1", _manifest(3))
    assert db.transition_state("b1", [STATE_FINDING], STATE_MOVING)

    # 终态失败后不可再迁移（迟到回传无法复活）
    assert db.fail_batch("b1", "boom")
    row = db.get_batch("b1")
    assert row["state"] == STATE_FAILED and row["failure_reason"] == "boom"
    assert not db.transition_state("b1", [STATE_MOVING], STATE_MOVING)
    assert not db.transition_state("b1", [STATE_FAILED], STATE_COMPLETED)
    assert db.get_batch("b1")["state"] == STATE_FAILED

def test_manifest_fixed_once(db):
    db.create_batch("b1", "1.2.840.11.2", "")
    db.transition_state("b1", [STATE_PENDING], STATE_FINDING)
    db.set_manifest("b1", _manifest(2))
    with pytest.raises(RuntimeError):
        db.set_manifest("b1", _manifest(4))
    assert db.get_batch("b1")["expected_count"] == 2


def test_register_dedup_and_offlist(db):
    db.create_batch("b1", "1.2.840.11.3", "")
    db.transition_state("b1", [STATE_PENDING], STATE_FINDING)
    db.set_manifest("b1", _manifest(2))
    db.transition_state("b1", [STATE_FINDING], STATE_MOVING)

    # 清单外
    assert not db.is_manifest_uid("b1", "1.2.3.99")
    assert db.register_instance("b1", "1.2.3.99", "/x", TS) is False

    # 首次登记计数
    assert db.register_instance("b1", "1.2.3.0", "/f0", TS)
    assert db.get_batch("b1")["received_count"] == 1
    # 重复登记不计数
    assert db.register_instance("b1", "1.2.3.0", "/f0-dup", TS) is False
    assert db.get_batch("b1")["received_count"] == 1
    assert db.is_already_received("b1", "1.2.3.0")

    assert db.register_instance("b1", "1.2.3.1", "/f1", TS)
    expected, received = db.received_progress("b1")
    assert (expected, received) == (2, 2)
    assert db.missing_uids("b1") == []


def test_study_routing_picks_latest_batch(db):
    db.create_batch("b-old", "1.2.840.22.1", "")
    db.fail_batch("b-old", "old failure")
    db.create_batch("b-new", "1.2.840.22.1", "")
    batch = db.find_batch_for_study("1.2.840.22.1")
    assert batch["id"] == "b-new"
    assert db.find_batch_for_study("missing") is None


def test_active_study_guard(db):
    db.create_batch("b1", "1.2.840.33.1", "")
    assert db.has_active_batch_for_study("1.2.840.33.1")
    db.fail_batch("b1", "done")
    assert not db.has_active_batch_for_study("1.2.840.33.1")


def test_recovery_lists_only_non_terminal(db):
    db.create_batch("b-done", "1.2.840.44.1", "")
    db.transition_state("b-done", [STATE_PENDING], STATE_FINDING)
    db.set_manifest("b-done", _manifest(1))
    db.transition_state("b-done", [STATE_FINDING], STATE_MOVING)
    db.register_instance("b-done", "1.2.3.0", "/f", TS)
    db.complete_batch("b-done", "/z.zip")

    db.create_batch("b-moving", "1.2.840.44.2", "")
    db.transition_state("b-moving", [STATE_PENDING], STATE_FINDING)
    db.set_manifest("b-moving", _manifest(2))
    db.transition_state("b-moving", [STATE_FINDING], STATE_MOVING)

    db.create_batch("b-failed", "1.2.840.44.3", "")
    db.fail_batch("b-failed", "already failed")

    active = {b["id"] for b in db.non_terminal_batches()}
    assert active == {"b-moving"}
    completed = {b["id"] for b in db.all_completed_batches()}
    assert completed == {"b-done"}


def test_events_recorded(db):
    db.create_batch("b1", "1.2.840.55.1", "")
    db.fail_batch("b1", "x")
    kinds = [e["kind"] for e in db.list_events("b1")]
    assert kinds == ["BATCH_CREATED", "BATCH_FAILED"]
