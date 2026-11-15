"""文件落盘、DICOM 校验、故障注入与 ZIP 打包。"""
from __future__ import annotations

import io
import logging
import os
import zipfile
from pathlib import Path

import pydicom
from pydicom.uid import ImplicitVRLittleEndian, ExplicitVRLittleEndian
from pynetdicom.sop_class import CTImageStorage

from .config import settings

log = logging.getLogger("storage")

# 只接受未压缩（原始 little-endian）传输语法
UNCOMPRESSED_TS = {
    str(ImplicitVRLittleEndian),
    str(ExplicitVRLittleEndian),
}

# 故障注入模式
FAULT_SAVE = "save_failure"          # 本批首个实例落盘即失败


class StorageError(Exception):
    """保存阶段失败（C-STORE 必须回失败状态）。"""


class RejectedError(Exception):
    """实例被拒收（非 CT / 压缩 / 不属于任何批 / 清单外 / 已失败批）。

    拒收不改变批状态；save_failure 等真正的保存异常才会置批失败。
    """


def batch_dir(batch_id: str) -> Path:
    d = settings.BATCHES_DIR / batch_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_batch_readonly(batch_id: str) -> None:
    """保存故障演示：去掉批目录写权限，使后续所有写入抛 PermissionError。

    容器以非 root 用户运行，chmod 555 后 open(...,'wb') 必然失败，
    是真实的磁盘写入失败而非模拟返回值。
    """
    d = batch_dir(batch_id)
    os.chmod(d, 0o555)


def restore_batch_writable(batch_id: str) -> None:
    d = settings.BATCHES_DIR / batch_id
    if d.exists():
        os.chmod(d, 0o755)


def validate_dataset(ds: pydicom.Dataset, ts: str) -> None:
    """只收非压缩 CT；缺关键标识也拒收。"""
    sop_class = str(getattr(ds, "SOPClassUID", ""))
    if sop_class != str(CTImageStorage):
        raise RejectedError(f"非 CT 图像（SOPClassUID={sop_class}），拒收")
    if ts not in UNCOMPRESSED_TS:
        raise RejectedError(f"压缩传输语法 {ts}，仅收未压缩 CT，拒收")
    for tag in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID"):
        if not getattr(ds, tag, ""):
            raise RejectedError(f"缺少 {tag}，拒收")


def safe_filename(ds: pydicom.Dataset) -> str:
    series = str(ds.SeriesInstanceUID).replace(".", "_")
    sop = str(ds.SOPInstanceUID).replace(".", "_")
    return f"{series}__{sop}.dcm"


def encode_dataset(ds: pydicom.Dataset, ts: str) -> bytes:
    """按协商得到的传输语法把实例编码为 DICOM 文件字节串。

    配额按“实例实际大小”预留：该字节串即随后原子写盘的内容，
    故 ``len(payload)`` 与最终文件大小一致（pydicom 确定性编码）。
    """
    # pynetdicom 的 event.dataset 不带 file_meta，需显式带上协商得到的 TS
    if not hasattr(ds, "file_meta") or ds.file_meta is None:
        ds.file_meta = pydicom.dataset.FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = pydicom.uid.UID(ts)
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def write_dataset(
    batch_id: str, ds: pydicom.Dataset, ts: str, payload: bytes | None = None
) -> Path:
    """原子化落盘：先写临时文件并 fsync，再 rename，最后登记。

    ``payload`` 为 :func:`encode_dataset` 预先编码好的字节串（配额预留
    必须在写盘前完成，因此编码也前置）；不传时现场编码以保持兼容。

    任一步失败都清理残留文件并抛 :class:`StorageError`，由 C-STORE 处理器
    回失败状态并把批置为 FAILED。
    """
    if payload is None:
        payload = encode_dataset(ds, ts)
    d = batch_dir(batch_id)
    final = d / safe_filename(ds)
    tmp = d / (final.name + ".part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(payload)
            fh.flush()
            # 强制刷盘，避免“回复成功但数据未落盘”
            os.fsync(fh.fileno())
        os.replace(tmp, final)
        _fsync_dir(d)
        return final
    except (OSError, PermissionError) as exc:
        for p in (tmp, final):
            try:
                p.unlink()
            except OSError:
                pass
        raise StorageError(f"文件写入失败：{exc}") from exc


def _fsync_dir(d: Path) -> None:
    try:
        fd = os.open(str(d), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def build_zip(batch_id: str) -> Path:
    """把已登记实例打包为 ZIP；只打包清单内且已登记的文件。"""
    from .db import get_db  # 局部导入避免循环依赖

    db = get_db()
    zip_path = settings.ZIP_DIR / f"{batch_id}.zip"
    tmp_zip = settings.ZIP_DIR / f"{batch_id}.zip.part"
    files = db.received_files(batch_id)
    with zipfile.ZipFile(tmp_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        manifest_lines = ["sop_instance_uid,file_name"]
        used_names: set[str] = set()
        for sop_uid, file_path in files:
            src = Path(file_path)
            name = src.name
            # 极端重名兜底
            if name in used_names:
                name = f"{sop_uid.replace('.', '_')}.dcm"
            used_names.add(name)
            zf.write(src, arcname=f"dicom/{name}")
            manifest_lines.append(f"{sop_uid},{name}")
        zf.writestr("MANIFEST.csv", "\n".join(manifest_lines) + "\n")
    os.replace(tmp_zip, zip_path)
    _fsync_dir(settings.ZIP_DIR)
    return zip_path


# ============================================================ 重启恢复
def reconcile_volume(db, quota) -> dict[str, int]:
    """重启后按卷中实际保留的文件重建配额占用并清理未登记残件。

    处理规则：

    * 已登记（DB ``instances.file_path`` 非空）但文件已丢失：清空登记，
      重启恢复随后会把未完成批判失败；已完成批则在 ZIP 重建时暴露问题。
    * 已登记且文件存在：按文件**实际字节数**计入该批配额。
    * 未登记残件（``.part`` 临时文件、不在任何批登记中的 ``.dcm``、
      不属于任何批的孤儿目录）：一律删除，绝不计入配额。

    返回 ``batch_id -> 实际保留文件字节合计``，用于 :meth:`QuotaManager.reset_stored`。
    """
    # DB 中全部已登记文件：path -> (batch_id, sop_uid)
    registered: dict[str, tuple[str, str]] = {}
    known_batch_dirs: set[Path] = set()
    for batch in db.list_batches():
        bid = batch["id"]
        known_batch_dirs.add(settings.BATCHES_DIR / bid)
        for sop_uid, file_path in db.received_files(bid):
            registered[os.path.abspath(file_path)] = (bid, sop_uid)

    # 先清掉崩溃留下的 .part 临时文件（任何批目录下）
    for part in settings.BATCHES_DIR.rglob("*.part"):
        try:
            part.unlink()
            owner = _batch_id_of(part, known_batch_dirs)
            db.add_event(
                "RECOVERY_CLEANUP",
                f"删除未登记残件（临时文件）：{part.name}",
                owner,
            )
        except OSError:
            pass

    per_batch: dict[str, int] = {}
    seen_registered: set[str] = set()

    if settings.BATCHES_DIR.exists():
        for path in sorted(settings.BATCHES_DIR.rglob("*")):
            if not path.is_file() or path.suffix != ".dcm":
                continue
            key = os.path.abspath(str(path))
            reg = registered.get(key)
            if reg is None:
                # 卷里存在但 DB 未登记：未登记残件，删除且不计配额
                owner = _batch_id_of(path, known_batch_dirs)
                try:
                    size = path.stat().st_size
                    path.unlink()
                    db.add_event(
                        "RECOVERY_CLEANUP",
                        f"删除未登记残件：{path.name}（{size} 字节，不计配额）",
                        owner,
                    )
                except OSError:
                    pass
                continue

            bid, sop_uid = reg
            seen_registered.add(key)
            try:
                per_batch[bid] = per_batch.get(bid, 0) + path.stat().st_size
            except OSError:
                # stat 失败按文件丢失处理
                db.clear_instance_registration(bid, sop_uid)
                db.add_event(
                    "RECOVERY_CLEANUP",
                    f"已登记实例文件不可读，清空登记：{path.name}",
                    bid,
                )

    # 已登记但卷中文件已丢失：清空登记，使缺件核对能发现
    for key, (bid, sop_uid) in registered.items():
        if key not in seen_registered:
            db.clear_instance_registration(bid, sop_uid)
            db.add_event(
                "RECOVERY_CLEANUP",
                f"已登记实例文件缺失，清空登记：SOP={sop_uid}",
                bid,
            )

    # 孤儿批目录（DB 中无此批）：整个删除
    if settings.BATCHES_DIR.exists():
        for child in settings.BATCHES_DIR.iterdir():
            if child.is_dir() and child not in known_batch_dirs:
                _remove_tree(child)
                db.add_event(
                    "RECOVERY_CLEANUP",
                    f"删除无登记记录的孤儿批目录：{child.name}",
                )

    # 清理空批目录（失败批可能一个实例都没留下）
    for d in list(known_batch_dirs):
        if d.exists() and not any(d.iterdir()):
            try:
                d.rmdir()
            except OSError:
                pass

    quota.reset_stored(per_batch)
    stored_total = sum(per_batch.values())
    # 全局汇总事件（batch_id 为空）+ 每个批各自的重建事件（便于在批事件流查看）
    db.add_event(
        "RECOVERY_QUOTA_RESTORED",
        f"按卷中实际文件重建配额占用：{stored_total} 字节，"
        f"涉及 {len(per_batch)} 个批",
    )
    for bid, size in per_batch.items():
        db.add_event(
            "RECOVERY_QUOTA_RESTORED",
            f"本批按卷中实际文件恢复占用 {size} 字节",
            bid,
        )
    return per_batch


def _remove_tree(root: Path) -> None:
    for p in sorted(root.rglob("*"), reverse=True):
        try:
            if p.is_dir() and not p.is_symlink():
                p.rmdir()
            else:
                p.unlink()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass


def _batch_id_of(path: Path, known_batch_dirs: set[Path]) -> str | None:
    """根据文件路径判断它属于哪个批目录；不属于任何已知批则返回 None。"""
    for d in known_batch_dirs:
        try:
            path.resolve().relative_to(d.resolve())
        except (ValueError, OSError):
            continue
        return d.name
    return None
