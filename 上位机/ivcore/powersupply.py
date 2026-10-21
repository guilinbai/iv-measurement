# -*- coding: utf-8 -*-
"""
直流电压源控制（Wanptek，MODBUS-RTU）—— 用于驱动 ch1/ch2 的温差
================================================================================
协议来自《通讯协议4_1.pdf》，**已用真机验证**（波特率 2400、从站地址 0）：

  串口 8N1；波特率 2400/4800/9600/19200（默认 **2400**）；
  从站地址 0~31（默认 **0**）；数据小端；CRC16-MODBUS（多项式 0xA001，低字节先发）

  读寄存器 0x03（起始 0x0000，最多 8 个）
    0x0000 状态寄存器0   0x0001 状态寄存器1
    0x0002 当前电压      0x0003 当前电流
    0x0004 设置电压      0x0005 设置电流
    0x0006 最大电压      0x0007 最大电流
  写寄存器 0x10（起始 0x0000~0x0002）
    0x0000 控制寄存器（bit8=OUTCTRL 输出, bit9=OCP, bit10=LOCK）
    0x0001 设置电压      0x0002 设置电流
  写线圈   0x05（一次一个）
    0x0000 电源输出（0xFF00=开 / 0x0000=关）
    0x0001 OCP        0x0002 键盘锁定
  读线圈   0x01：0x0004 恒压恒流状态（0=CV,1=CC）/ 0x0005 报警状态

  数据换算（由状态寄存器的 VPOD/IPOD 决定小数点位置）：
    VPOD=0 → 电压 2 位小数（1000 = 10.00 V）   VPOD=1 → 1 位小数
    IPOD=0 → 电流 3 位小数（1000 = 1.000 A）   IPOD=1 → 2 位小数

实测本机电源：31 V / 5.2 A，VPOD=0、IPOD=0，初值 设置电压 10.690 V、设置电流 1.000 A
"""

import time


# ----------------------------------------------------------------------------
#  MODBUS CRC16
# ----------------------------------------------------------------------------
def crc16(data):
    """返回 (crcL, crcH)，可直接按此顺序追加到帧尾。"""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFF, (crc >> 8) & 0xFF


def _with_crc(body):
    lo, hi = crc16(body)
    return bytes(body) + bytes([lo, hi])


def _check_crc(frame):
    if len(frame) < 4:
        return False
    lo, hi = crc16(frame[:-2])
    return frame[-2] == lo and frame[-1] == hi


def _hex(b):
    return " ".join("%02X" % x for x in b)


class PowerSupplyError(Exception):
    pass


# ============================================================================
class PowerSupply:
    """Wanptek 电源（MODBUS-RTU）。

    protocol:
        "modbus" —— 真机控制（默认；本机已验证）
        "none"   —— dry-run，只记录不发送（用于先验证界面/数据流）
    """

    def __init__(self, port="COM13", baud=2400, addr=0, protocol="modbus",
                 settle_s=2.0, timeout=0.5, max_voltage=None,
                 off_on_exit=False, name="电压源"):
        self.port = port
        self.baud = int(baud)
        self.addr = int(addr)
        self.protocol = (protocol or "none").lower()
        self.settle_s = float(settle_s)
        self.timeout = float(timeout)
        self.off_on_exit = bool(off_on_exit)   # 退出程序时是否关输出
        self.name = name

        self.ser = None
        self.info = {}                  # 最近一次读到的状态
        self.max_voltage = max_voltage  # 若为 None 则从设备读
        self.set_v = None
        self.set_a = None
        self.out_on = None

    # ------------------------------------------------------------------
    def open(self, verbose=False):
        if self.protocol == "none":
            return "dry-run（不发送）"
        import serial
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout,
                                     bytesize=8, parity="N", stopbits=1)
        except Exception as e:
            raise PowerSupplyError("%s: 打不开 %s@%d (%s)"
                                   % (self.name, self.port, self.baud, e))
        time.sleep(0.15)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        ok = self.read_status(verbose=verbose)
        if not ok:
            raise PowerSupplyError(
                "%s: 读不到状态（%s @ %d bps, 地址 %d）。"
                "请检查电源是否开机、地址/波特率是否与面板设置一致"
                % (self.name, self.port, self.baud, self.addr))
        return "已连接 %s @%d bps 地址%d  %.3fV/%.3fA 上限%.1fV/%.1fA" % (
            self.port, self.baud, self.addr,
            self.info.get("volt", 0), self.info.get("curr", 0),
            self.info.get("vmax", 0), self.info.get("imax", 0))

    def close(self, off_output=None):
        """断开连接。

        off_output:
            None  -> 按 self.off_on_exit 决定（默认 False，即**保持输出**，
                     避免程序退出打断正在进行的控温实验）
            True/False -> 强制关/不关
        """
        do_off = self.off_on_exit if off_output is None else bool(off_output)
        if do_off:
            try:
                if self.out_on:
                    self.output(False)
            except Exception:
                pass
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    @property
    def is_open(self):
        return self.protocol == "none" or (self.ser is not None and self.ser.is_open)

    # ------------------------------------------------------------------
    #  底层收发
    # ------------------------------------------------------------------
    def _xfer(self, body, expect_len):
        """发一帧（自动加 CRC），读回 expect_len 字节并校验。返回 bytes 或 b""。"""
        if self.protocol == "none":
            return b""
        if not self.ser:
            raise PowerSupplyError("%s: 串口未打开" % self.name)
        frame = _with_crc(bytes([self.addr]) + bytes(body)) \
            if body[0] != self.addr else _with_crc(body)
        try:
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            self.ser.flush()
        except Exception as e:
            raise PowerSupplyError("%s: 发送失败 (%s)" % (self.name, e))

        buf = b""
        t0 = time.monotonic()
        while len(buf) < expect_len and time.monotonic() - t0 < max(1.0, self.timeout * 3):
            chunk = self.ser.read(expect_len - len(buf))
            if chunk:
                buf += chunk
        return buf

    # ------------------------------------------------------------------
    #  读
    # ------------------------------------------------------------------
    def read_status(self, verbose=False):
        """读 0x0000 起 8 个寄存器，解析成 info 字典。成功返回 True。"""
        if self.protocol == "none":
            return True
        body = [self.addr, 0x03, 0x00, 0x00, 0x00, 0x08]
        frame = _with_crc(body)
        try:
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            self.ser.flush()
        except Exception as e:
            raise PowerSupplyError("%s: 发送失败 (%s)" % (self.name, e))

        need = 3 + 16 + 2
        buf = b""
        t0 = time.monotonic()
        while len(buf) < need and time.monotonic() - t0 < max(1.0, self.timeout * 3):
            chunk = self.ser.read(need - len(buf))
            if chunk:
                buf += chunk
        if len(buf) < need or not _check_crc(buf):
            if verbose and buf:
                print("      %s: 回应异常 %s" % (self.name, _hex(buf)))
            return False

        bc = buf[2]
        data = buf[3:3 + bc]
        if len(data) < 16:
            return False
        regs = [(data[i] | (data[i + 1] << 8)) for i in range(0, 16, 2)]
        stat0, stat1 = regs[0], regs[1]
        vpod = (stat0 >> 4) & 1
        ipod = (stat1 >> 4) & 1
        vscale = 100.0 if vpod == 0 else 10.0
        iscale = 1000.0 if ipod == 0 else 100.0

        self.info = {
            "raw": regs,
            "stat0": stat0, "stat1": stat1,
            "v_scale": vscale, "i_scale": iscale,
            "out_on": bool((stat0 >> 8) & 1),
            "ocp": bool((stat0 >> 9) & 1),
            "lock": bool((stat0 >> 10) & 1),
            "cv": not bool((stat0 >> 12) & 1),
            "alarm": bool((stat0 >> 13) & 1),
            "volt": regs[2] / vscale,
            "curr": regs[3] / iscale,
            "vset": regs[4] / vscale,
            "iset": regs[5] / iscale,
            "vmax": regs[6] / vscale,
            "imax": regs[7] / iscale,
        }
        self.set_v = self.info["vset"]
        self.set_a = self.info["iset"]
        self.out_on = self.info["out_on"]
        if self.max_voltage is None:
            self.max_voltage = self.info["vmax"]
        return True

    def status_text(self):
        if self.protocol == "none":
            return "dry-run"
        i = self.info
        if not i:
            return "未知"
        return ("%.3fV/%.3fA  设定%.2fV/%.3fA  %s%s%s"
                % (i["volt"], i["curr"], i["vset"], i["iset"],
                   "输出开" if i["out_on"] else "输出关",
                   " 报警" if i["alarm"] else "",
                   " 恒流" if not i["cv"] else ""))

    # ------------------------------------------------------------------
    #  写
    # ------------------------------------------------------------------
    def _ctrl_word(self, out_on=None, ocp=None):
        """推断"控制寄存器"的字。

        状态寄存器0 的 PS(bit8) ↔ 控制寄存器 OUTCTRL(bit8)
                     OS(bit9) ↔ 控制寄存器 OCP(bit9)
        """
        i = self.info or {}
        out = i.get("out_on", True) if out_on is None else bool(out_on)
        o = i.get("ocp", False) if ocp is None else bool(ocp)
        return ((1 if out else 0) << 8) | ((1 if o else 0) << 9)

    def _write3(self, ctrl, v_raw, i_raw):
        """★ 本设备固件要求：**一次写满 3 个寄存器**（start=0x0000, count=3）。

        实测：写单个寄存器（count=1）设备**完全不回应也不生效**；
              一次写 3 个（0x0000 控制 / 0x0001 电压 / 0x0002 电流）立即成功。
        数据段为小端（低字节在前）。
        """
        if self.protocol == "none":
            return True
        if not self.ser:
            raise PowerSupplyError("%s: 串口未打开" % self.name)

        body = [self.addr, 0x10, 0x00, 0x00, 0x00, 0x03, 0x06,
                ctrl & 0xFF, (ctrl >> 8) & 0xFF,
                int(v_raw) & 0xFF, (int(v_raw) >> 8) & 0xFF,
                int(i_raw) & 0xFF, (int(i_raw) >> 8) & 0xFF]
        frame = _with_crc(body)
        try:
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            self.ser.flush()
        except Exception as e:
            raise PowerSupplyError("%s: 发送失败 (%s)" % (self.name, e))

        buf = b""
        t0 = time.monotonic()
        while len(buf) < 8 and time.monotonic() - t0 < max(1.0, self.timeout * 3):
            c = self.ser.read(8 - len(buf))
            if c:
                buf += c
        return len(buf) >= 8 and _check_crc(buf)

    def set_voltage(self, volts, settle=True):
        """设置输出电压（V）。超过设备最大电压会拒绝。"""
        v = float(volts)
        if v < 0:
            raise PowerSupplyError("%s: 电压不能为负（%g）" % (self.name, v))
        if not self.info:
            self.read_status()
        vmax = self.max_voltage or self.info.get("vmax") or 30.0
        if v > vmax + 1e-9:
            raise PowerSupplyError(
                "%s: 设定 %.3f V 超过设备最大 %.2f V" % (self.name, v, vmax))

        if self.protocol == "none":
            self.set_v = v
            return v

        vs = self.info.get("v_scale", 100.0)
        is_ = self.info.get("i_scale", 1000.0)
        v_raw = int(round(v * vs))
        i_raw = int(round((self.set_a if self.set_a is not None else 1.0) * is_))

        if not self._write3(self._ctrl_word(), v_raw, i_raw):
            raise PowerSupplyError("%s: 设置电压无响应（检查波特率/站地址）" % self.name)

        self.set_v = v
        if settle and self.settle_s > 0:
            time.sleep(self.settle_s)
        self.read_status()          # ★ 回读确认
        if abs(self.info.get("vset", -1) - v) > max(0.02, v * 0.002):
            raise PowerSupplyError(
                "%s: 写入后回读 %.2f V，与设定 %.2f V 不符"
                % (self.name, self.info.get("vset", -1), v))
        return v

    def set_current(self, amps, settle=False):
        """设置限流值（A）。"""
        a = float(amps)
        if not self.info:
            self.read_status()
        imax = self.info.get("imax") or 5.0
        if a > imax + 1e-9:
            raise PowerSupplyError(
                "%s: 设定 %.3f A 超过设备最大 %.2f A" % (self.name, a, imax))
        if self.protocol == "none":
            self.set_a = a
            return a

        vs = self.info.get("v_scale", 100.0)
        is_ = self.info.get("i_scale", 1000.0)
        v_raw = int(round((self.set_v if self.set_v is not None else 0.0) * vs))
        if not self._write3(self._ctrl_word(), v_raw, int(round(a * is_))):
            raise PowerSupplyError("%s: 设置电流无响应" % self.name)
        self.set_a = a
        if settle and self.settle_s > 0:
            time.sleep(self.settle_s)
        self.read_status()
        return a

    def output(self, on):
        """开/关输出。

        本机实测：控制寄存器 OUTCTRL 位与写线圈**设备都不响应**（PS 恒为 1，
        且设定电压 > 0 时**实测电压仍为 0**），说明这台电源的输出使能不在串口
        控制范围内（很可能要按面板的 OUTPUT 键）。所以这里会：
          · 写控制字（尽力而为）
          · **回读校验**（PS 位 + 实测电压）
          · 没真正生效就抛异常，绝不让界面显示假的"成功"
        """
        on = bool(on)
        if self.protocol == "none":
            self.out_on = on
            return on
        if not self.info:
            self.read_status()

        vs = self.info.get("v_scale", 100.0)
        is_ = self.info.get("i_scale", 1000.0)
        v_raw = int(round((self.set_v if self.set_v is not None else 0.0) * vs))
        i_raw = int(round((self.set_a if self.set_a is not None else 1.0) * is_))

        if not self._write3(self._ctrl_word(out_on=on), v_raw, i_raw):
            raise PowerSupplyError("%s: 输出开关命令无响应" % self.name)

        time.sleep(0.4)
        self.read_status()
        ps = bool(self.info.get("out_on"))
        volt = float(self.info.get("volt") or 0.0)
        vset = float(self.info.get("vset") or 0.0)
        self.out_on = ps

        if on:
            # 真要"输出"，必须看到实测电压跟上（设定>1V 时）
            if vset > 1.0 and volt < vset * 0.5:
                raise PowerSupplyError(
                    "%s: 已经写了输出开（状态显示 PS=%d），但**实测电压仍为 %.2f V**"
                    "（设定 %.2f V）。说明这台电源的输出使能不受串口控制——"
                    "请在**电源面板上按 OUTPUT 键**让输出真正接通。"
                    % (self.name, ps, volt, vset))
            return True
        else:
            if ps:
                raise PowerSupplyError(
                    "%s: 设备没有响应'关输出'（PS 仍为 1）。"
                    "可改用『把电压设为 0 V』停止驱动" % self.name)
            return False


# ============================================================================
#  自测（只读，不改状态）
# ============================================================================
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    port = sys.argv[1] if len(sys.argv) > 1 else "COM13"
    print("=== 只读自测（不会改动电源状态）===")
    ps = PowerSupply(port=port, baud=2400, addr=0, protocol="modbus")
    print("  open:", ps.open(verbose=True))
    print("  原始寄存器:", ps.info.get("raw"))
    print("  状态:", ps.status_text())
    print("\n  连续读 3 次:")
    for k in range(3):
        ps.read_status()
        print("    #%d  %s" % (k + 1, ps.status_text()))
        time.sleep(0.3)
    ps.ser.close()
    print("\n  已断开（未改任何设置）")
