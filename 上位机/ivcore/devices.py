# -*- coding: utf-8 -*-
"""
硬件封装
================================================================================
    Arduino   串口通信（自动探测端口、看门狗心跳、行协议解析）
    DMM       DMM6500 单台表的封装（pyvisa / USB-TMC）

设计要点：
  · Arduino 的 CH340 **没有序列号**，且 USB 重插后端口号会变 →
    `Arduino.auto_detect()` 用"发 PING 看谁回应"的方式找它，而不是记 COM 号。
  · 只探测 USB 串口芯片（CH340/Arduino/FTDI/CP210x）；
    蓝牙串口与主板串口在 Windows 上打开会长时间阻塞，必须跳过。
  · DMM 读数用 :READ? 触发单次测量（实测约 25 ms），读数时刻与命令一一对应。
"""

import time


# 常见 USB 串口芯片的 VID（用于过滤掉蓝牙/主板串口）
KNOWN_USB_VID = {
    0x1A86,   # CH340 / CH341（本品 Arduino）
    0x2341,   # Arduino 官方
    0x2A03,   # Arduino（旧）
    0x0403,   # FTDI
    0x10C4,   # CP210x
    0x0483,   # STMicro
    0x1B4F,   # SparkFun
    0x239A,   # Adafruit
}


# ============================================================================
#  Arduino
# ============================================================================
class ArduinoError(Exception):
    pass


class Arduino:
    """Arduino Nano（开关阵列控制器）。

    协议（115200, 行尾 \\n）:
        发送: S / S 1111|1111 / s / c / u / i1..i4 / v1..v4 / a / PING
        接收: READY v1.1 / WIN <ch> <V|I> / ROUND DONE / SAFE / OK ... / WATCHDOG / ERR ...
    """

    def __init__(self, port=None, baud=115200, timeout=1.0):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None
        self._last_beat = 0.0
        self.heartbeat_s = 5.0

    # ---- 探测 ----
    @staticmethod
    def list_ports():
        """返回 [(device, description, vid, pid), ...]"""
        from serial.tools import list_ports
        out = []
        for p in list_ports.comports():
            out.append((p.device, p.description or "", p.vid, p.pid))
        return out

    @classmethod
    def auto_detect(cls, baud=115200, verbose=True):
        """逐个试探 USB 串口，返回回应的那个端口号（找不到返回 None）。"""
        import serial
        ports = cls.list_ports()
        usb = [p for p in ports if p[2] in KNOWN_USB_VID]

        if verbose:
            print("      USB 串口: %s" % (", ".join(p[0] for p in usb) or "无"))
            skipped = [p[0] for p in ports if p[2] not in KNOWN_USB_VID]
            if skipped:
                print("      跳过非 USB 串口: %s" % ", ".join(skipped))

        for dev, desc, vid, pid in usb:
            try:
                ser = serial.Serial(dev, baud, timeout=0.4)
            except Exception as e:
                if verbose:
                    msg = str(e)
                    hint = "   <- 被占用！关掉 Arduino IDE 的串口监视器" \
                        if ("Permission" in type(e).__name__ or "Permission" in msg
                            or "拒绝" in msg) else ""
                    print("      %-7s 打不开 (%s)%s" % (dev, msg[:40], hint))
                continue
            try:
                time.sleep(0.3)
                ser.reset_input_buffer()
                ser.write(b"PING\n")
                ser.flush()
                time.sleep(0.4)
                resp = ser.read(300).decode("utf-8", "replace")
            except Exception:
                resp = ""
            finally:
                ser.close()

            if resp.strip():
                tag = "v1.1" if "READY v1.1" in resp else "旧固件"
                if verbose:
                    print("      %-7s <- 有回应 [%s]" % (dev, tag))
                return dev
            elif verbose:
                print("      %-7s    无回应" % dev)
        return None

    # ---- 连接 ----
    def open(self, auto=True, verbose=True):
        import serial
        if not self.port and auto:
            self.port = self.auto_detect(self.baud, verbose)
        if not self.port:
            raise ArduinoError("未找到 Arduino。请检查 USB、驱动，并关掉 Arduino IDE 的串口监视器。")

        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
        except Exception as e:
            raise ArduinoError(
                "打不开串口 %s : %s\n"
                "        最常见原因：Arduino IDE 的【串口监视器】正开着，占用了串口。\n"
                "        -> 请在 IDE 里关掉串口监视器，再运行本程序。"
                % (self.port, e))
        time.sleep(1.8)                      # 等 Arduino 复位后启动
        self.ser.reset_input_buffer()
        banner = self.read_lines(0.8)
        self._last_beat = time.monotonic()
        return banner

    def close(self):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    @property
    def is_open(self):
        return self.ser is not None and self.ser.is_open

    # ---- 收发 ----
    def send(self, cmd):
        if not self.is_open:
            raise ArduinoError("串口未打开")
        self.ser.write((cmd + "\n").encode("ascii"))
        self.ser.flush()

    def readline(self):
        """读一行（已去空白）。超时返回空串。"""
        if not self.is_open:
            return ""
        raw = self.ser.readline()
        if not raw:
            return ""
        return raw.decode("utf-8", "replace").strip()

    def read_lines(self, seconds):
        """在给定时间内把所有到达的行读出来，返回列表。"""
        out = []
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            line = self.readline()
            if line:
                out.append(line)
        return out

    def beat(self, force=False):
        """看门狗心跳：固件 10 秒无命令会回安全态，这里定期喂 PING。"""
        now = time.monotonic()
        if force or (now - self._last_beat) >= self.heartbeat_s:
            self._last_beat = now
            self.send("PING")
            return True
        return False

    # ---- 安全 ----
    def safe(self, drain=0.3):
        self.send("s")
        return self.read_lines(drain)


# ============================================================================
#  DMM6500
# ============================================================================
class DMMError(Exception):
    pass


class DMM:
    """Keithley DMM6500 单台表。"""

    def __init__(self, resource=None, serial=None, timeout=5.0, name="DMM"):
        self.resource = resource
        self.serial = serial            # *IDN? 里的序列号，用于断线后重新识别
        self.timeout = timeout
        self.name = name
        self.inst = None
        self.idn = ""

    # ---- 发现 ----
    @staticmethod
    def list_resources():
        try:
            import pyvisa
        except ImportError:
            raise DMMError("缺少 pyvisa。请运行:  pip install pyvisa")
        rm = pyvisa.ResourceManager()
        return rm, list(rm.list_resources())

    @classmethod
    def find_by_serial(cls, rm, serial, verbose=True):
        """按序列号在 VISA 资源里找表（USB 重插后地址可能改变）。"""
        for res in rm.list_resources():
            if not res.startswith("USB"):
                continue
            if serial and serial in res:
                return res
        # 地址里没有序列号时，逐台读 *IDN? 比对
        for res in rm.list_resources():
            if not res.startswith("USB"):
                continue
            try:
                inst = rm.open_resource(res)
                inst.timeout = 3000
                idn = inst.query("*IDN?").strip()
                inst.close()
                if serial and serial in idn:
                    return res
            except Exception:
                continue
        return None

    # ---- 连接 ----
    def open(self, verbose=True):
        import pyvisa
        if not self.resource:
            raise DMMError("%s: 未配置 resource" % self.name)
        rm = pyvisa.ResourceManager()
        try:
            self.inst = rm.open_resource(self.resource)
        except Exception as e:
            # 尝试按序列号重新找（USB 重插后地址可能变）
            alt = self.find_by_serial(rm, self.serial, verbose) if self.serial else None
            if alt and alt != self.resource:
                if verbose:
                    print("      %s: 原地址失效，按序列号改连 %s" % (self.name, alt))
                self.resource = alt
                self.inst = rm.open_resource(self.resource)
            else:
                raise DMMError("%s: 打不开 %s (%s)" % (self.name, self.resource, e))

        self.inst.timeout = int(self.timeout * 1000)
        self.inst.write_termination = "\n"
        self.inst.read_termination = "\n"
        self.idn = self.inst.query("*IDN?").strip()
        return self.idn

    def close(self):
        if self.inst:
            try:
                self.inst.close()
            except Exception:
                pass
            self.inst = None

    # ---- 配置 ----
    def configure(self, meas_func=None, nplc=None, autozero=None,
                  autorange=None, rng=None, verbose=True):
        """按配置设置功能/量程/NPLC/自动调零。只改明确给出的项。

        ★ 量程策略（重要）：
          · autorange=True  → 开启自动量程，**不写** :SENS:RANG（锁死量程会超量程报错）
          · autorange=False 且给了 rng → 才写固定量程（谨慎使用）
          · autorange=None  → 量程不动（保持表原来的设置）
        """
        if meas_func:
            self.inst.write(":SENS:FUNC \"%s\"" % meas_func)

        f = self.current_func()
        key = "VOLT" if "VOLT" in f.upper() else "CURR"

        if autorange is True:
            self.inst.write(":SENS:%s:RANG:AUTO 1" % key)
        elif autorange is False and rng is not None:
            self.inst.write(":SENS:%s:RANG:AUTO 0" % key)
            self.inst.write(":SENS:%s:RANG %g" % (key, float(rng)))
        elif autorange is False and rng is None:
            self.inst.write(":SENS:%s:RANG:AUTO 0" % key)

        if nplc is not None:
            for k in ("VOLT", "CURR"):
                try:
                    self.inst.write(":SENS:%s:NPLC %g" % (k, float(nplc)))
                except Exception:
                    pass
        if autozero is not None:
            for k in ("VOLT", "CURR"):
                try:
                    self.inst.write(":SENS:%s:AZER %d" % (k, 1 if autozero else 0))
                except Exception:
                    pass

        self._refresh_range()
        if verbose:
            print("      %s: 已配置 %s" % (self.name, self.summary()))

    def _refresh_range(self):
        """读回当前量程模式与量程值。"""
        try:
            f = self.current_func()
            key = "VOLT" if "VOLT" in f.upper() else "CURR"
            self.range_auto = self.inst.query(":SENS:%s:RANG:AUTO?" % key).strip()
            self.range_now = self.inst.query(":SENS:%s:RANG?" % key).strip()
        except Exception:
            self.range_auto = "?"
            self.range_now = "?"

    def warmup(self, n=3, verbose=True):
        """连续读 n 次，让**自动量程稳定下来**。

        为什么要预热：自动量程在第一次测量时可能需要切换档位（耗时更长），
        预热后量程就稳定了，正式采集时每轮读数都在正常耗时内。
        返回 (读数列表, 每次耗时毫秒列表)。
        """
        vals, costs = [], []
        for _ in range(max(0, int(n))):
            t0 = time.perf_counter()
            try:
                vals.append(self.read())
            except DMMError:
                vals.append(None)
            costs.append((time.perf_counter() - t0) * 1000.0)
        self._refresh_range()
        if verbose and vals:
            ok = [v for v in vals if v is not None]
            print("      %s: 预热 %d 次，量程=%s(%s)，单次 %.0f~%.0f ms"
                  % (self.name, len(vals),
                     self.range_now, self.range_mode_cn(),
                     min(costs), max(costs)))
        return vals, costs

    def range_mode_cn(self):
        """量程模式的中文描述。"""
        return "自动" if self.range_auto.strip() in ("1", "+1", "ON") else "固定"

    def current_func(self):
        try:
            return self.inst.query(":SENS:FUNC?").strip()
        except Exception:
            return "?"

    def summary(self):
        f = self.current_func()
        self._refresh_range()
        return "func=%s 量程=%s(%s)" % (f, self.range_now, self.range_mode_cn())

    # ---- 读数 ----
    def read(self):
        """触发一次测量并返回浮点值。失败抛 DMMError。"""
        try:
            s = self.inst.query(":READ?").strip()
            return float(s)
        except ValueError:
            raise DMMError("%s: 读数无法解析 (%r)" % (self.name, s))
        except Exception as e:
            raise DMMError("%s: 读数失败 (%s)" % (self.name, str(e)[:80]))

    @property
    def is_open(self):
        return self.inst is not None


# ============================================================================
#  自测
# ============================================================================
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    print("== 串口列表 ==")
    for dev, desc, vid, pid in Arduino.list_ports():
        print("   %-7s %-30s vid=%s" % (dev, desc[:28], ("0x%04X" % vid) if vid else "-"))

    print("\n== 探测 Arduino ==")
    port = Arduino.auto_detect()
    print("   结果:", port)

    print("\n== 两台 DMM ==")
    try:
        rm, res = DMM.list_resources()
        for r in res:
            print("   ", r)
    except Exception as e:
        print("   ", e)
