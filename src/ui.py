"""
Kitty-optimised terminal renderer using blessed.
24-bit colour, Unicode box-drawing, animations, progress bars.
"""
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import blessed

from src.protocol import HistoryEntry, Request
from src.server import PendingItem, RequestQueue

term = blessed.Terminal()

# ── palette ──────────────────────────────────────────────────────────────────
BG        = term.on_color_rgb(13,  17,  23)
BG2       = term.on_color_rgb(22,  27,  34)
BG3       = term.on_color_rgb(33,  38,  45)
FG        = term.color_rgb(230, 237, 243)
DIM       = term.color_rgb(110, 118, 129)
BLUE      = term.color_rgb(88,  166, 255)
GREEN     = term.color_rgb(63,  185, 80)
RED       = term.color_rgb(248, 81,  73)
YELLOW    = term.color_rgb(210, 153, 34)
CYAN      = term.color_rgb(57,  197, 187)
MAGENTA   = term.color_rgb(188, 140, 255)
ORANGE    = term.color_rgb(255, 160, 72)
GREEN_BG  = term.on_color_rgb(15,  44,  28)
RED_BG    = term.on_color_rgb(56,  18,  22)
BLUE_BG   = term.on_color_rgb(12,  31,  56)

TOOL_COLOR: dict[str, str] = {
    "Bash":       YELLOW,
    "Edit":       CYAN,
    "Write":      RED,
    "Read":       GREEN,
    "WebSearch":  MAGENTA,
    "WebFetch":   MAGENTA,
    "Agent":      BLUE,
    "NotebookEdit": ORANGE,
}

TOOL_ICON: dict[str, str] = {
    "Bash":       "❯",
    "Edit":       "✎",
    "Write":      "✦",
    "Read":       "◎",
    "WebSearch":  "⌕",
    "WebFetch":   "⌕",
    "Agent":      "⬡",
    "NotebookEdit": "⊞",
}

# ── animations ────────────────────────────────────────────────────────────────
SPINNER   = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
PULSE     = ["█","▓","▒","░","▒","▓"]          # pulsing block for "waiting"
DOTS      = ["   ","·  ","·· ","···","·· ","·  "]  # searching dots


# ── helpers ───────────────────────────────────────────────────────────────────

def _strip(s: str) -> str:
    return re.sub(r'\x1b\[[0-9;]*m', '', s)


def _vis(s: str) -> int:
    return len(_strip(s))


def _clamp(s: str, n: int) -> str:
    return s[:n] if len(s) > n else s


def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _vis(s))


def _tool(name: str) -> str:
    color = TOOL_COLOR.get(name, FG)
    icon  = TOOL_ICON.get(name, "·")
    return f"{color}{term.bold}{icon} {name}{term.normal}"


def _age(secs: float) -> str:
    s = int(secs)
    if s < 10:
        color, urgency = GREEN, ""
    elif s < 30:
        color, urgency = YELLOW, " !"
    else:
        color, urgency = RED, " !!"
    text = f"{s}s" if s < 60 else f"{s//60}m{s%60:02d}s"
    return f"{color}{text}{urgency}{term.normal}"


def _age_bar(secs: float, width: int = 14) -> str:
    ratio  = min(secs / 60.0, 1.0)
    filled = int(ratio * width)
    if ratio < 0.17:
        color = GREEN
    elif ratio < 0.5:
        color = YELLOW
    else:
        color = RED
    bar = "█" * filled + "░" * (width - filled)
    pct = int(ratio * 100)
    return f"{color}{bar} {pct:3d}%{term.normal}"


def _fmt_uptime(secs: int) -> str:
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _box_line(content: str, box_w: int) -> str:
    """Left pad content inside a box of given inner width."""
    stripped = _strip(content)
    pad = max(0, box_w - len(stripped))
    return content + " " * pad


# ── state ─────────────────────────────────────────────────────────────────────

@dataclass
class UIState:
    queue:   RequestQueue
    history: list[HistoryEntry] = field(default_factory=list)
    cursor:  int   = 0
    tick:    int   = 0          # increments every 50ms
    allowed: int   = 0
    denied:  int   = 0
    start:   float = field(default_factory=time.time)
    dirty:   bool  = True

    @property
    def spinner(self) -> str:
        return SPINNER[self.tick % len(SPINNER)]

    @property
    def pulse(self) -> str:
        return PULSE[self.tick % len(PULSE)]

    @property
    def dots(self) -> str:
        return DOTS[self.tick % len(DOTS)]


# ── renderer ──────────────────────────────────────────────────────────────────

class Renderer:
    def __init__(self, state: UIState) -> None:
        self.state = state

    def draw(self) -> None:
        st = self.state
        h  = term.height
        w  = term.width

        left_w  = min(44, max(32, w // 3))
        right_w = w - left_w - 1
        inner_l = left_w - 2
        inner_r = right_w - 3

        out: list[str] = []
        E = out.append

        def at(r: int, c: int = 0) -> str:
            return term.move(r, c)

        # ── clear ────────────────────────────────────────────────────────────
        E(term.home + BG + term.clear)

        # ── top banner ────────────────────────────────────────────────────────
        n   = len(st.queue.pending)
        spin_c = BLUE if n else GREEN
        spin   = f"{spin_c}{st.spinner if n else '●'}{term.normal}"
        uptime = _fmt_uptime(int(time.time() - st.start))

        E(at(0) + BG2 + DIM + "┏" + "━" * (w - 2) + "┓" + term.normal)

        title = f"  ⬡  CLAUDE PERMISSION MANAGER"
        stats_parts = [
            f"{spin}  {BLUE}{term.bold}{n}{term.normal} pending",
            f"{DIM}·{term.normal}",
            f"{GREEN}✓ {st.allowed}{term.normal}",
            f"{RED}✗ {st.denied}{term.normal}",
            f"{DIM}⏱ {uptime}{term.normal}",
        ]
        stats = "  ".join(stats_parts) + "  "
        gap   = max(0, w - len(title) - _vis(stats) - 2)
        E(at(1) + BG2 + BLUE + term.bold + title + term.normal + " " * gap + stats)
        E(at(2) + BG2 + DIM + "┗" + "━" * (w - 2) + "┛" + term.normal)

        # ── column headers ────────────────────────────────────────────────────
        E(at(3) + BG2 + DIM +
          f" {'QUEUE':<{left_w - 2}}" + "│" +
          f" {'DETAIL':<{right_w - 2}}" + term.normal)
        E(at(4) + DIM + "─" * left_w + "┼" + "─" * right_w + term.normal)

        # ── layout math ───────────────────────────────────────────────────────
        HIST_ROWS  = 6   # divider + header + 4 visible history entries
        BOTTOM     = 2
        avail      = h - 5 - HIST_ROWS - BOTTOM   # rows for pending items
        max_items  = max(2, avail // 2)
        row        = 5

        # ── pending list ──────────────────────────────────────────────────────
        if not st.queue.pending:
            # empty state with animated dots
            mid = 5 + avail // 2
            msg = f"{DIM}{st.dots} Waiting for permission requests {st.dots}{term.normal}"
            sub = f"{DIM}Start Claude sessions in any terminal{term.normal}"
            E(at(mid - 1) + BG + _pad("", left_w) + DIM + "│" + term.normal)
            E(at(mid,     left_w // 2 - _vis(msg) // 2) + msg)
            E(at(mid + 1, left_w // 2 - _vis(sub) // 2) + sub)

        for i, item in enumerate(st.queue.pending[:max_items]):
            r   = item.request
            age = time.time() - r.timestamp
            sel = i == st.cursor
            bg  = BG3 if sel else BG
            arrow = f"{BLUE}▶{term.normal}" if sel else " "
            cwd_s = _clamp(r.cwd.replace(os.path.expanduser("~"), "~"), inner_l - 12)
            cmd   = _clamp(r.summary_command(), inner_l - 2)

            l1 = f"{arrow} {_tool(r.tool_name)}  {DIM}{r.short_session()[:6]}{term.normal}  {_age(age)}"
            l2 = f"  {DIM}{cmd}{term.normal}"

            E(at(row)     + bg + _pad(l1, left_w) + term.normal + DIM + "│" + term.normal)
            E(at(row + 1) + bg + _pad(l2, left_w) + term.normal + DIM + "│" + term.normal)
            row += 2

        # fill blank pending rows
        end_pending = 5 + max_items * 2
        while row < end_pending:
            E(at(row) + BG + " " * left_w + DIM + "│" + term.normal)
            row += 1

        # ── history section ───────────────────────────────────────────────────
        hist_label = f"HISTORY  {DIM}({len(st.history)} total){term.normal}" if st.history else f"HISTORY  {DIM}(none yet){term.normal}"
        E(at(row) + BG2 + DIM + "─" * left_w + "┼" + "─" * right_w + term.normal)
        row += 1
        E(at(row) + BG2 + " " + BLUE + term.bold + _pad(hist_label, left_w - 2) + term.normal + DIM + "│" + term.normal)
        row += 1

        hist_slots = h - row - BOTTOM
        for e in list(reversed(st.history))[:hist_slots]:
            icon  = f"{GREEN}✓{term.normal}" if e.decision == "allow" else f"{RED}✗{term.normal}"
            entry = f" {icon} {_tool(e.tool_name)}  {DIM}{_clamp(e.command_summary, inner_l - 10)}{term.normal}"
            E(at(row) + BG + _pad(entry, left_w) + DIM + "│" + term.normal)
            row += 1

        while row < h - BOTTOM:
            E(at(row) + BG + " " * left_w + DIM + "│" + term.normal)
            row += 1

        # ── detail panel (right side, rows 5 → h-BOTTOM) ────────────────────
        if st.queue.pending:
            item = st.queue.pending[min(st.cursor, len(st.queue.pending) - 1)]
            r    = item.request
            age  = time.time() - r.timestamp
            cwd  = r.cwd.replace(os.path.expanduser("~"), "~")
            cmd_lines = json.dumps(r.tool_input, indent=2).splitlines()

            # waiting animation
            pulse_c = GREEN if age < 10 else (YELLOW if age < 30 else RED)
            waiting_anim = f"{pulse_c}{st.pulse}{term.normal}"

            # build detail lines
            dl: list[str] = []

            # ── session badge ────────
            dl.append("")
            badge = f" {BLUE_BG}{BLUE}{term.bold}  SESSION {r.short_session()[:6]}  {term.normal} "
            dl.append(badge)
            dl.append("")

            # ── meta table ───────────
            def meta(label: str, value: str) -> str:
                return f"  {DIM}{label:<9}{term.normal}{value}"

            dl.append(meta("Tool",    _tool(r.tool_name)))
            dl.append(meta("CWD",     f"{DIM}{_clamp(cwd, inner_r - 12)}{term.normal}"))
            dl.append(meta("Waiting", f"{_age(age)}  {waiting_anim}"))
            dl.append(meta("Age bar", _age_bar(age, min(inner_r - 16, 20))))
            dl.append("")

            # ── command box ──────────
            box_inner = min(inner_r - 4, max((max(len(l) for l in cmd_lines) if cmd_lines else 10), 20))
            dl.append(f"  {DIM}╭{'─' * box_inner}╮{term.normal}")
            for cl in cmd_lines[:8]:
                padded = _clamp(cl, box_inner - 2)
                dl.append(f"  {DIM}│{term.normal} {FG}{_pad(padded, box_inner - 2)}{term.normal}{DIM}│{term.normal}")
            if len(cmd_lines) > 8:
                dl.append(f"  {DIM}│{term.normal} {DIM}{'… ' + str(len(cmd_lines)-8) + ' more lines':<{box_inner-2}}{term.normal}{DIM}│{term.normal}")
            dl.append(f"  {DIM}╰{'─' * box_inner}╯{term.normal}")
            dl.append("")

            # ── action buttons ───────
            allow_btn = f"{GREEN_BG}{GREEN}{term.bold}  ✓  ALLOW  {term.normal}{GREEN_BG}{GREEN}[A]{term.normal}"
            deny_btn  = f"{RED_BG}{RED}{term.bold}   ✗  DENY   {term.normal}{RED_BG}{RED}[D]{term.normal}"
            dl.append(f"  {allow_btn}    {deny_btn}")

            # render detail lines into right panel
            for i, line in enumerate(dl):
                dr = 5 + i
                if dr >= h - BOTTOM:
                    break
                E(at(dr, left_w + 1) + BG + _pad(line, right_w - 1) + term.normal)

        else:
            # no requests: show idle message in right panel
            mid = (h - BOTTOM + 5) // 2
            lines = [
                f"{BLUE}{term.bold}  ⬡  Permission Manager Active{term.normal}",
                "",
                f"  {DIM}Daemon is running and listening on{term.normal}",
                f"  {CYAN}/tmp/claude-perm-{os.environ.get('USER','user')}.sock{term.normal}",
                "",
                f"  {DIM}All Bash · Edit · Write · Agent calls{term.normal}",
                f"  {DIM}will appear here for your approval.{term.normal}",
                "",
                f"  {GREEN}{st.spinner}  Listening{st.dots}{term.normal}",
            ]
            for i, line in enumerate(lines):
                dr = mid - len(lines) // 2 + i
                if 5 <= dr < h - BOTTOM:
                    E(at(dr, left_w + 1) + BG + _pad(line, right_w - 1) + term.normal)

        # ── bottom bar ────────────────────────────────────────────────────────
        keys = (f"  {DIM}↑↓ / j k{term.normal} navigate"
                f"   {GREEN}A{term.normal} allow"
                f"   {RED}D{term.normal} deny"
                f"   {DIM}Q{term.normal} quit")
        E(at(h - 2) + BG2 + _pad(keys, w) + term.normal)
        E(at(h - 1) + BG2 + " " * w + term.normal)

        sys.stdout.write("".join(out))
        sys.stdout.flush()
