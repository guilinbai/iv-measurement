# -*- coding: utf-8 -*-
"""
电源控制回归测试：验证"各操作互不干扰"

这是针对之前那个根本性 bug 的测试 —— 协议要求一次写 3 个寄存器，
如果拿软件缓存去填另外两项，用户在面板上的改动就会被覆盖。

测试项：
  ① 设备电压被"面板"改成 15V 后，软件点"输出开" → 电压必须保持 15V
  ② 设备输出被"面板"关掉后，软件"设电压" → 输出必须保持关闭
  ③ 设电压不能改动限流
  ④ 设限流不能改动电压
  ⑤ 空载开输出不能抛异常（此时输出已成功打开）

"面板操作"用原始帧直接改设备来模拟。
"""
import sys
import time

import serial

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, ".")

from ivcore.powersupply import (PowerSupply, PowerSupplyError, crc16,  # noqa: E402
                                _with_crc, _check_crc)


def raw_set(s, port_baud=None, ctrl=0x0100, volts=8.0, amps=0.5):
    """用原始帧直接写设备（模拟"在面板上改"），绕过 PowerSupply 的缓存。

    s 直接传 ps.ser（同一个串口不能同时开两次）。
    """
    body = [0, 0x10, 0x00, 0x00, 0x00, 0x03, 0x06,
            (ctrl >> 8) & 0xFF, ctrl & 0xFF,
            int(round(volts * 100)) & 0xFF, (int(round(volts * 100)) >> 8) & 0xFF,
            int(round(amps * 1000)) & 0xFF, (int(round(amps * 1000)) >> 8) & 0xFF]
    s.reset_input_buffer()
    s.write(_with_crc(body))
    s.flush()
    buf = b""
    t0 = time.monotonic()
    while len(buf) < 8 and time.monotonic() - t0 < 1.0:
        c = s.read(8 - len(buf))
        if c:
            buf += c
    return len(buf) >= 8 and _check_crc(buf)


def main(port="COM13"):
    print("=" * 78)
    print("电源控制回归测试 @ %s" % port)
    print("=" * 78)

    ps = PowerSupply(port=port, baud=2400, addr=0, settle_s=0.5)
    print("连接:", ps.open())
    ps.read_status()
    i = ps.info
    orig = (i["vset"], i["iset"], i["out_on"])
    print("初始: %.2fV / %.0fmA  输出=%s  %s"
          % (i["vset"], i["iset"] * 1000, "开" if i["out_on"] else "关",
             "恒压" if i["cv"] else "恒流"))

    # 复用已有连接发原始帧（同一串口不能同时开两次）
    raw = ps.ser

    results = []

    def check(name, cond, detail):
        results.append((name, cond, detail))
        print("  %-46s %s   %s" % (name, "[OK]" if cond else "[X]", detail))

    # ---------- ① 面板改电压 → 软件开输出，电压不能被改回 ----------
    print("\n① 模拟面板把电压改成 15V，然后软件只点『输出开』")
    raw_set(raw, ctrl=0x0100, volts=15.0, amps=0.5)
    time.sleep(0.4)
    ps.read_status()
    print("     面板改后设备: 设定 %.2fV / %.0fmA" % (ps.info["vset"], ps.info["iset"] * 1000))
    if not ps.info["out_on"]:
        ps.output(True)
    time.sleep(0.5)
    ps.read_status()
    check("① 电压保持 15V（没被软件缓存覆盖）",
          abs(ps.info["vset"] - 15.0) < 0.05,
          "实际 %.2fV" % ps.info["vset"])

    # ---------- ② 面板关输出 → 软件设电压，输出必须保持关 ----------
    print("\n② 模拟面板关掉输出，然后软件只『设电压 8V』")
    raw_set(raw, ctrl=0x0000, volts=15.0, amps=0.5)
    time.sleep(0.4)
    ps.read_status()
    print("     面板关后设备: 输出=%s" % ("开" if ps.info["out_on"] else "关"))
    ps.set_voltage(8.0, settle=False)
    time.sleep(0.4)
    ps.read_status()
    check("② 输出保持关闭（没被意外打开）",
          ps.info["out_on"] is False,
          "输出=%s" % ("开" if ps.info["out_on"] else "关"))
    check("② 电压确实改成了 8V",
          abs(ps.info["vset"] - 8.0) < 0.05,
          "实际 %.2fV" % ps.info["vset"])

    # ---------- ③ 设电压不改限流 ----------
    print("\n③ 面板设限流 0.300A，软件设电压 12V，限流必须还是 0.300A")
    raw_set(raw, ctrl=0x0000, volts=8.0, amps=0.3)
    time.sleep(0.4)
    ps.set_voltage(12.0, settle=False)
    time.sleep(0.4)
    ps.read_status()
    check("③ 限流保持 0.300A",
          abs(ps.info["iset"] - 0.3) < 0.005,
          "实际 %.3fA" % ps.info["iset"])
    check("③ 电压改成了 12V",
          abs(ps.info["vset"] - 12.0) < 0.05,
          "实际 %.2fV" % ps.info["vset"])

    # ---------- ④ 设限流不改电压 ----------
    print("\n④ 软件设限流 0.800A，电压必须还是 12V")
    ps.set_current(0.8)
    time.sleep(0.4)
    ps.read_status()
    check("④ 电压保持 12V",
          abs(ps.info["vset"] - 12.0) < 0.05,
          "实际 %.2fV" % ps.info["vset"])
    check("④ 限流改成了 0.800A",
          abs(ps.info["iset"] - 0.8) < 0.005,
          "实际 %.3fA" % ps.info["iset"])

    # ---------- ⑤ 空载开输出不抛异常 ----------
    print("\n⑤ 关输出，设电压 0.0V（近似空载），再开输出 —— 不应抛异常")
    ps.output(False)
    ps.set_voltage(0.0, settle=False)
    time.sleep(0.3)
    try:
        ps.output(True)
        check("⑤ 空载开输出没有抛异常", True, "返回正常")
    except PowerSupplyError as e:
        check("⑤ 空载开输出没有抛异常", False, str(e)[:60])
    ps.read_status()
    check("⑤ 输出确实处于开启", ps.info["out_on"] is True,
          "输出=%s" % ("开" if ps.info["out_on"] else "关"))

    # ---------- 恢复原状态 ----------
    print("\n恢复原设定 %.2fV / %.0fmA / 输出%s"
          % (orig[0], orig[1] * 1000, "开" if orig[2] else "关"))
    try:
        ps.set_voltage(orig[0], settle=False)
        ps.set_current(orig[1])
        ps.output(orig[2])
    except Exception as e:
        print("  恢复时出错:", str(e)[:80])
    ps.read_status()
    print("  已恢复: %.2fV / %.0fmA  输出=%s"
          % (ps.info["vset"], ps.info["iset"] * 1000,
             "开" if ps.info["out_on"] else "关"))

    ps.close(off_output=False)

    print("\n" + "=" * 78)
    bad = [n for n, ok, _ in results if not ok]
    if bad:
        print("失败 %d 项:" % len(bad))
        for n in bad:
            print("   - " + n)
    else:
        print("全部 %d 项通过 ✓" % len(results))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "COM13"))
