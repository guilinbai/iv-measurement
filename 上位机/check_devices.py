#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
设备自检 —— 4 路 IV 测量系统
================================================================================
作用：一条命令查清所有硬件，并告诉你该怎么填 config.yaml

  ① 列出所有 VISA 资源，逐台读 *IDN? -> 找出两台 DMM6500（并读出当前功能/量程）
  ② 列出所有串口，逐个"试探"（发 PING）-> 找出哪一个是 Arduino
  ③ 实测 :READ? 耗时 -> 推算一轮耗时，给出推荐的采样间隔
  ④ 打印可直接粘贴的 config.yaml 片段

用法：
    python check_devices.py
    python check_devices.py --idn      # 只查表
    python check_devices.py --serial   # 只查串口
================================================================================
"""

import sys
import time

# Windows 控制台默认可能是 GBK；强制 UTF-8 并允许替换，避免输出报错
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

SEP = "=" * 78


def head(title):
    print()
    print(SEP)
    print("  " + title)
    print(SEP)


# ----------------------------------------------------------------------------
# ① VISA：找两台 DMM6500
# ----------------------------------------------------------------------------
def check_dmm(do_timing=True):
    head("① 万用表（VISA / USB）")
    try:
        import pyvisa
    except ImportError:
        print("  [X] 未安装 pyvisa。请运行:  pip install pyvisa")
        return []

    try:
        rm = pyvisa.ResourceManager()
        print("  VISA 后端 :", rm.visalib)
        resources = list(rm.list_resources())
    except Exception as e:
        print("  [X] 打开 VISA 失败:", type(e).__name__, str(e)[:160])
        print("    提示：需安装 NI-VISA（或 Keysight IO Libraries）")
        return []

    usb = [r for r in resources if r.startswith("USB")]
    print("  发现资源  : %d 个（USB 类 %d 个）" % (len(resources), len(usb)))
    for r in resources:
        print("      ", r)

    found = []
    for res in usb:
        try:
            inst = rm.open_resource(res)
            inst.timeout = 5000
            inst.write_termination = "\n"
            inst.read_termination = "\n"
            idn = inst.query("*IDN?").strip()
        except Exception as e:
            print("\n  [%s]\n    [X] 打开失败: %s" % (res, str(e)[:120]))
            continue

        parts = [p.strip() for p in idn.split(",")]
        model = parts[1] if len(parts) > 1 else "?"
        serial = parts[2] if len(parts) > 2 else "?"
        fw = parts[3] if len(parts) > 3 else "?"

        if "DMM6500" not in idn.upper() and "6500" not in model:
            print("\n  [%s]\n    IDN: %s  (不是 DMM6500，跳过)" % (res, idn))
            inst.close()
            continue

        print("\n  -- 找到 DMM6500 --------------------------------------------")
        print("    地址   : %s" % res)
        print("    序列号 : %s      固件: %s" % (serial, fw))

        info = {"resource": res, "serial": serial, "fw": fw}

        # 读当前测量功能与量程
        for label, q in [("当前功能", ":SENS:FUNC?"),
                         ("电压量程", ":SENS:VOLT:RANG?"),
                         ("电流量程", ":SENS:CURR:RANG?"),
                         ("NPLC",     ":SENS:VOLT:NPLC?"),
                         ("自动调零", ":SENS:VOLT:AZER?")]:
            try:
                info[label] = inst.query(q).strip()
                print("    %-8s: %s" % (label, info[label]))
            except Exception as e:
                info[label] = "?"
                print("    %-8s: <读取失败 %s>" % (label, str(e)[:40]))

        # 实测 :READ? 耗时
        if do_timing:
            ts = []
            vals = []
            for _ in range(5):
                t0 = time.perf_counter()
                v = inst.query(":READ?").strip()
                ts.append((time.perf_counter() - t0) * 1000)
                vals.append(v)
            info["read_ms"] = sum(ts) / len(ts)
            print("    :READ?   : 平均 %.1f ms（最小 %.1f / 最大 %.1f）"
                  % (info["read_ms"], min(ts), max(ts)))
            print("    读数样本 : %s" % ", ".join(vals[:3]))

        inst.close()
        found.append(info)

    if len(found) < 2:
        print("\n  [!] 只找到 %d 台 DMM6500。本方案需要两台（一台电压、一台电流）。"
              % len(found))
        if len(found) == 1:
            print("     请检查另一台是否已插 USB、是否被其它软件（KickStart 等）占用。")
    else:
        print("\n  [OK] 两台 DMM6500 就绪")
    return found


# ----------------------------------------------------------------------------
# ② 串口：试探哪个是 Arduino
# ----------------------------------------------------------------------------
def probe_arduino():
    head("② Arduino（串口试探）")
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        print("  [X] 未安装 pyserial。请运行:  pip install pyserial")
        return None

    ports = list(list_ports.comports())
    if not ports:
        print("  [X] 没有找到任何串口")
        return None

    print("  发现串口 %d 个：" % len(ports))
    for p in ports:
        vid = ("0x%04X" % p.vid) if p.vid else "-"
        pid = ("0x%04X" % p.pid) if p.pid else "-"
        print("     %-7s %-30s vid=%s pid=%s" % (p.device, (p.description or "")[:28], vid, pid))

    # ---- 只探测"USB 串口芯片"（CH340/Arduino/FTDI/CP210x）----
    # 蓝牙串口(COM5/COM6)、主板串口(COM1) 在 Windows 上打开会长时间阻塞，必须跳过
    KNOWN_USB_VID = {0x1A86, 0x2341, 0x2A03, 0x0403, 0x10C4, 0x0483, 0x1B4F, 0x239A}
    to_probe = [p for p in ports if p.vid in KNOWN_USB_VID]
    skipped = [p for p in ports if p.vid not in KNOWN_USB_VID]

    if skipped:
        print("\n  跳过 %d 个非 USB 串口（蓝牙/主板串口，打开会卡住）：%s"
              % (len(skipped), ", ".join(p.device for p in skipped)))
    if not to_probe:
        print("\n  [X] 没有找到 USB 串口芯片（CH340 等）。请检查 Arduino 是否插好、驱动是否安装。")
        return None

    print("\n  逐个试探 USB 串口（发 PING，看谁回应）...")
    candidates = []
    for p in to_probe:
        port = p.device
        t_start = time.time()
        try:
            ser = serial.Serial(port, 115200, timeout=0.4)
        except Exception as e:
            print("     %-7s 打不开（%s）" % (port, str(e)[:40]))
            continue

        try:
            time.sleep(0.3)              # 等 Arduino 复位（DTR）后启动
            ser.reset_input_buffer()
            ser.write(b"PING\n")
            ser.flush()
            time.sleep(0.4)
            resp = ser.read(200).decode("utf-8", "replace").strip()
        except Exception as e:
            resp = ""
        finally:
            ser.close()

        if resp:
            oneline = resp.replace("\r", " ").replace("\n", " | ")[:70]
            print("     %-7s <- 有回应 (%.1fs): %s" % (port, time.time() - t_start, oneline))
            candidates.append((port, resp))
        else:
            print("     %-7s    无回应 (%.1fs)" % (port, time.time() - t_start))

    if not candidates:
        print("\n  [X] 没有串口回应 PING。可能原因：")
        print("     - Arduino 上跑的是旧固件（v1.0 也会回应，但格式不同）——再试一次")
        print("     - Arduino 被 Arduino IDE 的串口监视器占用 -> 先关掉监视器")
        print("     - 数据线是只能充电的线（换一根确认能传数据的）")
        return None

    if len(candidates) > 1:
        print("\n  [!] 有多个串口回应了，请人工确认哪一个接的是开关阵列：")
        for port, resp in candidates:
            print("     %-7s -> %s" % (port, resp.strip()[:60]))
        return candidates[0][0]

    port, resp = candidates[0]
    tag = "v1.1（新固件）" if "READY v1.1" in resp else "旧固件（建议升级到 v1.1）"
    print("\n  [OK] Arduino 在 %s   [%s]" % (port, tag))
    if "READY v1.1" not in resp:
        print("     [!] 未检测到 v1.1 的 READY 横幅 —— 请上传《arduino测试\\12\\12.ino》")
    return port


# ----------------------------------------------------------------------------
# ③ 汇总
# ----------------------------------------------------------------------------
def summarize(dmms, port):
    head("③ 建议写入 config.yaml 的内容")
    volt = curr = None
    for d in dmms:
        func = d.get("当前功能", "")
        if "VOLT" in func.upper() and volt is None:
            volt = d
        elif "CURR" in func.upper() and curr is None:
            curr = d
    # 若按功能分不出来，就按序列号兜底分配
    rest = [d for d in dmms if d not in (volt, curr)]
    if volt is None and rest:
        volt = rest.pop(0)
    if curr is None and rest:
        curr = rest.pop(0)

    if volt:
        print("devices:")
        print("  dmm_voltage:")
        print("    resource: \"%s\"" % volt["resource"])
        print("    idn_serial: \"%s\"" % volt["serial"])
    else:
        print("# [!] 未识别到电压表")
    if curr:
        print("  dmm_current:")
        print("    resource: \"%s\"" % curr["resource"])
        print("    idn_serial: \"%s\"" % curr["serial"])
    else:
        print("# [!] 未识别到电流表")
    print("  arduino:")
    print("    port: \"%s\"          # 留空则自动探测" % (port or ""))

    if volt and curr and volt["serial"] == curr["serial"]:
        print("\n  [!] 警告：两台表读到的序列号相同，请确认接的是两台不同的表")

    # 一轮耗时推算
    reads = [d.get("read_ms", 30) for d in dmms if "read_ms" in d]
    read_ms = max(reads) if reads else 25
    head("④ 一轮耗时与间隔建议")
    for tw in (30, 50, 80):
        one = 30 + 80 + tw + 20 + 10
        rnd = 8 * one + 40
        print("  T_WINDOW=%-3d ms（读数实测 %.0f ms）-> 一轮 %.2f s，间隔 2 s 余量 %.2f s%s"
              % (tw, read_ms, rnd / 1000, 2.0 - rnd / 1000,
                 "  ← 推荐" if tw == 50 else ""))
    print("\n  结论：读数只需 %.0f ms，T_WINDOW 取 50 ms 有 2 倍余量；" % read_ms)
    print("        一轮约 1.56 s -> 采样间隔 2 s 可行（固件里 T_WINDOW 已设为 50）")


def main():
    args = set(sys.argv[1:])
    only_idn = "--idn" in args
    only_ser = "--serial" in args

    print(SEP)
    print("  4 路 IV 测量系统 - 设备自检")
    print(SEP)

    dmms, port = [], None
    if not only_ser:
        dmms = check_dmm()
    if not only_idn:
        port = probe_arduino()
    if not only_idn and not only_ser:
        summarize(dmms, port)

    print()
    print(SEP)
    print("  自检结束")
    print(SEP)


if __name__ == "__main__":
    main()
