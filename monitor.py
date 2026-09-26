#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Real-time Hardware Telemetry & Fan Speed Monitor (v5.1)
Supports AMD, NVIDIA (via nvidia-smi), Intel iGPUs, and Custom Manual Sensors.
(Updated: Added Min/Max/Avg session stats for Edge and Memory temperatures)
"""

# --------------------------------------------------------------------------- #
# Imports
# --------------------------------------------------------------------------- #
import csv
import curses
import glob
import json
import os
import platform
import psutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Tuple, Optional, List

# --------------------------------------------------------------------------- #
# Global variables used by several functions
# --------------------------------------------------------------------------- #
fan_names: Dict[str, str] = {}          # fan_path  → custom name
fan_assignments: Dict[int, str] = {}    # gpu_index → fan key
custom_sensors_config: List[Dict[str, Any]] = []

# --------------------------------------------------------------------------- #
# File names
# --------------------------------------------------------------------------- #
FAN_NAMES_FILE      = "fan_names.json"
CONFIG_FILE         = "monitor_config.json"
LOG_FILE            = "hardware_log.csv"
ASSIGNMENT_FILE     = "fan_assignment.json"
CUSTOM_SENSORS_FILE = "custom_sensors.json"

# --------------------------------------------------------------------------- #
# Update‑interval limits
# --------------------------------------------------------------------------- #
DEFAULT_UPDATE_INTERVAL = 1.0
MIN_UPDATE_INTERVAL    = 0.1
MAX_UPDATE_INTERVAL    = 3600.0

# --------------------------------------------------------------------------- #
# ASCII Sparkline Characters (9 levels)
# --------------------------------------------------------------------------- #
ASCII_BLOCKS = " .:-=+*#%@"

# --------------------------------------------------------------------------- #
# Helpers: text / float
# --------------------------------------------------------------------------- #
def read_text(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None

def read_float(path: str) -> Optional[float]:
    v = read_text(path)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None

def read_int(path: str) -> Optional[int]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None

def fmt_float(v: Optional[float], fmt: str = ".2f") -> str:
    return "n/a" if v is None else f"{v:{fmt}}"

def bytes_to_mib(v: Optional[float]) -> Optional[float]:
    return None if v is None or v < 0 else v / (1024 * 1024)

def clock_to_mhz(v: Optional[float]) -> Optional[float]:
    if v is None or v <= 0:
        return None
    return v / 1_000_000.0 if v >= 10_000_000 else v / 1_000.0

# --------------------------------------------------------------------------- #
# Sparkline Helper
# --------------------------------------------------------------------------- #
def get_sparkline(data: List[float], width: int = 20) -> str:
    if not data:
        return " " * width
    if len(data) > width:
        step = len(data) / width
        sampled = [data[int(i * step)] for i in range(width)]
    else:
        step = len(data) / width
        sampled = [data[int(i * step)] for i in range(width)]

    min_val = min(sampled)
    max_val = max(sampled)
    range_val = max_val - min_val if max_val != min_val else 1.0

    spark = ""
    for val in sampled:
        idx = int(((val - min_val) / range_val) * (len(ASCII_BLOCKS) - 1))
        idx = max(0, min(len(ASCII_BLOCKS) - 1, idx))
        spark += ASCII_BLOCKS[idx]
    return spark

# --------------------------------------------------------------------------- #
# Persistence helpers
# --------------------------------------------------------------------------- #
def load_fan_names() -> Dict[str, str]:
    try:
        with open(FAN_NAMES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}

def save_fan_names(d: Dict[str, str]) -> bool:
    try:
        with open(FAN_NAMES_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=4, sort_keys=True)
        return True
    except OSError:
        return False

def load_config() -> Dict[str, Any]:
    cfg = {"update_interval": DEFAULT_UPDATE_INTERVAL, "logging_enabled": True}
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        if isinstance(s, dict):
            interval = float(s.get("update_interval", DEFAULT_UPDATE_INTERVAL))
            cfg["update_interval"] = max(MIN_UPDATE_INTERVAL, min(MAX_UPDATE_INTERVAL, interval))
            cfg["logging_enabled"] = bool(s.get("logging_enabled", True))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    return cfg

def save_config(interval: float, logging: bool) -> bool:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"update_interval": interval, "logging_enabled": logging}, f, indent=4, sort_keys=True)
        return True
    except OSError:
        return False

def load_fan_assignments() -> Dict[int, str]:
    try:
        with open(ASSIGNMENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {int(k): str(v).strip() for k, v in data.items()}
    except Exception:
        return {}

def save_fan_assignments(d: Dict[int, str]) -> bool:
    try:
        with open(ASSIGNMENT_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=4, sort_keys=True)
        return True
    except Exception:
        return False

def load_custom_sensors() -> List[Dict[str, Any]]:
    try:
        with open(CUSTOM_SENSORS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []

# --------------------------------------------------------------------------- #
# CPU identification helpers
# --------------------------------------------------------------------------- #
def get_cpu_name() -> str:
    try:
        with open("/proc/cpuinfo", "r", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or "CPU"

def get_cpu_cores() -> int:
    return psutil.cpu_count(logical=False) or 0

def get_cpu_threads() -> int:
    return psutil.cpu_count(logical=True) or 0

# --------------------------------------------------------------------------- #
# RAPL helpers
# --------------------------------------------------------------------------- #
def find_cpu_rapl() -> Optional[str]:
    for base in glob.glob("/sys/class/powercap/intel-rapl:*"):
        for candidate in glob.glob(f"{base}/*/energy_uj"):
            return candidate
    return None

# --------------------------------------------------------------------------- #
# Find hardware (AMD, Intel GPUs, sensors, RAPL)
# --------------------------------------------------------------------------- #
def find_hardware() -> Dict[str, Any]:
    hw = {
        "gpus": {},
        "intel_gpus": [],
        "cpu_temp_path": None,
        "cpu_power_path": None,
        "cpu_energy_path": None,
        "cpu_freq_path": None,
        "cpu_cur_freq_path": None,
        "system_fans": [],
    }

    # AMD & Intel GPUs via sysfs
    for p in glob.glob("/sys/class/drm/card*"):
        name = os.path.basename(p)
        if "-" in name:
            continue
        try:
            idx = int(name.replace("card", ""))
        except ValueError:
            continue
        
        vendor = read_text(os.path.join(p, "device/vendor"))
        hwmons = sorted(glob.glob(os.path.join(p, "device/hwmon/hwmon*")))
        if not hwmons:
            continue
        hwmon = hwmons[0]
        
        if vendor == "0x1002": # AMD
            prod = read_text(os.path.join(p, "device/product_name"))
            dev_id = read_text(os.path.join(p, "device/device"))
            gpu_name = prod or (f"AMD GPU ({dev_id})" if dev_id else "AMD GPU")
            fan_path = os.path.join(hwmon, "fan1_input")
            if not os.path.exists(fan_path):
                fan_path = None
            hw["gpus"][idx] = {
                "hwmon": hwmon,
                "name": gpu_name,
                "util_path": os.path.join(p, "device/gpu_busy_percent"),
                "fan_path": fan_path,
            }
        elif vendor == "0x8086": # Intel
            prod = read_text(os.path.join(p, "device/product_name"))
            dev_id = read_text(os.path.join(p, "device/device"))
            gpu_name = prod or f"Intel GPU (0x{dev_id})" if dev_id else "Intel GPU"
            hw["intel_gpus"].append({"path": p, "name": gpu_name, "id": idx})

    # CPU / board sensors
    for p in glob.glob("/sys/class/hwmon/hwmon*"):
        driver = read_text(os.path.join(p, "name"))
        if not driver:
            continue
        if driver in ("k10temp", "zenpower"):
            t_path = os.path.join(p, "temp1_input")
            if os.path.exists(t_path):
                hw["cpu_temp_path"] = t_path
            p_path = os.path.join(p, "power1_input")
            if os.path.exists(p_path):
                hw["cpu_power_path"] = p_path
        for f_path in sorted(glob.glob(os.path.join(p, "fan*_input"))):
            chan = os.path.basename(f_path).replace("_input", "").upper()
            auto = f"{driver.upper()} {chan}"
            label = read_text(os.path.join(p, f"{chan.lower()}_label"))
            if label:
                auto = f"{driver.upper()} {label}"
            hw["system_fans"].append(
                {"key": f_path, "automatic_name": auto}
            )

    # CPU RAPL fallback
    rapl_path = find_cpu_rapl()
    if rapl_path:
        hw["cpu_energy_path"] = rapl_path

    # CPU frequency
    max_path = "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq"
    cur_path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
    hw["cpu_freq_path"] = max_path if os.path.exists(max_path) else None
    hw["cpu_cur_freq_path"] = cur_path if os.path.exists(cur_path) else None

    return hw

# --------------------------------------------------------------------------- #
# NVIDIA Auto-Detection (via nvidia-smi)
# --------------------------------------------------------------------------- #
def poll_nvidia_gpus() -> List[Dict[str, Any]]:
    nvidia_gpus = []
    try:
        cmd = [
            "nvidia-smi", 
            "--query-gpu=index,name,temperature.gpu,temperature.memory,power.draw,utilization.gpu,fan.speed,memory.total,memory.used", 
            "--format=csv,noheader,nounits"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=2)
        if result.returncode != 0:
            return nvidia_gpus
        
        for line in result.stdout.strip().split('\n'):
            if not line.strip(): continue
            parts = [p.strip() for p in line.split(',')]
            if len(parts) < 9: continue
            
            idx, name, temp_gpu, temp_mem, power, util, fan, mem_tot, mem_used = parts
            try:
                nvidia_gpus.append({
                    "id": int(idx) + 100,
                    "name": f"NVIDIA {name}",
                    "util": float(util),
                    "temp_edge": float(temp_gpu),
                    "temp_junction": None,
                    "temp_mem": float(temp_mem) if temp_mem != '[N/A]' else None,
                    "freq": 0.0, 
                    "mem_clock": 0.0,
                    "voltage": 0.0,
                    "power": float(power),
                    "tdp_cap": float(power) * 1.2,
                    "fan_rpm": int(float(fan) * 10) if fan != '[N/A]' else 0,
                    "vram": {"total_mb": float(mem_tot), "used_mb": float(mem_used), "free_mb": float(mem_tot) - float(mem_used)},
                    "type": "nvidia"
                })
            except ValueError: continue
    except Exception: pass
    return nvidia_gpus

# --------------------------------------------------------------------------- #
# Intel GPU Helpers
# --------------------------------------------------------------------------- #
def poll_intel_gpus(intel_gpus_list: List[Dict]) -> List[Dict[str, Any]]:
    stats = []
    for gpu in intel_gpus_list:
        p = gpu["path"]
        freq_path = os.path.join(p, "gt_cur_freq_mhz")
        freq = read_float(freq_path)
        
        stats.append({
            "id": gpu["id"] + 200,
            "name": gpu["name"],
            "util": 0.0, 
            "temp_edge": None, 
            "temp_junction": None,
            "temp_mem": None,
            "freq": freq if freq else 0.0,
            "mem_clock": 0.0,
            "voltage": 0.0,
            "power": 0.0, 
            "tdp_cap": 0.0,
            "fan_rpm": 0,
            "vram": {"total_mb": 0, "used_mb": 0, "free_mb": 0},
            "type": "intel"
        })
    return stats

# --------------------------------------------------------------------------- #
# GPU helpers (AMD)
# --------------------------------------------------------------------------- #
def gpu_freq(hw_path: str, idx: int) -> Optional[float]:
    return clock_to_mhz(read_float(os.path.join(hw_path, "freq1_input"))) or \
           clock_to_mhz(read_float(f"/sys/class/drm/card{idx}/device/freq1_input"))

def gpu_mem(hw_path: str, idx: int) -> Optional[float]:
    return clock_to_mhz(read_float(os.path.join(hw_path, "freq2_input"))) or \
           clock_to_mhz(read_float(f"/sys/class/drm/card{idx}/device/freq2_input"))

def gpu_power(hw_path: str) -> Optional[float]:
    for f in ("power1_average", "power1_input"):
        v = read_float(os.path.join(hw_path, f))
        if v is not None and v >= 0:
            return v / 1_000_000.0
    return None

def gpu_power_cap(hw_path: str) -> Optional[float]:
    v = read_float(os.path.join(hw_path, "power1_cap"))
    return None if v is None or v <= 0 else v / 1_000_000.0

def gpu_temps(hw_path: str) -> Dict[str, Optional[float]]:
    temps = {"edge": None, "junction": None, "mem": None}
    for i in range(1, 5):
        label_path = os.path.join(hw_path, f"temp{i}_label")
        input_path = os.path.join(hw_path, f"temp{i}_input")
        label = read_text(label_path)
        val = read_float(input_path)
        if val is not None:
            temp_c = val / 1000.0
            if label:
                lbl = label.strip().lower()
                if "edge" in lbl: temps["edge"] = temp_c
                elif "junction" in lbl or "hotspot" in lbl: temps["junction"] = temp_c
                elif "mem" in lbl or "vram" in lbl: temps["mem"] = temp_c
            else:
                if i == 1: temps["edge"] = temp_c
                elif i == 2: temps["junction"] = temp_c
                elif i == 3: temps["mem"] = temp_c
    return temps

def gpu_volt(hw_path: str) -> Optional[float]:
    for p in ("in0_input", "vddc_input", "vddgfx_input", "in1_input"):
        v = read_float(os.path.join(hw_path, p))
        if v is not None and 200 <= v <= 2_500:
            return v
    for p in sorted(glob.glob(os.path.join(hw_path, "in*__input"))):
        v = read_float(p)
        if v is not None and 200 <= v <= 2_500:
            return v
    return None

def gpu_vram(idx: int) -> Dict[str, Optional[float]]:
    d = f"/sys/class/drm/card{idx}/device"
    t = read_float(os.path.join(d, "mem_info_vram_total"))
    u = read_float(os.path.join(d, "mem_info_vram_used"))
    total = bytes_to_mib(t)
    used = bytes_to_mib(u)
    free = None
    if t is not None and u is not None:
        free = bytes_to_mib(max(0, t - u))
    return {"total_mb": total, "used_mb": used, "free_mb": free}

# --------------------------------------------------------------------------- #
# CPU utilisation counters
# --------------------------------------------------------------------------- #
def cpu_util_counters() -> Tuple[float, float]:
    try:
        with open("/proc/stat", "r", encoding="utf-8") as f:
            line = f.readline()
    except OSError:
        return 0.0, 0.0
    parts = line.split()
    if len(parts) < 5 or parts[0] != "cpu":
        return 0.0, 0.0
    try:
        vals = [float(v) for v in parts[1:]]
    except ValueError:
        return 0.0, 0.0
    work = vals[0] + vals[1] + vals[2] + vals[5] + vals[6] + vals[7]
    total = work + vals[3] + vals[4]
    return work, total

# --------------------------------------------------------------------------- #
# CPU power – RAPL‑based wattage
# --------------------------------------------------------------------------- #
def cpu_power(
    hw: Dict[str, Any],
    prev_energy: Optional[int] = None,
    prev_time: Optional[float] = None,
) -> Tuple[float, Optional[int], float]:
    now = time.time()
    cur_energy = read_int(hw["cpu_energy_path"]) if hw["cpu_energy_path"] else None

    if cur_energy is None:
        v = read_float(hw["cpu_power_path"] or "")
        watts = v / 1_000.0 if v is not None else 0.0
        return watts, prev_energy, now

    if prev_energy is not None and prev_time is not None:
        elapsed = now - prev_time
        if elapsed <= 0:
            watts = 0.0
        else:
            delta = cur_energy - prev_energy
            if delta < 0:
                delta += 2 ** 32
            if delta < 0:
                watts = 0.0
            else:
                watts = delta / elapsed / 1_000_000.0
    else:
        watts = 0.0

    return watts, cur_energy, now

# --------------------------------------------------------------------------- #
# POLLING FOR FAN POWER
# --------------------------------------------------------------------------- #
FAN_IDS = {"4", "7"}
RPM_MAX_FULL = 3000
CURRENT_AT_FULL = 0.33
VOLTAGE = 12.0

def find_fan_paths() -> Dict[str, Path]:
    fan_paths: Dict[str, Path] = {}
    for hw_dir in Path("/sys/class/hwmon").iterdir():
        if not hw_dir.is_dir():
            continue
        for fan_file in hw_dir.glob("fan*_input"):
            name = fan_file.name
            if name.startswith("fan"):
                num = name[3:-6]
                if num in FAN_IDS:
                    fan_paths[num] = fan_file.resolve()
                    continue
            label_file = fan_file.with_name(fan_file.stem + "_label")
            if label_file.is_file():
                try:
                    label = label_file.read_text(encoding="utf-8").strip()
                except OSError:
                    continue
                for fid in FAN_IDS:
                    if fid in label:
                        fan_paths[fid] = fan_file.resolve()
    return fan_paths

def estimate_power(rpm: int) -> float:
    current = (rpm / RPM_MAX_FULL) * CURRENT_AT_FULL
    return VOLTAGE * current

# --------------------------------------------------------------------------- #
# POLL ALL SENSORS
# --------------------------------------------------------------------------- #
def poll(
    hw: Dict[str, Any],
    prev_energy: Optional[int],
    prev_time: Optional[float],
) -> Dict[str, Any]:
    stats = {"cpu": None, "gpus": [], "system_fans": [], "memory": None, "custom_sensors": []}

    # CPU
    temp = None
    t_path = hw["cpu_temp_path"]
    if t_path:
        t = read_float(t_path)
        if t is not None:
            temp = t / 1000.0

    pwr, cur_e, cur_t = cpu_power(hw, prev_energy, prev_time)

    freq = None
    cur_path = hw.get("cpu_cur_freq_path")
    if cur_path and os.path.exists(cur_path):
        f = read_float(cur_path)
        if f is not None:
            freq = f / 1000.0
    else:
        max_path = hw.get("cpu_freq_path")
        if max_path and os.path.exists(max_path):
            f = read_float(max_path)
            if f is not None:
                freq = f / 1000.0

    stats["cpu"] = {
        "util": 0.0,
        "temp": temp,
        "freq": freq,
        "power": pwr,
        "tdp_cap": 105.0,
        "energy_counter": cur_e,
        "time_counter": cur_t,
        "name": get_cpu_name(),
        "cores": get_cpu_cores(),
        "threads": get_cpu_threads(),
    }

    # AMD GPUs
    for idx, info in sorted(hw["gpus"].items()):
        hwmon = info["hwmon"]
        util = read_float(info["util_path"]) or 0.0
        fan_rpm = None
        if info["fan_path"]:
            v = read_float(info["fan_path"])
            fan_rpm = int(v) if v is not None else None
        
        temps = gpu_temps(hwmon)
        
        stats["gpus"].append(
            {
                "id": idx,
                "name": info["name"],
                "util": util,
                "temp_edge": temps["edge"],
                "temp_junction": temps["junction"],
                "temp_mem": temps["mem"],
                "freq": gpu_freq(hwmon, idx),
                "mem_clock": gpu_mem(hwmon, idx),
                "voltage": gpu_volt(hwmon),
                "power": gpu_power(hwmon),
                "tdp_cap": gpu_power_cap(hwmon),
                "fan_rpm": fan_rpm,
                "vram": gpu_vram(idx),
                "type": "amd"
            }
        )

    # Intel & NVIDIA GPUs
    stats["gpus"].extend(poll_intel_gpus(hw["intel_gpus"]))
    stats["gpus"].extend(poll_nvidia_gpus())

    # System fans
    for fan in hw["system_fans"]:
        v = read_float(fan["key"])
        if v is None:
            continue
        rpm = int(v)
        stats["system_fans"].append(
            {"key": fan["key"], "automatic_name": fan["automatic_name"], "rpm": rpm}
        )

    # Custom Manual Sensors
    for sensor in custom_sensors_config:
        name = sensor.get("name", "Unknown")
        path = sensor.get("path", "")
        scale = sensor.get("scale", 1)
        unit = sensor.get("unit", "°C")
        val = read_float(path)
        if val is not None:
            stats["custom_sensors"].append({
                "name": name,
                "value": val / scale,
                "unit": unit
            })

    # Memory
    mem = psutil.virtual_memory()
    stats["memory"] = {
        "total_mb": mem.total // (1024 * 1024),
        "free_mb": mem.available // (1024 * 1024),
        "used_percent": mem.percent,
    }

    return stats

# --------------------------------------------------------------------------- #
# History helpers
# --------------------------------------------------------------------------- #
def upd_hist(hist: Dict[str, Dict[str, Any]], ident: str, val: Optional[float]):
    if val is None:
        return None, None, None
    if ident not in hist:
        hist[ident] = {"min": val, "max": val, "sum": val, "cnt": 1}
    else:
        h = hist[ident]
        h["min"] = min(h["min"], val)
        h["max"] = max(h["max"], val)
        h["sum"] += val
        h["cnt"] += 1
    h = hist[ident]
    return h["min"], h["max"], h["sum"] / h["cnt"]

def get_hist(hist: Dict[str, Dict[str, Any]], ident: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    h = hist.get(ident)
    if h is None:
        return None, None, None
    return h["min"], h["max"], h["sum"] / h["cnt"]

def upd_all_hist(hist: Dict[str, Dict[str, Any]], stats: Dict[str, Any]):
    cpu = stats["cpu"]
    if cpu:
        upd_hist(hist, "cpu_power", cpu.get("power"))
        upd_hist(hist, "cpu_temp", cpu.get("temp"))
        upd_hist(hist, "cpu_freq", cpu.get("freq"))
    for g in stats.get("gpus", []):
        gid = g['id']
        upd_hist(hist, f"gpu_{gid}_power", g.get("power"))
        # UPDATED: Now tracking Edge and Mem temps for session stats
        upd_hist(hist, f"gpu_{gid}_temp_edge", g.get("temp_edge"))
        upd_hist(hist, f"gpu_{gid}_temp_junction", g.get("temp_junction"))
        upd_hist(hist, f"gpu_{gid}_temp_mem", g.get("temp_mem"))
        upd_hist(hist, f"gpu_{gid}_voltage", g.get("voltage"))
        upd_hist(hist, f"gpu_{gid}_freq", g.get("freq"))
        upd_hist(hist, f"gpu_{gid}_mem_clock", g.get("mem_clock"))
        upd_hist(hist, f"gpu_{gid}_fan", g.get("fan_rpm"))
    for f in stats.get("system_fans", []):
        upd_hist(hist, f"fan_{f['key']}_rpm", f.get("rpm"))

def reset_stats() -> Tuple[Dict[str, Dict[str, Any]], Optional[int], float]:
    return {}, None, time.time()

# --------------------------------------------------------------------------- #
# Logging helpers
# --------------------------------------------------------------------------- #
def log_open() -> bool:
    if os.path.exists(LOG_FILE):
        return True
    header = [
        "timestamp", "cpu_util_percent", "cpu_temp_c", "cpu_power_w", "cpu_freq_mhz",
        "gpu_id", "gpu_name", "gpu_type", "gpu_util_percent", "gpu_temp_edge_c", "gpu_temp_junction_c", "gpu_temp_mem_c", "gpu_voltage_mv",
        "gpu_power_w", "gpu_power_cap_w", "gpu_fan_rpm", "gpu_core_freq_mhz",
        "gpu_mem_clock_mhz", "gpu_vram_total_mib", "gpu_vram_used_mib", "gpu_vram_free_mib",
        "system_fan_key", "system_fan_name", "system_fan_rpm",
        "memory_total_mib", "memory_free_mib", "memory_used_percent",
    ]
    try:
        with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(header)
        return True
    except OSError:
        return False

def log_stats(stats: Dict[str, Any], fan_names: Dict[str, str]) -> bool:
    if not log_open():
        return False
    ts = datetime.now().astimezone().isoformat(timespec="milliseconds")
    cpu = stats.get("cpu") or {}
    gpus = stats.get("gpus") or [{"id": "", "name": "", "type": "", "util": "", "temp_edge": "", "temp_junction": "", "temp_mem": "", "voltage": "", "power": "", "tdp_cap": "", "fan_rpm": "", "freq": "", "mem_clock": "", "vram": {}}]
    fans = stats.get("system_fans") or [{"key": "", "automatic_name": "", "rpm": ""}]

    rows = []
    for g in gpus:
        vram = g.get("vram") or {}
        for f in fans:
            name = fan_names.get(f["key"], f["automatic_name"]) if f["key"] else ""
            rows.append([
                ts, cpu.get("util", ""), cpu.get("temp", ""), cpu.get("power", ""), cpu.get("freq", ""),
                g["id"], g["name"], g.get("type", ""), g.get("util", ""), g.get("temp_edge", ""), g.get("temp_junction", ""), g.get("temp_mem", ""),
                g.get("voltage", ""), g.get("power", ""), g.get("tdp_cap", ""), g.get("fan_rpm", ""), g.get("freq", ""),
                g.get("mem_clock", ""), vram.get("total_mb", ""), vram.get("used_mb", ""), vram.get("free_mb", ""),
                f["key"], name, f["rpm"],
                stats["memory"]["total_mb"], stats["memory"]["free_mb"], stats["memory"]["used_percent"],
            ])
    try:
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            csv.writer(f).writerows(rows)
        return True
    except OSError:
        return False

# --------------------------------------------------------------------------- #
# Curses helpers & Colors
# --------------------------------------------------------------------------- #
C_NORMAL = 0
C_HEADER = 1
C_GOOD   = 2
C_WARN   = 3
C_CRIT   = 4

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(C_HEADER, curses.COLOR_WHITE, -1)
    curses.init_pair(C_GOOD, curses.COLOR_CYAN, -1)
    curses.init_pair(C_WARN, curses.COLOR_YELLOW, -1)
    curses.init_pair(C_CRIT, curses.COLOR_RED, -1)

def get_temp_color(temp: Optional[float], is_junction: bool = False) -> int:
    if temp is None: return C_NORMAL
    if is_junction:
        if temp >= 100.0: return C_CRIT
        if temp >= 90.0: return C_WARN
    else:
        if temp >= 90.0: return C_CRIT
        if temp >= 85.0: return C_WARN
    return C_GOOD

def get_fan_color(rpm: Optional[int]) -> int:
    if rpm is not None and rpm == 0: return C_WARN
    return C_GOOD

def fmt_session(minv: Optional[float], maxv: Optional[float], avg: Optional[float],
                unit: str = "", dec: int = 2) -> str:
    return (
        "n/a"
        if minv is None or maxv is None or avg is None
        else f"Min:{minv:.{dec}f}{unit} | Max:{maxv:.{dec}f}{unit} | Avg:{avg:.{dec}f}{unit}"
    )

def fan_display_name(fan: Dict[str, Any], fan_names: Dict[str, str]) -> str:
    return fan_names.get(fan["key"], fan["automatic_name"])

# --------------------------------------------------------------------------- #
# Dashboard rendering
# --------------------------------------------------------------------------- #
def draw(scr, stats, hist, fan_names, fan_stats, msg, logging, interval, live_total_power, rolling_data):
    scr.erase()
    max_y, max_x = scr.getmaxyx()
    row = 0

    def write_line(segments: List[Tuple[str, int]]):
        nonlocal row
        if row >= max_y: return
        col = 0
        for text, color in segments:
            s = str(text)
            if col + len(s) > max_x:
                s = s[:max_x-col]
            try:
                attr = curses.color_pair(color)
                scr.addnstr(row, col, s, max_x - col - 1, attr)
            except curses.error:
                pass
            col += len(s)
        row += 1

    def write_plain(text: str, color: int = C_NORMAL):
        write_line([(text, color)])

    write_plain("=" * 80, C_HEADER)
    write_plain(" Real-time Hardware Telemetry & Fan Speed Monitor (v5.1)", C_HEADER)
    write_plain("=" * 80, C_HEADER)
    write_plain("")

    cpu = stats.get("cpu")
    if cpu:
        pmin, pmax, pavg = get_hist(hist, "cpu_power")
        tmin, tmax, tavg = get_hist(hist, "cpu_temp")
        fmin, fmax, favg = get_hist(hist, "cpu_freq")
        
        t_color = get_temp_color(cpu.get("temp"), is_junction=False)
        spark_util = get_sparkline(rolling_data.get('cpu_util', []), 20)
        spark_temp = get_sparkline(rolling_data.get('cpu_temp', []), 20)

        write_line([(f"  ️  CPU: {cpu.get('name')} ({cpu.get('cores')} cores, {cpu.get('threads')} threads)", C_HEADER)])
        write_line([("   • Utilization:    ", C_NORMAL), (f"{fmt_float(cpu.get('util'), '.1f')}%", C_GOOD), (f" {spark_util}", C_GOOD)])
        write_line([("   • Temperature:    ", C_NORMAL), (f"{fmt_float(cpu.get('temp'), '.1f')}°C", t_color), (f" {spark_temp}", t_color)])
        write_plain(f"   • Temp Session:   {fmt_session(tmin, tmax, tavg, '°C', 1)}")
        write_plain(f"   • Frequency:      {fmt_float(cpu.get('freq'))} MHz")
        write_plain(f"   • Freq Session:   {fmt_session(fmin, fmax, favg, ' MHz', 2)}")
        write_plain(f"   • Current Power:  {fmt_float(cpu.get('power'))} W / {fmt_float(cpu.get('tdp_cap'), '.1f')} W")
        write_plain(f"   • Power Session:  {fmt_session(pmin, pmax, pavg, ' W', 2)}")
        write_plain("")

    for g in stats.get("gpus", []):
        gid = g["id"]
        pmin, pmax, pavg = get_hist(hist, f"gpu_{gid}_power")
        
        # UPDATED: Fetching session stats for Edge, Junction, and Mem
        te_min, te_max, te_avg = get_hist(hist, f"gpu_{gid}_temp_edge")
        tj_min, tj_max, tj_avg = get_hist(hist, f"gpu_{gid}_temp_junction")
        tm_min, tm_max, tm_avg = get_hist(hist, f"gpu_{gid}_temp_mem")
        
        vmin, vmax, varg = get_hist(hist, f"gpu_{gid}_voltage")
        fmin, fmax, favg = get_hist(hist, f"gpu_{gid}_freq")
        mmin, mmax, mavg = get_hist(hist, f"gpu_{gid}_mem_clock")

        volt = g.get("voltage")
        volt_txt = (f"{volt:.0f} mV ({volt / 1000:.3f} V)" if volt is not None else "n/a")

        fan_rpm = g.get("fan_rpm")
        fan_lbl = "(unassigned)" if fan_rpm is None or fan_rpm == 0 else f"{fan_rpm} RPM"
        fan_color = get_fan_color(fan_rpm)

        if fan_rpm is None or fan_rpm == 0:
            key = fan_assignments.get(gid)
            if key:
                fan_obj = next((f for f in stats["system_fans"] if f["key"] == key), None)
                if fan_obj:
                    name = fan_display_name(fan_obj, fan_names)
                    fan_lbl = f"{name} ({fan_obj['rpm']} RPM)"
                    fan_color = get_fan_color(fan_obj['rpm'])

        vram = g.get("vram") or {}
        
        t_junc = g.get("temp_junction")
        t_color = get_temp_color(t_junc, is_junction=True)
        spark_junc = get_sparkline(rolling_data.get(f'gpu_{gid}_temp_junction', []), 20)
        gpu_type = g.get("type", "unknown").upper()

        write_line([(f"   ⚡ {g['name']} [Card {gid}] ({gpu_type})", C_HEADER)])
        write_line([("   • Core Load:      ", C_NORMAL), (f"{fmt_float(g.get('util'), '.0f')}%", C_GOOD), (f" {get_sparkline(rolling_data.get(f'gpu_{gid}_util', []), 20)}", C_GOOD)])
        
        if g.get("temp_edge") is not None:
            write_line([("   • Edge Temp:      ", C_NORMAL), (f"{fmt_float(g.get('temp_edge'), '.1f')}°C", C_GOOD)])
            write_plain(f"   • Edge Session:   {fmt_session(te_min, te_max, te_avg, '°C', 1)}") # ADDED
            
        if t_junc is not None:
            write_line([("   • Junction Temp:  ", C_NORMAL), (f"{fmt_float(t_junc, '.1f')}°C", t_color), (f" {spark_junc}", t_color)])
            write_plain(f"   • Junc Session:   {fmt_session(tj_min, tj_max, tj_avg, '°C', 1)}")
            
        if g.get("temp_mem") is not None:
            write_line([("   • Memory Temp:    ", C_NORMAL), (f"{fmt_float(g.get('temp_mem'), '.1f')}°C", C_GOOD)])
            write_plain(f"   • Mem Session:    {fmt_session(tm_min, tm_max, tm_avg, '°C', 1)}") # ADDED
        
        write_plain(f"   • Core Frequency: {fmt_float(g.get('freq'))} MHz")
        write_plain(f"   • Core Freq Session: {fmt_session(fmin, fmax, favg, ' MHz', 2)}")
        write_plain(f"   • Mem Clock:      {fmt_float(g.get('mem_clock'))} MHz")
        write_plain(f"   • Mem Clock Session: {fmt_session(mmin, mmax, mavg, ' MHz', 2)}")
        write_plain(f"   • Core Voltage:   {volt_txt}")
        write_plain(f"   • Volt Session:   {fmt_session(vmin, vmax, varg, ' mV', 0)}")
        write_plain(f"   • VRAM:           {fmt_float(vram.get('used_mb'))} / {fmt_float(vram.get('total_mb'))} MiB (Free {fmt_float(vram.get('free_mb'))} MiB)")
        write_line([("   • Fan:            ", C_NORMAL), (fan_lbl, fan_color)])
        write_plain(f"   • Current Power:  {fmt_float(g.get('power'))} W / {fmt_float(g.get('tdp_cap'), '.1f')} W")
        write_plain(f"   • Power Session:  {fmt_session(pmin, pmax, pavg, ' W', 2)}")
        write_plain("")

    # CUSTOM SENSORS SECTION
    custom = stats.get("custom_sensors", [])
    if custom:
        write_line([(" 🛠️  Custom Manual Sensors:", C_HEADER)])
        for s in custom:
            write_plain(f"   • {s['name']}: {s['value']:.1f}{s['unit']}", C_GOOD)
        write_plain("")

    # FAN SECTION
    write_line([(" 🌀  Fan Power (FAN 4 & 7)", C_HEADER)])
    if fan_stats:
        for fid, f in fan_stats.items():
            pmin, pmax, pavg = get_hist(hist, f"fan_{fid}_power")
            f_color = get_fan_color(f['rpm'])
            write_line([
                (f"   • Fan {fid} | RPM:", C_NORMAL), 
                (f"{f['rpm']:<6}", f_color), 
                (f" | Power:{f['power']:.2f} W ", C_NORMAL),
                (fmt_session(pmin, pmax, pavg, ' W', 2), C_GOOD)
            ])
        write_plain("")
        tmin, tmax, tavg = get_hist(hist, "total_power")
        write_line([("  🏁 Total Power: ", C_HEADER), (f"{live_total_power:.2f} W", C_GOOD), (f"  ({fmt_session(tmin, tmax, tavg, ' W', 2)})", C_NORMAL)])
        write_plain("")
    else:
        write_plain("   • No fan data available.")

    write_line([(" 🌀 ✇ Cooling & Chassis Fan Arrays:", C_HEADER)])
    fs = stats.get("system_fans", [])
    if fs:
        for f in fs:
            fmin, fmax, favg = get_hist(hist, f"fan_{f['key']}_rpm")
            name = fan_display_name(f, fan_names)
            sess = (f"Min:{fmin:.0f} Max:{fmax:.0f} Avg:{favg:.0f}" if fmin is not None else "n/a")
            f_color = get_fan_color(f['rpm'])
            write_line([(f"   • {name}: ", C_NORMAL), (f"{f['rpm']} RPM", f_color), (f" ({sess})", C_NORMAL)])
    else:
        write_plain("   • No active system fan telemetry detected.")

    # MEMORY SECTION
    mem = stats.get("memory")
    if mem:
        write_plain("")
        write_line([(" 🟩 🧬  Memory Usage:", C_HEADER)])
        write_plain(f"   • Total: {mem['total_mb']} MiB  |  Free: {mem['free_mb']} MiB  |  Used: {mem['used_percent']:.1f}%", C_GOOD)

    write_plain("")
    write_plain(f" Logging: {'ON' if logging else 'OFF'}")
    write_plain(f" Update interval: {interval:.2f}s")
    write_plain("")
    write_plain("=" * 80, C_HEADER)
    write_plain(" [n] Rename  [a] Assign  [s] Save  [r] Reset  [l] Logging  [i] Interval  [q] Quit", C_HEADER)
    if msg:
        write_line([(" Message: ", C_WARN), (msg, C_NORMAL)])

    scr.refresh()

# --------------------------------------------------------------------------- #
# Interactive screens (Assign, Rename, Interval)
# --------------------------------------------------------------------------- #
def fan_assign_screen(scr):
    scr.erase()
    scr.addstr(0, 0, "Fan Assignments (key: d=Delete, e=Edit, q=Close)")
    for idx, (g, lbl) in enumerate(sorted(fan_assignments.items()), start=1):
        scr.addstr(1 + idx, 0, f"{idx}. GPU {g} => {lbl}")
    scr.refresh()
    curses.curs_set(0)
    curses.noecho()
    curses.cbreak()
    scr.nodelay(False)

    while True:
        key = scr.getch()
        if key == ord("q"): break
        if key == ord("d"):
            scr.addstr(len(fan_assignments) + 2, 0, "Delete GPU # (or Enter to cancel): ".ljust(80))
            scr.refresh()
            curses.echo()
            raw = scr.getstr(len(fan_assignments) + 2, 41, 10).decode("utf-8", "replace").strip()
            curses.noecho()
            if raw.isdigit():
                sel = int(raw) - 1
                if 0 <= sel < len(fan_assignments):
                    g = sorted(fan_assignments.keys())[sel]
                    del fan_assignments[g]
                    scr.addstr(len(fan_assignments) + 3, 0, f"Removed assignment for GPU {g}".ljust(80))
        if key == ord("e"):
            scr.addstr(len(fan_assignments) + 2, 0, "Edit GPU # (or Enter to cancel): ".ljust(80))
            scr.refresh()
            curses.echo()
            raw = scr.getstr(len(fan_assignments) + 2, 41, 10).decode("utf-8", "replace").strip()
            curses.noecho()
            if raw.isdigit():
                sel = int(raw) - 1
                if 0 <= sel < len(fan_assignments):
                    g = sorted(fan_assignments.keys())[sel]
                    scr.addstr(len(fan_assignments) + 3, 0, f"New label for GPU {g}: ".ljust(80))
                    scr.refresh()
                    curses.echo()
                    new = scr.getstr(len(fan_assignments) + 4, 0, 30).decode("utf-8", "replace").strip()
                    curses.noecho()
                    if new:
                        fan_assignments[g] = new
                        scr.addstr(len(fan_assignments) + 5, 0, f"GPU {g} -> {new}".ljust(80))
    scr.erase()
    scr.refresh()

def prompt_interval(scr, current):
    scr.erase()
    scr.addstr(0, 0, f"Current interval: {current:.2f}s")
    scr.addstr(1, 0, "Enter new interval in seconds (Enter to cancel): ")
    scr.refresh()
    curses.echo()
    curses.nodelay(False)
    try:
        raw = scr.getstr(1, 48, 10).decode("utf-8", "replace").strip()
    finally:
        curses.noecho()
        curses.nodelay(True)
    if not raw: return None
    try:
        val = float(raw)
    except ValueError:
        return None
    if val < MIN_UPDATE_INTERVAL or val > MAX_UPDATE_INTERVAL: return None
    return val

def add_assignment(scr, hw, stats):
    global fan_names, fan_assignments
    scr.nodelay(False)
    curses.echo()
    scr.erase()
    scr.addstr(0, 0, "Assign fan to GPU")
    scr.refresh()

    gpu_list = sorted(hw["gpus"].items())
    for i, (gpu_idx, info) in enumerate(gpu_list, start=1):
        cur = fan_assignments.get(gpu_idx, "(unassigned)")
        scr.addstr(1 + i, 0, f"{i}. GPU {gpu_idx} ({info['name']}) => {cur}")

    scr.addstr(len(gpu_list) + 2, 0, "Select GPU # (or press Enter to cancel): ")
    scr.refresh()
    raw = scr.getstr(len(gpu_list) + 2, 45, 10).decode("utf-8", "replace").strip()
    if not raw:
        scr.nodelay(True); curses.noecho(); return
    if not raw.isdigit():
        scr.addstr(len(gpu_list) + 3, 0, "Invalid number".ljust(80))
        scr.refresh(); time.sleep(1.5)
        scr.nodelay(True); curses.noecho(); return

    sel = int(raw) - 1
    if sel < 0 or sel >= len(gpu_list):
        scr.nodelay(True); curses.noecho(); return

    gpu_idx, info = gpu_list[sel]
    fan_list = stats.get("system_fans", [])
    if not fan_list:
        scr.addstr(len(gpu_list) + 3, 0, "No system fans detected".ljust(80))
        scr.refresh(); time.sleep(1.5)
        scr.nodelay(True); curses.noecho(); return

    scr.erase()
    scr.addstr(0, 0, f"Assign fan to GPU {gpu_idx} ({info['name']})")
    for i, fan in enumerate(fan_list, start=1):
        name = fan_display_name(fan, fan_names)
        scr.addstr(1 + i, 0, f"{i}. {name} ({fan['rpm']} RPM)")

    scr.addstr(len(fan_list) + 2, 0, "Select fan # (or press Enter to cancel): ")
    scr.refresh()
    raw = scr.getstr(len(fan_list) + 2, 45, 10).decode("utf-8", "replace").strip()
    if not raw:
        scr.nodelay(True); curses.noecho(); return
    if not raw.isdigit():
        scr.addstr(len(fan_list) + 3, 0, "Invalid number".ljust(80))
        scr.refresh(); time.sleep(1.5)
        scr.nodelay(True); curses.noecho(); return

    sel_fan = int(raw) - 1
    if sel_fan < 0 or sel_fan >= len(fan_list):
        scr.nodelay(True); curses.noecho(); return

    fan = fan_list[sel_fan]
    fan_assignments[gpu_idx] = fan["key"]

    scr.erase()
    name = fan_display_name(fan, fan_names)
    scr.addstr(0, 0, f"Assigned fan '{name}' (key={fan['key']}) to GPU {gpu_idx}.")
    scr.addstr(1, 0, "Press any key to return.")
    scr.refresh()
    scr.getch()
    scr.nodelay(True)
    curses.noecho()

def rename_fan(scr, stats, fan_names):
    fans = stats.get("system_fans", [])
    if not fans: return "No fans to rename."
    scr.erase()
    scr.addstr(0, 0, "Select fan to rename")
    for i, f in enumerate(fans, 1):
        name = fan_display_name(f, fan_names)
        scr.addstr(1 + i, 0, f"{i}. {name} ({f['rpm']} RPM)")
    scr.addstr(len(fans) + 2, 0, "Fan # (or press Enter to cancel): ")
    scr.refresh()
    curses.echo()
    raw = scr.getstr(len(fans) + 2, 45, 10).decode("utf-8", "replace").strip()
    curses.noecho()
    if not raw: return "Rename cancelled."
    if not raw.isdigit(): return "Invalid number."
    sel = int(raw) - 1
    if 0 <= sel < len(fans):
        fan = fans[sel]
        scr.addstr(len(fans) + 3, 0, f"New name for fan {fan['key']}: ".ljust(80))
        scr.refresh()
        curses.echo()
        new = scr.getstr(len(fans) + 4, 0, 30).decode("utf-8", "replace").strip()
        curses.noecho()
        if new:
            fan_names[fan["key"]] = new
            return f"Fan {fan['key']} renamed."
    return "Rename cancelled."

# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #
def main_loop(scr):
    init_colors()
    curses.curs_set(0)
    scr.nodelay(True)
    scr.keypad(True)

    hw = find_hardware()

    global fan_names, fan_assignments, custom_sensors_config
    fan_names = load_fan_names()
    fan_assignments = load_fan_assignments()
    custom_sensors_config = load_custom_sensors()

    cfg = load_config()
    interval, logging = cfg["update_interval"], cfg["logging_enabled"]

    hist: Dict[str, Dict[str, Any]] = {}
    rolling_data: Dict[str, List[float]] = {}
    prev_energy: Optional[int] = None
    prev_time: float = time.time()

    prev_w, prev_tst = cpu_util_counters()
    message = f"Logging {'ON' if logging else 'OFF'}"

    def update_rolling(key: str, val: Optional[float], max_len: int = 40):
        if key not in rolling_data:
            rolling_data[key] = []
        if val is not None:
            rolling_data[key].append(val)
            if len(rolling_data[key]) > max_len:
                rolling_data[key].pop(0)

    while True:
        loop_start = time.monotonic()

        stats = poll(hw, prev_energy, prev_time)
        cpu = stats["cpu"]
        if cpu:
            prev_energy = cpu["energy_counter"]
            prev_time = cpu["time_counter"]

        work, total = cpu_util_counters()
        if total > prev_tst:
            util = (work - prev_w) / (total - prev_tst) * 100.0
        else:
            util = 0.0
        prev_w, prev_tst = work, total
        if cpu:
            cpu["util"] = max(0.0, min(100.0, util))

        if cpu:
            update_rolling('cpu_temp', cpu.get('temp'))
            update_rolling('cpu_util', cpu.get('util'))
        
        for g in stats.get("gpus", []):
            update_rolling(f'gpu_{g["id"]}_temp_junction', g.get('temp_junction'))
            update_rolling(f'gpu_{g["id"]}_util', g.get('util'))

        # ---- FAN POWER SECTION ------------------------------------
        fan_paths = find_fan_paths()
        fan_stats: Dict[str, Dict[str, Any]] = {}
        for fid, path in fan_paths.items():
            rpm = read_int(path) or 0
            power = estimate_power(rpm)
            fan_stats[fid] = {"rpm": rpm, "power": power}
            upd_hist(hist, f"fan_{fid}_power", power)

        # ---- TOTAL POWER (CPU + GPUs + FAN4/7) -------------------
        total_power = 0.0
        if cpu and cpu.get("power") is not None:
            total_power += cpu.get("power")
        for g in stats.get("gpus", []):
            if g.get("power") is not None:
                total_power += g.get("power")
        total_power += sum(f["power"] for f in fan_stats.values())
        
        upd_hist(hist, "total_power", total_power)
        upd_all_hist(hist, stats)
        
        if logging:
            log_stats(stats, fan_names)

        draw(scr, stats, hist, fan_names, fan_stats, message, logging, interval, total_power, rolling_data)

        try:
            key = scr.getch()
        except curses.error:
            key = -1

        if key in (ord("q"), ord("Q")):
            break
        if key in (ord("r"), ord("R")):
            hist, prev_energy, prev_time = reset_stats()
            rolling_data.clear()
            message = "All statistics reset."
        if key in (ord("n"), ord("N")):
            message = rename_fan(scr, stats, fan_names)
        if key in (ord("s"), ord("S")):
            ns = save_fan_names(fan_names)
            cs = save_config(interval, logging)
            as_ = save_fan_assignments(fan_assignments)
            message = "Names, settings, and assignments saved." if ns and cs and as_ else "Failed to save something."
        if key in (ord("l"), ord("L")):
            logging = not logging
            message = "Logging enabled." if logging else "Logging disabled."
        if key in (ord("i"), ord("I")):
            new_int = prompt_interval(scr, interval)
            if new_int is not None:
                interval = new_int
                message = f"Interval set to {interval:.2f}s"
        if key in (ord("a"), ord("A")):
            add_assignment(scr, hw, stats)

        elapsed = time.monotonic() - loop_start
        try:
            time.sleep(max(0.0, interval - elapsed))
        except KeyboardInterrupt:
            break

    save_fan_names(fan_names)
    save_config(interval, logging)
    save_fan_assignments(fan_assignments)

if __name__ == "__main__":
    curses.wrapper(main_loop)
