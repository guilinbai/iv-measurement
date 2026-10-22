# -*- coding: utf-8 -*-
"""无头验证：滚轮缩放横坐标后，温度曲线是否还在。"""
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


def npts(win, ch, mode):
    c = win.plots.curves.get((ch, mode))
    if c is None or c.xData is None:
        return 0
    return len(c.xData)


def main():
    app = QApplication(sys.argv)
    win = MainWindow(Config(os.path.join(HERE, "config.yaml")))
    win.show()
    win.chk_fake.setChecked(True)
    win._start()
    spin(app, 6.0)

    print("=" * 74)
    print("滚轮缩放 X 后温度曲线是否保留")
    print("=" * 74)

    def report(tag):
        v1 = npts(win, 1, "V")
        i1 = npts(win, 1, "I")
        t1 = npts(win, 1, "T")
        t2 = npts(win, 2, "T")
        print("  %-14s V1=%-4d I1=%-4d T1=%-4d T2=%-4d  realtime=%s follow=%s"
              % (tag, v1, i1, t1, t2,
                 win.span_s <= 7200, win.follow))
        return t1, t2

    t1a, t2a = report("缩放前")

    # 三个方向都试：放大、缩小、平移
    tests = [
        ("放大(X缩到1/4)", lambda vb: vb.scaleBy((0.25, 1.0))),
        ("缩小(X放大4倍)", lambda vb: vb.scaleBy((4.0, 1.0))),
        ("平移", lambda vb: vb.translateBy(x=-0.5)),
    ]
    ok_all = True
    for name, act in tests:
        act(win.plots.p_v.getViewBox())
        app.processEvents()
        spin(app, 1.5)                     # 让刷新定时器跑几轮
        t1, t2 = report(name)
        if t1 == 0 or t2 == 0:
            ok_all = False
            print("      [X] 温度曲线没了！")
        else:
            print("      [OK] 温度仍在")

    # 再验证更长跨度（>2 小时走数据库）
    print()
    print("  --- 切到 3 小时视图（走数据库查询）---")
    win.spin_span.setValue(3.0)
    win._on_custom_span()
    spin(app, 2.0)
    t1, t2 = report("3 小时视图")
    db_ok = (t1 > 0 or t2 > 0)
    print("      %s" % ("[OK] 数据库也能取到温度" if db_ok
                       else "[!] 数据库里可能没有温度数据（今天的库才有）"))

    win.close()
    print()
    print("总结:", "[OK] 缩放不再丢温度曲线" if ok_all else "[X] 仍有问题")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
