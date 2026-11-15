# DICOM CT 离线取数流水线

面向影像科研数据员的院内 PACS 取数工具：提交 `StudyInstanceUID`，经
**C-FIND 固定本批 SOP 清单 → C-MOVE 发起回传 → C-STORE 接收落盘 →
核对收齐 → 打包 ZIP 下载** 的完整流水线，支持多检查并行、慢回传交错、
重复投送去重、清单外实例拒收、保存失败判失败、**数据卷字节配额**与
重启恢复。

* **API 框架**：FastAPI（自带 Swagger/OpenAPI）
* **DICOM 栈**：pynetdicom 3.x（C-STORE SCP + C-FIND/C-MOVE SCU）、pydicom
* **持久化**：SQLite（WAL，全量落在数据卷）
* **字节配额**：所有批共享的可配置上限，C-STORE 写盘前按实例实际大小预留
* **模拟 PACS**：pynetdicom 实现的真实 DICOM 网络对端，内置虚构合成 CT，
  可配置慢回传与重复投送；另带 rogue 投送器演示拒收

---

## 1. 快速开始（Docker Compose）

```bash
# 构建并启动 API + 模拟 PACS（首次会自动生成 8/6/5 个实例的三个虚构 CT 检查）
docker compose up --build -d

# Swagger 文档
open http://localhost:8000/docs

# 一键端到端演示：交错回传 / 重复投送 / 清单外拒收 / 压缩拒收 /
# 保存失败 / 失败批迟到拒收 / 重启恢复
docker compose run --rm demo
```

端口：

| 服务 | 端口 | 用途 |
|---|---|---|
| api | 8000 | REST API + Swagger |
| api | 11113 | DICOM C-STORE SCP（AE=`RETRIEVER`）|
| pacs | 11112 | 模拟 PACS（AE=`MYPACS`）|

数据卷：`api-data`（SQLite、DICOM 文件、ZIP）、`pacs-data`（虚构 CT）。

---

## 2. 业务规则与实现位置

| 规则 | 实现 |
|---|---|
| 只收**非压缩 CT** | SCP 只公布 Implicit/Explicit VR Little Endian 的 CT 存储上下文，压缩 TS 在协商阶段即被拒；`app/storage.py::validate_dataset` 二次校验 |
| 源端批内**不增删实例** | C-FIND（IMAGE 级）一次性固定清单写入 `instances` 表，`set_manifest` 只允许固定一次 |
| 回传**归入对应批** | C-STORE 按 `StudyInstanceUID` 经 `db.find_batch_for_study` 路由（按 rowid 取最新批） |
| **清单外实例不入包** | `db.is_manifest_uid` 校验，拒收并回 `0xC000`，事件 `STORE_REJECTED` |
| **重复实例不重复计数** | `(batch_id, sop_instance_uid)` 唯一约束 + `register_instance` 条件更新；重复直接回 SUCCESS，不写盘不计数，事件 `STORE_DUPLICATE` |
| **文件写完且登记成功才回 C-STORE 成功** | `handle_c_store` 严格顺序：编码取实际大小 → 配额预留 → 临时文件 → fsync → rename → 目录 fsync → DB 登记 → 预留转已存；任一步失败回 `0xA700/0xC000`，登记失败会删除已写文件并只释放本次预留 |
| **数据卷字节配额（所有批共享）** | `app/quota.py::QuotaManager`；`QUOTA_LIMIT_BYTES`（0=不限额）。C-STORE 写盘前按实例编码后的实际字节数 `reserve`，所有批的**已存文件 + 在途预留**合计不得超限 |
| **额度不足仅该批失败并说明缺口** | 预留超限抛 `QuotaExceeded`，只 `fail_batch` 当前批并记录 `缺口 N 字节`，C-STORE 回 `0xA700`、事件 `QUOTA_EXCEEDED`；其它批已占/预留额度完全不动 |
| **合法重投不占额** | 重复实例在进入配额逻辑前即被识别（`is_already_received`），不写盘、不计数、不预留，直接回 SUCCESS（事件 `STORE_DUPLICATE`） |
| **写盘/登记失败只释放本次预留** | `quota.release` 仅回滚当前实例的在途预留，绝不动其它批的已存额度 |
| **C-MOVE 成功且清单收齐才可下载** | 下载接口仅允许 `COMPLETED` 且 zip_path 存在；MOVING/FAILED 返回 409 |
| 传输/保存失败、结束后缺件均**判失败** | C-MOVE 非 0x0000（含 0xA702/0xB000）失败；`missing_uids` 非空失败；写盘异常立即 `fail_batch` |
| **迟到回传不能恢复失败批** | 终态 FAILED 的批收到 C-STORE 一律拒收；DB 层 `transition_state` 禁止任何状态迁出终态 |
| 多检查**并行、慢回传互不阻塞** | 每批独立线程；C-STORE SCP 每关联一线程；C-MOVE 回传与其它批的 API/DICOM 处理无共享阻塞 |
| **重启恢复** | 启动先 `storage.reconcile_volume`：按卷中**实际保留的文件**重建配额已存占用（在途预留不跨进程，归零），清理 `.part`/未登记 `.dcm`/孤儿批目录等残件（不计配额）；随后所有非终态批置 FAILED；COMPLETED 批保留可下载，ZIP 丢失自动按登记重建 |

### 批状态机

```
PENDING ──C-FIND──▶ FINDING ──清单固定+故障武装──▶ MOVING
                                          │
                  收齐+C-MOVE 成功 ────────┴──▶ COMPLETED（可下载，终态）
                  任何失败/缺件 ──────────────▶ FAILED（终态，迟到回传拒收）
重启时 PENDING/FINDING/MOVING ───────────────▶ FAILED
```

---

## 3. REST API

Swagger：`GET /docs`（OpenAPI JSON：`/openapi.json`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/batches` | 提交检查，body：`{"study_instance_uid": "...", "fault_inject": ""}`，`fault_inject` 可选 `save_failure` |
| GET | `/batches` | 列出全部批（含 `stored_bytes`/`reserved_bytes`/`quota_total_bytes` 配额占用）|
| GET | `/batches/{id}` | 批状态与进度（`expected_count/received_count/state/downloadable` + 配额字节）|
| GET | `/batches/{id}/events` | 事件流水（接收、重复、拒收、状态迁移、故障、`QUOTA_EXCEEDED`、恢复）|
| GET | `/batches/{id}/instances` | 清单、已收、缺失 SOP 列表 |
| GET | `/batches/{id}/download` | 下载 ZIP（仅 COMPLETED），ZIP 内含 `dicom/` 与 `MANIFEST.csv` |
| GET | `/quota` | 数据卷字节配额：上限、已存、在途预留、已用、剩余可用及各批明细 |
| GET | `/health` | 健康检查（含 `quota_limit_bytes`/`quota_used_bytes`）|
| GET | `/internal/pacs-studies` | （演示）C-FIND 列出模拟 PACS 检查 |
| POST | `/internal/suicide` | （演示）退出进程，由 compose 自动重启以演示恢复 |

手工调用示例：

```bash
BASE=http://localhost:8000
STUDY=$(curl -s $BASE/internal/pacs-studies | jq -r '.[0].study_instance_uid')

BID=$(curl -s -X POST $BASE/batches -H 'Content-Type: application/json' \
  -d "{\"study_instance_uid\":\"$STUDY\"}" | jq -r .id)

watch "curl -s $BASE/batches/$BID | jq"
curl -s $BASE/batches/$BID/events | jq
curl -OJ $BASE/batches/$BID/download

# 观察数据卷配额（回传进行中可见 reserved_bytes 在途预留起伏）
curl -s $BASE/quota | jq

# 保存失败演示
curl -s -X POST $BASE/batches -H 'Content-Type: application/json' \
  -d "{\"study_instance_uid\":\"$STUDY\",\"fault_inject\":\"save_failure\"}"
```

### 3.1 字节配额语义

配额只统计 **DICOM 实例文件**（SQLite、ZIP 不计入），由两部分构成：

```
已用 used = 已存 stored（写盘+登记成功的文件实际字节）
          + 在途预留 reserved（已 reserve 尚未登记完成的实例）
used <= QUOTA_LIMIT_BYTES   # 0 表示不限额
```

单个 C-STORE 实例的处理顺序（`app/dicom.py::handle_c_store`）：

```
编码得到实际字节 N  →  quota.reserve(N)   # 不足则仅本批失败，报缺口
   → 原子写盘 fsync  →  DB 登记
        成功: quota.commit(N)   # 预留转已存
        失败: 删文件 + quota.release(N)   # 只释放本次预留，不碰其它批
```

* 重复实例在 `reserve` 之前就被去重，**合法重投不占额**。
* 额度不足返回 DICOM 状态 `0xA700`（Out-of-resources），批 `failure_reason`
  形如 `数据卷额度不足，批失败：缺口 5850 字节（本实例 9202 字节，配额
  168960 字节，已占 165608 字节）`，并产生 `QUOTA_EXCEEDED` 事件。
* 配额是“先到先得”的共享资源：并行批交错回传时，谁先预留成功谁占额，
  失败批不影响已占额的批继续取数与下载。

---

## 4. 演示场景说明

模拟 PACS 支持环境变量（见 `docker-compose.yml`）：

* `MOVE_DELAY=1.0`：每个实例发送前停顿 1 秒，制造慢回传，便于观察多批交错
* `MOVE_DUPLICATE_EVERY=3`：每 3 个实例额外用一条新关联重发一次，模拟源端重试
  （接收端应回 SUCCESS 但不计数、不占额）

API 配额环境变量：

* `QUOTA_LIMIT_BYTES=168960`：数据卷实例字节上限（compose 默认 165KB）。
  演示中前三个批正常/按预期失败后，再提交的批会在途中触发额度不足，
  `failure_reason` 与 `QUOTA_EXCEEDED` 事件给出具体缺口；设为 `0` 关闭限额。

`docker compose run --rm demo` 会完整演示：并行交错回传、重复去重不占额、
rogue 拒收、保存失败只释放本次预留、**额度不足仅该批失败（含缺口与重投
仍报缺口）**、ZIP 清单核对，以及**重启后按实际文件恢复配额、清理残件、
完成包继续可下载**。也可加 `--skip-restart` 跳过重启段落，或
`--quota-bytes <N>` 提示期望阈值。

rogue 投送器（演示容器/API 镜像内均可运行）：

```bash
# 清单外实例：期望 0xC000 拒收
python -m simulator.rogue_sender off-list   --study <StudyUID> --host api --port 11113
# 声明 JPEG 压缩：期望关联协商被拒
python -m simulator.rogue_sender compressed --study <StudyUID> --host api --port 11113
```

保存失败演示的实现是**真实故障**而非 mock：提交时把批目录 `chmod 555`，
非 root 容器用户写入必然 `PermissionError`，可观察到：
首个实例 C-STORE 回 `0xA700` → 批立即 FAILED → 后续实例全部以“已失败”拒收
→ C-MOVE 最终 `0xA702` → 下载返回 409。

---

## 5. 目录结构

```
app/
  config.py      环境变量配置（AE、端口、数据卷路径、超时、QUOTA_LIMIT_BYTES）
  db.py          SQLite schema/迁移、状态机、清单/登记去重（含 size_bytes）、重启查询
  quota.py       字节配额管理：预留/提交/释放、额度不足缺口、按卷重建
  storage.py     非压缩 CT 校验、实例编码取实际大小、原子写盘+fsync、ZIP、卷核对/残件清理
  dicom.py       C-STORE SCP 处理器（编码→预留→写盘→登记→提交/释放）、C-FIND/C-MOVE SCU
  pipeline.py    每批后台线程编排与启动恢复（先卷核对配额，再恢复批状态）
  main.py        FastAPI 路由、lifespan、Swagger、/quota
simulator/
  generate_data.py  虚构合成 CT 生成器（确定性模体像素，无真实患者信息）
  pacs.py           模拟 PACS（C-ECHO/C-FIND/C-MOVE，慢回传+重复投送）
  rogue_sender.py   清单外/压缩实例投送器
demo/
  run_demo.py       端到端演示编排
tests/
  test_db.py          状态机/去重/终态保护单元测试
  test_storage.py     校验/原子写/ZIP 单元测试
  test_quota.py       配额语义与重启卷核对/残件清理单元测试
  test_e2e.py         真实 pynetdicom 网络栈端到端测试（9 个场景）
  test_quota_e2e.py   小配额下额度不足/隔离/重投/重启恢复端到端测试（6 个场景）
docker-compose.yml  Dockerfile  Dockerfile.pacs  entrypoint.sh
```

## 6. 本地测试（无需 Docker）

```bash
pip install -r requirements.txt pytest
python -m pytest tests/ -v
```

端到端测试会在临时目录启动完整的 PACS + API 真实进程对，覆盖：
正常收齐打包、并行交错、重复去重不占额、清单外/压缩/未知 Study 拒收、
保存失败与迟到拒收、409 冲突、重启恢复，以及字节配额的额度不足/
批间隔离/重投不占额/重启按实际文件恢复（共 37 个测试）。

## 7. 接入真实 PACS 的注意事项

1. 在真实 PACS 上注册 AE Title `RETRIEVER` 及本服务主机/11113 端口作为
   C-MOVE 目标，并放通防火墙。
2. 用 `PACS_AET/PACS_HOST/PACS_PORT` 指向真实源端；确认其 C-FIND 支持
   Study Root IMAGE 级查询并返回 `TransferSyntaxUID`。
3. 流水线只接受未压缩 little-endian CT；若源端只存压缩 TS，需在源端配置
   转码，或扩展 `app/dicom.py` 的上下文与 `storage.UNCOMPRESSED_TS` 策略。
4. 数据卷需保证持久化挂载与足够 inode/容量；SQLite 与 DICOM 文件均在
   `DATA_DIR` 下，备份该目录即可完整备份批状态与文件。
5. 用 `QUOTA_LIMIT_BYTES` 限制实例对数据卷的占用：上限只统计 DICOM 实例
   （已存文件 + 在途预留），不含 SQLite 与 ZIP；离线节点应按卷容量扣除
   数据库/ZIP/余量后设置。配额超限不影响其它批，仅令当前批失败并报缺口。
