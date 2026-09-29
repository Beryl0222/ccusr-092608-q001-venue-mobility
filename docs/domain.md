# 领域约定

定义分散赛区的行程事实、运力承诺、临时改线和到场复盘事件。

聚合对象包括`fact_catalog`、`travel_demand`、`route_revision`、`capacity_commitment`、`arrival_case`。所有发生时间都必须携带时区，版本号从 1 开始递增，基础校验不会改写调用方输入。

## 事实目录（fact_catalog）

`FACT_REGISTERED` 登记可被放行引用的版本化事实，载荷包含 `fact_kind`（`schedule` / `road_network` / `lodging` / `public_transit` / `resource_pool` / `security_window` 之一）、`fact_key`、`fact_version` 与 `snapshot`。

- 赛程事实携带场次时间；路网事实携带路段与受事故影响的封闭状态；二者各自独立版本化。
- 每次放行（`CLEARANCE_ISSUED`）必须在载荷中锁定当时采用的 `schedule_version` 与 `road_version`，之后事实升级不会追溯改写已发行程。

## 申请与放行（travel_demand）

- `DEMAND_SUBMITTED`：申请载荷包含 `requester`、`category`（`athlete` / `media` / `official` / `staff`）、`headcount`、`accessibility_seats`、`origin_ref`、`venue_ref`、`needed_at` 以及申请自陈的 `facts`。
- `FACT_CONFIRMED`：运动队（`team`）、交通方（`transport`）、安保方（`security`）只确认各自掌握的事实，载荷携带 `role`、`confirmation_key`、`claims`；重复确认按 `confirmation_key` 幂等。
- `SCHEDULE_FROZEN`：三方确认齐备后锁定赛程与路网版本。
- `CLEARANCE_ISSUED`：载荷还需包含 `schedule_version`、`road_version`、`itinerary`、`checkpoints`、`access`（通道标识及适用人员类别）。媒体类行程不会获得仅对运动员开放的通道。
- 同一申请键复送但路线或人数指纹不同时，登记 `DEMAND_ISOLATED` 并分配新的申请标识，原申请不受影响；回执（`receipt_key`）重放不得二次扣减运力。

## 运力承诺（capacity_commitment）

- `CAPACITY_HELD`：载荷还需包含 `resource_ref`、`quantity`。车辆、司机、无障碍席位为三个独立维度，任一维度超配即拒绝整笔占用。
- `CAPACITY_RELEASED`：按同一 `resource_ref` 归还运力，载荷包含 `reason`。

## 临时改线（route_revision）

- `ROUTE_REVISED`：载荷还需包含 `impact_scope`、`effective_at`。
- `EMERGENCY_RELEASED`：紧急改线可先占用最低必要资源，载荷包含 `revision_id`、`resource_ref`、`quantity`、`review_due_at`、`reason`；未在 `review_due_at` 前收到 `EMERGENCY_RATIFIED`（载荷含 `revision_id`）即释放临时占用并升级。

## 到场复盘（arrival_case）

- `DEPARTED` 携带 `departed_at`；发车后超时未到场触发 `NO_SHOW_RELEASED`（载荷含 `resource_ref`、`quantity`、`released_at`）释放运力。
- `ARRIVAL_CONFIRMED` 携带 `arrived_at`；`ESCALATED` 携带 `to_role`、`reason`；`CASE_OPENED` 携带 `demand_id`、`opened_at`、`reason`，将一次行程的事实变化、资源调整与最终到场串成可复盘链路。

相同事件标识的业务幂等、冲突隔离和状态推进由上层服务负责；本仓库的基础校验只定义可稳定交换的事实。
