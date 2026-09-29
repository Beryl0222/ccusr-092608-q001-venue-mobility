# 分散赛区通行保障台

把场馆日程、驻地、人员类别、无障碍需求、公共交通班次、接驳运力、安检要求、事故影响和临时承诺放进**同一决策过程**的通行保障服务。所有决策只追加领域事件、锁定当时的赛程与路网版本；进程可凭事件日志与时钟侧车完整恢复。

## 目录

- `contracts/domain.schema.json`：对象、事件和载荷字段约定。
- `data/sample.json`：可直接校验的联调样例；`data/scenario-delay.json`：完整延误复盘脚本。
- `src/venue_mobility/`
  - `contracts.py` 基础契约校验（不改写输入）。
  - `clock.py` 可控时钟；`facts.py` 版本化事实目录；`inventory.py` 三维运力库存。
  - `planner.py` 依据锁定版本生成行程；`service.py` 事件溯源决策内核。
  - `projections.py` 角色最小披露视图；`httpapi.py` HTTP 接口。
  - `store.py` JSONL 事件日志与时钟侧车；`scenario.py` 复盘引擎；`cli.py` 命令行。
- `tests/`：契约、库存、服务生命周期、隔离/幂等、恢复、角色投影、HTTP 端到端、复盘脚本。
- `docs/domain.md`：领域对象与事件语义。

## 核心规则

- **版本锁定**：每次放行（`CLEARANCE_ISSUED`）都在事件里锁定 `schedule_version` 与 `road_version`；之后事实升级不追溯改写已发行程。
- **三方各认其事实**：`team` 只认人数/类别/驻地，`transport` 只认路网与车辆就绪，`security` 只认通道与类别；越权确认直接拒绝。确认齐备才能锁定、放行。
- **三维原子占用**：车辆、司机、无障碍席位独立核算；任一维度不足则整笔申请被拒，绝不留下半占用。命令串行化，并发申请不会超配。
- **幂等与隔离**：同一申请键+同指纹重放不产生事件、不二次扣减；重复回执跨申请复用时被拒绝；同申请键但路线或人数不同时登记 `DEMAND_ISOLATED` 并分配独立申请。
- **紧急改线**：可先占用“最低必要”的增量资源，但必须在 `review_due_at`（默认 30 分钟）前补齐 `EMERGENCY_RATIFIED`；到期未批自动释放临持并升级。批准时临时占用转正、冗余运力归还，账实相符。
- **可控时钟**：只随显式推进而走；发车、失约释放（默认 90 分钟）、批准到期都按事件中记录的**绝对时间**结算，进程恢复后期限不平移。
- **最小披露**：运动队只看本队的登车/安检/到场时间与通道；交通方看路线与运力清单；安保方看通道、类别与人数；运行中心看完整依据。媒体类行程永远不会拿到仅对运动员开放的通道。

## 测试

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
```

## 命令行

契约校验（输出 `valid` 或逐行 `事件 字段 代码 说明`）：

```bash
PYTHONPATH=src python3 -m venue_mobility.cli validate contracts/domain.schema.json data/sample.json
```

延误复盘——一次从事故封路、地铁停运、并发超配被拒、绕行缓行到最终到场立案的完整依据：

```bash
PYTHONPATH=src python3 -m venue_mobility.cli replay data/scenario-delay.json \
  --data-dir .run/delay --export .run/delay/report.json
```

报告分四段：决策时间线（事实变化 → 资源调整 → 到场）、行程结果与延误、运力台账、事实版本台账。脚本中以 `"expect_error": true` 标注的步骤是**被预期拒绝**的领域行为（如运力超配）。

启动 HTTP 服务：

```bash
PYTHONPATH=src python3 -m venue_mobility.cli serve --data-dir .run --port 8080
```

## HTTP 接口（摘要）

所有请求用 `X-Role: team|transport|security|ops` 表明角色，team 另用 `X-Identity: <队伍标识>` 限定本队。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/admin/facts` | ops | 登记版本化事实（赛程/路网/驻地/公交/运力池/安检） |
| POST | `/demands` | 任意 | 提交申请；同键异指纹自动隔离 |
| POST | `/demands/{id}/confirmations` | 与 body.role 一致 | 仅确认本角色掌握的事实，按 confirmation_key 幂等 |
| POST | `/demands/{id}/freeze` | ops | 三方齐备后锁定赛程/路网版本 |
| POST | `/demands/{id}/clearance` | ops | 原子占用并发行程（含版本、行程、检查点、通道、资源） |
| POST | `/demands/{id}/emergency-revise` | ops | 紧急改线，先占最低必要资源并记录批准限期 |
| POST | `/revisions/{id}/ratify` | ops | 限期内补齐批准 |
| POST | `/demands/{id}/depart` / `arrive` | transport/ops；team\|ops | 发车；确认到场并归还运力 |
| POST | `/demands/{id}/escalate` | ops | 升级处置并立案 |
| POST | `/clock/tick` | ops | 推进可控时钟并自动结算到期事项 |
| GET | `/demands/{id}` | 分角色 | 最小披露行程；team 只能看本队 |
| GET | `/demands/{id}/trace` | ops | 完整事件依据链 |
| GET | `/usage` | transport/ops | 三维运力台账；`/facts` 查事实版本 |

## 持久化与恢复

事件以 JSONL 只追加写入 `<data-dir>/events.jsonl`，时钟快照落在同目录 `clock.json`。重新打开目录即重放全部事件重建库存与状态；所有截止时间都是带时区的绝对时间，因此恢复后继续按原截止时间推进，无需任何平移。
