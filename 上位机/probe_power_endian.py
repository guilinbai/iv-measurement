# -*- coding: utf-8 -*-
"""
决定性实验：控制寄存器到底该按"高字节先"还是"小端"写？

做法：两种字节序各发一次，看哪一种能让状态寄存器0 的输出位(bit8)变 1。
      为了安全，先在"关输出"状态下测，测完恢复关闭。

只用 0x10 count=3（实测唯一被支持的写命令），电压电流保持设备当前值不动。
"""
import sys
import time

import serial

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM13"


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


def crc_ok(f):
    if len(f) < 4:
        return False
    lo, hi = crc16(f[:-2])
    return f[-2] == lo and f[-1] == hi


def bits16(v):
    return "".join("1" if (v >> i) & 1 else "0" for i in range(15, -1, -1))


s = serial.Serial(PORT, 2400, timeout=0.4, bytesize=8, parity="N", stopbits=1)
time.sleep(0.2)
s.reset_input_buffer()


def read_raw():
    s.reset_input_buffer()
    s.write(mk([0, 0x03, 0, 0, 0, 8]))
    s.flush()
    b = b""
    t0 = time.monotonic()
    while len(b) < 21 and time.monotonic() - t0 < 0.8:
        c = s.read(21 - len(b))
        if c:
            b += c
    if len(b) < 21 or not crc_ok(b):
        return None, b
    return b[3:19], b


def parse_be(d):
    """状态0/1 高字节先，其余小端"""
    if d is None:
        return None
    s0 = (d[0] << 8) | d[1]
    s1 = (d[2] << 8) | d[3]
    le = lambda i: d[i] | (d[i + 1] << 8)
    return s0, s1, le(4), le(6), le(8), le(10)


def write3(ctrl, ctrl_bigendian, v_raw, i_raw):
    if ctrl_bigendian:
        cb = [(ctrl >> 8) & 0xFF, ctrl & 0xFF]
    else:
        cb = [ctrl & 0xFF, (ctrl >> 8) & 0xFF]
    body = [0, 0x10, 0x00, 0x00, 0x00, 0x03, 0x06,
            cb[0], cb[1],
            v_raw & 0xFF, (v_raw >> 8) & 0xFF,
            i_raw & 0xFF, (i_raw >> 8) & 0xFF]
    f = mk(body)
    s.reset_input_buffer()
    s.write(f)
    s.flush()
    b = b""
    t0 = time.monotonic()
    while len(b) < 8 and time.monotonic() - t0 < 0.8:
        c = s.read(8 - len(b))
        if c:
            b += c
    return f, b


print("=" * 88)
print("决定性实验：控制字字节序  @ %s" % PORT)
print("=" * 88)

d, raw = read_raw()
if d is None:
    print("[X] 读失败")
    s.close()
    raise SystemExit(1)
st = parse_be(d)
print("\n初始状态:")
print("  原始16字节: %s" % hx(d))
print("  状态0 = 0x%04X  b=%s  PS(bit8)=%d" % (st[0], bits16(st[0]), (st[0] >> 8) & 1))
print("  设定 %.2fV / %.0fmA" % (st[4] / 100, st[5] / 1000))
v_raw, i_raw = st[4], st[5]

# 先确保输出是关的（用高字节先写 0x0000 = 全关）
write3(0x0000, True, v_raw, i_raw)
time.sleep(0.4)
d, _ = read_raw()
st = parse_be(d)
print("\n[准备] 已写控制字 0（输出关），当前 PS=%d" % ((st[0] >> 8) & 1))


def trial(name, ctrl, bigendian):
    """发一次控制字，看 PS 位和实测电压"""
    f, resp = write3(ctrl, bigendian, v_raw, i_raw)
    time.sleep(0.8)
    d, _ = read_raw()
    st = parse_be(d)
    ps = (st[0] >> 8) & 1
    print("\n  %s" % name)
    print("     TX      : %s" % hx(f))
    print("     RX      : %s  %s" % (hx(resp), "CRC-OK" if crc_ok(resp) else ""))
    print("     状态0   : 0x%04X  b=%s" % (st[0], bits16(st[0])))
    print("     PS(bit8): %d   %s" % (ps, "★ 输出打开了！" if ps else "输出仍关"))
    print("     实测    : %.2fV / %.3fA" % (st[2] / 100, st[3] / 1000))
    return ps


print("\n" + "-" * 88)
print("实验 1：控制字 0x0100 按【高字节先】发 -> 字节 01 00")
ps1 = trial("高字节先", 0x0100, True)

# 先关回去
write3(0x0000, True, v_raw, i_raw)
time.sleep(0.5)

print("\n" + "-" * 88)
print("实验 2：控制字 0x0100 按【小端】发 -> 字节 00 01")
ps2 = trial("小端", 0x0100, False)

# 收尾：关输出，恢复原设定
write3(0x0000, True, v_raw, i_raw)
time.sleep(0.5)
d, _ = read_raw()
st = parse_be(d)
print("\n" + "=" * 88)
print("收尾：已关输出，设定 %.2fV / %.0fmA，PS=%d"
      % (st[4] / 100, st[5] / 1000, (st[0] >> 8) & 1))
print()
print("结论：")
if ps1 and not ps2:
    print("  ★ 控制字必须【高字节先】（01 00）—— 与状态寄存器的处理方式一致")
elif ps2 and not ps1:
    print("  ★ 控制字必须【小端】（00 01）—— 与电压电流一致")
elif ps1 and ps2:
    print("  ⚠ 两种都能开输出 —— 说明设备对控制字的字节序不敏感（那问题不在端序）")
else:
    print("  ⚠ 两种都开不了输出 —— 说明输出使能确实不受串口控制，")
    print("     或者需要额外的前置条件（例如面板必须先按 OUTPUT 键）")
print("=" * 88)

s.close()
