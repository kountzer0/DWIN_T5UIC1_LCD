#!/usr/bin/env python3
"""Aquila landscape DWIN control panel v1.8j4.

Keeps the stock Voxelab T5UIC1 firmware/assets.  Uses the Pi UART directly
and talks to the existing Klipper/Moonraker installation over HTTP.

Implemented:
  * 480x272 landscape UI
  * live hotend/bed/position/print status (change-driven redraws)
  * dedicated print-status page with pause/resume/cancel
  * encoder navigation and push button
  * file browser with print confirmation
  * info screen
  * prepare controls with confirmation: PLA preheat, cooldown, home all, motors off
  * safe relative-axis jog page with 10 / 1 / 0.1 mm steps
  * print status screen with live progress, pause/resume/cancel
  * automatic entry into print status for externally started jobs
  * manual four-corner bed-tramming wizard
  * manual centre Z-level / paper-test helper
  * safe BLTouch / 3DTouch placeholder with automatic probe detection

Printer-changing actions are gated behind an explicit confirmation screen.
"""
from __future__ import annotations

import json
import time
import threading
from pathlib import Path
from collections import deque
from typing import Any

import requests
import RPi.GPIO as GPIO

from DWIN_Screen import T5UIC1_LCD

PORT = "/dev/ttyAMA0"
BASE = "http://127.0.0.1:7125"
PIN_A = 19
PIN_B = 26
PIN_ENT = 13

# RGB565 colours
BLACK = 0x0000
WHITE = 0xFFFF
BLUE = 0x001F
CYAN = 0x07FF
GREEN = 0x07E0
YELLOW = 0xFFE0
RED = 0xF800
GREY = 0x7BEF
DARK = 0x2104
MID = 0x4208

# Cohesive UI styling palette for the 480x272 Aquila DWIN panel.
ACCENT = CYAN
PANEL = 0x18C3
PANEL2 = 0x2945
TEXT_DIM = 0xBDF7
HOT = 0xFD20
OK = GREEN
WARN = YELLOW
DANGER = RED

MAIN_ITEMS = ["PRINT", "PREPARE", "MOVE", "TEMPERATURE", "CONTROL", "STATUS", "LEVELING", "SETTINGS", "INFO"]
LEVEL_ITEMS = ["BACK", "MANUAL BED LEVEL", "Z LEVEL (PAPER TEST)", "PROBE SETUP"]

# Conservative stock-Aquila travel points. These are deliberately inside the
# 235 x 235 bed rather than assuming the exact bed-screw coordinates.
BED_LEVEL_POINTS = [
    ("FRONT LEFT", 25.0, 25.0),
    ("FRONT RIGHT", 210.0, 25.0),
    ("REAR RIGHT", 210.0, 210.0),
    ("REAR LEFT", 25.0, 210.0),
]
BED_LEVEL_ACTIONS = ["Z -", "Z +", "STEP", "NEXT", "DONE"]
Z_LEVEL_ACTIONS = ["Z -", "Z +", "STEP", "DONE"]
LEVEL_TRAVEL_Z = 5.0
LEVEL_MIN_Z = 0.20
LEVEL_MAX_Z = 20.0
LEVEL_STEPS = [1.0, 0.1, 0.01]
PREPARE_ITEMS = ["BACK", "PLA PREHEAT", "COOLDOWN", "HOME ALL", "MOTORS OFF"]
TEMP_ITEMS = ["BACK", "HOTEND", "BED"]
TEMP_STEP = 5.0
HOTEND_MAX = 250.0
BED_MAX = 100.0
CONTROL_ITEMS = ["BACK", "FAN", "PRINT SPEED"]
FAN_STEP = 5.0
SPEED_STEP = 5.0
SPEED_MIN = 50.0
SPEED_MAX = 150.0

SETTINGS_ITEMS = ["BACK", "BRIGHTNESS", "SLEEP TIMEOUT", "LOGGING"]

MOVE_AXES = ["BACK", "X", "Y", "Z"]
MOVE_STEPS = [10.0, 1.0, 0.1]
MOVE_FEED = {"X": 3000, "Y": 3000, "Z": 300}

# Display power-saving settings.  These control the DWIN backlight only.
DISPLAY_TIMEOUT_IDLE = 60.0       # default idle timeout
DISPLAY_TIMEOUT_PRINTING = 300.0  # default printing timeout
SETTINGS_FILE = "/home/kount/.aquila_dwin_settings.json"
BRIGHTNESS_DEFAULT = 100
BRIGHTNESS_LEVELS = [25, 50, 75, 100]
TIMEOUT_LEVELS = [30, 60, 120, 300, 0]  # 0 = never



def load_settings() -> dict[str, Any]:
    settings = {
        "brightness": BRIGHTNESS_DEFAULT,
        "timeout": int(DISPLAY_TIMEOUT_IDLE),
        "logging": True,
    }
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if int(saved.get("brightness", BRIGHTNESS_DEFAULT)) in BRIGHTNESS_LEVELS:
            settings["brightness"] = int(saved["brightness"])
        timeout = int(saved.get("timeout", int(DISPLAY_TIMEOUT_IDLE)))
        if timeout in TIMEOUT_LEVELS:
            settings["timeout"] = timeout
        settings["logging"] = bool(saved.get("logging", True))
    except (OSError, ValueError, TypeError):
        pass
    return settings


def save_settings(settings: dict[str, Any]) -> None:
    tmp = SETTINGS_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(settings, fh)
        Path(tmp).replace(SETTINGS_FILE)
    except (OSError, ValueError, TypeError) as exc:
        print(f"Settings save failed: {exc}")


def api_get(path: str) -> dict[str, Any]:
    r = requests.get(BASE + path, timeout=2)
    r.raise_for_status()
    return r.json()


def api_post(path: str, payload: Any = None) -> dict[str, Any]:
    r = requests.post(BASE + path, json=payload, timeout=3)
    r.raise_for_status()
    if not r.content:
        return {}
    return r.json()


def dwin_backlight(lcd: T5UIC1_LCD, value: int) -> None:
    """Set T5UIC1 backlight brightness directly (0x00=off, 0xFF=max)."""
    lcd.Byte(0x30)
    lcd.Byte(max(0, min(0xFF, int(value))))
    lcd.Send()


def screen_sleep(lcd: T5UIC1_LCD) -> None:
    # T5UIC1 command 0x30 with 0x00 turns the backlight off.
    dwin_backlight(lcd, 0x00)


def screen_wake(lcd: T5UIC1_LCD, brightness: int = BRIGHTNESS_DEFAULT) -> None:
    # Restore the configured brightness after sleep or a bridge restart.
    dwin_backlight(lcd, round(max(0, min(100, brightness)) * 255 / 100))


def get_status() -> dict[str, Any]:
    q = ("/printer/objects/query?extruder&heater_bed&toolhead&"
         "print_stats&virtual_sdcard&fan&gcode_move")
    return api_get(q).get("result", {}).get("status", {})


def get_files() -> list[str]:
    data = api_get("/server/files/list").get("result", [])
    return [x.get("path", "") for x in data if x.get("path", "").lower().endswith(".gcode")]


def get_info() -> dict[str, Any]:
    info = api_get("/printer/info").get("result", {})
    try:
        tool = api_get("/printer/objects/query?toolhead").get("result", {}).get("status", {}).get("toolhead", {})
    except Exception:
        tool = {}
    return {
        "software": info.get("software_version", "unknown"),
        "machine": tool.get("axis_maximum", [0, 0, 0]),
    }


def send_text(lcd: T5UIC1_LCD, x: int, y: int, text: str, size: int = 1,
              fg: int = WHITE, bg: int = BLACK, show_bg: bool = True) -> None:
    lcd.Draw_String(False, show_bg, size, fg, bg, x, y, text[:42])


def header(lcd: T5UIC1_LCD, title: str) -> None:
    lcd.Draw_Rectangle(1, PANEL2, 0, 0, 479, 31)
    lcd.Draw_Rectangle(1, ACCENT, 0, 30, 479, 31)
    send_text(lcd, 10, 7, f"AQUILA  {title}", 2, WHITE, PANEL2)

def footer(lcd: T5UIC1_LCD, text: str = "TURN = SELECT   PRESS = ENTER") -> None:
    lcd.Draw_Rectangle(1, DARK, 0, 247, 479, 271)
    lcd.Draw_Rectangle(1, ACCENT, 0, 247, 479, 249)
    send_text(lcd, 10, 254, text, 1, TEXT_DIM, DARK)

def panel(lcd: T5UIC1_LCD, x1: int, y1: int, x2: int, y2: int,
          fill: int = PANEL, border: int = PANEL2) -> None:
    lcd.Draw_Rectangle(1, fill, x1, y1, x2, y2)
    lcd.Draw_Rectangle(0, border, x1, y1, x2, y2)



def draw_menu_icon(lcd: T5UIC1_LCD, kind: str, x: int, y: int,
                   fg: int) -> None:
    """Draw a tiny low-bandwidth vector glyph, avoiding the mismatched stock ICOs."""
    cx = x + 8
    cy = y + 10

    if kind == "PRINT":
        lcd.Draw_Rectangle(0, fg, x + 3, y + 5, x + 14, y + 16)
        lcd.Draw_Rectangle(1, fg, x + 6, y + 2, x + 11, y + 6)
        lcd.Draw_Line(fg, x + 6, y + 9, x + 11, y + 9)
        lcd.Draw_Line(fg, x + 6, y + 12, x + 11, y + 12)
    elif kind == "PREPARE":
        lcd.Draw_Rectangle(0, fg, x + 2, y + 4, x + 14, y + 16)
        lcd.Draw_Line(fg, x + 1, y + 8, x + 15, y + 8)
        lcd.Draw_Line(fg, x + 1, y + 12, x + 15, y + 12)
        lcd.Draw_Line(fg, x + 5, y + 1, x + 5, y + 18)
        lcd.Draw_Line(fg, x + 11, y + 1, x + 11, y + 18)
    elif kind == "MOVE":
        lcd.Draw_Line(fg, cx, y + 1, cx, y + 18)
        lcd.Draw_Line(fg, x + 1, cy, x + 15, cy)
        lcd.Draw_Line(fg, cx, y + 1, x + 5, y + 5)
        lcd.Draw_Line(fg, cx, y + 1, x + 11, y + 5)
        lcd.Draw_Line(fg, cx, y + 18, x + 5, y + 14)
        lcd.Draw_Line(fg, cx, y + 18, x + 11, y + 14)
        lcd.Draw_Line(fg, x + 1, cy, x + 5, y + 6)
        lcd.Draw_Line(fg, x + 1, cy, x + 5, y + 14)
        lcd.Draw_Line(fg, x + 15, cy, x + 11, y + 6)
        lcd.Draw_Line(fg, x + 15, cy, x + 11, y + 14)
    elif kind == "STATUS":
        lcd.Draw_Rectangle(1, fg, x + 2, y + 11, x + 5, y + 17)
        lcd.Draw_Rectangle(1, fg, x + 7, y + 7, x + 10, y + 17)
        lcd.Draw_Rectangle(1, fg, x + 12, y + 3, x + 15, y + 17)
    elif kind == "INFO":
        lcd.Draw_Circle(fg, cx, cy, 7)
        lcd.Draw_Rectangle(1, fg, x + 7, y + 8, x + 9, y + 9)
        lcd.Draw_Rectangle(1, fg, x + 7, y + 11, x + 9, y + 16)
    else:
        lcd.Draw_Rectangle(0, fg, x + 3, y + 3, x + 14, y + 17)


def menu_card(lcd: T5UIC1_LCD, x: int, y: int, w: int, h: int,
              label: str, selected: bool, accent: int = ACCENT,
              icon: str | None = None) -> None:
    """Compact control-panel card used by secondary menus."""
    bg = PANEL2 if selected else BLACK
    border = accent if selected else GREY
    lcd.Draw_Rectangle(1, bg, x, y, x + w, y + h)
    lcd.Draw_Rectangle(0, border, x, y, x + w, y + h)
    if icon is None:
        icon = label.split()[0]
    draw_menu_icon(lcd, icon, x + 8, y + max(0, (h - 20) // 2),
                   accent if selected else TEXT_DIM)
    send_text(lcd, x + 31, y + (h // 2) - 7, label[:24], 1, WHITE, bg)


def menu_grid(lcd: T5UIC1_LCD, items: list[str], selected: int,
              y: int = 44, row_h: int = 40, gap: int = 8,
              accent_for: Any = None) -> None:
    """Two-column menu grid, preserving linear encoder order."""
    for i, item in enumerate(items):
        col = i % 2; row = i // 2
        x = 10 + col * 235
        accent = accent_for(item, i) if accent_for else ACCENT
        menu_card(lcd, x, y + row * (row_h + gap), 224, row_h, item, i == selected, accent)
    if len(items) > 4:
        send_text(lcd, 435, 225, f"{selected + 1}/{len(items)}", 1, TEXT_DIM, BLACK)


def menu_row(lcd: T5UIC1_LCD, y: int, label: str, selected: bool,
             accent: int = ACCENT) -> None:
    bg = PANEL2 if selected else BLACK
    border = accent if selected else GREY
    lcd.Draw_Rectangle(1, bg, 10, y, 469, y + 17)
    lcd.Draw_Rectangle(0, border, 10, y, 469, y + 17)

    icon_fg = accent if selected else TEXT_DIM
    draw_menu_icon(lcd, label.split()[0], 17, y, icon_fg)

    # Keep the text clear of the icon and make the selected row read instantly.
    send_text(lcd, 43, y + 2, label[:31], 1, WHITE, bg)


def value_card(lcd: T5UIC1_LCD, x1: int, x2: int, label: str,
               value: str, fg: int = WHITE) -> None:
    panel(lcd, x1, 38, x2, 88)
    send_text(lcd, x1 + 10, 43, label, 1, TEXT_DIM, PANEL)
    send_text(lcd, x1 + 10, 61, value, 2, fg, PANEL)


def draw_status(lcd: T5UIC1_LCD, status: dict[str, Any]) -> None:
    ext = status.get("extruder", {})
    bed = status.get("heater_bed", {})
    tool = status.get("toolhead", {})
    stats = status.get("print_stats", {})
    vsd = status.get("virtual_sdcard", {})

    e_t = float(ext.get("temperature", 0))
    e_s = float(ext.get("target", 0))
    b_t = float(bed.get("temperature", 0))
    b_s = float(bed.get("target", 0))
    pos = tool.get("position", [0, 0, 0, 0])
    raw = str(stats.get("state", "unknown")).lower()
    state = "PRINTING" if raw == "printing" else "PAUSED" if raw == "paused" else "READY"
    progress = float(vsd.get("progress", 0)) * 100

    value_card(lcd, 6, 151, "HOTEND", f"{e_t:4.1f}/{e_s:3.0f} C", HOT)
    value_card(lcd, 158, 303, "BED", f"{b_t:4.1f}/{b_s:3.0f} C", WHITE)
    panel(lcd, 310, 38, 473, 88)
    send_text(lcd, 320, 43, "POSITION", 1, TEXT_DIM, PANEL)
    send_text(lcd, 320, 59, f"X {float(pos[0]):.1f}  Y {float(pos[1]):.1f}", 1, WHITE, PANEL)
    send_text(lcd, 320, 73, f"Z {float(pos[2]):.2f}", 1, WHITE, PANEL)
    send_text(lcd, 390, 73, state, 1, OK if state == "PRINTING" else WARN if state == "PAUSED" else OK, PANEL)

    panel(lcd, 6, 95, 473, 119)
    lcd.Draw_Rectangle(1, DARK, 16, 101, 463, 112)
    fill = 16 + int(447 * max(0, min(100, progress)) / 100)
    if fill > 16:
        lcd.Draw_Rectangle(1, OK if state == "PRINTING" else ACCENT, 16, 101, fill, 112)
    send_text(lcd, 20, 99, f"{progress:5.1f}%", 1, WHITE, DARK)

def format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def draw_print_status(
    lcd: T5UIC1_LCD,
    status: dict[str, Any],
    selected: int,
) -> None:
    lcd.Frame_Clear(BLACK)
    stats = status.get("print_stats", {})
    raw_state = str(stats.get("state", "standby")).lower()
    state = "PRINTING" if raw_state == "printing" else "PAUSED" if raw_state == "paused" else "READY"
    header(lcd, f"PRINT  {state}")

    ext = status.get("extruder", {})
    bed = status.get("heater_bed", {})
    tool = status.get("toolhead", {})
    vsd = status.get("virtual_sdcard", {})
    e_t = float(ext.get("temperature", 0))
    e_s = float(ext.get("target", 0))
    b_t = float(bed.get("temperature", 0))
    b_s = float(bed.get("target", 0))
    pos = tool.get("position", [0, 0, 0, 0])
    name = str(stats.get("filename", ""))
    progress = max(0.0, min(100.0, float(vsd.get("progress", 0)) * 100.0))
    elapsed = float(stats.get("print_duration", 0))

    panel(lcd, 8, 37, 471, 62)
    send_text(
        lcd, 18, 43,
        name.split("/")[-1] if state in ("PRINTING", "PAUSED") and name else "No active print",
        1, WHITE, PANEL
    )

    value_card(lcd, 8, 155, "HOTEND", f"{e_t:4.1f}/{e_s:3.0f} C", HOT)
    value_card(lcd, 164, 311, "BED", f"{b_t:4.1f}/{b_s:3.0f} C", WHITE)

    panel(lcd, 320, 67, 471, 96)
    send_text(lcd, 329, 72, f"X {float(pos[0]):.1f}  Y {float(pos[1]):.1f}", 1, WHITE, PANEL)
    send_text(lcd, 329, 85, f"Z {float(pos[2]):.2f}  {format_elapsed(elapsed)}", 1, ACCENT, PANEL)

    panel(lcd, 8, 101, 471, 127)
    lcd.Draw_Rectangle(1, DARK, 17, 108, 462, 119)
    fill = 17 + int(445 * progress / 100.0)
    if fill > 17:
        lcd.Draw_Rectangle(1, OK if state == "PRINTING" else ACCENT, 17, 108, fill, 119)
    send_text(lcd, 22, 104, f"{progress:5.1f}%", 1, WHITE, DARK)

    if state == "PAUSED":
        actions = ["RESUME", "CANCEL", "BACK"]
    elif state == "PRINTING":
        actions = ["PAUSE", "CANCEL", "BACK"]
    else:
        actions = ["BACK"]

    for i, label in enumerate(actions):
        x = 18 + i * 150
        sel = i == selected
        bg = PANEL2 if sel else BLACK
        border = DANGER if label == "CANCEL" else (OK if sel else GREY)
        lcd.Draw_Rectangle(1, bg, x, 139, x + 130, 181)
        lcd.Draw_Rectangle(0, border, x, 139, x + 130, 181)
        send_text(lcd, x + 28, 151, label, 1, WHITE, bg)

    footer(lcd)
    lcd.UpdateLCD()


def action_state(status: dict[str, Any]) -> str:
    return str(status.get("print_stats", {}).get("state", "standby")).lower()


def draw_settings_item(lcd: T5UIC1_LCD, index: int, selected: int, logging_enabled: bool) -> None:
    row = index // 2
    col = index % 2
    x = 10 + col * 235
    y = 157 + row * 45
    w, h = 225, 40
    item = SETTINGS_ITEMS[index]
    bg = PANEL2 if index == selected else BLACK
    border = ACCENT if index == selected else GREY
    lcd.Draw_Rectangle(1, bg, x, y, x + w, y + h)
    lcd.Draw_Rectangle(0, border, x, y, x + w, y + h)

    if item == "LOGGING":
        send_text(lcd, x + 10, y + 11, "DIAG LOG", 1, WHITE, bg)
        # Simple hardware-style toggle: coloured track with a sliding knob.
        tx1, ty1, tx2, ty2 = x + 158, y + 10, x + 210, y + 30
        track = OK if logging_enabled else GREY
        lcd.Draw_Rectangle(1, track, tx1, ty1, tx2, ty2)
        knob_x = tx2 - 10 if logging_enabled else tx1 + 10
        lcd.Draw_Rectangle(1, WHITE, knob_x - 7, ty1 + 3, knob_x + 7, ty2 - 3)
        send_text(lcd, x + 116, y + 11, "ON" if logging_enabled else "OFF", 1,
                  OK if logging_enabled else TEXT_DIM, bg)
    else:
        icon = item.split()[0]
        draw_menu_icon(lcd, icon, x + 8, y + 10, ACCENT if index == selected else TEXT_DIM)
        send_text(lcd, x + 31, y + 11, item, 1, WHITE, bg)


def draw_settings(
    lcd: T5UIC1_LCD,
    brightness: int,
    timeout: int,
    selected: int,
    logging_enabled: bool,
) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "SETTINGS")

    timeout_text = "NEVER" if timeout == 0 else f"{timeout} SEC"

    panel(lcd, 8, 38, 471, 98)
    send_text(lcd, 20, 45, "BRIGHTNESS", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 63, f"{brightness:3d} %", 2, ACCENT, PANEL)

    panel(lcd, 8, 106, 471, 149)
    send_text(lcd, 20, 113, "SLEEP TIMEOUT", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 131, timeout_text, 2, WHITE, PANEL)

    for i in range(len(SETTINGS_ITEMS)):
        draw_settings_item(lcd, i, selected, logging_enabled)

    footer(lcd, "TURN = SELECT   PRESS = EDIT / TOGGLE")
    lcd.UpdateLCD()

def draw_settings_selection(lcd: T5UIC1_LCD, old_selected: int, selected: int, logging_enabled: bool) -> None:
    for i in {old_selected, selected}:
        draw_settings_item(lcd, i, selected, logging_enabled)
    lcd.UpdateLCD()

def draw_settings_edit(lcd: T5UIC1_LCD, kind: str, value: int) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, f"SET {kind}")

    if kind == "BRIGHTNESS":
        shown = f"{int(value):3d} %"
        detail = "DISPLAY BACKLIGHT"
        limits = "25 / 50 / 75 / 100 %"
    else:
        shown = "NEVER" if int(value) == 0 else f"{int(value)} SEC"
        detail = "IDLE BACKLIGHT TIMEOUT"
        limits = "30 / 60 / 120 / 300 / NEVER"

    panel(lcd, 8, 45, 471, 113)
    send_text(lcd, 20, 52, detail, 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 72, shown, 3, ACCENT, PANEL)

    panel(lcd, 8, 125, 471, 172)
    send_text(lcd, 20, 134, "AVAILABLE", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 151, limits, 1, WHITE, PANEL)

    footer(lcd, "TURN = ADJUST   PRESS = APPLY")
    lcd.UpdateLCD()


def main_menu_window(selected: int) -> tuple[int, int]:
    """Return (first_index, visible_count) for a five-row scrolling home menu."""
    visible = 5
    count = len(MAIN_ITEMS)
    if count <= visible:
        return 0, count

    first = max(0, selected - visible + 1)
    first = min(first, count - visible)
    return first, visible


def draw_main(lcd: T5UIC1_LCD, status: dict[str, Any], selected: int) -> None:
    lcd.Frame_Clear(BLACK)
    raw = str(status.get("print_stats", {}).get("state", "standby")).lower()
    state = "PRINTING" if raw == "printing" else "PAUSED" if raw == "paused" else "READY"

    header(lcd, f"MAIN  {state}")
    draw_status(lcd, status)
    lcd.Draw_Line(PANEL2, 8, 121, 471, 121)

    # Two-column home layout: all nine functions remain directly visible,
    # while the encoder still walks them in the existing linear order.
    # This gives the home screen a proper control-panel feel without changing
    # any navigation semantics.
    card_w = 225
    card_h = 20
    gap_x = 9
    x_positions = (10, 244)
    row_y = 126
    for idx, item in enumerate(MAIN_ITEMS):
        col = idx % 2
        row = idx // 2
        x = x_positions[col]
        y = row_y + row * 22
        selected_row = idx == selected
        bg = PANEL2 if selected_row else BLACK
        border = ACCENT if selected_row else MID
        icon_fg = ACCENT if selected_row else TEXT_DIM
        lcd.Draw_Rectangle(1, bg, x, y, x + card_w, y + card_h)
        lcd.Draw_Rectangle(0, border, x, y, x + card_w, y + card_h)
        draw_menu_icon(lcd, item.split()[0], x + 7, y, icon_fg)
        send_text(lcd, x + 29, y + 2, item[:27], 1, WHITE, bg)

    # Position indicator doubles as a useful reminder that the encoder order
    # is linear even though the presentation is a grid.
    send_text(lcd, 407, 235, f"{selected + 1}/{len(MAIN_ITEMS)}", 1, TEXT_DIM, BLACK)

    footer(lcd)
    lcd.UpdateLCD()

def draw_main_status(lcd: T5UIC1_LCD, status: dict[str, Any]) -> None:
    """Redraw only the live header/status band on the home screen."""
    raw = str(status.get("print_stats", {}).get("state", "standby")).lower()
    state = "PRINTING" if raw == "printing" else "PAUSED" if raw == "paused" else "READY"
    header(lcd, f"MAIN  {state}")
    draw_status(lcd, status)
    lcd.UpdateLCD()


def draw_main_selection(lcd: T5UIC1_LCD, old_selected: int, selected: int) -> None:
    """Redraw only the two changed home-menu cards and position indicator."""
    card_w = 225
    card_h = 20
    x_positions = (10, 244)
    row_y = 126

    for idx in {old_selected, selected}:
        item = MAIN_ITEMS[idx]
        col = idx % 2
        row = idx // 2
        x = x_positions[col]
        y = row_y + row * 22
        selected_row = idx == selected
        bg = PANEL2 if selected_row else BLACK
        border = ACCENT if selected_row else MID
        icon_fg = ACCENT if selected_row else TEXT_DIM
        lcd.Draw_Rectangle(1, bg, x, y, x + card_w, y + card_h)
        lcd.Draw_Rectangle(0, border, x, y, x + card_w, y + card_h)
        draw_menu_icon(lcd, item.split()[0], x + 7, y, icon_fg)
        send_text(lcd, x + 29, y + 2, item[:27], 1, WHITE, bg)

    # The indicator has its own small area, so it can be refreshed without
    # touching the rest of the home screen.
    send_text(lcd, 407, 235, f"{selected + 1}/{len(MAIN_ITEMS)}", 1, TEXT_DIM, BLACK)
    lcd.UpdateLCD()

def draw_prepare_selection(lcd: T5UIC1_LCD, old_selected: int, selected: int) -> None:
    for i in {old_selected, selected}:
        item = PREPARE_ITEMS[i]
        accent = WARN if item == "PLA PREHEAT" else ACCENT
        row = i // 2
        col = i % 2
        x = 10 + col * 235
        y = 45 + row * (42 + 9)
        menu_card(lcd, x, y, 224, 42, item, i == selected, accent)
    lcd.UpdateLCD()


def draw_prepare(lcd: T5UIC1_LCD, selected: int) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "PREPARE")
    menu_grid(lcd, PREPARE_ITEMS, selected, y=45, row_h=42, gap=9,
              accent_for=lambda item, i: WARN if item == "PLA PREHEAT" else ACCENT)
    panel(lcd, 10, 197, 469, 224)
    send_text(lcd, 22, 205, "MACHINE PREPARATION", 1, ACCENT, PANEL)
    send_text(lcd, 22, 216, "HEAT  •  HOME  •  COOLDOWN  •  MOTORS", 1, TEXT_DIM, PANEL)
    footer(lcd, "TURN = SELECT   PRESS = ENTER")
    lcd.UpdateLCD()

def draw_leveling_selection(lcd: T5UIC1_LCD, old_selected: int, selected: int) -> None:
    for i in {old_selected, selected}:
        item = LEVEL_ITEMS[i]
        accent = WARN if item == "MANUAL BED LEVEL" else ACCENT
        row = i // 2
        col = i % 2
        x = 10 + col * 235
        y = 47 + row * (54 + 10)
        menu_card(lcd, x, y, 224, 54, item, i == selected, accent)
    lcd.UpdateLCD()


def draw_info(lcd: T5UIC1_LCD, info: dict[str, Any]) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "INFO")
    panel(lcd, 8, 41, 471, 99)
    send_text(lcd, 20, 49, "KLIPPER", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 66, str(info.get("software", "unknown")), 2, WHITE, PANEL)

    machine = info.get("machine", [0, 0, 0])
    panel(lcd, 8, 108, 471, 166)
    send_text(lcd, 20, 116, "BUILD VOLUME", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 136, f"{machine[0]:g} x {machine[1]:g} x {machine[2]:g} mm", 2, WHITE, PANEL)

    panel(lcd, 8, 176, 471, 223)
    send_text(lcd, 20, 185, "DISPLAY", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 203, "DWIN T5UIC1  |  480 x 272", 1, ACCENT, PANEL)
    footer(lcd, "PRESS = BACK")
    lcd.UpdateLCD()

def draw_confirm(lcd: T5UIC1_LCD, filename: str, yes: bool) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "PRINT")
    panel(lcd, 8, 42, 471, 105)
    send_text(lcd, 20, 50, "START PRINT?", 2, WARN, PANEL)
    send_text(lcd, 20, 78, filename.split("/")[-1], 1, WHITE, PANEL)
    for i, label in enumerate(("YES", "NO")):
        sel = (i == 0 and yes) or (i == 1 and not yes)
        x = 55 + i * 220
        bg = PANEL2 if sel else BLACK
        border = OK if sel else GREY
        lcd.Draw_Rectangle(1, bg, x, 127, x + 165, 185)
        lcd.Draw_Rectangle(0, border, x, 127, x + 165, 185)
        send_text(lcd, x + 58, 147, label, 2, WHITE, bg)
    footer(lcd, "TURN = SELECT   PRESS = CONFIRM")
    lcd.UpdateLCD()

def draw_action_confirm(lcd: T5UIC1_LCD, action: str, yes: bool) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "CONFIRM")
    panel(lcd, 8, 42, 471, 111)
    send_text(lcd, 20, 51, action, 2, WARN if action != "MOTORS OFF" else DANGER, PANEL)
    send_text(lcd, 20, 82, "Execute this printer action?", 1, WHITE, PANEL)
    for i, label in enumerate(("YES", "NO")):
        sel = (i == 0 and yes) or (i == 1 and not yes)
        x = 55 + i * 220
        bg = PANEL2 if sel else BLACK
        border = OK if sel else GREY
        lcd.Draw_Rectangle(1, bg, x, 136, x + 165, 194)
        lcd.Draw_Rectangle(0, border, x, 136, x + 165, 194)
        send_text(lcd, x + 58, 156, label, 2, WHITE, bg)
    footer(lcd, "TURN = SELECT   PRESS = CONFIRM")
    lcd.UpdateLCD()

def draw_files(lcd: T5UIC1_LCD, files: list[str], selected: int) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "PRINT FILES")
    items = ["BACK"] + files
    visible = 7
    start_idx = max(0, min(max(0, len(items) - visible), selected - visible + 1))
    for row, idx in enumerate(range(start_idx, min(start_idx + visible, len(items)))):
        menu_row(lcd, 38 + row * 29, items[idx].split("/")[-1], idx == selected)
    if len(items) > visible:
        send_text(lcd, 406, 232, f"{selected + 1}/{len(items)}", 1, TEXT_DIM, BLACK)
    footer(lcd, "TURN = SELECT   PRESS = ENTER")
    lcd.UpdateLCD()

def quadrature_step(last_ab: tuple[int, int], ab: tuple[int, int]) -> int:
    # +1 = CW, -1 = CCW, 0 = no completed detent
    cw = {(1, 1): (0, 1), (0, 1): (0, 0), (0, 0): (1, 0), (1, 0): (1, 1)}
    ccw = {(1, 1): (1, 0), (1, 0): (0, 0), (0, 0): (0, 1), (0, 1): (1, 1)}
    if cw.get(last_ab) == ab and ab == (1, 1):
        return 1
    if ccw.get(last_ab) == ab and ab == (1, 1):
        return -1
    return 0


def run_gcode(script: str) -> None:
    # Current Moonraker GCode API. See /printer/gcode/script.
    api_post("/printer/gcode/script", {"script": script})


def start_print(filename: str) -> None:
    r = requests.post(BASE + "/printer/print/start",
                      params={"filename": filename},
                      timeout=3)
    r.raise_for_status()


def pause_print() -> None:
    api_post("/printer/print/pause")


def resume_print() -> None:
    api_post("/printer/print/resume")


def cancel_print() -> None:
    api_post("/printer/print/cancel")


def jog_axis(axis: str, distance: float) -> None:
    if axis not in MOVE_FEED:
        return
    # Relative move followed by restoring absolute mode. This keeps the jog
    # operation self-contained and avoids leaving Klipper in G91.
    script = f"G91\nG1 {axis}{distance:g} F{MOVE_FEED[axis]}\nG90"
    run_gcode(script)


def draw_move_axes(lcd: T5UIC1_LCD, selected: int) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "MOVE")
    items = ["BACK", "JOG X", "JOG Y", "JOG Z"]
    menu_grid(lcd, items, selected, y=48, row_h=54, gap=10)
    send_text(lcd, 12, 169, "SELECT AN AXIS TO JOG", 1, TEXT_DIM, BLACK)
    footer(lcd, "TURN = SELECT   PRESS = ENTER")
    lcd.UpdateLCD()

def draw_jog(lcd: T5UIC1_LCD, axis: str, step_index: int, selected: int) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, f"JOG {axis}")
    step = MOVE_STEPS[step_index]
    panel(lcd, 8, 38, 471, 82)
    send_text(lcd, 20, 44, "STEP SIZE", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 61, f"{step:g} mm", 2, ACCENT, PANEL)
    send_text(lcd, 145, 62, "RELATIVE JOG", 1, WHITE, PANEL)
    for i, label in enumerate(("- MOVE", "+ MOVE", "STEP", "BACK")):
        x = 12 + i * 117
        sel = i == selected
        bg = PANEL2 if sel else BLACK
        border = OK if sel else GREY
        lcd.Draw_Rectangle(1, bg, x, 98, x + 104, 156)
        lcd.Draw_Rectangle(0, border, x, 98, x + 104, 156)
        send_text(lcd, x + 21, 119, label, 1, WHITE, bg)
    footer(lcd, "PRESS = ACTION   STEP = 10 / 1 / 0.1 mm")
    lcd.UpdateLCD()



def draw_leveling(lcd: T5UIC1_LCD, selected: int) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "LEVELING")
    menu_grid(lcd, LEVEL_ITEMS, selected, y=47, row_h=54, gap=10,
              accent_for=lambda item, i: WARN if item == "MANUAL BED LEVEL" else ACCENT)
    panel(lcd, 10, 177, 469, 214)
    send_text(lcd, 22, 185, "CALIBRATION TOOLS", 1, ACCENT, PANEL)
    send_text(lcd, 22, 202, "MANUAL BED  •  PAPER Z  •  PROBE", 1, TEXT_DIM, PANEL)
    footer(lcd, "TURN = SELECT   PRESS = ENTER")
    lcd.UpdateLCD()

def draw_bed_level(
    lcd: T5UIC1_LCD,
    point_index: int,
    action_selected: int,
    z_position: float,
    step_index: int,
) -> None:
    lcd.Frame_Clear(BLACK)
    point_name, x, y = BED_LEVEL_POINTS[point_index]
    header(lcd, f"BED LEVEL  {point_index + 1}/4")

    panel(lcd, 8, 39, 471, 91)
    send_text(lcd, 20, 46, point_name, 2, ACCENT, PANEL)
    send_text(lcd, 250, 50, f"X {x:g}  Y {y:g}", 1, WHITE, PANEL)
    send_text(lcd, 250, 68, f"Z {z_position:.2f} mm", 1, WHITE, PANEL)

    panel(lcd, 8, 100, 471, 127)
    send_text(lcd, 20, 107, "ADJUST BED SCREW WITH PAPER / FEELER", 1, TEXT_DIM, PANEL)

    for i, item in enumerate(BED_LEVEL_ACTIONS):
        row = i % 3
        col = i // 3
        x1 = 10 + col * 230
        y1 = 136 + row * 30
        sel = i == action_selected
        bg = PANEL2 if sel else BLACK
        border = OK if sel else GREY
        lcd.Draw_Rectangle(1, bg, x1, y1, x1 + 215, y1 + 24)
        lcd.Draw_Rectangle(0, border, x1, y1, x1 + 215, y1 + 24)
        send_text(lcd, x1 + 82, y1 + 5, item, 1, WHITE, bg)

    send_text(lcd, 20, 225, f"STEP {LEVEL_STEPS[step_index]:g} mm", 1, TEXT_DIM, BLACK)
    footer(lcd, "Z-/Z+ ADJUST   STEP CHANGE   NEXT CORNER   DONE")
    lcd.UpdateLCD()


def draw_z_level(
    lcd: T5UIC1_LCD,
    action_selected: int,
    z_position: float,
    step_index: int,
) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "Z LEVEL (PAPER TEST)")
    panel(lcd, 8, 39, 471, 94)
    send_text(lcd, 20, 46, "NOZZLE POSITION", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 64, f"Z {z_position:.2f} mm", 3, ACCENT, PANEL)

    panel(lcd, 8, 103, 471, 137)
    send_text(lcd, 20, 111, "CENTRE OF BED", 1, TEXT_DIM, PANEL)
    send_text(lcd, 170, 111, "PAPER / FEELER TEST", 1, WHITE, PANEL)
    send_text(lcd, 20, 139, "THIS HELPER DOES NOT SAVE AN OFFSET.", 1, TEXT_DIM, BLACK)

    for i, item in enumerate(Z_LEVEL_ACTIONS):
        x1 = 10 + i * 115
        sel = i == action_selected
        bg = PANEL2 if sel else BLACK
        border = OK if sel else GREY
        lcd.Draw_Rectangle(1, bg, x1, 153, x1 + 103, 205)
        lcd.Draw_Rectangle(0, border, x1, 153, x1 + 103, 205)
        send_text(lcd, x1 + 34, 173, item, 1, WHITE, bg)

    send_text(lcd, 20, 219, f"STEP {LEVEL_STEPS[step_index]:g} mm", 1, TEXT_DIM, BLACK)
    footer(lcd, "TURN = SELECT   PRESS = ACTION")
    lcd.UpdateLCD()


def probe_detected() -> bool:
    """Detect a configured Klipper probe without enabling any probe motion."""
    try:
        objects = api_get("/printer/objects/list").get("result", {}).get("objects", [])
        return any(name in objects for name in ("bltouch", "probe"))
    except Exception:
        return False


def draw_probe_setup(lcd: T5UIC1_LCD, detected: bool) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "PROBE SETUP")
    panel(lcd, 8, 42, 471, 112)
    state = "PROBE DETECTED" if detected else "NOT INSTALLED"
    fg = OK if detected else WARN
    send_text(lcd, 20, 51, "BLTOUCH / 3DTOUCH", 2, WHITE, PANEL)
    send_text(lcd, 20, 82, state, 2, fg, PANEL)

    panel(lcd, 8, 123, 471, 202)
    if detected:
        send_text(lcd, 20, 134, "PROBE HARDWARE IS PRESENT.", 1, OK, PANEL)
        send_text(lcd, 20, 153, "CALIBRATION / MESH UI RESERVED", 1, WHITE, PANEL)
        send_text(lcd, 20, 170, "FOR THE NEXT LEVELING STEP.", 1, WHITE, PANEL)
    else:
        send_text(lcd, 20, 134, "SAFE PLACEHOLDER ONLY.", 1, TEXT_DIM, PANEL)
        send_text(lcd, 20, 153, "NO PROBE COMMANDS ARE SENT.", 1, WHITE, PANEL)
        send_text(lcd, 20, 170, "FIT + CONFIGURE THE PROBE FIRST.", 1, WHITE, PANEL)
    footer(lcd, "PRESS = BACK")
    lcd.UpdateLCD()


def start_manual_bed_level() -> tuple[int, float]:
    """Home, move to a safe height, then approach the first corner."""
    point = BED_LEVEL_POINTS[0]
    run_gcode(
        "G28\n"
        f"G90\nG1 Z{LEVEL_TRAVEL_Z:g} F300\n"
        f"G1 X{point[1]:g} Y{point[2]:g} F3000\n"
        f"G1 Z{LEVEL_TRAVEL_Z:g} F300"
    )
    return 0, LEVEL_TRAVEL_Z


def move_bed_level_point(point_index: int, z_position: float) -> None:
    point = BED_LEVEL_POINTS[point_index]
    run_gcode(
        f"G90\nG1 Z{LEVEL_TRAVEL_Z:g} F300\n"
        f"G1 X{point[1]:g} Y{point[2]:g} F3000\n"
        f"G1 Z{z_position:g} F300"
    )


def bed_level_z_move(delta: float, z_position: float) -> float:
    new_z = max(LEVEL_MIN_Z, min(LEVEL_MAX_Z, z_position + delta))
    actual_delta = new_z - z_position
    if abs(actual_delta) > 1e-9:
        run_gcode(f"G91\nG1 Z{actual_delta:g} F120\nG90")
    return new_z


def finish_leveling() -> None:
    run_gcode(f"G90\nG1 Z{LEVEL_TRAVEL_Z:g} F300")


def start_z_level() -> float:
    run_gcode(
        "G28\n"
        "G90\n"
        f"G1 X117.5 Y117.5 Z{LEVEL_TRAVEL_Z:g} F3000"
    )
    return LEVEL_TRAVEL_Z


def get_fan_percent(status: dict[str, Any]) -> float:
    return max(0.0, min(100.0, float(status.get("fan", {}).get("speed", 0.0)) * 100.0))


def get_speed_percent(status: dict[str, Any]) -> float:
    return max(
        0.0,
        min(200.0, float(status.get("gcode_move", {}).get("speed_factor", 1.0)) * 100.0)
    )


def set_fan_percent(value: float) -> None:
    value = max(0.0, min(100.0, value))
    pwm = round(value * 255.0 / 100.0)
    run_gcode(f"M106 S{pwm}")


def set_speed_percent(value: float) -> None:
    value = max(SPEED_MIN, min(SPEED_MAX, value))
    run_gcode(f"M220 S{value:g}")


def draw_control(lcd: T5UIC1_LCD, status: dict[str, Any], selected: int) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "CONTROL")

    fan = get_fan_percent(status)
    speed = get_speed_percent(status)

    panel(lcd, 8, 38, 235, 91)
    send_text(lcd, 20, 45, "FAN", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 62, f"{fan:3.0f} %", 2, ACCENT, PANEL)
    send_text(lcd, 150, 67, "PART COOLING", 1, WHITE, PANEL)

    panel(lcd, 245, 38, 471, 91)
    send_text(lcd, 257, 45, "PRINT SPEED", 1, TEXT_DIM, PANEL)
    send_text(lcd, 257, 62, f"{speed:3.0f} %", 2, WARN, PANEL)
    send_text(lcd, 257, 67, "M220", 1, WHITE, PANEL)

    for i, item in enumerate(CONTROL_ITEMS):
        x = 10 + i * 153
        accent = WARN if item == "PRINT SPEED" else ACCENT
        menu_card(lcd, x, 108, 143, 54, item, i == selected, accent)
    footer(lcd, "TURN = SELECT   PRESS = EDIT / ENTER")
    lcd.UpdateLCD()


def draw_control_selection(lcd: T5UIC1_LCD, old_selected: int, selected: int) -> None:
    """Redraw only the changed CONTROL selection cards."""
    for i in {old_selected, selected}:
        item = CONTROL_ITEMS[i]
        x = 10 + i * 153
        accent = WARN if item == "PRINT SPEED" else ACCENT
        menu_card(lcd, x, 108, 143, 54, item, i == selected, accent)
    lcd.UpdateLCD()


def draw_control_edit(
    lcd: T5UIC1_LCD,
    kind: str,
    value: float,
    status: dict[str, Any],
) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, f"SET {kind}")

    if kind == "FAN":
        current = get_fan_percent(status)
        step = FAN_STEP
        accent = ACCENT
        limits = "0 - 100 %"
    else:
        current = get_speed_percent(status)
        step = SPEED_STEP
        accent = WARN
        limits = "50 - 150 %"

    panel(lcd, 8, 40, 471, 101)
    send_text(lcd, 20, 47, "CURRENT", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 67, f"{current:3.0f} %", 2, WHITE, PANEL)

    panel(lcd, 8, 111, 471, 172)
    send_text(lcd, 22, 120, "TARGET", 1, TEXT_DIM, PANEL)
    send_text(lcd, 22, 137, f"{value:3.0f} %", 2, accent, PANEL)
    send_text(lcd, 170, 141, f"STEP {step:g} %", 1, ACCENT, PANEL)
    lcd.Draw_Rectangle(0, OK, 25, 185, 454, 217)
    send_text(lcd, 45, 194, f"ROTATE = ADJUST   LIMIT {limits}", 1, WHITE, BLACK)
    footer(lcd, "PRESS = APPLY TARGET")
    lcd.UpdateLCD()


def temp_limit(kind: str) -> float:
    return HOTEND_MAX if kind == "HOTEND" else BED_MAX


def get_current_target(status: dict[str, Any], kind: str) -> float:
    if kind == "HOTEND":
        return float(status.get("extruder", {}).get("target", 0))
    return float(status.get("heater_bed", {}).get("target", 0))


def set_temperature(kind: str, value: float) -> None:
    value = max(0.0, min(temp_limit(kind), value))
    if kind == "HOTEND":
        run_gcode(f"M104 S{value:g}")
    else:
        run_gcode(f"M140 S{value:g}")


def draw_temperature(lcd: T5UIC1_LCD, status: dict[str, Any], selected: int) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, "TEMPERATURE")

    ext = status.get("extruder", {})
    bed = status.get("heater_bed", {})

    e_actual = float(ext.get("temperature", 0))
    e_target = float(ext.get("target", 0))
    b_actual = float(bed.get("temperature", 0))
    b_target = float(bed.get("target", 0))

    panel(lcd, 8, 38, 235, 103)
    send_text(lcd, 20, 45, "HOTEND", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 64, f"{e_actual:5.1f} / {e_target:3.0f} C", 2, HOT, PANEL)

    panel(lcd, 245, 38, 471, 103)
    send_text(lcd, 257, 45, "BED", 1, TEXT_DIM, PANEL)
    send_text(lcd, 257, 64, f"{b_actual:5.1f} / {b_target:3.0f} C", 2, WHITE, PANEL)

    for i, item in enumerate(TEMP_ITEMS):
        x = 10 + i * 153
        accent = WARN if item in ("HOTEND", "BED") else ACCENT
        menu_card(lcd, x, 116, 143, 54, item, i == selected, accent)

    footer(lcd, "TURN = SELECT   PRESS = EDIT / ENTER")
    lcd.UpdateLCD()


def draw_temperature_selection(lcd: T5UIC1_LCD, old_selected: int, selected: int) -> None:
    for i in {old_selected, selected}:
        item = TEMP_ITEMS[i]
        x = 10 + i * 153
        accent = WARN if item in ("HOTEND", "BED") else ACCENT
        menu_card(lcd, x, 116, 143, 54, item, i == selected, accent)
    lcd.UpdateLCD()


def draw_temperature_edit(
    lcd: T5UIC1_LCD,
    kind: str,
    value: float,
    status: dict[str, Any],
) -> None:
    lcd.Frame_Clear(BLACK)
    header(lcd, f"SET {kind}")

    actual = (
        float(status.get("extruder", {}).get("temperature", 0))
        if kind == "HOTEND"
        else float(status.get("heater_bed", {}).get("temperature", 0))
    )

    panel(lcd, 8, 40, 471, 101)
    send_text(lcd, 20, 47, "CURRENT", 1, TEXT_DIM, PANEL)
    send_text(lcd, 20, 67, f"{actual:5.1f} C", 2, WHITE, PANEL)

    panel(lcd, 8, 111, 471, 172)
    send_text(lcd, 22, 120, "TARGET", 1, TEXT_DIM, PANEL)
    send_text(lcd, 22, 137, f"{value:5.0f} C", 2, HOT if kind == "HOTEND" else WHITE, PANEL)
    send_text(lcd, 245, 141, f"STEP {TEMP_STEP:g} C", 1, ACCENT, PANEL)

    lcd.Draw_Rectangle(0, OK, 25, 185, 454, 217)
    send_text(lcd, 45, 194, "ROTATE = ADJUST", 1, WHITE, BLACK)

    footer(lcd, "PRESS = APPLY TARGET   LIMITS 250 / 100 C")
    lcd.UpdateLCD()


def perform_prepare_action(action: str) -> None:
    commands = {
        "PLA PREHEAT": "M140 S60\nM104 S200",
        "COOLDOWN": "M104 S0\nM140 S0",
        "HOME ALL": "G28",
        "MOTORS OFF": "M84",
    }
    script = commands.get(action)
    if script is None:
        return
    run_gcode(script)


def main() -> None:
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PIN_A, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(PIN_B, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(PIN_ENT, GPIO.IN, pull_up_down=GPIO.PUD_UP)

    lcd = T5UIC1_LCD(PORT)

    settings = load_settings()
    brightness = int(settings["brightness"])
    idle_timeout = int(settings["timeout"])

    # Always wake the DWIN backlight when the bridge process starts/restarts.
    screen_wake(lcd, brightness)

    # Allow the DWIN panel to settle after handshake/wake before the
    # first framebuffer operation.  This is particularly important when
    # launched automatically by systemd during boot.
    time.sleep(1.0)

    lcd.Frame_SetDir(0)
    time.sleep(0.05)

    page = "main"
    selected = 0
    files: list[str] = []
    last_ab = (GPIO.input(PIN_A), GPIO.input(PIN_B))
    last_ent = GPIO.input(PIN_ENT)
    last_button_ms = 0.0
    redraw = True
    last_refresh = 0.0
    last_status_signature: tuple[Any, ...] | None = None

    # V1.8c diagnostics: measure encoder-to-redraw latency and the time
    # spent actually drawing/updating the DWIN. These are console-only and
    # deliberately do not alter navigation or printer behaviour.
    diag_events = 0
    diag_latency_total = 0.0
    diag_latency_max = 0.0
    diag_raw_changes = 0
    diag_invalid_transitions = 0
    diag_detents = 0
    diag_pending_events: deque[float] = deque()
    diag_draws = 0
    diag_draw_total = 0.0
    diag_draw_max = 0.0
    diag_status_total = 0.0
    diag_status_max = 0.0
    diag_last_report = time.monotonic()
    diag_logging_enabled = bool(settings.get("logging", True))
    diag_log_path = "/tmp/aquila_dwin_diag.log" if diag_logging_enabled else None
    if diag_log_path:
        try:
            with open(diag_log_path, "a", encoding="utf-8") as f:
                f.write(f"\n--- V1.8j3 diagnostics started {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
        except OSError:
            diag_log_path = None
    diag_pending_event_at: float | None = None
    diag_last_loop = time.monotonic()
    diag_loop_max = 0.0
    diag_status_calls = 0
    diag_draw_by_page: dict[str, list[float]] = {}
    diag_partial_draws = 0
    diag_full_draws = 0
    status: dict[str, Any] = {}
    info: dict[str, Any] = {}

    # V1.8h: Moonraker status polling runs outside the UI loop.  The worker
    # owns the HTTP wait; the display loop only takes a short lock to copy
    # the latest cached status.  This prevents slow HTTP responses from
    # blocking encoder polling and DWIN redraws.
    status_lock = threading.Lock()
    status_stop = threading.Event()
    status_cache: dict[str, Any] = {}
    status_cache_updated = 0.0
    status_worker_total = 0.0
    status_worker_max = 0.0
    status_worker_calls = 0

    def status_worker() -> None:
        nonlocal status_cache, status_cache_updated
        nonlocal status_worker_total, status_worker_max, status_worker_calls
        next_poll = 0.0
        while not status_stop.is_set():
            now_worker = time.monotonic()
            if now_worker < next_poll:
                status_stop.wait(min(0.05, next_poll - now_worker))
                continue
            started = time.monotonic()
            try:
                fetched = get_status()
                elapsed = time.monotonic() - started
                with status_lock:
                    status_cache = fetched
                    status_cache_updated = time.monotonic()
                    status_worker_total += elapsed
                    status_worker_max = max(status_worker_max, elapsed)
                    status_worker_calls += 1
            except Exception as exc:
                elapsed = time.monotonic() - started
                with status_lock:
                    status_worker_total += elapsed
                    status_worker_max = max(status_worker_max, elapsed)
                    status_worker_calls += 1
                print(f"Background status failed: {exc}")
            next_poll = time.monotonic() + 1.0

    def get_cached_status() -> dict[str, Any]:
        with status_lock:
            return dict(status_cache)

    status_thread = threading.Thread(target=status_worker, name="moonraker-status", daemon=True)
    status_thread.start()
    confirm_yes = True
    confirm_file = ""
    confirm_action = ""
    confirm_job_action = ""
    running = True

    sleeping = False
    last_activity = time.monotonic()

    move_axis = "X"
    move_step_index = 1  # 1.0 mm default
    jog_selected = 0

    temp_selected = 0
    temp_kind = "HOTEND"
    temp_value = 0.0

    control_selected = 0
    partial_redraw: tuple[str, int, int] | None = None
    control_kind = "FAN"
    control_value = 0.0

    settings_selected = 0
    logging_enabled = bool(settings.get("logging", True))
    settings_kind = "BRIGHTNESS"
    settings_value = BRIGHTNESS_DEFAULT

    leveling_selected = 0
    level_confirm_action = ""
    bed_level_point = 0
    bed_level_action_selected = 0
    bed_level_z = LEVEL_TRAVEL_Z
    bed_level_step_index = 1
    z_level_action_selected = 0
    z_level_z = LEVEL_TRAVEL_Z
    z_level_step_index = 1
    probe_is_detected = False

    # Per-page selection counts. Print-status has dynamic PAUSE/RESUME + CANCEL + BACK.
    print_status_selected = 0

    print("Aquila landscape control v1.8j4 diagnostics")
    if diag_log_path:
        print(f"Diagnostics log: {diag_log_path}")
    print("Ctrl+C to stop")

    try:
        while running:
            ab = (GPIO.input(PIN_A), GPIO.input(PIN_B))
            if ab != last_ab:
                diag_raw_changes += 1
            step = quadrature_step(last_ab, ab)
            if ab != last_ab and step == 0:
                # Count state changes that were not completed detents. This
                # is useful for spotting missed/invalid transitions when a
                # long DWIN redraw blocks the polling loop.
                valid_next = (
                    ((last_ab, ab) in {((1, 1), (0, 1)), ((0, 1), (0, 0)),
                                       ((0, 0), (1, 0)), ((1, 0), (1, 1))})
                    or
                    ((last_ab, ab) in {((1, 1), (1, 0)), ((1, 0), (0, 0)),
                                       ((0, 0), (0, 1)), ((0, 1), (1, 1))})
                )
                if not valid_next:
                    diag_invalid_transitions += 1

            ent = GPIO.input(PIN_ENT)
            now = time.monotonic()
            diag_loop_max = max(diag_loop_max, now - diag_last_loop)
            diag_last_loop = now
            button_press = last_ent == 1 and ent == 0 and (now - last_button_ms) > 0.25

            if step:
                diag_detents += 1
                diag_pending_events.append(now)
            if button_press:
                # Buttons are useful activity markers but are not included in
                # encoder latency statistics.
                pass

            # Any encoder movement or button press wakes the display.
            # The event that wakes it is deliberately consumed, so the first
            # movement does not also change a menu item or trigger an action.
            if sleeping and (step or button_press):
                screen_wake(lcd, brightness)
                sleeping = False
                last_activity = now
                last_refresh = 0.0
                last_status_signature = None
                redraw = True
                print("DWIN wake")
                last_ab = ab
                last_ent = ent
                time.sleep(0.005)
                continue

            if sleeping:
                last_ab = ab
                last_ent = ent
                time.sleep(0.02)
                continue

            if step:
                last_activity = now
                if page == "main":
                    old_selected = selected
                    selected = (selected + step) % len(MAIN_ITEMS)
                    if selected != old_selected and not redraw:
                        partial_redraw = ("main", old_selected, selected)
                elif page == "prepare":
                    old_selected = selected
                    selected = (selected + step) % len(PREPARE_ITEMS)
                    if selected != old_selected and not redraw:
                        partial_redraw = ("prepare", old_selected, selected)
                elif page == "action_confirm":
                    confirm_yes = not confirm_yes
                elif page == "files":
                    selected = max(0, min(len(files), selected + step))
                elif page == "confirm":
                    confirm_yes = not confirm_yes
                elif page == "job_confirm":
                    confirm_yes = not confirm_yes
                elif page == "print_status":
                    state_for_actions = action_state(status)
                    action_count = 3 if state_for_actions in ("printing", "paused") else 1
                    print_status_selected = (print_status_selected + step) % action_count
                elif page == "move_axes":
                    selected = (selected + step) % len(MOVE_AXES)
                elif page == "jog":
                    jog_selected = (jog_selected + step) % 4
                elif page == "temperature":
                    old_temp_selected = temp_selected
                    temp_selected = (temp_selected + step) % len(TEMP_ITEMS)
                    if temp_selected != old_temp_selected and not redraw:
                        partial_redraw = ("temperature", old_temp_selected, temp_selected)
                elif page == "temperature_edit":
                    temp_value = max(
                        0.0,
                        min(temp_limit(temp_kind), temp_value + (step * TEMP_STEP))
                    )
                elif page == "control":
                    old_control_selected = control_selected
                    control_selected = (control_selected + step) % len(CONTROL_ITEMS)
                    if control_selected != old_control_selected and not redraw:
                        partial_redraw = ("control", old_control_selected, control_selected)
                elif page == "control_edit":
                    if control_kind == "FAN":
                        control_value = max(0.0, min(100.0, control_value + step * FAN_STEP))
                    else:
                        control_value = max(
                            SPEED_MIN,
                            min(SPEED_MAX, control_value + step * SPEED_STEP)
                        )
                elif page == "settings":
                    old_settings_selected = settings_selected
                    settings_selected = (settings_selected + step) % len(SETTINGS_ITEMS)
                    if settings_selected != old_settings_selected and not redraw:
                        partial_redraw = ("settings", old_settings_selected, settings_selected)
                elif page == "leveling":
                    old_leveling_selected = leveling_selected
                    leveling_selected = (leveling_selected + step) % len(LEVEL_ITEMS)
                    if leveling_selected != old_leveling_selected and not redraw:
                        partial_redraw = ("leveling", old_leveling_selected, leveling_selected)
                elif page == "level_confirm":
                    confirm_yes = not confirm_yes
                elif page == "bed_level":
                    bed_level_action_selected = (bed_level_action_selected + step) % len(BED_LEVEL_ACTIONS)
                elif page == "z_level":
                    z_level_action_selected = (z_level_action_selected + step) % len(Z_LEVEL_ACTIONS)
                elif page == "settings_edit":
                    if settings_kind == "BRIGHTNESS":
                        idx = BRIGHTNESS_LEVELS.index(int(settings_value))
                        idx = (idx + step) % len(BRIGHTNESS_LEVELS)
                        settings_value = BRIGHTNESS_LEVELS[idx]
                    else:
                        idx = TIMEOUT_LEVELS.index(int(settings_value))
                        idx = (idx + step) % len(TIMEOUT_LEVELS)
                        settings_value = TIMEOUT_LEVELS[idx]

                # Most pages need a complete repaint. CONTROL is the first
                # partial-redraw experiment and only needs its selection cards.
                redraw = True

            last_ab = ab

            if button_press:
                last_activity = now
                last_button_ms = now

                if page == "main":
                    item = MAIN_ITEMS[selected]
                    if item == "PRINT":
                        try:
                            files = get_files()
                        except Exception as exc:
                            print(f"File list failed: {exc}")
                            files = []
                        selected = 0
                        page = "files"
                    elif item == "PREPARE":
                        selected = 0
                        page = "prepare"
                    elif item == "MOVE":
                        selected = 0
                        page = "move_axes"
                    elif item == "TEMPERATURE":
                        temp_selected = 0
                        page = "temperature"
                    elif item == "CONTROL":
                        control_selected = 0
                        page = "control"
                    elif item == "LEVELING":
                        leveling_selected = 0
                        page = "leveling"
                    elif item == "SETTINGS":
                        settings_selected = 0
                        page = "settings"
                    elif item == "STATUS":
                        status = get_cached_status()
                        print_status_selected = 0
                        page = "print_status"
                    elif item == "INFO":
                        try:
                            info = get_info()
                        except Exception as exc:
                            print(f"Info failed: {exc}")
                            info = {"software": "error", "machine": [0, 0, 0]}
                        page = "info"

                elif page == "files":
                    if selected == 0:
                        page = "main"
                        selected = 0
                    elif files:
                        confirm_file = files[selected - 1]
                        confirm_yes = True
                        page = "confirm"

                elif page == "confirm":
                    if confirm_yes:
                        try:
                            start_print(confirm_file)
                            print(f"Started: {confirm_file}")
                            confirm_file = ""
                        except Exception as exc:
                            print(f"Print start failed: {exc}")
                    print_status_selected = 0
                    page = "print_status"

                elif page == "prepare":
                    if selected == 0:
                        page = "main"
                        selected = 0
                    else:
                        confirm_action = PREPARE_ITEMS[selected]
                        confirm_yes = True
                        page = "action_confirm"

                elif page == "action_confirm":
                    if confirm_yes:
                        try:
                            perform_prepare_action(confirm_action)
                            print(f"Prepare action: {confirm_action}")
                        except Exception as exc:
                            print(f"Prepare action failed: {exc}")
                    page = "prepare"
                    selected = 0

                elif page == "print_status":
                    state = action_state(status)
                    if state == "paused":
                        actions = ["RESUME", "CANCEL", "BACK"]
                    elif state == "printing":
                        actions = ["PAUSE", "CANCEL", "BACK"]
                    else:
                        actions = ["BACK"]

                    print_status_selected %= len(actions)
                    action = actions[print_status_selected]
                    if action == "BACK":
                        page = "main"
                        selected = 0
                    else:
                        confirm_job_action = action
                        confirm_yes = False if action == "CANCEL" else True
                        page = "job_confirm"

                elif page == "job_confirm":
                    if confirm_yes:
                        try:
                            if confirm_job_action == "PAUSE":
                                pause_print()
                            elif confirm_job_action == "RESUME":
                                resume_print()
                            elif confirm_job_action == "CANCEL":
                                cancel_print()
                            print(f"Print action: {confirm_job_action}")
                        except Exception as exc:
                            print(f"Print action failed: {exc}")
                    page = "print_status"
                    print_status_selected = 0

                elif page == "temperature":
                    item = TEMP_ITEMS[temp_selected]
                    if item == "BACK":
                        page = "main"
                        selected = MAIN_ITEMS.index("TEMPERATURE")
                    else:
                        temp_kind = item
                        try:
                            temp_value = get_current_target(status, temp_kind)
                        except Exception:
                            temp_value = 0.0
                        page = "temperature_edit"

                elif page == "temperature_edit":
                    try:
                        set_temperature(temp_kind, temp_value)
                        print(f"{temp_kind} target set to {temp_value:g} C")
                    except Exception as exc:
                        print(f"Temperature set failed: {exc}")
                    page = "temperature"
                    temp_selected = TEMP_ITEMS.index(temp_kind)

                elif page == "control":
                    item = CONTROL_ITEMS[control_selected]
                    if item == "BACK":
                        page = "main"
                        selected = MAIN_ITEMS.index("CONTROL")
                    else:
                        control_kind = item
                        control_value = (
                            get_fan_percent(status)
                            if item == "FAN"
                            else get_speed_percent(status)
                        )
                        page = "control_edit"

                elif page == "control_edit":
                    try:
                        if control_kind == "FAN":
                            set_fan_percent(control_value)
                        else:
                            set_speed_percent(control_value)
                        print(f"{control_kind} set to {control_value:g} %")
                    except Exception as exc:
                        print(f"{control_kind} set failed: {exc}")
                    page = "control"
                    control_selected = CONTROL_ITEMS.index(control_kind)

                elif page == "leveling":
                    item = LEVEL_ITEMS[leveling_selected]
                    if item == "BACK":
                        page = "main"
                        selected = MAIN_ITEMS.index("LEVELING")
                    elif item == "MANUAL BED LEVEL":
                        level_confirm_action = "START BED LEVEL"
                        confirm_yes = True
                        page = "level_confirm"
                    elif item == "Z LEVEL (PAPER TEST)":
                        level_confirm_action = "START Z LEVEL"
                        confirm_yes = True
                        page = "level_confirm"
                    elif item == "PROBE SETUP":
                        probe_is_detected = probe_detected()
                        page = "probe_setup"

                elif page == "level_confirm":
                    if confirm_yes:
                        try:
                            if level_confirm_action == "START BED LEVEL":
                                bed_level_point, bed_level_z = start_manual_bed_level()
                                bed_level_action_selected = 0
                                bed_level_step_index = 1
                                page = "bed_level"
                            else:
                                z_level_z = start_z_level()
                                z_level_action_selected = 0
                                z_level_step_index = 1
                                page = "z_level"
                        except Exception as exc:
                            print(f"Leveling start failed: {exc}")
                            page = "leveling"
                    else:
                        page = "leveling"

                elif page == "bed_level":
                    action = BED_LEVEL_ACTIONS[bed_level_action_selected]
                    if action == "Z -":
                        bed_level_z = bed_level_z_move(-LEVEL_STEPS[bed_level_step_index], bed_level_z)
                    elif action == "Z +":
                        bed_level_z = bed_level_z_move(LEVEL_STEPS[bed_level_step_index], bed_level_z)
                    elif action == "STEP":
                        bed_level_step_index = (bed_level_step_index + 1) % len(LEVEL_STEPS)
                    elif action == "NEXT":
                        bed_level_point = (bed_level_point + 1) % len(BED_LEVEL_POINTS)
                        try:
                            move_bed_level_point(bed_level_point, bed_level_z)
                        except Exception as exc:
                            print(f"Bed-level move failed: {exc}")
                    elif action == "DONE":
                        finish_leveling()
                        page = "leveling"
                        leveling_selected = 1

                elif page == "z_level":
                    action = Z_LEVEL_ACTIONS[z_level_action_selected]
                    if action == "Z -":
                        z_level_z = bed_level_z_move(-LEVEL_STEPS[z_level_step_index], z_level_z)
                    elif action == "Z +":
                        z_level_z = bed_level_z_move(LEVEL_STEPS[z_level_step_index], z_level_z)
                    elif action == "STEP":
                        z_level_step_index = (z_level_step_index + 1) % len(LEVEL_STEPS)
                    elif action == "DONE":
                        finish_leveling()
                        page = "leveling"
                        leveling_selected = 2

                elif page == "probe_setup":
                    page = "leveling"
                    leveling_selected = 3

                elif page == "settings":
                    item = SETTINGS_ITEMS[settings_selected]
                    if item == "BACK":
                        page = "main"
                        selected = MAIN_ITEMS.index("SETTINGS")
                    elif item == "LOGGING":
                        logging_enabled = not logging_enabled
                        settings["logging"] = logging_enabled
                        save_settings(settings)
                        diag_logging_enabled = logging_enabled
                        diag_log_path = "/tmp/aquila_dwin_diag.log" if logging_enabled else None
                        diag_last_report = time.monotonic()
                        diag_events = 0
                        diag_latency_total = 0.0
                        diag_latency_max = 0.0
                        diag_raw_changes = 0
                        diag_invalid_transitions = 0
                        diag_detents = 0
                        diag_pending_events.clear()
                        diag_draws = 0
                        diag_draw_total = 0.0
                        diag_draw_max = 0.0
                        diag_status_total = 0.0
                        diag_status_max = 0.0
                        diag_status_calls = 0
                        diag_loop_max = 0.0
                        diag_draw_by_page.clear()
                        diag_partial_draws = 0
                        diag_full_draws = 0
                        if logging_enabled:
                            try:
                                with open(diag_log_path, "a", encoding="utf-8") as f:
                                    f.write(f"\n--- V1.8j3 diagnostics logging enabled {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
                            except OSError:
                                diag_log_path = None
                            print("Diagnostics logging ON", flush=True)
                        else:
                            print("Diagnostics logging OFF", flush=True)
                        # J4: the logging switch is an isolated UI change.
                        # Redraw only the Settings card so the toggle flips
                        # immediately without repainting the whole page.
                        partial_redraw = ("settings", settings_selected, settings_selected)
                    else:
                        settings_kind = item
                        if item == "BRIGHTNESS":
                            settings_value = int(brightness)
                        else:
                            settings_value = int(idle_timeout)
                        page = "settings_edit"

                elif page == "settings_edit":
                    if settings_kind == "BRIGHTNESS":
                        brightness = int(settings_value)
                        settings["brightness"] = brightness
                        screen_wake(lcd, brightness)
                    else:
                        idle_timeout = int(settings_value)
                        settings["timeout"] = idle_timeout

                    save_settings(settings)
                    page = "settings"
                    settings_selected = SETTINGS_ITEMS.index(settings_kind)

                elif page == "move_axes":
                    axis = MOVE_AXES[selected]
                    if axis == "BACK":
                        page = "main"
                        selected = 0
                    else:
                        move_axis = axis
                        jog_selected = 0
                        page = "jog"

                elif page == "jog":
                    action = ["- MOVE", "+ MOVE", "STEP", "BACK"][jog_selected]
                    if action == "BACK":
                        page = "move_axes"
                        selected = MOVE_AXES.index(move_axis)
                    elif action == "STEP":
                        move_step_index = (move_step_index + 1) % len(MOVE_STEPS)
                    elif action == "- MOVE":
                        try:
                            jog_axis(move_axis, -MOVE_STEPS[move_step_index])
                            print(f"Jog {move_axis} {-MOVE_STEPS[move_step_index]:g} mm")
                        except Exception as exc:
                            print(f"Jog failed: {exc}")
                    elif action == "+ MOVE":
                        try:
                            jog_axis(move_axis, MOVE_STEPS[move_step_index])
                            print(f"Jog {move_axis} +{MOVE_STEPS[move_step_index]:g} mm")
                        except Exception as exc:
                            print(f"Jog failed: {exc}")

                elif page == "info":
                    page = "main"
                    selected = 0

                redraw = True
            last_ent = ent

            # Backlight sleep is based on user inactivity, not sensor changes.
            timeout = (
                DISPLAY_TIMEOUT_PRINTING
                if action_state(status) in ("printing", "paused")
                else idle_timeout
            )
            if timeout > 0 and (now - last_activity) >= timeout:
                screen_sleep(lcd)
                sleeping = True
                print(f"DWIN sleep after {timeout:.0f}s idle")
                last_refresh = now
                time.sleep(0.02)
                continue

            if now - last_refresh >= 1.0:
                try:
                    # V1.8j: never wait for Moonraker here.  Take the latest
                    # snapshot produced by the background worker.
                    new_status = get_cached_status()

                    ext_t = float(new_status.get("extruder", {}).get("temperature", 0))
                    ext_s = float(new_status.get("extruder", {}).get("target", 0))
                    bed_t = float(new_status.get("heater_bed", {}).get("temperature", 0))
                    bed_s = float(new_status.get("heater_bed", {}).get("target", 0))
                    pos = new_status.get("toolhead", {}).get("position", [0, 0, 0, 0])
                    p = [float(v) for v in pos[:3]]
                    state_now = str(new_status.get("print_stats", {}).get("state", "standby")).lower()
                    name_now = str(new_status.get("print_stats", {}).get("filename", ""))
                    progress_now = float(new_status.get("virtual_sdcard", {}).get("progress", 0)) * 100
                    elapsed_now = float(new_status.get("print_stats", {}).get("print_duration", 0))
                    fan_now = get_fan_percent(new_status)
                    speed_now = get_speed_percent(new_status)

                    sig = (
                        f"{ext_t:.1f}",
                        f"{ext_s:.0f}",
                        f"{bed_t:.1f}",
                        f"{bed_s:.0f}",
                        tuple(f"{v:.1f}" for v in p),
                        state_now,
                        name_now,
                        f"{progress_now:.1f}",
                        f"{elapsed_now:.0f}" if page == "print_status" else "",
                        f"{fan_now:.0f}" if page == "control" else "",
                        f"{speed_now:.0f}" if page == "control" else "",
                    )

                    previous_state = action_state(status)
                    status = new_status

                    # Automatically enter print status when a job starts or becomes paused.
                    if page in ("main", "files") and state_now in ("printing", "paused"):
                        if previous_state not in ("printing", "paused"):
                            print_status_selected = 0
                            page = "print_status"
                            redraw = True

                    if sig != last_status_signature:
                        last_status_signature = sig
                        if page == "main":
                            # V1.8j2: only use the partial status redraw when
                            # the screen is already fully rendered.  On startup
                            # and after wake, redraw=True must be allowed to
                            # perform the complete home-screen draw first.
                            if not redraw:
                                if not partial_redraw:
                                    partial_redraw = ("main_status", 0, 0)
                            redraw = True
                        elif page in ("print_status", "bed_level", "z_level"):
                            redraw = True

                except Exception as exc:
                    print(f"Status cache failed: {exc}")

                last_refresh = now

            if redraw and not sleeping:
                draw_started = time.monotonic()
                draw_page = page
                draw_mode = "full"
                if page == "main":
                    if partial_redraw and partial_redraw[0] == "main":
                        draw_mode = "partial"
                        draw_main_selection(lcd, partial_redraw[1], partial_redraw[2])
                    elif partial_redraw and partial_redraw[0] == "main_status":
                        draw_mode = "partial"
                        draw_main_status(lcd, status)
                    else:
                        draw_main(lcd, status, selected)
                elif page == "files":
                    draw_files(lcd, files, selected)
                elif page == "confirm":
                    draw_confirm(lcd, confirm_file, confirm_yes)
                elif page == "prepare":
                    if partial_redraw and partial_redraw[0] == "prepare":
                        draw_prepare_selection(lcd, partial_redraw[1], partial_redraw[2])
                    else:
                        draw_prepare(lcd, selected)
                elif page == "temperature":
                    if partial_redraw and partial_redraw[0] == "temperature":
                        draw_temperature_selection(lcd, partial_redraw[1], partial_redraw[2])
                    else:
                        draw_temperature(lcd, status, temp_selected)
                elif page == "temperature_edit":
                    draw_temperature_edit(lcd, temp_kind, temp_value, status)
                elif page == "control":
                    if partial_redraw and partial_redraw[0] == "control":
                        draw_mode = "partial"
                        draw_control_selection(lcd, partial_redraw[1], partial_redraw[2])
                    else:
                        draw_control(lcd, status, control_selected)
                elif page == "control_edit":
                    draw_control_edit(lcd, control_kind, control_value, status)
                elif page == "leveling":
                    if partial_redraw and partial_redraw[0] == "leveling":
                        draw_leveling_selection(lcd, partial_redraw[1], partial_redraw[2])
                    else:
                        draw_leveling(lcd, leveling_selected)
                elif page == "level_confirm":
                    draw_action_confirm(lcd, level_confirm_action, confirm_yes)
                elif page == "bed_level":
                    draw_bed_level(lcd, bed_level_point, bed_level_action_selected,
                                   bed_level_z, bed_level_step_index)
                elif page == "z_level":
                    draw_z_level(lcd, z_level_action_selected, z_level_z, z_level_step_index)
                elif page == "probe_setup":
                    draw_probe_setup(lcd, probe_is_detected)
                elif page == "settings":
                    if partial_redraw and partial_redraw[0] == "settings":
                        draw_settings_selection(lcd, partial_redraw[1], partial_redraw[2], logging_enabled)
                    else:
                        draw_settings(lcd, brightness, idle_timeout, settings_selected, logging_enabled)
                elif page == "settings_edit":
                    draw_settings_edit(lcd, settings_kind, settings_value)
                elif page == "move_axes":
                    draw_move_axes(lcd, selected)
                elif page == "jog":
                    draw_jog(lcd, move_axis, move_step_index, jog_selected)
                elif page == "action_confirm":
                    draw_action_confirm(lcd, confirm_action, confirm_yes)
                elif page == "print_status":
                    draw_print_status(lcd, status, print_status_selected)
                elif page == "job_confirm":
                    lcd.Frame_Clear(BLACK)
                    header(lcd, "PRINT")
                    panel(lcd, 8, 42, 471, 122)
                    send_text(lcd, 20, 51, "CONFIRM ACTION", 1, TEXT_DIM, PANEL)
                    action_fg = DANGER if confirm_job_action == "CANCEL" else WARN
                    send_text(lcd, 20, 73, confirm_job_action, 2, action_fg, PANEL)
                    prompt = (
                        "STOP THIS PRINT?" if confirm_job_action == "CANCEL"
                        else "Pause the current print?" if confirm_job_action == "PAUSE"
                        else "Resume the current print?"
                    )
                    send_text(lcd, 20, 101, prompt, 1, WHITE, PANEL)
                    for i, label in enumerate(("YES", "NO")):
                        sel = (i == 0 and confirm_yes) or (i == 1 and not confirm_yes)
                        x = 55 + i * 220
                        bg = PANEL2 if sel else BLACK
                        border = OK if sel else GREY
                        lcd.Draw_Rectangle(1, bg, x, 140, x + 165, 198)
                        lcd.Draw_Rectangle(0, border, x, 140, x + 165, 198)
                        send_text(lcd, x + 58, 160, label, 2, WHITE, bg)
                    footer(lcd, "TURN = SELECT   PRESS = CONFIRM")
                    lcd.UpdateLCD()
                elif page == "info":
                    draw_info(lcd, info)

                draw_elapsed = time.monotonic() - draw_started
                diag_draws += 1
                diag_draw_total += draw_elapsed
                diag_draw_max = max(diag_draw_max, draw_elapsed)
                page_stats = diag_draw_by_page.setdefault(draw_page, [])
                page_stats.append(draw_elapsed)
                if draw_mode == "partial":
                    diag_partial_draws += 1
                else:
                    diag_full_draws += 1
                # Associate each completed encoder detent with the next
                # completed redraw. If several detents arrived while the
                # display was busy, record latency for each one.
                redraw_finished = time.monotonic()
                while diag_pending_events:
                    event_at = diag_pending_events.popleft()
                    latency = redraw_finished - event_at
                    diag_events += 1
                    diag_latency_total += latency
                    diag_latency_max = max(diag_latency_max, latency)
                partial_redraw = None
                redraw = False

            # Print a compact diagnostic report every 10 seconds. This makes
            # it possible to distinguish encoder polling lag from DWIN redraw
            # time and from Moonraker status-request stalls.
            now_diag = time.monotonic()
            if diag_logging_enabled and now_diag - diag_last_report >= 10.0:
                avg_latency = (diag_latency_total / diag_events * 1000) if diag_events else 0.0
                avg_draw = (diag_draw_total / diag_draws * 1000) if diag_draws else 0.0
                avg_status = (diag_status_total / max(1, int((now_diag - diag_last_report) + 0.5))) * 1000
                avg_status = (diag_status_total / diag_status_calls * 1000) if diag_status_calls else 0.0
                with status_lock:
                    worker_calls = status_worker_calls
                    worker_total = status_worker_total
                    worker_max = status_worker_max
                worker_avg = (worker_total / worker_calls * 1000) if worker_calls else 0.0
                page_bits = []
                for page_name, samples in sorted(diag_draw_by_page.items()):
                    page_avg = sum(samples) / len(samples) * 1000
                    page_max = max(samples) * 1000
                    page_bits.append(f"{page_name}={page_avg:.1f}/{page_max:.1f}")
                pages_text = ",".join(page_bits) if page_bits else "none"
                diag_line = (
                    f"{time.strftime('%H:%M:%S')} DIAG  detents={diag_detents} raw={diag_raw_changes} invalid={diag_invalid_transitions} "
                    f"lat={avg_latency:.1f}/{diag_latency_max*1000:.1f}ms "
                    f"draw={avg_draw:.1f}/{diag_draw_max*1000:.1f}ms "
                    f"status_ui={avg_status:.1f}/{diag_status_max*1000:.1f}ms calls={diag_status_calls} "
                    f"worker={worker_avg:.1f}/{worker_max*1000:.1f}ms calls={worker_calls} "
                    f"loopmax={diag_loop_max*1000:.1f}ms partial={diag_partial_draws} full={diag_full_draws} "
                    f"pages[{pages_text}]"
                )
                print(diag_line, flush=True)
                if diag_log_path:
                    try:
                        with open(diag_log_path, "a", encoding="utf-8") as f:
                            f.write(diag_line + "\n")
                    except OSError:
                        pass
                diag_events = 0
                diag_latency_total = 0.0
                diag_latency_max = 0.0
                diag_raw_changes = 0
                diag_invalid_transitions = 0
                diag_detents = 0
                diag_pending_events.clear()
                diag_draws = 0
                diag_draw_total = 0.0
                diag_draw_max = 0.0
                diag_status_total = 0.0
                diag_status_max = 0.0
                diag_status_calls = 0
                with status_lock:
                    status_worker_total = 0.0
                    status_worker_max = 0.0
                    status_worker_calls = 0
                diag_loop_max = 0.0
                diag_draw_by_page.clear()
                diag_partial_draws = 0
                diag_full_draws = 0
                diag_last_report = now_diag

            time.sleep(0.005)

    except KeyboardInterrupt:
        status_stop.set()
        status_thread.join(timeout=1.5)
        try:
            run_gcode("M104 S0\nM140 S0")
            print("Ctrl+C: heaters turned off.")
        except Exception as exc:
            print(f"Cool-down on exit failed: {exc}")
    finally:
        GPIO.cleanup()
        print("Aquila DWIN control stopped.")


if __name__ == "__main__":
    main()
