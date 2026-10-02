"""隔离推演分支（what-if 沙盘）子系统。

核心隔离原则：
  * 每个分支拥有**独立世界**（world_id = 分支 id），另存一份不可变基线世界
    （world_id = `baseline:<id>`）；所有推演写入只落在分支世界，实战 live 世界
    的任何表都不会被分支操作触碰。
  * 分支世界复制实战的路网/运力/收容/家庭与**当前已承诺状态**（已离场批次、
    出发记录、床位占用、已离场车辆），因此推演起点与实战完全一致；分支内随后
    产生的命令、离场记录与已承诺占用都是分支世界的私有副本。

提升（三方合并）：
  * 基线 = 创建分支时的实战资源快照；当前 = 提升时刻的实战；分支 = 推演结果。
  * 只有“资源配置类”字段允许提升（道路状态/容量、车辆座位/状态、避难点床位/
    状态、网格受威胁标志）。推演产生的命令、离场记录、已承诺占用、规划、
    批次、安置**一律不带入**实战。
  * 逐字段三方比较：基线≠分支（推演改过）时，当前必须仍等于基线，否则视为
    实战与推演在同一对象同一字段上冲突。只要存在任一冲突，或基线已过期
    （创建后实战修订号已前进），整体拒绝（不写入任何改动），并列出对象与字段。

全部操作幂等：同一 Idempotency-Key 重复请求返回首次结果；结果持久化，
服务重启后分支历史、差异与审计记录完整恢复。
"""
import hashlib
import json
import time
import uuid

# ---------------------------------------------------------------- 常量

LIVE = "live"

# 允许从推演提升到实战的“资源配置”字段（其余字段绝不提升）
PROMOTABLE = {
    "roads": ["status", "capacity"],
    "vehicles": ["seats", "accessible", "status"],
    "shelters": ["beds", "accessible", "pets_allowed", "status"],
    "grids": ["threatened"],
}
RESOURCE_TABLES = list(PROMOTABLE)
COPY_TABLES = ["grids", "shelters", "roads", "vehicles", "households",
               "plans", "orders", "batches", "assignments"]

# 新建分支时默认注入的推演剧本（相对 tick，0 表示在第 1 个 tick 生效）
DEFAULT_SCENARIO = [
    {"at_tick": 0, "type": "road_close", "code": "r5"},
    {"at_tick": 1, "type": "shelter_suspend", "code": "S3"},
    {"at_tick": 2, "type": "vehicle_down", "code": "V2"},
    {"at_tick": 3, "type": "road_open", "code": "r5"},
    {"at_tick": 4, "type": "shelter_resume", "code": "S3"},
    {"at_tick": 5, "type": "vehicle_recovered", "code": "V2"},
]

# 推演事件 → (资源表, 字段, 固定目标值)；目标值为 None 时从事件载荷取值。
# 关键：模拟中“车辆离场”不在此映射内，因此推演产生的离场/承诺永不进入提升集。
INTENT_FIELDS = {
    "road_close": ("roads", "status", "closed"),
    "road_open": ("roads", "status", "open"),
    "road_set_capacity": ("roads", "capacity", None),
    "vehicle_down": ("vehicles", "status", "down"),
    "vehicle_recovered": ("vehicles", "status", "available"),
    "shelter_suspend": ("shelters", "status", "suspended"),
    "shelter_resume": ("shelters", "status", "active"),
    "shelter_set_beds": ("shelters", "beds", None),
    "fire_expand": ("grids", "threatened", 1),
    "fire_clear": ("grids", "threatened", 0),
}


def _intent_of_event(ev):
    """若该事件表达显式资源配置意图，返回 (kind, code, field, value)，否则 None。"""
    spec = INTENT_FIELDS.get(ev.get("type"))
    if not spec:
        return None
    kind, field, val = spec
    if val is None:
        val = int(ev[field])
    return kind, ev.get("code"), field, _norm(val)


# ---------------------------------------------------------------- 异常

class HttpError(Exception):
    def __init__(self, status, message, extra=None):
        self.status = status
        self.extra = extra or {}
        super().__init__(message)


class NotFound(HttpError):
    def __init__(self, m, extra=None): super().__init__(404, m, extra)


class Conflict(HttpError):
    def __init__(self, m, extra=None): super().__init__(409, m, extra)


class BadRequest(HttpError):
    def __init__(self, m, extra=None): super().__init__(400, m, extra)


def now_id(prefix):
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------- 幂等

def _idem_key(body):
    return (body or {}).get("idempotency_key") or (body or {}).get("IdempotencyKey")


def idem_begin(db, scope, key, action):
    """返回存储行（含首次响应）。key 为 None 时不做去重（None 表示每次都执行）。"""
    if not key:
        return None
    row = db.conn.execute(
        "SELECT response,status_code FROM idempotency WHERE scope=? AND idem_key=?",
        (scope, key)).fetchone()
    if row and row["status_code"] != 0:
        resp = json.loads(row["response"])
        resp["idempotency_replayed"] = True
        return {"replayed": True, "response": resp,
                "status_code": row["status_code"]}
    # status_code=0 的占位行表示上次请求在写回结果前中断 → 本次重新执行并覆盖
    if not row:
        db.conn.execute(
            "INSERT INTO idempotency(scope,idem_key,action,request_hash,response,status_code,created_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (scope, key, action, "", "", 0, time.time()))
    return {"replayed": False, "id": (scope, key)}


def idem_commit(db, token, status_code, response):
    if not token:
        return
    db.conn.execute(
        "UPDATE idempotency SET response=?, status_code=? WHERE scope=? AND idem_key=?",
        (json.dumps(response, ensure_ascii=False), status_code, *token["id"]))


# ---------------------------------------------------------------- 世界复制

def _copy_world(db, src, dst):
    """把 src 世界的全部业务表复制到 dst（dst 视为干净目标）。"""
    c = db.conn
    for table in COPY_TABLES:
        cols = [r["name"] for r in c.execute(f"PRAGMA table_info({table})")]
        src_cols = [x for x in cols if x != "id"]
        dst_cols = ["world_id"] + [x for x in src_cols if x != "world_id"]
        placeholders = ",".join("?" for _ in dst_cols)
        select_cols = ["?"] + [x for x in src_cols if x != "world_id"]
        c.execute(
            f"INSERT INTO {table}({','.join(dst_cols)}) "
            f"SELECT {','.join(select_cols)} FROM {table} WHERE world_id=?",
            (dst, src))


def _delete_world(db, world):
    c = db.conn
    for table in COPY_TABLES:
        c.execute(f"DELETE FROM {table} WHERE world_id=?", (world,))
    c.execute("DELETE FROM event_log WHERE world_id=?", (world,))
    c.execute("DELETE FROM meta WHERE key=?", (f"revision:{world}",))


# ---------------------------------------------------------------- 资源快照（三方比较用）

def _resource_snapshot(db, world):
    c = db.conn
    out = {}
    for table, fields in PROMOTABLE.items():
        out[table] = {}
        for r in c.execute(f"SELECT code,{','.join(fields)} FROM {table} WHERE world_id=?",
                           (world,)):
            d = dict(r)
            code = d.pop("code")
            # 网格用 code，统一做布尔/整型归一
            out[table][code] = {k: _norm(v) for k, v in d.items()}
    return out


def _norm(v):
    if isinstance(v, bool):
        return 1 if v else 0
    return v


# ---------------------------------------------------------------- 分支生命周期

def create_branch(db, body):
    name = (body or {}).get("name") or f"推演 {time.strftime('%m-%d %H:%M')}"
    commander = (body or {}).get("commander") or "值班员"
    token = idem_begin(db, "branch_create", _idem_key(body), "create")
    if token and token["replayed"]:
        return token["status_code"], token["response"]

    bid = now_id("br")
    base_world, branch_world = f"baseline:{bid}", bid
    live_rev = db.revision(LIVE)
    # 基线世界与分支世界都从当前实战完整复制（含已承诺状态）
    _copy_world(db, LIVE, base_world)
    _copy_world(db, LIVE, branch_world)
    db.set_meta(f"revision:{branch_world}", str(live_rev))

    db.conn.execute(
        "INSERT INTO branches(id,name,status,base_revision,world_id,baseline_world_id,"
        "tick,commander,created_at,updated_at,pending_events,last_tick_result)"
        " VALUES(?,?, 'active', ?,?,?, 0,?,?,?,?, '{}')",
        (bid, name, live_rev, branch_world, base_world, commander,
         time.time(), time.time(), json.dumps(DEFAULT_SCENARIO, ensure_ascii=False)))
    db.log_event(LIVE, "branch_created",
                 {"branch": bid, "name": name, "base_revision": live_rev,
                  "commander": commander})
    db.log_event(branch_world, "branch_created",
                 {"branch": bid, "name": name, "base_revision": live_rev})
    db.commit()
    resp = get_branch(db, bid)
    resp["idempotency_replayed"] = False
    idem_commit(db, token, 200, resp)
    db.commit()
    return 200, resp


def _get_branch_row(db, bid):
    row = db.conn.execute("SELECT * FROM branches WHERE id=?", (bid,)).fetchone()
    if not row:
        raise NotFound(f"分支不存在：{bid}")
    return row


def get_branch(db, bid):
    row = _get_branch_row(db, bid)
    d = dict(row)
    d.pop("pending_events", None)
    d.pop("last_tick_result", None)
    d["pending_events"] = json.loads(row["pending_events"])
    d["last_tick_result"] = json.loads(row["last_tick_result"])
    d["world_revision"] = db.revision(row["world_id"])
    return d


def list_branches(db):
    rows = db.conn.execute("SELECT * FROM branches ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        d = {k: r[k] for k in
             ("id", "name", "status", "base_revision", "world_id", "tick",
              "commander", "created_at", "updated_at", "promoted_at",
              "discarded_at")}
        d["live_revision"] = db.revision(LIVE)
        d["world_revision"] = db.revision(r["world_id"])
        d["stale"] = d["live_revision"] != r["base_revision"]
        out.append(d)
    return out


def discard_branch(db, bid, body):
    row = _get_branch_row(db, bid)
    token = idem_begin(db, f"branch_discard:{bid}", _idem_key(body), "discard")
    if token and token["replayed"]:
        return token["status_code"], token["response"]
    if row["status"] in ("discarded", "promoted"):
        resp = {"id": bid, "status": row["status"], "idempotency_replayed": False,
                "note": "分支已处于终态，废弃操作幂等无操作"}
        idem_commit(db, token, 200, resp)
        db.commit()
        return 200, resp
    db.conn.execute(
        "UPDATE branches SET status='discarded', discarded_at=?, updated_at=? WHERE id=?",
        (time.time(), time.time(), bid))
    # 分支世界与基线世界可被清除；分支元数据、审计保留用于历史
    _delete_world(db, row["world_id"])
    _delete_world(db, row["baseline_world_id"])
    db.log_event(LIVE, "branch_discarded", {"branch": bid, "name": row["name"]})
    db.commit()
    resp = {"id": bid, "status": "discarded", "idempotency_replayed": False}
    idem_commit(db, token, 200, resp)
    db.commit()
    return 200, resp


# ---------------------------------------------------------------- 态势视图

def _build_world_state(db, world):
    """构造指定世界的全量态势（与实战 /api/state 同构）。"""
    from server import build_state_for   # 延迟导入避免循环依赖
    return build_state_for(db, world)


def branch_state(db, bid):
    row = _get_branch_row(db, bid)
    if row["status"] == "discarded":
        raise Conflict("分支已废弃，其推演世界已清除", {"id": bid, "status": "discarded"})
    return _build_world_state(db, row["world_id"])


# ---------------------------------------------------------------- 事件入队 / 态势注入

def add_event(db, bid, body):
    """向分支注入推演事件（封路/恢复/车辆故障/避难点暂停/火线扩大等）。

    事件进入待执行队列，在下一次 tick 生效，绝不触碰实战。
    """
    row = _get_branch_row(db, bid)
    if row["status"] != "active":
        raise Conflict(f"分支状态为 {row['status']}，不能再注入事件")
    etype = (body or {}).get("type")
    if not etype:
        raise BadRequest("缺少 type")
    token = idem_begin(db, f"branch_event:{bid}", _idem_key(body), "event")
    if token and token["replayed"]:
        return token["status_code"], token["response"]
    events = json.loads(row["pending_events"])
    ev = {k: v for k, v in body.items() if k != "idempotency_key"}
    ev["at_tick"] = int(body.get("at_tick", row["tick"] + 1))
    ev["manual"] = True
    events.append(ev)
    db.conn.execute("UPDATE branches SET pending_events=?, updated_at=? WHERE id=?",
                    (json.dumps(events, ensure_ascii=False), time.time(), bid))
    db.log_event(row["world_id"], "branch_event_queued",
                 {"branch": bid, "event": ev})
    db.commit()
    resp = {"id": bid, "queued": ev, "pending_events": events,
            "idempotency_replayed": False}
    idem_commit(db, token, 200, resp)
    db.commit()
    return 200, resp


# ---------------------------------------------------------------- 连续模拟

def _apply_resource_event(db, world, ev):
    """在指定世界内应用一个资源事件（复用与实战一致的语义）。"""
    c = db.conn
    kind = ev["type"]
    code = ev.get("code")

    def get(table):
        r = c.execute(f"SELECT * FROM {table} WHERE world_id=? AND code=?",
                      (world, code)).fetchone()
        if not r:
            raise NotFound(f"{table} 中不存在 {code}")
        return dict(r)

    released = []
    if kind == "road_close":
        get("roads")
        c.execute("UPDATE roads SET status='closed' WHERE world_id=? AND code=?",
                  (world, code))
        released = _cancel_batches_on_road(db, world, code, f"道路封闭 {code}")
    elif kind == "road_open":
        get("roads")
        c.execute("UPDATE roads SET status='open' WHERE world_id=? AND code=?",
                  (world, code))
    elif kind == "vehicle_down":
        get("vehicles")
        c.execute("UPDATE vehicles SET status='down' WHERE world_id=? AND code=?",
                  (world, code))
        released = _release_scheduled(db, world, "vehicle_code=?", (code,),
                                      f"车辆故障 {code}")
    elif kind == "vehicle_recovered":
        get("vehicles")
        c.execute("UPDATE vehicles SET status='available' WHERE world_id=? AND code=?"
                  " AND status<>'departed'", (world, code))
    elif kind == "shelter_suspend":
        get("shelters")
        c.execute("UPDATE shelters SET status='suspended' WHERE world_id=? AND code=?",
                  (world, code))
        released = _release_scheduled(db, world, "shelter_code=?", (code,),
                                      f"避难点暂停接收 {code}")
    elif kind == "shelter_resume":
        get("shelters")
        c.execute("UPDATE shelters SET status='active' WHERE world_id=? AND code=?",
                  (world, code))
    elif kind == "shelter_set_beds":
        get("shelters")
        c.execute("UPDATE shelters SET beds=? WHERE world_id=? AND code=?",
                  (int(ev["beds"]), world, code))
    elif kind == "road_set_capacity":
        get("roads")
        c.execute("UPDATE roads SET capacity=? WHERE world_id=? AND code=?",
                  (max(0, int(ev["capacity"])), world, code))
    elif kind == "fire_expand":
        get("grids")
        c.execute("UPDATE grids SET threatened=1 WHERE world_id=? AND code=?",
                  (world, code))
        for rc in ev.get("close_roads", []):
            c.execute("UPDATE roads SET status='closed' WHERE world_id=? AND code=?",
                      (world, rc))
            released += _cancel_batches_on_road(db, world, rc, f"火线扩大封路 {rc}")
    elif kind == "fire_clear":
        get("grids")
        c.execute("UPDATE grids SET threatened=0 WHERE world_id=? AND code=?",
                  (world, code))
    else:
        raise BadRequest(f"未知事件类型 {kind}")
    return released


def _release_scheduled(db, world, where, params, reason):
    c = db.conn
    args = (world, *params)
    cur = c.execute(
        f"SELECT id FROM batches WHERE world_id=? AND status='scheduled' AND {where}",
        args)
    ids = [r["id"] for r in cur]
    released = []
    for bid_ in ids:
        hs = [r["household_ref"] for r in c.execute(
            "SELECT household_ref FROM assignments WHERE world_id=? AND batch_id=?",
            (world, bid_))]
        released.extend(hs)
        c.execute("DELETE FROM assignments WHERE world_id=? AND batch_id=?",
                  (world, bid_))
        c.execute("UPDATE batches SET status='cancelled' WHERE world_id=? AND id=?",
                  (world, bid_))
    if ids:
        db.log_event(world, "batch_cancelled",
                     {"batches": ids, "released": released, "reason": reason})
    return released


def _cancel_batches_on_road(db, world, road_code, reason):
    c = db.conn
    ids = []
    for r in c.execute("SELECT id,route FROM batches WHERE world_id=? AND status='scheduled'",
                       (world,)):
        if road_code in json.loads(r["route"]).get("edges", []):
            ids.append(r["id"])
    released = []
    for bid_ in ids:
        hs = [x["household_ref"] for x in c.execute(
            "SELECT household_ref FROM assignments WHERE world_id=? AND batch_id=?",
            (world, bid_))]
        released.extend(hs)
        c.execute("DELETE FROM assignments WHERE world_id=? AND batch_id=?",
                  (world, bid_))
        c.execute("UPDATE batches SET status='cancelled' WHERE world_id=? AND id=?",
                  (world, bid_))
    if ids:
        db.log_event(world, "batch_cancelled",
                     {"batches": ids, "released": released, "reason": reason})
    return released


def tick(db, bid, body):
    """推进一个（或连续多个）模拟时钟。

    每个 tick：先让 at_tick<=新时钟 的待执行事件生效（资源变化），再用规划器
    对未撤离家庭自动求解并发布调度结果、令全部待发批次离场形成承诺；
    无解家庭的受阻原因随 tick 结果保存。重复 tick 同一目标时钟幂等（不重放）。
    """
    body = body or {}
    row = _get_branch_row(db, bid)
    if row["status"] != "active":
        raise Conflict(f"分支状态为 {row['status']}，不能继续模拟")
    target = int(body.get("target_tick", row["tick"] + 1))
    auto_depart = bool(body.get("auto_depart", True))

    # 幂等：同一目标时钟（不前进）直接回放上次结果
    token = idem_begin(
        db, f"branch_tick:{bid}",
        _idem_key(body) or f"auto:{bid}:{target}", "tick")
    if token and token["replayed"]:
        return token["status_code"], token["response"]

    if target < row["tick"]:
        raise BadRequest(f"目标时钟 {target} 小于当前 {row['tick']}，时钟不能倒流")
    if target == row["tick"]:
        resp = json.loads(row["last_tick_result"]) or {}
        resp.update({"idempotency_replayed": True, "noop": True})
        idem_commit(db, token, 200, resp)
        db.commit()
        return 200, resp

    from planner import Snapshot, plan as run_plan
    world = row["world_id"]
    base_world = row["baseline_world_id"]
    # 基线（实战快照）中已离场的车辆是真实承诺，推演中永不折返
    base_departed = {r["code"] for r in db.conn.execute(
        "SELECT code FROM vehicles WHERE world_id=? AND status='departed'",
        (base_world,))}
    events = json.loads(row["pending_events"])
    intended = json.loads(row["intended_changes"] or "{}")
    steps = []
    cur = row["tick"]
    while cur < target:
        cur += 1
        # 上一 tick 推演自动离场的车辆视为已折返，重新可用（真实承诺车辆除外）
        _return_sim_vehicles(db, world, base_departed)
        due = [e for e in events if int(e["at_tick"]) <= cur]
        step_events = []
        released_all = []
        for ev in due:
            rel = _apply_resource_event(db, world, ev)
            released_all.extend(rel)
            # 仅记录用户显式表达的资源配置意图（车辆自动离场不在此列）
            intent = _intent_of_event(ev)
            if intent:
                kind, code, field, val = intent
                intended.setdefault(kind, {}).setdefault(code, {})[field] = val
            db.bump_revision_nocommit(world)
            db.log_event(world, ev["type"], {"code": ev.get("code"), "tick": cur,
                                             "released": rel})
            step_events.append(ev)
        for ev in due:
            events.remove(ev)

        # 自动规划 → 发布 →（可选）离场
        snap = Snapshot(db, world)
        result = run_plan(snap)
        all_evacuated = len(snap.to_plan) == 0
        if all_evacuated:
            # 已无待撤离家庭：视为成功完成，而非“无解空方案”
            result["feasible"] = True
        pid = now_id("plan")
        db.conn.execute(
            "INSERT INTO plans(world_id,id,name,commander,base_revision,status,payload,created_at)"
            " VALUES(?,?,?,?,?, 'candidate', ?,?)",
            (world, pid, f"推演 T{cur}", "auto-sim", db.revision(world),
             json.dumps(result, ensure_ascii=False), time.time()))

        plan_feasible = result["feasible"]
        order_id = None
        departed = []
        unassigned = result.get("unassigned", [])
        if plan_feasible and result["batches"]:
            order_id = _publish_snapshot(db, world, pid, result)
            if auto_depart:
                # 每个 tick 只离场当前最小波次：若后续 tick 发生封路/车辆故障，
                # 尚未离场的后波次会被撤回并在下个 tick 重新调度，从而体现
                # 路网/运力/收容随时间变化对调度结果的连续影响。
                departed = _depart_first_wave(db, world)
        elif not result["batches"]:
            unassigned = result.get("unassigned", [])

        db.bump_revision_nocommit(world)
        db.log_event(world, "branch_tick",
                     {"branch": bid, "tick": cur,
                      "events": [e["type"] + ":" + str(e.get("code", "")) for e in step_events],
                      "feasible": plan_feasible,
                      "batches": len(result.get("batches", [])),
                      "departed": len(departed),
                      "unassigned": [u["ref"] for u in unassigned]})
        steps.append({
            "tick": cur,
            "events": [{"type": e["type"], "code": e.get("code")} for e in step_events],
            "released_for_replan": sorted(set(released_all)),
            "plan_id": pid, "order_id": order_id,
            "feasible": plan_feasible,
            "batch_count": len(result.get("batches", [])),
            "departed_batch_count": len(departed),
            "unassigned": unassigned,
        })

    last_result = {"branch": bid, "tick": target, "steps": steps,
                   "world_revision": db.revision(world),
                   "idempotency_replayed": False}
    db.conn.execute(
        "UPDATE branches SET tick=?, pending_events=?, intended_changes=?, "
        "last_tick_result=?, updated_at=? WHERE id=?",
        (target, json.dumps(events, ensure_ascii=False),
         json.dumps(intended, ensure_ascii=False),
         json.dumps(last_result, ensure_ascii=False), time.time(), bid))
    db.commit()
    last_result.update(get_branch(db, bid))
    # 保持响应里的主体字段在前
    resp = {"branch": bid, "tick": target, "steps": steps,
            "world_revision": db.revision(world)}
    resp["branch_info"] = {k: last_result[k] for k in
                           ("id", "name", "status", "base_revision", "tick",
                            "commander") if k in last_result}
    idem_commit(db, token, 200, resp)
    db.commit()
    return 200, resp


def _publish_snapshot(db, world, plan_id, result):
    """分支世界内自动发布：替换该世界未出发批次（已离场承诺保留）。"""
    c = db.conn
    # 取消该世界所有未出发批次
    for b in c.execute(
            "SELECT id FROM batches WHERE world_id=? AND status='scheduled'",
            (world,)).fetchall():
        c.execute("DELETE FROM assignments WHERE world_id=? AND batch_id=?",
                  (world, b["id"]))
        c.execute("UPDATE batches SET status='cancelled' WHERE world_id=? AND id=?",
                  (world, b["id"]))
    c.execute("UPDATE plans SET status='superseded' WHERE world_id=? AND status='published'",
              (world,))
    c.execute("UPDATE plans SET status='published' WHERE world_id=? AND id=?",
              (world, plan_id))
    oid = now_id("order")
    c.execute(
        "INSERT INTO orders(world_id,id,plan_id,commander,revision,created_at)"
        " VALUES(?,?,?,?,?,?)",
        (world, oid, plan_id, "auto-sim", db.revision(world), time.time()))
    for b in result["batches"]:
        btid = now_id("batch")
        c.execute(
            "INSERT INTO batches(world_id,id,order_id,seq,wave,vehicle_code,grid,"
            "shelter_code,route,status) VALUES(?,?,?,?,?,?,?,?,?, 'scheduled')",
            (world, btid, oid, b["seq"], b["wave"], b["vehicle"], b["grid"],
             b["shelter"], json.dumps({"edges": b["route_edges"],
                                       "nodes": b["route_nodes"]})))
        for h in b["households"]:
            c.execute(
                "INSERT INTO assignments(world_id,batch_id,household_ref) VALUES(?,?,?)",
                (world, btid, h["ref"]))
    return oid


def _return_sim_vehicles(db, world, keep_departed):
    """推演模拟：让上一 tick 自动离场的车辆折返，重新变为可用。

    keep_departed 中的车辆是创建分支时实战已离场的真实承诺，永不折返。
    显式标记故障的车辆由事件维护其 down 状态，不在本函数内恢复——
    折返只作用于“已离场”的车辆，因此故障/可用状态都不受影响。
    """
    db.conn.execute(
        "UPDATE vehicles SET status='available' WHERE world_id=? "
        "AND status='departed'" +
        (" AND code NOT IN (%s)" % ",".join("?" * len(keep_departed))
         if keep_departed else ""),
        (world, *sorted(keep_departed)))


def _depart_first_wave(db, world):
    """令该世界最小波次的待发批次离场（模拟该 tick 完成一批撤离）。

    只离场一个波次，使后续 tick 的中断能够影响尚未出发的后波次。
    """
    c = db.conn
    row = c.execute(
        "SELECT MIN(wave) AS m FROM batches WHERE world_id=? AND status='scheduled'",
        (world,)).fetchone()
    if row["m"] is None:
        return []
    ids = [r["id"] for r in c.execute(
        "SELECT id FROM batches WHERE world_id=? AND status='scheduled' AND wave=?",
        (world, row["m"])).fetchall()]
    for btid in ids:
        b = c.execute("SELECT * FROM batches WHERE world_id=? AND id=?",
                      (world, btid)).fetchone()
        ts = time.time()
        c.execute("UPDATE batches SET status='departed', departed_at=? WHERE world_id=? AND id=?",
                  (ts, world, btid))
        c.execute("UPDATE vehicles SET status='departed' WHERE world_id=? AND code=?",
                  (world, b["vehicle_code"]))
    return ids


# ---------------------------------------------------------------- 差异（分支 vs 实战）

def _household_destinations(state):
    out = {}
    for ref, st in state.get("household_state", {}).items():
        out[ref] = {
            "status": st.get("status"),
            "shelter": st.get("shelter"),
            "wave": st.get("wave"),
            "batch": st.get("batch"),
        }
    return out


def diff(db, bid):
    row = _get_branch_row(db, bid)
    if row["status"] == "discarded":
        raise Conflict("分支已废弃，无法查看差异", {"id": bid})
    live_state = _build_world_state(db, LIVE)
    branch_state = _build_world_state(db, row["world_id"])
    base_snap = _resource_snapshot(db, row["baseline_world_id"])
    live_snap = _resource_snapshot(db, LIVE)

    # 1) 人员去向差异
    ld, bd = _household_destinations(live_state), _household_destinations(branch_state)
    people = []
    for ref in sorted(set(ld) | set(bd)):
        a, b = ld.get(ref), bd.get(ref)
        if a != b:
            hh = next((h for h in branch_state["households"] if h["ref"] == ref), None)
            people.append({"ref": ref,
                           "name": (hh or {}).get("name", ""),
                           "live": a, "branch": b})

    # 2) 资源余量差异（床位 / 车辆 / 道路状态）
    resources = []
    for code in sorted(set(live_state["capacity"]) | set(branch_state["capacity"])):
        cl = live_state["capacity"].get(code, {})
        cb = branch_state["capacity"].get(code, {})
        if (cl.get("remaining"), cl.get("committed"), cl.get("reserved"),
                cl.get("status")) != (cb.get("remaining"), cb.get("committed"),
                                      cb.get("reserved"), cb.get("status")):
            resources.append({"kind": "shelter", "code": code,
                              "name": cb.get("name") or cl.get("name"),
                              "live": {"remaining": cl.get("remaining"),
                                       "committed": cl.get("committed"),
                                       "reserved": cl.get("reserved"),
                                       "status": cl.get("status")},
                              "branch": {"remaining": cb.get("remaining"),
                                         "committed": cb.get("committed"),
                                         "reserved": cb.get("reserved"),
                                         "status": cb.get("status")}})
    for v in live_state["vehicles"]:
        vb = next((x for x in branch_state["vehicles"] if x["code"] == v["code"]), None)
        if vb and v["status"] != vb["status"]:
            resources.append({"kind": "vehicle", "code": v["code"], "name": v["name"],
                              "live": {"status": v["status"]},
                              "branch": {"status": vb["status"]}})
    for r in live_state["roads"]:
        rb = next((x for x in branch_state["roads"] if x["code"] == r["code"]), None)
        if rb and (r["status"] != rb["status"] or r["capacity"] != rb["capacity"]):
            resources.append({"kind": "road", "code": r["code"],
                              "name": f"{r['a']}↔{r['b']}",
                              "live": {"status": r["status"], "capacity": r["capacity"]},
                              "branch": {"status": rb["status"],
                                         "capacity": rb["capacity"]}})

    # 3) 受阻原因差异：对分支与实战分别基于“当前态势”现算一次规划诊断（不落库），
    #    以便并排比较此刻仍无法安置的家庭及其具体阻断原因。
    from planner import Snapshot, plan as run_plan
    def _cur_blocked(world):
        diag = run_plan(Snapshot(db, world))
        if len(Snapshot(db, world).to_plan) == 0:
            return {}  # 全部已撤离，无受阻
        return {u["ref"]: u.get("reasons", []) for u in diag.get("unassigned", [])}
    blocked_live = _cur_blocked(LIVE)
    blocked_branch = _cur_blocked(row["world_id"])
    blocked = []
    for ref in sorted(set(blocked_live) | set(blocked_branch)):
        if blocked_live.get(ref) != blocked_branch.get(ref):
            blocked.append({"ref": ref, "live": blocked_live.get(ref, []),
                            "branch": blocked_branch.get(ref, [])})

    # 4) 可提升的资源改动（分支相对基线，且当前实战仍等于基线）
    # 4) 可提升的资源配置意图（覆盖层）：仅显式设定且相对基线有净改动的字段，
    #    并标注当前实战是否已在同字段偏离基线（即提升时是否会冲突）。
    intended = _intended_overlay(row)
    promotable_changes = []
    for kind, by_code in intended.items():
        for code, fields in by_code.items():
            basevals = base_snap.get(kind, {}).get(code)
            if basevals is None:
                continue
            for field, iv in fields.items():
                iv = _norm(iv)
                bv = _norm(basevals.get(field))
                if bv == iv:
                    continue
                cur = _norm(live_snap.get(kind, {}).get(code, {}).get(field))
                promotable_changes.append({
                    "kind": kind, "code": code, "field": field,
                    "baseline": bv, "branch": iv, "live": cur,
                    "conflicts_with_live": cur != bv})

    return {
        "branch": get_branch(db, bid),
        "live_revision": db.revision(LIVE),
        "people_destinations": people,
        "resource_remaining": resources,
        "blocked_reasons": blocked,
        "promotable_changes": promotable_changes,
        "counts": {
            "people_diff": len(people),
            "resource_diff": len(resources),
            "blocked_diff": len(blocked),
            "promotable": len(promotable_changes),
        },
    }


# ---------------------------------------------------------------- 提升（三方合并）

def _intended_overlay(row):
    return json.loads(row["intended_changes"] or "{}")


def _evaluate_promotion(db, row):
    """基于“资源配置意图覆盖层”做三方比较（基线 / 当前实战 / 推演意图）。

    只评估推演中通过事件**显式设定**的资源目标值；模拟产生的车辆离场、命令、
    离场记录与已承诺占用不在覆盖层中，因而绝不可能被提升。
    返回 dict：changes / conflicts / stale / base_revision / live_revision。
    """
    base = _resource_snapshot(db, row["baseline_world_id"])
    cur = _resource_snapshot(db, LIVE)
    intended = _intended_overlay(row)
    live_rev = db.revision(LIVE)

    changes, conflicts = [], []
    for kind, by_code in intended.items():
        for code, fields in by_code.items():
            if code not in base.get(kind, {}):
                conflicts.append({"kind": kind, "code": code, "field": "*",
                                  "reason": "object_missing_since_baseline"})
                continue
            for field, iv in fields.items():
                iv = _norm(iv)
                bv = _norm(base[kind][code].get(field))
                cv = _norm(cur[kind].get(code, {}).get(field))
                if bv == iv:
                    continue  # 意图最终与基线相同（中断后已恢复）→ 净改动为空
                ch = {"kind": kind, "code": code, "field": field,
                      "baseline": bv, "branch": iv, "live": cv}
                changes.append(ch)
                if cv != bv:
                    # 实战与推演都改了同一对象同一字段 → 冲突
                    conflicts.append({**ch, "reason": "live_and_branch_both_changed"})

    # 容量安全：推演意图把床位调小到小于实战当前已承诺占用 → 冲突
    live_cap = _build_world_state(db, LIVE)["capacity"]
    for code, fields in intended.get("shelters", {}).items():
        if "beds" not in fields:
            continue
        new_beds = int(fields["beds"])
        bv = base["shelters"].get(code, {}).get("beds")
        committed = live_cap.get(code, {}).get("committed", 0)
        if bv is not None and new_beds != bv and new_beds < committed:
            conflicts.append({"kind": "shelters", "code": code, "field": "beds",
                              "baseline": bv, "branch": new_beds,
                              "live": cur["shelters"][code]["beds"],
                              "reason": "below_committed_occupancy",
                              "committed": committed})

    # 过期：创建后实战已推进；仅当确有净改动需要应用时才据此拒绝
    stale = live_rev != row["base_revision"]
    return {"changes": changes, "conflicts": conflicts, "stale": stale,
            "base_revision": row["base_revision"], "live_revision": live_rev}


def check_promotion(db, bid):
    """纯检查，不写库。返回 (ok, info)。"""
    row = _get_branch_row(db, bid)
    if row["status"] != "active":
        return False, {"ok": False, "reason": "branch_not_active",
                       "status": row["status"], "conflicts": [], "stale": False,
                       "changes": [], "base_revision": row["base_revision"],
                       "live_revision": db.revision(LIVE)}
    ev = _evaluate_promotion(db, row)
    blocking_stale = ev["stale"] and bool(ev["changes"])
    ok = (not ev["conflicts"]) and (not blocking_stale)
    info = {"ok": ok, "stale": ev["stale"], "blocking_stale": blocking_stale,
            "base_revision": ev["base_revision"], "live_revision": ev["live_revision"],
            "changes": ev["changes"], "conflicts": ev["conflicts"],
            "excluded": "推演产生的命令、离场记录、已承诺占用、规划/批次/安置一律不提升"}
    return ok, info


def promote(db, bid, body=None):
    body = body or {}
    # 幂等重放优先于终态判断，保证“成功后重试同一键”稳定回放首次结果
    token = idem_begin(db, f"branch_promote:{bid}", _idem_key(body), "promote")
    if token and token["replayed"]:
        return token["status_code"], token["response"]
    row = _get_branch_row(db, bid)
    if row["status"] == "promoted":
        raise Conflict("分支已提升，不能重复提升（如需请新建分支）",
                       {"id": bid, "status": "promoted"})
    if row["status"] == "discarded":
        raise Conflict("分支已废弃，不能提升", {"id": bid, "status": "discarded"})

    ev = _evaluate_promotion(db, row)
    blocking_stale = ev["stale"] and bool(ev["changes"])
    if ev["conflicts"] or blocking_stale:
        # 整体拒绝：不写入任何改动，并列出冲突对象与字段
        db.log_event(LIVE, "branch_promote_rejected",
                     {"branch": bid, "stale": blocking_stale,
                      "conflicts": ev["conflicts"]})
        db.commit()
        why = ("基线已过期：分支创建于修订号 %s，实战已推进到 %s" %
               (ev["base_revision"], ev["live_revision"])
               if blocking_stale else "存在实战/推演三方冲突")
        rejected = {"error": "提升被整体拒绝：" + why +
                            "。未应用任何资源改动，请基于最新实战新建分支后重试。",
                    "rejected": True, "ok": False, "stale": blocking_stale,
                    "base_revision": ev["base_revision"],
                    "live_revision": ev["live_revision"],
                    "changes": ev["changes"], "conflicts": ev["conflicts"],
                    "excluded": "推演产生的命令、离场记录、已承诺占用一律不带入"}
        idem_commit(db, token, 409, rejected)
        db.commit()
        return 409, rejected

    # 全部干净 → 在单事务内仅应用资源配置意图（绝不带命令/离场/承诺）
    c = db.conn
    applied = []
    for ch in ev["changes"]:
        table, code, field = ch["kind"], ch["code"], ch["field"]
        c.execute(f"UPDATE {table} SET {field}=? WHERE world_id=? AND code=?",
                  (ch["branch"], LIVE, code))
        applied.append(ch)
    c.execute("UPDATE branches SET status='promoted', promoted_at=?, updated_at=? WHERE id=?",
              (time.time(), time.time(), bid))
    rev = db.bump_revision(LIVE)
    db.log_event(LIVE, "branch_promoted",
                 {"branch": bid, "name": row["name"],
                  "commander": body.get("commander", row["commander"]),
                  "base_revision": row["base_revision"], "applied": applied})
    db.commit()
    resp = {"promoted": True, "branch": bid, "revision": rev,
            "applied_changes": applied, "applied_count": len(applied),
            "excluded": "推演产生的命令、离场记录、已承诺占用、规划/批次/安置未带入",
            "idempotency_replayed": False}
    idem_commit(db, token, 200, resp)
    db.commit()
    return 200, resp


# ---------------------------------------------------------------- 分支审计

def branch_audit(db, bid=None, limit=200):
    c = db.conn
    if bid:
        row = _get_branch_row(db, bid)
        worlds = (LIVE, bid, row["baseline_world_id"])
        # 废弃分支的世界已清除：仅查仍保留的实战侧审计（创建/提升/拒绝/废弃）
        rows = c.execute(
            "SELECT * FROM event_log WHERE world_id=? OR world_id=? OR world_id=? "
            "ORDER BY id DESC LIMIT ?", (*worlds, limit)).fetchall()
    else:
        rows = c.execute(
            "SELECT * FROM event_log WHERE kind LIKE 'branch%' ORDER BY id DESC LIMIT ?",
            (limit,)).fetchall()
    out = []
    for r in rows:
        out.append({"id": r["id"], "ts": r["ts"], "world": r["world_id"],
                    "kind": r["kind"], "detail": json.loads(r["detail"]),
                    "revision": r["revision"]})
    return out
