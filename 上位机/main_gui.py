#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
4 路 IV 测量 · 图形界面（第 2 期）
================================================================================
用法：
    python main_gui.py                      # 正常启动（读 config.yaml）
    python main_gui.py --config xxx.yaml
    python main_gui.py --storage-dir D:/IV_data
    python main_gui.py --fake                # 启动即进入模拟模式（不接硬件）
    python main_gui.py --screenshot out.png  # 无头模式截图（用于自检/预览）

界面功能：
    · 上=电压图、下=电流图，**共享统一时间轴**（X 轴联动缩放/平移）
    · 视图范围四档：10 分钟 / 1 小时 / 1 天 / 7 天（后两档走聚合表，秒出）
    · 跟随最新（自动滚动）；手动缩放会自动取消跟随
    · 通道显示开关（空通道的噪声曲线点一下隐藏，不影响采集与存储）
    · 采样间隔可改 + 间隔保护实时提示
    · 状态栏：轮数 / 周期 / 余量 / 点数 / 时长 / 数据来源
    · 日志区：连接、探测、事件、错误
    · 关闭窗口时先让采集线程优雅收尾（Arduino 回安全态、校验 CSV）
================================================================================
"""

import argparse
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


def main():
    ap = argparse.ArgumentParser(description="4 路 IV 测量 · 图形界面")
    ap.add_argument("--config", default=os.path.join(HERE, "config.yaml"))
    ap.add_argument("--storage-dir", default=None, help="覆盖存储根目录")
    ap.add_argument("--interval", type=float, default=None, help="覆盖采样间隔（秒）")
    ap.add_argument("--fake", action="store_true", help="启动即进入模拟模式")
    ap.add_argument("--screenshot", default=None,
                    help="无头模式：渲染界面并截图到该文件后退出（自检用）")
    ap.add_argument("--shot-seconds", type=float, default=3.0,
                    help="截图前等待秒数（让曲线有数据）")
    args = ap.parse_args()

    from ivcore.config import Config

    cfg = Config(args.config)
    if args.storage_dir:
        cfg.raw["storage"]["dir"] = os.path.abspath(args.storage_dir)
    if args.interval:
        cfg.raw["acquisition"]["interval_s"] = args.interval

    from PySide6.QtWidgets import QApplication
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("4CH-IV Monitor")

    win = MainWindow(cfg)

    if args.fake:
        win.chk_fake.setChecked(True)

    if args.screenshot:
        # ---- 无头自检模式：启动采集 → 等一会儿 → 截图 → 退出 ----
        # 默认连真实硬件；加 --fake 才用假数据
        if args.fake:
            win.chk_fake.setChecked(True)
        win.show()
        win._start()

        shot_done = {"v": False}

        def take_shot():
            path = os.path.abspath(args.screenshot)
            pix = win.grab()
            pix.save(path)
            print("已保存截图: %s  (%dx%d)" % (path, pix.width(), pix.height()))
            shot_done["v"] = True
            app.quit()

        from PySide6.QtCore import QTimer
        QTimer.singleShot(int(args.shot_seconds * 1000), take_shot)
        rc = app.exec()
        print("截图模式退出，返回码 %s" % rc)
        return rc

    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
