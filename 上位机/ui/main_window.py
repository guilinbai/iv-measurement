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

import os
import shutil
import time
from datetime import datetime

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QDoubleSpinBox, QFrame,
                               QGroupBox, QHBoxLayout, QLabel, QPlainTextEdit,
                               QPushButton, QRadioButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from .buffer import TrendBuffer, query_history, query_count, list_databases
from .plots import TrendPlots
from .worker import AcquisitionWorker, FakeWorker

# 视图范围：名称 -> 秒
SPANS = [("10 分钟", 600), ("1 小时", 3600), ("1 天", 86400), ("7 天", 604800)]


class MainWindow(QWidget):
    def __init__(self, cfg, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.nch = int(cfg["acquisition"]["channels"])
        self.root = cfg["storage"]["dir"]

        self.worker = None
        self.buf = TrendBuffer(nch=self.nch, maxlen=60000)
        self.span_s = 3600                 # 当前视图跨度
        self.follow = True
        self.fake_mode = False
        self.t_start = None
        self.n_rounds = 0
        self.last_lag = 0.0
        self._hist_query_t = 0.0
        self._hist_info = ""

        self.setWindowTitle("4 路 IV 测量 · 实时监视  (v0.2)")
        self.resize(1280, 860)
        self._build_ui()

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

        # ---------------- 双图 ----------------
        self.plots = TrendPlots(nch=self.nch,
                                buckets=int(self.cfg.get("ui", "downsample_buckets",
                                                         default=1600)))
        self.plots.sig_user_range_changed.connect(self._on_user_range)
        root.addWidget(self.plots, 1)

        # ---------------- 状态 + 日志 ----------------
        box = QHBoxLayout()
        box.setSpacing(8)

        gb_state = QGroupBox("状态")
        st = QVBoxLayout(gb_state)
        self.lbl_state = QLabel("○ 未开始")
        self.lbl_state.setFont(QFont("", 10, QFont.Bold))
        st.addWidget(self.lbl_state)
        self.lbl_stats = QLabel("轮数 —   周期 —   余量 —   点数 —   时长 —")
        st.addWidget(self.lbl_stats)
        self.lbl_store = QLabel("")
        self.lbl_store.setWordWrap(True)
        st.addWidget(self.lbl_store)
        st.addStretch(1)
        box.addWidget(gb_state, 2)

        gb_log = QGroupBox("日志")
        lg = QVBoxLayout(gb_log)
        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(300)
        self.txt_log.setFont(QFont("Consolas", 8))
        lg.addWidget(self.txt_log)
        box.addWidget(gb_log, 3)

        root.addLayout(box, 0)
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
                "⚠ 一轮耗时 %.2f s 但间隔只有 %.2f s（余量 %.2f s）—— "
                "数据会持续滞后！建议间隔 ≥ %.2f s，或调小 config.yaml 里的 timing"
                % (cost, iv, margin, min_iv))
            self.lbl_hint.setStyleSheet("color:#c0392b; font-weight:bold;")

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

        self.buf.clear()
        self.plots.clear()
        self.n_rounds = 0
        self.t_start = time.time()

        if self.fake_mode:
            self.worker = FakeWorker(self.cfg, interval=0.2, nch=self.nch)
        else:
            self.worker = AcquisitionWorker(self.cfg, interval=iv)

        self.worker.sig_state_msg.connect(self._on_state)
        self.worker.sig_log.connect(self._append_log)
        self.worker.sig_round.connect(self._on_round)
        self.worker.sig_stats.connect(self._on_stats)
        self.worker.sig_failed.connect(self._on_failed)
        self.worker.start()

        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.chk_fake.setEnabled(False)
        self.spin_interval.setEnabled(False)
        self._append_log("——— 开始采集（间隔 %.2f s，%s）———"
                         % (iv, "模拟模式" if self.fake_mode else "真实硬件"))

    def _stop(self):
        if self.worker and self.worker.isRunning():
            self.btn_stop.setEnabled(False)
            self._append_log("正在停止（等当前轮结束并收尾）…")
            self.worker.stop()

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
        self._hist_query_t = 0.0          # 强制下次刷新时重新查询
        self._append_log("视图范围 → %s" % rb.text())

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
        realtime = self.span_s <= 3600 and (self.follow or self.worker is None)

        if realtime:
            t1 = now
            span_t0 = now - self.span_s
            # 数据还不够铺满视图跨度时（刚启动），从数据起点开始显示，
            # 否则曲线会全部挤在最右边一条线上，看不出形状。
            tmin, _tmax = self.buf.span()
            t0 = span_t0 if (tmin is None or tmin <= span_t0) else tmin
            data = {}
            for ch in range(1, self.nch + 1):
                for mode in ("V", "I"):
                    data[(ch, mode)] = self.buf.arrays(ch, mode, t0, t1)
            self.plots.update_data(data)
            if self.follow:
                self.plots.set_x_range(t0, t1)
            self._hist_info = "内存缓冲（实时）"
        else:
            # 历史视图：从数据库查（每 10 秒最多查一次，避免频繁 IO）
            if now - self._hist_query_t > 10.0:
                self._hist_query_t = now
                t1 = now
                t0 = now - self.span_s
                data, (table, is_agg) = query_history(self.root, t0, t1, self.nch)
                self.plots.update_data(data)
                self._hist_info = "数据库 %s（跨天合并）" % table
                if not self.follow:
                    self.plots.set_x_range(t0, t1)

        self.plots.set_y_auto()
        self._update_status(now)

    def _update_status(self, now):
        lag = self.last_lag
        lag_txt = "%+.2f s" % lag if self.n_rounds else "—"
        dur = (now - self.t_start) if self.t_start else 0
        h, rem = divmod(int(dur), 3600)
        m, s = divmod(rem, 60)
        self.lbl_stats.setText(
            "轮数 %d   周期 %.2f s   余量 %s   点数 %d   时长 %d:%02d:%02d   来源 %s"
            % (self.n_rounds, self.spin_interval.value(), lag_txt,
               self.buf.n_total, h, m, s, self._hist_info or "—"))

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
    def closeEvent(self, e):
        if self.worker and self.worker.isRunning():
            self._append_log("关闭窗口：正在让采集线程优雅收尾…")
            self.worker.stop()
            if not self.worker.wait(15000):
                self._append_log("[!] 收尾超时，强制退出（数据已落盘，可事后校验）")
        e.accept()
