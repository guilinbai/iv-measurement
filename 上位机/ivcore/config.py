# -*- coding: utf-8 -*-
"""
配置加载
================================================================================
从 config.yaml 读取全部配置，并提供带默认值的访问器。
所有路径都会做展开（支持 ~ 和相对路径）。
"""

import os
import sys

try:
    import yaml
except ImportError:
    yaml = None


def _expand(path):
    if not path:
        return path
    return os.path.abspath(os.path.expanduser(str(path)))


class Config:
    """配置对象。用 cfg['storage']['dir'] 或 cfg.get_path('storage','dir') 访问。"""

    def __init__(self, path="config.yaml"):
        self.path = _expand(path)
        if yaml is None:
            raise RuntimeError("缺少 PyYAML。请运行:  pip install PyYAML")
        if not os.path.isfile(self.path):
            raise FileNotFoundError("找不到配置文件: %s" % self.path)

        with open(self.path, "r", encoding="utf-8") as f:
            self.raw = yaml.safe_load(f) or {}

        # ---- 关键项做规范化 ----
        st = self.raw.setdefault("storage", {})
        st["dir"] = _expand(st.get("dir", "D:/IV_data"))
        st.setdefault("write_csv_realtime", True)
        st.setdefault("verify_on_exit", True)
        st.setdefault("archive_daily", False)

        aq = self.raw.setdefault("acquisition", {})
        aq["interval_s"] = float(aq.get("interval_s", 2.0))
        aq["channels"] = int(aq.get("channels", 4))

        dev = self.raw.setdefault("devices", {})
        for key in ("dmm_voltage", "dmm_current"):
            d = dev.setdefault(key, {})
            d.setdefault("nplc", 1)
            d.setdefault("autozero", True)
            d.setdefault("autorange", False)
            d.setdefault("timeout_s", 5.0)
        dev.setdefault("arduino", {})
        dev["arduino"].setdefault("port", "")
        dev["arduino"].setdefault("baud", 115200)
        dev["arduino"].setdefault("timeout_s", 1.0)
        dev["arduino"].setdefault("heartbeat_s", 5)

        # ---- 温度采集器（TA612）----
        ta = dev.setdefault("tasensor", {})
        ta.setdefault("enabled", True)
        ta.setdefault("port", "COM4")
        ta.setdefault("baud", 9600)
        ta.setdefault("timeout_s", 1.0)
        ta.setdefault("interval_s", 5.0)
        ta.setdefault("channels", [1, 2])
        ta.setdefault("invalid_above", 100.0)

        # ---- 直流电压源（Wanptek MODBUS-RTU）----
        ps = dev.setdefault("powersupply", {})
        ps.setdefault("enabled", True)
        ps.setdefault("port", "COM13")
        ps.setdefault("baud", 2400)
        ps.setdefault("addr", 0)
        ps.setdefault("protocol", "modbus")
        ps.setdefault("settle_s", 2.0)
        ps.setdefault("max_voltage", 31.0)
        ps.setdefault("timeout_s", 0.5)
        ps.setdefault("off_on_exit", False)

        tm = self.raw.setdefault("timing", {})
        tm.setdefault("t_open_ms", 30)
        tm.setdefault("t_settle_ms", 80)
        tm.setdefault("t_window_ms", 50)
        tm.setdefault("t_off_ms", 20)
        tm.setdefault("switch_overhead_ms", 40)

        ui = self.raw.setdefault("ui", {})
        ui.setdefault("refresh_hz", 3)
        ui.setdefault("default_view", "1h")
        ui.setdefault("downsample_buckets", 1600)

    # ---- 便捷访问 ----
    def __getitem__(self, k):
        return self.raw[k]

    def get(self, *keys, **kw):
        node = self.raw
        for k in keys:
            if not isinstance(node, dict) or k not in node:
                return kw.get("default")
            node = node[k]
        return node

    # ---- 派生量 ----
    def per_measure_ms(self):
        """单次测量的微时序总耗时（毫秒），与固件里的参数对应。"""
        t = self.raw["timing"]
        return (t["t_open_ms"] + t["t_settle_ms"] + t["t_window_ms"]
                + t["t_off_ms"] + 10)

    def round_cost_s(self, nch=None):
        """一轮（4 路 × V+I）预计耗时（秒）。"""
        nch = nch or self.raw["acquisition"]["channels"]
        per = self.per_measure_ms() / 1000.0
        switch = self.raw["timing"]["switch_overhead_ms"] / 1000.0
        return per * nch * 2 + switch

    def min_interval_s(self, margin=1.2):
        """建议的最小采样间隔（含安全余量）。"""
        return self.round_cost_s() * margin

    def interval_ok(self, interval=None, margin=1.2):
        iv = interval if interval is not None else self.raw["acquisition"]["interval_s"]
        return iv >= self.min_interval_s(margin), iv

    def dump_summary(self):
        """打印一份配置摘要。"""
        aq, st = self.raw["acquisition"], self.raw["storage"]
        dv = self.raw["devices"]
        lines = [
            "配置摘要 (%s)" % os.path.basename(self.path),
            "  存储目录    : %s" % st["dir"],
            "  实时写 CSV  : %s" % ("是" if st["write_csv_realtime"] else "否"),
            "  采样间隔    : %.2f s   (一轮预计 %.2f s，最小建议 %.2f s)"
            % (aq["interval_s"], self.round_cost_s(), self.min_interval_s()),
            "  通道数      : %d   (每轮 %d 项测量)" % (aq["channels"], aq["channels"] * 2),
            "  电压表      : %s" % dv["dmm_voltage"].get("resource", "(未配置)"),
            "  电流表      : %s" % dv["dmm_current"].get("resource", "(未配置)"),
            "  Arduino     : %s" % (dv["arduino"].get("port") or "(自动探测)"),
            "  温度采集器  : %s"
            % ("%s @ %s，每 %.0f s 采一次，通道 %s"
               % ("TA 系列", dv["tasensor"]["port"], dv["tasensor"]["interval_s"],
                  dv["tasensor"]["channels"])
               if dv["tasensor"]["enabled"] else "未启用"),
            "  电压源      : %s"
            % ("%s @ %d bps 地址%d，协议 %s，上限 %.1f V"
               % (dv["powersupply"]["port"], dv["powersupply"]["baud"],
                  dv["powersupply"]["addr"], dv["powersupply"]["protocol"],
                  dv["powersupply"]["max_voltage"])
               if dv["powersupply"]["enabled"] else "未启用"),
        ]
        return "\n".join(lines)


def load(path="config.yaml"):
    return Config(path)


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    cfg = Config(os.path.join(os.path.dirname(here), "config.yaml"))
    print(cfg.dump_summary())
    ok, iv = cfg.interval_ok()
    print("\n  间隔检查    : %s"
          % ("[OK] 可行" if ok
             else "[!] 余量不足 20%%（间隔 %.2f s / 一轮 %.2f s）"
                  % (iv, cfg.round_cost_s())))
