# -*- coding: utf-8 -*-
"""4 路 IV 测量系统 · 上位机采集核心

模块划分：
    config    配置加载
    devices   硬件封装（Arduino 串口 + 两台 DMM6500）
    storage   数据存储（SQLite 主存 + CSV 实时副本 + 聚合表）
    scheduler 采集调度（发命令、收窗口、读表、间隔保护）
"""

__version__ = "0.1.0"
