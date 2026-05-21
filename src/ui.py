"""
Kitty-optimised terminal renderer using blessed.

Draws directly to stdout with 24-bit colour and Unicode box-drawing
characters that Kitty renders pixel-perfectly.
"""
import os
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import blessed

from src.protocol import HistoryEntry, Request
from src.server import PendingItem, RequestQueue

term = blessed.Terminal()

# ── palette (GitHub dark) ────────────────────────────────────────────────────
BG          = term.on_color_rgb(13,  17,  23)   # #0d1117
BG2         = term.on_color_rgb(22,  27,  34)   # #161b22
BG3         = term.on_color_rgb(28,  33,  40)   # #1c2128
FG          = term.color_rgb(230, 237, 243)      # #e6edf3
DIM         = term.color_rgb(110, 118, 129)      # #6e7681
BLUE        = term.color_rgb(88,  166, 255)      # #58a6ff
GREEN       = term.color_rgb(63,  185, 80)       # #3fb950
RED         = term.color_rgb(248, 81,  73)       # #f85149
YELLOW      = term.color_rgb(210, 153, 34)       # #d29922
CYAN        = term.color_rgb(57,  197, 187)      # #39c5bb
MAGENTA     = term.color_rgb(188, 140, 255)      # #bc8cff
GREEN_BG    = term.on_color_rgb(15,  44,  28)
RED_BG      = term.on_color_rgb(45,  16,  21)
GREEN_BORD  = term.color_rgb(46,  160, 67)
RED_BORD    = term.color_rgb(218, 54,  51)

TOOL_COLOR: dict[str, Any] = {
    "Bash":      YELLOW + term.bold,
    "Edit":      CYAN   + term.bold,
    "Write":     RED    + term.bold,
    "Read":      GREEN  + term.bold,
    "WebSearch": MAGENTA + term.bold,
    "WebFetch":  MAGENTA + term.bold,
    "Agent":     BLUE   + term.bold,
}

SPINNER = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]


# ── helpers ──────────────────────────────────────────────────────────────────

def _tool(name: str) -> str:
    color = TOOL_COLOR.get(name, term.bold)
    return f"{color}{name}{term.normal}"


def _age(secs: float) -> str:
    s = int(secs)
    color = GREEN if s < 10 else (YELLOW if s < 30 else RED)
    text = f"{s}s" if s < 60 else f"{s//60}m{s%60:02d}s"
    return f"{color}{text}{term.normal}"


def _age_bar(secs: float, width: int = 12) -> str:
    """A small progress bar showing urgency."""
    ratio = min(secs / 60, 1.0)
    filled = int(ratio * width)
    color = GREEN if ratio < 0.17 else (YELLOW if ratio < 0.5 else RED)
    bar = "█" * filled + "░" * (width - filled)
    return f"{color}{bar}{term.normal}"


def _fmt_uptime(secs: int) -> str:
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def _clamp(s: str, n: int) -> str:
    return s[:n] if len(s) > n else s


def _pad(s: str, n: int) -> str:
    # strip ANSI before measuring printable width
    import re
    visible = re.sub(r'\x1b\[[0-9;]*m', '', s)
    pad = max(0, n - len(visible))
    return s + " " * pad


# ── screen state ─────────────────────────────────────────────────────────────

@dataclass
class UIState:
    queue:    RequestQueue
    history:  list[HistoryEntry] = field(default_factory=list)
    cursor:   int = 0
    spinner:  int = 0
    allowed:  int = 0
    denied:   int = 0
    start:    float = field(default_factory=time.time)
    dirty:    bool = True


# ── renderer ─────────────────────────────────────────────────────────────────

class Renderer:
    def __init__(self, state: UIState) -> None:
        self.state = state
        self._last_height = 0
        self._last_width  = 0

    def draw(self) -> None:
        st = self.state
        h  = term.height
        w  = term.width

        # Recalculate layout
        left_w  = min(42, w // 2)
        right_w = w - left_w - 1       # -1 for divider
        inner_left  = left_w - 2
        inner_right = right_w - 3

        output: list[str] = []
        emit = output.append

        def at(row: int, col: int) -> str:
            return term.move(row, col)

        def hline(row: int, col: int, width: int, char: str = "─") -> None:
            emit(at(row, col) + DIM + char * width + term.normal)

        def vline(row: int, col: int, height: int) -> None:
            for r in range(height):
                emit(at(row + r, col) + DIM + "│" + term.normal)

        # ── clear ──
        emit(term.home + BG + term.clear)

        # ── top bar ──────────────────────────────────────────────────────────
        n_pending = len(st.queue.pending)
        spin = SPINNER[st.spinner] if n_pending else "●"
        spin_col = BLUE if n_pending else GREEN
        uptime = _fmt_uptime(int(time.time() - st.start))

        top = (
            f"{BG2}{DIM}┏{'━' * (w - 2)}┓{term.normal}"
        )
        emit(at(0, 0) + top)

        title = f"  ⬢  CLAUDE PERMISSION MANAGER"
        stats = (
            f"{spin_col}{spin}{term.normal}  "
            f"{BLUE}{term.bold}{n_pending}{term.normal} pending  "
            f"{DIM}·{term.normal}  "
            f"{GREEN}✓ {st.allowed}{term.normal}  "
            f"{RED}✗ {st.denied}{term.normal}  "
            f"{DIM}· {uptime}{term.normal}  "
        )
        import re
        stats_visible = len(re.sub(r'\x1b\[[0-9;]*m', '', stats))
        title_col = BLUE + term.bold
        row1 = (
            f"{BG2}{title_col}{title}{term.normal}"
            + " " * max(0, w - len(title) - stats_visible - 2)
            + stats
        )
        emit(at(1, 0) + row1)
        emit(at(2, 0) + f"{BG2}{DIM}┗{'━' * (w - 2)}┛{term.normal}")

        # ── column headers ────────────────────────────────────────────────────
        emit(at(3, 0) + BG2 + DIM)
        emit(f" {'PENDING':^{left_w - 1}}" + "│" + f" {'DETAIL':^{right_w - 1}}")
        emit(term.normal)
        emit(at(4, 0) + DIM + "─" * left_w + "┼" + "─" * right_w + term.normal)

        # ── pending list ──────────────────────────────────────────────────────
        list_rows = h - 12      # rows available for pending + history
        pending_rows = max(4, list_rows - 6)
        row = 5

        for i, item in enumerate(st.queue.pending[:pending_rows]):
            r    = item.request
            age  = time.time() - r.timestamp
            sel  = i == st.cursor
            bg   = BG3 if sel else BG
            arrow = f"{BLUE}▶{term.normal}" if sel else " "
            cwd_s = _clamp(r.cwd.replace(os.path.expanduser("~"), "~"), 16)

            line1 = f"{arrow} {_tool(r.tool_name)}  {DIM}{r.short_session()}{term.normal}  {_age(age)}"
            line2 = f"  {DIM}{_clamp(r.summary_command(), inner_left - 2)}{term.normal}"

            emit(at(row,     0) + bg + _pad(line1, left_w) + term.normal + DIM + "│" + term.normal)
            emit(at(row + 1, 0) + bg + _pad(line2, left_w) + term.normal + DIM + "│" + term.normal)
            row += 2

        # fill remaining pending rows
        while row < 5 + pending_rows * 2:
            emit(at(row, 0) + BG + " " * left_w + DIM + "│" + term.normal)
            row += 1

        # ── history section ───────────────────────────────────────────────────
        emit(at(row, 0) + BG2 + DIM + "─" * left_w + "┼" + "─" * right_w + term.normal)
        row += 1
        emit(at(row, 0) + BG2 + DIM + f" {'HISTORY':^{left_w - 1}}│" + " " * right_w + term.normal)
        row += 1

        hist_rows = h - row - 3
        for e in list(reversed(st.history[-hist_rows:]))[:hist_rows]:
            icon = f"{GREEN}✓{term.normal}" if e.decision == "allow" else f"{RED}✗{term.normal}"
            line = f" {icon} {_tool(e.tool_name)}  {DIM}{_clamp(e.command_summary, inner_left - 8)}{term.normal}"
            emit(at(row, 0) + BG + _pad(line, left_w) + DIM + "│" + term.normal)
            row += 1

        while row < h - 3:
            emit(at(row, 0) + BG + " " * left_w + DIM + "│" + term.normal)
            row += 1

        # ── detail panel ──────────────────────────────────────────────────────
        if st.queue.pending:
            item = st.queue.pending[st.cursor]
            r    = item.request
            age  = time.time() - r.start if hasattr(r, 'start') else time.time() - r.timestamp
            age  = time.time() - r.timestamp
            cwd  = r.cwd.replace(os.path.expanduser("~"), "~")

            import json
            detail_lines = [
                f" {DIM}Session{term.normal}  {term.bold}{r.short_session()}{term.normal}",
                f" {DIM}Tool   {term.normal}  {_tool(r.tool_name)}",
                f" {DIM}CWD    {term.normal}  {DIM}{_clamp(cwd, inner_right - 10)}{term.normal}",
                f" {DIM}Waiting{term.normal}  {_age(age)}  {_age_bar(age)}",
                "",
            ]

            # command box
            cmd_lines = json.dumps(r.tool_input, indent=2).splitlines()
            box_w = min(inner_right - 2, max(len(l) for l in cmd_lines) + 4)
            detail_lines.append(f" {DIM}╭{'─' * box_w}╮{term.normal}")
            for cl in cmd_lines[:6]:
                padded = _clamp(cl, box_w - 2)
                detail_lines.append(f" {DIM}│{term.normal} {FG}{padded:<{box_w-2}}{term.normal} {DIM}│{term.normal}")
            if len(cmd_lines) > 6:
                detail_lines.append(f" {DIM}│{term.normal} {DIM}…{term.normal}{' ' * (box_w - 3)}{DIM}│{term.normal}")
            detail_lines.append(f" {DIM}╰{'─' * box_w}╯{term.normal}")
            detail_lines.append("")

            # action buttons
            allow_btn = (
                f"{GREEN_BG}{GREEN_BORD}┌──────────────┐{term.normal}"
            )
            detail_lines.append(f"  {GREEN_BG} {GREEN}✓  ALLOW  {term.bold}[A]{term.normal}{GREEN_BG} {term.normal}    "
                                 f"{RED_BG} {RED}✗  DENY   {term.bold}[D]{term.normal}{RED_BG} {term.normal}")

            for i, dl in enumerate(detail_lines):
                dr = 5 + i
                if dr >= h - 3:
                    break
                emit(at(dr, left_w + 1) + _pad(dl, right_w - 1))

        else:
            # empty state
            mid_r = (h - 3) // 2
            mid_c = left_w + 1 + (right_w - 1) // 2
            msg1 = "Waiting for permission requests…"
            msg2 = "Start Claude sessions in any terminal."
            emit(at(mid_r - 1, left_w + 1 + (right_w - 1 - len(msg1)) // 2) + DIM + msg1 + term.normal)
            emit(at(mid_r,     left_w + 1 + (right_w - 1 - len(msg2)) // 2) + DIM + msg2 + term.normal)

        # ── bottom bar ────────────────────────────────────────────────────────
        emit(at(h - 3, 0) + BG2 + DIM + "─" * w + term.normal)
        keys = f"  {DIM}↑↓{term.normal} navigate   {DIM}A{term.normal} allow   {DIM}D{term.normal} deny   {DIM}Q{term.normal} quit"
        emit(at(h - 2, 0) + BG2 + keys + term.normal)
        emit(at(h - 1, 0) + BG2 + " " * w + term.normal)

        # flush
        sys.stdout.write("".join(output))
        sys.stdout.flush()
