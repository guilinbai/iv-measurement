# -*- coding: utf-8 -*-
"""
界面数据层：内存缓冲 + 降采样 + 历史查询
================================================================================
设计要点（"7 天不卡"的根本）：
  · 实时显示只画"最近的窗口"（如最近 10 分钟 = 几百个点）→ 开销极小
  · 历史视图按时间跨度自动选数据源：
        ≤ 2 小时  → 原始 samples 表
        ≤ 40 小时 → agg_1min 聚合表
        更长      → agg_1hour 聚合表
    这样"看 7 天"只需读约 168 个小时点，查询毫秒级
  · 无论原始多少点，最终都降采样到约 N 个点（N ≈ 屏幕像素宽）再画
    → 绘图耗时只跟屏幕宽度有关，与数据量无关
  · 内存缓冲是**定长 deque**（绝不无限增长）
"""

import glob
import os
import sqlite3
from collections import deque

import numpy as np


# ============================================================================
#  降采样
# ============================================================================
def downsample_minmax(ts, vals, buckets=1600):
    """把 (ts, vals) 压到约 buckets 个点，保留每桶的 min/max/avg。

    返回 (ts_out, vmin, vmax, vavg)，都是 np.ndarray。
    """
    ts = np.asarray(ts, dtype=float)
    vals = np.asarray(vals, dtype=float)
    n = len(ts)
    if n == 0:
        e = np.array([])
        return e, e, e, e
    if n <= buckets:
        return ts, vals, vals, vals

    edges = np.linspace(0, n, buckets + 1).astype(int)
    idx = np.arange(buckets)
    starts, ends = edges[:-1], edges[1:]
    ends = np.maximum(ends, starts + 1)

    t_out = np.empty(buckets)
    vmin = np.empty(buckets)
    vmax = np.empty(buckets)
    vavg = np.empty(buckets)

    for i in range(buckets):
        a, b = starts[i], min(ends[i], n)
        seg = vals[a:b]
        t_out[i] = ts[(a + b - 1) // 2]
        vmin[i] = seg.min()
        vmax[i] = seg.max()
        vavg[i] = seg.mean()
    return t_out, vmin, vmax, vavg


# ============================================================================
#  内存环形缓冲
# ============================================================================
class TrendBuffer:
    """按 (ch, mode) 存最近的数据点，定长。

    mode: "V" 电压 / "I" 电流 / "T" 温度（环境量，独立节奏）
    """

    def __init__(self, nch=4, maxlen=60000, env_channels=None):
        self.nch = nch
        self.maxlen = maxlen
        self.env_channels = list(env_channels or [])
        self.data = {}
        for ch in range(1, nch + 1):
            for mode in ("V", "I"):
                self.data[(ch, mode)] = deque(maxlen=maxlen)
        for ch in self.env_channels:
            self.data[(ch, "T")] = deque(maxlen=maxlen)
        self.n_total = 0
        self.env_latest = {}

    def add(self, ts, ch, mode, val):
        key = (ch, mode)
        if key in self.data and val is not None:
            self.data[key].append((ts, val))
            self.n_total += 1

    def add_round(self, samples):
        """samples: [(ts, ch, mode, value, unit, quality), ...]"""
        for s in samples:
            if len(s) >= 4:
                self.add(s[0], s[1], s[2], s[3])

    def add_env(self, ts, values):
        """环境量：values = {通道: 温度}"""
        for ch, val in values.items():
            self.add(ts, ch, "T", val)
            if val is not None:
                self.env_latest[ch] = val

    def clear(self):
        for k in self.data:
            self.data[k].clear()
        self.n_total = 0
        self.env_latest = {}

    def arrays(self, ch, mode, t0=None, t1=None):
        """取出 [t0, t1] 范围内的 (ts, vals) 数组。"""
        d = self.data.get((ch, mode))
        if not d:
            empty = np.array([])
            return empty, empty
        if t0 is None and t1 is None:
            arr = np.array(d, dtype=float)
        else:
            lo = t0 if t0 is not None else -np.inf
            hi = t1 if t1 is not None else np.inf
            arr = np.array([p for p in d if lo <= p[0] <= hi], dtype=float)
        if arr.size == 0:
            empty = np.array([])
            return empty, empty
        return arr[:, 0], arr[:, 1]

    def span(self):
        """返回缓冲里覆盖的时间范围 (t_min, t_max)，空则 (None, None)。"""
        tmin, tmax = None, None
        for d in self.data.values():
            if d:
                a, b = d[0][0], d[-1][0]
                tmin = a if tmin is None else min(tmin, a)
                tmax = b if tmax is None else max(tmax, b)
        return tmin, tmax


# ============================================================================
#  历史查询（跨天，自动选聚合表）
# ============================================================================
def list_databases(root):
    return sorted(glob.glob(os.path.join(root, "*", "*.db")))


def pick_source(span_s):
    """按跨度选数据源表与列。返回 (表名, 时间列, 是否聚合)"""
    if span_s <= 2 * 3600:
        return "samples", "ts", False
    if span_s <= 40 * 3600:
        return "agg_1min", "bucket", True
    return "agg_1hour", "bucket", True


def query_history(root, t0, t1, nch=4):
    """跨天查询历史数据。

    返回 {(ch, mode): (ts_array, val_array)}，聚合数据用 v_avg。
    """
    span = max(1.0, t1 - t0)
    table, tcol, is_agg = pick_source(span)

    out = {}
    for ch in range(1, nch + 1):
        for mode in ("V", "I"):
            out[(ch, mode)] = ([], [])

    for db in list_databases(root):
        try:
            con = sqlite3.connect(db)
            if is_agg:
                sql = ("SELECT %s, ch, mode, v_avg FROM %s"
                       " WHERE %s>=? AND %s<=? ORDER BY %s"
                       % (tcol, table, tcol, tcol, tcol))
            else:
                sql = ("SELECT ts, ch, mode, value FROM samples"
                       " WHERE ts>=? AND ts<=? AND value IS NOT NULL ORDER BY ts")
            for ts, ch, mode, val in con.execute(sql, (t0, t1)):
                key = (ch, mode)
                if key in out and val is not None:
                    out[key][0].append(ts)
                    out[key][1].append(val)
            con.close()
        except Exception:
            continue

    res = {}
    for k, (ts, vals) in out.items():
        res[k] = (np.asarray(ts, dtype=float), np.asarray(vals, dtype=float))
    return res, (table, is_agg)


def query_history_env(root, t0, t1, channels):
    """跨天查询环境数据（温度）。

    ★ v0.4.0 起温度存进了 env_round 表（一行一轮，t1/t2/dt 列，
      与 samples 共用同一个 grid_ts）；更早的数据在旧 env 表（长表）。
      这里**两张表都查**，新数据用 env_round，旧数据兜底用 env，
      保证换版本之后历史曲线仍然连续。

    返回 {ch: (ts_array, val_array)}
    """
    chs = list(channels or [])
    out = {ch: ([], []) for ch in chs}
    if not chs:
        return out

    for db in list_databases(root):
        con = None
        try:
            con = sqlite3.connect(db)
            cols = {r[1] for r in con.execute("PRAGMA table_info(env_round)")}
            has_round = "grid_ts" in cols
            # 旧表是否存在
            old_cols = {r[1] for r in con.execute("PRAGMA table_info(env)")}
            has_old = "ts" in old_cols

            # ---- 新表：env_round（一行一轮）----
            if has_round and chs:
                fields = []
                for ch in chs:
                    fields.append("t%d" % ch)
                sql = ("SELECT grid_ts, %s FROM env_round"
                       " WHERE grid_ts>=? AND grid_ts<=? ORDER BY grid_ts"
                       % ", ".join(fields))
                try:
                    for row in con.execute(sql, (t0, t1)):
                        ts = row[0]
                        if ts is None:
                            continue
                        for i, ch in enumerate(chs):
                            val = row[i + 1]
                            if val is not None and ch in out:
                                out[ch][0].append(ts)
                                out[ch][1].append(val)
                except Exception:
                    pass

            # ---- 旧表兜底：env（长表）----
            if has_old:
                sql = ("SELECT ts, ch, value FROM env"
                       " WHERE ts>=? AND ts<=? AND value IS NOT NULL ORDER BY ts")
                try:
                    for ts, ch, val in con.execute(sql, (t0, t1)):
                        if ch in out and ts is not None:
                            out[ch][0].append(ts)
                            out[ch][1].append(val)
                except Exception:
                    pass
        except Exception:
            continue
        finally:
            if con is not None:
                try:
                    con.close()
                except Exception:
                    pass

    # 两张表都查过 → 同一通道的点可能重复/乱序，按时间排序去重
    res = {}
    for ch, (a, b) in out.items():
        if not a:
            res[ch] = (np.array([]), np.array([]))
            continue
        ta = np.asarray(a, dtype=float)
        va = np.asarray(b, dtype=float)
        order = np.argsort(ta, kind="mergesort")
        ta, va = ta[order], va[order]
        keep = np.concatenate(([True], np.diff(ta) > 1e-6))
        res[ch] = (ta[keep], va[keep])
    return res


def query_count(root, t0, t1):
    """快速估算某时间范围内的原始点数（用于显示统计信息）。"""
    total = 0
    for db in list_databases(root):
        try:
            con = sqlite3.connect(db)
            r = con.execute("SELECT COUNT(*) FROM samples WHERE ts>=? AND ts<=?",
                            (t0, t1)).fetchone()
            total += r[0] if r else 0
            con.close()
        except Exception:
            continue
    return total
