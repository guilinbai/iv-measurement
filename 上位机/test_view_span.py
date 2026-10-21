# -*- coding: utf-8 -*-
"""无头验证：视图跨度切换是否真的改变了 X 轴范围。"""
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PySide6.QtWidgets import QApplication

from ivcore.config import Config
from ui.main_window import MainWindow


def xr(win):
    a, b = win.plots.x_range()
    return b - a, a, b


def main():
    app = QApplication(sys.argv)
    cfg = Config(os.path.join(HERE, "config.yaml"))
    win = MainWindow(cfg)
    win.show()

    # 走模拟模式，产生一些数据
    win.chk_fake.setChecked(True)
    win._start()
    t0 = time.time()
    while time.time() - t0 < 3.0:
        app.processEvents()
        time.sleep(0.05)

    print("=" * 70)
    print("验证视图跨度切换")
    print("=" * 70)

    # ---- 1) 预设按钮 ----
    for i, (name, sec) in enumerate([("10 分钟", 600), ("1 小时", 3600),
                                     ("1 天", 86400), ("7 天", 604800)]):
        btn = win.span_group.button(i)
        btn.setChecked(True)
        app.processEvents()
        span, a, b = xr(win)
        ok = abs(span - sec) <= max(120, sec * 0.02)
        print("  预设 %-6s -> X 跨度 %8.1f s (期望 %8d)  %s"
              % (name, span, sec, "[OK]" if ok else "[X] 不符"))

    # ---- 2) 自定义小时 ----
    for hours in (0.5, 3.0, 12.0, 48.0):
        win.spin_span.setValue(hours)
        win._on_custom_span()
        app.processEvents()
        span, a, b = xr(win)
        want = hours * 3600
        ok = abs(span - want) <= max(120, want * 0.02)
        print("  自定义 %.2f 小时 -> X 跨度 %8.1f s (期望 %8.0f)  %s"
              % (hours, span, want, "[OK]" if ok else "[X] 不符"))

    # ---- 3) 横坐标右端应该≈当前时间 ----
    span, a, b = xr(win)
    now = time.time()
    print("\n  横坐标右端 = %.0f, 当前时间 = %.0f, 差 %.1f s  %s"
          % (b, now, now - b, "[OK]" if abs(now - b) < 120 else "[!]"))

    win.close()
    print("\n完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
