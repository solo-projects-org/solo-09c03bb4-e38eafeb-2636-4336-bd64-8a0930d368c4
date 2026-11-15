"""演示用 C-STORE 投送器（rogue SCU）。

两种模式
========
* ``off-list``：构造一个 SOPInstanceUID 不在批清单内的合成 CT，
  直接 C-STORE 给接收端，验证“清单外实例不入包”，期望应答 0xC000。
* ``compressed``：构造声明 JPEG 压缩传输语法的 CT，验证“只收非压缩 CT”，
  期望在关联协商阶段被拒或收到失败应答。

用法（容器内）::

    python -m simulator.rogue_sender off-list  --study <UID> [--host api --port 11113]
    python -m simulator.rogue_sender compressed --study <UID>
"""
from __future__ import annotations

import argparse
import sys

from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    JPEGBaseline8Bit,
    UID,
)
from pynetdicom import AE
from pynetdicom.presentation import build_context

from .generate_data import _make_uid, build_ct_dataset


def _send(ds, transfer_syntaxes: list[str], host: str, port: int,
          calling_ae: str = "ROGUE", called_ae: str = "RETRIEVER") -> int:
    ae = AE(ae_title=calling_ae)
    ae.requested_contexts = [
        build_context(str(CTImageStorage), transfer_syntaxes)
    ]
    assoc = ae.associate(host, port, ae_title=called_ae)
    if not assoc.is_established:
        print("  [rogue] 关联被拒（无可用呈现上下文，拒收成立）")
        return 2
    try:
        accepted = [
            str(c.abstract_syntax)
            for c in assoc.accepted_contexts
        ]
        if not accepted:
            print("  [rogue] 无接受的上下文，拒收成立")
            return 2
        status = assoc.send_c_store(ds)
        code = int(status.Status)
        print(f"  [rogue] C-STORE 应答状态：0x{code:04X}")
        return code
    finally:
        if assoc.is_alive:
            assoc.release()


def main() -> int:
    parser = argparse.ArgumentParser(description="rogue C-STORE 投送器")
    parser.add_argument("mode", choices=["off-list", "compressed"])
    parser.add_argument("--study", required=True, help="目标 StudyInstanceUID")
    parser.add_argument("--host", default="api")
    parser.add_argument("--port", type=int, default=11113)
    args = parser.parse_args()

    if args.mode == "off-list":
        # 虚构一个绝不与清单冲突的 SOP UID（序列号段 9xxx）
        rogue_sop = _make_uid(9999)
        ds = build_ct_dataset(
            study_uid=args.study,
            series_uid=_make_uid(9998),
            sop_uid=rogue_sop,
            patient_id="P999",
            patient_name="ROGUE^OffList",
            instance_number=99,
            instance_count=1,
        )
        print(f"[rogue] 投送清单外实例 SOP={rogue_sop}（期望接收端拒收 0xC000）")
        code = _send(ds, [str(ExplicitVRLittleEndian),
                          str(ImplicitVRLittleEndian)],
                     args.host, args.port)
        return 0 if code == 0xC000 else 1

    # compressed：声明 JPEG Baseline（1.2.840.10008.1.2.4.50）
    rogue_sop = _make_uid(9997)
    ds = build_ct_dataset(
        study_uid=args.study,
        series_uid=_make_uid(9996),
        sop_uid=rogue_sop,
        patient_id="P998",
        patient_name="ROGUE^Compressed",
        instance_number=98,
        instance_count=1,
    )
    ds.file_meta.TransferSyntaxUID = UID(str(JPEGBaseline8Bit))
    print(f"[rogue] 投送声明 JPEG 压缩的实例 SOP={rogue_sop}（期望拒收）")
    code = _send(ds, [str(JPEGBaseline8Bit)], args.host, args.port)
    # 协商被拒返回 2，也算拒收成立
    return 0 if code in (0xC000, 0xA900, 2) else 1


if __name__ == "__main__":
    sys.exit(main())
