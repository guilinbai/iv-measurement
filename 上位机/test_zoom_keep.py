# -*- coding: utf-8 -*-
"""无头验证：模拟用户滚轮缩放后，范围是否会被自动刷新重置。"""
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PySide6.QtWidgets import QApplication

from ivcore.config import Config
from ui.main_window import MainWindow


def spin(app, sec=0.6):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents()
        time.sleep(0.02)


def main():
    app = QApplication(sys.argv)
    cfg = Config(os.path.join(HERE, "config.yaml"))
    win = MainWindow(cfg)
    win.show()
    win.chk_fake.setChecked(True)
    win._start()
    spin(app, 4.0)                      # 攒点数据

    # 设成 1 小时视图
    win.spin_span.setValue(1.0)
    win._on_custom_span()
    spin(app, 0.5)

    a, b = win.plots.x_range()
    print("=" * 72)
    print("模拟滚轮缩放测试")
    print("=" * 72)
    print("  缩放前: 跨度 %.1f s   follow=%s" % (b - a, win.follow))

    # 模拟滚轮放大 5 倍（以屏幕中心为锚点）
    vb = win.plots.p_v.getViewBox()
    vb.scaleBy((0.2, 1.0))               # X 缩到 1/5
    app.processEvents()
    spin(app, 0.2)

    a1, b1 = win.plots.x_range()
    span1 = b1 - a1
    print("  缩放后: 跨度 %.1f s   follow=%s" % (span1, win.follow))

    # 让刷新定时器跑几轮，看范围会不会被重置
    spin(app, 2.0)
    a2, b2 = win.plots.x_range()
    span2 = b2 - a2
    print("  等 2 秒刷新后: 跨度 %.1f s   follow=%s" % (span2, win.follow))

    ok = abs(span2 - span1) < max(1.0, span1 * 0.02)
    print()
    print("  结论: %s" % ("[OK] 缩放被保留了，没有被自动重置"
                        if ok else
                        "[X] 缩放被重置了（%.1f -> %.1f）" % (span1, span2)))

    # 再验证"点跟随最新"能恢复自动滚动
    print()
    print("  --- 点『跟随最新』应恢复自动滚动 ---")
    win.chk_follow.setChecked(True)
    win.follow = True
    win._auto_fit = False
    win.span_s = 3600.0
    win._last_set_xr = None
    spin(app, 1.2)
    a3, b3 = win.plots.x_range()
    print("  恢复后: 跨度 %.1f s   follow=%s   右端距现在 %.1f s"
          % (b3 - a3, win.follow, time.time() - b3))

    win.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
