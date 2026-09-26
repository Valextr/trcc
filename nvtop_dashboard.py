#!/usr/bin/env python3
"""Render nvtop GPU stats to a 480x480 LCD via trcc display send-image.

Usage:
    python3 nvtop_dashboard.py [--interval SECONDS] [--device KEY]

The script:
  1. Calls nvtop -s to get JSON GPU stats
  2. Renders a styled 480x480 PNG with Pillow
  3. Pushes the image to the LCD via trcc's send-image command
  4. Loops at the given interval (default 2s)
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# LCD dimensions
WIDTH, HEIGHT = 480, 480

# Colors (RGB)
BG = (10, 10, 15)
TEXT_WHITE = (220, 220, 230)
TEXT_DIM = (130, 130, 150)
ACCENT_GREEN = (0, 200, 80)
ACCENT_AMBER = (255, 180, 0)
ACCENT_RED = (255, 60, 60)
ACCENT_BLUE = (60, 140, 255)
ACCENT_CYAN = (0, 200, 200)
BAR_BG = (40, 40, 50)
SECTION_BG = (20, 20, 30)
BORDER = (60, 60, 80)


def get_color_for_pct(val, good=None, warn=None):
    """Return color based on percentage thresholds."""
    if val < 50:
        return ACCENT_GREEN
    if val < 80:
        return ACCENT_AMBER
    return ACCENT_RED


def get_font(size):
    """Load Fira Code font, fall back to default."""
    for path in [
        "/usr/local/share/fonts/fira-code/FiraCode-Medium.ttf",
        "/usr/share/fonts/fira-code/FiraCode-Medium.ttf",
        "/usr/share/fonts/TTF/FiraCode-Medium.ttf",
    ]:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    try:
        return ImageFont.truetype("DejaVuSansMono.ttf", size)
    except (OSError, IOError):
        return ImageFont.load_default()


# Resolve via env var, fall back to script-relative path
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_TRCC_BIN = os.environ.get("TRCC_BIN") or os.path.join(_SCRIPT_DIR, ".venv", "bin", "trcc")
_NVTOP_BIN = "/usr/bin/nvtop"


def fetch_nvtop():
    """Call nvtop -s and return parsed JSON array."""
    try:
        out = subprocess.check_output(
            [_NVTOP_BIN, "-s"],
            text=True,
            timeout=5,
        )
        return json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
        return [{"error": str(e)}]


def draw_bar(draw, x, y, w, h, pct, color, bar_bg=BAR_BG):
    """Draw a horizontal bar with rounded-ish appearance."""
    draw.rectangle([x, y, x + w, y + h], fill=bar_bg)
    filled = int(w * pct / 100)
    if filled > 0:
        draw.rectangle([x, y, x + filled, y + h], fill=color)


def render_frame(devices):
    """Render GPU stats to a 480x480 image."""
    img = Image.new("RGB", (WIDTH, HEIGHT), BG)
    draw = ImageDraw.Draw(img)

    title_font = get_font(36)
    big_font = get_font(48)
    med_font = get_font(28)
    small_font = get_font(22)
    label_font = get_font(20)

    # Title
    draw.text((24, 12), "GPU MONITOR", fill=ACCENT_CYAN, font=title_font)
    draw.line([(24, 60), (WIDTH - 24, 60)], fill=BORDER, width=2)

    # Handle error
    if devices and "error" in devices[0]:
        draw.text((30, 100), "nvtop error:", fill=ACCENT_RED, font=med_font)
        draw.text((30, 140), devices[0]["error"], fill=TEXT_DIM, font=small_font)
        return img

    if not devices:
        draw.text((30, 100), "No GPU devices found", fill=TEXT_DIM, font=med_font)
        return img

    y = 80
    gap = 140

    for dev in devices:
        # Device name
        name = dev.get("device_name", "Unknown")
        draw.text((24, y), name, fill=TEXT_WHITE, font=med_font)
        y += 36

        # Stats rows
        rows = [
            ("Temp", dev.get("temp", "N/A")),
            ("GPU Clock", dev.get("gpu_clock", "N/A")),
            ("Mem Clock", dev.get("mem_clock", "N/A")),
            ("Power", dev.get("power_draw", "N/A")),
        ]

        for label, value in rows:
            draw.text((30, y), label + ":", fill=TEXT_DIM, font=label_font)
            draw.text((180, y), str(value), fill=TEXT_WHITE, font=label_font)
            y += 28

        y += 4

        # GPU utilization bar
        try:
            gpu_pct = int(dev.get("gpu_util", "0%").rstrip("%"))
            color = get_color_for_pct(gpu_pct)
        except (ValueError, TypeError):
            gpu_pct = 0
            color = TEXT_DIM
        draw.text((30, y), "GPU:", fill=TEXT_DIM, font=label_font)
        draw.text((80, y), f"{gpu_pct}%", fill=color, font=label_font)
        y += 26
        draw_bar(draw, 30, y, WIDTH - 60, 18, gpu_pct, color)
        y += 26

        # Mem utilization bar
        try:
            mem_pct = int(dev.get("mem_util", "0%").rstrip("%"))
            color = get_color_for_pct(mem_pct)
        except (ValueError, TypeError):
            mem_pct = 0
            color = TEXT_DIM
        draw.text((30, y), "MEM:", fill=TEXT_DIM, font=label_font)
        draw.text((80, y), f"{mem_pct}%", fill=color, font=label_font)
        y += 26
        draw_bar(draw, 30, y, WIDTH - 60, 18, mem_pct, color)

        y += gap

    return img


def send_image_to_lcd(image, device_key):
    """Push image to LCD via trcc display send-image."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        image.save(f.name, "PNG")
        result = subprocess.run(
            [_TRCC_BIN, "display", "send-image", device_key, f.name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            print(f"trcc send-image failed: {result.stderr}", file=sys.stderr)
        else:
            print(f"Sent: {result.stdout.strip()}")


def main():
    parser = argparse.ArgumentParser(description="nvtop GPU dashboard for trcc LCD")
    parser.add_argument(
        "--interval", type=float, default=2.0,
        help="Refresh interval in seconds (default: 2)"
    )
    parser.add_argument(
        "--device", default=os.environ.get("TRCC_DEVICE", None),
        help="Device key (default: env var TRCC_DEVICE)"
    )
    args = parser.parse_args()

    print(f"Starting nvtop dashboard (interval={args.interval}s, device={args.device})")
    print("Press Ctrl+C to stop")

    import time
    while True:
        try:
            devices = fetch_nvtop()
            img = render_frame(devices)
            send_image_to_lcd(img, args.device)
        except KeyboardInterrupt:
            print("\nStopping...")
            break
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
        time.sleep(args.interval)


if __name__ == "__main__":
    main()