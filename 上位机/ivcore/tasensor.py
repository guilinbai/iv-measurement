# -*- coding: utf-8 -*-
"""
TA 系列温度采集器（TA612，USB-串口 9600 8N1）
================================================================================
协议（来自《TA系列通讯协议.docx》），**已用真机 TA612 V3.50 验证通过**：

  上位机 → 设备（指令帧）
    获取型号版本/停止发送 : AA 55 00 03 02
    开始实时数据          : AA 55 01 03 03
    开始发送记录数据      : AA 55 02 03 04

  设备 → 上位机（TA612 实时数据帧）
    55 AA | 01 | 帧长 | ch1_lo ch1_hi | ch2 | ch3 | ch4 | Sum
      · 帧长 = "不包括帧头"的字节数（示例 0x0B = 11 → 整帧 2+11 = 13 字节）
      · 通道数据 16bit 低字节在前，**温度 = 原始值 / 10**（单位 ℃）
      · Sum = 从帧头开始累加，取低 8 位
    实测示例：AA 55 01 03 03 → 55 AA 01 0B 0B 01 0B 01 60 6D 60 6D BD
      ch1 = 0x010B = 267 → 26.7 ℃    ch2 = 26.7 ℃
      ch3/ch4 = 0x6D60 = 28000 → **未接传感器的标志值**（应过滤掉）

  型号帧：55 AA 00 07 64 02 5E 01 CB → 型号 0x0264=612(TA612)，版本 0x015E/100 = V3.50
  型号码：TA612=0x264  TA622=0x26E  TA632=0x277  TA642=0x282  TA652=0x28C
"""

import time

CMD_INFO = bytes([0xAA, 0x55, 0x00, 0x03, 0x02])
CMD_REALTIME = bytes([0xAA, 0x55, 0x01, 0x03, 0x03])
CMD_RECORDS = bytes([0xAA, 0x55, 0x02, 0x03, 0x04])

MODELS = {0x0264: "TA612", 0x026E: "TA622", 0x0277: "TA632",
          0x0282: "TA642", 0x028C: "TA652"}


class TASensorError(Exception):
    pass


def _hex(b):
    return " ".join("%02X" % x for x in b)


class TASensor:
    """TA612 四通道温度采集器。"""

    def __init__(self, port="COM4", baud=9600, timeout=1.0, name="温度采集器"):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.name = name
        self.ser = None
        self.model = "?"
        self.version = 0.0

    # ------------------------------------------------------------------
    def open(self, verbose=False):
        import serial
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout,
                                     bytesize=8, parity="N", stopbits=1)
        except Exception as e:
            raise TASensorError("%s: 打不开 %s (%s)" % (self.name, self.port, e))
        time.sleep(0.2)
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass
        # 问一下型号（同时让设备停止连续发送）
        info = self.read_info(verbose=verbose)
        if info:
            self.model, self.version = info
        return "%s V%.2f" % (self.model, self.version)

    def close(self):
        if self.ser:
            try:
                # 让设备停止发送
                self.ser.write(CMD_INFO)
                self.ser.flush()
            except Exception:
                pass
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    @property
    def is_open(self):
        return self.ser is not None and self.ser.is_open

    # ------------------------------------------------------------------
    def read_frame(self, timeout=None):
        """读一帧：自动找帧头 0x55AA、按帧长取整帧、校验。失败返回 None。"""
        if not self.is_open:
            return None
        tmo = self.timeout if timeout is None else timeout
        deadline = time.monotonic() + tmo
        buf = bytearray()

        while time.monotonic() < deadline:
            chunk = self.ser.read(1)
            if not chunk:
                continue
            buf += chunk
            while len(buf) >= 2:
                if not (buf[0] == 0x55 and buf[1] == 0xAA):
                    buf.pop(0)                  # 不是帧头，丢一个字节继续找
                    continue
                if len(buf) < 4:
                    break                       # 还没到帧长字节
                total = 2 + buf[3]              # 帧长不含帧头
                if total < 6 or total > 70:
                    buf.pop(0)                  # 帧长不合理，跳过
                    continue
                if len(buf) < total:
                    break                       # 等更多字节
                frame = bytes(buf[:total])
                if (sum(frame[:-1]) & 0xFF) == frame[-1]:
                    return frame
                buf.pop(0)                      # 校验失败 → 跳过这个伪帧头
        return None

    # ------------------------------------------------------------------
    def read_info(self, verbose=False):
        """返回 (型号, 版本号) 或 None。"""
        if not self.is_open:
            return None
        try:
            self.ser.reset_input_buffer()
            self.ser.write(CMD_INFO)
            self.ser.flush()
        except Exception as e:
            raise TASensorError("%s: 发送失败 (%s)" % (self.name, e))

        deadline = time.monotonic() + max(0.6, self.timeout)
        while time.monotonic() < deadline:
            frame = self.read_frame(timeout=max(0.05, deadline - time.monotonic()))
            if not frame:
                break
            if frame[2] != 0x00 or len(frame) < 9:
                continue                        # 不是型号帧
            raw_model = frame[4] | (frame[5] << 8)
            raw_ver = frame[6] | (frame[7] << 8)
            model = MODELS.get(raw_model, "0x%04X" % raw_model)
            ver = raw_ver / 100.0
            if verbose:
                print("      %s: %s V%.2f  (型号帧 %s)"
                      % (self.name, model, ver, _hex(frame)))
            return model, ver
        return None

    # ------------------------------------------------------------------
    def read_temperatures(self, channels=(1, 2), invalid_above=100.0):
        """触发一次实时数据，返回 {通道: 温度℃ 或 None}。

        invalid_above: 超过此值视为无效（未接探头的标志值，实测为 2800.0）
        """
        if not self.is_open:
            raise TASensorError("%s: 串口未打开" % self.name)
        try:
            self.ser.reset_input_buffer()
            self.ser.write(CMD_REALTIME)
            self.ser.flush()
        except Exception as e:
            raise TASensorError("%s: 发送失败 (%s)" % (self.name, e))

        deadline = time.monotonic() + max(0.8, self.timeout * 2)
        while time.monotonic() < deadline:
            frame = self.read_frame(timeout=max(0.05, deadline - time.monotonic()))
            if not frame:
                break
            if frame[2] != 0x01 or len(frame) < 13:
                continue                        # 不是实时数据帧
            out = {}
            for i in range(4):
                lo = frame[4 + i * 2]
                hi = frame[5 + i * 2]
                raw = lo | (hi << 8)
                val = raw / 10.0
                out[i + 1] = None if val > invalid_above else val
            return {ch: out.get(ch) for ch in channels}
        raise TASensorError("%s: 读取超时（没收到有效数据帧）" % self.name)


# ============================================================================
#  自测：直接读真机
# ============================================================================
if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    port = sys.argv[1] if len(sys.argv) > 1 else "COM4"
    s = TASensor(port)
    print("打开 %s ..." % port)
    print("  识别到:", s.open(verbose=True))
    print("  连续读 5 次（ch1/ch2，间隔 0.5 s）:")
    try:
        for k in range(5):
            d = s.read_temperatures(channels=(1, 2, 3, 4))
            print("     #%d  ch1=%s  ch2=%s  ch3=%s  ch4=%s   ΔT=%.1f"
                  % (k + 1, d[1], d[2], d[3], d[4],
                     (d[1] or 0) - (d[2] or 0)))
            time.sleep(0.5)
    finally:
        s.close()
        print("已关闭")
