# -*- coding: utf-8 -*-
"""
双图组件：上=电压，下=电流，共享统一时间轴（X 轴联动）
================================================================================
  · X 轴用 pyqtgraph 的 DateAxisItem → 直接显示日期时间
  · 两个图用 setXLink 联动：缩放/平移任意一个，另一个同步
  · 绘图前先降采样到约 buckets 个点（默认 ~1600 ≈ 屏幕像素宽）
    → 无论数据是几百点还是几十万点，绘图耗时都一样
  · Y 轴启用 SI 前缀自动缩放（1e-5 A 会显示成 10 µA，1e-4 V 显示成 100 µV）
"""

import numpy as np
import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from .buffer import downsample_minmax

# 4 个通道的固定配色（电压/电流两图用同一套颜色，便于对照）
CH_COLORS = {1: (31, 119, 180), 2: (255, 127, 14),
             3: (44, 160, 44), 4: (214, 39, 40)}

# 温度通道配色：ch1 红（热端）、ch2 蓝（冷端），红蓝最直观
T_COLORS = {1: (214, 39, 40), 2: (31, 119, 180),
            3: (44, 160, 44), 4: (140, 86, 75)}

pg.setConfigOptions(antialias=False)      # 大数据量时关抗锯齿，快很多
pg.setConfigOption("background", "w")
pg.setConfigOption("foreground", "k")


class TrendPlots(QWidget):
    sig_user_range_changed = Signal()      # 用户手动缩放/平移了

    def __init__(self, nch=4, buckets=1600, env_channels=None, parent=None):
        super().__init__(parent)
        self.nch = nch
        self.buckets = buckets
        self.env_channels = list(env_channels or [])
        self._setting_range = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)

        # ---------------- 电压图 ----------------
        self.p_v = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.p_v.setLabel("left", "Voc（电压）")
        self.p_v.showGrid(x=True, y=True, alpha=0.25)
        self.p_v.addLegend(offset=(8, 8), labelTextSize="9pt")
        self.p_v.getAxis("bottom").setStyle(showValues=False)   # 上图不重复显示时间
        self.p_v.setMinimumHeight(140)

        # ---------------- 电流图 ----------------
        self.p_i = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.p_i.setLabel("left", "Isc（电流）")
        self.p_i.showGrid(x=True, y=True, alpha=0.25)
        self.p_i.addLegend(offset=(8, 8), labelTextSize="9pt")
        self.p_i.setMinimumHeight(140)
        self.p_i.getAxis("bottom").setStyle(showValues=not self.env_channels)

        # ---------------- 温度图（仅当启用了温度采集）----------------
        self.p_t = None
        if self.env_channels:
            self.p_t = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
            self.p_t.setLabel("left", "温度（℃）")
            self.p_t.setLabel("bottom", "时间")
            self.p_t.showGrid(x=True, y=True, alpha=0.25)
            self.p_t.addLegend(offset=(8, 8), labelTextSize="9pt")
            self.p_t.setMinimumHeight(120)
        else:
            self.p_i.setLabel("bottom", "时间")

        # ★ X 轴联动：三个图共享同一时间轴（缩放/平移任一个，其余同步）
        self.p_i.setXLink(self.p_v)
        if self.p_t is not None:
            self.p_t.setXLink(self.p_v)

        # ---------------- 曲线 ----------------
        self.curves = {}          # (ch, mode) -> PlotDataItem
        self.enabled = {ch: True for ch in range(1, nch + 1)}
        for ch in range(1, nch + 1):
            r, g, b = CH_COLORS.get(ch, (0, 0, 0))
            pen = pg.mkPen((r, g, b), width=1.4)
            self.curves[(ch, "V")] = self.p_v.plot(
                pen=pen, name="ch%d" % ch, autoDownsample=False, clipToView=True)
            self.curves[(ch, "I")] = self.p_i.plot(
                pen=pen, name="ch%d" % ch, autoDownsample=False, clipToView=True)
        for ch in self.env_channels:
            r, g, b = T_COLORS.get(ch, (100, 100, 100))
            self.curves[(ch, "T")] = self.p_t.plot(
                pen=pg.mkPen((r, g, b), width=1.8), name="T%d" % ch,
                autoDownsample=False, clipToView=True)

        lay.addWidget(self.p_v, 3)
        lay.addWidget(self.p_i, 3)
        if self.p_t is not None:
            lay.addWidget(self.p_t, 2)

        # 记录"最后画过的数据"，用于只改变通道显隐时快速重画
        self._last = {}

        # 监听用户操作
        self.p_v.getViewBox().sigRangeChangedManually.connect(self._on_manual)

    # ------------------------------------------------------------------
    def _on_manual(self):
        if not self._setting_range:
            self.sig_user_range_changed.emit()

    # ------------------------------------------------------------------
    def set_channel_visible(self, ch, visible):
        self.enabled[ch] = bool(visible)
        for mode in ("V", "I", "T"):
            c = self.curves.get((ch, mode))
            if c is not None:
                c.setVisible(self.enabled[ch])

    # ------------------------------------------------------------------
    def set_x_range(self, t0, t1, emit_guard=True):
        """外部设置时间范围（不会被当成"用户操作"）。"""
        self._setting_range = True
        try:
            self.p_v.setXRange(t0, t1, padding=0)
        finally:
            self._setting_range = False

    def x_range(self):
        return self.p_v.getViewBox().viewRange()[0]

    # ------------------------------------------------------------------
    def update_data(self, data, buckets=None):
        """更新曲线。

        data : dict {(ch, mode): (ts_array, val_array)}
        """
        b = buckets or self.buckets
        for (ch, mode), (ts, vals) in data.items():
            curve = self.curves.get((ch, mode))
            if curve is None:
                continue
            if ts is None or len(ts) == 0:
                curve.setData([], [])
                self._last[(ch, mode)] = (ts, vals)
                continue
            if len(ts) > b:
                t_out, _vmin, _vmax, vavg = downsample_minmax(ts, vals, b)
                curve.setData(t_out, vavg)
            else:
                curve.setData(ts, vals)
            self._last[(ch, mode)] = (ts, vals)

    # ------------------------------------------------------------------
    def redraw_last(self, buckets=None):
        if self._last:
            self.update_data(self._last, buckets)

    def clear(self):
        self._last = {}
        for c in self.curves.values():
            c.setData([], [])

    # ------------------------------------------------------------------
    def set_y_auto(self, mode=None):
        if mode in (None, "V"):
            self.p_v.enableAutoRange(axis="y")
        if mode in (None, "I"):
            self.p_i.enableAutoRange(axis="y")
        if mode in (None, "T") and self.p_t is not None:
            self.p_t.enableAutoRange(axis="y")

    def set_y_range(self, mode, lo, hi):
        w = {"V": self.p_v, "I": self.p_i, "T": self.p_t}.get(mode)
        if w is not None:
            w.setYRange(lo, hi, padding=0)
