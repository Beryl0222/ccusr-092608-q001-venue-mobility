# 领域约定

定义分散赛区的行程事实、运力承诺、临时改线和到场复盘事件。

聚合对象包括`travel_demand`、`route_revision`、`capacity_commitment`、`arrival_case`。事件类型包括`SCHEDULE_FROZEN`、`ROUTE_REVISED`、`CAPACITY_HELD`、`EMERGENCY_RELEASED`、`ARRIVAL_CONFIRMED`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事件载荷

- `ROUTE_REVISED`：载荷还需包含 `impact_scope`, `effective_at`。
- `CAPACITY_HELD`：载荷还需包含 `resource_ref`, `quantity`。
- `EMERGENCY_RELEASED`：载荷还需包含 `review_due_at`, `reason`。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库只定义可稳定交换的基础事实。
