# -*- coding: utf-8 -*-
"""
Wanptek 直流电源控制（MODBUS-RTU）—— 完全基于实测行为重写
================================================================================
本文件的每一条约束都用「发命令 → 看回应」实测确认过，不是照协议文档抄。

■ 实测确认的硬约束（违反任何一条，设备就不理你）
    1. 串口 8N1，波特率 **2400**，从站地址 **0**
    2. **读**只支持 0x03 + 起始 0x0000 + 个数 **8**
       实测：读 1 / 2 / 4 个 → 全部无回应（1.08 秒满额超时）
    3. **写**只支持 0x10 + 起始 0x0000 + 个数 **3**
       实测：写 1 / 2 个、写单个 0x06 → 全部无回应
       只有 count=3 会在约 110 ms 内回 `00 10 00 00 00 03 81 D9`
    4. **线圈功能完全不支持**：0x01 读线圈地址 0~7 全部超时；
       0x05 写线圈 0xFF00/0x0001/0x0000 全部无应答
    5. 端序：
       状态寄存器0 / 状态寄存器1   → **高字节先**（协议 1.3 固定）
       控制寄存器                 → **高字节先**
       电压 / 电流寄存器           → **小端**（低字节先）
       决定性实验：控制字 0x0100 发 `01 00` → PS 变 1；发 `00 01` → PS 仍 0

■ 状态位
    状态0 bit8  = PS   输出开关（1=开）
    状态0 bit9  = OS   OCP 是否启用
    状态0 bit10 = LS   面板是否锁定
    状态0 bit11 = BL   0=小端 1=大端
    状态0 bit12 = WS   0=恒压(CV) 1=恒流(CC)
    状态0 bit13 = AS   报警
    状态0 bit4  = VPOD 0=电压2位小数(/100) 1=1位小数(/10)
    状态1 bit4  = IPOD 0=电流3位小数(/1000) 1=2位小数(/100)

■ 本版不做事重连
    串口一旦出错就抛 PowerSupplyError，由界面提示用户重新连接。
    原因：自动重连会在写操作中途改变端口与状态缓存，导致"基于错误前提去写"，
    比直接报错更难排查。
"""

import time

# ----------------------------------------------------------------------------
#  寄存器与位定义（全部来自实测）
# ----------------------------------------------------------------------------
REG_CTRL = 0x0000      # 控制寄存器（写）
REG_VSET = 0x0001      # 设定电压（写）
REG_ISET = 0x0002      # 设定电流（写）
CTRL_OUT_BIT = 8       # 控制字里的输出开关位
CTRL_OCP_BIT = 9       # 控制字里的 OCP 位

READ_COUNT = 8         # ★ 实测：只有读 8 个才有回应
WRITE_COUNT = 3        # ★ 实测：只有写 3 个才有回应


# ----------------------------------------------------------------------------
#  MODBUS CRC16（多项式 0xA001），返回 (crcL, crcH) 可直接追加到帧尾
# ----------------------------------------------------------------------------
def crc16(data):
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if (crc & 1) else (crc >> 1)
    return crc & 0xFF, (crc >> 8) & 0xFF


def _with_crc(body):
    lo, hi = crc16(body)
    return bytes(body) + bytes([lo, hi])


def _crc_ok(frame):
    if len(frame) < 4:
        return False
    lo, hi = crc16(frame[:-2])
    return frame[-2] == lo and frame[-1] == hi


def _hex(b):
    return " ".join("%02X" % x for x in b) if b else "(无回应)"


def _bits16(v):
    """打印 16 位二进制，方便核对哪一位在变。"""
    return "".join("1" if (v >> i) & 1 else "0" for i in range(15, -1, -1))


class PowerSupplyError(Exception):
    pass


# ============================================================================
class PowerSupply:
    """Wanptek 电源控制。

    protocol:
        "modbus" —— 真机控制（默认）
        "none"   —— dry-run：只记录不发送，用于先验证界面/数据流
    """

    def __init__(self, port="COM13", baud=2400, addr=0, protocol="modbus",
                 settle_s=1.0, timeout=0.4, max_voltage=None,
                 off_on_exit=True, debug=False, name="电压源"):
        self.port = port
        self.baud = int(baud)
        self.addr = int(addr)
        self.protocol = (protocol or "none").lower()
        self.settle_s = float(settle_s)
        self.timeout = float(timeout)
        self.max_voltage = max_voltage
        self.off_on_exit = bool(off_on_exit)
        self.debug = bool(debug)
        self.name = name

        self.ser = None
        self.info = {}          # 最近一次 read_status() 的解析结果
        self.last_verify = {}   # 最近一次设电压后的「设定 vs 实测」校验
        self.last_error = ""

    # ------------------------------------------------------------------
    def _log(self, msg):
        if self.debug:
            print("      [%s] %s" % (self.name, msg))

    # ------------------------------------------------------------------
    #  连接 / 断开（没有自动重连）
    # ------------------------------------------------------------------
    def open(self, verbose=False):
        """打开串口并读一次状态验证。失败直接抛异常。"""
        if self.protocol == "none":
            return "dry-run（不发送任何命令）"
        import serial
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout,
                                     bytesize=8, parity="N", stopbits=1)
        except Exception as e:
            self.ser = None
            raise PowerSupplyError(
                "%s: 打不开 %s @ %d bps —— %s"
                % (self.name, self.port, self.baud, str(e)[:120]))
        time.sleep(0.15)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

        if not self.read_status():
            err = self.last_error
            self._close_serial()
            raise PowerSupplyError(
                "%s: 端口 %s 能打开但读不到应答（%s）。请确认：\n"
                "  ① 电源已开机；② 波特率 %d、站地址 %d 与面板设置一致；\n"
                "  ③ 没有被官方上位机等软件占用"
                % (self.name, self.port, err, self.baud, self.addr))

        i = self.info
        if self.max_voltage is None:
            self.max_voltage = i.get("vmax") or 31.0
        return ("已连接 %s @%d bps 地址%d  实测 %.3fV/%.3fA  设定 %.2fV/%.3fA  %s"
                % (self.port, self.baud, self.addr,
                   i["volt"], i["curr"], i["vset"], i["iset"],
                   "输出开" if i["out_on"] else "输出关"))

    def _close_serial(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def close(self, off_output=None):
        """断开。off_output=None 时按 off_on_exit 决定是否先关输出。"""
        do_off = self.off_on_exit if off_output is None else bool(off_output)
        if do_off:
            try:
                if self.out_on:
                    self.output(False)
            except Exception:
                pass
        self._close_serial()

    @property
    def is_open(self):
        return self.protocol == "none" or (self.ser is not None)

    @property
    def out_on(self):
        return bool((self.info or {}).get("out_on", False))

    # ------------------------------------------------------------------
    #  ★ 唯一的读入口
    # ------------------------------------------------------------------
    def read_status(self, verbose=False):
        """读 0x0000 起 **8 个**寄存器并解析。成功返回 True。

        实测：这是设备唯一支持的读命令（读 1/2/4 个都无回应）。
        """
        if self.protocol == "none":
            return True
        if not self.ser:
            self.last_error = "串口未打开"
            return False

        frame = _with_crc([self.addr, 0x03, 0x00, 0x00,
                           (READ_COUNT >> 8) & 0xFF, READ_COUNT & 0xFF])
        self._log("TX 读8   %s" % _hex(frame))
        try:
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            self.ser.flush()
        except Exception as e:
            self.last_error = "发送失败: %s" % str(e)[:80]
            self._log("!! " + self.last_error)
            return False

        need = 3 + 2 * READ_COUNT + 2       # 地址+功能码+字节数 + 数据 + CRC
        buf = b""
        t0 = time.monotonic()
        while len(buf) < need and time.monotonic() - t0 < max(0.6, self.timeout * 2):
            chunk = self.ser.read(need - len(buf))
            if chunk:
                buf += chunk
        self._log("RX 读8   %s" % _hex(buf))

        if len(buf) < need:
            self.last_error = "读超时（只收到 %d/%d 字节）" % (len(buf), need)
            return False
        if not _crc_ok(buf):
            self.last_error = "CRC 校验失败: %s" % _hex(buf)
            return False
        if buf[0] != self.addr or buf[1] != 0x03:
            self.last_error = "回应帧头不符: %s" % _hex(buf[:4])
            return False

        data = buf[3:3 + 2 * READ_COUNT]
        # ★ 状态寄存器0/1：高字节先（实测 0x1101 才与"输出开+恒流"自洽）
        stat0 = (data[0] << 8) | data[1]
        stat1 = (data[2] << 8) | data[3]
        # ★ 其余寄存器：小端
        le = lambda i: data[i] | (data[i + 1] << 8)
        volt_raw = le(4)      # 寄存器2 当前电压
        curr_raw = le(6)      # 寄存器3 当前电流
        vset_raw = le(8)      # 寄存器4 设定电压
        iset_raw = le(10)     # 寄存器5 设定电流
        vmax_raw = le(12)     # 寄存器6 设备最大电压
        imax_raw = le(14)     # 寄存器7 设备最大电流

        vpod = (stat0 >> 4) & 1
        ipod = (stat1 >> 4) & 1
        vs = 100.0 if vpod == 0 else 10.0       # 电压：2位小数 / 1位小数
        is_ = 1000.0 if ipod == 0 else 100.0    # 电流：3位小数 / 2位小数

        self.info = {
            "stat0": stat0, "stat1": stat1,
            "raw": [stat0, stat1, volt_raw, curr_raw,
                    vset_raw, iset_raw, vmax_raw, imax_raw],
            "v_scale": vs, "i_scale": is_,
            "out_on": bool((stat0 >> 8) & 1),       # PS
            "ocp": bool((stat0 >> 9) & 1),          # OS
            "lock": bool((stat0 >> 10) & 1),        # LS
            "big_endian": bool((stat0 >> 11) & 1),  # BL
            "cv": not bool((stat0 >> 12) & 1),      # WS=0 → 恒压
            "alarm": bool((stat0 >> 13) & 1),       # AS
            "volt": volt_raw / vs,
            "curr": curr_raw / is_,
            "vset": vset_raw / vs,
            "iset": iset_raw / is_,
            "vmax": vmax_raw / vs,
            "imax": imax_raw / is_,
        }
        self.last_error = ""
        if verbose:
            self._log("状态0=0x%04X b=%s" % (stat0, _bits16(stat0)))
        return True

    # ------------------------------------------------------------------
    #  ★ 唯一的写入口
    # ------------------------------------------------------------------
    def _write3(self, ctrl, v_raw, i_raw):
        """一次写 3 个寄存器：0x0000 控制 / 0x0001 电压 / 0x0002 电流。

        实测：这是设备唯一支持的写命令（写 1/2 个、0x06 都无回应）。
        端序：控制字 **高字节先**；电压/电流 **小端**。
        成功判据：收到 8 字节且 CRC 正确。
        """
        if self.protocol == "none":
            return True
        if not self.ser:
            self.last_error = "串口未打开"
            return False

        body = [self.addr, 0x10,
                0x00, 0x00,                                        # 起始 0x0000
                (WRITE_COUNT >> 8) & 0xFF, WRITE_COUNT & 0xFF,     # 个数 3
                2 * WRITE_COUNT,                                   # 字节数 6
                (ctrl >> 8) & 0xFF, ctrl & 0xFF,                   # 控制字：高字节先
                int(v_raw) & 0xFF, (int(v_raw) >> 8) & 0xFF,       # 电压：小端
                int(i_raw) & 0xFF, (int(i_raw) >> 8) & 0xFF]       # 电流：小端
        frame = _with_crc(body)
        self._log("TX 写3   %s" % _hex(frame))
        try:
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            self.ser.flush()
        except Exception as e:
            self.last_error = "发送失败: %s" % str(e)[:80]
            self._log("!! " + self.last_error)
            return False

        buf = b""
        t0 = time.monotonic()
        while len(buf) < 8 and time.monotonic() - t0 < max(0.6, self.timeout * 2):
            c = self.ser.read(8 - len(buf))
            if c:
                buf += c
        self._log("RX 写3   %s" % _hex(buf))

        if len(buf) < 8:
            self.last_error = "写超时（只收到 %d/8 字节）" % len(buf)
            return False
        if not _crc_ok(buf):
            self.last_error = "写应答 CRC 失败: %s" % _hex(buf)
            return False
        self.last_error = ""
        return True

    def _write_merged(self, out_on=None, volts=None, amps=None):
        """读设备当前真实值，只替换调用者指定的项，然后一次写 3 个寄存器。

        为什么要"先读"：协议强制一次写满 3 个，若拿本地缓存去填另外两项，
        就会覆盖用户在面板上的改动（例如点"输出开"会把电压改回旧值）。

        out_on / volts / amps 传 None 表示"保持设备当前值"。
        """
        if self.protocol == "none":
            return True
        if not self.read_status():
            return False
        i = self.info
        vs = i["v_scale"]
        is_ = i["i_scale"]

        # 控制字：bit8=输出, bit9=OCP（不指定就保持设备当前状态）
        out = i["out_on"] if out_on is None else bool(out_on)
        ocp = i["ocp"]
        ctrl = (((1 if out else 0) << CTRL_OUT_BIT)
                | ((1 if ocp else 0) << CTRL_OCP_BIT))

        v_raw = int(round((i["vset"] if volts is None else float(volts)) * vs))
        i_raw = int(round((i["iset"] if amps is None else float(amps)) * is_))
        return self._write3(ctrl, v_raw, i_raw)

    # ------------------------------------------------------------------
    #  对外操作
    # ------------------------------------------------------------------
    def set_voltage(self, volts, settle=True):
        """只改设定电压，不动输出开关和限流。"""
        v = float(volts)
        if v < 0:
            raise PowerSupplyError("%s: 电压不能为负（%g）" % (self.name, v))
        if not self.info and not self.read_status():
            raise PowerSupplyError("%s: 读不到状态，无法设置电压（%s）"
                                   % (self.name, self.last_error))
        vmax = self.max_voltage or self.info.get("vmax") or 31.0
        if v > vmax + 1e-9:
            raise PowerSupplyError("%s: %.3f V 超过设备最大 %.2f V"
                                   % (self.name, v, vmax))

        if self.protocol == "none":
            return v

        if not self._write_merged(volts=v):
            raise PowerSupplyError("%s: 设置电压失败 —— %s"
                                   % (self.name, self.last_error))

        if settle and self.settle_s > 0:
            time.sleep(self.settle_s)

        # 回读确认设定值真的进去了
        if not self.read_status():
            raise PowerSupplyError("%s: 写入后读回失败 —— %s"
                                   % (self.name, self.last_error))
        i = self.info
        if abs(i["vset"] - v) > max(0.02, v * 0.002):
            raise PowerSupplyError(
                "%s: 写入 %.2f V，但设备读回 %.2f V" % (self.name, v, i["vset"]))

        # 「设定 vs 实测」差异提示（恒压模式下才有意义）
        self.last_verify = {
            "vset": i["vset"], "volt": i["volt"], "curr": i["curr"],
            "cv": i["cv"], "diff": i["volt"] - i["vset"], "warn": "",
        }
        if i["cv"] and abs(self.last_verify["diff"]) > max(0.05, v * 0.03):
            self.last_verify["warn"] = (
                "设定 %.2f V 但实测 %.3f V（差 %+.3f V）。可能是：负载在变、"
                "读数太早、或电源在 2.99~3.06 V 区间有已知 +0.22 V 偏差"
                % (i["vset"], i["volt"], self.last_verify["diff"]))
        return v

    def set_current(self, amps, settle=False):
        """只改限流，不动输出电压和输出开关。"""
        a = float(amps)
        if a <= 0:
            raise PowerSupplyError("%s: 限流必须大于 0（%g）" % (self.name, a))
        if not self.info and not self.read_status():
            raise PowerSupplyError("%s: 读不到状态，无法设置限流（%s）"
                                   % (self.name, self.last_error))
        imax = self.info.get("imax") or 5.0
        if a > imax + 1e-9:
            raise PowerSupplyError("%s: %.3f A 超过设备最大 %.2f A"
                                   % (self.name, a, imax))

        if self.protocol == "none":
            return a

        if not self._write_merged(amps=a):
            raise PowerSupplyError("%s: 设置限流失败 —— %s"
                                   % (self.name, self.last_error))
        if settle and self.settle_s > 0:
            time.sleep(self.settle_s)

        if not self.read_status():
            raise PowerSupplyError("%s: 写入后读回失败 —— %s"
                                   % (self.name, self.last_error))
        if abs(self.info["iset"] - a) > max(0.002, a * 0.005):
            raise PowerSupplyError(
                "%s: 写入 %.3f A，但设备读回 %.3f A"
                % (self.name, a, self.info["iset"]))
        return a

    def output(self, on):
        """只改输出开关，不动电压和限流。校验只看 PS 位。"""
        on = bool(on)
        if self.protocol == "none":
            return on
        if not self.info and not self.read_status():
            raise PowerSupplyError("%s: 读不到状态，无法开关输出（%s）"
                                   % (self.name, self.last_error))

        last_err = ""
        for _ in range(3):
            if not self._write_merged(out_on=on):
                last_err = self.last_error
                time.sleep(0.2)
                continue
            time.sleep(0.4)
            if not self.read_status():
                last_err = self.last_error
                continue
            if self.info["out_on"] == on:
                # 开输出后如果既没电压也没电流，只提示不报错
                # （此时输出确实已经打开，抛异常会误导）
                if on:
                    self.last_verify = {
                        "vset": self.info["vset"], "volt": self.info["volt"],
                        "curr": self.info["curr"], "cv": self.info["cv"],
                        "diff": self.info["volt"] - self.info["vset"],
                        "warn": "",
                    }
                    if self.info["volt"] < 0.2 and self.info["curr"] < 0.001:
                        self.last_verify["warn"] = (
                            "输出已开（PS=1），但实测 %.2f V / %.0f mA —— "
                            "负载端似乎没接通"
                            % (self.info["volt"], self.info["curr"] * 1000))
                return on
            last_err = "PS 位没变（仍为 %d）" % self.info["out_on"]
            time.sleep(0.2)

        raise PowerSupplyError(
            "%s: 连试 3 次都没能让输出%s。最后一次原因：%s\n"
            "  可能：① 面板处于本地/锁定状态，按一下面板 OUTPUT 键；"
            "② 限流设成 0 导致一开就保护"
            % (self.name, "打开" if on else "关闭", last_err))

    # ------------------------------------------------------------------
    def status_text(self):
        if self.protocol == "none":
            return "dry-run"
        i = self.info
        if not i:
            return "未读取"
        return ("实测 %.3fV/%.0fmA  设定 %.2fV/%.0fmA  %s  %s%s"
                % (i["volt"], i["curr"] * 1000, i["vset"], i["iset"] * 1000,
                   "输出开" if i["out_on"] else "输出关",
                   "恒压" if i["cv"] else "恒流",
                   "  ⚠报警" if i["alarm"] else ""))

    def dump_bits(self):
        """把两个状态寄存器的 16 位全打出来（排查用）。"""
        i = self.info
        if not i:
            return "未读取"
        s0, s1 = i["stat0"], i["stat1"]
        return ("状态0=0x%04X b=%s (PS=%d OS=%d LS=%d BL=%d WS=%d AS=%d VPOD=%d)\n"
                "状态1=0x%04X b=%s (IPOD=%d)"
                % (s0, _bits16(s0), (s0 >> 8) & 1, (s0 >> 9) & 1,
                   (s0 >> 10) & 1, (s0 >> 11) & 1, (s0 >> 12) & 1,
                   (s0 >> 13) & 1, (s0 >> 4) & 1,
                   s1, _bits16(s1), (s1 >> 4) & 1))


# ============================================================================
#  只读自测（不改动任何状态）
# ============================================================================
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    port = sys.argv[1] if len(sys.argv) > 1 else "COM13"
    print("=" * 78)
    print("Wanptek 电源只读自测 @ %s（不会改动任何状态）" % port)
    print("=" * 78)
    ps = PowerSupply(port=port, debug=True)
    print(ps.open(verbose=True))
    print()
    print(ps.dump_bits())
    print()
    for k in range(3):
        ps.read_status()
        print("  #%d %s" % (k + 1, ps.status_text()))
        time.sleep(0.3)
    ps._close_serial()
    print("\n已断开（未改任何设置）")
