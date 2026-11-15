"""端到端演示编排（在 api 容器内或任何能访问 API/PACS 的环境运行）。

演示序列
========
1. 列出模拟 PACS 中的虚构 CT 检查
2. 并行提交两个正常批（交错/慢回传互不阻塞，其中一个启用源端重复投送）
3. 回传进行中投送“清单外实例”和“压缩实例”，验证均被拒收、不入包
4. 提交一个 save_failure 批，验证保存失败即批失败、C-STORE 回失败码、
   只释放本次预留而不扣减其它批额度
5. 观察数据卷字节配额：已存 + 在途预留；再提交批触发“额度不足仅该批失败”，
   失败原因带具体缺口，重投不再占额且仍报缺口
6. 轮询进度，下载完成批 ZIP 并校验实例数与清单一致
7. 重启恢复演示：制造一个未完成批后调用进程退出，验证重启后该批判失败、
   已完成批仍可下载、配额按卷中实际文件恢复、未登记残件被清理

运行::

    python -m demo.run_demo            # 完整演示
    python -m demo.run_demo --skip-restart
    python -m demo.run_demo --quota-bytes 200000   # 覆盖配额阈值
"""
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import zipfile

import requests

# 默认走 compose 服务名；本地裸跑时可用环境变量覆盖为 127.0.0.1
API = os.getenv("API_BASE", "http://api:8000")
HOST_API = os.getenv("STORE_HOST", "api")
PORT_API = int(os.getenv("STORE_PORT", "11113"))


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72)


def wait_state(batch_id: str, target: set[str], timeout: float = 180) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{API}/batches/{batch_id}", timeout=10)
        r.raise_for_status()
        view = r.json()
        if view["state"] in target:
            return view
        print(f"    ... {batch_id[:8]} {view['state']} "
              f"{view['received_count']}/{view['expected_count']} "
              f"占用={view['quota_total_bytes']}B")
        time.sleep(1.5)
    raise TimeoutError(f"批 {batch_id} 未在 {timeout}s 内到达 {target}")


def show_events(batch_id: str) -> None:
    events = requests.get(f"{API}/batches/{batch_id}/events",
                          timeout=10).json()
    for e in events:
        print(f"    [{e['created_at'][11:]}] {e['kind']}: {e['message']}")


def show_quota(title: str) -> dict:
    q = requests.get(f"{API}/quota", timeout=10).json()
    if q["unlimited"]:
        print(f"    [{title}] 未配置配额上限（QUOTA_LIMIT_BYTES=0）")
    else:
        print(f"    [{title}] 上限={q['limit_bytes']}B "
              f"已存={q['stored_bytes']}B 在途预留={q['reserved_bytes']}B "
              f"已用={q['used_bytes']}B 可用={q['available_bytes']}B")
        for bid, b in q["batches"].items():
            print(f"        批 {bid[:8]}: 已存={b['stored_bytes']}B "
                  f"预留={b['reserved_bytes']}B 合计={b['total_bytes']}B")
    return q


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-restart", action="store_true",
                        help="跳过重启恢复演示")
    parser.add_argument(
        "--quota-bytes", type=int, default=None,
        help="覆盖数据卷字节配额（需 API 以该 QUOTA_LIMIT_BYTES 启动）；"
             "默认只读取 API 当前配置",
    )
    args = parser.parse_args()

    banner("1. 列出模拟 PACS 中的检查")
    studies = requests.get(f"{API}/internal/pacs-studies", timeout=20).json()
    for s in studies:
        print(f"    Study={s['study_instance_uid']} 患者={s['patient_name']} "
              f"实例数={s['instances']}")
    if len(studies) < 3:
        print("PACS 至少需要 3 个检查")
        return 1
    study_a, study_b, study_c = (s["study_instance_uid"] for s in studies[:3])

    quota = show_quota("初始")
    if args.quota_bytes is not None:
        print(f"    提示：演示期望 API 配额上限为 {args.quota_bytes}B，"
              f"当前为 {quota['limit_bytes']}B")

    banner("2. 并行提交批 A（含源端重复投送）与批 B，验证交错回传互不阻塞")
    # 批 A 的重复投送由 PACS 环境变量 MOVE_DUPLICATE_EVERY=3 控制
    r_a = requests.post(f"{API}/batches",
                        json={"study_instance_uid": study_a}, timeout=10)
    r_a.raise_for_status()
    batch_a = r_a.json()["id"]
    r_b = requests.post(f"{API}/batches",
                        json={"study_instance_uid": study_b}, timeout=10)
    r_b.raise_for_status()
    batch_b = r_b.json()["id"]
    print(f"    批 A={batch_a}  Study={study_a}")
    print(f"    批 B={batch_b}  Study={study_b}")

    # 等清单固定、进入 MOVING，再投 rogue 流量；同时观察在途预留
    time.sleep(3)
    show_quota("回传进行中（可见在途预留随 C-STORE 起伏）")

    banner("3. 清单外实例 / 压缩实例攻击，均应被拒收且不入包")
    from simulator import rogue_sender

    old_argv = sys.argv
    sys.argv = ["rogue", "off-list", "--study", study_a,
                "--host", HOST_API, "--port", str(PORT_API)]
    try:
        rc_offlist = rogue_sender.main()
    finally:
        sys.argv = old_argv
    sys.argv = ["rogue", "compressed", "--study", study_b,
                "--host", HOST_API, "--port", str(PORT_API)]
    try:
        rc_compressed = rogue_sender.main()
    finally:
        sys.argv = old_argv
    print(f"    清单外投送拒收校验返回码={rc_offlist}（0=符合预期）")
    print(f"    压缩投送拒收校验返回码={rc_compressed}（0=符合预期）")

    banner("4. 提交批 C 并注入保存失败（批目录只读，真实写盘失败）")
    r_c = requests.post(f"{API}/batches",
                        json={"study_instance_uid": study_c,
                              "fault_inject": "save_failure"}, timeout=10)
    r_c.raise_for_status()
    batch_c = r_c.json()["id"]
    print(f"    批 C={batch_c}（save_failure）：写盘/登记失败只会释放"
            f"本次预留，不扣减 A/B 已占额度")

    banner("5. 等待三个批到达终态并观察配额")
    view_a = wait_state(batch_a, {"COMPLETED", "FAILED"})
    view_b = wait_state(batch_b, {"COMPLETED", "FAILED"})
    view_c = wait_state(batch_c, {"COMPLETED", "FAILED"}, timeout=120)
    print(f"    批 A 终态={view_a['state']} "
          f"{view_a['received_count']}/{view_a['expected_count']} "
          f"已存={view_a['stored_bytes']}B")
    print(f"    批 B 终态={view_b['state']} "
          f"{view_b['received_count']}/{view_b['expected_count']} "
          f"已存={view_b['stored_bytes']}B")
    print(f"    批 C 终态={view_c['state']} 原因={view_c['failure_reason']}")
    show_quota("A/B 终态后（在途预留应归零）")

    print("\n  -- 批 A 事件流水（可见重复投送去重、rogue 拒收）--")
    show_events(batch_a)
    print("\n  -- 批 C 事件流水（可见保存失败）--")
    show_events(batch_c)

    banner("6. 额度不足：仅让新批失败并说明缺口，其它批不受影响")
    r_d = requests.post(f"{API}/batches",
                        json={"study_instance_uid": study_c}, timeout=10)
    r_d.raise_for_status()
    batch_d = r_d.json()["id"]
    view_d = wait_state(batch_d, {"COMPLETED", "FAILED"}, timeout=120)
    print(f"    批 D={batch_d} 终态={view_d['state']}")
    print(f"    失败原因：{view_d['failure_reason']}")
    if view_d["state"] == "FAILED":
        assert "额度不足" in (view_d["failure_reason"] or "")
        assert "缺口" in (view_d["failure_reason"] or "")
        print("    符合预期：仅批 D 失败，原因含具体缺口字节数")
        show_events(batch_d)
        # 重投同一 Study：合法重投不占额，但新实例仍会因额度不足失败
        r_d2 = requests.post(f"{API}/batches",
                             json={"study_instance_uid": study_c}, timeout=10)
        r_d2.raise_for_status()
        batch_d2 = r_d2.json()["id"]
        view_d2 = wait_state(batch_d2, {"COMPLETED", "FAILED"}, timeout=120)
        print(f"    重投批 {batch_d2[:8]} 终态={view_d2['state']} "
              f"原因={view_d2['failure_reason']}")
        assert view_d2["state"] == "FAILED" and "缺口" in (view_d2["failure_reason"] or "")
    else:
        print("    当前配额仍充足，批 D 正常完成（可调小 QUOTA_LIMIT_BYTES 复现额度不足）")
    quota_after = show_quota("额度不足演练后（已存额度不被失败批扣减）")

    banner("7. 下载批 A 的 ZIP 并核对内容")
    if view_a["state"] != "COMPLETED":
        print("    批 A 未完成，跳过下载校验")
    else:
        resp = requests.get(f"{API}/batches/{batch_a}/download", timeout=30)
        resp.raise_for_status()
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        dicom_files = [n for n in zf.namelist() if n.startswith("dicom/")]
        manifest = zf.read("MANIFEST.csv").decode().strip().splitlines()
        print(f"    ZIP 内 DICOM 文件数={len(dicom_files)}，"
              f"清单行数（含表头）={len(manifest)}")
        assert len(dicom_files) == view_a["expected_count"], "ZIP 实例数与清单不符"
        assert len(manifest) == view_a["expected_count"] + 1
        print("    校验通过：ZIP 仅含清单内实例，数量一致")

    # 对失败批下载应被拒绝
    denied = requests.get(f"{API}/batches/{batch_c}/download", timeout=10)
    print(f"    失败批 C 下载 HTTP 状态={denied.status_code}（期望 409）")
    assert denied.status_code == 409

    if args.skip_restart:
        banner("演示完成（已跳过重启恢复）")
        return 0

    banner("8. 重启恢复：制造未完成批后重启进程，配额按卷内实际文件恢复")
    # 再提交一个新检查的批，立刻杀掉进程，使其停在非终态。
    r_e = requests.post(f"{API}/batches",
                        json={"study_instance_uid": study_b}, timeout=10)
    if r_e.status_code == 409:
        print("    存在同 Study 进行中批，重启恢复场景略过")
    else:
        r_e.raise_for_status()
        batch_e = r_e.json()["id"]
        print(f"    批 E={batch_e} 已提交，立即触发进程退出")
        try:
            requests.post(f"{API}/internal/suicide", timeout=5)
        except requests.RequestException:
            pass
        # 等待容器编排（compose restart: always）或外部守护拉起进程
        deadline = time.time() + 90
        healthy = False
        while time.time() < deadline:
            try:
                if requests.get(f"{API}/health", timeout=3).ok:
                    healthy = True
                    break
            except requests.RequestException:
                time.sleep(1.5)
        if not healthy:
            print("    服务未在 90s 内恢复（本地裸跑请手动重启 API 后重试）")
            return 2
        view_e = wait_state(batch_e, {"COMPLETED", "FAILED"})
        print(f"    重启后批 E 状态={view_e['state']} "
              f"原因={view_e['failure_reason']}")
        assert view_e["state"] == "FAILED"
        assert "重启" in (view_e["failure_reason"] or "")
        show_events(batch_e)

        # 配额按卷中实际文件恢复：已用字节与重启前一致（残件不计入）
        quota_restored = show_quota("重启恢复后")
        assert quota_restored["reserved_bytes"] == 0, "重启后不应存在在途预留"
        assert quota_restored["stored_bytes"] == quota_after["stored_bytes"], (
            "重启后按实际文件恢复的已存额度应与重启前一致"
        )

        # 已完成批仍可下载；若 ZIP 丢失也会在启动时重建
        if view_a["state"] == "COMPLETED":
            again = requests.get(f"{API}/batches/{batch_a}/download", timeout=30)
            print(f"    重启后已完成批 A 下载 HTTP 状态={again.status_code}（期望 200）")
            assert again.status_code == 200

    banner("全部演示完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
