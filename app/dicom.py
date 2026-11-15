"""DICOM 网络层。

* :func:`start_store_scp` —— C-STORE SCP，接收源端 C-MOVE 推回的实例，
  严格按“先写盘并 fsync、再登记，两者都成功才回 SUCCESS”的顺序处理。
* :func:`c_find_study` —— 对源端做 IMAGE 级 C-FIND，固定本批 SOP 清单。
* :func:`c_move_study` —— 发起 STUDY 级 C-MOVE，并汇总状态。
"""
from __future__ import annotations

import logging
from typing import Any

from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
from pynetdicom import AE, evt
from pynetdicom.presentation import build_context
from pynetdicom.sop_class import (
    CTImageStorage,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
    Verification,
)
from pynetdicom.transport import ThreadedAssociationServer

from . import storage
from .config import settings
from .db import (
    STATE_FAILED,
    STATE_MOVING,
    Database,
)
from .quota import QuotaExceeded, get_quota

log = logging.getLogger("dicom")

# DICOM 状态码
STORE_SUCCESS = 0x0000
STORE_OUT_OF_RESOURCES = 0xA700      # 保存失败（资源/磁盘类错误）
STORE_CANNOT_UNDERSTAND = 0xC000     # 无法理解 / 拒收
STORE_DATASET_ERROR = 0xA900         # 数据集不符合要求

# 只接受未压缩 little-endian 的 CT 存储上下文
UNCOMPRESSED_TS = [str(ImplicitVRLittleEndian), str(ExplicitVRLittleEndian)]


# ============================================================ C-STORE SCP
def _store_status(code: int, message: str = "") -> Dataset:
    ds = Dataset()
    ds.Status = code
    if message:
        ds.ErrorComment = message[:64]
    return ds


def handle_c_store(event: evt.Event, db: Database) -> Dataset:
    """C-STORE 请求处理器（在 pynetdicom 的关联线程内执行）。"""
    ds: Dataset = event.dataset
    ts = str(event.file_meta.TransferSyntaxUID)
    study_uid = str(getattr(ds, "StudyInstanceUID", "")).strip()
    sop_uid = str(getattr(ds, "SOPInstanceUID", "")).strip()

    if not study_uid or not sop_uid:
        log.warning("C-STORE 缺少 Study/SOP UID，拒收")
        return _store_status(STORE_DATASET_ERROR, "missing UIDs")

    batch = db.find_batch_for_study(study_uid)
    if batch is None:
        db.add_event(
            "STORE_REJECTED",
            f"无对应批的回传（Study={study_uid}, SOP={sop_uid}），清单外实例不入包",
        )
        log.warning("C-STORE 找不到 Study=%s 的批，拒收", study_uid)
        return _store_status(STORE_CANNOT_UNDERSTAND, "unknown study")

    batch_id = batch["id"]

    # 终态批（尤其 FAILED）的迟到回传一律拒绝，且不允许“复活”
    if batch["state"] == STATE_FAILED:
        db.add_event(
            "STORE_REJECTED",
            f"批已失败，迟到回传拒收（SOP={sop_uid}），失败不可恢复",
            batch_id,
        )
        log.info("批 %s 已失败，迟到实例 %s 拒收", batch_id, sop_uid)
        return _store_status(STORE_CANNOT_UNDERSTAND, "batch already failed")
    if batch["state"] != STATE_MOVING:
        db.add_event(
            "STORE_REJECTED",
            f"批不在回传阶段（state={batch['state']}），SOP={sop_uid} 拒收",
            batch_id,
        )
        return _store_status(STORE_CANNOT_UNDERSTAND, "batch not moving")

    # 只收非压缩 CT
    try:
        storage.validate_dataset(ds, ts)
    except storage.RejectedError as exc:
        db.add_event("STORE_REJECTED", str(exc), batch_id)
        log.warning("批 %s 拒收实例 %s：%s", batch_id, sop_uid, exc)
        return _store_status(STORE_DATASET_ERROR, str(exc))

    # 清单外实例不入包
    if not db.is_manifest_uid(batch_id, sop_uid):
        db.add_event(
            "STORE_REJECTED",
            f"清单外实例拒收（SOP={sop_uid}），不入包不计数",
            batch_id,
        )
        log.warning("批 %s 收到清单外实例 %s，拒收", batch_id, sop_uid)
        return _store_status(STORE_CANNOT_UNDERSTAND, "not in manifest")

    # 重复实例：不重复计数、不重复写盘、不再占额，直接回成功（源端重发是合法重试）
    if db.is_already_received(batch_id, sop_uid):
        db.add_event(
            "STORE_DUPLICATE",
            f"重复实例 SOP={sop_uid}，不重复计数不占额度",
            batch_id,
        )
        log.info("批 %s 重复实例 %s，忽略", batch_id, sop_uid)
        return _store_status(STORE_SUCCESS)

    # ---- 先按实例实际大小编码，再预留数据卷额度 ----
    try:
        payload = storage.encode_dataset(ds, ts)
    except Exception as exc:  # 编码失败：无文件无预留，直接判失败
        db.fail_batch(batch_id, f"实例编码失败：{exc}")
        log.exception("批 %s 实例 %s 编码失败", batch_id, sop_uid)
        return _store_status(STORE_DATASET_ERROR, "encode failed")
    instance_size = len(payload)

    quota = get_quota()
    try:
        token = quota.reserve(batch_id, instance_size)
    except QuotaExceeded as exc:
        # 仅让当前批失败并说明缺口；不触碰其它批已占/预留额度
        reason = (
            f"数据卷额度不足，批失败：缺口 {exc.gap} 字节"
            f"（本实例 {exc.size} 字节，配额 {exc.limit} 字节，"
            f"已占 {exc.used} 字节）"
        )
        db.fail_batch(batch_id, reason)
        db.add_event("QUOTA_EXCEEDED", reason, batch_id)
        log.warning("批 %s 额度不足：%s", batch_id, exc)
        # 0xA700 Out-of-resources 语义贴合“资源/容量不足”
        return _store_status(STORE_OUT_OF_RESOURCES, "quota exceeded")

    # ---- 关键顺序：先写盘（含 fsync），再登记；任一步失败只释放本次预留 ----
    try:
        path = storage.write_dataset(batch_id, ds, ts, payload=payload)
    except storage.StorageError as exc:
        quota.release(batch_id, token, instance_size)
        db.fail_batch(batch_id, f"保存失败：{exc}")
        log.error("批 %s 保存实例 %s 失败，批置 FAILED：%s", batch_id, sop_uid, exc)
        return _store_status(STORE_OUT_OF_RESOURCES, str(exc))

    try:
        registered = db.register_instance(
            batch_id, sop_uid, str(path), ts, size_bytes=instance_size
        )
    except Exception as exc:  # 登记本身故障：删除文件、释放本次预留，回失败置批失败
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        quota.release(batch_id, token, instance_size)
        db.fail_batch(batch_id, f"登记失败：{exc}")
        log.exception("批 %s 登记实例 %s 失败", batch_id, sop_uid)
        return _store_status(STORE_OUT_OF_RESOURCES, "registration failed")

    if not registered:
        # 并发重发竞态兜底：另一线程已登记，删除本次文件、释放本次预留，按重复处理
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        quota.release(batch_id, token, instance_size)
        db.add_event("STORE_DUPLICATE", f"并发重复 SOP={sop_uid}，不重复计数不占额度",
                     batch_id)
        return _store_status(STORE_SUCCESS)

    # 写盘与登记均成功：在途预留转为已存占用
    quota.commit(batch_id, token, instance_size)

    expected, received = db.received_progress(batch_id)
    db.add_event(
        "STORE_RECEIVED",
        f"已接收 SOP={sop_uid}（{received}/{expected}，{instance_size} 字节）",
        batch_id,
    )
    log.info("批 %s 接收实例 %s（%d/%d，%d 字节）",
             batch_id, sop_uid, received, expected, instance_size)
    return _store_status(STORE_SUCCESS)


def _build_scp_ae() -> AE:
    ae = AE(ae_title=settings.APP_AET)
    ae.acse_timeout = settings.ACSE_TIMEOUT
    ae.dimse_timeout = settings.DIMSE_TIMEOUT
    ae.network_timeout = settings.NETWORK_TIMEOUT
    # 仅公布未压缩 CT 的存储上下文 + C-ECHO；压缩/其他模态在协商阶段即被拒
    ae.supported_contexts = [
        build_context(str(CTImageStorage), UNCOMPRESSED_TS),
        build_context(str(Verification)),
    ]
    return ae


def start_store_scp(db: Database) -> ThreadedAssociationServer:
    ae = _build_scp_ae()
    handlers = [(evt.EVT_C_STORE, lambda event: handle_c_store(event, db))]
    server = ae.start_server(
        (settings.APP_HOST, settings.STORE_PORT),
        block=False,
        evt_handlers=handlers,
    )
    log.info(
        "C-STORE SCP 已启动：AE=%s 监听 %s:%d",
        settings.APP_AET, settings.APP_HOST, settings.STORE_PORT,
    )
    return server


# ============================================================ C-FIND SCU
class FindError(Exception):
    """C-FIND 阶段失败。"""


def _scp_ae(timeout: int | None = None) -> AE:
    ae = AE(ae_title=settings.APP_AET)
    ae.acse_timeout = settings.ACSE_TIMEOUT
    ae.dimse_timeout = settings.DIMSE_TIMEOUT
    ae.network_timeout = settings.NETWORK_TIMEOUT
    return ae


def c_find_study(study_uid: str) -> list[tuple[str, str, str]]:
    """IMAGE 级 C-FIND，返回 ``(SOPInstanceUID, SOPClassUID, TransferSyntax)``。

    只保留非压缩 CT 实例；要求源端返回 QueryRetrieveLevel=IMAGE 的标识符。
    """
    ae = _scp_ae()
    ae.requested_contexts = [
        build_context(str(StudyRootQueryRetrieveInformationModelFind),
                      [str(ExplicitVRLittleEndian), str(ImplicitVRLittleEndian)])
    ]
    assoc = ae.associate(settings.PACS_HOST, settings.PACS_PORT,
                         ae_title=settings.PACS_AET)
    if not assoc.is_established:
        raise FindError(f"无法与 PACS {settings.PACS_AET}@{settings.PACS_HOST}:"
                        f"{settings.PACS_PORT} 建立关联")
    try:
        identifier = Dataset()
        identifier.QueryRetrieveLevel = "IMAGE"
        identifier.StudyInstanceUID = study_uid
        identifier.SOPInstanceUID = ""
        identifier.SOPClassUID = ""
        identifier.TransferSyntaxUID = ""
        identifier.Modality = ""
        identifier.Rows = ""
        identifier.Columns = ""

        results: list[tuple[str, str, str]] = []
        failure_status: int | None = None
        for status_ds, rsp_identifier in assoc.send_c_find(
            identifier, str(StudyRootQueryRetrieveInformationModelFind)
        ):
            if rsp_identifier is None:
                # 最终状态
                if status_ds.Status != 0x0000:
                    failure_status = int(status_ds.Status)
                continue
            if str(getattr(rsp_identifier, "Modality", "")) != "CT":
                continue
            sop_class = str(getattr(rsp_identifier, "SOPClassUID", ""))
            if sop_class != str(CTImageStorage):
                continue
            ts = str(getattr(rsp_identifier, "TransferSyntaxUID", ""))
            if ts not in [str(ImplicitVRLittleEndian), str(ExplicitVRLittleEndian)]:
                continue
            sop_uid = str(rsp_identifier.SOPInstanceUID)
            results.append((sop_uid, sop_class, ts))

        if failure_status is not None:
            raise FindError(f"C-FIND 返回失败状态 0x{failure_status:04X}")
        if not results:
            raise FindError("C-FIND 未找到任何非压缩 CT 实例")
        # 源端批内不增删实例；去重保护
        deduped = {sop: (cls, ts) for sop, cls, ts in results}
        return [(sop, cls, ts) for sop, (cls, ts) in sorted(deduped.items())]
    finally:
        if assoc.is_alive:
            assoc.release()


# ============================================================ C-MOVE SCU
class MoveError(Exception):
    """C-MOVE 阶段失败（关联/最终状态失败/警告）。"""


def c_move_study(study_uid: str) -> dict[str, Any]:
    """STUDY 级 C-MOVE，目标为本机接收 AE。返回状态汇总。"""
    ae = _scp_ae()
    ae.requested_contexts = [
        build_context(str(StudyRootQueryRetrieveInformationModelMove),
                      [str(ExplicitVRLittleEndian), str(ImplicitVRLittleEndian)])
    ]
    assoc = ae.associate(settings.PACS_HOST, settings.PACS_PORT,
                         ae_title=settings.PACS_AET)
    if not assoc.is_established:
        raise MoveError(f"无法与 PACS 建立 C-MOVE 关联")
    try:
        identifier = Dataset()
        identifier.QueryRetrieveLevel = "STUDY"
        identifier.StudyInstanceUID = study_uid

        completed = failed = warning = 0
        final_status: int | None = None
        for status_ds, _ in assoc.send_c_move(
            identifier, settings.APP_AET,
            str(StudyRootQueryRetrieveInformationModelMove),
        ):
            if status_ds is None:
                continue
            final_status = int(status_ds.Status)
            if final_status == 0xFF00:  # Pending
                completed = int(getattr(status_ds, "NumberOfCompletedSuboperations", completed) or 0)
                failed = int(getattr(status_ds, "NumberOfFailedSuboperations", failed) or 0)
                warning = int(getattr(status_ds, "NumberOfWarningSuboperations", warning) or 0)

        if final_status is None:
            raise MoveError("C-MOVE 未返回最终状态（连接中断）")
        if final_status != 0x0000:
            raise MoveError(
                f"C-MOVE 最终状态 0x{final_status:04X}，"
                f"completed={completed} failed={failed} warning={warning}"
            )
        if failed or warning:
            raise MoveError(
                f"C-MOVE 报告 failed={failed} warning={warning}，按失败处理"
            )
        return {"completed": completed, "failed": failed, "warning": warning}
    finally:
        if assoc.is_alive:
            assoc.release()
