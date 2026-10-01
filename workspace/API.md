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
