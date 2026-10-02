# API 说明

所有接口均在全局事务锁内串行执行；写接口成功后数据修订号 `revision` 单调递增。
请求/响应均为 `application/json; charset=utf-8`。

## 资源模型

| 对象 | 关键字段 | 说明 |
|---|---|---|
| 网格 `grids` | `code,name,x,y,threatened` | 受威胁网格是路网节点与家庭位置 |
| 道路 `roads` | `code,a,b,length,capacity,status` | 无向边；`capacity`=单波次可并发通过的车辆数；`status=open/closed` |
| 避难点 `shelters` | `code,name,x,y,beds,accessible,pets_allowed,status` | `status=active/suspended` |
| 车辆 `vehicles` | `code,name,seats,accessible,status` | `status=available/down/departed` |
| 家庭 `households` | `ref,name,grid,members,mobility,pets,contact` | `ref` 为外部户号，**家庭不可拆分**，重复 `ref` 不重复登记 |

容量口径：床位按**人数**计；车辆座位按 `members + pets`（宠物占座）；
道路容量按**车辆批次**计；已出发（`departed`）占用永久锁定。

## 接口

### 状态
- `GET /api/state` — 全量态势：网格/道路/避难点/车辆/家庭、批次与安置、
  每户状态、各避难点 `committed/reserved/remaining` 床位、修订号、最近事件。
- `GET /api/events` — 审计事件流。

### 导入与登记（幂等）
- `POST /api/import/grids` `/roads` `/shelters` `/vehicles`
  - 体：对象数组（或 `{"items":[...]}`）。按 `code` upsert，重复导入覆盖更新、不产生重复行。
- `POST /api/households`
  - 单个对象或数组；同 `ref` 已存在则跳过（`skipped_duplicates` 计数，且不覆盖原登记）。

### 规划与发布
- `POST /api/plan` `{"name","commander"}`
  - 仅对**受威胁网格中尚未出发**的家庭求解；返回
    `feasible,max_wave,batches[],unassigned[],capacity,totals` 及方案 `id`。
  - 不改变任何已发布命令，可反复调用以比较候选方案。
- `GET /api/plans` / `GET /api/plans/{id}` — 候选与历史方案列表/详情。
- `POST /api/plans/{id}/publish` `{"commander"}`
  - 发布分区撤离令。乐观并发：方案 `base_revision` 必须等于当前 `revision`，
    否则返回 **409**（已有他人发布，或路况/资源已变化），需重新规划。
  - 不可行方案、空方案、违反硬约束的方案一律 409 拒绝。
  - 发布即替换全部**未出发**计划批次；已出发批次与其床位占用原样保留。

### 执行（承诺）
- `POST /api/batches/{id}/depart`
  - 车辆离场：批次置 `departed`、车辆置 `departed`、目的地床位计入 `committed`。
    同一辆车后续未出发批次自动撤回（`cancelled`）等待重新编排。
  - **承诺不可逆**：之后任何事件与重新规划都不会改派该户或释放其床位。

### 态势事件（只影响未出发批次）
`POST /api/events`，按 `type`：
- `road_close {"code"}` — 封闭道路；路径经过该道路的未出发批次撤回。
- `road_open {"code"}` — 恢复道路，之后可继续编排。
- `vehicle_down / vehicle_recovered {"code"}`
- `shelter_suspend / shelter_resume {"code"}`
- `fire_expand {"code","close_roads":[...]}` — 火线扩大，可同时封路。
- `fire_clear {"code"}` — 网格解除威胁。

返回均含 `revision` 与 `released_for_replan`（被撤回待重排的户号）。

### 运维
- `POST /api/reset` — 清空并恢复演示数据。

## 隔离推演分支（what-if 沙盘）

每个分支拥有**独立世界**（`world_id=分支id`）与一份**不可变基线世界**
（`world_id=baseline:<id>`）。分支完整复制创建时刻的实战（含已承诺批次、出发记录、
床位占用、已离场车辆）；此后所有写入只落在分支世界，实战表零改动。分支世界有自己的
修订号，推演推进不影响实战修订号。除特别说明外，写接口均接受 `idempotency_key`，
同键重试安全返回首次结果；所有操作持久化，重启后恢复。

### 分支生命周期
- `POST /api/branches` `{"name","commander","idempotency_key?"}`
  - 从当前实战创建隔离分支，返回分支 `id/status/base_revision/tick/pending_events`。
  - 默认注入一段推演剧本（封路→暂停→故障→依次恢复）。同键重复创建返回同一分支。
- `GET /api/branches` — 分支列表（含 `live_revision` 与 `stale`：基线是否过期）。
- `GET /api/branches/{id}` — 分支详情：时钟、待执行事件、最近一次模拟结果。
- `POST /api/branches/{id}/discard` `{"idempotency_key?"}`
  - 废弃分支：清除其推演/基线世界，保留元数据与审计；终态幂等，废弃后不可再写。
- `GET /api/branches/{id}/state` — 分支世界全量态势（结构同 `/api/state`，`world` 标识分支）。
- `GET /api/branches/{id}/audit` — 该分支及实战侧相关审计流（创建/tick/提升/拒绝/废弃）。
- `GET /api/branch-audit` — 全局分支相关审计。

### 连续模拟
- `POST /api/branches/{id}/events`
  `{"type","code","capacity?","beds?","close_roads?","at_tick?","idempotency_key?"}`
  - 向推演队列注入资源事件；`at_tick` 默认“下一时钟”。类型除实战事件外另支持
    `road_set_capacity`、`shelter_set_beds`。只在分支世界生效。
- `POST /api/branches/{id}/tick` `{"target_tick?","auto_depart?","idempotency_key?"}`
  - 从当前时钟连续推进到 `target_tick`（默认 +1）。每个时钟：
    1) 到期事件生效（路网/运力/收容变化，撤回受影响未出发批次）；
    2) 规划器对未撤离家庭自动求解并发布调度；
    3) 离场当前最小波次（车辆下一时钟自动折返，基线里真实离场车辆永不折返）。
  - 每步返回 `feasible/batch_count/departed_batch_count/unassigned[]`（含每户受阻原因）。
  - 时钟不倒流；推进到同一目标时钟为幂等 noop/重放。

### 并排差异
- `GET /api/branches/{id}/diff` — 同时返回分支与实战的
  `people_destinations[]`（每户去向：待安排/已排波次/已离场→避难点）、
  `resource_remaining[]`（床位 committed/reserved/remaining、车辆与道路状态）、
  `blocked_reasons[]`（当前态势下逐户受阻原因差异）、
  `promotable_changes[]`（可提升资源字段及是否与实战冲突）与 `counts`。

### 提升（三方合并）
- `GET /api/branches/{id}/promotion-check` — 纯预检（不写库）。
- `POST /api/branches/{id}/promote` `{"commander?","idempotency_key?"}`
  - 三方 = **创建基线** / **提升时实战** / **推演配置意图**。
  - 只提升推演中通过事件**显式设定**的资源字段（道路 status/capacity、车辆
    status/seats/accessible、避难点 status/beds/accessible/pets_allowed、网格 threatened），
    且仅取相对基线有净变化的字段（中断后恢复回基线的不计入）。
  - 推演产生的**命令、离场记录、已承诺占用、规划/批次/安置一律不带入**实战。
  - 规则：任一字段实战也改过（基线≠当前≠推演）即为冲突；或创建后实战修订号已前进
    （基线过期）且确有净改动时，**整体拒绝**（HTTP 409），不写入任何改动。
  - 409 响应列出 `conflicts[]`（对象 kind/code、字段 field、baseline/live/branch 三方值、
    reason）及 `stale/base_revision/live_revision`；床位调到小于实战已承诺占用也拒绝。
  - 成功返回 `applied_changes[]/applied_count/revision`；分支置 `promoted`，不可重复提升。

## 硬约束（规划器与发布前校验器双重保证）

1. 床位：`已承诺 + 本方案安置人数 ≤ 总床位`；
2. 座位：`成员 + 宠物数 ≤ 车辆座位`；
3. 行动辅助户只能乘无障碍车辆、进入无障碍避难点；
4. 携带宠物户只能进入 `pets_allowed` 避难点；
5. 路径必须全部由 `open` 道路连续构成（系统不安排家庭自行经过关闭路段）；
6. 同一波次同一条道路上的批次数 ≤ 道路容量；同一车辆同一波次最多一批；
7. 暂停接收的避难点、故障/已离场车辆不得使用；
8. 每个待撤离家庭恰好安置一次；已出发家庭不得被重新分配。

无可行解时，`unassigned[]` 按户给出具体阻断原因（封路、无障碍车不足、
座位不足、某避难点剩余床位、户间竞争耗尽床位、道路时序瓶颈等）。
