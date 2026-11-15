"""数据卷字节配额管理。

配额模型
========
数据卷内所有 DICOM 实例文件共享一个可配置的总配额
（``QUOTA_LIMIT_BYTES``，0 表示不限制）。额度由两部分组成，任何时刻合计
不得超过上限：

* **已存（stored）**：已经写盘并登记成功的实例，按文件实际字节数计入。
  进程重启后这部分额度不依赖内存，而是按数据卷中实际保留的文件重建。
* **在途预留（reserved）**：C-STORE 处理线程在写盘前，按该实例编码后的
  实际大小预占的额度；写盘+登记成功后预留转为已存，任何一步失败则只
  释放本次预留，不影响其它批已经占用的额度。

关键语义
========
* 线程安全：所有 check-then-write 都在同一把锁内完成，多个批的 C-STORE
  关联线程并发到达时不会超额。
* 合法重投（重复实例）在进入配额逻辑之前就被识别，不占额度。
* 额度不足抛 :class:`QuotaExceeded`，由调用方只把当前批判失败并记录缺口；
  其它批的已存/预留额度完全不动。
* 内存中的在途预留随进程消失；重启恢复时按卷内实际文件重建已存额度，
  并清理未登记的残件，因此重启后不会有“幽灵预留”。
"""
from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field

from .config import settings


class QuotaExceeded(Exception):
    """额度不足。

    :param size: 本次申请的字节数
    :param limit: 配额上限
    :param used: 申请前已占用（已存 + 在途）字节数
    """

    def __init__(self, size: int, limit: int, used: int) -> None:
        self.size = size
        self.limit = limit
        self.used = used
        self.gap = used + size - limit
        super().__init__(
            f"数据卷额度不足：申请 {size} 字节，配额上限 {limit} 字节，"
            f"当前已占 {used} 字节，缺口 {self.gap} 字节"
        )


@dataclass
class BatchQuota:
    """单个批的额度占用（已存 + 在途预留）。"""
    stored: int = 0
    reserved: int = 0

    @property
    def total(self) -> int:
        return self.stored + self.reserved


@dataclass
class QuotaSnapshot:
    limit_bytes: int
    stored_bytes: int
    reserved_bytes: int
    used_bytes: int
    available_bytes: int | None          # None 表示不限额
    unlimited: bool
    batches: dict[str, dict] = field(default_factory=dict)


class QuotaManager:
    def __init__(self, limit_bytes: int | None = None) -> None:
        self.limit_bytes: int = (
            settings.QUOTA_LIMIT_BYTES if limit_bytes is None else limit_bytes
        )
        self._lock = threading.RLock()
        self._batches: dict[str, BatchQuota] = {}
        self._ids = itertools.count(1)

    # ------------------------------------------------------------- properties
    @property
    def unlimited(self) -> bool:
        return self.limit_bytes <= 0

    def _entry(self, batch_id: str) -> BatchQuota:
        return self._batches.setdefault(batch_id, BatchQuota())

    # ------------------------------------------------------------- reservations
    def reserve(self, batch_id: str, size: int) -> str:
        """写盘前按实例实际大小预留额度。

        成功返回预留句柄（用于 :meth:`commit` / :meth:`release`）；
        额度不足抛 :class:`QuotaExceeded`，且不改变任何占用。
        """
        if size < 0:
            raise ValueError("预留大小不能为负")
        if self.unlimited:
            # 不限额也维护每批预留数，保证释放/提交配对可校验
            with self._lock:
                token = f"r{next(self._ids)}"
                entry = self._entry(batch_id)
                entry.reserved += size
                return token

        with self._lock:
            used = self._used_locked()
            if used + size > self.limit_bytes:
                raise QuotaExceeded(size, self.limit_bytes, used)
            token = f"r{next(self._ids)}"
            entry = self._entry(batch_id)
            entry.reserved += size
            return token

    def release(self, batch_id: str, token: str, size: int) -> None:
        """写盘或登记失败：只释放本次预留，绝不动其它批的已存额度。"""
        with self._lock:
            entry = self._entry(batch_id)
            entry.reserved = max(0, entry.reserved - size)

    def commit(self, batch_id: str, token: str, size: int) -> None:
        """写盘且登记成功：在途预留转为已存占用。"""
        with self._lock:
            entry = self._entry(batch_id)
            entry.reserved = max(0, entry.reserved - size)
            entry.stored += size

    # ------------------------------------------------------------- recovery
    def reset_stored(self, per_batch: dict[str, int]) -> None:
        """重启恢复：按卷中实际保留的文件重建已存额度。

        在途预留不跨进程，重建时全部归零；``per_batch`` 为
        ``batch_id -> 实际文件字节合计``。
        """
        with self._lock:
            self._batches = {
                bid: BatchQuota(stored=size, reserved=0)
                for bid, size in per_batch.items()
            }

    # ------------------------------------------------------------- reporting
    def _used_locked(self) -> int:
        return sum(b.total for b in self._batches.values())

    def usage(self) -> tuple[int, int]:
        """返回 ``(已存字节, 在途预留字节)``。"""
        with self._lock:
            stored = sum(b.stored for b in self._batches.values())
            reserved = sum(b.reserved for b in self._batches.values())
        return stored, reserved

    def snapshot(self) -> QuotaSnapshot:
        with self._lock:
            stored, reserved = self.usage()
            batches = {
                bid: {"stored_bytes": b.stored, "reserved_bytes": b.reserved,
                      "total_bytes": b.total}
                for bid, b in sorted(self._batches.items())
                if b.total > 0
            }
        used = stored + reserved
        available = None if self.unlimited else max(0, self.limit_bytes - used)
        return QuotaSnapshot(
            limit_bytes=self.limit_bytes,
            stored_bytes=stored,
            reserved_bytes=reserved,
            used_bytes=used,
            available_bytes=available,
            unlimited=self.unlimited,
            batches=batches,
        )


# 进程级单例（lifespan 中初始化），供 dicom 层使用
_quota: QuotaManager | None = None


def set_quota(manager: QuotaManager) -> None:
    global _quota
    _quota = manager


def get_quota() -> QuotaManager:
    if _quota is None:
        raise RuntimeError("配额管理器尚未初始化")
    return _quota
