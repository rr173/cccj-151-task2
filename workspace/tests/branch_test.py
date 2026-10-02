"""推演分支子系统端到端验证（独立端口 + 临时库）。

覆盖：隔离复制、连续模拟、上下文/差异、干净提升（不带离场/承诺）、
三方冲突整体拒绝并列字段、过期基线整体拒绝、幂等、废弃、重启恢复。
"""
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.join(HERE, "..", "backend")
PORT = "8097"
BASE = f"http://127.0.0.1:{PORT}"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def call(m, p, b=None):
    data = json.dumps(b).encode() if b is not None else None
    r = urllib.request.Request(BASE + p, data=data, method=m,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def start(db_path):
    env = dict(os.environ, EVAC_PORT=PORT, EVAC_DB=db_path, EVAC_NO_SEED="0")
    p = subprocess.Popen([sys.executable, os.path.join(BACKEND, "server.py")],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            call("GET", "/api/state")
            return p
        except Exception:
            time.sleep(0.15)
    raise RuntimeError("服务启动失败")


def main():
    tmp = tempfile.mkdtemp(prefix="evac_branch_")
    db_path = os.path.join(tmp, "t.db")
    proc = start(db_path)
    uniq = str(int(time.time() * 1000))[-7:]
    try:
        print("【1】创建分支：实战快照隔离复制，列表/状态/基线修订号")
        _, s0 = call("GET", "/api/state")
        st, br = call("POST", "/api/branches",
                      {"name": "扩r2推演", "commander": "赵值班",
                       "idempotency_key": f"create-{uniq}"})
        A = br["id"]
        check("创建成功并记录 base_revision=0", st == 200 and br["base_revision"] == 0)
        # 幂等重放：同键返回同一分支
        st2, br2 = call("POST", "/api/branches",
                        {"name": "扩r2推演", "idempotency_key": f"create-{uniq}"})
        check("创建幂等：同键返回同一分支", st2 == 200 and br2["id"] == A)
        st, bs = call("GET", f"/api/branches/{A}/state")
        check("分支世界复制了 11 户/11 路/5 车",
              len(bs["households"]) == 11 and len(bs["roads"]) == 11
              and len(bs["vehicles"]) == 5)
        check("分支状态 world 字段标识分支世界", bs["world"] == A)
        st, lst = call("GET", "/api/branches")
        check("分支列表含该分支且标记未过期",
              any(x["id"] == A and x["stale"] is False for x in lst))

        print("【2】连续模拟：注入剧本并推进 6 个 tick，实战零污染")
        call("POST", f"/api/branches/{A}/events",
             {"type": "road_set_capacity", "code": "r2", "capacity": 2,
              "at_tick": 1, "idempotency_key": f"ev-{uniq}"})
        st, tk = call("POST", f"/api/branches/{A}/tick", {"target_tick": 6})
        check("推进到 T6", st == 200 and tk["tick"] == 6 and len(tk["steps"]) == 6)
        feas = [(x["tick"], x["feasible"]) for x in tk["steps"]]
        check("中断初期不可行、路网/收容恢复后恢复可行",
              feas[0][1] is False and any(f for _, f in feas[2:5]), feas)
        # tick 幂等：重复推进同一目标时钟不新增结果
        st, tk2 = call("POST", f"/api/branches/{A}/tick", {"target_tick": 6})
        check("tick 幂等：同目标时钟为 noop/重放",
              st == 200 and (tk2.get("noop") or tk2.get("idempotency_replayed")))
        _, live = call("GET", "/api/state")
        check("实战修订号仍为 0（推演未触碰实战）", live["revision"] == 0)
        check("实战 r5 仍开放、r2 容量仍为 1",
              next(r for r in live["roads"] if r["code"] == "r5")["status"] == "open"
              and next(r for r in live["roads"] if r["code"] == "r2")["capacity"] == 1)
        check("实战无任何推演产生的批次/命令/离场",
              len(live["batches"]) == 0 and len(live["orders"]) == 0
              and all(v["status"] == "available" for v in live["vehicles"]))

        print("【3】并排差异：人员去向/资源余量/受阻原因 + 可提升项")
        st, d = call("GET", f"/api/branches/{A}/diff")
        check("差异含人员去向与资源余量",
              d["counts"]["people_diff"] >= 0 and "resource_remaining" in d)
        promotable = {(c["kind"], c["code"], c["field"]): c
                      for c in d["promotable_changes"]}
        check("r2 容量是唯一净资源改动（临时中断已恢复回基线）",
              list(promotable) == [("roads", "r2", "capacity")],
                  list(promotable))
        check("r2 改动不与实战冲突", promotable[("roads", "r2", "capacity")]
              ["conflicts_with_live"] is False)

        print("【4】干净提升：只应用 r2 容量，不带命令/离场/承诺")
        st, prom = call("POST", f"/api/branches/{A}/promote",
                        {"idempotency_key": f"prom-{uniq}"})
        applied = [(x["kind"], x["code"], x["field"]) for x in
                   prom.get("applied_changes", [])]
        check("提升成功且仅应用 r2.capacity",
              st == 200 and applied == [("roads", "r2", "capacity")], applied)
        _, live = call("GET", "/api/state")
        check("实战 r2 容量更新为 2、修订号前进",
              next(r for r in live["roads"] if r["code"] == "r2")["capacity"] == 2
              and live["revision"] == 1)
        check("推演车辆离场/故障未带入实战",
              all(v["status"] == "available" for v in live["vehicles"]))
        check("推演命令/批次/安置未带入实战",
              len(live["batches"]) == 0 and len(live["orders"]) == 0
              and all(stt["status"] == "unplanned"
                      for stt in live["household_state"].values()))
        # 幂等重放
        st, prom2 = call("POST", f"/api/branches/{A}/promote",
                         {"idempotency_key": f"prom-{uniq}"})
        check("提升幂等重放返回首次结果",
              st == 200 and prom2.get("applied_count") == 1)
        _, live = call("GET", "/api/state")
        check("重放未重复推进实战修订号", live["revision"] == 1)
        # 已提升分支不能再改提升（换新键应被拒）
        st, again = call("POST", f"/api/branches/{A}/promote",
                         {"idempotency_key": f"prom2-{uniq}"})
        check("已提升分支二次提升被拒", st == 409 and again.get("status") == "promoted")

        print("【5】三方冲突：实战与推演改同一对象同一字段 → 整体拒绝并列字段")
        _, brc = call("POST", "/api/branches",
                      {"name": "冲突分支", "idempotency_key": f"cc-{uniq}"})
        C = brc["id"]
        call("POST", "/api/import/roads",
             [{"code": "r3", "a": "G2", "b": "G3", "length": 2.4, "capacity": 3}])
        call("POST", f"/api/branches/{C}/events",
             {"type": "road_set_capacity", "code": "r3", "capacity": 5, "at_tick": 1})
        call("POST", f"/api/branches/{C}/tick", {"target_tick": 1})
        st, rj = call("POST", f"/api/branches/{C}/promote",
                      {"idempotency_key": f"cp-{uniq}"})
        conf = rj.get("conflicts", [])
        check("冲突时返回 409 整体拒绝", st == 409 and rj.get("rejected"))
        check("列出冲突对象与字段（roads/r3/capacity）含三方取值",
              any(c["kind"] == "roads" and c["code"] == "r3"
                  and c["field"] == "capacity"
                  and c["baseline"] == 2 and c["live"] == 3 and c["branch"] == 5
                  for c in conf), conf)
        _, live = call("GET", "/api/state")
        check("被拒后实战 r3 保持为 3，未写入任何改动",
              next(r for r in live["roads"] if r["code"] == "r3")["capacity"] == 3)

        print("【6】过期基线：实战已推进，即使改动不同对象也整体拒绝")
        _, bre = call("POST", "/api/branches",
                      {"name": "过期分支", "idempotency_key": f"ec-{uniq}"})
        E = bre["id"]
        call("POST", "/api/events", {"type": "road_close", "code": "r1"})
        call("POST", f"/api/branches/{E}/events",
             {"type": "shelter_set_beds", "code": "S1", "beds": 100, "at_tick": 1})
        call("POST", f"/api/branches/{E}/tick", {"target_tick": 1})
        st, rj = call("POST", f"/api/branches/{E}/promote",
                      {"idempotency_key": f"ep-{uniq}"})
        check("过期基线返回 409 且 stale=true、无字段冲突",
              st == 409 and rj.get("stale") is True and rj.get("conflicts") == [])
        _, live = call("GET", "/api/state")
        check("被拒后 S1 床位保持 80",
              next(x for x in live["shelters"] if x["code"] == "S1")["beds"] == 80)

        print("【7】废弃幂等且终态封闭")
        st, d1 = call("POST", f"/api/branches/{E}/discard",
                      {"idempotency_key": f"dd-{uniq}"})
        st, d2 = call("POST", f"/api/branches/{E}/discard",
                      {"idempotency_key": f"dd-{uniq}"})
        check("废弃成功且幂等",
              d1["status"] == "discarded" and d2["status"] == "discarded")
        _, info = call("GET", f"/api/branches/{E}")
        check("分支状态持久为 discarded", info["status"] == "discarded")
        check("废弃后不能再模拟/看态势/提升",
              call("POST", f"/api/branches/{E}/tick", {"target_tick": 2})[0] == 409
              and call("GET", f"/api/branches/{E}/state")[0] == 409
              and call("POST", f"/api/branches/{E}/promote", {})[0] == 409)

        print("【8】审计记录完整")
        _, au = call("GET", f"/api/branches/{A}/audit")
        kinds = {e["kind"] for e in au}
        check("审计含创建/模拟/提升",
              {"branch_created", "branch_tick", "branch_promoted"} <= kinds)

        print("【9】重启后恢复分支历史、差异与审计")
        orders_before = live["revision"]
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        proc = start(db_path)
        _, lst = call("GET", "/api/branches")
        by_id = {x["id"]: x for x in lst}
        check("三个分支元数据恢复",
              {by_id[A]["status"], by_id[C]["status"], by_id[E]["status"]}
              == {"promoted", "active", "discarded"})
        check("分支 tick 时钟恢复（A=6）", by_id[A]["tick"] == 6)
        st, bs = call("GET", f"/api/branches/{A}/state")
        dep = sum(1 for stt in bs["household_state"].values()
                  if stt["status"] == "departed")
        check("分支推演结果恢复：人员离场记录仍在", dep == 11)
        st, d = call("GET", f"/api/branches/{A}/diff")
        check("差异接口重启后可用", st == 200 and "people_destinations" in d)
        _, au = call("GET", f"/api/branches/{A}/audit")
        check("审计记录重启后恢复",
              {"branch_created", "branch_tick", "branch_promoted"}
              <= {e["kind"] for e in au})
        _, live = call("GET", "/api/state")
        check("实战提升结果（r2=2、修订号）重启后保持",
              next(r for r in live["roads"] if r["code"] == "r2")["capacity"] == 2
              and live["revision"] == orders_before)

    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()

    print(f"\n结果：{PASS} 通过，{FAIL} 失败")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
