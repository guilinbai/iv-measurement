# -*- coding: utf-8 -*-
"""
主窗口
================================================================================
布局：
    ┌ 控制区：开始/停止 · 间隔 · 视图范围 · 跟随 · 通道 · 模拟模式 ┐
    ├ 电压图（4 条曲线）                                          ┤
    ├ 电流图（4 条曲线）    ← 与上图共享统一时间轴（X 轴联动）      ┤
    └ 状态栏 + 日志区                                             ┘

要点：
  · 采集在后台线程，界面只从缓冲/数据库取数 → 永不卡顿
  · 实时视图只画"最近窗口"（开销极小）；1 天 / 7 天视图走聚合表（查询毫秒级）
  · 间隔保护：实时显示"一轮耗时 / 当前间隔 / 余量"，不够时红字警告
  · 关窗时先让采集线程优雅收尾（Arduino 回安全态、校验 CSV 再退出）
"""

import json
import os
import queue
import shutil
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDoubleSpinBox, QFrame,
                               QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QRadioButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from .buffer import (TrendBuffer, query_history, query_history_env,
                     query_count, list_databases)
from .plots import TrendPlots
from .worker import AcquisitionWorker, FakeWorker, EnvWorker

# 视图范围：名称 -> 秒
SPANS = [("10 分钟", 600), ("1 小时", 3600), ("1 天", 86400), ("7 天", 604800)]

# 界面状态文件（记住上次的采样间隔、视图跨度等）
STATE_FILE = "ui_state.json"


def _fmt_num(v, unit):
    """按量级自动选单位：V → V/mV/µV；A → A/mA/µA/nA。"""
    try:
        a = abs(v)
    except Exception:
        return "—"
    if unit == "V":
        if a >= 1.0:
            return "%.4f V" % v
        if a >= 1e-3:
            return "%.2f mV" % (v * 1e3)
        return "%.1f µV" % (v * 1e6)
    if a >= 1.0:
        return "%.3f A" % v
    if a >= 1e-3:
        return "%.2f mA" % (v * 1e3)
    if a >= 1e-6:
        return "%.2f µA" % (v * 1e6)
    return "%.1f nA" % (v * 1e9)


class MainWindow(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.nch = int(cfg["acquisition"]["channels"])
        self.root = cfg["storage"]["dir"]

        # 温度通道（启用温度采集时才有）
        ta = cfg["devices"]["tasensor"]
        self.env_chs = list(ta["channels"]) if ta.get("enabled") else []

        self.worker = None
        self.env_worker = None
        self.env_queue = queue.Queue()      # 温度数据 → 采集线程落盘
        self.buf = TrendBuffer(nch=self.nch, maxlen=60000,
                               env_channels=self.env_chs)
        self.span_s = 3600                 # 当前视图跨度
        self.follow = True
        self.fake_mode = False
        self.t_start = None
        self.n_rounds = 0
        self.last_lag = 0.0
        self._hist_query_t = 0.0
        self._hist_info = ""
        # True = 刚启动/数据不足时自动贴合数据起点；用户一旦明确选跨度就关掉
        self._auto_fit = True
        # 上次由程序设置的 X 范围；用来检测"用户是否手动缩放/平移过"
        self._last_set_xr = None
        # 温度超限保护是否已触发（避免重复关电源）
        self._overheat_tripped = False
        # 最近一轮各路数值 {(ch, "V"|"I"): value}，供状态栏显示
        self.last_samples = {}

        self.setWindowTitle("4 路 IV 测量 · 实时监视  (v0.3 · 含温度)")
        self.resize(1280, 900)
        self._build_ui()
        self._restore_state()          # ★ 恢复上次的采样间隔/视图跨度等

        # 刷新定时器（限帧 3 Hz —— 人眼看曲线不需要更高）
        self.timer = QTimer(self)
        self.timer.setInterval(333)
        self.timer.timeout.connect(self._refresh)
        self.timer.start()

        self._update_interval_hint()
        self._append_log("界面就绪。存储目录：%s" % self.root)
        self._append_log("提示：开始采集前请确认 Arduino IDE 的【串口监视器】已关闭。")

    # ==================================================================
    #  界面搭建
    # ==================================================================
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(6)

        # ---------------- 控制区 ----------------
        ctl = QHBoxLayout()
        ctl.setSpacing(10)

        self.btn_start = QPushButton("▶ 开始采集")
        self.btn_start.setMinimumWidth(110)
        self.btn_start.clicked.connect(self._start)
        self.btn_stop = QPushButton("■ 停止")
        self.btn_stop.setMinimumWidth(90)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop)
        ctl.addWidget(self.btn_start)
        ctl.addWidget(self.btn_stop)

        ctl.addWidget(self._vline())

        ctl.addWidget(QLabel("采样间隔"))
        self.spin_interval = QDoubleSpinBox()
        self.spin_interval.setRange(0.5, 3600.0)
        self.spin_interval.setDecimals(2)
        self.spin_interval.setSingleStep(0.5)
        self.spin_interval.setSuffix(" s")
        self.spin_interval.setValue(float(self.cfg["acquisition"]["interval_s"]))
        self.spin_interval.setMinimumWidth(90)
        self.spin_interval.valueChanged.connect(self._update_interval_hint)
        ctl.addWidget(self.spin_interval)

        ctl.addWidget(self._vline())

        ctl.addWidget(QLabel("视图"))
        self.span_group = QButtonGroup(self)
        for i, (name, sec) in enumerate(SPANS):
            rb = QRadioButton(name)
            rb.setProperty("span", sec)
            if sec == self.span_s:
                rb.setChecked(True)
            rb.toggled.connect(self._on_span_changed)
            self.span_group.addButton(rb, i)
            ctl.addWidget(rb)

        # ★ 自定义视图跨度（小时）
        ctl.addWidget(QLabel("或"))
        self.spin_span = QDoubleSpinBox()
        self.spin_span.setRange(0.01, 720.0)
        self.spin_span.setDecimals(2)
        self.spin_span.setSingleStep(0.5)
        self.spin_span.setSuffix(" 小时")
        self.spin_span.setValue(1.0)
        self.spin_span.setMinimumWidth(92)
        ctl.addWidget(self.spin_span)
        self.btn_span = QPushButton("确认")
        self.btn_span.setMaximumWidth(58)
        self.btn_span.clicked.connect(self._on_custom_span)
        ctl.addWidget(self.btn_span)

        self.chk_follow = QCheckBox("跟随最新")
        self.chk_follow.setChecked(True)
        self.chk_follow.toggled.connect(self._on_follow_changed)
        ctl.addWidget(self.chk_follow)

        ctl.addWidget(self._vline())

        ctl.addWidget(QLabel("通道"))
        self.chk_ch = {}
        for ch in range(1, self.nch + 1):
            cb = QCheckBox("ch%d" % ch)
            cb.setChecked(True)
            cb.toggled.connect(lambda v, c=ch: self.plots.set_channel_visible(c, v))
            self.chk_ch[ch] = cb
            ctl.addWidget(cb)

        ctl.addStretch(1)

        self.chk_fake = QCheckBox("模拟模式（不接硬件）")
        self.chk_fake.setToolTip("勾选后不连接任何硬件，用假数据预览界面/测绘图性能")
        ctl.addWidget(self.chk_fake)

        root.addLayout(ctl)

        # ---------------- 间隔保护提示条 ----------------
        self.lbl_hint = QLabel("")
        self.lbl_hint.setFont(QFont("", 9))
        root.addWidget(self.lbl_hint)

        # ---------------- 电压源控制（驱动 ch1/ch2 温差）----------------
        ps_cfg = self.cfg["devices"]["powersupply"]
        self.ps = None
        self._ps_last_read = 0.0
        self._ps_dead = False       # 电源串口是否已失联
        if ps_cfg.get("enabled"):
            bar = QHBoxLayout()
            bar.setSpacing(8)
            bar.addWidget(QLabel("驱动电压源："))
            self.spin_volt = QDoubleSpinBox()
            self.spin_volt.setRange(0.0, float(ps_cfg.get("max_voltage", 31.0)))
            self.spin_volt.setDecimals(2)
            self.spin_volt.setSingleStep(0.5)
            self.spin_volt.setSuffix(" V")
            self.spin_volt.setValue(0.0)
            self.spin_volt.setMinimumWidth(90)
            bar.addWidget(self.spin_volt)

            self.btn_volt_apply = QPushButton("应用")
            self.btn_volt_apply.clicked.connect(self._apply_voltage)
            bar.addWidget(self.btn_volt_apply)

            # ★ 上限电流（限流）设置
            bar.addWidget(QLabel("上限电流"))
            self.spin_cur = QDoubleSpinBox()
            self.spin_cur.setRange(0.001, 100.0)
            self.spin_cur.setDecimals(3)
            self.spin_cur.setSingleStep(0.1)
            self.spin_cur.setSuffix(" A")
            self.spin_cur.setValue(1.0)
            self.spin_cur.setMinimumWidth(92)
            bar.addWidget(self.spin_cur)
            self.btn_cur_apply = QPushButton("应用")
            self.btn_cur_apply.setMaximumWidth(58)
            self.btn_cur_apply.clicked.connect(self._apply_current)
            bar.addWidget(self.btn_cur_apply)

            self.btn_out = QPushButton("输出：—")
            self.btn_out.setCheckable(True)
            self.btn_out.clicked.connect(self._toggle_output)
            bar.addWidget(self.btn_out)

            # ★ 重连按钮：USB 转串口拔插/掉线后，不用重启软件
            self.btn_ps_reconnect = QPushButton("重连")
            self.btn_ps_reconnect.setMaximumWidth(58)
            self.btn_ps_reconnect.setToolTip(
                "电源串口掉线时点这里重新连接\n"
                "（会先关旧串口、再自动扫描所有 COM 口找这台电源）")
            self.btn_ps_reconnect.clicked.connect(self._reconnect_ps)
            bar.addWidget(self.btn_ps_reconnect)

            self.lbl_ps = QLabel("未连接（点开始采集时连接 %s）" % ps_cfg.get("port"))
            self.lbl_ps.setStyleSheet("color:#666;")
            bar.addWidget(self.lbl_ps, 1)
            root.addLayout(bar)

        # ---------------- 多图（电压 / 电流 / 温度）----------------
        self.plots = TrendPlots(nch=self.nch,
                                buckets=int(self.cfg.get("ui", "downsample_buckets",
                                                         default=1600)),
                                env_channels=self.env_chs)
        self.plots.sig_user_range_changed.connect(self._on_user_range)

        # ---------------- 主体：左侧三图(3/4) + 右侧状态/日志(1/4) ----------------
        body = QHBoxLayout()
        body.setSpacing(6)
        body.addWidget(self.plots, 3)          # 左侧：三个图，各占 1/3 高度

        right = QVBoxLayout()
        right.setSpacing(6)

        gb_state = QGroupBox("状态")
        st = QVBoxLayout(gb_state)
        self.lbl_state = QLabel("○ 未开始")
        self.lbl_state.setFont(QFont("", 10, QFont.Bold))
        st.addWidget(self.lbl_state)
        self.lbl_stats = QLabel("轮数 —\n周期 —\n余量 —\n点数 —\n时长 —")
        self.lbl_stats.setWordWrap(True)
        st.addWidget(self.lbl_stats)
        self.lbl_store = QLabel("")
        self.lbl_store.setWordWrap(True)
        self.lbl_store.setFont(QFont("", 8))
        st.addWidget(self.lbl_store)
        st.addStretch(1)
        right.addWidget(gb_state, 1)           # 右上

        gb_log = QGroupBox("日志")
        lg = QVBoxLayout(gb_log)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(300)
        self.txt_log.setFont(QFont("Consolas", 8))
        lg.addWidget(self.txt_log)
        right.addWidget(gb_log, 1)             # 右下

        body.addLayout(right, 1)               # 右侧只占 1/4 宽度
        root.addLayout(body, 1)
        self._update_store_label()

    @staticmethod
    def _vline():
        f = QFrame()
        f.setFrameShape(QFrame.VLine)
        f.setFrameShadow(QFrame.Sunken)
        return f

    # ==================================================================
    #  间隔保护
    # ==================================================================
    def _update_interval_hint(self):
        cost = self.cfg.round_cost_s()
        iv = self.spin_interval.value()
        min_iv = self.cfg.min_interval_s()
        margin = iv - cost
        if iv >= min_iv:
            self.lbl_hint.setText(
                "✓ 一轮耗时 %.2f s ／ 间隔 %.2f s ／ 余量 %.2f s —— 可行"
                % (cost, iv, margin))
            self.lbl_hint.setStyleSheet("color:#1a7f37;")
        else:
            self.lbl_hint.setText(
                "⚠ 一轮耗时 %.2f s ／ 间隔 %.2f s ／ 余量只有 %.2f s（低于 20%%）"
                "—— 偶发抖动可能追不上；建议间隔 ≥ %.2f s，"
                "或把 config.yaml 的 t_settle_ms 调小"
                % (cost, iv, margin, min_iv))
            self.lbl_hint.setStyleSheet("color:#c0392b; font-weight:bold;")

    # ------------------------------------------------------------------
    #  界面状态记忆（采样间隔 / 视图跨度 / 电压源设定 …）
    # ------------------------------------------------------------------
    def _state_path(self):
        return os.path.join(os.path.dirname(os.path.abspath(self.cfg.path)),
                            STATE_FILE)

    def _load_state(self):
        try:
            with open(self._state_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_state(self):
        """把"下次打开还想保持的界面设置"存到 ui_state.json。"""
        st = {"_note": "界面状态记忆文件，删掉即恢复默认。由程序自动写入。"}
        try:
            st["interval_s"] = round(float(self.spin_interval.value()), 4)
            st["span_hours"] = round(self.span_s / 3600.0, 5)
            st["fake"] = bool(self.chk_fake.isChecked())
            if hasattr(self, "spin_span"):
                st["span_hours_input"] = round(float(self.spin_span.value()), 4)
            if hasattr(self, "spin_volt"):
                st["volt_set"] = round(float(self.spin_volt.value()), 3)
            if hasattr(self, "spin_cur"):
                st["curr_set"] = round(float(self.spin_cur.value()), 4)
            if getattr(self, "chk_ch", None):
                st["channels"] = {str(k): bool(v.isChecked())
                                  for k, v in self.chk_ch.items()}
        except Exception:
            pass
        try:
            with open(self._state_path(), "w", encoding="utf-8") as f:
                json.dump(st, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self._append_log("[!] 保存界面状态失败: %s" % str(e)[:60])

    def _restore_state(self):
        """启动时恢复上次的界面设置。"""
        st = self._load_state()
        if not st:
            return
        applied = []
        try:
            if "interval_s" in st:
                v = float(st["interval_s"])
                lo, hi = self.spin_interval.minimum(), self.spin_interval.maximum()
                v = max(lo, min(hi, v))
                self.spin_interval.setValue(v)
                applied.append("间隔 %.2f s" % v)
            if "span_hours" in st:
                v = float(st["span_hours"]) * 3600.0
                if v >= 60:
                    self.span_s = v
                    applied.append("视图 %.2f 小时" % (v / 3600.0))
            if hasattr(self, "spin_span") and "span_hours_input" in st:
                self.spin_span.setValue(float(st["span_hours_input"]))
            if hasattr(self, "spin_volt") and "volt_set" in st:
                v = float(st["volt_set"])
                v = max(self.spin_volt.minimum(), min(self.spin_volt.maximum(), v))
                self.spin_volt.setValue(v)
                applied.append("电压 %.2f V" % v)
            if hasattr(self, "spin_cur") and "curr_set" in st:
                a = float(st["curr_set"])
                a = max(self.spin_cur.minimum(), min(self.spin_cur.maximum(), a))
                self.spin_cur.setValue(a)
                applied.append("上限电流 %.3f A" % a)
            if getattr(self, "chk_ch", None) and isinstance(st.get("channels"), dict):
                for k, on in st["channels"].items():
                    cb = self.chk_ch.get(int(k)) if k.isdigit() else None
                    if cb is not None:
                        cb.setChecked(bool(on))
        except Exception as e:
            self._append_log("[!] 恢复界面状态出错: %s" % str(e)[:60])

        if applied:
            self._append_log("已恢复上次设置：%s" % "，".join(applied))
        self._update_interval_hint()

    # ------------------------------------------------------------------
    #  电压源操作
    # ------------------------------------------------------------------
    def _connect_ps(self):
        """连接电压源（在主线程，约 0.3 s）。"""
        ps_cfg = self.cfg["devices"]["powersupply"]
        if not ps_cfg.get("enabled") or self.ps is not None:
            return
        from ivcore.powersupply import PowerSupply, PowerSupplyError
        try:
            ps = PowerSupply(port=ps_cfg["port"], baud=ps_cfg["baud"],
                             addr=ps_cfg.get("addr", 0),
                             protocol=ps_cfg.get("protocol", "modbus"),
                             settle_s=ps_cfg.get("settle_s", 2.0),
                             max_voltage=ps_cfg.get("max_voltage"),
                             off_on_exit=ps_cfg.get("off_on_exit", False))
            msg = ps.open(verbose=False)
            self.ps = ps
            self.spin_volt.setValue(ps.info.get("vset") or 0.0)
            self._append_log("电压源已连接: %s" % msg)
            self._refresh_ps_label()
        except PowerSupplyError as e:
            self._append_log("[!] 电压源连接失败: %s" % e)
            self.lbl_ps.setText("连接失败：%s" % str(e)[:60])
            self.lbl_ps.setStyleSheet("color:#c0392b;")

    def _reconnect_ps(self):
        """手动重连电源（USB 拔插后不用重启软件）。"""
        from PySide6.QtWidgets import QApplication
        ps_cfg = self.cfg["devices"]["powersupply"]
        if not ps_cfg.get("enabled"):
            self._append_log("[!] 电压源在配置里是禁用的（devices.powersupply.enabled）")
            return
        self.lbl_ps.setText("正在重连…（会自动扫描所有 COM 口）")
        self.lbl_ps.setStyleSheet("color:#8a6100;")
        QApplication.processEvents()

        # 已有连接 → 直接 reconnect（会重新发现端口）
        if self.ps is not None:
            try:
                self.ps.reconnect(max_try=3, verbose=False)
                self._append_log("电压源已重连: %s"
                                 % self.ps.status_text())
                self._ps_dead = False
                self._refresh_ps_label()
                return
            except Exception as e:
                self._append_log("[!] 重连失败: %s" % str(e)[:160])
                self._ps_dead = True
                self.ps = None      # 落到下面重新建对象

        # 没有连接对象 → 重新建
        self.ps = None
        self._ps_last_read = 0.0
        self._ps_dead = False       # 电源串口是否已失联
        self._connect_ps()
        if self.ps is not None:
            self._ps_dead = False

    def _ps_health(self):
        """定期健康检查：只读状态、**不自动重连**（避免主线程卡住）。

        ★ 必须**连续失败**才判定"断开"：串口偶发丢一帧很常见，
          一次失败就报红字会让用户以为电源出问题了。
        """
        if not self.ps:
            return
        try:
            ok = self.ps.read_status(_retry=False)
        except Exception:
            ok = False
        if not ok:
            self._ps_fail_count = getattr(self, "_ps_fail_count", 0) + 1
            if self._ps_fail_count < 3:
                # 偶发失败：不改显示，等下一次
                return
            if not self._ps_dead:
                self._ps_dead = True
                self._append_log(
                    "[!] 电源串口连续 %d 次无响应（USB 掉线？）。"
                    "点电压源那一行的『重连』按钮即可恢复，不用重启软件"
                    % self._ps_fail_count)
            self.btn_out.setText("输出：?")
            self.lbl_ps.setText("⚠ 已断开（连续 %d 次无响应）—— 拔插 USB 后点『重连』"
                                % self._ps_fail_count)
            self.lbl_ps.setStyleSheet("color:#c0392b; font-weight:bold;")
            return
        # 成功
        if getattr(self, "_ps_fail_count", 0):
            if self._ps_dead:
                self._append_log("电源串口已恢复通信")
            self._ps_fail_count = 0
            self._ps_dead = False

    def _refresh_ps_label(self):
        if not self.ps:
            return
        try:
            ok = self.ps.read_status(_retry=False)
        except Exception as e:
            self.lbl_ps.setText("读取状态失败: %s" % str(e)[:50])
            self.lbl_ps.setStyleSheet("color:#c0392b;")
            return
        if not ok:
            self._ps_health()
            return
        i = self.ps.info
        self.btn_out.setText("输出：%s" % ("开" if i["out_on"] else "关"))
        self.btn_out.setChecked(bool(i["out_on"]))
        # ★ "实测"和"设定"必须区分清楚：恒压时两者电压接近、电流由负载决定；
        #   恒流时电流达到上限、电压被负载拉低。用户很容易把"实测 120mA"
        #   当成"设定 500mA 没生效"，所以这里用箭头分开、单位统一成 mA。
        mode_txt = ("恒压 CV" if i["cv"]
                    else "恒流 CC（已到限流，电压由负载决定）")
        self.lbl_ps.setText(
            "实测 → %.3f V / %.1f mA     设定 ← %.2f V / %.0f mA     %s%s"
            % (i["volt"], i["curr"] * 1000.0, i["vset"], i["iset"] * 1000.0,
               mode_txt, "   ⚠报警" if i["alarm"] else ""))
        color = "#c0392b" if i["alarm"] else "#1a7f37" if i["out_on"] else "#666"
        self.lbl_ps.setStyleSheet("color:%s;" % color)

    def _apply_voltage(self):
        if not self.ps:
            self._connect_ps()
        if not self.ps:
            return
        from PySide6.QtWidgets import QApplication
        v = self.spin_volt.value()
        self.lbl_ps.setText("正在设置 %.2f V（等稳定 %.1f s）…"
                            % (v, self.ps.settle_s))
        self.lbl_ps.setStyleSheet("color:#8a6100;")
        QApplication.processEvents()
        try:
            self.ps.set_voltage(v)
            self._append_log("电压源设为 %.2f V" % v)
            # ★ 设定 vs 实测 校验：偏差大就提示（本机 2.99~3.06V 有已知偏高）
            lv = getattr(self.ps, "last_verify", {}) or {}
            if lv.get("warn"):
                self._append_log("[!] " + lv["warn"])
            elif lv:
                self._append_log("   校验：实测 %.3f V（偏差 %+.3f V，%s）"
                                 % (lv.get("volt", 0), lv.get("diff", 0),
                                    "恒压" if lv.get("cv") else "恒流"))
        except Exception as e:
            self._append_log("[!] 设置电压失败: %s" % e)
        self._refresh_ps_label()

    def _apply_current(self):
        """设置上限电流（限流值）。"""
        if not self.ps:
            self._connect_ps()
        if not self.ps:
            return
        from PySide6.QtWidgets import QApplication
        a = self.spin_cur.value()
        self.lbl_ps.setText("正在设置上限电流 %.3f A …" % a)
        self.lbl_ps.setStyleSheet("color:#8a6100;")
        QApplication.processEvents()
        try:
            self.ps.set_current(a)
            self._append_log("电压源上限电流设为 %.3f A" % a)
        except Exception as e:
            self._append_log("[!] 设置上限电流失败: %s" % e)
        self._refresh_ps_label()

    def _toggle_output(self):
        if not self.ps:
            self._connect_ps()
        if not self.ps:
            return
        # ★ 先刷新真实状态再决定要切到哪个方向。
        #   之前直接用缓存的 self.ps.out_on，如果那之后设备状态变了
        #   （面板按键、拔插 USB 复位、上一次读失败），就会切反。
        try:
            self.ps.read_status(_retry=False)
        except Exception:
            pass
        want = not bool(self.ps.out_on)
        try:
            self.ps.output(want)
            self._append_log("电压源输出 → %s" % ("开" if want else "关"))
        except Exception as e:
            self._append_log("[!] 输出开关失败: %s" % e)
            # 失败时把按钮状态纠正回设备的真实状态
            try:
                self.ps.read_status(_retry=False)
            except Exception:
                pass
        self._refresh_ps_label()

    # ==================================================================
    #  开始 / 停止
    # ==================================================================
    def _start(self):
        if self.worker and self.worker.isRunning():
            return

        self.fake_mode = self.chk_fake.isChecked()
        iv = self.spin_interval.value()

        # 把界面上的间隔写回配置（存储快照与预估都用它）
        self.cfg.raw["acquisition"]["interval_s"] = iv

        # 电压源（真实模式下才连）
        if not self.fake_mode:
            self._connect_ps()

        self.buf.clear()
        self.plots.clear()
        self._reset_auto_fit()
        self.plots.set_y_auto()          # ★ 新一轮采集开始时才重置 Y 轴自动缩放
        self.n_rounds = 0
        self.t_start = time.time()

        if self.fake_mode:
            self.worker = FakeWorker(self.cfg, interval=0.2, nch=self.nch,
                                     env_channels=self.env_chs)
            self.worker.sig_env.connect(self._on_env)     # 假温度也走同一条链路
        else:
            self.worker = AcquisitionWorker(self.cfg, interval=iv,
                                            env_queue=self.env_queue)

        self.worker.sig_state_msg.connect(self._on_state)
        self.worker.sig_log.connect(self._append_log)
        self.worker.sig_round.connect(self._on_round)
        self.worker.sig_stats.connect(self._on_stats)
        self.worker.sig_failed.connect(self._on_failed)
        if hasattr(self.worker, "sig_env"):
            self.worker.sig_env.connect(self._on_env)   # 温度（与测量同一轮）
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.chk_fake.setEnabled(False)
        self.spin_interval.setEnabled(False)
        self._append_log("——— 开始采集（间隔 %.2f s，%s%s）———"
                         % (iv, "模拟模式" if self.fake_mode else "真实硬件",
                            "，温度随每轮采集" if self.env_chs else ""))

    def _stop(self):
        # ★ 如果配置要求"停止时也关电源"，先关（防火）
        if self.cfg.get("safety", "on_stop_off", default=False) and self.ps:
            self._shutdown_power("停止采集（安全设置 on_stop_off）")
        if self.worker and self.worker.isRunning():
            self.btn_stop.setEnabled(False)
            self._append_log("正在停止（等当前轮结束并收尾）…")
            self.worker.stop()

    # ------------------------------------------------------------------
    def _on_env(self, ts, vals):
        """收到一轮温度（界面线程）：更新显示缓冲 + 温度上限保护。"""
        self.buf.add_env(ts, vals)
        self._check_overheat(vals)

    def _check_overheat(self, vals):
        """★ 温度上限保护：超过 safety.max_temp_c 就立刻关电源输出。"""
        try:
            lim = float(self.cfg.get("safety", "max_temp_c", default=0) or 0)
        except Exception:
            lim = 0.0
        if lim <= 0:
            return
        hot = [(ch, v) for ch, v in (vals or {}).items()
               if v is not None and v > lim]
        if not hot:
            if self._overheat_tripped:
                self._append_log("[安全] 温度已回落到 %.1f℃ 以下（输出不会自动重开，"
                                 "请确认后手动开启）" % lim)
                self._overheat_tripped = False
            return
        if self._overheat_tripped or not self.ps:
            return
        self._overheat_tripped = True
        ch, t = max(hot, key=lambda x: x[1])
        self._append_log("=" * 60)
        self._append_log("[安全][!] 温度 T%d = %.1f℃ 超过上限 %.1f℃ → 立即关闭电源输出！"
                         % (ch, t, lim))
        self._append_log("=" * 60)
        self._shutdown_power("温度超限 %.1f℃" % t)

    # ==================================================================
    #  采集线程回调（全部在界面线程执行）
    # ==================================================================
    def _on_state(self, state, msg):
        icon = {"connecting": "◌", "running": "●", "stopping": "◌",
                "stopped": "○", "error": "×"}.get(state, "○")
        self.lbl_state.setText("%s %s" % (icon, msg))
        color = {"running": "#1a7f37", "error": "#c0392b",
                 "stopped": "#666"}.get(state, "#8a6100")
        self.lbl_state.setStyleSheet("color:%s;" % color)

        if state in ("stopped", "error"):
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(False)
            self.chk_fake.setEnabled(True)
            self.spin_interval.setEnabled(True)
            self._update_store_label()

    def _on_round(self, rno, payload):
        samples, t_round, lag = payload
        self.buf.add_round(samples)
        self.n_rounds = rno
        self.last_lag = lag
        # 记住本轮各路数值，供状态栏显示
        self.last_samples = {}
        for (ts, ch, mode, val, unit, q) in samples:
            if val is not None:
                self.last_samples[(ch, mode)] = val

    def _on_stats(self, stats):
        if stats:
            self._append_log("统计：轮数 %s ／ 数据点 %s ／ 读数失败 %s ／ 超时轮 %s"
                             % (stats.get("rounds", "-"), stats.get("samples", "-"),
                                stats.get("failed_reads", "-"),
                                stats.get("slow_rounds", "-")))

    def _on_failed(self, msg):
        self._append_log("[×] 失败：%s" % msg)
        self.lbl_state.setText("× 失败")
        self.lbl_state.setStyleSheet("color:#c0392b; font-weight:bold;")
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.chk_fake.setEnabled(True)
        self.spin_interval.setEnabled(True)

    # ==================================================================
    #  视图范围
    # ==================================================================
    def _on_span_changed(self):
        rb = self.sender()
        if not rb or not rb.isChecked():
            return
        self.span_s = rb.property("span")
        self._apply_span("预设 %s" % rb.text())

    def _on_custom_span(self):
        """自定义视图跨度（单位：小时）。"""
        hours = float(self.spin_span.value())
        if hours <= 0:
            return
        self.span_s = hours * 3600.0
        self._apply_span("自定义 %.2f 小时" % hours)

    def _set_x(self, t0, t1):
        """由程序设置 X 范围，并记住它（供 _detect_user_zoom 对比）。"""
        self.plots.set_x_range(t0, t1)
        self._last_set_xr = (t0, t1)

    def _detect_user_zoom(self):
        """检测用户是否手动缩放/平移过 X 轴。

        不依赖 pyqtgraph 的 sigRangeChangedManually（滚轮时不可靠），
        而是把"当前实际范围"与"上次程序设置的范围"对比：
        只要明显不同，就认定用户动过手 → 永久停止自动跟随。
        """
        if not self.follow or self._last_set_xr is None:
            return False
        try:
            cur = self.plots.x_range()
        except Exception:
            return False
        tol = max(1.0, abs(self.span_s) * 0.005)   # 0.5% 或 1 秒
        if (abs(cur[0] - self._last_set_xr[0]) > tol
                or abs(cur[1] - self._last_set_xr[1]) > tol):
            self.follow = False
            if hasattr(self, "chk_follow"):
                self.chk_follow.setChecked(False)
            span = cur[1] - cur[0]
            if span > 1:
                # 顺便把"视图跨度"调成用户缩放后的实际宽度，
                # 这样后续刷新取数据也按这个宽度来，不会画出奇怪的内容
                self.span_s = span
            self._append_log("检测到手动缩放/平移 → 已停止自动跟随"
                             "（想恢复请点『跟随最新』或重新选视图跨度）")
            return True
        return False

    def _apply_span(self, label):
        """立刻应用新的视图跨度：重新跟随 + 立即设置 X 轴范围 + 强制刷新。"""
        self.follow = True
        self._auto_fit = False              # ★ 用户明确选了跨度，就严格按它显示
        self._last_set_xr = None            # 重置对比基线，避免误判
        if hasattr(self, "chk_follow"):
            self.chk_follow.setChecked(True)
        self._hist_query_t = 0.0            # 历史视图下次刷新必须重查
        now = time.time()
        self._set_x(now - self.span_s, now)   # ★ 立即改横坐标
        self._append_log("视图跨度 → %s（%.2f 小时）" % (label, self.span_s / 3600.0))
        self._refresh()

    def _reset_auto_fit(self):
        """启动新一轮采集时恢复"自动贴合"。"""
        self._auto_fit = True

    def _on_follow_changed(self, on):
        self.follow = bool(on)

    def _on_user_range(self):
        """用户手动缩放/平移 → 自动取消跟随，避免打架。"""
        if self.chk_follow.isChecked():
            self.chk_follow.setChecked(False)
            self._append_log("检测到手动缩放/平移，已取消『跟随最新』")

    # ==================================================================
    #  定时刷新
    # ==================================================================
    def _refresh(self):
        now = time.time()
        # 先看用户是不是手动缩放/平移过（滚轮也算）；是就不再自动改范围
        self._detect_user_zoom()
        # ★ 是否用内存缓冲：只看"跨度是否在缓冲能覆盖的范围内"。
        #   之前还带了个 (self.follow or ...) 的条件，导致一旦手动缩放（follow=False）
        #   就强制走数据库分支，而数据库历史查询那条路对温度的取数容易出问题
        #   —— 表现就是"滚轮缩放横坐标后温度曲线消失"。
        #   缓冲 maxlen=60000，按 2 秒一轮能覆盖 30 小时以上，所以 2 小时内完全够用。
        realtime = self.span_s <= 7200

        if realtime:
            t1 = now
            span_t0 = now - self.span_s
            # 只有"自动贴合"模式（刚启动、数据不足以铺满跨度）才从数据起点显示；
            # 用户明确选过跨度后，就严格按所选跨度显示（曲线短一点很正常）。
            if self._auto_fit:
                tmin, _tmax = self.buf.span()
                if tmin is not None and tmin > span_t0:
                    # 数据还不够铺满跨度 → 从数据起点开始显示
                    t0 = tmin
                    if (t1 - tmin) >= self.span_s * 0.95:
                        self._auto_fit = False      # 铺满了，自然过渡到固定跨度
                else:
                    t0 = span_t0
                    # ★ 只有"确实有数据"且已经铺满跨度时才退出贴合模式；
                    #   没数据时（刚启动）保持 _auto_fit=True，否则一旦有一次
                    #   空刷新就会永久退化成大跨度视图，导致曲线挤在最右边看不见。
                    if tmin is not None and (t1 - tmin) >= self.span_s * 0.95:
                        self._auto_fit = False
            else:
                t0 = span_t0
            data = {}
            for ch in range(1, self.nch + 1):
                for mode in ("V", "I"):
                    data[(ch, mode)] = self.buf.arrays(ch, mode, t0, t1)
            for ch in self.env_chs:
                data[(ch, "T")] = self.buf.arrays(ch, "T", t0, t1)
            self.plots.update_data(data)
            if self.follow:
                self._set_x(t0, t1)
            self._hist_info = "内存缓冲（实时）"
        else:
            # 历史视图：从数据库查（每 10 秒最多查一次，避免频繁 IO）
            if now - self._hist_query_t > 10.0:
                self._hist_query_t = now
                t1 = now
                t0 = now - self.span_s
                data, (table, is_agg) = query_history(self.root, t0, t1, self.nch)
                for ch in self.env_chs:
                    data[(ch, "T")] = query_history_env(self.root, t0, t1, [ch])[ch]
                self.plots.update_data(data)
                self._hist_info = "数据库 %s（跨天合并）" % table
                if self.follow:
                    self._set_x(t0, t1)

        # ★ 不要每帧强制 Y 轴自动缩放！
        #   之前这里调 set_y_auto() 会把用户手动缩放的 Y 轴打回自动，
        #   表现为"滚轮缩放纵坐标后图表自己刷新"，而且三个图的 Y 会被一起重置
        #   （看起来就像"缩放温度的纵坐标影响了电信号图"）。
        #   pyqtgraph 的 Y 轴 autoRange 默认是开的，用户一旦手动缩放它会自己关掉，
        #   所以我们只要**不主动干预**即可。新一轮采集开始时才重置（见 _start）。
        self._update_status(now)

        # ★ 电源串口健康检查（每 5 秒一次，只读不重连，避免卡界面）
        if self.ps is not None and (now - self._ps_last_read) >= 5.0:
            self._ps_last_read = now
            self._ps_health()

    def _update_status(self, now):
        lag = self.last_lag
        lag_txt = "%+.2f s" % lag if self.n_rounds else "—"
        dur = (now - self.t_start) if self.t_start else 0
        h, rem = divmod(int(dur), 3600)
        m, s = divmod(rem, 60)

        lines = [
            "轮数 %d" % self.n_rounds,
            "周期 %.2f s" % self.spin_interval.value(),
            "余量 %s" % lag_txt,
            "点数 %d" % self.buf.n_total,
            "时长 %d:%02d:%02d" % (h, m, s),
            "来源 %s" % (self._hist_info or "—"),
        ]

        # ★ 四路电压 / 电流的当前值
        lines.append("")
        for ch in range(1, self.nch + 1):
            v = self.last_samples.get((ch, "V"))
            i = self.last_samples.get((ch, "I"))
            lines.append("通道%d  V %-10s I %s"
                         % (ch,
                            "—" if v is None else _fmt_num(v, "V"),
                            "—" if i is None else _fmt_num(i, "A")))

        # 温度与温差（ΔT 是用户最关心的）
        if self.env_chs:
            lines.append("")
            vals = []
            for ch in self.env_chs:
                v = self.buf.env_latest.get(ch)
                vals.append(v)
                lines.append("T%d   %s" % (ch, "—" if v is None else "%.1f ℃" % v))
            if len(self.env_chs) >= 2 and vals[0] is not None and vals[1] is not None:
                lines.append("ΔT   %.1f ℃" % (vals[0] - vals[1]))

        self.lbl_stats.setText("\n".join(lines))

    def _update_store_label(self):
        try:
            du = shutil.disk_usage(self.root if os.path.isdir(self.root) else ".")
            free_gb = du.free / (1024.0 ** 3)
        except Exception:
            free_gb = 0
        dbs = list_databases(self.root)
        db_txt = os.path.basename(dbs[-1]) if dbs else "(今天还没有数据文件)"
        warn = ""
        if free_gb < 1:
            warn = "  ⚠ 磁盘空间不足！"
        self.lbl_store.setText(
            "存储目录：%s\n当前文件：%s\n磁盘剩余：%.1f GB%s\n数据库：%d 个（跨天）"
            % (self.root, db_txt, free_gb, warn, len(dbs)))

    # ==================================================================
    def _append_log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.txt_log.appendPlainText("[%s] %s" % (ts, msg))

    # ==================================================================
    # ------------------------------------------------------------------
    #  ★ 安全：关闭电源输出（防火）
    # ------------------------------------------------------------------
    def _shutdown_power(self, reason="关闭程序"):
        """尽最大努力关闭电源输出。返回 True 表示已确认关闭。"""
        if not self.ps:
            return True
        # 连试 3 次（设备偶发不响应）
        for attempt in range(3):
            try:
                self.ps.output(False)
                self._append_log("[安全] %s：已关闭电源输出" % reason)
                return True
            except Exception as ex:
                self._append_log("[安全] 第 %d 次关输出失败：%s"
                                 % (attempt + 1, str(ex)[:70]))
                time.sleep(0.3)
        # 兜底：直接把输出关掉（用底层写控制字）
        try:
            self.ps.read_status()
            vs = self.ps.info.get("v_scale", 100.0)
            is_ = self.ps.info.get("i_scale", 1000.0)
            v_raw = int(round((self.ps.set_v or 0) * vs))
            i_raw = int(round((self.ps.set_a or 1.0) * is_))
            if self.ps._write3(0x0000, v_raw, i_raw):     # 控制字全 0 = 输出关
                self._append_log("[安全] 已用兜底方式关闭电源输出")
                return True
        except Exception as ex:
            self._append_log("[安全][!] 兜底关输出也失败：%s" % str(ex)[:70])
        self._append_log("[安全][!] ⚠ 电源输出可能仍处于开启状态，"
                         "请立刻手动关闭电源或拔掉输出线！")
        return False

    def closeEvent(self, e):
        # ★ 记住界面设置（下次打开还是这些值）
        self._save_state()

        # ★★★ 第一优先：先关电源输出（防止加热器无人看管）★★★
        if self.ps:
            self._shutdown_power("关闭窗口")

        if self.env_worker and self.env_worker.isRunning():
            self._append_log("关闭窗口：正在停止温度采集…")
            self.env_worker.stop()
            self.env_worker.wait(3000)
        if self.worker and self.worker.isRunning():
            self._append_log("关闭窗口：正在让采集线程优雅收尾…")
            self.worker.stop()
            if not self.worker.wait(15000):
                self._append_log("[!] 收尾超时，强制退出（数据已落盘，可事后校验）")

        # 再关一次（采集线程收尾期间可能有其他写入）
        if self.ps:
            try:
                self.ps.output(False)
            except Exception:
                pass
            try:
                self.ps.close(off_output=True)
            except Exception:
                pass
            self.ps = None
        e.accept()
