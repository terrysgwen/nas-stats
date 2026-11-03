"""本机资源 monitor：直接读宿主 /proc 与 /sys（容器内未启用 lxcfs 即为整机口径）。"""
import asyncio
import glob
import os

from .base import BaseMonitor

_CPU_TEMP_TYPES = ("x86_pkg_temp", "coretemp", "k10temp", "zenpower", "cpu_thermal")


def _read_cpu_times():
    with open("/proc/stat", encoding="utf-8") as f:
        parts = f.readline().split()[1:]
    v = [int(x) for x in parts]
    idle = v[3] + (v[4] if len(v) > 4 else 0)
    return sum(v), idle


async def _cpu_pct(sample_s: float = 0.3) -> float:
    t1, i1 = _read_cpu_times()
    await asyncio.sleep(sample_s)
    t2, i2 = _read_cpu_times()
    dt, di = t2 - t1, i2 - i1
    return round((1 - di / dt) * 100, 1) if dt > 0 else 0.0


def _mem() -> dict:
    info = {}
    with open("/proc/meminfo", encoding="utf-8") as f:
        for line in f:
            key, _, rest = line.partition(":")
            if rest:
                info[key] = int(rest.split()[0])
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    used = total - avail
    return {
        "mem_pct": round(used / total * 100, 1) if total > 0 else 0.0,
        "mem_used_gb": round(used / 1048576, 1),
        "mem_total_gb": round(total / 1048576, 1),
    }


def _cpu_temp():
    try:
        for zone in glob.glob("/sys/class/thermal/thermal_zone*"):
            type_file = os.path.join(zone, "type")
            temp_file = os.path.join(zone, "temp")
            if os.path.isfile(type_file) and os.path.isfile(temp_file):
                with open(type_file, encoding="utf-8") as f:
                    if f.read().strip() in _CPU_TEMP_TYPES:
                        with open(temp_file, encoding="utf-8") as g:
                            return round(int(g.read().strip()) / 1000, 1)
        for hw in glob.glob("/sys/class/hwmon/hwmon*"):
            name_file = os.path.join(hw, "name")
            temp_file = os.path.join(hw, "temp1_input")
            if os.path.isfile(name_file) and os.path.isfile(temp_file):
                with open(name_file, encoding="utf-8") as f:
                    if f.read().strip() in _CPU_TEMP_TYPES:
                        with open(temp_file, encoding="utf-8") as g:
                            return round(int(g.read().strip()) / 1000, 1)
    except Exception:
        pass
    return None


class HostMonitor(BaseMonitor):
    display_name = "本机 (CPU/内存/温度)"

    async def fetch(self) -> dict:
        try:
            if not os.path.exists("/proc/stat"):
                # Windows 或非 Linux 本地开发环境兼容兜底
                try:
                    import psutil
                    cpu = psutil.cpu_percent(interval=0.3)
                    vm = psutil.virtual_memory()
                    return {
                        "enabled": True,
                        "status": "ok",
                        "cpu_pct": cpu,
                        "cpu_temp": None,
                        "load": [round(cpu / 10, 2), round(cpu / 12, 2), round(cpu / 15, 2)],
                        "mem_pct": vm.percent,
                        "mem_used_gb": round(vm.used / (1024**3), 1),
                        "mem_total_gb": round(vm.total / (1024**3), 1),
                    }
                except ImportError:
                    return {
                        "enabled": True,
                        "status": "ok",
                        "cpu_pct": 0.0,
                        "cpu_temp": None,
                        "load": [0.0, 0.0, 0.0],
                        "mem_pct": 0.0,
                        "mem_used_gb": 0.0,
                        "mem_total_gb": 0.0,
                    }

            load = [0.0, 0.0, 0.0]
            if os.path.exists("/proc/loadavg"):
                with open("/proc/loadavg", encoding="utf-8") as f:
                    load = [float(x) for x in f.read().split()[:3]]

            return {
                "enabled": True,
                "status": "ok",
                "cpu_pct": await _cpu_pct(),
                "cpu_temp": _cpu_temp(),
                "load": load,
                **_mem(),
            }
        except Exception as exc:
            return {"enabled": True, "status": "error", "error": str(exc)}

    async def test_connection(self) -> dict:
        data = await self.fetch()
        return {"ok": data.get("status") == "ok", "data": data}
