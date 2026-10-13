#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
数据查看与筛选导出
================================================================================
用途：不开界面也能看数据、并把数据按需导出成 CSV。

    python inspect_db.py                          # 看最近一个库的概况
    python inspect_db.py <db路径>                 # 看指定库
    python inspect_db.py --dir D:\IV_data         # 列出所有天的库
    python inspect_db.py --export out.csv --ch 1,3 --mode V --from 14:00 --to 15:00

参数：
    --ch 1,3        只导出这些通道（★ 空通道在这里一次筛掉）
    --mode V|I      只导出电压或电流
    --from / --to   时间范围，支持 "HH:MM" 或 "YYYY-MM-DD HH:MM"
    --wide          导出宽表（一行一轮、8 列，便于 Origin 直接画图）
================================================================================
"""

import argparse
import csv
import glob
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SEP = "=" * 78


def find_latest_db(root):
    dbs = sorted(glob.glob(os.path.join(root, "*", "*.db")))
    return dbs[-1] if dbs else None


def parse_time(s, base_day=None):
    """支持 'HH:MM[:SS]' 或 'YYYY-MM-DD HH:MM[:SS]'。"""
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
                "%H:%M:%S", "%H:%M"):
        try:
            dt = datetime.strptime(s, fmt)
            if "%Y" not in fmt:
                d = base_day or datetime.now().date()
                dt = dt.replace(year=d.year, month=d.month, day=d.day)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError("无法解析时间: %s" % s)


def show_overview(db):
    con = sqlite3.connect(db)
    print(SEP)
    print("  数据库: %s" % db)
    print(SEP)

    # 先列出库里有哪些表（含行数）—— 回答"我能看什么"
    print("  表清单：")
    for (t,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"
                            " AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        try:
            n = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
        except Exception:
            n = -1
        note = {"samples": "每个测量点一行（含精确 ts / grid_ts / run_id / quality）",
                "round_log": "每轮一行（含空轮）→ 宽表可完整重建",
                "runs": "每次启动程序 = 一个 run（时间 + 配置快照）",
                "events": "事件日志（断线/重连/网格重对齐等）",
                "agg_1min": "1 分钟聚合（画 1 天视图用）",
                "agg_1hour": "1 小时聚合（画 7 天视图用）"}.get(t, "")
        print("    %-12s %7d 行   %s" % (t, n, note))
    print()

    n = con.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    if n == 0:
        print("  （空库）")
        return con
    t0, t1 = con.execute("SELECT MIN(ts), MAX(ts) FROM samples").fetchone()
    print("  数据点  : %d" % n)
    print("  时间段  : %s  ->  %s" % (datetime.fromtimestamp(t0), datetime.fromtimestamp(t1)))
    dur = t1 - t0
    print("  时长    : %.1f s (%.2f 小时)" % (dur, dur / 3600))
    if dur > 0:
        print("  平均速率: %.2f 点/秒" % (n / dur))

    print("\n  按通道/模式统计：")
    print("    %-4s %-5s %8s %14s %14s %14s" % ("ch", "mode", "n", "min", "max", "avg"))
    for ch, mode, cnt, mn, mx, av in con.execute(
            "SELECT ch, mode, COUNT(*), MIN(value), MAX(value), AVG(value)"
            " FROM samples GROUP BY ch, mode ORDER BY mode, ch"):
        print("    %-4d %-5s %8d %14.6g %14.6g %14.6g" % (ch, mode, cnt, mn, mx, av))

    runs = con.execute("SELECT id, start_ts, end_ts FROM runs").fetchall()
    print("\n  运行记录: %d 条" % len(runs))
    for rid, st, en in runs[-3:]:
        print("    run %-3d  %s -> %s" % (
            rid, datetime.fromtimestamp(st).strftime("%H:%M:%S") if st else "-",
            datetime.fromtimestamp(en).strftime("%H:%M:%S") if en else "(未结束)"))

    print("\n  最近 6 个样本：")
    print("    %-12s %-5s %-4s %-5s %14s %-3s %s"
          % ("time", "round", "ch", "mode", "value", "q", "unit"))
    for ts, rid, ch, mode, val, unit, q in con.execute(
            "SELECT ts, round_id, ch, mode, value, unit, quality"
            " FROM samples ORDER BY ts DESC LIMIT 6"):
        print("    %-12s %-5s %-4d %-5s %14.6g %-3d %s"
              % (datetime.fromtimestamp(ts).strftime("%H:%M:%S.%f")[:-3],
                 rid, ch, mode, val if val is not None else float("nan"), q, unit))

    ev = con.execute("SELECT ts, level, src, msg FROM events ORDER BY ts DESC LIMIT 5").fetchall()
    if ev:
        print("\n  最近事件:")
        for ts, lv, src, msg in ev:
            print("    %s [%s] %s: %s" % (
                datetime.fromtimestamp(ts).strftime("%H:%M:%S"), lv, src, msg[:60]))
    return con


def export(con, out, chs=None, modes=None, t0=None, t1=None, wide=False, nch=4):
    sql = ("SELECT ts, grid_ts, round_id, ch, mode, value, unit, quality"
           " FROM samples WHERE 1=1")
    args = []
    if t0 is not None:
        sql += " AND grid_ts>=?"; args.append(t0)
    if t1 is not None:
        sql += " AND grid_ts<=?"; args.append(t1)
    if chs:
        sql += " AND ch IN (%s)" % ",".join("?" * len(chs)); args += chs
    if modes:
        sql += " AND mode IN (%s)" % ",".join("?" * len(modes)); args += modes
    sql += " ORDER BY grid_ts, ch"
    rows = con.execute(sql, args).fetchall()

    with open(out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        if wide:
            # 宽表：一行一轮，与实时副本 2026-10-12_samples.csv 格式一致
            cols = ["v%d" % c for c in range(1, nch + 1)]
            head = []
            for c in range(1, nch + 1):
                head += ["v%d" % c, "i%d" % c]
            w.writerow(["iso_time", "round_id"] + head)

            rounds = {}
            for ts, gts, rid, ch, mode, val, unit, q in rows:
                key = (gts if gts else round(ts, 2), rid)
                rounds.setdefault(key, {})["%s%d" % (mode.lower(), ch)] = val

            for (gts, rid) in sorted(rounds):
                d = rounds[(gts, rid)]
                w.writerow([datetime.fromtimestamp(gts).strftime("%Y-%m-%d %H:%M:%S"), rid]
                           + [("" if d.get(c) is None else "%.6g" % d[c]) for c in head])
            n_out = len(rounds)
        else:
            w.writerow(["timestamp", "iso_time", "grid_ts", "round_id",
                        "ch", "mode", "value", "unit", "quality"])
            for ts, gts, rid, ch, mode, val, unit, q in rows:
                w.writerow(["%.6f" % ts,
                            datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                            "%.6f" % gts if gts else "",
                            rid, ch, mode,
                            "" if val is None else "%.9g" % val, unit, q])
            n_out = len(rows)
    print("\n  [OK] 已导出 %d %s -> %s"
          % (n_out, "轮" if wide else "行", out))


def show_runs(con, limit=10):
    """显示 runs 表：每次启动 main.py = 一个 run。"""
    print(SEP)
    print("  runs 表 —— 每次启动 main.py 就是一个 run（这里只显示最近 %d 条）" % limit)
    print(SEP)
    rows = con.execute(
        "SELECT id, start_ts, end_ts, config_json FROM runs ORDER BY id DESC LIMIT ?",
        (limit,)).fetchall()
    if not rows:
        print("  （还没有运行记录）")
        return
    for rid, st, en, cfg in rows:
        if en:
            dur = en - st
            dur_s = ("%.1f 秒" % dur) if dur < 120 else \
                    ("%.1f 分钟" % (dur / 60)) if dur < 7200 else \
                    ("%.2f 小时" % (dur / 3600))
        else:
            dur_s = "(未正常结束 —— 断电/崩溃/被强杀)"
        print("\n  run_id = %d" % rid)
        print("     开始 : %s" % (datetime.fromtimestamp(st).strftime("%Y-%m-%d %H:%M:%S")
                                  if st else "-"))
        print("     结束 : %s" % (datetime.fromtimestamp(en).strftime("%Y-%m-%d %H:%M:%S")
                                  if en else "-"))
        print("     时长 : %s" % dur_s)
        try:
            d = json.loads(cfg) if cfg else {}
        except Exception:
            d = {}
        if d:
            aq = d.get("acquisition", {})
            tm = d.get("timing", {})
            dm = d.get("dmms", {}) or {
                "dmm_voltage": d.get("devices", {}).get("dmm_voltage", {}).get("resource"),
                "dmm_current": d.get("devices", {}).get("dmm_current", {}).get("resource"),
            }
            iv = aq.get("interval_s", d.get("interval_s", "?"))
            nch = aq.get("channels", d.get("channels", "?"))
            print("     配置 : 间隔=%s s   通道=%s   T_WINDOW=%s ms"
                  % (iv, nch, tm.get("t_window_ms", "?")))
            for k, v in dm.items():
                if v:
                    print("            %s = %s" % (k, v))
            if d.get("storage", {}).get("dir"):
                print("            存储目录: %s" % d["storage"]["dir"])
            if d.get("_override"):
                print("            本次命令行覆盖: %s" % d["_override"])
            if d.get("_program"):
                print("            程序: %s" % d["_program"])
        else:
            print("     配置 : (无快照)")


def main():
    ap = argparse.ArgumentParser(description="数据查看与筛选导出")
    ap.add_argument("db", nargs="?", help="数据库路径（默认取 --dir 下最新的）")
    ap.add_argument("--dir", default="D:/IV_data", help="数据根目录")
    ap.add_argument("--list", action="store_true", help="列出所有天的库")
    ap.add_argument("--runs", action="store_true",
                    help="只看 runs 表（每次运行的时间与配置快照）")
    ap.add_argument("--export", help="导出到指定 CSV")
    ap.add_argument("--ch", help="通道，如 1,3")
    ap.add_argument("--mode", help="模式 V 或 I")
    ap.add_argument("--from", dest="t_from", help="起始时间 HH:MM 或 'YYYY-MM-DD HH:MM'")
    ap.add_argument("--to", dest="t_to", help="结束时间")
    ap.add_argument("--wide", action="store_true", help="导出宽表（一行一轮）")
    args = ap.parse_args()

    if args.list:
        dbs = sorted(glob.glob(os.path.join(args.dir, "*", "*.db")))
        print("  %d 个数据库：" % len(dbs))
        for d in dbs:
            print("    %-60s  %8.1f KB" % (d, os.path.getsize(d) / 1024))
        return

    db = args.db or find_latest_db(args.dir)
    if not db or not os.path.isfile(db):
        print("  [X] 找不到数据库。用 --dir 指定目录，或 --list 看看有哪些。")
        return

    if args.runs:
        con = sqlite3.connect(db)
        print("  数据库: %s" % db)
        show_runs(con)
        con.close()
        return

    con = show_overview(db)

    if args.export:
        chs = [int(x) for x in args.ch.split(",")] if args.ch else None
        modes = [m.strip().upper() for m in args.mode.split(",")] if args.mode else None
        base = datetime.fromtimestamp(
            con.execute("SELECT MAX(ts) FROM samples").fetchone()[0] or 0).date()
        t0 = parse_time(args.t_from, base)
        t1 = parse_time(args.t_to, base)
        export(con, args.export, chs, modes, t0, t1, args.wide)
    con.close()


if __name__ == "__main__":
    main()
