"""批处理流水线：每个批一个后台线程，互不阻塞。"""
from __future__ import annotations

import logging
import threading

from . import dicom, storage
from .db import (
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_FINDING,
    STATE_MOVING,
    STATE_PENDING,
    Database,
)
from .quota import QuotaManager

log = logging.getLogger("pipeline")


class Pipeline:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()

    def submit(self, batch_id: str) -> None:
        with self._lock:
            t = threading.Thread(
                target=self._run, args=(batch_id,), name=f"batch-{batch_id[:8]}",
                daemon=True,
            )
            self._threads[batch_id] = t
        t.start()

    def _run(self, batch_id: str) -> None:
        db = self.db
        batch = db.get_batch(batch_id)
        if batch is None:
            return

        # ---------------- C-FIND：固定本批 SOP 清单 ----------------
        if not db.transition_state(batch_id, [STATE_PENDING], STATE_FINDING):
            return
        db.add_event("FIND_START", "开始 C-FIND 固定实例清单", batch_id)
        try:
            manifest = dicom.c_find_study(batch["study_instance_uid"])
        except dicom.FindError as exc:
            db.fail_batch(batch_id, f"C-FIND 失败：{exc}")
            log.warning("批 %s C-FIND 失败：%s", batch_id, exc)
            return
        except Exception as exc:  # 网络层意外
            db.fail_batch(batch_id, f"C-FIND 异常：{exc}")
            log.exception("批 %s C-FIND 异常", batch_id)
            return

        db.set_manifest(batch_id, manifest)
        db.add_event(
            "MANIFEST_FIXED",
            f"清单固定：{len(manifest)} 个非压缩 CT SOP，源端批内不增删",
            batch_id,
        )
        log.info("批 %s 清单固定 %d 个 SOP", batch_id, len(manifest))

        # ---------------- 故障注入：保存失败 ----------------
        if batch["fault_inject"] == storage.FAULT_SAVE:
            storage.make_batch_readonly(batch_id)
            db.add_event(
                "FAULT_ARMED",
                "已启用保存失败注入（批目录只读），下一次写入将失败",
                batch_id,
            )

        # ---------------- C-MOVE：回传由 C-STORE SCP 异步接收 ----------------
        if not db.transition_state(batch_id, [STATE_FINDING], STATE_MOVING):
            return
        db.add_event("MOVE_START", f"发起 C-MOVE，目标 AE={dicom.settings.APP_AET}",
                     batch_id)
        move_failed = False
        move_error = ""
        try:
            summary = dicom.c_move_study(batch["study_instance_uid"])
            db.add_event(
                "MOVE_DONE",
                f"C-MOVE 成功结束：completed={summary['completed']}",
                batch_id,
            )
        except dicom.MoveError as exc:
            move_failed = True
            move_error = str(exc)
            log.warning("批 %s C-MOVE 失败：%s", batch_id, exc)
        except Exception as exc:
            move_failed = True
            move_error = f"C-MOVE 异常：{exc}"
            log.exception("批 %s C-MOVE 异常", batch_id)

        # C-STORE 处理器可能已因保存失败把批置为 FAILED；终态不可改
        current = db.get_batch(batch_id)
        if current["state"] == STATE_FAILED:
            db.add_event(
                "MOVE_ABORT_NOTICE",
                "批已因接收侧错误失败；C-MOVE 结果不再改变终态",
                batch_id,
            )
            storage.restore_batch_writable(batch_id)
            return

        if move_failed:
            db.fail_batch(batch_id, f"C-MOVE 失败：{move_error}")
            storage.restore_batch_writable(batch_id)
            return

        # ---------------- 收尾核对：C-MOVE 成功且清单收齐才可下载 ----------------
        missing = db.missing_uids(batch_id)
        if missing:
            db.fail_batch(
                batch_id,
                f"C-MOVE 结束后缺件 {len(missing)} 个，示例：{missing[:3]}",
            )
            log.warning("批 %s 缺件 %d 个，判失败", batch_id, len(missing))
            storage.restore_batch_writable(batch_id)
            return

        try:
            zip_path = storage.build_zip(batch_id)
        except Exception as exc:
            db.fail_batch(batch_id, f"ZIP 打包失败：{exc}")
            log.exception("批 %s ZIP 打包失败", batch_id)
            storage.restore_batch_writable(batch_id)
            return

        storage.restore_batch_writable(batch_id)
        if not db.complete_batch(batch_id, str(zip_path)):
            # 理论上不会发生（状态已在本线程内核对），兜底防卡死在 MOVING
            db.fail_batch(batch_id, "完成登记失败，状态迁移未生效")
            return
        log.info("批 %s 完成，ZIP=%s", batch_id, zip_path)


# ---------------------------------------------------------------- recovery
def recover_on_startup(db: Database, quota: QuotaManager | None = None) -> None:
    """重启恢复。

    顺序很重要：

    1. 先按数据卷中**实际保留的文件**重建配额占用，并清理未登记残件
       （``.part``、未登记 ``.dcm``、孤儿批目录），保证配额不被幽灵占用；
    2. 未完成批一律置 FAILED（迟到回传不可恢复），其已存文件仍计入配额；
    3. 已完成批缺失 ZIP 则按登记文件补建，继续可下载。
    """
    if quota is not None:
        storage.reconcile_volume(db, quota)

    for batch in db.non_terminal_batches():
        db.fail_batch(
            batch["id"],
            "服务重启导致传输中断，未完成批按失败处理（迟到回传不可恢复）",
        )
        storage.restore_batch_writable(batch["id"])

    for batch in db.all_completed_batches():
        zip_path = storage.settings.ZIP_DIR / f"{batch['id']}.zip"
        if not zip_path.exists():
            try:
                rebuilt = storage.build_zip(batch["id"])
                db.transition_state(
                    batch["id"], [STATE_COMPLETED], STATE_COMPLETED,
                    zip_path=str(rebuilt),
                )
                db.add_event("ZIP_REBUILT", "重启后重建 ZIP", batch["id"])
            except Exception as exc:  # 数据卷损坏等
                db.transition_state(
                    batch["id"], [STATE_COMPLETED], STATE_FAILED,
                    failure_reason=f"重启后 ZIP 重建失败：{exc}",
                )
