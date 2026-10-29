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
        self.last_verify = {}           # 最近一次设电压后的"设定 vs 实测"校验结果
        self.max_voltage = max_voltage  # 若为 None 则从设备读
        self.set_v = None
        self.set_a = None
        self.out_on = None

    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    #  端口自动发现（USB 转串口拔插后 COM 号可能会变）
    # ------------------------------------------------------------------
    # 只探测"确定是 USB 转串口"的芯片 VID。
    # 教训：Windows 上打开蓝牙/主板自带的 COM 口会阻塞很久（几十秒到几分钟），
    #       全端口扫描会把程序卡死。这里只扫真正插着 USB 芯片的口。
    USB_VIDS = {0x1A86,   # CH340 / CH341（本机电源用的就是它）
                0x2341, 0x2A03, 0x1B4F, 0x239A,   # Arduino 系
                0x0403,  # FTDI
                0x10C4,  # CP210x（Silicon Labs）
                0x0483,  # STM32 虚拟串口
                0x067B,  # PL2303
                0x1B4F,  # SparkFun
                }

    @classmethod
    def list_usb_ports(cls):
        """只列出 USB 转串口的端口 [(device, vid, description), ...]"""
        out = []
        try:
            from serial.tools import list_ports as lp
            for p in lp.comports():
                vid = getattr(p, "vid", None)
                if vid in cls.USB_VIDS:
                    out.append((p.device, vid, p.description or ""))
        except Exception:
            pass
        return out

    def find_port(self, verbose=False, max_scan=6):
        """扫描 **USB 转串口** 的端口，找出能按当前协议应答的那个。

        用途：USB 转串口拔插后 Windows 可能把 COM13 变成 COM14 / COM5，
        或者旧端口号被别的设备占走。这里靠"发一帧读状态、看有没有正确应答"
        来认设备，比猜端口号可靠。
        """
        import serial
        cands = self.list_usb_ports()
        if not cands:
            return None
        tried = []
        for dev, _vid, desc in cands[:max_scan]:
            if dev == self.port:
                continue          # 原端口已经在 open() 里试过了
            tried.append(dev)
            old_port, old_ser = self.port, self.ser
            self.port = dev
            self.ser = None
            ok = False
            try:
                self.ser = serial.Serial(dev, self.baud, timeout=self.timeout,
                                         bytesize=8, parity="N", stopbits=1)
                time.sleep(0.12)
                self.ser.reset_input_buffer()
                ok = self.read_status(verbose=False, _retry=False)
            except Exception:
                ok = False
            finally:
                if not ok:
                    try:
                        if self.ser:
                            self.ser.close()
                    except Exception:
                        pass
                    self.ser = None
                    self.port = old_port
            if ok:
                if verbose:
                    print("      在 %s (%s) 上找到了电源" % (dev, desc))
                return dev
        if verbose:
            print("      扫过这些 USB 串口都没有应答: %s"
                  % (", ".join(tried) or "（无其它 USB 串口）"))
        return None

    # ------------------------------------------------------------------
    def open(self, verbose=False):
        if self.protocol == "none":
            return "dry-run（不发送）"
        import serial
        err = None
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout,
                                     bytesize=8, parity="N", stopbits=1)
        except Exception as e:
            err = e
            self.ser = None

        if self.ser is not None:
            time.sleep(0.15)
            try:
                self.ser.reset_input_buffer()
            except Exception:
                pass
            ok = self.read_status(verbose=verbose)
            if ok:
                return "已连接 %s @%d bps 地址%d  %.3fV/%.3fA 上限%.1fV/%.1fA" % (
                    self.port, self.baud, self.addr,
                    self.info.get("volt", 0), self.info.get("curr", 0),
                    self.info.get("vmax", 0), self.info.get("imax", 0))
            err = "端口能打开但读不到正确应答"

        # ---- 原端口不行 → 自动扫描其它端口（拔插后 COM 号变了的情况）----
        old = self.port
        found = self.find_port(verbose=verbose)
        if found:
            return ("已连接 %s（原端口 %s 已失效，自动改到新端口）@%d bps 地址%d"
                    "  %.3fV/%.3fA" % (self.port, old, self.baud, self.addr,
                                      self.info.get("volt", 0),
                                      self.info.get("curr", 0)))

        ports = ", ".join("%s(%s)" % (d, desc)
                          for d, _v, desc in self.list_usb_ports()) \
            or "（未发现 USB 串口）"
        raise PowerSupplyError(
            "%s: 连不上（%s @ %d bps 地址 %d）。原因：%s\n"
            "  当前可用串口：%s\n"
            "  处理办法：① 拔插一下电源的 USB 线，等 3 秒再点『重连』；"
            "② 确认电源已开机；③ 确认没有被其它软件（如官方上位机）占用"
            % (self.name, old, self.baud, self.addr, err, ports))

    # ------------------------------------------------------------------
    def reconnect(self, max_try=3, verbose=False):
        """断线后重连：关掉旧串口 → 重新打开（含端口自动发现）。"""
        if self.protocol == "none":
            return True
        self._close_serial()
        last = None
        for i in range(max(1, int(max_try))):
            try:
                msg = self.open(verbose=verbose)
                if verbose:
                    print("      " + msg)
                return True
            except Exception as e:
                last = e
                time.sleep(0.4 * (i + 1))     # 退避：0.4 / 0.8 / 1.2 s
        raise PowerSupplyError("重连失败（试了 %d 次）：%s" % (max_try, str(last)[:200]))

    def _close_serial(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

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
        self._close_serial()

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
    def read_status(self, verbose=False, _retry=True):
        """读 0x0000 起 8 个寄存器，解析成 info 字典。成功返回 True。

        串口异常（USB 掉线、端口消失）时自动重连一次再重试。
        """
        if self.protocol == "none":
            return True
        if self.ser is None:
            if not _retry:
                return False
            self.reconnect(max_try=2, verbose=verbose)

        body = [self.addr, 0x03, 0x00, 0x00, 0x00, 0x08]
        frame = _with_crc(body)
        try:
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            self.ser.flush()
        except Exception as e:
            if _retry:
                if verbose:
                    print("      %s: 串口异常(%s)，尝试重连…" % (self.name, str(e)[:60]))
                try:
                    self.reconnect(max_try=2, verbose=verbose)
                    return self.read_status(verbose=verbose, _retry=False)
                except Exception as e2:
                    raise PowerSupplyError(
                        "%s: 串口断开且重连失败 (%s / %s)"
                        % (self.name, str(e)[:60], str(e2)[:120]))
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

        # ★★★ 端序陷阱（协议 1.3 明确写的）★★★
        #   状态寄存器0 / 状态寄存器1 —— **固定"高字节先"**，
        #     不受"串口数据大小端设定"影响
        #   电压 / 电流 / 最大电压电流 —— 按大小端设定（本机为**小端：低字节先**）
        #   写控制寄存器时同样必须"高字节先"，否则 bit8(OUTCTRL) 永远写不进去，
        #   表现就是"点了输出开、PS 位不变、实测电压恒为 0"。
        stat0 = (data[0] << 8) | data[1]
        stat1 = (data[2] << 8) | data[3]

        le = lambda i: data[i] | (data[i + 1] << 8)     # 小端
        volt_raw, curr_raw = le(4), le(6)
        vset_raw, iset_raw = le(8), le(10)
        vmax_raw, imax_raw = le(12), le(14)

        vpod = (stat0 >> 4) & 1
        ipod = (stat1 >> 4) & 1
        vscale = 100.0 if vpod == 0 else 10.0
        iscale = 1000.0 if ipod == 0 else 100.0

        self.info = {
            "raw": [stat0, stat1, volt_raw, curr_raw,
                    vset_raw, iset_raw, vmax_raw, imax_raw],
            "stat0": stat0, "stat1": stat1,
            "v_scale": vscale, "i_scale": iscale,
            "out_on": bool((stat0 >> 8) & 1),
            "ocp": bool((stat0 >> 9) & 1),
            "lock": bool((stat0 >> 10) & 1),
            "big_endian": bool((stat0 >> 11) & 1),
            "cv": not bool((stat0 >> 12) & 1),
            "alarm": bool((stat0 >> 13) & 1),
            "volt": volt_raw / vscale,
            "curr": curr_raw / iscale,
            "vset": vset_raw / vscale,
            "iset": iset_raw / iscale,
            "vmax": vmax_raw / vscale,
            "imax": imax_raw / iscale,
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

    def _write3(self, ctrl, v_raw, i_raw, _retry=True):
        """★ 本设备固件要求：**一次写满 3 个寄存器**（start=0x0000, count=3）。

        实测：写单个寄存器（count=1）设备**完全不回应也不生效**；
              一次写 3 个（0x0000 控制 / 0x0001 电压 / 0x0002 电流）立即成功。

        ★★★ 端序（协议 1.3）★★★
          控制寄存器 = 状态寄存器一类的"位寄存器" → **高字节先**
          电压/电流  = 数据寄存器 → 按大小端设定（本机**小端：低字节先**）
          写反了的典型症状：bit8(OUTCTRL) 写不进去，PS 位永远是 0，
          表现为"点了输出开、电源就是不输出"。
        """
        if self.protocol == "none":
            return True
        if not self.ser:
            raise PowerSupplyError("%s: 串口未打开" % self.name)

        body = [self.addr, 0x10, 0x00, 0x00, 0x00, 0x03, 0x06,
                (ctrl >> 8) & 0xFF, ctrl & 0xFF,              # 控制字：高字节先
                int(v_raw) & 0xFF, (int(v_raw) >> 8) & 0xFF,   # 电压：小端
                int(i_raw) & 0xFF, (int(i_raw) >> 8) & 0xFF]   # 电流：小端
        frame = _with_crc(body)
        if self.ser is None:
            if not _retry:
                return False
            self.reconnect(max_try=2)
        try:
            self.ser.reset_input_buffer()
            self.ser.write(frame)
            self.ser.flush()
        except Exception as e:
            if _retry:
                try:
                    self.reconnect(max_try=2)
                    return self._write3(ctrl, v_raw, i_raw, _retry=False)
                except Exception as e2:
                    raise PowerSupplyError(
                        "%s: 串口断开且重连失败 (%s / %s)"
                        % (self.name, str(e)[:60], str(e2)[:120]))
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
        i = self.info
        if abs(i.get("vset", -1) - v) > max(0.02, v * 0.002):
            raise PowerSupplyError(
                "%s: 写入后回读设定值 %.2f V，与设定 %.2f V 不符"
                % (self.name, i.get("vset", -1), v))

        # ★ 设定值 vs 实测值 的差异检查（只在 CV 模式下有意义；
        #   CC 模式实测电压本来就会低于设定值）
        self.last_verify = {
            "vset": i.get("vset"), "volt": i.get("volt"),
            "curr": i.get("curr"), "cv": i.get("cv"),
            "diff": (i.get("volt") or 0) - (i.get("vset") or 0),
            "warn": "",
        }
        if i.get("cv") and abs(self.last_verify["diff"]) > max(0.05, v * 0.03):
            self.last_verify["warn"] = (
                "设定 %.2f V 但实测 %.3f V（偏差 %+.3f V，%.1f%%）。可能原因："
                "① 负载还在变化/热态电阻变化；② 电源在特定区间有固件偏差"
                "（本机实测 2.99~3.06 V 会偏高约 +0.22 V）；③ 读数时机太早。"
                "若需要精确电压，建议避开该区间或等一下再看"
                % (i.get("vset"), i.get("volt"), self.last_verify["diff"],
                   100.0 * self.last_verify["diff"] / max(0.01, v)))
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
        """开/关输出（写控制寄存器 bit8，高字节先）。

        校验只信 **PS 位**（状态寄存器0 的 bit8）—— 不能用实测电压判断，
        因为电源在**恒流(CC)模式**下实测电压会明显低于设定值，那属于正常现象。
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

        ok = False
        for attempt in range(3):                # 设备偶发不响应，重试几次
            if not self._write3(self._ctrl_word(out_on=on), v_raw, i_raw):
                time.sleep(0.15)
                continue
            time.sleep(0.35)
            self.read_status()
            if bool(self.info.get("out_on")) == on:
                ok = True
                break
            time.sleep(0.2)

        if not ok:
            raise PowerSupplyError(
                "%s: 设备没有响应『输出%s』（连试 3 次，PS 位仍为 %d）。"
                "请检查电源面板是否处于本地模式，或按一下面板 OUTPUT 键"
                % (self.name, "开" if on else "关",
                   bool(self.info.get("out_on"))))

        self.out_on = on
        # 仅作提示：输出开了但既没电压也没电流 → 可能没接负载
        if on:
            v = float(self.info.get("volt") or 0)
            a = float(self.info.get("curr") or 0)
            if v < 0.2 and a < 0.001:
                raise PowerSupplyError(
                    "%s: 输出已开启（PS=1），但实测 %.2f V / %.3f A，"
                    "负载端似乎没接通（空载或加热器未连接）"
                    % (self.name, v, a))
        return on


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
