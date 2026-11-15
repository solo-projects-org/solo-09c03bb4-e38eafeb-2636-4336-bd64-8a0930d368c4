"""FastAPI 入口：批管理 REST API + Swagger 文档 + DICOM 服务生命周期。"""
from __future__ import annotations

import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import dicom, storage
from .config import settings
from .db import (
    STATE_COMPLETED,
    Database,
    set_db,
)
from .pipeline import Pipeline, recover_on_startup
from .quota import QuotaManager, set_quota

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("api")

# 进程级单例（lifespan 中初始化）
db: Database | None = None
pipeline: Pipeline | None = None
quota: QuotaManager | None = None
_scp_server = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, pipeline, quota, _scp_server
    settings.ensure_dirs()
    db = Database(settings.DB_PATH)
    set_db(db)
    quota = QuotaManager(settings.QUOTA_LIMIT_BYTES)
    set_quota(quota)
    # 先按卷中实际文件重建配额、清理未登记残件，再恢复批状态
    recover_on_startup(db, quota)
    _scp_server = dicom.start_store_scp(db)
    pipeline = Pipeline(db)
    log.info(
        "API 就绪，数据卷=%s，字节配额=%s",
        settings.DATA_DIR,
        "不限额" if quota.unlimited else f"{quota.limit_bytes} 字节",
    )
    try:
        yield
    finally:
        if _scp_server is not None:
            _scp_server.shutdown()
        if db is not None:
            db.close()


app = FastAPI(
    title="DICOM CT 离线取数流水线",
    version="1.1.0",
    lifespan=lifespan,
    description=(
        "提交 StudyInstanceUID → C-FIND 固定 SOP 清单 → C-MOVE 回传 → "
        "C-STORE 接收非压缩 CT → 核对收齐后打包 ZIP 下载。\n\n"
        "规则：清单外实例不入包、重复实例不重复计数不占额度、"
        "C-STORE 写盘前按实例实际大小预留数据卷字节配额（所有批共享）、"
        "额度不足仅当前批失败并说明缺口、保存/登记失败只释放本次预留、"
        "失败批迟到回传不可恢复、重启后按卷内实际文件恢复配额并清理残件。"
    ),
)


# ------------------------------------------------------------------ schemas
class BatchCreate(BaseModel):
    study_instance_uid: str = Field(
        ..., description="源端检查的 StudyInstanceUID",
        examples=["1.2.840.10008.1.2.99.1"],
    )
    fault_inject: str = Field(
        default="",
        description="可选故障注入：save_failure=保存失败演示；留空为正常流程",
    )


class BatchView(BaseModel):
    id: str
    study_instance_uid: str
    state: str
    expected_count: int
    received_count: int
    failure_reason: str | None
    zip_path: str | None
    fault_inject: str
    created_at: str
    updated_at: str
    downloadable: bool
    stored_bytes: int = Field(
        0, description="该批已写盘登记实例占用的字节数"
    )
    reserved_bytes: int = Field(
        0, description="该批在途预留（已预留尚未登记完成）的字节数"
    )
    quota_total_bytes: int = Field(
        0, description="该批当前合计占用（已存 + 在途预留）字节数"
    )


def _view(row: dict) -> BatchView:
    snap = quota.snapshot() if quota is not None else None
    bq = snap.batches.get(row["id"], {}) if snap else {}
    stored = int(bq.get("stored_bytes", 0))
    reserved = int(bq.get("reserved_bytes", 0))
    return BatchView(
        id=row["id"],
        study_instance_uid=row["study_instance_uid"],
        state=row["state"],
        expected_count=row["expected_count"],
        received_count=row["received_count"],
        failure_reason=row["failure_reason"],
        zip_path=row["zip_path"],
        fault_inject=row["fault_inject"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        downloadable=row["state"] == STATE_COMPLETED and bool(row["zip_path"]),
        stored_bytes=stored,
        reserved_bytes=reserved,
        quota_total_bytes=stored + reserved,
    )


class QuotaView(BaseModel):
    unlimited: bool = Field(..., description="true 表示未配置上限（QUOTA_LIMIT_BYTES=0）")
    limit_bytes: int = Field(..., description="配额上限字节数；0 表示不限额")
    stored_bytes: int = Field(..., description="所有批已存实例文件字节合计")
    reserved_bytes: int = Field(..., description="所有批在途预留字节合计")
    used_bytes: int = Field(..., description="已存 + 在途预留合计")
    available_bytes: int | None = Field(
        None, description="剩余可用字节；不限额时为 null"
    )
    batches: dict[str, dict] = Field(
        default_factory=dict,
        description="每个批的占用明细：stored_bytes/reserved_bytes/total_bytes",
    )


# ------------------------------------------------------------------ routes
@app.post("/batches", response_model=BatchView, status_code=201,
          tags=["batches"], summary="提交检查，启动取数批")
def create_batch(body: BatchCreate) -> BatchView:
    if body.fault_inject not in ("", storage.FAULT_SAVE):
        raise HTTPException(400, "fault_inject 只支持 '' 或 'save_failure'")
    uid = body.study_instance_uid.strip()
    if not uid:
        raise HTTPException(400, "study_instance_uid 不能为空")

    if db.has_active_batch_for_study(uid):
        raise HTTPException(409, "该 Study 已有进行中的批，请等待其结束")

    batch_id = uuid.uuid4().hex
    db.create_batch(batch_id, uid, body.fault_inject)
    pipeline.submit(batch_id)
    return _view(db.get_batch(batch_id))


@app.get("/batches", response_model=list[BatchView],
         tags=["batches"], summary="列出全部批")
def list_batches() -> list[BatchView]:
    return [_view(r) for r in db.list_batches()]


@app.get("/batches/{batch_id}", response_model=BatchView,
         tags=["batches"], summary="查询批状态与进度")
def get_batch(batch_id: str) -> BatchView:
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    return _view(row)


@app.get("/batches/{batch_id}/events",
         tags=["batches"], summary="查询批事件流水（接收/重复/拒收/状态迁移）")
def get_events(batch_id: str) -> list[dict]:
    if db.get_batch(batch_id) is None:
        raise HTTPException(404, "批不存在")
    return db.list_events(batch_id)


@app.get("/batches/{batch_id}/instances",
         tags=["batches"], summary="查询批清单及每个 SOP 的接收情况")
def get_instances(batch_id: str) -> dict:
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    files = db.received_files(batch_id)
    received = {sop: path for sop, path in files}
    return {
        "batch_id": batch_id,
        "state": row["state"],
        "manifest": sorted(db.manifest_uids(batch_id)),
        "received": sorted(received),
        "missing": db.missing_uids(batch_id),
    }


@app.get("/batches/{batch_id}/download",
         tags=["batches"], summary="下载 ZIP（仅 COMPLETED 批可下载）")
def download_zip(batch_id: str) -> FileResponse:
    row = db.get_batch(batch_id)
    if row is None:
        raise HTTPException(404, "批不存在")
    if row["state"] != STATE_COMPLETED or not row["zip_path"]:
        raise HTTPException(
            409,
            f"批状态 {row['state']} 不可下载；仅 C-MOVE 成功且清单收齐的批可下载",
        )
    path = Path(row["zip_path"])
    if not path.exists():
        raise HTTPException(410, "ZIP 文件缺失（数据卷可能已损坏）")
    return FileResponse(
        path, media_type="application/zip",
        filename=f"{batch_id}.zip",
    )


@app.get("/quota", response_model=QuotaView, tags=["quota"],
         summary="查询数据卷字节配额与各批占用（已存 + 在途预留）")
def get_quota_view() -> QuotaView:
    snap = quota.snapshot()
    return QuotaView(
        unlimited=snap.unlimited,
        limit_bytes=snap.limit_bytes,
        stored_bytes=snap.stored_bytes,
        reserved_bytes=snap.reserved_bytes,
        used_bytes=snap.used_bytes,
        available_bytes=snap.available_bytes,
        batches=snap.batches,
    )


@app.get("/health", tags=["system"], summary="健康检查")
def health() -> dict:
    snap = quota.snapshot()
    return {"status": "ok", "store_port": settings.STORE_PORT,
            "ae_title": settings.APP_AET,
            "quota_limit_bytes": snap.limit_bytes,
            "quota_used_bytes": snap.used_bytes}


# ------------------------------------------------------------------ debug
@app.get("/internal/pacs-studies", tags=["internal"],
         summary="（演示用）STUDY 级 C-FIND 列出模拟 PACS 中的检查")
def pacs_studies() -> list[dict]:
    from pydicom.dataset import Dataset
    from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian
    from pynetdicom import AE
    from pynetdicom.presentation import build_context
    from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind

    ae = AE(ae_title=settings.APP_AET)
    ae.requested_contexts = [build_context(
        str(StudyRootQueryRetrieveInformationModelFind),
        [str(ExplicitVRLittleEndian), str(ImplicitVRLittleEndian)])]
    assoc = ae.associate(settings.PACS_HOST, settings.PACS_PORT,
                         ae_title=settings.PACS_AET)
    if not assoc.is_established:
        raise HTTPException(502, "无法连接模拟 PACS")
    try:
        q = Dataset()
        q.QueryRetrieveLevel = "STUDY"
        q.StudyInstanceUID = ""
        q.PatientID = ""
        q.PatientName = ""
        q.Modality = ""
        q.NumberOfStudyRelatedInstances = ""
        out = []
        for status, ident in assoc.send_c_find(
            q, str(StudyRootQueryRetrieveInformationModelFind)
        ):
            if ident is None:
                continue
            out.append({
                "study_instance_uid": str(ident.StudyInstanceUID),
                "patient_id": str(getattr(ident, "PatientID", "")),
                "patient_name": str(getattr(ident, "PatientName", "")),
                "modality": str(getattr(ident, "Modality", "")),
                "instances": int(getattr(ident, "NumberOfStudyRelatedInstances", 0) or 0),
            })
        return out
    finally:
        if assoc.is_alive:
            assoc.release()


@app.post("/internal/suicide", tags=["internal"],
          summary="（演示用）退出进程以演示重启恢复；compose 自动重启")
def suicide(tasks: BackgroundTasks) -> dict:
    def _die() -> None:
        os._exit(1)
    tasks.add_task(_die)
    return {"detail": "进程即将退出，等待 Docker 重启"}
