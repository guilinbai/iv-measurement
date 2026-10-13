#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
4 路 IV 测量 · 采集主程序（命令行版 · 第 1 期）
================================================================================
用途：把"两台 DMM6500 + Arduino 开关阵列"跑起来，边测边落盘。
本版**不带界面**，目的是先把"时序对齐 + 数据落盘"验证扎实；
跑通后再套双图界面（界面层直接复用 ivcore）。

用法：
    python main.py                       # 按 config.yaml 一直跑（Ctrl+C 停止）
    python main.py --rounds 5            # 只跑 5 轮
    python main.py --interval 3          # 临时改成 3 秒间隔
    python main.py --duration 60         # 跑 60 秒
    python main.py --no-csv              # 不写 CSV 实时副本
    python main.py --dry                 # 不连硬件，用假数据验证存储链路

退出时：Arduino 回安全态 → 写结束事件 → 校验并补齐 CSV → 打印统计
================================================================================
"""

import argparse
import os
import sys
import time
from datetime import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from ivcore.config import Config                      # noqa: E402
from ivcore.devices import Arduino, DMM, ArduinoError, DMMError   # noqa: E402
from ivcore.storage import Storage                    # noqa: E402
from ivcore.scheduler import Acquisition              # noqa: E402

SEP = "=" * 78


def head(t):
    print()
    print(SEP)
    print("  " + t)
    print(SEP)


def fmt_val(v, unit):
    if v is None:
        return "  ---  "
    if unit == "A":
        if abs(v) < 1e-6:
            return "%7.2f nA" % (v * 1e9)
        if abs(v) < 1e-3:
            return "%7.3f uA" % (v * 1e6)
        return "%7.3f mA" % (v * 1e3)
    if abs(v) < 1.0:
        return "%7.3f mV" % (v * 1e3)
    return "%7.4f V " % v


# ----------------------------------------------------------------------------
def connect(cfg, args):
    """连接全部硬件，返回 (arduino, dmm_v, dmm_i)"""
    head("连接硬件")

    # --- Arduino ---
    ard_cfg = cfg["devices"]["arduino"]
    port = args.port or ard_cfg.get("port") or None
    print("  Arduino:")
    arduino = Arduino(port=port, baud=ard_cfg["baud"],
                      timeout=ard_cfg["timeout_s"])
    arduino.heartbeat_s = float(ard_cfg["heartbeat_s"])
    try:
        banner = arduino.open(auto=True, verbose=True)
    except ArduinoError as e:
        print("    [X] %s" % e)
        raise
    print("    已连接 %s" % arduino.port)
    for line in banner:
        print("      | %s" % line)
    if not any("READY v1.1" in b for b in banner):
        print("    [!] 未检测到 'READY v1.1' —— 请确认已上传《arduino测试\\12\\12.ino》")

    # --- 两台表 ---
    dmms = {}
    for key, name in (("dmm_voltage", "电压表"), ("dmm_current", "电流表")):
        d = cfg["devices"][key]
        print("  %s:" % name)
        dmm = DMM(d.get("resource"), d.get("idn_serial"), d.get("timeout_s"), name)
        try:
            idn = dmm.open(verbose=True)
        except DMMError as e:
            print("    [X] %s" % e)
            raise
        print("    %s" % idn)
        # 按配置设置
        rng_key = "range" if "range" in d else None
        try:
            dmm.configure(meas_func=d.get("meas_func"),
                          nplc=d.get("nplc"),
                          autozero=d.get("autozero"),
                          autorange=d.get("autorange"),
                          rng=d.get(rng_key) if rng_key else None,
                          verbose=True)
        except Exception as e:
            print("    [!] 配置时出错（继续）: %s" % str(e)[:100])
        dmms[key] = dmm

    return arduino, dmms["dmm_voltage"], dmms["dmm_current"]


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="4 路 IV 测量采集主程序（命令行版）")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--rounds", type=int, default=None, help="跑多少轮后停止")
    ap.add_argument("--duration", type=float, default=None, help="跑多少秒后停止")
    ap.add_argument("--interval", type=float, default=None, help="覆盖采样间隔（秒）")
    ap.add_argument("--port", default=None, help="手动指定 Arduino 串口，如 COM9")
    ap.add_argument("--no-csv", action="store_true", help="不写 CSV 实时副本")
    ap.add_argument("--storage-dir", default=None, help="覆盖存储根目录")
    ap.add_argument("--dry", action="store_true", help="不连硬件，用假数据验证存储")
    args = ap.parse_args()

    print(SEP)
    print("  4 路 IV 测量 · 采集主程序（命令行版）")
    print(SEP)

    cfg = Config(args.config)
    if args.interval:
        cfg.raw["acquisition"]["interval_s"] = args.interval
    if args.storage_dir:
        cfg.raw["storage"]["dir"] = os.path.abspath(args.storage_dir)
    if args.no_csv:
        cfg.raw["storage"]["write_csv_realtime"] = False

    head("配置")
    print(cfg.dump_summary())

    ok, iv = cfg.interval_ok()
    if not ok:
        print("\n  [!] 警告：采样间隔 %.2f s < 一轮耗时 %.2f s，"
              "实际周期会被拉长" % (iv, cfg.round_cost_s()))
        print("      建议间隔 >= %.2f s，或调小 config.yaml 里的 timing 参数"
              % cfg.min_interval_s())

    # --- 存储 ---
    head("打开存储")
    storage = Storage(cfg["storage"]["dir"],
                      write_csv=cfg["storage"]["write_csv_realtime"],
                      nch=cfg["acquisition"]["channels"])
    last = storage.last_ts()
    if last:
        print("      上次数据到: %s" % datetime.fromtimestamp(last)
              .strftime("%Y-%m-%d %H:%M:%S"))
    # 完整配置快照（结构与 config.yaml 一致，便于日后追溯"这批数据当时怎么设的"）
    import json as _json
    snap = _json.loads(_json.dumps(cfg.raw, default=str))
    snap["_started"] = datetime.now().isoformat(timespec="seconds")
    snap["_program"] = "main.py (CLI, phase-1)"
    snap["_override"] = {k: v for k, v in
                         (("interval", args.interval), ("duration", args.duration),
                          ("rounds", args.rounds), ("port", args.port))
                         if v is not None}
    storage.start_run(snap)

    arduino = dmm_v = dmm_i = None
    acq = None
    stop = {"flag": False}
    t_begin = time.monotonic()

    def on_round(rno, samples, t_round, lag):
        """每轮打印一行紧凑摘要。"""
        vs = [s for s in samples if s[2] == "V"]
        iss = [s for s in samples if s[2] == "I"]
        parts = []
        for tag, arr, unit in (("V", vs, "V"), ("I", iss, "A")):
            if arr:
                vals = " ".join(fmt_val(s[3], unit).strip() for s in arr)
                parts.append("%s: %s" % (tag, vals))
        print("  [%5d] %2d/%d  %s | lag %+5.2fs | %s"
              % (rno, len(samples), cfg["acquisition"]["channels"] * 2,
                 datetime.fromtimestamp(t_round).strftime("%H:%M:%S"),
                 lag, "   ".join(parts)))

    try:
        if args.dry:
            head("DRY RUN（用假数据验证存储链路，不连硬件）")
            import random

            class _FakeArd:
                def send(self, c):
                    pass

                def readline(self):
                    return ""

                def beat(self):
                    return False

                def safe(self, drain=0.3):
                    pass

            class _FakeAcq:
                def __init__(self):
                    self.round_no = 0
                    self.n_samples = 0

                def run(self, max_rounds=None, stop_flag=None, on_round=None):
                    n = max_rounds or 3
                    for r in range(n):
                        self.round_no = r + 1
                        t0 = time.time()
                        s = []
                        for ch in (1, 2, 3, 4):
                            s.append((t0 + ch * 0.05, ch, "V",
                                      1.4 + 0.02 * ch + random.uniform(-.001, .001), "V", 0))
                            s.append((t0 + ch * 0.05 + 0.02, ch, "I",
                                      0.0098 + 0.0002 * ch + random.uniform(-1e-5, 1e-5), "A", 0))
                        storage.write_round(s)
                        self.n_samples += len(s)
                        if on_round:
                            on_round(self.round_no, s, t0, 0.0)
                        time.sleep(0.3)
                    return {"rounds": self.round_no, "samples": self.n_samples,
                            "failed_reads": 0, "slow_rounds": 0,
                            "t_first": time.time(), "t_last": time.time()}

            acq = _FakeAcq()
            stats = acq.run(max_rounds=args.rounds or 3, stop_flag=None, on_round=on_round)
        else:
            arduino, dmm_v, dmm_i = connect(cfg, args)
            acq = Acquisition(cfg, arduino, dmm_v, dmm_i, storage)

            # 启动前先让开关阵列回一个确定状态
            arduino.safe()

            head("开始采集（Ctrl+C 停止）")

            def stop_flag():
                if stop["flag"]:
                    return True
                if args.duration and (time.monotonic() - t_begin) >= args.duration:
                    return True
                return False

            stats = acq.run(max_rounds=args.rounds,
                            stop_flag=stop_flag,
                            on_round=on_round)

    except KeyboardInterrupt:
        print("\n\n  [用户中断 Ctrl+C]")
        stats = acq.stats() if acq and hasattr(acq, "stats") else {}
    except (ArduinoError, DMMError) as e:
        print("\n  [X] 硬件错误: %s" % e)
        stats = acq.stats() if acq and hasattr(acq, "stats") else {}
    except Exception as e:
        print("\n  [X] 未预期错误: %s: %s" % (type(e).__name__, e))
        import traceback
        traceback.print_exc()
        stats = acq.stats() if acq and hasattr(acq, "stats") else {}
    finally:
        head("收尾")
        # ① 停采集 + Arduino 回安全态
        if arduino:
            try:
                print("      让 Arduino 回安全态…")
                arduino.safe()
            except Exception as e:
                print("      [!] 安全态命令失败: %s" % str(e)[:80])

        # ② 结束 run、写事件
        try:
            storage.event("INFO", "session", "采集结束")
            storage.end_run()
        except Exception:
            pass

        # ③ 校验并补齐 CSV
        if cfg["storage"]["verify_on_exit"]:
            print("      校验 CSV 与数据库…")
            ok, msg = storage.verify_and_repair()
            print("      %s %s" % ("[OK]" if ok else "[!]", msg))

        db_n, csv_n = storage.counts()
        storage.close()

        # ④ 打印统计
        head("统计")
        if stats:
            print("      轮数        : %s" % stats.get("rounds", 0))
            print("      数据点      : %s" % stats.get("samples", 0))
            print("      读数失败    : %s" % stats.get("failed_reads", 0))
            print("      超时轮次    : %s" % stats.get("slow_rounds", 0))
            t0 = stats.get("t_first")
            if t0:
                dur = time.time() - t0
                print("      时长        : %.1f s (%.2f 小时)" % (dur, dur / 3600))
        print("      数据库行数  : %s" % db_n)
        print("      CSV 行数    : %s" % csv_n)
        print("      数据目录    : %s" % os.path.join(storage.root, storage.day or ""))
        print()
        print("  [OK] 已退出")
        print(SEP)


if __name__ == "__main__":
    main()
