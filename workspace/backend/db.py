"""SQLite 持久化层（多世界：实战 live + 每个推演分支独立世界）。

进程重启后，已发布命令、出发记录、资源状态、剩余容量以及全部推演分支
（基线、分支世界、待执行剧本、tick 结果、提升记录、审计）都从此文件恢复。
所有写操作在 server 层的全局锁内串行执行；每个世界各自维护修订号，
分支推演递增其私有修订号，绝不触碰实战修订号与实战数据。
"""
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 受威胁网格（也作为路网节点）。world_id='live' 为实战，其余为分支世界
CREATE TABLE IF NOT EXISTS grids(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  world_id   TEXT NOT NULL DEFAULT 'live',
  code       TEXT NOT NULL,
  name       TEXT NOT NULL,
  x          REAL NOT NULL,
  y          REAL NOT NULL,
  threatened INTEGER NOT NULL DEFAULT 1,
  UNIQUE(world_id, code)
);

-- 避难点
CREATE TABLE IF NOT EXISTS shelters(
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  world_id     TEXT NOT NULL DEFAULT 'live',
  code         TEXT NOT NULL,
  name         TEXT NOT NULL,
  x            REAL NOT NULL,
  y            REAL NOT NULL,
  beds         INTEGER NOT NULL,
  accessible   INTEGER NOT NULL DEFAULT 0,
  pets_allowed INTEGER NOT NULL DEFAULT 1,
  status       TEXT NOT NULL DEFAULT 'active',
  UNIQUE(world_id, code)
);

-- 道路（无向边）
CREATE TABLE IF NOT EXISTS roads(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  world_id TEXT NOT NULL DEFAULT 'live',
  code     TEXT NOT NULL,
  a        TEXT NOT NULL,
  b        TEXT NOT NULL,
  length   REAL NOT NULL DEFAULT 1,
  capacity INTEGER NOT NULL DEFAULT 1,
  status   TEXT NOT NULL DEFAULT 'open',
  UNIQUE(world_id, code)
);

-- 车辆
CREATE TABLE IF NOT EXISTS vehicles(
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  world_id   TEXT NOT NULL DEFAULT 'live',
  code       TEXT NOT NULL,
  name       TEXT NOT NULL,
  seats      INTEGER NOT NULL,
  accessible INTEGER NOT NULL DEFAULT 0,
  status     TEXT NOT NULL DEFAULT 'available',
  UNIQUE(world_id, code)
);

-- 家庭（不可拆分单元）。ref 为外部户号，同一世界内重复导入按 ref 去重
CREATE TABLE IF NOT EXISTS households(
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  world_id TEXT NOT NULL DEFAULT 'live',
  ref      TEXT NOT NULL,
  name     TEXT NOT NULL DEFAULT '',
  grid     TEXT NOT NULL,
  members  INTEGER NOT NULL,
  mobility INTEGER NOT NULL DEFAULT 0,
  pets     INTEGER NOT NULL DEFAULT 0,
  contact  TEXT NOT NULL DEFAULT '',
  UNIQUE(world_id, ref)
);

-- 候选方案 / 已发布方案
CREATE TABLE IF NOT EXISTS plans(
  id            TEXT PRIMARY KEY,
  world_id      TEXT NOT NULL DEFAULT 'live',
  name          TEXT NOT NULL,
  commander     TEXT NOT NULL DEFAULT '',
  base_revision INTEGER NOT NULL,
  status        TEXT NOT NULL DEFAULT 'candidate',
  payload       TEXT NOT NULL,
  created_at    REAL NOT NULL
);

-- 撤离令
CREATE TABLE IF NOT EXISTS orders(
  id         TEXT PRIMARY KEY,
  world_id   TEXT NOT NULL DEFAULT 'live',
  plan_id    TEXT NOT NULL,
  commander  TEXT NOT NULL DEFAULT '',
  revision   INTEGER NOT NULL,
  created_at REAL NOT NULL
);

-- 撤离批次
CREATE TABLE IF NOT EXISTS batches(
  id           TEXT PRIMARY KEY,
  world_id     TEXT NOT NULL DEFAULT 'live',
  order_id     TEXT NOT NULL,
  seq          INTEGER NOT NULL,
  wave         INTEGER NOT NULL,
  vehicle_code TEXT NOT NULL,
  grid         TEXT NOT NULL,
  shelter_code TEXT NOT NULL,
  route        TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'scheduled',
  departed_at  REAL
);

-- 家庭→批次 安置记录
CREATE TABLE IF NOT EXISTS assignments(
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  world_id      TEXT NOT NULL DEFAULT 'live',
  batch_id      TEXT NOT NULL,
  household_ref TEXT NOT NULL,
  UNIQUE(world_id, household_ref, batch_id)
);

-- 事件审计日志（按世界隔离；分支的 branch_created / tick 等也记录于此）
CREATE TABLE IF NOT EXISTS event_log(
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  world_id  TEXT NOT NULL DEFAULT 'live',
  ts        REAL NOT NULL,
  kind      TEXT NOT NULL,
  detail    TEXT NOT NULL,
  revision  INTEGER NOT NULL
);

-- 推演分支元数据
CREATE TABLE IF NOT EXISTS branches(
  id                 TEXT PRIMARY KEY,           -- 同时作为分支世界的 world_id
  name               TEXT NOT NULL,
  status             TEXT NOT NULL DEFAULT 'active', -- active|promoted|discarded
  base_revision      INTEGER NOT NULL,           -- 创建时实战修订号（提升三方基线）
  world_id           TEXT NOT NULL,
  baseline_world_id  TEXT NOT NULL,              -- 不可变基线世界 baseline:<id>
  tick               INTEGER NOT NULL DEFAULT 0,
  commander          TEXT NOT NULL DEFAULT '',
  pending_events     TEXT NOT NULL DEFAULT '[]', -- 待执行剧本（JSON）
  intended_changes   TEXT NOT NULL DEFAULT '{}', -- 已应用的资源配置意图覆盖层（JSON）
  last_tick_result   TEXT NOT NULL DEFAULT '{}', -- 最近一次模拟结果（含受阻诊断）
  created_at         REAL NOT NULL,
  updated_at         REAL NOT NULL,
  promoted_at        REAL,
  discarded_at       REAL
);

-- 幂等键去重记录（创建/模拟/提升/废弃均可安全重试）
CREATE TABLE IF NOT EXISTS idempotency(
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  scope       TEXT NOT NULL,
  idem_key    TEXT NOT NULL,
  action      TEXT NOT NULL DEFAULT '',
  request_hash TEXT NOT NULL DEFAULT '',
  response    TEXT NOT NULL DEFAULT '',
  status_code INTEGER NOT NULL DEFAULT 0,        -- 0=占位（执行中崩溃，可重做）
  created_at  REAL NOT NULL,
  UNIQUE(scope, idem_key)
);

CREATE INDEX IF NOT EXISTS idx_batches_world ON batches(world_id, status);
CREATE INDEX IF NOT EXISTS idx_assign_world ON assignments(world_id, batch_id);
CREATE INDEX IF NOT EXISTS idx_events_world ON event_log(world_id, id);
CREATE INDEX IF NOT EXISTS idx_plans_world ON plans(world_id);
"""


class DB:
    def __init__(self, path):
        self.path = path
        d = os.path.dirname(os.path.abspath(path))
        os.makedirs(d, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._migrate_legacy_worlds()  # 旧库（无 world_id）先补列，再建新结构/索引
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()
        if self.get_meta("revision:live") is None:
            # 旧库把修订号存在 meta['revision']：继承为实战世界修订号
            old = self.get_meta("revision")
            self.set_meta("revision:live", old if old is not None else "0")

    def _table_exists(self, name):
        return self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone() is not None

    def _columns(self, table):
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate_legacy_worlds(self):
        """把单世界旧库升级为多世界：给缺失的业务表补 world_id='live' 列。

        在 executescript 之前执行，确保后续新建的 world 维度索引可用；
        旧数据整体归属实战世界，历史命令/出发记录/审计无损保留。
        """
        if not self._table_exists("meta"):
            return  # 全新库，SCHEMA 会建全部表
        for t in ("grids", "shelters", "roads", "vehicles", "households",
                  "plans", "orders", "batches", "assignments", "event_log"):
            if self._table_exists(t) and "world_id" not in self._columns(t):
                self.conn.execute(
                    f"ALTER TABLE {t} ADD COLUMN world_id TEXT NOT NULL DEFAULT 'live'")

    def _migrate(self):
        """对已存在的旧库补齐后加列（幂等）。"""
        have = {r["name"] for r in self.conn.execute("PRAGMA table_info(branches)")}
        if "branches" in {t[0] for t in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}:
            if "intended_changes" not in have:
                self.conn.execute(
                    "ALTER TABLE branches ADD COLUMN intended_changes TEXT NOT NULL DEFAULT '{}'")

    # ---- meta / revision（按世界）----
    def get_meta(self, key):
        r = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
        self.conn.commit()

    def revision(self, world="live"):
        return int(self.get_meta(f"revision:{world}") or "0")

    def bump_revision(self, world="live"):
        v = self.revision(world) + 1
        self.set_meta(f"revision:{world}", v)
        return v

    def bump_revision_nocommit(self, world="live"):
        """仅推进修订号而不提交：供需要把多个步骤包进单一事务的调用方，
        由其在最后统一 commit（保证整段原子、崩溃可安全重做）。"""
        v = self.revision(world) + 1
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"revision:{world}", v))
        return v

    def log_event(self, world, kind, detail):
        self.conn.execute(
            "INSERT INTO event_log(world_id,ts,kind,detail,revision) VALUES(?,?,?,?,?)",
            (world, time.time(), kind,
             detail if isinstance(detail, str) else json_dumps(detail),
             self.revision(world)),
        )

    def commit(self):
        self.conn.commit()


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)
