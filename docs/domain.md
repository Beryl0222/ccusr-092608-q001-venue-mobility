# 领域约定

定义分散赛区的行程事实、运力承诺、临时改线和到场复盘事件。

聚合对象包括 `travel_demand`、`route_revision`、`capacity_commitment`、`arrival_case`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事实归属

每类事实只有一个责任角色可以确认，服务层会拒绝越权上报：

| 事实类型 | 含义 | 确认角色 |
| --- | --- | --- |
| `fixture` | 赛程（含提前、改期） | `team` |
| `lodging` | 驻地（含临时改住处） | `team` |
| `roster` | 人员类别、人数、无障碍需求 | `team` |
| `leg` | 走廊常规车程 | `transport` |
| `transit` | 公共交通班次（含停运） | `transport` |
| `resource` | 车辆/司机/无障碍席位运力池 | `transport` |
| `screening` | 安检窗口（含收紧、准入类别、运动员专用） | `security` |
| `incident` | 事故影响与延误分钟数 | `security` |

事实按版本覆盖：新版本取代旧版本，旧版本仍保留在日志中供复盘。事实变化归入两个领域版本计数器：赛程域（`fixture`/`lodging`/`roster`）与路网域（其余）。每次放行在事件中锁定当时的 `{schedule, network}` 版本对，后续事实变化不影响已锁定的行程依据。

## 事件类型

`FACT_RECORDED`、`SCHEDULE_FROZEN`、`ROUTE_REVISED`、`CAPACITY_HELD`、`CAPACITY_RELEASED`、`REQUEST_DENIED`、`EMERGENCY_RELEASED`、`EMERGENCY_RATIFIED`、`DISPATCH_CONFIRMED`、`NO_SHOW_RELEASED`、`ESCALATION_RAISED`、`ARRIVAL_CONFIRMED`。

## 事件载荷

- `FACT_RECORDED`：`fact_type`, `owner_role`（另有 `ref`, `version`, `data`, 幂等 `receipt`）。
- `ROUTE_REVISED`：`impact_scope`, `effective_at`。
- `CAPACITY_HELD`：`resource_ref`, `quantity`（另含时间窗、`emergency` 与补批截止）。
- `CAPACITY_RELEASED`：`resource_ref`, `quantity`。
- `REQUEST_DENIED`：`request_ref`, `reason_code`（超配、身份冲突、无通道、赶不上截止等）。
- `EMERGENCY_RELEASED`：`review_due_at`, `reason`。紧急改线先占用最低必要资源，须在补批截止前由 `EMERGENCY_RATIFIED` 补齐批准。
- `EMERGENCY_RATIFIED`：`request_ref`, `approver`。
- `DISPATCH_CONFIRMED`：`request_ref`, `departed_at`。发车后取消失约宽限计时。
- `NO_SHOW_RELEASED`：`request_ref`, `released_at`。超过发车宽限仍未发车时释放运力。
- `ESCALATION_RAISED`：`request_ref`, `reason_code`。紧急占用逾期未批时升级并释放。
- `ARRIVAL_CONFIRMED`：`arrived_at`（另含计划到场时间，用于复盘偏差）。

## 关键不变量

- **幂等**：写操作携带 `receipt`，重复回执原样回放既有结论，不二次扣减资源。
- **隔离**：相同 `request_ref` 但路线/人数/时间指纹不同，记 `identity_conflict` 拒绝，原申请与占用不动。
- **不超配**：车辆、司机、无障碍席位分别建池，按时间窗计算并发占用；任一池不足即拒绝。紧急占用可先行，但逾期未批自动升级释放。
- **时钟驱动**：集合、发车、失约释放、升级处置全部由可控时钟推进触发；时钟与事件日志落盘，进程恢复后沿用原截止时间。
- **最小披露**：行程投影按角色裁剪，媒体不能查看或使用运动员专用通道。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；契约层只定义可稳定交换的基础事实。
