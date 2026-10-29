# -*- coding: utf-8 -*-
"""
数据存储
================================================================================
    Storage     SQLite（WAL）主存储 + CSV 实时副本（宽表）+ 多分辨率聚合表

★ 数据格式（本版按用户要求改为宽表）
--------------------------------------------------------------------------------
CSV 实时副本：**一行一轮**，时间列严格等间隔（= 采样间隔）

    iso_time,round_id,v1,i1,v2,i2,v3,i3,v4,i4
    2026-10-12 14:23:37,1,1.4231,9.87e-05,1.4203,9.88e-05,...
    2026-10-12 14:23:39,2,1.4233,9.87e-05,1.4201,9.86e-05,...

  · 第 1 列 iso_time：**只到秒**，且严格按采样间隔递增
    （用"本轮网格时刻" t_grid0 + n×interval，不是各点的物理测量时刻）
  · 第 2 列 round_id：轮号，在**同一个数据库文件（同一天）内连续递增**，
    不随程序重启重置 —— 所以重启后的数据也能按 round_id 排序
  · 后 8 列：v1,i1,v2,i2,v3,i3,v4,i4（通道数可变，由 nch 决定）
  · 某轮某项读数失败 → 该格留空（不是写 0，避免误判）
  · 某轮整体失败 → 仍写一行（数据列全空），保证时间列**不断档**

SQLite 主存储保留完整信息（追溯用）：
  samples.ts        每个测量点的**物理时刻**（精确到微秒）
  samples.grid_ts   该点所属轮次的**网格时刻**（严格等间隔）
  samples.run_id    第几次运行会话（每启动一次 main.py 就是一个 run）
  samples.quality   0=正常 1=可疑 2=无效（读数失败）
  runs / events     运行记录与事件日志

其它设计（对应方案文档 §6/§13）：
  · 按天分目录：<root>/YYYY-MM-DD/YYYY-MM-DD.db + _samples.csv
  · 文件名零填充 → 字符串排序 == 时间排序
  · WAL 模式：断电/崩溃不损坏（最多回滚最后一个未提交事务）
  · 每轮 commit 一次 + CSV flush → 边测边落盘
  · 写入时顺手更新聚合表（1 分钟 / 1 小时）→ 7 天视图查询 <10 ms
"""

import csv
import json
import os
import sqlite3
import time
from datetime import datetime

HEAD_COLS = ["iso_time", "round_id"]


def _iso_sec(ts):
    """只到秒（用户要求的时间列精度）。"""
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


def _iso_ms(ts):
    d = datetime.fromtimestamp(ts)
    return d.strftime("%Y-%m-%d %H:%M:%S.") + "%03d" % (d.microsecond // 1000)


def _fmt_val(v):
    if v is None:
        return ""
    if isinstance(v, float):
        return "%.6g" % v
    return str(v)


class Storage:
    """按天分库的存储（宽表 CSV + 长表 SQLite）。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS samples (
        ts       REAL NOT NULL,      -- 物理测量时刻
        grid_ts  REAL,               -- 所属轮次的网格时刻（等间隔）
        run_id   INTEGER,
        round_id INTEGER,
        ch       INTEGER,
        mode     TEXT,
        value    REAL,
        unit     TEXT,
        quality  INTEGER DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS runs (
        id          INTEGER PRIMARY KEY,
        start_ts    REAL,
        end_ts      REAL,
        config_json TEXT
    );

    CREATE TABLE IF NOT EXISTS events (
        ts    REAL,
        level TEXT,
        src   TEXT,
        msg   TEXT
    );

    -- 每轮一行（含"整体失败的空轮"）—— 让宽表能被完整重建、校验才准确
    CREATE TABLE IF NOT EXISTS round_log (
        grid_ts  REAL PRIMARY KEY,
        round_id INTEGER,
        run_id   INTEGER,
        n_ok     INTEGER,
        n_bad    INTEGER
    );

    -- 环境数据（温度/湿度）：**与测量同一轮采样**，带同一个 grid_ts / round_id
    CREATE TABLE IF NOT EXISTS env (
        ts       REAL,               -- 实际读取时刻
        grid_ts  REAL,               -- ★ 所属轮次的网格时刻（与 samples 对齐）
        run_id   INTEGER,
        round_id INTEGER,            -- ★ 轮号（与 samples.csv 的 round_id 一致）
        sensor   TEXT,
        ch       INTEGER,
        value    REAL,
        unit     TEXT,
        quality  INTEGER DEFAULT 0
    );

    -- ★ 温度宽表（一行一轮）：与 samples 用**同一个 grid_ts**，
    --   等价于 _samples.csv 里的 t1/t2/dt 三列，方便 SQL 直接和电学量按轮关联
    CREATE TABLE IF NOT EXISTS env_round (
        grid_ts  REAL PRIMARY KEY,
        run_id   INTEGER,
        round_id INTEGER,
        t1       REAL,
        t2       REAL,
        dt       REAL
    );

    CREATE TABLE IF NOT EXISTS agg_1min (
        bucket INTEGER, ch INTEGER, mode TEXT,
        v_min REAL, v_max REAL, v_avg REAL, n INTEGER,
        PRIMARY KEY (bucket, ch, mode)
    );    CREATE TABLE IF NOT EXISTS agg_1hour (
        bucket INTEGER, ch INTEGER, mode TEXT,
        v_min REAL, v_max REAL, v_avg REAL, n INTEGER,
        PRIMARY KEY (bucket, ch, mode)
    );
    """

    # 索引单独建：必须在 _migrate() 补齐新列之后执行，
    # 否则老库（缺 grid_ts 列）会因为 "no such column" 报错。
    INDEXES = """
    CREATE INDEX IF NOT EXISTS idx_samples_ts      ON samples(ts);
    CREATE INDEX IF NOT EXISTS idx_samples_grid    ON samples(grid_ts);
    CREATE INDEX IF NOT EXISTS idx_samples_ch_mode ON samples(ch, mode, ts);
    CREATE INDEX IF NOT EXISTS idx_env_ts          ON env(ts);
    """

    def __init__(self, root, write_csv=True, nch=4, env_channels=None, verbose=True):
        self.root = os.path.abspath(root)
        self.write_csv = write_csv
        self.nch = int(nch)
        self.env_channels = list(env_channels or [])
        self.verbose = verbose

        # 宽表列：iso_time, round_id, v1, i1, v2, i2, ... , t1, t2, dt
        # ★ 温度与电学量**同一轮、同一时间戳**，所以合并在同一个宽表里，
        #   自变量统一为 iso_time（一行 = 一轮 = 一个时刻的全部数据）。
        self.csv_cols = list(HEAD_COLS)
        for ch in range(1, self.nch + 1):
            self.csv_cols += ["v%d" % ch, "i%d" % ch]
        for ch in self.env_channels:
            self.csv_cols.append("t%d" % ch)
        if len(self.env_channels) >= 2:
            self.csv_cols.append("dt")

        self.con = None
        self.db = None
        self.csv_file = None
        self.csv_writer = None
        self.env_file = None
        self.env_writer = None
        self.day = None

        self.run_id = None
        self.round_id = 0
        self.n_written = 0
        self.n_csv = 0
        self.n_env = 0
        self._pending_agg = {}

        # 环境数据宽表列：iso_time, round_id, t1, t2, ..., dt（温差）
        self.env_cols = ["iso_time", "round_id"]
        for ch in self.env_channels:
            self.env_cols.append("t%d" % ch)
        if len(self.env_channels) >= 2:
            self.env_cols.append("dt")

        os.makedirs(self.root, exist_ok=True)
        self._rollover(force=True)

    # ------------------------------------------------------------------
    #  按天切换
    # ------------------------------------------------------------------
    def _day_dir(self, day):
        return os.path.join(self.root, day)

    def _rollover(self, force=False, ts=None):
        day = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else \
            datetime.now().strftime("%Y-%m-%d")
        if not force and day == self.day:
            return False

        self._close_files()
        self.day = day
        d = self._day_dir(day)
        os.makedirs(d, exist_ok=True)

        db_path = os.path.join(d, "%s.db" % day)
        self.con = sqlite3.connect(db_path, timeout=10.0)
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.executescript(self.SCHEMA)
        self.con.commit()
        self._migrate()                        # ① 先给老库补列
        self.con.executescript(self.INDEXES)   # ② 再建索引（依赖新列）
        self.con.commit()
        self.db = db_path

        # round_id 在同一天内连续递增（不随重启重置）
        r = self.con.execute("SELECT MAX(round_id) FROM samples").fetchone()
        base = r[0] if r and r[0] else 0
        self.round_id = int(base)
        self._round_base = int(base)

        if self.write_csv:
            csv_path = os.path.join(d, "%s_samples.csv" % day)
            # ★ 表头与当前列不一致（例如旧版本 11 列、新增温度后 13 列）
            #   → 把旧文件归档，绝不能让新旧格式混在同一个文件里
            if os.path.exists(csv_path) and os.path.getsize(csv_path) > 0:
                try:
                    with open(csv_path, "r", encoding="utf-8") as f:
                        old_head = (f.readline() or "").strip()
                    if old_head and old_head.split(",") != self.csv_cols:
                        bak = csv_path.replace(".csv", "_oldformat.csv")
                        n = 1
                        while os.path.exists(bak):
                            bak = csv_path.replace(".csv", "_oldformat%d.csv" % n)
                            n += 1
                        os.replace(csv_path, bak)
                        if self.verbose:
                            print("      [%s] 表头已变更（旧 %d 列 / 新 %d 列），"
                                  "旧文件归档为 %s"
                                  % (day, len(old_head.split(",")),
                                     len(self.csv_cols), os.path.basename(bak)))
                except Exception:
                    pass

            new = (not os.path.exists(csv_path)) or os.path.getsize(csv_path) == 0
            self.csv_file = open(csv_path, "a", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
            if new:
                self.csv_writer.writerow(self.csv_cols)
                self.csv_file.flush()
            self.n_csv = self._count_csv_rows(csv_path)

            # ★ 不再创建 _env.csv：v0.4.0 起温度已并入 _samples.csv 的
            #   t1/t2/dt 三列（同一行、同一时间自变量），单独的环境 CSV 已废弃。
            #   历史遗留的 _env.csv 保留不动（里面的旧数据仍然有效）。
            legacy = os.path.join(d, "%s_env.csv" % day)
            if os.path.exists(legacy) and os.path.getsize(legacy) > 64 and self.verbose:
                print("      (提示: 发现旧格式 %s，其中数据已合并进 %s_samples.csv，"
                      "确认后可自行删除)" % (os.path.basename(legacy), day))

        if self.verbose:
            print("      存储: %s  (宽表 %d 列, 已有轮号到 %d)"
                  % (db_path, len(self.csv_cols), self.round_id))
        return True

    def _migrate(self):
        """老库补列（samples 的 grid_ts、env 的 grid_ts/round_id）。

        注意：CREATE TABLE IF NOT EXISTS **不会**修改已存在的表，
        所以每次加新列都必须在这里补，否则新代码 INSERT 会一直报错。
        """
        try:
            cols = [r[1] for r in self.con.execute("PRAGMA table_info(samples)")]
            if "grid_ts" not in cols:
                self.con.execute("ALTER TABLE samples ADD COLUMN grid_ts REAL")
                self.con.execute(
                    "UPDATE samples SET grid_ts = ts WHERE grid_ts IS NULL")
                self.con.commit()
                if self.verbose:
                    print("      (已给旧库 samples 补上 grid_ts 列)")

            ecols = [r[1] for r in self.con.execute("PRAGMA table_info(env)")]
            added = []
            if ecols and "grid_ts" not in ecols:
                self.con.execute("ALTER TABLE env ADD COLUMN grid_ts REAL")
                self.con.execute("UPDATE env SET grid_ts = ts WHERE grid_ts IS NULL")
                added.append("grid_ts")
            if ecols and "round_id" not in ecols:
                self.con.execute("ALTER TABLE env ADD COLUMN round_id INTEGER")
                added.append("round_id")
            if added:
                self.con.commit()
                if self.verbose:
                    print("      (已给旧库 env 补上 %s 列)" % "、".join(added))
        except Exception as e:
            if self.verbose:
                print("      [!] 迁移出错: %s" % str(e)[:80])

    def _close_files(self):
        if self.con:
            try:
                self._flush_agg()
                self.con.commit()
                self.con.close()
            except Exception:
                pass
            self.con = None
        for attr in ("csv_file", "env_file"):
            f = getattr(self, attr, None)
            if f:
                try:
                    f.flush()
                    f.close()
                except Exception:
                    pass
                setattr(self, attr, None)
        self.csv_writer = None
        self.env_writer = None

    @staticmethod
    def _count_csv_rows(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return max(0, sum(1 for _ in f) - 1)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    #  轮号
    # ------------------------------------------------------------------
    @property
    def round_base(self):
        return getattr(self, "_round_base", 0)

    def begin_round(self, ts=None):
        """分配一个新轮号（同一天内连续，跨天从 1 重新开始）。

        ★ 必须先按本轮的时间切库再分配轮号：之前是先分配、后由
          write_round() 切库，导致跨零点那一轮拿到了昨天的大轮号，
          下一条又从 1 开始 —— CSV 里 round_id 出现 48680 → 1 的跳变。
        """
        self._rollover(ts=ts)
        self.round_id += 1
        return self.round_id

    # ------------------------------------------------------------------
    #  运行/事件
    # ------------------------------------------------------------------
    def start_run(self, config_snapshot):
        cur = self.con.execute(
            "INSERT INTO runs (start_ts, end_ts, config_json) VALUES (?,?,?)",
            (time.time(), None, json.dumps(config_snapshot, ensure_ascii=False)))
        self.run_id = cur.lastrowid
        self.con.commit()
        return self.run_id

    def end_run(self):
        if self.run_id:
            self.con.execute("UPDATE runs SET end_ts=? WHERE id=?",
                             (time.time(), self.run_id))
            self.con.commit()

    def event(self, level, src, msg):
        ts = time.time()
        try:
            self.con.execute("INSERT INTO events (ts, level, src, msg) VALUES (?,?,?,?)",
                             (ts, level, src, msg))
            self.con.commit()
        except Exception:
            pass
        if self.verbose:
            print("      [%s] %s: %s" % (level, src, msg))

    # ------------------------------------------------------------------
    #  写一轮
    # ------------------------------------------------------------------
    def write_round(self, samples, grid_ts=None, round_id=None, env=None):
        """写一轮数据。

        samples : [(ts, ch, mode, value, unit, quality), ...]
                  可以为空（该轮整体失败）——仍会在 CSV 里写一行，
                  以保证时间列不断档。
        grid_ts : 本轮的"网格时刻"（严格等间隔）。缺省用第一个点或现在。
        round_id: 轮号。缺省用 self.round_id（需先 begin_round()）。
        env     : {通道: 温度℃} 或 None —— ★ 与这一轮同一次采样的温度，
                  会写进 samples 表的 t1/t2/dt 列和同一个 CSV 行，
                  这样电学量与温度就是**同一时间自变量**下的同一行数据。
        """
        if round_id is None:
            round_id = self.round_id
        if grid_ts is None:
            grid_ts = samples[0][0] if samples else time.time()

        self._rollover(ts=grid_ts)

        # ---- 温度（与电学量同一行）----
        env = env or {}
        tvals = []
        for ch in self.env_channels:
            tvals.append(env.get(ch))
        t1 = tvals[0] if len(tvals) >= 1 else None
        t2 = tvals[1] if len(tvals) >= 2 else None
        dt = (t1 - t2) if (t1 is not None and t2 is not None) else None

        # ---- SQLite（长表，保留精确信息）----
        rows = [(ts, grid_ts, self.run_id, round_id, ch, mode, val, unit, q)
                for (ts, ch, mode, val, unit, q) in samples if val is not None]
        if rows:
            self.con.executemany(
                "INSERT INTO samples (ts, grid_ts, run_id, round_id, ch, mode,"
                " value, unit, quality) VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self.con.commit()
            self.n_written += len(rows)

        # ---- 温度：写进 env_round 宽表（一行一轮，与 samples 同一 grid_ts）----
        if any(v is not None for v in tvals):
            self.con.execute(
                "INSERT INTO env_round (grid_ts, run_id, round_id,"
                " t1, t2, dt) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(grid_ts) DO UPDATE SET"
                "   t1=excluded.t1, t2=excluded.t2, dt=excluded.dt",
                (grid_ts, self.run_id, round_id, t1, t2, dt))
            self.con.commit()
            self.n_env += sum(1 for v in tvals if v is not None)

        # ---- 轮记录（空轮也留痕，保证时间列不断档、校验准确）----
        n_bad = sum(1 for s in samples if s[3] is None)
        self.con.execute(
            "INSERT INTO round_log (grid_ts, round_id, run_id, n_ok, n_bad)"
            " VALUES (?,?,?,?,?)"
            " ON CONFLICT(grid_ts) DO UPDATE SET"
            "   n_ok = n_ok + excluded.n_ok, n_bad = n_bad + excluded.n_bad",
            (grid_ts, round_id, self.run_id, len(rows), n_bad))
        self.con.commit()

        # ---- CSV（宽表，一行一轮：电学量 + 温度，统一时间自变量）----
        if self.csv_writer:
            d = {}
            for (ts, ch, mode, val, unit, q) in samples:
                d["%s%d" % (mode.lower(), ch)] = val
            row = [_iso_sec(grid_ts), round_id]
            for c in self.csv_cols[2:]:
                if c == "dt":
                    row.append("" if dt is None else "%.2f" % dt)
                elif c.startswith("t"):
                    v = env.get(int(c[1:])) if c[1:].isdigit() else None
                    row.append("" if v is None else "%.2f" % v)
                else:
                    row.append(_fmt_val(d.get(c)))
            self.csv_writer.writerow(row)
            self.csv_file.flush()               # 实时副本：立刻落盘
            self.n_csv += 1

        # ---- 聚合 ----
        for (ts, ch, mode, val, unit, q) in samples:
            if val is not None:
                self._agg_add(grid_ts, ch, mode, val)
        self._flush_agg()

        return len(rows)

    # ------------------------------------------------------------------
    #  写环境数据（温度/湿度，独立节奏）
    # ------------------------------------------------------------------
    def write_env(self, values, ts=None, grid_ts=None, round_id=None,
                  sensor="TA612"):
        """写一次环境采样（与测量同一轮）。

        values : {通道: 温度℃ 或 None}，如 {1: 26.7, 2: 26.7}
        ts     : 实际读取时刻（PC 时间）
        grid_ts: 所属轮次的网格时刻（与 samples 表一致，用它可以和电学数据按轮对齐）
        round_id: 轮号（与 samples.csv 的 round_id 一致）
        """
        ts = ts or time.time()
        self._rollover(ts=ts)
        rid = self.round_id if round_id is None else round_id
        gts = ts if grid_ts is None else grid_ts

        rows = [(ts, gts, self.run_id, rid, sensor, ch, val, "C", 0)
                for ch, val in sorted(values.items()) if val is not None]
        if rows:
            self.con.executemany(
                "INSERT INTO env (ts, grid_ts, run_id, round_id, sensor, ch,"
                " value, unit, quality) VALUES (?,?,?,?,?,?,?,?,?)", rows)
            self.con.commit()
            self.n_env += len(rows)

        # ---- CSV 宽表（一行一轮，与 samples.csv 的 round_id 对齐）----
        if self.env_writer and self.env_channels:
            row = [_iso_sec(gts), rid]
            for ch in self.env_channels:
                v = values.get(ch)
                row.append("" if v is None else "%.2f" % v)
            if len(self.env_channels) >= 2:
                a, b = values.get(self.env_channels[0]), values.get(self.env_channels[1])
                row.append("" if (a is None or b is None) else "%.2f" % (a - b))
            self.env_writer.writerow(row)
            self.env_file.flush()

        return len(rows)

    def query_env(self, t0=None, t1=None, chs=None):
        """返回 [(ts, ch, value), ...]"""
        sql = "SELECT ts, ch, value FROM env WHERE 1=1"
        args = []
        if t0 is not None:
            sql += " AND ts>=?"; args.append(t0)
        if t1 is not None:
            sql += " AND ts<=?"; args.append(t1)
        if chs:
            sql += " AND ch IN (%s)" % ",".join("?" * len(chs)); args += list(chs)
        sql += " ORDER BY ts"
        return self.con.execute(sql, args).fetchall()

    def _agg_add(self, ts, ch, mode, val):
        for name, width in (("agg_1min", 60), ("agg_1hour", 3600)):
            bucket = int(ts // width) * width
            key = (name, bucket, ch, mode)
            cur = self._pending_agg.get(key)
            if cur is None:
                self._pending_agg[key] = [val, val, val, 1]
            else:
                cur[0] = min(cur[0], val)
                cur[1] = max(cur[1], val)
                cur[2] += val
                cur[3] += 1

    def _flush_agg(self):
        if not self._pending_agg:
            return
        for (name, bucket, ch, mode), (vmin, vmax, vsum, n) in self._pending_agg.items():
            self.con.execute(
                "INSERT INTO %s (bucket, ch, mode, v_min, v_max, v_avg, n)"
                " VALUES (?,?,?,?,?,?,?)"
                " ON CONFLICT(bucket, ch, mode) DO UPDATE SET"
                "   v_min = MIN(v_min, excluded.v_min),"
                "   v_max = MAX(v_max, excluded.v_max),"
                "   v_avg = (v_avg * n + excluded.v_avg * excluded.n) / (n + excluded.n),"
                "   n     = n + excluded.n" % name,
                (bucket, ch, mode, vmin, vmax, vsum / n, n))
        self.con.commit()
        self._pending_agg.clear()

    # ------------------------------------------------------------------
    #  查询
    # ------------------------------------------------------------------
    def last_ts(self):
        try:
            r = self.con.execute("SELECT MAX(grid_ts) FROM samples").fetchone()
            return r[0] if r and r[0] else None
        except Exception:
            return None

    def last_round_id(self):
        try:
            r = self.con.execute("SELECT MAX(round_id) FROM samples").fetchone()
            return r[0] if r and r[0] else 0
        except Exception:
            return 0

    def counts(self):
        """(数据库测量值行数, CSV 轮数)"""
        try:
            db_n = self.con.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
        except Exception:
            db_n = -1
        return db_n, self.n_csv

    def query_range(self, t0, t1, chs=None, modes=None, limit=None):
        sql = ("SELECT ts, grid_ts, ch, mode, value, unit, quality FROM samples"
               " WHERE ts>=? AND ts<=?")
        args = [t0, t1]
        if chs:
            sql += " AND ch IN (%s)" % ",".join("?" * len(chs))
            args += list(chs)
        if modes:
            sql += " AND mode IN (%s)" % ",".join("?" * len(modes))
            args += list(modes)
        sql += " ORDER BY ts"
        if limit:
            sql += " LIMIT %d" % int(limit)
        return self.con.execute(sql, args).fetchall()

    def query_wide(self, t0=None, t1=None, chs=None, modes=None):
        """按轮聚合的宽表数据：[(grid_ts, round_id, {mode+ch: value}), ...]"""
        sql = ("SELECT grid_ts, round_id, ch, mode, value FROM samples WHERE 1=1")
        args = []
        if t0 is not None:
            sql += " AND grid_ts>=?"; args.append(t0)
        if t1 is not None:
            sql += " AND grid_ts<=?"; args.append(t1)
        sql += " ORDER BY grid_ts"
        out = {}
        for gts, rid, ch, mode, val in self.con.execute(sql, args):
            key = (gts, rid)
            out.setdefault(key, {})["%s%d" % (mode.lower(), ch)] = val
        return [(gts, rid, d) for (gts, rid), d in sorted(out.items())]

    def query_agg(self, table, t0, t1, chs=None, modes=None):
        sql = ("SELECT bucket, ch, mode, v_min, v_max, v_avg, n FROM %s"
               " WHERE bucket>=? AND bucket<=?" % table)
        args = [t0, t1]
        if chs:
            sql += " AND ch IN (%s)" % ",".join("?" * len(chs))
            args += list(chs)
        if modes:
            sql += " AND mode IN (%s)" % ",".join("?" * len(modes))
            args += list(modes)
        sql += " ORDER BY bucket"
        return self.con.execute(sql, args).fetchall()

    # ------------------------------------------------------------------
    #  关闭时校验补齐
    # ------------------------------------------------------------------
    def verify_and_repair(self, log=print):
        """校验 CSV 轮数是否与数据库一致，缺则按宽表重写。"""
        if not self.write_csv:
            return True, "未启用实时 CSV（跳过校验）"
        try:
            db_rounds = self.con.execute("SELECT COUNT(*) FROM round_log").fetchone()[0]
        except Exception:
            db_rounds = 0
        if db_rounds == self.n_csv:
            return True, "一致（%d 轮）" % self.n_csv

        log("      CSV 轮数(%d) 与数据库(%d) 不一致，正在按宽表补齐…"
            % (self.n_csv, db_rounds))
        path = os.path.join(self._day_dir(self.day), "%s_samples.csv" % self.day)

        # ★ Windows 不允许替换仍被打开的文件：先关掉 CSV 句柄
        if self.csv_file:
            try:
                self.csv_file.flush()
                self.csv_file.close()
            except Exception:
                pass
            self.csv_file = None
            self.csv_writer = None

        try:
            tmp = path + ".tmp"
            written = 0
            with open(tmp, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(self.csv_cols)
                for gts, rid, d in self.query_wide():
                    w.writerow([_iso_sec(gts), rid]
                               + [_fmt_val(d.get(c)) for c in self.csv_cols[2:]])
                    written += 1
            os.replace(tmp, path)
            self.n_csv = written            # ★ 记实际写出的行数，不是数据库轮数
            ok_msg = "已重写为 %d 轮" % written
            err = None
        except Exception as e:
            ok_msg, err = None, "补齐失败: %s" % e

        # 重新打开 CSV 供后续追加
        try:
            self.csv_file = open(path, "a", newline="", encoding="utf-8")
            self.csv_writer = csv.writer(self.csv_file)
        except Exception:
            pass

        return (err is None), (ok_msg if err is None else err)

    def close(self):
        self._close_files()


# ============================================================================
#  自测
# ============================================================================
if __name__ == "__main__":
    import sys, tempfile
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    root = os.path.join(tempfile.gettempdir(), "iv_store_test2")
    print("测试目录:", root)
    st = Storage(root, nch=4)
    st.start_run({"test": True})
    t0 = time.time()
    for rd in range(1, 4):
        gts = t0 + (rd - 1) * 2.0            # 网格：严格 2 秒
        s = []
        for ch in (1, 2, 3, 4):
            for mode in ("V", "I"):
                v = 1.4 + 0.01 * rd if mode == "V" else 0.0098 + 0.0001 * ch
                s.append((gts + ch * 0.19 + (0 if mode == "V" else 0.76), ch, mode,
                          v, "V" if mode == "V" else "A", 0))
        st.begin_round()
        st.write_round(s, grid_ts=gts)
    # 再写一轮"整体失败"（空数据），验证时间列不断档
    st.begin_round()
    st.write_round([], grid_ts=t0 + 3 * 2.0)
    print("计数 (db行, csv轮):", st.counts())
    print("校验:", st.verify_and_repair())
    st.end_run()
    st.close()

    csv_path = os.path.join(root, st.day, "%s_samples.csv" % st.day)
    print("\n生成的宽表 CSV:")
    with open(csv_path, encoding="utf-8") as f:
        for line in f:
            print("   " + line.rstrip())
