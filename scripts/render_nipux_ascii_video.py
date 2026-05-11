#!/usr/bin/env python3
"""Render a Nipux ASCII-art CLI intro as an MP4.

The renderer is dependency-light on purpose: it draws a small embedded
bitmap font into raw RGB frames and pipes those frames directly to ffmpeg.
"""

from __future__ import annotations

import argparse
import math
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


WIDTH = 1440
HEIGHT = 900
FPS = 30
DURATION = 8.0
COLS = 96
ROWS = 34
CELL_W = 13
CELL_H = 22
ORIGIN_X = (WIDTH - COLS * CELL_W) // 2
ORIGIN_Y = (HEIGHT - ROWS * CELL_H) // 2
SCALE = 2

BG = (2, 5, 5)
BG_SCAN = (1, 3, 4)
PANEL = (4, 10, 8)
PANEL_EDGE = (10, 56, 37)
PANEL_GLOW = (4, 30, 24)
DIM = (33, 92, 62)
MID = (72, 180, 112)
MAIN = (126, 255, 164)
HOT = (104, 238, 255)
AMBER = (255, 184, 70)
MAGENTA = (255, 95, 154)
WHITE = (220, 255, 238)

LOGO = [
    r"##   ##  ####  ######  ##   ##  ##   ##",
    r"###  ##   ##   ##   ## ##   ##   ## ## ",
    r"#### ##   ##   ##   ## ##   ##    ###  ",
    r"## ####   ##   ######  ##   ##    ###  ",
    r"##  ###   ##   ##      ##   ##   ## ## ",
    r"##   ##  ####  ##       #####   ##   ##",
]

GLITCH_CHARS = ".:-=+*#%@/\\|<>[]{}01"
RAIN_CHARS = ".:-+*#01/\\<>"


GLYPHS: dict[str, tuple[str, ...]] = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01111", "10000", "10000", "10011", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "a": ("00000", "00000", "01110", "00001", "01111", "10001", "01111"),
    "b": ("10000", "10000", "10110", "11001", "10001", "10001", "11110"),
    "c": ("00000", "00000", "01110", "10000", "10000", "10001", "01110"),
    "d": ("00001", "00001", "01101", "10011", "10001", "10001", "01111"),
    "e": ("00000", "00000", "01110", "10001", "11111", "10000", "01110"),
    "f": ("00110", "01001", "01000", "11100", "01000", "01000", "01000"),
    "g": ("00000", "01111", "10001", "10001", "01111", "00001", "01110"),
    "h": ("10000", "10000", "10110", "11001", "10001", "10001", "10001"),
    "i": ("00100", "00000", "01100", "00100", "00100", "00100", "01110"),
    "j": ("00010", "00000", "00110", "00010", "00010", "10010", "01100"),
    "k": ("10000", "10000", "10010", "10100", "11000", "10100", "10010"),
    "l": ("01100", "00100", "00100", "00100", "00100", "00100", "01110"),
    "m": ("00000", "00000", "11010", "10101", "10101", "10101", "10101"),
    "n": ("00000", "00000", "10110", "11001", "10001", "10001", "10001"),
    "o": ("00000", "00000", "01110", "10001", "10001", "10001", "01110"),
    "p": ("00000", "00000", "11110", "10001", "11110", "10000", "10000"),
    "q": ("00000", "00000", "01101", "10011", "01111", "00001", "00001"),
    "r": ("00000", "00000", "10110", "11001", "10000", "10000", "10000"),
    "s": ("00000", "00000", "01111", "10000", "01110", "00001", "11110"),
    "t": ("01000", "01000", "11100", "01000", "01000", "01001", "00110"),
    "u": ("00000", "00000", "10001", "10001", "10001", "10011", "01101"),
    "v": ("00000", "00000", "10001", "10001", "10001", "01010", "00100"),
    "w": ("00000", "00000", "10001", "10001", "10101", "10101", "01010"),
    "x": ("00000", "00000", "10001", "01010", "00100", "01010", "10001"),
    "y": ("00000", "00000", "10001", "10001", "01111", "00001", "01110"),
    "z": ("00000", "00000", "11111", "00010", "00100", "01000", "11111"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ",": ("00000", "00000", "00000", "00000", "00000", "01100", "01000"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    ";": ("00000", "01100", "01100", "00000", "01100", "01000", "10000"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    "'": ("00100", "00100", "01000", "00000", "00000", "00000", "00000"),
    '"': ("01010", "01010", "01010", "00000", "00000", "00000", "00000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    "*": ("00000", "10101", "01110", "11111", "01110", "10101", "00000"),
    "#": ("01010", "11111", "01010", "01010", "11111", "01010", "01010"),
    "@": ("01110", "10001", "10111", "10101", "10111", "10000", "01110"),
    "%": ("11001", "11010", "00100", "01000", "10110", "00110", "00000"),
    "$": ("00100", "01111", "10100", "01110", "00101", "11110", "00100"),
    "&": ("01100", "10010", "10100", "01000", "10101", "10010", "01101"),
    "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
    "\\": ("10000", "01000", "00100", "00010", "00001", "00000", "00000"),
    "|": ("00100", "00100", "00100", "00100", "00100", "00100", "00100"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "[": ("01110", "01000", "01000", "01000", "01000", "01000", "01110"),
    "]": ("01110", "00010", "00010", "00010", "00010", "00010", "01110"),
    "{": ("00010", "00100", "00100", "01000", "00100", "00100", "00010"),
    "}": ("01000", "00100", "00100", "00010", "00100", "00100", "01000"),
    "~": ("00000", "00000", "01001", "10110", "00000", "00000", "00000"),
    "^": ("00100", "01010", "10001", "00000", "00000", "00000", "00000"),
}


@dataclass(frozen=True)
class Cell:
    char: str = " "
    color: tuple[int, int, int] = DIM


class TextGrid:
    def __init__(self) -> None:
        self.cells = [[Cell() for _ in range(COLS)] for _ in range(ROWS)]

    def set(self, x: int, y: int, char: str, color: tuple[int, int, int]) -> None:
        if 0 <= x < COLS and 0 <= y < ROWS and char:
            self.cells[y][x] = Cell(char[0], color)

    def put(self, x: int, y: int, text: str, color: tuple[int, int, int]) -> None:
        for offset, char in enumerate(text):
            self.set(x + offset, y, char, color)

    def center(self, y: int, text: str, color: tuple[int, int, int]) -> None:
        self.put((COLS - len(text)) // 2, y, text, color)

    def box(self, title: str, color: tuple[int, int, int]) -> None:
        top = "+" + "-" * (COLS - 2) + "+"
        bottom = "+" + "-" * (COLS - 2) + "+"
        self.put(0, 0, top, color)
        self.put(0, ROWS - 1, bottom, color)
        for y in range(1, ROWS - 1):
            self.set(0, y, "|", color)
            self.set(COLS - 1, y, "|", color)
        label = f" {title} "
        self.put(3, 0, label[: COLS - 8], color)


def clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def ease(value: float) -> float:
    value = clamp(value)
    return value * value * (3.0 - 2.0 * value)


def mix(a: tuple[int, int, int], b: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    amount = clamp(amount)
    return tuple(int(a[i] + (b[i] - a[i]) * amount) for i in range(3))


def logo_origin() -> tuple[int, int]:
    max_width = max(len(line) for line in LOGO)
    return (COLS - max_width) // 2, 8


def put_logo(grid: TextGrid, frame: int, reveal: float, stable: bool = False) -> None:
    left, top = logo_origin()
    for y, line in enumerate(LOGO):
        for x, char in enumerate(line):
            if char == " ":
                continue
            rng = random.Random(frame * 1009 + x * 97 + y * 53)
            if stable or rng.random() < reveal:
                shown = char
                color = HOT if stable or rng.random() > 0.18 else AMBER
                if stable and rng.random() < 0.015:
                    shown = rng.choice("*+#")
                    color = WHITE
                elif not stable and rng.random() > reveal + 0.18:
                    shown = rng.choice(GLITCH_CHARS)
                    color = MAGENTA
                grid.set(left + x, top + y, shown, color)
            elif rng.random() < 0.08 + 0.22 * reveal:
                grid.set(left + x, top + y, rng.choice(GLITCH_CHARS), mix(DIM, HOT, 0.35))


def put_collapsing_logo(grid: TextGrid, frame: int, progress: float) -> None:
    left, top = logo_origin()
    cursor_y = 24
    glyph_index = 0
    for y, line in enumerate(LOGO):
        for x, char in enumerate(line):
            if char == " ":
                continue
            rng = random.Random(frame * 1493 + x * 31 + y * 41)
            target_x = 8 + (glyph_index % 30)
            target_y = cursor_y + (glyph_index // 30) % 2
            wobble = math.sin(frame * 0.35 + glyph_index * 0.9) * (1.0 - progress) * 2.0
            px = round((left + x) * (1.0 - progress) + target_x * progress + wobble)
            py = round((top + y) * (1.0 - progress) + target_y * progress)
            shown = char if progress < 0.68 else rng.choice("nipux$>_-/")
            color = mix(HOT, MAIN, progress)
            if rng.random() < 0.08:
                shown = rng.choice(GLITCH_CHARS)
                color = MAGENTA
            grid.set(px, py, shown, color)
            glyph_index += 1


def put_progress_bar(grid: TextGrid, x: int, y: int, width: int, progress: float) -> None:
    progress = clamp(progress)
    filled = int(round(width * progress))
    bar = "[" + "#" * filled + "-" * (width - filled) + "]"
    grid.put(x, y, bar, MID)
    grid.put(x + 1, y, "#" * filled, HOT if progress > 0.88 else MAIN)
    grid.put(x + width + 4, y, f"{int(progress * 100):03d}%", AMBER if progress < 1.0 else HOT)


def put_rain(grid: TextGrid, frame: int, intensity: float) -> None:
    if intensity <= 0:
        return
    for x in range(2, COLS - 2):
        rng = random.Random(9001 + x * 113)
        stream_speed = 1 + rng.randint(0, 2)
        head = (frame * stream_speed + rng.randint(0, ROWS * 3)) % (ROWS + 16) - 8
        for trail in range(5):
            y = head - trail
            if 2 <= y < ROWS - 2 and random.Random(frame * 313 + x * 17 + trail).random() < intensity:
                char = random.Random(frame * 997 + x * 19 + trail * 11).choice(RAIN_CHARS)
                fade = max(0.18, 1.0 - trail * 0.18)
                color = mix((8, 28, 20), MID, fade * intensity)
                grid.set(x, y, char, color)


def put_boot_lines(grid: TextGrid, frame: int, t: float) -> None:
    lines = [
        "$ nipux video --ascii --into-cli",
        "[scan] terminal cells online",
        "[map ] routing logo glyphs",
        "[sync] prompt target locked",
    ]
    start_y = 22
    for i, line in enumerate(lines):
        reveal = int(clamp((t - 0.12 - i * 0.22) / 0.32) * len(line))
        color = MAIN if i == 0 else MID
        grid.put(7, start_y + i, line[:reveal], color)
    put_progress_bar(grid, 7, 28, 38, clamp(t / 1.12))


def put_cli(grid: TextGrid, frame: int, t: float) -> None:
    command = "$ nipux enter --render ascii"
    start = 4.78
    typed = int(clamp((t - start) / 1.08) * len(command))
    grid.put(7, 24, command[:typed], MAIN)
    cursor_x = 7 + typed
    if frame // 8 % 2 == 0 and typed < len(command):
        grid.set(cursor_x, 24, "_", HOT)

    if t > 5.9:
        grid.put(7, 26, "[ok] word packed into cli prompt", MID)
    if t > 6.26:
        grid.put(7, 27, "[ok] ascii signal clean", MID)
    if t > 6.58:
        put_progress_bar(grid, 7, 29, 42, clamp((t - 6.55) / 0.62))
    if t > 7.18:
        final = "nipux> "
        grid.put(7, 31, final, HOT)
        if frame // 10 % 2 == 0:
            grid.set(7 + len(final), 31, "_", HOT)


def build_grid(frame: int, total_frames: int) -> TextGrid:
    t = frame / FPS
    grid = TextGrid()
    grid.box("nipux ascii cli capture", DIM)
    grid.put(COLS - 25, 0, " render:rawrgb->mp4 ", DIM)
    grid.put(4, 2, "MODE ASCII/CLI", MID)
    grid.put(COLS - 23, 2, f"FRAME {frame:03d}/{total_frames - 1:03d}", DIM)

    rain_intensity = 0.36
    if t > 5.0:
        rain_intensity *= 0.35
    put_rain(grid, frame, rain_intensity)

    if t < 1.18:
        put_boot_lines(grid, frame, t)
    elif t < 2.75:
        put_boot_lines(grid, frame, 1.18)
        reveal = ease((t - 1.18) / 1.42)
        put_logo(grid, frame, reveal)
        grid.center(16, "glyphs are snapping into nipux", DIM)
    elif t < 3.72:
        put_logo(grid, frame, 1.0, stable=True)
        grid.center(16, "nipux", WHITE if frame // 7 % 2 == 0 else HOT)
        grid.center(18, "pressing the word into a command line", DIM)
    elif t < 5.08:
        progress = ease((t - 3.72) / 1.36)
        put_collapsing_logo(grid, frame, progress)
        grid.put(7, 24, "$ ", MAIN)
        if progress > 0.45:
            partial = "nipux"[: int((progress - 0.45) / 0.55 * 5)]
            grid.put(9, 24, partial, HOT)
        grid.center(18, "collapsing ascii mass -> cli input", AMBER)
    else:
        put_cli(grid, frame, t)

    if t > 4.8:
        grid.put(COLS - 27, 30, "STATUS: PROMPT CONTROL", DIM)
    elif t > 2.0:
        grid.put(COLS - 24, 30, "STATUS: GLYPH LOCK", DIM)
    else:
        grid.put(COLS - 23, 30, "STATUS: BOOT RAIL", DIM)

    return grid


def draw_rect(buf: bytearray, x: int, y: int, w: int, h: int, color: tuple[int, int, int]) -> None:
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(WIDTH, x + w)
    y1 = min(HEIGHT, y + h)
    if x0 >= x1 or y0 >= y1:
        return
    row = bytes(color) * (x1 - x0)
    for py in range(y0, y1):
        start = (py * WIDTH + x0) * 3
        buf[start : start + len(row)] = row


def build_base_frame() -> bytearray:
    buf = bytearray(WIDTH * HEIGHT * 3)
    for y in range(HEIGHT):
        color = BG_SCAN if y % 4 == 0 else BG
        row = bytes(color) * WIDTH
        start = y * WIDTH * 3
        buf[start : start + len(row)] = row

    panel_x = ORIGIN_X - 28
    panel_y = ORIGIN_Y - 28
    panel_w = COLS * CELL_W + 56
    panel_h = ROWS * CELL_H + 56
    draw_rect(buf, panel_x - 8, panel_y - 8, panel_w + 16, panel_h + 16, PANEL_GLOW)
    draw_rect(buf, panel_x, panel_y, panel_w, panel_h, PANEL)
    draw_rect(buf, panel_x, panel_y, panel_w, 2, PANEL_EDGE)
    draw_rect(buf, panel_x, panel_y + panel_h - 2, panel_w, 2, PANEL_EDGE)
    draw_rect(buf, panel_x, panel_y, 2, panel_h, PANEL_EDGE)
    draw_rect(buf, panel_x + panel_w - 2, panel_y, 2, panel_h, PANEL_EDGE)

    for y in range(panel_y + 36, panel_y + panel_h - 20, 44):
        draw_rect(buf, panel_x + 18, y, panel_w - 36, 1, (5, 24, 18))
    return buf


BASE_FRAME = build_base_frame()


def glyph_for(char: str) -> tuple[str, ...]:
    return GLYPHS.get(char, GLYPHS.get(char.upper(), GLYPHS["?"]))


GLYPHS["?"] = GLYPHS["?"] if "?" in GLYPHS else ("01110", "10001", "00010", "00100", "00100", "00000", "00100")


def draw_glyph(buf: bytearray, char: str, x: int, y: int, color: tuple[int, int, int], glow: bool) -> None:
    glyph = glyph_for(char)
    if glyph is GLYPHS[" "]:
        return
    if glow:
        glow_color = tuple(max(0, int(c * 0.16)) for c in color)
    for row_i, row in enumerate(glyph):
        for col_i, bit in enumerate(row):
            if bit != "1":
                continue
            px = x + col_i * SCALE
            py = y + row_i * SCALE
            if glow:
                draw_rect(buf, px - 1, py - 1, SCALE + 2, SCALE + 2, glow_color)
            draw_rect(buf, px, py, SCALE, SCALE, color)


def render_frame(frame: int, total_frames: int) -> bytes:
    grid = build_grid(frame, total_frames)
    buf = bytearray(BASE_FRAME)
    jitter = 1 if frame % 37 == 0 else 0
    for y, row in enumerate(grid.cells):
        for x, cell in enumerate(row):
            if cell.char == " ":
                continue
            px = ORIGIN_X + x * CELL_W + 1 + jitter
            py = ORIGIN_Y + y * CELL_H + 4
            bright = sum(cell.color) > 420
            draw_glyph(buf, cell.char, px, py, cell.color, bright)

    # A light CRT sweep, sparse enough to keep text readable.
    sweep_y = int((frame * 9) % HEIGHT)
    draw_rect(buf, 0, sweep_y, WIDTH, 2, (4, 18, 16))
    return bytes(buf)


def render_video(output: Path, poster: Path | None) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SystemExit("ffmpeg was not found on PATH")

    output.parent.mkdir(parents=True, exist_ok=True)
    total_frames = int(FPS * DURATION)
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    assert process.stdin is not None
    for frame in range(total_frames):
        process.stdin.write(render_frame(frame, total_frames))
        if frame % FPS == 0:
            print(f"rendered {frame // FPS:02d}s/{int(DURATION):02d}s")
    process.stdin.close()
    stderr = process.stderr.read().decode("utf-8", errors="replace") if process.stderr else ""
    return_code = process.wait()
    if return_code != 0:
        raise SystemExit(f"ffmpeg failed with exit code {return_code}\n{stderr}")

    if poster:
        poster.parent.mkdir(parents=True, exist_ok=True)
        poster_cmd = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            "00:00:03.05",
            "-i",
            str(output),
            "-frames:v",
            "1",
            str(poster),
        ]
        subprocess.run(poster_cmd, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render the Nipux ASCII CLI intro video.")
    parser.add_argument("--output", type=Path, default=Path("docs/nipux_ascii_cli.mp4"))
    parser.add_argument("--poster", type=Path, default=Path("docs/nipux_ascii_cli_poster.png"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    render_video(args.output, args.poster)
    print(f"video:  {args.output}")
    print(f"poster: {args.poster}")


if __name__ == "__main__":
    main()
