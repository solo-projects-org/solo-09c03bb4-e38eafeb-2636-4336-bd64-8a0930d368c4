"""生成完全虚构的合成 CT 数据集（无任何真实患者信息）。

每个实例都是合法的 DICOM CT Image Storage 对象，未压缩传输语法，
可被 pydicom/pynetdicom 直接读写与网络传输。
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path

import pydicom
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    CTImageStorage,
    ExplicitVRLittleEndian,
    generate_uid,
)

# 固定 UID 前缀，保证演示环境内 UID 稳定可读
UID_PREFIX = "1.2.826.0.1.3680043.8.498."
PATIENT_NAMES = {
    1: "PHANTOM^Alpha",
    2: "PHANTOM^Bravo",
    3: "PHANTOM^Charlie",
}


def _make_uid(serial: int) -> str:
    return f"{UID_PREFIX}{serial}"


def build_ct_dataset(
    study_uid: str,
    series_uid: str,
    sop_uid: str,
    patient_id: str,
    patient_name: str,
    instance_number: int,
    instance_count: int,
    rows: int = 64,
    cols: int = 64,
) -> Dataset:
    """构造一个内存中的合成 CT Dataset（带 file_meta）。"""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = str(CTImageStorage)
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = str(ExplicitVRLittleEndian)
    file_meta.ImplementationClassUID = _make_uid(9900)
    file_meta.ImplementationVersionName = "SIMPACS_1"

    ds = Dataset()
    ds.file_meta = file_meta
    ds.preamble = b"\0" * 128

    ds.SOPClassUID = str(CTImageStorage)
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid

    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "O"

    ds.Modality = "CT"
    ds.StudyDate = "20261101"
    ds.StudyTime = "090000"
    ds.ContentDate = "20261101"
    minute = (instance_number - 1) % 60
    ds.ContentTime = f"09{minute:02d}00"
    ds.AccessionNumber = f"SIM{patient_id}"
    ds.StudyID = f"S{patient_id}"
    ds.StudyDescription = f"Synthetic CT demo study {patient_id}"
    ds.SeriesNumber = 1
    ds.SeriesDescription = "Synthetic axial CT"
    ds.InstanceNumber = instance_number

    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.SamplesPerPixel = 1
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.Rows = rows
    ds.Columns = cols
    ds.PixelSpacing = [1.0, 1.0]
    ds.SliceThickness = 2.0
    ds.SliceLocation = (instance_number - 1 - instance_count / 2) * 2.0
    ds.ImagePositionPatient = [0.0, 0.0, float(ds.SliceLocation)]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
    ds.RescaleIntercept = -1024
    ds.RescaleSlope = 1
    ds.RescaleType = "HU"
    ds.DeviceSerialNumber = "SYNTH-001"
    ds.Manufacturer = "Synthetic PACS Demo"

    # 确定性的“模体”像素：径向渐变 + 层间变化，HU 值在常见 CT 范围内
    pixels = bytearray()
    cx = cy = (rows - 1) / 2.0
    for r in range(rows):
        for c in range(cols):
            dist = math.sqrt((r - cy) ** 2 + (c - cx) ** 2)
            hu = int(400 * math.exp(-(dist ** 2) / 200.0)
                     + 50 * math.sin(instance_number + dist / 4) - 200)
            hu = max(-1024, min(3071, hu))
            pixels += int(hu + 1024).to_bytes(2, "little", signed=False)
    ds.PixelData = bytes(pixels)
    return ds


def generate_study(
    out_dir: Path,
    study_index: int,
    instance_count: int,
) -> dict:
    """生成一个检查的全部实例文件，返回检查元数据。"""
    patient_id = f"P{study_index:03d}"
    study_uid = _make_uid(1000 + study_index)
    series_uid = _make_uid(2000 + study_index)
    study_dir = out_dir / study_uid
    study_dir.mkdir(parents=True, exist_ok=True)

    for i in range(1, instance_count + 1):
        sop_uid = _make_uid(3000 + study_index * 100 + i)
        ds = build_ct_dataset(
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=sop_uid,
            patient_id=patient_id,
            patient_name=PATIENT_NAMES[study_index],
            instance_number=i,
            instance_count=instance_count,
        )
        ds.save_as(str(study_dir / f"{i:04d}.dcm"), enforce_file_format=True)

    return {
        "study_instance_uid": study_uid,
        "series_instance_uid": series_uid,
        "patient_id": patient_id,
        "patient_name": PATIENT_NAMES[study_index],
        "instances": instance_count,
        "directory": str(study_dir),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="生成虚构合成 CT 数据")
    parser.add_argument("--out", required=True, help="输出目录")
    parser.add_argument(
        "--counts", default="8,6,5",
        help="三个检查各自的实例数，逗号分隔（默认 8,6,5）",
    )
    args = parser.parse_args()

    out_dir = Path(args.out)
    counts = [int(x) for x in args.counts.split(",")]
    manifest = []
    for idx, count in enumerate(counts, start=1):
        meta = generate_study(out_dir, idx, count)
        manifest.append(meta)
        print(f"generated study {meta['study_instance_uid']} "
              f"({count} instances) -> {meta['directory']}")

    import json
    manifest_path = out_dir / "studies.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False))
    print(f"manifest -> {manifest_path}")


if __name__ == "__main__":
    main()
