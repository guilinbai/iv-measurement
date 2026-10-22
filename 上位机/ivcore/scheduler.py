# -*- coding: utf-8 -*-
"""
采集调度
================================================================================
    Acquisition     按采样间隔循环：发 S → 收 WIN → 读表 → 交给存储

时序对齐的关键（对应方案 §5）：
    1. 调度器到点 → 给 Arduino 发 "S"
    2. Arduino 按微时序执行，在**读数窗口打开时**上报 "WIN <ch> <V|I>"
    3. 调度器收到 WIN **立刻**打时间戳（时间基准在 PC，不受串口延迟影响），
       再查对应的表（实测 :READ? 约 25 ms，窗口 50 ms 有 2 倍余量）
    4. 一轮结束收到 "ROUND DONE" → 交给存储批量写盘

采样间隔 = **周期**（相邻两轮"开始时刻"之差），不是"本轮结束后再等 N 秒"。
"""

import time

from .devices import ArduinoError, DMMError

QUALITY_OK = 0
QUALITY_SUSPECT = 1
QUALITY_BAD = 2


class Acquisition:
    def __init__(self, cfg, arduino, dmm_v, dmm_i, storage, log=print,
                 env_provider=None):
        self.cfg = cfg
        self.arduino = arduino
        self.dmm_v = dmm_v
        self.dmm_i = dmm_i
        self.storage = storage
        self.log = log
        # ★ 每轮写盘前调用，返回 {通道: 温度℃}；温度与这一轮的电学量同一次采样
        self.env_provider = env_provider

        self.interval = float(cfg["acquisition"]["interval_s"])
        self.nch = int(cfg["acquisition"]["channels"])
        self.round_timeout = cfg.round_cost_s() * 3 + 5.0   # 单轮超时保护

        self.round_no = 0
        self.t_first = None
        self.n_samples = 0
        self.n_failed = 0
        self._slow_rounds = 0
        self.t_grid0 = None          # 网格基准（墙钟），时间列 = t_grid0 + n×间隔
        self.last_grid_ts = None     # 最近一轮的网格时刻（温度采集与它对齐）

    # ------------------------------------------------------------------
    def _dmm_for(self, mode):
        return self.dmm_v if mode.upper() == "V" else self.dmm_i

    def _unit_for(self, mode):
        return "V" if mode.upper() == "V" else "A"

    # ------------------------------------------------------------------
    def run_round(self, grid_ts=None):
        """执行一轮，返回 (samples, t_round)。

        grid_ts：本轮的**网格时刻**（严格等间隔）—— 数据的时间列就用它，
                 所以 CSV 里每行的时间差恒等于采样间隔。
        """
        self.round_no += 1
        rid = self.storage.begin_round()          # 轮号在当天内连续（跨重启不重置）
        if grid_ts is None:
            grid_ts = (self.t_grid0 + (self.round_no - 1) * self.interval
                       if self.t_grid0 else time.time())
        samples = []
        t_round = time.time()

        self.arduino.send("S")
        t_deadline = time.monotonic() + self.round_timeout
        got_done = False

        while time.monotonic() < t_deadline:
            line = self.arduino.readline()
            if not line:
                continue

            if line.startswith("WIN "):
                ts = time.time()                       # ★ 触发瞬间打时间戳
                parts = line.split()
                if len(parts) < 3:
                    continue
                try:
                    ch = int(parts[1])
                except ValueError:
                    continue
                mode = parts[2].upper()

                dmm = self._dmm_for(mode)
                try:
                    val = dmm.read()
                    q = QUALITY_OK
                except DMMError as e:
                    val = None
                    q = QUALITY_BAD
                    self.n_failed += 1
                    self.log("      [!] %s" % e)

                samples.append((ts, ch, mode, val, self._unit_for(mode), q))

            elif line.startswith("ROUND DONE"):
                got_done = True
                break
            elif line.startswith("WATCHDOG"):
                self.log("      [!] 固件看门狗触发（已回安全态）")
                self.storage.event("WARN", "arduino", "watchdog fired")
            elif line.startswith("ERR"):
                self.log("      [!] 固件报错: %s" % line)
                self.storage.event("ERROR", "arduino", line)

        if not got_done:
            self.log("      [!] 第 %d 轮超时未收到 ROUND DONE（已收 %d 项）"
                     % (self.round_no, len(samples)))
            self.storage.event("ERROR", "acq", "round %d timeout" % self.round_no)

        if self.t_first is None and samples:
            self.t_first = samples[0][0]

        self.n_samples += len(samples)
        self.last_grid_ts = grid_ts       # 供外部（温度采集）对齐同一轮用

        # ★ 取本轮的温度（与电学量同一次采样），和 samples 一起写盘
        env = None
        if self.env_provider is not None:
            try:
                env = self.env_provider()
            except Exception as e:
                self.log("      [!] 温度采样失败: %s" % str(e)[:70])
                env = None
                self.storage.event("WARN", "tasensor", str(e)[:200])

        self.storage.write_round(samples, grid_ts=grid_ts, round_id=rid, env=env)
        return samples, t_round

    # ------------------------------------------------------------------
    def run(self, max_rounds=None, stop_flag=None, on_round=None,
            pace_t0=None):
        """主循环。

        max_rounds : 跑多少轮后停（None = 一直跑）
        stop_flag  : 可调用对象，返回 True 时优雅停止
        on_round   : 每轮结束回调 (round_no, samples, t_round, lag_s)
        pace_t0    : 周期基准（默认 = 现在）
        """
        ok, iv = self.cfg.interval_ok(self.interval)
        if not ok:
            self.log("      [!] 采样间隔 %.2f s 的余量只有 %.2f s（低于一轮耗时的 20%%），"
                     "偶发抖动可能追不上；建议 >= %.2f s"
                     % (self.interval, self.interval - self.cfg.round_cost_s(),
                        self.cfg.min_interval_s()))
            self.storage.event("WARN", "acq",
                               "interval %.2f below round cost %.2f"
                               % (self.interval, self.cfg.round_cost_s()))

        t_next = pace_t0 if pace_t0 else time.monotonic()
        if self.t_grid0 is None:
            self.t_grid0 = time.time()
        behind = 0
        self.log("      开始采集：间隔 %.2f s，一轮预计 %.2f s，"
                 "每轮 %d 项；时间列 = 严格 %.1f s 等间隔"
                 % (self.interval, self.cfg.round_cost_s(), self.nch * 2, self.interval))

        while True:
            if stop_flag and stop_flag():
                break
            if max_rounds and self.round_no >= max_rounds:
                break

            # ★ 网格时刻：严格等间隔（CSV 的时间列就用它）
            grid_ts = self.t_grid0 + self.round_no * self.interval

            t0 = time.monotonic()
            try:
                samples, t_round = self.run_round(grid_ts=grid_ts)
            except ArduinoError as e:
                self.log("      [!] 串口错误: %s" % e)
                self.storage.event("ERROR", "arduino", str(e)[:120])
                break

            elapsed = time.monotonic() - t0
            t_next += self.interval
            lag = time.monotonic() - t_next

            if elapsed > self.interval:
                self._slow_rounds += 1
                behind += 1
                if self._slow_rounds in (1, 10) or self._slow_rounds % 100 == 0:
                    self.log("      [!] 第 %d 轮耗时 %.2f s > 间隔 %.2f s"
                             "（累计 %d 次），数据会持续滞后"
                             % (self.round_no, elapsed, self.interval, self._slow_rounds))
                if self._slow_rounds == 1:
                    self.storage.event("WARN", "acq", "round slower than interval")
                # 连续跟不上 → 把网格重新对齐到当前时刻
                # （时间列会出现一次跳跃，但事件表里有记录，不会"悄悄说谎"）
                if behind >= 5 and self.t_grid0 is not None:
                    drift = time.time() - (self.t_grid0 + self.round_no * self.interval)
                    self.t_grid0 += drift
                    behind = 0
                    self.log("      [!] 连续 %d 轮跟不上，网格已重新对齐"
                             "（时间列跳过 %.1f s，请考虑调大采样间隔）" % (5, drift))
                    self.storage.event("WARN", "acq",
                                       "grid re-aligned, drift %.1fs" % drift)
                t_next = time.monotonic()
            else:
                behind = 0

            if on_round:
                try:
                    on_round(self.round_no, samples, t_round, lag)
                except Exception as e:
                    self.log("      [!] 回调异常: %s" % e)

            self.arduino.beat()                    # 喂看门狗（长时间间隔时才需要）

            sleep = t_next - time.monotonic()
            if sleep > 0:
                # 分片睡眠，保证能及时响应停止
                end = time.monotonic() + sleep
                while time.monotonic() < end:
                    if stop_flag and stop_flag():
                        break
                    time.sleep(min(0.05, max(0.0, end - time.monotonic())))

        return self.stats()

    # ------------------------------------------------------------------
    def stats(self):
        return {
            "rounds": self.round_no,
            "samples": self.n_samples,
            "failed_reads": self.n_failed,
            "slow_rounds": self._slow_rounds,
            "t_first": self.t_first,
            "t_last": time.time(),
        }


# ============================================================================
#  降采样（界面阶段用；这里先放好，便于复用）
# ============================================================================
def downsample_minmax(points, buckets=1600):
    """把 (ts, value) 序列压到约 buckets 个点，保留每桶的 min/max/avg。

    返回 [(ts, vmin, vmax, vavg), ...]
    绘图耗时只与 buckets 有关，与原始点数无关 —— 这是"7 天不卡"的关键。
    """
    n = len(points)
    if n == 0:
        return []
    if n <= buckets * 2:
        return [(t, v, v, v) for (t, v) in points]

    out = []
    step = n / float(buckets)
    i = 0
    for b in range(buckets):
        j = int((b + 1) * step)
        if j <= i:
            j = i + 1
        chunk = points[i:j]
        i = j
        if not chunk:
            continue
        vals = [v for (_, v) in chunk if v is not None]
        if not vals:
            continue
        out.append((chunk[len(chunk) // 2][0], min(vals), max(vals),
                    sum(vals) / len(vals)))
    return out
