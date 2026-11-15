"""配额端到端集成测试：真实 pynetdicom 网络栈 + 小字节配额。

覆盖
====
* 配额充足：正常批完成，/quota 与批视图的字节统计与磁盘文件一致
* 配额不足：仅当前批 FAILED 且 failure_reason 带缺口；其它有额度的批继续取数
* 合法重投不占额：重复实例后 stored_bytes 不重复增加
* 终态后重投仍被去重；失败批重投仍报缺口
* 重启后按卷中实际文件恢复配额、清理未登记残件，已完成包继续可下载

每个用例对执行顺序不敏感：断言基于“当前已用量”的相对关系而非绝对数值。
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


def _wait_port_free(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return
        time.sleep(0.3)
    raise RuntimeError(f"端口 {port} 长时间被占用")


@pytest.fixture()
def quota_stack(tmp_path_factory):
    """与 test_e2e 同构的独立栈，但给 API 配置很小的字节配额。

    配额 = 8 个实例大小：够单个检查完整完成（最大 6 个实例），
    但容不下两个并行批（4+6=10）。
    """
    work = tmp_path_factory.mktemp("dicom-quota")
    pacs_data = work / "pacs"
    api_data = work / "api"
    http_port = _free_port()
    store_port = _free_port()
    pacs_port = _free_port()

    spawned: list[subprocess.Popen] = []
    log_handles: list = []

    subprocess.run(
        [sys.executable, "-m", "simulator.generate_data",
         "--out", str(pacs_data), "--counts", "6,5,4"],
        cwd=ROOT, check=True, capture_output=True, text=True,
    )

    sample = next(pacs_data.rglob("*.dcm"))
    per_instance = sample.stat().st_size
    # 8 个实例容量：够单个检查完整完成（最大 6），但放不下两个并行批
    # （4+6=10），从而确定性出现“一个完成、一个额度不足失败”。
    quota_limit = per_instance * 8

    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(ROOT),
        "PACS_DATA_DIR": str(pacs_data),
        "PACS_AET": "MYPACS",
        "PACS_PORT": str(pacs_port),
        "APP_AET": "RETRIEVER",
        "DEST_HOST": "127.0.0.1",
        "DEST_PORT": str(store_port),
        "MOVE_DELAY": "0.05",
        "MOVE_DUPLICATE_EVERY": "3",
    })
    pacs_log = open(work / "pacs.log", "w")
    log_handles.append(pacs_log)
    pacs_proc = subprocess.Popen(
        [sys.executable, "-m", "simulator.pacs"],
        cwd=ROOT, env=env, start_new_session=True,
        stdout=pacs_log, stderr=subprocess.STDOUT, text=True,
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
        "QUOTA_LIMIT_BYTES": str(quota_limit),
    })
    api_log = open(work / "api.log", "w")
    log_handles.append(api_log)
    api_proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(http_port)],
        cwd=ROOT, env=api_env, start_new_session=True,
        stdout=api_log, stderr=subprocess.STDOUT, text=True,
    )
    spawned.append(api_proc)
    base = f"http://127.0.0.1:{http_port}"
    try:
        _wait_http(f"{base}/health")
        deadline = time.time() + 15
        while time.time() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", store_port)) == 0:
                    break
            time.sleep(0.3)
        else:
            raise RuntimeError("C-STORE SCP 端口未就绪")
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
            "quota_limit": quota_limit,
            "per_instance": per_instance,
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
        for port in (http_port, pacs_port, store_port):
            _wait_port_free(port)
        for handle in log_handles:
            handle.close()


def _studies(stack):
    return requests.get(f"{stack['base']}/internal/pacs-studies",
                        timeout=15).json()


def _wait_terminal(stack, batch_id, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        view = requests.get(f"{stack['base']}/batches/{batch_id}",
                            timeout=5).json()
        if view["state"] in ("COMPLETED", "FAILED"):
            return view
        time.sleep(0.5)
    raise TimeoutError(batch_id)


def _submit(stack, study_uid, fault=""):
    r = requests.post(f"{stack['base']}/batches",
                      json={"study_instance_uid": study_uid,
                            "fault_inject": fault}, timeout=10)
    assert r.status_code == 201, r.text
    return r.json()["id"]


# =============================================================== tests
def test_q01_health_reports_configured_quota(quota_stack):
    stack = quota_stack
    h = requests.get(f"{stack['base']}/health", timeout=5).json()
    assert h["quota_limit_bytes"] == stack["quota_limit"]
    q = requests.get(f"{stack['base']}/quota", timeout=5).json()
    assert q["unlimited"] is False
    assert q["limit_bytes"] == stack["quota_limit"]
    assert q["used_bytes"] == 0 and q["stored_bytes"] == 0
    assert q["available_bytes"] == stack["quota_limit"]


def test_q02_happy_batch_accounts_actual_bytes(quota_stack):
    stack = quota_stack
    study = _studies(stack)[2]["study_instance_uid"]   # 最小检查 4 实例
    bid = _submit(stack, study)
    view = _wait_terminal(stack, bid)
    assert view["state"] == "COMPLETED", view

    on_disk = sum(p.stat().st_size
                  for p in (stack["api_data"] / "batches" / bid).glob("*.dcm"))
    q = requests.get(f"{stack['base']}/quota", timeout=5).json()
    assert q["stored_bytes"] == on_disk
    assert q["reserved_bytes"] == 0
    assert q["batches"][bid]["stored_bytes"] == on_disk
    assert q["used_bytes"] <= q["limit_bytes"]

    view2 = requests.get(f"{stack['base']}/batches/{bid}", timeout=5).json()
    assert view2["stored_bytes"] == on_disk
    assert view2["quota_total_bytes"] == on_disk
    # 源端每 3 个实例重发一次：计数与字节都不重复
    assert view2["received_count"] == 4
    # 精确：磁盘上 4 个实例文件（去重后），字节统计与磁盘一致
    assert len(list((stack["api_data"] / "batches" / bid).glob("*.dcm"))) == 4
    assert view2["stored_bytes"] == on_disk


def test_q03_quota_shortage_fails_only_that_batch_with_gap(quota_stack):
    stack = quota_stack
    studies = _studies(stack)
    # 先跑一个 4 实例的小检查占用部分额度
    bid_ok = _submit(stack, studies[2]["study_instance_uid"])
    view_ok = _wait_terminal(stack, bid_ok)
    assert view_ok["state"] == "COMPLETED"

    q_before = requests.get(f"{stack['base']}/quota", timeout=5).json()

    # 再跑 6 实例的大检查：剩余额度放不下整个检查，必然在途中额度不足
    bid_big = _submit(stack, studies[0]["study_instance_uid"])
    view_big = _wait_terminal(stack, bid_big)
    assert view_big["state"] == "FAILED", view_big
    reason = view_big["failure_reason"] or ""
    assert "额度不足" in reason and "缺口" in reason, reason

    events = requests.get(f"{stack['base']}/batches/{bid_big}/events",
                          timeout=5).json()
    assert any(e["kind"] == "QUOTA_EXCEEDED" for e in events)

    q_after = requests.get(f"{stack['base']}/quota", timeout=5).json()
    # 总量绝不超限；在途预留全部归零（失败只释放本次预留，不留尾巴）
    assert q_after["used_bytes"] <= q_after["limit_bytes"]
    assert q_after["reserved_bytes"] == 0
    assert q_after["available_bytes"] == q_after["limit_bytes"] - q_after["used_bytes"]

    # 其它批已占额度不被失败批扣减
    assert q_after["batches"][bid_ok]["stored_bytes"] == \
        q_before["batches"][bid_ok]["stored_bytes"]

    # 已完成批不受影响，继续可下载
    dl = requests.get(f"{stack['base']}/batches/{bid_ok}/download", timeout=20)
    assert dl.status_code == 200


def test_q04_interleaved_batches_one_fails_other_continues(quota_stack):
    """并行交错：两个批同时回传，额度只够部分实例，必有批因额度失败。

    交错回传下哪个批先抢到额度取决于线程调度，因此不预设赢家；
    验证的是配额的核心隔离语义：恰好一个完成、另一个因额度不足失败、
    成功批额度不被失败批扣减、总量不超限、在途预留归零。
    """
    stack = quota_stack
    studies = _studies(stack)
    # 小检查（4 实例）与大检查（6 实例）并行；总容量只有 6 实例，需求 10
    b_small = _submit(stack, studies[2]["study_instance_uid"])  # 4
    b_big = _submit(stack, studies[0]["study_instance_uid"])    # 6
    v_small = _wait_terminal(stack, b_small)
    v_big = _wait_terminal(stack, b_big)

    views = {b_small: v_small, b_big: v_big}
    states = [v["state"] for v in views.values()]
    # 不可能两个都完成（需求 10 > 容量 6）
    assert states.count("COMPLETED") == 1, states
    assert states.count("FAILED") == 1, states

    failed_view = next(v for v in views.values() if v["state"] == "FAILED")
    assert "额度不足" in (failed_view["failure_reason"] or "")
    failed_events = requests.get(
        f"{stack['base']}/batches/{failed_view['id']}/events",
        timeout=5).json()
    assert any(e["kind"] == "QUOTA_EXCEEDED" for e in failed_events)

    q = requests.get(f"{stack['base']}/quota", timeout=5).json()
    assert q["used_bytes"] <= q["limit_bytes"]
    assert q["reserved_bytes"] == 0

    # 成功批的已存额度完整保留，没有被失败批的回滚扣减
    winner = next(bid for bid, v in views.items() if v["state"] == "COMPLETED")
    assert q["batches"][winner]["stored_bytes"] == views[winner]["stored_bytes"]
    # 成功批可下载，失败批 409
    dl_ok = requests.get(f"{stack['base']}/batches/{winner}/download", timeout=20)
    assert dl_ok.status_code == 200
    loser = next(bid for bid, v in views.items() if v["state"] == "FAILED")
    dl_no = requests.get(f"{stack['base']}/batches/{loser}/download", timeout=10)
    assert dl_no.status_code == 409


def test_q05_resubmit_failed_batch_reports_gap_again(quota_stack):
    stack = quota_stack
    studies = _studies(stack)
    pi = stack["per_instance"]

    # 连续提交批直到出现额度不足失败，把数据卷填到无法再收一个完整实例
    fill_views = []
    for study in studies:
        bid = _submit(stack, study["study_instance_uid"])
        view = _wait_terminal(stack, bid)
        fill_views.append(view)
        if view["state"] == "FAILED":
            assert "额度不足" in (view["failure_reason"] or "")
            break
    q = requests.get(f"{stack['base']}/quota", timeout=5).json()
    # 至少有一个批失败，且剩余空间不足一个实例
    assert any(v["state"] == "FAILED" for v in fill_views), fill_views
    assert q["available_bytes"] < pi

    # 终态后允许重新提交同一 Study：额度仍不足，首个实例即失败并再报缺口
    failed_study = next(v["study_instance_uid"] for v in fill_views
                        if v["state"] == "FAILED")
    bid_again = _submit(stack, failed_study)
    view_again = _wait_terminal(stack, bid_again)
    assert view_again["state"] == "FAILED"
    assert "额度不足" in (view_again["failure_reason"] or "")
    assert "缺口" in (view_again["failure_reason"] or "")
    # 重投不增加任何占用
    q2 = requests.get(f"{stack['base']}/quota", timeout=5).json()
    assert q2["used_bytes"] == q["used_bytes"]


def test_q06_restart_restores_quota_and_cleans_residuals(quota_stack):
    stack = quota_stack
    studies = _studies(stack)
    bid = _submit(stack, studies[2]["study_instance_uid"])  # 4 实例
    view = _wait_terminal(stack, bid)
    assert view["state"] == "COMPLETED"

    batches_dir = stack["api_data"] / "batches"
    before = sum(p.stat().st_size for p in batches_dir.rglob("*.dcm"))

    # 手动制造未登记残件：.part 与未登记 .dcm
    target_dir = batches_dir / bid
    (target_dir / "crash.part").write_bytes(b"x" * 1234)
    (target_dir / "unregistered.dcm").write_bytes(b"y" * 2345)

    proc = stack["api_proc"]
    proc.terminate()
    proc.wait(timeout=10)

    log = open(stack["log_dir"] / "api-restart.log", "w")
    restarted = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app",
         "--host", "127.0.0.1", "--port", str(stack["http_port"])],
        cwd=ROOT, env=stack["api_env"], start_new_session=True,
        stdout=log, stderr=subprocess.STDOUT, text=True,
    )
    stack["api_proc"] = restarted
    stack["spawned"].append(restarted)
    stack["log_handles"].append(log)
    _wait_http(f"{stack['base']}/health")

    # 残件已清理
    assert not (target_dir / "crash.part").exists()
    assert not (target_dir / "unregistered.dcm").exists()

    # 配额按实际文件恢复（残件不计入，在途预留归零）
    q = requests.get(f"{stack['base']}/quota", timeout=5).json()
    assert q["stored_bytes"] == before
    assert q["reserved_bytes"] == 0
    assert q["used_bytes"] <= q["limit_bytes"]

    # 已完成包继续可下载
    dl = requests.get(f"{stack['base']}/batches/{bid}/download", timeout=20)
    assert dl.status_code == 200

    events = requests.get(f"{stack['base']}/batches/{bid}/events",
                          timeout=5).json()
    kinds = [e["kind"] for e in events]
    assert "RECOVERY_CLEANUP" in kinds
    assert "RECOVERY_QUOTA_RESTORED" in kinds
