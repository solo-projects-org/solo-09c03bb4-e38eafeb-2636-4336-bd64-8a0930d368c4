"""字节配额与重启恢复的单元测试（无网络）。

覆盖
====
* QuotaManager：预留/提交/释放、额度不足缺口、互不扣减、不限额模式
* 重启恢复：按卷中实际文件重建配额、清理 .part/未登记残件/孤儿目录、
  已登记但文件缺失的登记清空
* 编码字节数 == 落盘文件大小（按实例实际大小预留的前提）
"""
from __future__ import annotations

import pytest

from app import storage
from app.config import Settings
from app.db import Database, set_db
from app.quota import QuotaExceeded, QuotaManager
from simulator.generate_data import build_ct_dataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

TS = str(ExplicitVRLittleEndian)
CT = str(CTImageStorage)


# ============================================================ QuotaManager
def test_reserve_commit_release_basic():
    q = QuotaManager(100)
    token = q.reserve("b1", 40)
    assert q.usage() == (0, 40)
    q.commit("b1", token, 40)
    assert q.usage() == (40, 0)

    token2 = q.reserve("b2", 30)
    assert q.usage() == (40, 30)
    q.release("b2", token2, 30)
    assert q.usage() == (40, 0)
    # 释放后额度可被其它批重新使用
    token3 = q.reserve("b3", 60)
    q.commit("b3", token3, 60)
    assert q.usage() == (100, 0)


def test_quota_exceeded_reports_gap_and_changes_nothing():
    q = QuotaManager(100)
    q.commit("b1", q.reserve("b1", 60), 60)
    token = q.reserve("b2", 30)
    q.commit("b2", token, 30)  # 已占 90

    with pytest.raises(QuotaExceeded) as ei:
        q.reserve("b3", 25)
    assert ei.value.gap == 15          # 90 + 25 - 100
    assert ei.value.size == 25
    # 拒绝后任何占用都不变
    assert q.usage() == (90, 0)
    snap = q.snapshot()
    assert set(snap.batches) == {"b1", "b2"}
    assert snap.available_bytes == 10


def test_failed_batch_release_does_not_touch_other_batches():
    q = QuotaManager(100)
    other = q.reserve("other", 40)
    q.commit("other", other, 40)

    mine = q.reserve("mine", 30)
    q.release("mine", mine, 30)       # 本批写盘失败，只释放本次预留

    snap = q.snapshot()
    assert snap.batches["other"]["stored_bytes"] == 40
    assert "mine" not in snap.batches or snap.batches["mine"]["total_bytes"] == 0
    assert snap.used_bytes == 40


def test_duplicate_reservation_does_not_double_count():
    q = QuotaManager(100)
    t1 = q.reserve("b1", 50)
    q.commit("b1", t1, 50)
    # 重复实例不进入配额逻辑；此处模拟再次预留同一批的另一实例前，
    # 已存额度稳定不变
    assert q.snapshot().batches["b1"]["stored_bytes"] == 50
    t2 = q.reserve("b1", 50)
    q.commit("b1", t2, 50)
    assert q.usage() == (100, 0)
    with pytest.raises(QuotaExceeded):
        q.reserve("b2", 1)


def test_unlimited_mode():
    q = QuotaManager(0)
    assert q.unlimited
    for i in range(1000):
        t = q.reserve("big", 10 ** 9)
        q.commit("big", t, 10 ** 9)
    snap = q.snapshot()
    assert snap.available_bytes is None and snap.unlimited
    assert snap.stored_bytes == 1000 * 10 ** 9


def test_concurrent_reservations_never_oversell():
    import threading

    q = QuotaManager(1000)
    granted: list[int] = []
    lock = threading.Lock()

    def worker():
        try:
            token = q.reserve("t", 100)
        except QuotaExceeded:
            return
        with lock:
            granted.append(1)
        # 模拟在途：不立即提交
        q.commit("t", token, 100)

    threads = [threading.Thread(target=worker) for _ in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(granted) == 10          # 只有 10 个 100 字节能放下
    assert q.usage()[0] <= 1000


# ============================================================ 重启恢复
@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    data = tmp_path / "data"
    monkeypatch.setattr(Settings, "DATA_DIR", data)
    monkeypatch.setattr(Settings, "DB_PATH", data / "retrieval.db")
    monkeypatch.setattr(Settings, "BATCHES_DIR", data / "batches")
    monkeypatch.setattr(Settings, "ZIP_DIR", data / "zips")
    Settings.ensure_dirs()
    db = Database(Settings.DB_PATH)
    set_db(db)
    quota = QuotaManager(10 ** 9)
    yield data, db, quota
    db.close()


def _ds(sop: str):
    return build_ct_dataset(
        study_uid="1.2.3.9", series_uid="1.2.3.8", sop_uid=sop,
        patient_id="P1", patient_name="T^T", instance_number=1,
        instance_count=1,
    )


def _moving_batch(db: Database, bid: str, sops: list[str]):
    db.create_batch(bid, "1.2.3.9", "")
    db.transition_state(bid, ["PENDING"], "FINDING")
    db.set_manifest(bid, [(s, CT, TS) for s in sops])
    db.transition_state(bid, ["FINDING"], "MOVING")


def test_encoded_size_equals_written_file_size(workspace):
    data, db, quota = workspace
    _moving_batch(db, "b1", ["1.2.3.4"])
    ds = _ds("1.2.3.4")
    payload = storage.encode_dataset(ds, TS)
    path = storage.write_dataset("b1", ds, TS, payload=payload)
    assert path.stat().st_size == len(payload)


def test_reconcile_rebuilds_quota_from_actual_files(workspace):
    data, db, quota = workspace
    _moving_batch(db, "b1", ["1.2.3.4", "1.2.3.5"])
    sizes = {}
    for sop in ("1.2.3.4", "1.2.3.5"):
        ds = _ds(sop)
        payload = storage.encode_dataset(ds, TS)
        path = storage.write_dataset("b1", ds, TS, payload=payload)
        assert db.register_instance("b1", sop, str(path), TS, len(payload))
        sizes[sop] = len(payload)

    # 模拟重启：新建配额管理器（内存占用归零），再按卷重建
    fresh_quota = QuotaManager(10 ** 9)
    per_batch = storage.reconcile_volume(db, fresh_quota)
    assert per_batch == {"b1": sum(sizes.values())}
    snap = fresh_quota.snapshot()
    assert snap.stored_bytes == sum(sizes.values())
    assert snap.reserved_bytes == 0     # 重启不保留在途预留


def test_reconcile_removes_unregistered_residuals(workspace):
    data, db, quota = workspace
    _moving_batch(db, "b1", ["1.2.3.4"])
    ds = _ds("1.2.3.4")
    payload = storage.encode_dataset(ds, TS)
    path = storage.write_dataset("b1", ds, TS, payload=payload)
    db.register_instance("b1", "1.2.3.4", str(path), TS, len(payload))
    good_size = len(payload)

    bdir = Settings.BATCHES_DIR / "b1"
    # 崩溃残留：未登记的 .part 与未登记 .dcm
    (bdir / "x.dcm.part").write_bytes(b"partial" * 100)
    (bdir / "rogue.dcm").write_bytes(b"rogue" * 100)
    # 孤儿批目录
    orphan = Settings.BATCHES_DIR / "orphan-batch"
    orphan.mkdir(parents=True)
    (orphan / "ghost.dcm").write_bytes(b"ghost" * 50)

    fresh_quota = QuotaManager(10 ** 9)
    per_batch = storage.reconcile_volume(db, fresh_quota)

    assert not (bdir / "x.dcm.part").exists()
    assert not (bdir / "rogue.dcm").exists()
    assert not orphan.exists()
    # 残件不计入配额，只有合法文件计入
    assert per_batch == {"b1": good_size}
    assert fresh_quota.snapshot().stored_bytes == good_size
    events = [e["kind"] for e in db.list_events("b1")]
    assert "RECOVERY_CLEANUP" in events
    # 全局清理事件（无 batch_id）也应存在
    all_events = db._conn.execute("SELECT kind FROM events").fetchall()
    assert any(r["kind"] == "RECOVERY_QUOTA_RESTORED" for r in all_events)


def test_reconcile_clears_registration_when_file_missing(workspace):
    data, db, quota = workspace
    _moving_batch(db, "b1", ["1.2.3.4", "1.2.3.5"])
    ds = _ds("1.2.3.4")
    payload = storage.encode_dataset(ds, TS)
    path = storage.write_dataset("b1", ds, TS, payload=payload)
    db.register_instance("b1", "1.2.3.4", str(path), TS, len(payload))
    # 第二个实例只登记、文件被外部删除
    missing_path = Settings.BATCHES_DIR / "b1" / "missing.dcm"
    db.register_instance("b1", "1.2.3.5", str(missing_path), TS, 123)

    fresh_quota = QuotaManager(10 ** 9)
    per_batch = storage.reconcile_volume(db, fresh_quota)
    # 只统计实际存在的文件
    assert per_batch == {"b1": len(payload)}
    # 缺失文件的登记被清空，进入 missing 清单
    assert "1.2.3.5" in db.missing_uids("b1")
    assert db.get_batch("b1")["received_count"] == 1


def test_reconcile_quota_blocks_new_batch_after_rebuild(workspace):
    """重建出的已存额度同样参与限额：剩余空间不足时新预留被拒。"""
    data, db, quota = workspace
    _moving_batch(db, "b1", ["1.2.3.4"])
    ds = _ds("1.2.3.4")
    payload = storage.encode_dataset(ds, TS)
    path = storage.write_dataset("b1", ds, TS, payload=payload)
    db.register_instance("b1", "1.2.3.4", str(path), TS, len(payload))

    tight = QuotaManager(len(payload) + 5)     # 只剩 5 字节
    storage.reconcile_volume(db, tight)
    with pytest.raises(QuotaExceeded):
        tight.reserve("b2", 6)
