# -*- coding: utf-8 -*-
"""
采集线程
================================================================================
把 ivcore 的采集流程跑在后台线程里，通过 Qt 信号把数据送到界面 ——
**界面线程绝不碰串口和 VISA，绝不会被阻塞**。

信号：
    sig_state(str)           状态变化：connecting/running/stopping/stopped/error
    sig_log(str)             一行日志
    sig_round(int, object)   一轮完成：(round_no, samples)
    sig_stats(dict)          结束时统计
    sig_failed(str)          致命错误
"""

import os
import time
from datetime import datetime

from PySide6.QtCore import QThread, Signal

from ivcore.devices import Arduino, DMM, ArduinoError, DMMError
from ivcore.storage import Storage
from ivcore.scheduler import Acquisition


class AcquisitionWorker(QThread):
    sig_state = Signal(str)
    sig_state_msg = Signal(str, str)          # (state, message)
    sig_log = Signal(str)
    sig_round = Signal(int, object)
    sig_stats = Signal(dict)
    sig_failed = Signal(str)
    sig_env = Signal(float, object)          # (网格时刻, {ch: 温度}) 与测量同一轮

    def __init__(self, cfg, interval=None, port=None, max_rounds=None,
                 env_queue=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.interval = interval or float(cfg["acquisition"]["interval_s"])
        self.port = port
        self.max_rounds = max_rounds
        self.env_queue = env_queue        # 温度数据队列（由界面线程投递）
        self._stop = False

        self.arduino = None
        self.dmm_v = None
        self.dmm_i = None
        self.storage = None
        self.acq = None
        self.stats = {}
        self.hw_ready = False

    # ------------------------------------------------------------------
    def stop(self):
        """请求停止（异步；线程会在当前轮结束后退出）。"""
        self._stop = True

    def _log(self, msg):
        self.sig_log.emit(msg)

    # ------------------------------------------------------------------
    def run(self):
        try:
            self._connect()
            self._acquire()
        except (ArduinoError, DMMError) as e:
            self.sig_failed.emit(str(e))
        except Exception as e:
            import traceback
            self.sig_failed.emit("%s: %s" % (type(e).__name__, e))
            self.sig_log.emit(traceback.format_exc())
        finally:
            self._finish()

    # ------------------------------------------------------------------
    def _connect(self):
        cfg = self.cfg
        self.sig_state_msg.emit("connecting", "正在连接硬件…")

        # --- Arduino ---
        ard_cfg = cfg["devices"]["arduino"]
        port = self.port or ard_cfg.get("port") or None
        self.arduino = Arduino(port=port, baud=ard_cfg["baud"],
                               timeout=ard_cfg["timeout_s"])
        self.arduino.heartbeat_s = float(ard_cfg["heartbeat_s"])
        self._log("探测 Arduino…")
        banner = self.arduino.open(auto=True, verbose=False)
        self._log("Arduino 已连接: %s" % self.arduino.port)
        for line in banner:
            self._log("   | %s" % line)
        if not any("READY v1.1" in b for b in banner):
            self._log("[!] 未检测到 'READY v1.1'，请确认已上传固件 v1.1")

        # --- 两台表 ---
        for key, name in (("dmm_voltage", "电压表"), ("dmm_current", "电流表")):
            d = cfg["devices"][key]
            dmm = DMM(d.get("resource"), d.get("idn_serial"),
                      d.get("timeout_s"), name)
            idn = dmm.open(verbose=False)
            self._log("%s: %s" % (name, idn))
            try:
                dmm.configure(meas_func=d.get("meas_func"), nplc=d.get("nplc"),
                              autozero=d.get("autozero"),
                              autorange=d.get("autorange"),
                              rng=d.get("range"), verbose=False)
                self._log("   %s 已配置: %s" % (name, dmm.summary()))
            except Exception as e:
                self._log("[!] %s 配置出错（继续）: %s" % (name, str(e)[:80]))
            # ★ 量程预热：让自动量程先稳定，避免第一轮因切换档位而拖慢/超时
            try:
                _vals, costs = dmm.warmup(n=3, verbose=False)
                self._log("   %s 预热 3 次，%s，单次 %.0f~%.0f ms"
                          % (name, dmm.summary(), min(costs) if costs else 0,
                             max(costs) if costs else 0))
            except Exception as e:
                self._log("[!] %s 预热失败（继续）: %s" % (name, str(e)[:60]))
            if key == "dmm_voltage":
                self.dmm_v = dmm
            else:
                self.dmm_i = dmm

        self.hw_ready = True

    # ------------------------------------------------------------------
    def _acquire(self):
        cfg = self.cfg
        nch = int(cfg["acquisition"]["channels"])

        # --- 温度采集器（与测量同一轮）---
        self.tas = None
        self._env_ok = 0
        self._env_fail = 0
        ta = cfg["devices"]["tasensor"]
        if ta.get("enabled"):
            from ivcore.tasensor import TASensor
            try:
                self.tas = TASensor(ta["port"], ta["baud"], ta["timeout_s"])
                info = self.tas.open(verbose=False)
                self._log("温度采集器: %s @ %s（每轮随测量采一次）"
                          % (info, ta["port"]))
            except Exception as e:
                self._log("[!] 温度采集器打开失败（继续，无温度）: %s" % str(e)[:80])
                self.tas = None

        # --- 存储 ---
        ta = cfg["devices"]["tasensor"]
        env_chs = list(ta["channels"]) if ta.get("enabled") else []
        self.storage = Storage(cfg["storage"]["dir"],
                               write_csv=cfg["storage"]["write_csv_realtime"],
                               nch=nch, env_channels=env_chs, verbose=False)
        last = self.storage.last_ts()
        if last:
            self._log("上次数据到: %s"
                      % datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S"))
        self._log("存储目录: %s" % cfg["storage"]["dir"])

        import json as _json
        snap = _json.loads(_json.dumps(cfg.raw, default=str))
        snap["_started"] = datetime.now().isoformat(timespec="seconds")
        snap["_program"] = "main_gui.py (GUI, phase-2)"
        self.storage.start_run(snap)

        # --- 采集 ---
        # env_provider: 每轮写盘前读一次温度，与电学量一起写进同一条记录
        self.acq = Acquisition(cfg, self.arduino, self.dmm_v, self.dmm_i,
                               self.storage, log=self._log,
                               env_provider=self._read_temp)
        self.acq.interval = self.interval

        self.arduino.safe()
        self.sig_state_msg.emit("running", "采集中")

        def on_round(rno, samples, t_round, lag):
            self.sig_round.emit(rno, (samples, t_round, lag))

        self.stats = self.acq.run(max_rounds=self.max_rounds,
                                  stop_flag=lambda: self._stop,
                                  on_round=on_round)

    def _read_temp(self):
        """读一次温度，返回 {通道: 温度℃}。

        由 scheduler 在**写盘前**调用，所以温度和这一轮的电学量是
        同一次采样、同一个 grid_ts —— 会一起写进同一条记录里。
        这里不写盘（交给 storage.write_round 统一写）。
        """
        if not self.tas:
            return None
        ta = self.cfg["devices"]["tasensor"]
        try:
            vals = self.tas.read_temperatures(channels=ta["channels"],
                                              invalid_above=ta["invalid_above"])
            self._env_ok += 1
        except Exception as e:
            self._env_fail += 1
            if self._env_fail in (1, 10) or self._env_fail % 100 == 0:
                self._log("[!] 温度读取失败(%d次): %s" % (self._env_fail, str(e)[:70]))
            return None
        # 通知界面：画温度曲线 + 超温保护
        try:
            self.sig_env.emit(self.acq.last_grid_ts or time.time(), vals)
        except Exception:
            pass
        return vals

    def _drain_env(self, max_n=200):
        """兼容旧接口（温度已随每轮一起写入，不再需要队列）。"""
        pass

    # ------------------------------------------------------------------
    def _finish(self):
        self.sig_state_msg.emit("stopping", "收尾中…")

        if self.arduino:
            try:
                self.arduino.safe()
                self._log("Arduino 已回安全态")
            except Exception as e:
                self._log("[!] 安全态命令失败: %s" % str(e)[:80])

        if self.storage:
            try:
                self.storage.event("INFO", "session", "采集结束（GUI）")
                self.storage.end_run()
            except Exception:
                pass
            if self.cfg["storage"]["verify_on_exit"]:
                try:
                    ok, msg = self.storage.verify_and_repair(log=self._log)
                    self._log("%s %s" % ("[OK]" if ok else "[!]", msg))
                except Exception as e:
                    self._log("[!] 校验失败: %s" % str(e)[:80])
            try:
                self.storage.close()
            except Exception:
                pass

        for d in (self.dmm_v, self.dmm_i):
            try:
                if d:
                    d.close()
            except Exception:
                pass
        if self.tas:
            try:
                self.tas.close()
                self._log("温度采集器已关闭（成功 %d / 失败 %d 次）"
                          % (self._env_ok, self._env_fail))
            except Exception:
                pass
        if self.arduino:
            try:
                self.arduino.close()
            except Exception:
                pass

        self.sig_stats.emit(self.stats or {})
        self.sig_state_msg.emit("stopped", "已停止")


# ============================================================================
#  环境量采集线程（温度，独立节奏，不干扰测量）
# ============================================================================
class EnvWorker(QThread):
    """按固定间隔读 TA612 温度。

    只负责"采 + 发信号"，**不碰数据库**（避免两个线程写同一个 SQLite 连接）。
    落盘由测量线程从队列取出后统一写入（见 AcquisitionWorker 的 env_queue）。
    """

    sig_env = Signal(float, object)      # (ts, {ch: 温度})
    sig_log = Signal(str)
    sig_error = Signal(str)

    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.interval = float(cfg["devices"]["tasensor"]["interval_s"])
        self._stop = False
        self.ok_count = 0
        self.err_count = 0

    def stop(self):
        self._stop = True

    def run(self):
        from ivcore.tasensor import TASensor, TASensorError

        d = self.cfg["devices"]["tasensor"]
        sensor = TASensor(d["port"], d["baud"], d["timeout_s"])
        try:
            info = sensor.open(verbose=False)
            self.sig_log.emit("温度采集器已连接: %s @ %s（每 %.1f s 采一次）"
                              % (info, d["port"], self.interval))
        except TASensorError as e:
            self.sig_error.emit(str(e))
            return

        try:
            while not self._stop:
                t0 = time.monotonic()
                try:
                    vals = sensor.read_temperatures(
                        channels=d["channels"], invalid_above=d["invalid_above"])
                    self.sig_env.emit(time.time(), vals)
                    self.ok_count += 1
                except TASensorError as e:
                    self.err_count += 1
                    if self.err_count in (1, 10) or self.err_count % 100 == 0:
                        self.sig_error.emit("温度读取失败(%d次): %s"
                                            % (self.err_count, e))
                # 分片睡眠，保证能及时响应停止
                end = t0 + self.interval
                while time.monotonic() < end and not self._stop:
                    time.sleep(min(0.1, max(0.0, end - time.monotonic())))
        finally:
            sensor.close()
            self.sig_log.emit("温度采集器已关闭（成功 %d 次 / 失败 %d 次）"
                              % (self.ok_count, self.err_count))


# ============================================================================
#  模拟采集（不接硬件，只用来看界面效果 / 验证绘图性能）
# ============================================================================
class FakeWorker(QThread):
    """生成假数据，用于在没有硬件时预览界面、测试 7 天视图的绘图性能。"""

    sig_state = Signal(str)
    sig_state_msg = Signal(str, str)
    sig_log = Signal(str)
    sig_round = Signal(int, object)
    sig_env = Signal(float, object)      # 假温度数据
    sig_stats = Signal(dict)
    sig_failed = Signal(str)

    def __init__(self, cfg, interval=0.2, nch=4, env_channels=None, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.interval = interval
        self.nch = nch
        self.env_channels = list(env_channels or [])
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        import math
        import random
        self.sig_state_msg.emit("running", "模拟采集（假数据）")
        self.sig_log.emit("模拟模式：不连接硬件，生成假数据")
        if self.env_channels:
            self.sig_log.emit("模拟温度通道: %s（ch1 逐渐升温、ch2 基本不变，ΔT 逐渐拉大）"
                              % self.env_channels)
        rno = 0
        t0 = time.time()
        n_env_every = max(1, int(round(2.0 / self.interval)))   # 约每 2 秒发一次温度

        try:
            while not self._stop:
                rno += 1
                gts = t0 + (rno - 1) * self.interval
                samples = []
                for ch in range(1, self.nch + 1):
                    for mode in ("V", "I"):
                        ph = time.time() / 600.0 + ch
                        if mode == "V":
                            v = 1.4 + 0.12 * math.sin(ph) + 0.03 * ch / 4.0 \
                                + random.gauss(0, 0.002)
                            unit = "V"
                        else:
                            v = 0.0098 + 0.0016 * math.sin(ph + 1.0) \
                                + random.gauss(0, 2e-5)
                            unit = "A"
                        samples.append((gts + ch * 0.02, ch, mode, v, unit, 0))
                self.sig_round.emit(rno, (samples, gts, 0.0))

                # 假温度：ch1 随"驱动电压"线性升温（模拟 ΔT 拉大）
                if self.env_channels and rno % n_env_every == 0:
                    el = time.time() - t0
                    heat = min(12.0, el * 0.25)          # 最多升 12℃
                    vals = {}
                    for i, ch in enumerate(self.env_channels):
                        vals[ch] = (26.5 + (heat if i == 0 else 0.0)
                                    + random.gauss(0, 0.05))
                    self.sig_env.emit(time.time(), vals)

                time.sleep(self.interval)
        finally:
            self.sig_stats.emit({"rounds": rno, "samples": rno * self.nch * 2,
                                 "failed_reads": 0, "slow_rounds": 0,
                                 "t_first": t0, "t_last": time.time()})
            self.sig_state_msg.emit("stopped", "已停止")
