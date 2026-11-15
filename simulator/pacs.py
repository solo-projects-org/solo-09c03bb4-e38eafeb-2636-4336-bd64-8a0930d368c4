"""真实 DICOM 网络行为的模拟 PACS（pynetdicom SCP）。

能力
====
* C-ECHO 验证
* STUDY / IMAGE 级 C-FIND（Study Root）
* STUDY 级 C-MOVE：作为 SCU 反向关联接收端并 C-STORE 推流
* 可配置回传节奏（``MOVE_DELAY`` 秒/实例），用于演示多批交错、慢回传不阻塞
* 可配置重复投送（``MOVE_DUPLICATE_EVERY``）：同实例合法重发，
  供接收端验证“重复实例不重复计数”

数据来自 ``simulator/generate_data.py`` 生成的虚构合成 CT 目录。
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pydicom
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s sim-pacs | %(message)s",
)
log = logging.getLogger("sim-pacs")

DATA_ROOT = Path(os.getenv("PACS_DATA_DIR", "/data/pacs"))
PACS_AET = os.getenv("PACS_AET", "MYPACS")
PACS_PORT = int(os.getenv("PACS_PORT", "11112"))
# 已知的 C-MOVE 目标 AE -> (host, port)
DEST_AET = os.getenv("APP_AET", "RETRIEVER")
DEST_HOST = os.getenv("DEST_HOST", "api")
DEST_PORT = int(os.getenv("DEST_PORT", "11113"))
# 每个实例发送前停顿秒数，模拟慢回传（可被演示脚本按 Study 覆盖）
MOVE_DELAY = float(os.getenv("MOVE_DELAY", "0.5"))
# 每 N 个实例重复发送一次（0 表示不重复）
MOVE_DUPLICATE_EVERY = int(os.getenv("MOVE_DUPLICATE_EVERY", "0"))

UNCOMPRESSED_TS = [str(ImplicitVRLittleEndian), str(ExplicitVRLittleEndian)]


# --------------------------------------------------------------- registry
class Registry:
    """扫描数据目录建立内存索引。"""

    def __init__(self, root: Path) -> None:
        self.studies: dict[str, dict] = {}
        self._scan(root)

    def _scan(self, root: Path) -> None:
        if not root.exists():
            raise SystemExit(f"PACS 数据目录不存在：{root}")
        for study_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            files = sorted(study_dir.glob("*.dcm"))
            if not files:
                continue
            probe = pydicom.dcmread(str(files[0]), stop_before_pixels=True)
            study_uid = str(probe.StudyInstanceUID)
            instances = []
            for f in files:
                meta = pydicom.dcmread(str(f), stop_before_pixels=True)
                instances.append({
                    "sop_uid": str(meta.SOPInstanceUID),
                    "sop_class": str(meta.SOPClassUID),
                    "transfer_syntax": str(
                        meta.file_meta.TransferSyntaxUID
                    ),
                    "path": f,
                })
            self.studies[study_uid] = {
                "study_uid": study_uid,
                "series_uid": str(probe.SeriesInstanceUID),
                "patient_id": str(getattr(probe, "PatientID", "")),
                "patient_name": str(getattr(probe, "PatientName", "")),
                "modality": str(getattr(probe, "Modality", "CT")),
                "instances": instances,
            }
            log.info("索引检查 %s：%d 个实例", study_uid, len(instances))

    def find_study(self, study_uid: str) -> dict | None:
        return self.studies.get(study_uid)


REGISTRY: Registry | None = None
# 演示脚本可按 Study 动态设置慢回传秒数 / 重复开关
RUNTIME_OVERRIDES: dict[str, dict] = {}


# --------------------------------------------------------------- handlers
def handle_echo(event: evt.Event) -> int:
    return 0x0000


def handle_find(event: evt.Event):
    """Study Root C-FIND，支持 STUDY 与 IMAGE 两级。"""
    ident: Dataset = event.identifier
    level = str(getattr(ident, "QueryRetrieveLevel", "")).upper()
    want_study = str(getattr(ident, "StudyInstanceUID", "") or "")
    log.info("C-FIND level=%s study=%s", level, want_study or "*")

    candidates = (
        [REGISTRY.find_study(want_study)] if want_study
        else list(REGISTRY.studies.values())
    )
    for study in candidates:
        if study is None:
            continue
        if event.is_cancelled:
            yield 0xFE00, None
            return

        if level == "STUDY":
            rsp = Dataset()
            rsp.QueryRetrieveLevel = "STUDY"
            rsp.StudyInstanceUID = study["study_uid"]
            rsp.PatientID = study["patient_id"]
            rsp.PatientName = study["patient_name"]
            rsp.Modality = study["modality"]
            rsp.NumberOfStudyRelatedInstances = len(study["instances"])
            rsp.NumberOfStudyRelatedSeries = 1
            rsp.RetrieveAETitle = PACS_AET
            yield 0xFF00, rsp
            continue

        if level == "IMAGE":
            for inst in study["instances"]:
                if event.is_cancelled:
                    yield 0xFE00, None
                    return
                rsp = Dataset()
                rsp.QueryRetrieveLevel = "IMAGE"
                rsp.StudyInstanceUID = study["study_uid"]
                rsp.SeriesInstanceUID = study["series_uid"]
                rsp.SOPInstanceUID = inst["sop_uid"]
                rsp.SOPClassUID = inst["sop_class"]
                rsp.TransferSyntaxUID = inst["transfer_syntax"]
                rsp.Modality = "CT"
                rsp.Rows = 64
                rsp.Columns = 64
                rsp.RetrieveAETitle = PACS_AET
                yield 0xFF00, rsp
    # 生成器正常结束 => pynetdicom 自动回 0x0000 Success


def handle_move(event: evt.Event):
    """Study Root C-MOVE：反连目标 AE 后逐实例 C-STORE。"""
    ident: Dataset = event.identifier
    study_uid = str(getattr(ident, "StudyInstanceUID", "") or "")
    study = REGISTRY.find_study(study_uid)
    log.info("C-MOVE study=%s destination=%s", study_uid, event.move_destination)

    if event.move_destination != DEST_AET:
        log.warning("未知 Move Destination：%s", event.move_destination)
        yield None, None
        return
    if study is None:
        log.warning("C-MOVE 找不到检查 %s", study_uid)
        yield 0xA900, None
        return

    # 第一步：告知目标地址及可用于存储子操作的上下文
    contexts = [
        build_context(str(CTImageStorage), UNCOMPRESSED_TS),
    ]
    yield DEST_HOST, DEST_PORT, {"contexts": contexts}

    instances = list(study["instances"])
    # 第二步：子操作总数
    yield len(instances)

    overrides = RUNTIME_OVERRIDES.get(study_uid, {})
    delay = float(overrides.get("delay", MOVE_DELAY))
    dup_every = int(overrides.get("duplicate_every", MOVE_DUPLICATE_EVERY))

    # 重复投送计划：这些 SOP 在正常 C-MOVE 子操作之外，由源端“重试”再发一次。
    # 不能作为额外 Pending yield——框架按子操作总数计数，超出部分会被丢弃。
    duplicate_uids: set[str] = set()
    if dup_every:
        duplicate_uids = {
            inst["sop_uid"]
            for i, inst in enumerate(instances, start=1)
            if i % dup_every == 0
        }

    # 第三步：逐实例 yield 数据集，pynetdicom 自动建立反向关联并执行
    # C-STORE 子操作，按 Store SCP 的应答累计 completed/failed。
    for idx, inst in enumerate(instances, start=1):
        if event.is_cancelled:
            yield 0xFE00, None
            return
        if delay:
            time.sleep(delay)
        log.info("C-MOVE 推流 %s", inst["sop_uid"])
        yield 0xFF00, pydicom.dcmread(str(inst["path"]))  # Pending + 数据集

        # 合法重发：另开一条 SCU 关联重试同一实例。
        # 接收端必须识别为重复：不重复计数、不重复写盘，仍回 SUCCESS。
        if inst["sop_uid"] in duplicate_uids:
            if delay:
                time.sleep(delay)
            _retry_store(inst["path"], log)
    # 生成器结束 => 框架按子操作累计结果回 0x0000 / 0xB000 / 0xA702


def _retry_store(path: Path, logger: logging.Logger) -> None:
    """模拟源端重试：新建关联把同一实例再 C-STORE 一次。"""
    ae = AE(ae_title=PACS_AET)
    ae.requested_contexts = [
        build_context(str(CTImageStorage), UNCOMPRESSED_TS),
    ]
    assoc = ae.associate(DEST_HOST, DEST_PORT, ae_title=DEST_AET)
    if not assoc.is_established:
        logger.warning("重复投送关联被拒：%s", path.name)
        return
    try:
        status = assoc.send_c_store(pydicom.dcmread(str(path)))
        logger.info(
            "重复投送 %s -> Store SCP 应答 0x%04X（接收端应去重）",
            path.name, int(status.Status),
        )
    finally:
        if assoc.is_alive:
            assoc.release()


def main() -> None:
    global REGISTRY
    REGISTRY = Registry(DATA_ROOT)

    ae = AE(ae_title=PACS_AET)
    ae.acse_timeout = 60
    ae.dimse_timeout = 600
    ae.network_timeout = 600
    ae.supported_contexts = [
        build_context(str(Verification)),
        build_context(str(StudyRootQueryRetrieveInformationModelFind),
                      UNCOMPRESSED_TS),
        build_context(str(StudyRootQueryRetrieveInformationModelMove),
                      UNCOMPRESSED_TS),
    ]
    handlers = [
        (evt.EVT_C_ECHO, handle_echo),
        (evt.EVT_C_FIND, handle_find),
        (evt.EVT_C_MOVE, handle_move),
    ]
    log.info(
        "模拟 PACS 启动 AE=%s 端口=%d，Move 目标 %s@%s:%d，延迟=%.2fs/实例",
        PACS_AET, PACS_PORT, DEST_AET, DEST_HOST, DEST_PORT, MOVE_DELAY,
    )
    ae.start_server(("", PACS_PORT), block=True, evt_handlers=handlers)


if __name__ == "__main__":
    main()
