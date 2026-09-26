#!/usr/bin/env python3
"""AIO LCD dashboard — GPU focus, no temp duplication with case display."""

import argparse
import os
import signal
import sys
import tempfile
import time
from ctypes import CDLL
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont
from pathlib import Path

# Set LD_LIBRARY_PATH only for PyNVML init, then unset so Qt6 works.
# (PyNVML needs /usr/lib64; PySide6 QtRenderer fails when
#  LD_LIBRARY_PATH points there because of Qt_6.11_PRIVATE_API mismatch.)
CDLL("/usr/lib64/libnvidia-ml.so.1")  # preload the handle
os.environ["LD_LIBRARY_PATH"] = "/usr/lib64"
import pynvml
os.environ.pop("LD_LIBRARY_PATH", None)  # clear before any Qt import

# Import TRCC API after clearing LD_LIBRARY_PATH (TRCC uses Qt for rendering)
from trcc._boot import trcc as _get_trcc_app
from trcc.core.commands.device import SendImage as _SendImage

W, H = 480, 480

BG = (8, 8, 14)
WHITE = (220, 220, 230)
DIM = (110, 110, 130)
GREEN = (0, 200, 80)
AMBER = (255, 180, 0)
RED = (255, 60, 60)
CYAN = (0, 200, 200)
BLUE = (60, 140, 255)
PURPLE = (180, 80, 255)
TEAL = (0, 200, 160)
YELLOW = (255, 220, 60)
ORANGE = (255, 140, 0)
BAR_BG = (28, 28, 40)
BORDER = (45, 45, 65)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Whole-PC power limit (watts) for the "X / Y W" readout. The PSU's max
# rating is a spec-sheet number the OS cannot discover (SMBus-telemetry
# PSUs report live draw, not rated watts), so the operator sets it per
# machine via PC_POWER_LIMIT_W (passed through by launch-dashboard.sh /
# the local systemd unit). Unset -> show wattage only, no "/ limit".
PC_LIMIT = None
_pc_limit_env = os.environ.get("PC_POWER_LIMIT_W", "").strip()
if _pc_limit_env:
    try:
        PC_LIMIT = int(_pc_limit_env)
    except ValueError:
        PC_LIMIT = None

# --- CPU power via zenergy (whole-socket energy accumulator, microjoules) ---
# Esocket0 accumulates µJ for the entire CPU socket. Diffing it between
# samples gives CPU package watts (verified: raw/1e6/dt matches the
# per-core Ecore sum). hwmon index is not stable, so resolve by name.
_ZE_INPUT = None
_ZE_LAST = None  # (raw_value, time)


def _zenergy_socket_file():
    """Find the zenergy hwmon Esocket0 _input path (cached)."""
    global _ZE_INPUT
    if _ZE_INPUT:
        return _ZE_INPUT
    for h in sorted(Path("/sys/class/hwmon").glob("hwmon*")):
        try:
            if h.joinpath("name").read_text().strip() != "zenergy":
                continue
            for f in h.iterdir():
                if f.name.endswith("_label"):
                    try:
                        if f.read_text().strip() == "Esocket0":
                            _ZE_INPUT = str(h / (f.name[:-len("_label")] + "_input"))
                            return _ZE_INPUT
                    except OSError:
                        pass
        except OSError:
            pass
    return None


def cpu_power_w():
    """CPU package power in watts from the zenergy Esocket0 diff."""
    global _ZE_LAST
    p = _zenergy_socket_file()
    if not p:
        return None
    try:
        raw = int(Path(p).read_text())
    except (OSError, ValueError):
        return None
    now = time.monotonic()
    last = _ZE_LAST
    _ZE_LAST = (raw, now)
    if last is None:
        return None
    d_raw = raw - last[0]
    if d_raw <= 0:
        return 0.0  # counter stall / wrap guard
    dt = now - last[1]
    if dt <= 0:
        return 0.0
    return d_raw / 1e6 / dt  # µJ -> J (/1e6) then /s = W

HISTORY_LEN = 60
hist_gpu = [0] * HISTORY_LEN
hist_vram = [0] * HISTORY_LEN

def push(val_a, val_b):
    hist_gpu.pop(0); hist_gpu.append(val_a)
    hist_vram.pop(0); hist_vram.append(val_b)

def sparkline(draw, x, y, w, h, buf_a, col_a, buf_b=None, col_b=None):
    draw.rectangle([x, y, x + w, y + h], fill=BAR_BG)
    for pct in (25, 50, 75):
        gy = y + int(h * (100 - pct) / 100)
        draw.line([(x, gy), (x + w, gy)], fill=BORDER, width=1)
    n = len(buf_a)
    step = w / (n - 1) if n > 1 else w
    pts_a = [(x + int(i * step), y + h - int(h * v / 100)) for i, v in enumerate(buf_a)]
    draw.line(pts_a, fill=col_a, width=2)
    if buf_b and col_b:
        pts_b = [(x + int(i * step), y + h - int(h * v / 100)) for i, v in enumerate(buf_b)]
        draw.line(pts_b, fill=col_b, width=2)

running = True
def shutdown(sig, frame):
    global running
    running = False
signal.signal(signal.SIGTERM, shutdown)
signal.signal(signal.SIGINT, shutdown)

def c(v):
    return GREEN if v < 50 else AMBER if v < 80 else RED

def font(sz):
    for p in ["/usr/local/share/fonts/fira-code/FiraCode-Medium.ttf",
              "/usr/share/fonts/fira-code/FiraCode-Medium.ttf",
              "/usr/share/fonts/TTF/FiraCode-Medium.ttf"]:
        try:
            return ImageFont.truetype(p, sz)
        except:
            continue
    try:
        return ImageFont.truetype("DejaVuSansMono.ttf", sz)
    except:
        return ImageFont.load_default()

def bar(draw, x, y, w, h, pct, col):
    draw.rectangle([x, y, x + w, y + h], fill=BAR_BG)
    fill = max(0, int(w * pct / 100))
    if fill:
        draw.rectangle([x, y, x + fill, y + h], fill=col)

_HWCACHE = None
def _hw_string():
    global _HWCACHE
    if _HWCACHE:
        return _HWCACHE
    cpu, gpu = "CPU", "GPU"
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
    except:
        pass
    try:
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(h)
        gpu = name.decode() if isinstance(name, bytes) else name
        pynvml.nvmlShutdown()
    except:
        pass
    for brand in ("AMD", "NVIDIA ", "Intel "):
        if cpu.startswith(brand):
            cpu = cpu[len(brand):]
            break
    # Trim CPU suffixes ("16-Core Processor", etc.)
    for suffix in ("16-Core Processor", "8-Core Processor", "12-Core Processor", "6-Core Processor", "4-Core Processor"):
        if cpu.endswith(suffix):
            cpu = cpu[: -len(suffix)].strip()
            break
    for brand in ("NVIDIA ", "GeForce ", "Quadro ", "Tesla "):
        if gpu.startswith(brand):
            gpu = gpu[len(brand):]
            break
    # Trim GPU suffixes
    for suffix in (" Workstation Edition", " FE", " OC"):
        if gpu.endswith(suffix):
            gpu = gpu[: -len(suffix)]
            break
    _HWCACHE = f"{cpu} | {gpu}"
    return _HWCACHE

def gpu_data():
    try:
        pynvml.nvmlInit()
        h = pynvml.nvmlDeviceGetHandleByIndex(0)
        d = {}
        name = pynvml.nvmlDeviceGetName(h)
        d["name"] = name.decode() if isinstance(name, bytes) else name
        d["temp"] = pynvml.nvmlDeviceGetTemperature(h, pynvml.NVML_TEMPERATURE_GPU)
        d["clock"] = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_GRAPHICS)
        d["mem_clock"] = pynvml.nvmlDeviceGetClockInfo(h, pynvml.NVML_CLOCK_MEM)
        s = pynvml.nvmlDeviceGetUtilizationRates(h)
        d["gpu_util"] = s.gpu
        d["mem_util"] = s.memory
        d["power"] = pynvml.nvmlDeviceGetPowerUsage(h) / 1000
        m = pynvml.nvmlDeviceGetMemoryInfo(h)
        d["mem_used"] = m.used / 1024**3
        d["mem_total"] = m.total / 1024**3
        try:
            d["fan"] = pynvml.nvmlDeviceGetFanSpeed(h)
        except:
            d["fan"] = None
        # System RAM from /proc/meminfo (kB)
        try:
            meminfo = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split()
                    key = parts[0].rstrip(":")
                    val = int(parts[1])  # kB
                    meminfo[key] = val
            ram_total = meminfo.get("MemTotal", 0) / 1024  # MB
            ram_avail = meminfo.get("MemAvailable", 0) / 1024  # MB
            d["ram_used"] = ram_total - ram_avail
            d["ram_total"] = ram_total
        except:
            d["ram_used"] = None
            d["ram_total"] = None
        pynvml.nvmlShutdown()
        return d
    except Exception as e:
        print(f"GPU error: {e}", file=sys.stderr)
        return None

def render(gpu):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    XL = font(40)
    T = font(22)
    S = font(20)
    SM = font(17)
    L = font(15)
    BAR_H = 18
    M = 20  # side margin
    P = 6   # padding

    y = 12  # top border padding

    # Header — date/time + uptime
    now = datetime.now()
    ts = now.strftime("%Y-%m-%d  %H:%M:%S")
    T2 = font(21)
    d.text((M, y), ts, fill=CYAN, font=T2)
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
        h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
        uptime = f"up {h:02d}:{m:02d}:{s:02d}"
        d.text((W - M, y), uptime, fill=WHITE, font=T2, anchor="ra")
    except:
        pass
    d.line([(M, 40), (W - M, 40)], fill=BORDER, width=2)
    y += 50

    # PC POWER + GPU FAN
    # PC value uses 36px (not XL=40): worst case "9999 / 9999 W" is 13
    # monospace chars — at 40px it ends past fan_x=314 and collides with
    # the GPU FAN column. 36px ends at x<=309 (>=5px gap).
    fan_x = M + (W - 2 * M) * 67 // 100
    XLp = font(36)
    pc_pw = gpu.get("pc_power")
    d.text((M, y), "PC POWER", fill=DIM, font=SM)
    if pc_pw is not None:
        pc_text = f"{pc_pw:.0f} / {PC_LIMIT} W" if PC_LIMIT else f"{pc_pw:.0f} W"
        d.text((M, y + 30), pc_text, fill=YELLOW, font=XLp)
    else:
        d.text((M, y + 30), "n/a", fill=DIM, font=XLp)
    if gpu["fan"] is not None:
        d.text((fan_x, y), "GPU FAN", fill=DIM, font=SM)
        d.text((fan_x, y + 26), f"{gpu['fan']}%", fill=TEAL, font=XL)
    y += 72
    d.line([(M, y), (W - M, y)], fill=BORDER, width=1)
    y += 8

    # System RAM bar
    if gpu.get("ram_used") is not None and gpu.get("ram_total") is not None:
        ram_pct = int(gpu["ram_used"] / gpu["ram_total"] * 100)
        ram_color = c(ram_pct)
        d.text((M, y), "RAM", fill=DIM, font=SM)
        d.text((W - M, y), f"{gpu['ram_used']:.0f} / {gpu['ram_total']:.0f} GB ({ram_pct}%)", fill=ram_color, font=SM, anchor="ra")
        y += 20
        bar(d, M, y, W - 2 * M, BAR_H, ram_pct, ram_color)
        y += BAR_H + 6
    d.line([(M, y), (W - M, y)], fill=BORDER, width=1)
    y += 8

    # Clocks
    d.line([(M, y), (W - M, y)], fill=BORDER, width=1)
    y += 8
    max_clk = 2800
    clk_pct = min(100, gpu["clock"] / max_clk * 100)
    d.text((M, y), "GPU CLK", fill=BLUE, font=SM)
    d.text((W - M, y), f"{gpu['clock']} MHz", fill=BLUE, font=SM, anchor="ra")
    y += 20
    bar(d, M, y, W - 2 * M, BAR_H, clk_pct, BLUE)
    y += BAR_H + 8
    max_mclk = 14000
    mclk_pct = min(100, gpu["mem_clock"] / max_mclk * 100)
    d.text((M, y), "MEM CLK", fill=PURPLE, font=SM)
    d.text((W - M, y), f"{gpu['mem_clock']} MHz", fill=PURPLE, font=SM, anchor="ra")
    y += 20
    bar(d, M, y, W - 2 * M, BAR_H, mclk_pct, PURPLE)
    y += BAR_H + 6

    # Footer
    d.line([(M, y + 8), (W - M, y + 8)], fill=BORDER, width=1)
    y += 14
    gu = gpu["gpu_util"]
    vram_pct = int(gpu["mem_used"] / gpu["mem_total"] * 100) if gpu["mem_total"] else 0
    d.text((M, y), f"GPU {gu}%", fill=GREEN, font=SM)
    d.text((M + 100, y), f"VRAM {gpu['mem_used']:.0f}/{gpu['mem_total']:.0f}GB ({vram_pct}%)", fill=ORANGE, font=SM)
    y += 18
    sparkline(d, M, y, W - 2 * M, 72, hist_gpu, GREEN, hist_vram, ORANGE)
    y += 80
    d.line([(M, y + 6), (W - M, y + 6)], fill=BORDER, width=1)
    d.text((W // 2, y + 10), _hw_string(), fill=DIM, font=L, anchor="ma")

    return img

def send(img, key, trcc_app):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        img.save(f.name, "PNG")
        try:
            result = trcc_app.dispatch(_SendImage(key=key, path=Path(f.name)))
            if not result.ok:
                print(f"trcc failed: {result.message}", file=sys.stderr)
            else:
                print(f"Sent {result.bytes_sent} bytes")
        except Exception as e:
            print(f"trcc dispatch error: {e}", file=sys.stderr)
        finally:
            try:
                os.unlink(f.name)
            except:
                pass

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--device", default=os.environ.get("TRCC_DEVICE", None))
    a = p.parse_args()
    print(f"Dashboard (interval={a.interval}s, device={a.device})")

    # Initialize persistent TRCC connection (avoids reconnection flashes)
    trcc_app = _get_trcc_app()
    print(f"TRCC app initialized, connecting to {a.device}...")
    # Make sure device is connected before starting the loop
    from trcc.core.commands.device import EnsureConnected as _EnsureConnected
    result = trcc_app.dispatch(_EnsureConnected(key=a.device))
    if not result.ok:
        print(f"Failed to connect to {a.device}: {result.message}", file=sys.stderr)
        sys.exit(1)
    print(f"Connected to {a.device}")

    tick = 0
    while running:
        try:
            gpu = gpu_data()
            if not gpu:
                print("GPU unavailable", file=sys.stderr)
                time.sleep(5)
                continue
            # PC power = GPU (NVML) + CPU (zenergy Esocket0 diff)
            cp = cpu_power_w()
            gpu["cpu_power"] = cp
            gpu["pc_power"] = (gpu["power"] + cp) if cp is not None else None
            if tick % 15 == 0:
                if gpu["pc_power"] is not None:
                    print(f"PC {gpu['pc_power']:.0f} W (GPU {gpu['power']:.0f} + CPU {cp:.0f})")
                else:
                    print(f"GPU {gpu['power']:.0f} W (CPU power n/a)")
            vram_pct = int(gpu["mem_used"] / gpu["mem_total"] * 100) if gpu["mem_total"] else 0
            push(gpu["gpu_util"], vram_pct)
            img = render(gpu)
            send(img, a.device, trcc_app)
            tick += 1
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
        time.sleep(a.interval)
    print("Stopped.")

if __name__ == "__main__":
    main()