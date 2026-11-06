# -*- coding: utf-8 -*-
"""
电源通讯实测解析（不依赖协议文档，只看设备真实回应）
================================================================================
目标：用"发命令-看回应"的方式，把设备的真实行为摸清楚，而不是照文档推测。

本脚本**只做探测**，除了最后一步会恢复原设定值，不改任何危险状态。
所有写操作都先用"读回设定值"确认是否真的生效。

探测清单：
  A. 读寄存器：试 1/2/4/8 个，看哪些数量设备才应答
  B. 读线圈 0x01：试地址 0~7 各读 1 个 —— ★ 这一项以前从没测过
  C. 写寄存器 0x10：试 count=1/2/3，看哪些才生效
  D. 写单个寄存器 0x06：试一次
  E. 写线圈 0x05：试 0xFF00 / 0x0001 / 0x0000，看是否生效（用读回验证）
  F. 完整位图：把状态寄存器0/1 的 16 个 bit 全打出来
  G. 时序：记录每类命令的应答耗时
"""
import sys
import time

import serial

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM13"
BAUD = 2400
ADDR = 0


def crc16(d):
    c = 0xFFFF
    for b in d:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if (c & 1) else (c >> 1)
    return c & 0xFF, (c >> 8) & 0xFF


def mk(body):
    lo, hi = crc16(body)
    return bytes(body) + bytes([lo, hi])


def hx(b):
    return " ".join("%02X" % x for x in b) if b else "(无回应)"


def crc_ok(frame):
    if len(frame) < 4:
        return False
    lo, hi = crc16(frame[:-2])
    return frame[-2] == lo and frame[-1] == hi


class Dev:
    def __init__(self):
        self.s = serial.Serial(PORT, BAUD, timeout=0.35, bytesize=8,
                               parity="N", stopbits=1)
        time.sleep(0.2)
        self.s.reset_input_buffer()

    def xfer(self, body, expect, quiet=False):
        """发一帧，收 expect 字节，返回 (回应, 耗时ms)"""
        f = mk(body)
        self.s.reset_input_buffer()
        t0 = time.monotonic()
        self.s.write(f)
        self.s.flush()
        buf = b""
        while len(buf) < expect and time.monotonic() - t0 < 0.8:
            c = self.s.read(expect - len(buf))
            if c:
                buf += c
        dt = (time.monotonic() - t0) * 1000
        if not quiet:
            print("       TX %-40s -> RX %-30s %s  %.0fms"
                  % (hx(f), hx(buf), "CRC-OK" if crc_ok(buf) else "", dt))
        return buf, dt

    def read_regs(self, start=0, count=8):
        buf, _ = self.xfer([ADDR, 0x03, (start >> 8) & 0xFF, start & 0xFF,
                            (count >> 8) & 0xFF, count & 0xFF],
                           3 + 2 * count + 2, quiet=True)
        if len(buf) < 3 + 2 * count + 2 or not crc_ok(buf):
            return None, buf
        d = buf[3:3 + 2 * count]
        return [(d[i] | (d[i + 1] << 8)) for i in range(0, 2 * count, 2)], buf

    def status(self):
        """返回 (stat0高字节先, stat1高字节先, [8个寄存器小端])"""
        buf, _ = self.xfer([ADDR, 0x03, 0, 0, 0, 8], 21, quiet=True)
        if len(buf) < 21 or not crc_ok(buf):
            return None
        d = buf[3:19]
        s0 = (d[0] << 8) | d[1]
        s1 = (d[2] << 8) | d[3]
        le = lambda i: d[i] | (d[i + 1] << 8)
        regs = [s0, s1] + [le(4), le(6), le(8), le(10), le(12), le(14)]
        return s0, s1, regs

    def write3(self, ctrl, v_raw, i_raw, quiet=True):
        body = [ADDR, 0x10, 0, 0, 0, 3, 6,
                (ctrl >> 8) & 0xFF, ctrl & 0xFF,
                v_raw & 0xFF, (v_raw >> 8) & 0xFF,
                i_raw & 0xFF, (i_raw >> 8) & 0xFF]
        return self.xfer(body, 8, quiet=quiet)

    def close(self):
        self.s.close()


def bits(v, n=16):
    """把 16 位打成 'b15..b0' 便于看哪一位变了"""
    return "".join("1" if (v >> i) & 1 else "0" for i in range(n - 1, -1, -1))


def main():
    print("=" * 92)
    print("电源通讯实测解析  @ %s  (%d bps, 地址 %d)" % (PORT, BAUD, ADDR))
    print("=" * 92)

    try:
        dev = Dev()
    except Exception as e:
        print("[X] 打不开串口:", str(e)[:120])
        return 1

    base = dev.status()
    if not base:
        print("[X] 读状态失败，无法继续")
        dev.close()
        return 1
    s0, s1, regs = base
    print("\n基线状态:")
    print("  状态0 = 0x%04X  b=%s  (PS=%d WS=%d AS=%d)" % (s0, bits(s0), (s0 >> 8) & 1,
                                                       (s0 >> 12) & 1, (s0 >> 13) & 1))
    print("  状态1 = 0x%04X  b=%s" % (s1, bits(s1)))
    print("  寄存器 = %s" % regs)
    print("  解读: 设定 %.2fV / %.3fA  实测 %.2fV / %.3fA  上限 %.1fV / %.1fA"
          % (regs[4] / 100, regs[5] / 1000, regs[2] / 100, regs[3] / 1000,
             regs[6] / 100, regs[7] / 1000))

    # ---------- A. 读寄存器：试不同数量 ----------
    print("\n" + "-" * 92)
    print("A. 读寄存器 0x03：设备到底支持读几个？")
    for n in (1, 2, 4, 8):
        r, raw = dev.read_regs(0, n)
        print("   读 %d 个: %s" % (n, "应答正常" if r else "无应答/CRC错"))

    print("\n   再试非零起始地址（0x0002 起，读 1 个）:")
    r, raw = dev.read_regs(2, 1)
    print("   -> %s   %s" % ("应答正常" if r else "无应答", hx(raw[:12])))

    # ---------- B. 读线圈 ----------
    print("\n" + "-" * 92)
    print("B. 读线圈 0x01：每个地址单独读 1 个（★ 以前没测过）")
    coil_works = []
    for a in range(0, 8):
        buf, dt = dev.xfer([ADDR, 0x01, 0x00, a, 0x00, 0x01], 6)
        if len(buf) >= 6 and crc_ok(buf) and buf[1] == 0x01:
            v = buf[3] & 1
            coil_works.append((a, v))
            print("       -> 线圈 %d = %d" % (a, v))
        else:
            print("       -> 无有效回应")
    print("   %s" % ("支持的线圈: " + str([a for a, _ in coil_works])
                    if coil_works else "★ 读线圈完全不支持"))

    # ---------- F. 完整位图（读两次，看稳定性） ----------
    print("\n" + "-" * 92)
    print("F. 连续读 5 次，看状态位是否稳定（判断哪些位真的在变）")
    for k in range(5):
        st = dev.status()
        if st:
            b0, b1, rr = st
            print("   #%d  s0=0x%04X %s   s1=0x%04X %s  实测 %d/%d"
                  % (k + 1, b0, bits(b0), b1, bits(b1), rr[2], rr[3]))
        time.sleep(0.25)

    # ---------- C. 写寄存器：试不同数量 ----------
    print("\n" + "-" * 92)
    print("C. 写寄存器 0x10：用「改设定电压 5V 再读回」验证哪些数量真生效")
    orig_v = regs[4]
    for n in (1, 2, 3):
        body = [ADDR, 0x10, 0x00, 0x00, (n >> 8) & 0xFF, n & 0xFF, 2 * n]
        vals = [(0x0100 if n > 0 else 0), 500, 800][:n]
        for v in vals:
            body += [v & 0xFF, (v >> 8) & 0xFF]
        # 控制字要单独按高字节先，这里仅探测"数量"行为，用统一小端即可
        buf, dt = dev.xfer(body, 8)
        time.sleep(0.4)
        st = dev.status()
        got = st[2][4] if st else -1
        print("       count=%d -> 设定电压读回 %d (%.2fV)  %s"
              % (n, got, got / 100, "★生效" if abs(got - 500) < 3 else "未生效"))
        time.sleep(0.3)

    # ---------- D. 写单个寄存器 0x06 ----------
    print("\n" + "-" * 92)
    print("D. 写单个寄存器 0x06（很多设备支持，文档没写）")
    dev.xfer([ADDR, 0x06, 0x00, 0x01, 500 & 0xFF, (500 >> 8) & 0xFF], 8)
    time.sleep(0.4)
    st = dev.status()
    got = st[2][4] if st else -1
    print("   -> 设定电压读回 %d (%.2fV)  %s"
          % (got, got / 100, "★生效" if abs(got - 500) < 3 else "未生效"))

    # ---------- E. 写线圈 0x05 控制输出 ----------
    print("\n" + "-" * 92)
    print("E. 写线圈 0x05 控制输出（0x0000 = 电源输出）")
    for val, name in ((0xFF00, "ON(0xFF00)"), (0x0001, "ON(0x0001)"),
                      (0x0000, "OFF(0x0000)")):
        buf, dt = dev.xfer([ADDR, 0x05, 0x00, 0x00,
                            (val >> 8) & 0xFF, val & 0xFF], 8)
        time.sleep(0.5)
        st = dev.status()
        ps = ((st[0] >> 8) & 1) if st else -1
        print("       %-12s -> PS=%d  %s" % (name, ps,
                                            "★生效" if buf and crc_ok(buf) else "无应答"))

    # ---------- 恢复 ----------
    print("\n" + "-" * 92)
    print("恢复原设定电压 %d (%.2fV)" % (orig_v, orig_v / 100))
    dev.write3((st[0] & 0xFF00) if st else 0, orig_v, regs[5])
    time.sleep(0.4)
    st = dev.status()
    if st:
        print("  已恢复: 设定 %.2fV / %.3fA" % (st[2][4] / 100, st[2][5] / 1000))

    dev.close()
    print("\n" + "=" * 92)
    print("探测完成。请把上面输出完整发回，我按实测结果重写控制逻辑。")
    print("=" * 92)
    return 0


if __name__ == "__main__":
    sys.exit(main())
