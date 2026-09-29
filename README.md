# 分散赛区通行保障台

把场馆日程、驻地、人员类别、无障碍需求、公共交通班次、接驳运力、安检要求、事故影响和临时承诺置于同一决策过程：每次放行锁定当时的赛程与路网版本，紧急改线先占用最低必要资源并限期补批，时钟驱动失约释放与升级，事件日志支持进程恢复与完整复盘。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例。
- `src/venue_mobility/`
  - `contracts.py`：基础契约校验（不改写输入）。
  - `clock.py`：可控时钟（落盘，只能向前）。
  - `journal.py`：只追加事件日志（event_id 幂等，重放恢复）。
  - `resources.py`：车辆/司机/无障碍席位时间窗台账。
  - `domain.py`：事实注册表（角色归属、版本覆盖）与行程规划器。
  - `service.py`：决策服务（版本锁定、放行、紧急改线、发车/失约/升级）。
  - `app.py`：HTTP 接口与角色行程投影。
  - `scenario.py`：完整延误场景（改住处、赛程提前、公交停运、安检收紧、事故）。
  - `replay.py`：「事实变化 → 资源调整 → 最终到场」复盘报告。
  - `cli.py`：命令行入口。
- `tests/`：契约、服务、HTTP、场景、CLI 测试。
- `docs/domain.md`：领域对象、事实归属与事件语义。

## 测试与检查

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

## 命令行

```bash
# 推演内置延误场景并打印复盘报告（--state 可落盘，随后 replay 复盘）
PYTHONPATH=src python3 -m venue_mobility.cli scenario
PYTHONPATH=src python3 -m venue_mobility.cli scenario --state ./run

# 只复盘某一次申请的完整依据
PYTHONPATH=src python3 -m venue_mobility.cli replay --state ./run --request REQ-ALPHA-2
PYTHONPATH=src python3 -m venue_mobility.cli replay --events ./run/events.jsonl --json

# 启动 HTTP 服务
PYTHONPATH=src python3 -m venue_mobility.cli serve --port 8080 [--state ./run]

# 兼容原有契约校验
PYTHONPATH=src python3 -m venue_mobility.cli contracts/domain.schema.json data/sample.json
```

## HTTP 接口

所有写操作携带 `receipt` 做业务幂等；时间字段必须携带时区。

| 方法与路径 | 作用 |
| --- | --- |
| `POST /facts` | 责任方确认事实（角色与事实类型不符返回 403） |
| `POST /requests` | 提交通行申请，返回批准/拒绝、行程、占用与锁定版本；`emergency=true` 先占用后补批 |
| `POST /requests/{ref}/ratify` | 紧急占用补齐批准 |
| `POST /requests/{ref}/dispatch` | 发车确认 |
| `POST /requests/{ref}/arrival` | 到场确认并释放运力 |
| `POST /clock/advance` | 推进时钟（`minutes` 或 `until`），同时处理失约释放与逾期升级 |
| `GET /requests?role=…` | 按角色列出申请（媒体只见本类别） |
| `GET /requests/{ref}/itinerary?role=…` | 角色裁剪后的可执行行程 |
| `GET /resources` | 运力池容量与当前占用 |
| `GET /events` | 原始事件日志 |

角色最小披露：运动队看发车/到场/安检与版本依据；交通方看时间窗与所需运力；安保方看安检点、类别与人数；媒体只能看到媒体类别的指引，不能查看或使用运动员专用通道。

## 决策要点

- **版本锁定**：放行事件固化 `{schedule, network}` 版本对与规划依据链（走廊、班次、安检、事故、运力），事后事实变化不改变已批准的行程。
- **并发不超配**：车辆、司机、无障碍席位分池，按时间窗取最大并发占用；重叠申请任一池不足即拒绝。
- **幂等与隔离**：重复回执原样回放不二次扣减；同标识不同路线/人数返回 `identity_conflict` 并保留原占用。
- **紧急改线**：可先占资源，`review_due_at` 前未补批则时钟推进时自动 `ESCALATION_RAISED` 并释放；未补批不能发车。
- **恢复语义**：时钟与事件日志落盘，重启后重放重建全部状态，补批截止与发车宽限沿用原时间。
