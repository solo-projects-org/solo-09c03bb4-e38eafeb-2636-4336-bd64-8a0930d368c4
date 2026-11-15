"""端到端集成测试：在本机用真实 pynetdicom 网络栈跑完整 DICOM 流程。

覆盖
====
* 正常批：C-FIND 固定清单 -> C-MOVE -> C-STORE 落盘登记 -> COMPLETED -> ZIP
* 多批并行、慢回传交错互不阻塞
* 源端重复投送：不重复计数
* 清单外实例 / 压缩实例：拒收、不入包
* 保存失败：C-STORE 回失败码且批置 FAILED，迟到回传不可恢复
* 重启恢复：未完成批失败，已完成批可下载（ZIP 可重建）

运行：python -m pytest tests/ -v
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
import requests
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEGBaseline8Bit,
)
from pynetdicom import AE
from pynetdicom.presentation import build_context

ROOT = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_http(url: str, timeout: float = 40) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(url, timeout=2).ok:
                return
        except requests.RequestException:
            time.sleep(0.5)
    raise TimeoutError(f"service not ready: {url}")


@pytest.fixture(scope="session")
def env_stack(tmp_path_factory):
    work = tmp_path_factory.mktemp("dicom-e2e")
    pacs_data = work / "pacs"
    api_data = work / "api"
    http_port = _free_port()
    store_port = _free_port()
    pacs_port = _free_port()

    spawned: list[subprocess.Popen] = []
    log_handles: list = []

    def _wait_port_free(port: int, timeout: float = 10.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    return
            time.sleep(0.3)
        raise RuntimeError(f"端口 {port} 长时间被占用")

    # 1) 生成虚构 CT
    gen = subprocess.run(
        [sys.executable, "-m", "simulator.generate_data",
         "--out", str(pacs_data), "--counts", "6,5,4"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )
    assert gen.returncode == 0

    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(ROOT),
        "PACS_DATA_DIR": str(pacs_data),
        "PACS_AET": "MYPACS",
        "PACS_PORT": str(pacs_port),
        "APP_AET": "RETRIEVER",
        "DEST_HOST": "127.0.0.1",
        "DEST_PORT": str(store_port),
        "MOVE_DELAY": "0.15",
        "MOVE_DUPLICATE_EVERY": "3",
    })
    pacs_log = open(work / "pacs.log", "w")
    log_handles.append(pacs_log)
    pacs_proc = subprocess.Popen(
        [sys.executable, "-m", "simulator.pacs"],
        cwd=ROOT, env=env, start_new_session=True,
        stdout=pacs_log,
        stderr=subprocess.STDOUT, text=True,
    )
    spawned.append(pacs_proc)

    api_env = dict(env)
    api_env.update({
        "DATA_DIR": str(api_data),
        "STORE_PORT": str(store_port),
        "APP_HOST": "127.0.0.1",
        "PACS_HOST": "127.0.0.1",
        "PACS_PORT": str(pacs_port),
        "DIMSE_TIMEOUT": "120",
        "NETWORK_TIMEOUT": "120",
    })
    api_log = open(work / "api.log", "w")
    log_handles.append(api_log)
    api_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(http_port), "--log-level", "info"],
        cwd=ROOT, env=api_env, start_new_session=True,
        stdout=api_log,
        stderr=subprocess.STDOUT, text=True,
    )
    spawned.append(api_proc)
    base = f"http://127.0.0.1:{http_port}"
    try:
        _wait_http(f"{base}/health")
        # 再等 C-STORE SCP 端口就绪
        deadline = time.time() + 15
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", store_port)) == 0:
                    break
            time.sleep(0.3)
        else:
            raise RuntimeError("C-STORE SCP 端口未就绪，见 "
                               f"{work / 'api.log'}")
        yield {
            "base": base,
            "store_port": store_port,
            "pacs_data": pacs_data,
            "api_data": api_data,
            "api_proc": api_proc,
            "pacs_proc": pacs_proc,
            "api_env": api_env,
            "http_port": http_port,
            "log_dir": work,
            "spawned": spawned,
            "log_handles": log_handles,
        }
    finally:
        import signal
        for p in spawned:
            if p.poll() is not None:
                continue
            try:
                os.killpg(os.getpgid(p.pid), signal.SIGTERM)
            except ProcessLookupError:
                p.terminate()
        for p in spawned:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except ProcessLookupError:
                    p.kill()
                p.wait(timeout=5)
        # 等待监听端口彻底释放，避免下一次会话遇到占用
        for port in (http_port, pacs_port, store_port):
            _wait_port_free(port)
        for handle in log_handles:
            handle.close()


def _studies(stack):
    return requests.get(f"{stack['base']}/internal/pacs-studies", timeout=15).json()


def _wait_terminal(stack, batch_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = requests.get(f"{stack['base']}/batches/{batch_id}",
                            timeout=5).json()
        if view["state"] in ("COMPLETED", "FAILED"):
            return view
        time.sleep(0.7)
    raise TimeoutError(batch_id)


def _send_store(ds, ts_list, stack):
    ae = AE(ae_title="ROGUE")
    ae.requested_contexts = [build_context(str(CTImageStorage), ts_list)]
    assoc = ae.associate("127.0.0.1", stack["store_port"],
                         ae_title="RETRIEVER")
    if not assoc.is_established:
        try:
            assoc.release()
        except Exception:
            pass
        return None  # 协商被拒
    try:
        status = assoc.send_c_store(ds)
        return int(status.Status)
    finally:
        if assoc.is_alive:
            assoc.release()


# =============================================================== tests
def test_01_health_and_pacs_listing(env_stack):
    r = requests.get(f"{env_stack['base']}/health", timeout=5)
    assert r.ok and r.json()["status"] == "ok"
    studies = _studies(env_stack)
    assert len(studies) == 3
    assert all(s["modality"] == "CT" for s in studies)


def test_02_happy_path_completes_and_zip_matches_manifest(env_stack):
    studies = _studies(env_stack)
    study = studies[0]["study_instance_uid"]
    r = requests.post(f"{env_stack['base']}/batches",
                      json={"study_instance_uid": study}, timeout=10)
    assert r.status_code == 201
    bid = r.json()["id"]
    view = _wait_terminal(env_stack, bid)
    assert view["state"] == "COMPLETED", view
    expected = studies[0]["instances"]
    assert view["expected_count"] == expected
    # 源端每 3 个实例重发一次：接收计数必须等于清单数
    assert view["received_count"] == expected

    info = requests.get(f"{env_stack['base']}/batches/{bid}/instances",
                        timeout=5).json()
    assert sorted(info["manifest"]) == sorted(info["received"])
    assert info["missing"] == []

    events = requests.get(f"{env_stack['base']}/batches/{bid}/events",
                          timeout=5).json()
    kinds = [e["kind"] for e in events]
    assert "MANIFEST_FIXED" in kinds and "BATCH_COMPLETED" in kinds
    # 应记录过去重事件
    assert any(k == "STORE_DUPLICATE" for k in kinds), kinds

    zr = requests.get(f"{env_stack['base']}/batches/{bid}/download",
                      timeout=20)
    assert zr.status_code == 200
    import io, zipfile
    zf = zipfile.ZipFile(io.BytesIO(zr.content))
    dcm = [n for n in zf.namelist() if n.startswith("dicom/")]
    assert len(dcm) == expected
    assert "MANIFEST.csv" in zf.namelist()


def test_03_parallel_batches_interleave_without_blocking(env_stack):
    studies = _studies(env_stack)
    t0 = time.time()
    ids = []
    for s in studies[1:]:
        r = requests.post(f"{env_stack['base']}/batches",
                          json={"study_instance_uid": s["study_instance_uid"]},
                          timeout=10)
        assert r.status_code == 201
        ids.append(r.json()["id"])
    views = [_wait_terminal(env_stack, bid) for bid in ids]
    elapsed = time.time() - t0
    assert all(v["state"] == "COMPLETED" for v in views), views
    # 两批并行（各 5/4 实例 * 0.15s + 重复），串行需要两倍时长
    assert elapsed < 12, f"并行批耗时过长：{elapsed:.1f}s"


def test_04_offlist_and_compressed_are_rejected(env_stack):
    from simulator.generate_data import _make_uid, build_ct_dataset

    studies = _studies(env_stack)
    study = studies[1]["study_instance_uid"]

    # 新建批并等到进入 MOVING，在回传进行中投 rogue 流量
    r = requests.post(f"{env_stack['base']}/batches",
                      json={"study_instance_uid": study}, timeout=10)
    assert r.status_code == 201
    bid = r.json()["id"]
    deadline = time.time() + 30
    while time.time() < deadline:
        view = requests.get(f"{env_stack['base']}/batches/{bid}",
                            timeout=5).json()
        if view["state"] == "MOVING":
            break
        time.sleep(0.3)
    assert view["state"] == "MOVING"

    rogue = build_ct_dataset(
        study_uid=study, series_uid=_make_uid(8888),
        sop_uid=_make_uid(9999), patient_id="P9", patient_name="R^X",
        instance_number=1, instance_count=1,
    )
    code = _send_store(rogue, [str(ExplicitVRLittleEndian),
                               str(ImplicitVRLittleEndian)], env_stack)
    assert code == 0xC000, (
        f"清单外实例应被拒收，实际状态 {code}"
    )

    comp = build_ct_dataset(
        study_uid=study, series_uid=_make_uid(8887),
        sop_uid=_make_uid(9997), patient_id="P8", patient_name="R^C",
        instance_number=1, instance_count=1,
    )
    comp.file_meta.TransferSyntaxUID = JPEGBaseline8Bit
    code = _send_store(comp, [str(JPEGBaseline8Bit)], env_stack)
    # SCP 只公布未压缩上下文：协商阶段即被拒（None）
    assert code is None, f"压缩实例应在协商阶段被拒，实际 {code}"

    # 原批不受 rogue 流量影响，仍正常完成
    view = _wait_terminal(env_stack, bid)
    assert view["state"] == "COMPLETED", view

    # 清单外/压缩文件绝不能出现在数据卷（rogue 的 SOP/Series UID 特征段）
    on_disk = list((env_stack["api_data"] / "batches").rglob("*.dcm"))
    assert all("8888" not in p.name and "8887" not in p.name
               and "9999" not in p.name and "9997" not in p.name
               for p in on_disk)


def test_05_unknown_study_store_rejected(env_stack):
    from simulator.generate_data import _make_uid, build_ct_dataset

    ds = build_ct_dataset(
        study_uid=_make_uid(424242), series_uid=_make_uid(424243),
        sop_uid=_make_uid(424244), patient_id="PX", patient_name="N^N",
        instance_number=1, instance_count=1,
    )
    code = _send_store(ds, [str(ExplicitVRLittleEndian)], env_stack)
    assert code == 0xC000


def test_06_save_failure_marks_batch_failed(env_stack):
    studies = _studies(env_stack)
    study = studies[2]["study_instance_uid"]
    r = requests.post(f"{env_stack['base']}/batches",
                      json={"study_instance_uid": study,
                            "fault_inject": "save_failure"}, timeout=10)
    assert r.status_code == 201
    bid = r.json()["id"]
    view = _wait_terminal(env_stack, bid)
    assert view["state"] == "FAILED"
    assert "保存失败" in (view["failure_reason"] or "")

    # 下载必须被拒绝
    dl = requests.get(f"{env_stack['base']}/batches/{bid}/download", timeout=5)
    assert dl.status_code == 409

    # 迟到回传不能恢复失败批
    from simulator.generate_data import build_ct_dataset
    import pydicom
    files = sorted((env_stack["pacs_data"] / study).glob("*.dcm"))
    ds = pydicom.dcmread(str(files[0]))
    code = _send_store(ds, [str(ExplicitVRLittleEndian),
                            str(ImplicitVRLittleEndian)], env_stack)
    assert code == 0xC000  # 已失败批拒收
    view2 = requests.get(f"{env_stack['base']}/batches/{bid}",
                         timeout=5).json()
    assert view2["state"] == "FAILED"


def test_07_validation_and_duplicate_active_conflict(env_stack):
    studies = _studies(env_stack)
    study = studies[0]["study_instance_uid"]

    # 非法故障注入参数 -> 400
    r = requests.post(f"{env_stack['base']}/batches",
                      json={"study_instance_uid": study,
                            "fault_inject": "bogus"}, timeout=10)
    assert r.status_code == 400

    # 空白 UID -> 400（应用层校验）
    r = requests.post(f"{env_stack['base']}/batches",
                      json={"study_instance_uid": "   "}, timeout=10)
    assert r.status_code == 400

    # 同一 Study 连续两个进行中批 -> 第二个 409
    r1 = requests.post(f"{env_stack['base']}/batches",
                       json={"study_instance_uid": study}, timeout=10)
    assert r1.status_code == 201
    r2 = requests.post(f"{env_stack['base']}/batches",
                       json={"study_instance_uid": study}, timeout=10)
    assert r2.status_code == 409
    bid = r1.json()["id"]
    view = _wait_terminal(env_stack, bid)
    assert view["state"] == "COMPLETED"

    # 终态后允许重新提交同一 Study
    r3 = requests.post(f"{env_stack['base']}/batches",
                       json={"study_instance_uid": study}, timeout=10)
    assert r3.status_code == 201
    view = _wait_terminal(env_stack, r3.json()["id"])
    assert view["state"] == "COMPLETED"


def test_08_concurrent_duplicate_stores_count_once(env_stack):
    """并发重发同一 SOP：MOVING 窗口内的竞态去重 + 终态后的迟到拒收。"""
    import threading
    import pydicom

    studies = _studies(env_stack)
    study = studies[2]["study_instance_uid"]

    # ---- 场景 A：新批进入 MOVING 后，多关联并发重发首个实例 ----
    r = requests.post(f"{env_stack['base']}/batches",
                      json={"study_instance_uid": study}, timeout=10)
    bid = r.json()["id"]
    deadline = time.time() + 30
    view = {}
    while time.time() < deadline:
        view = requests.get(f"{env_stack['base']}/batches/{bid}",
                            timeout=5).json()
        if view["state"] == "MOVING" and view["received_count"] >= 1:
            break
        time.sleep(0.2)
    assert view["state"] == "MOVING", view

    ds_template = pydicom.dcmread(
        str(sorted((env_stack["pacs_data"] / study).glob("*.dcm"))[0])
    )
    first_sop = str(ds_template.SOPInstanceUID)

    moving_results: list[int | None] = []
    lock = threading.Lock()

    def _hammer(ds, sink):
        ae = AE(ae_title="DUP")
        ae.requested_contexts = [
            build_context(str(CTImageStorage),
                          [str(ExplicitVRLittleEndian),
                           str(ImplicitVRLittleEndian)])
        ]
        assoc = ae.associate("127.0.0.1", env_stack["store_port"],
                             ae_title="RETRIEVER")
        code = None
        if assoc.is_established:
            code = int(assoc.send_c_store(ds).Status)
            assoc.release()
        with lock:
            sink.append(code)

    threads = [threading.Thread(target=_hammer,
                                args=(ds_template, moving_results))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(moving_results) == 5
    # 全部回 SUCCESS（重复投递语义上接受），但只允许计数一次
    assert all(code == 0x0000 for code in moving_results), moving_results

    view = _wait_terminal(env_stack, bid)
    assert view["state"] == "COMPLETED", view
    assert view["received_count"] == view["expected_count"]
    info = requests.get(f"{env_stack['base']}/batches/{bid}/instances",
                        timeout=5).json()
    assert info["manifest"].count(first_sop) == 1
    assert len(info["received"]) == len(set(info["received"]))
    # 磁盘上每个 SOP 仅一份文件
    batch_files = list(
        (env_stack["api_data"] / "batches" / bid).glob("*.dcm")
    )
    assert len(batch_files) == view["expected_count"]

    # ---- 场景 B：终态后再并发打，全部拒收且状态不变 ----
    late_results: list[int | None] = []
    threads = [threading.Thread(target=_hammer,
                                args=(ds_template, late_results))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert len(late_results) == 5
    assert all(code == 0xC000 for code in late_results), late_results
    final = requests.get(f"{env_stack['base']}/batches/{bid}",
                         timeout=5).json()
    assert final["state"] == "COMPLETED"
    assert final["received_count"] == view["expected_count"]


def test_09_restart_recovery(env_stack):
    # 先确认已有完成批
    batches = requests.get(f"{env_stack['base']}/batches", timeout=5).json()
    completed = next(b for b in batches if b["state"] == "COMPLETED")

    # 制造一个“进行中”的批：直接在 DB 里插入 MOVING 批并写清单，
    # 模拟进程在回传中途被杀
    import sqlite3
    db_path = env_stack["api_data"] / "retrieval.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO batches(id, study_instance_uid, state, expected_count, "
        "received_count, fault_inject, created_at, updated_at) "
        "VALUES ('orphan0001', ?, 'MOVING', 3, 1, '', '2026-01-01T00:00:00', "
        "'2026-01-01T00:00:00')",
        (completed["study_instance_uid"],),
    )
    for i, sop in enumerate(("sop1", "sop2", "sop3")):
        conn.execute(
            "INSERT INTO instances(batch_id, sop_instance_uid, sop_class_uid, "
            "transfer_syntax, file_path, received_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("orphan0001", sop, str(CTImageStorage),
             str(ExplicitVRLittleEndian),
             "/tmp/x" if i == 0 else "", "" if i else "2026-01-01"),
        )
    conn.commit()
    conn.close()

    proc = env_stack["api_proc"]
    proc.terminate()
    proc.wait(timeout=10)

    restart_log = open(env_stack["log_dir"] / "api-restart.log", "w")
    restarted = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(env_stack["http_port"]),
         "--log-level", "warning"],
        cwd=ROOT, env=env_stack["api_env"], start_new_session=True,
        stdout=restart_log, stderr=subprocess.STDOUT, text=True,
    )
    env_stack["api_proc"] = restarted
    env_stack["spawned"].append(restarted)
    env_stack["log_handles"].append(restart_log)
    try:
        _wait_http(f"{env_stack['base']}/health")
        orphan = requests.get(
            f"{env_stack['base']}/batches/orphan0001", timeout=5).json()
        assert orphan["state"] == "FAILED"
        assert "重启" in orphan["failure_reason"]

        # 已完成批仍可下载
        dl = requests.get(
            f"{env_stack['base']}/batches/{completed['id']}/download",
            timeout=20)
        assert dl.status_code == 200
    finally:
        pass
