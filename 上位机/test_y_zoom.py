# -*- coding: utf-8 -*-
"""无头验证：Y 轴手动缩放是否被保留；缩放温度 Y 是否影响电信号图。"""
import os
import sys
import time

os.environ["QT_QPA_PLATFORM"] = "offscreen"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from PySide6.QtWidgets import QApplication

from ivcore.config import Config
from ui.main_window import MainWindow


def spin(app, sec):
    t0 = time.time()
    while time.time() - t0 < sec:
        app.processEvents()
        time.sleep(0.02)


def yrange(widget):
    return widget.getViewBox().viewRange()[1]


def fmt(r):
    return "[%.3f, %.3f]" % (r[0], r[1])


def main():
    app = QApplication(sys.argv)
    cfg = Config(os.path.join(HERE, "config.yaml"))
    win = MainWindow(cfg)
    win.show()
    win.chk_fake.setChecked(True)
    win._start()
    spin(app, 4.0)

    pv, pi, pt = win.plots.p_v, win.plots.p_i, win.plots.p_t
    print("=" * 74)
    print("Y 轴缩放保持测试")
    print("=" * 74)

    # --- 1) 缩放电压图的 Y ---
    y0 = yrange(pv)
    pv.getViewBox().scaleBy((1.0, 0.4))
    app.processEvents()
    y1 = yrange(pv)
    print("  ① 电压图 Y: %s -> %s (缩到 40%%)" % (fmt(y0), fmt(y1)))
    spin(app, 2.0)
    y2 = yrange(pv)
    span1, span2 = y1[1] - y1[0], y2[1] - y2[0]
    ok1 = abs(span2 - span1) < max(1e-9, span1 * 0.02)
    print("     等 2 秒刷新后: %s  %s" % (fmt(y2), "[OK] 保持住了" if ok1 else "[X] 被打回"))

    # --- 2) 缩放温度图的 Y，看电压图是否受影响 ---
    #     先让电压/电流图都处于"已被用户缩放"状态（autoRange 关闭），
    #     这样任何变化都只能来自真正的联动，而不是它们自己的 autoRange 跟随数据。
    if pt is not None:
        pv.getViewBox().scaleBy((1.0, 0.8))
        pi.getViewBox().scaleBy((1.0, 0.8))
        app.processEvents()
        spin(app, 0.6)

        yv_before = yrange(pv)
        yi_before = yrange(pi)
        pt.getViewBox().scaleBy((1.0, 0.3))
        app.processEvents()
        spin(app, 2.0)
        yv_after = yrange(pv)
        yi_after = yrange(pi)
        yt_after = yrange(pt)

        sv_b = yv_before[1] - yv_before[0]
        sv_a = yv_after[1] - yv_after[0]
        si_b = yi_before[1] - yi_before[0]
        si_a = yi_after[1] - yi_after[0]
        dv = abs(sv_a - sv_b) / max(1e-12, abs(sv_b))
        di = abs(si_a - si_b) / max(1e-12, abs(si_b))
        # 相对变化 < 0.1% 视为完全独立
        ok2 = dv < 1e-3 and di < 1e-3
        print("  ② 缩放温度图 Y 后:")
        print("     电压图 Y 跨度: %.6g -> %.6g  (相对变化 %.3g%%)"
              % (sv_b, sv_a, dv * 100))
        print("     电流图 Y 跨度: %.6g -> %.6g  (相对变化 %.3g%%)"
              % (si_b, si_a, di * 100))
        print("     温度图 Y = %s" % fmt(yt_after))
        print("     %s" % ("[OK] 电信号图完全没受影响" if ok2 else "[X] 被影响了"))
    else:
        ok2 = True
        print("  ② 没有温度图，跳过")

    # --- 3) X 轴联动是否还在 ---
    xv = win.plots.p_v.getViewBox().viewRange()[0]
    xi = win.plots.p_i.getViewBox().viewRange()[0]
    xt = win.plots.p_t.getViewBox().viewRange()[0] if pt else xv
    ok3 = (abs(xv[0] - xi[0]) < 0.5 and abs(xv[0] - xt[0]) < 0.5)
    print("  ③ X 轴联动: 电压 %.1f / 电流 %.1f / 温度 %.1f  %s"
          % (xv[0], xi[0], xt[0], "[OK] 仍联动" if ok3 else "[X] 不同步"))

    win.close()
    print()
    print("总结: Y保持=%s  电信号不受影响=%s  X联动=%s"
          % (ok1, ok2, ok3))
    return 0 if (ok1 and ok2 and ok3) else 1


if __name__ == "__main__":
    sys.exit(main())
