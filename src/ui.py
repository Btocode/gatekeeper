"""
Three-pane Kitty-native terminal UI.

LEFT   : active sessions + history
MIDDLE : pending permission queue
RIGHT  : request detail + message composer
"""
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

import blessed

from src.protocol import HistoryEntry, Request
from src.server import RequestQueue
from src.sessions import Session, SessionRegistry

term = blessed.Terminal()

# ── palette (GitHub dark) ─────────────────────────────────────────────────────
BG       = term.on_color_rgb(13,  17,  23)
BG2      = term.on_color_rgb(22,  27,  34)
BG3      = term.on_color_rgb(33,  38,  45)
BG4      = term.on_color_rgb(48,  54,  61)
FG       = term.color_rgb(230, 237, 243)
DIM      = term.color_rgb(110, 118, 129)
BLUE     = term.color_rgb(88,  166, 255)
GREEN    = term.color_rgb(63,  185, 80)
RED      = term.color_rgb(248, 81,  73)
YELLOW   = term.color_rgb(210, 153, 34)
CYAN     = term.color_rgb(57,  197, 187)
MAGENTA  = term.color_rgb(188, 140, 255)
ORANGE   = term.color_rgb(255, 160, 72)

TOOL_COLOR = {
    "Bash": YELLOW, "Edit": CYAN, "Write": RED,
    "Read": GREEN, "WebSearch": MAGENTA, "WebFetch": MAGENTA,
    "Agent": BLUE, "NotebookEdit": ORANGE,
}
TOOL_ICON = {
    "Bash": "❯", "Edit": "✎", "Write": "✦", "Read": "◎",
    "WebSearch": "⌕", "WebFetch": "⌕", "Agent": "⬡", "NotebookEdit": "⊞",
}

SPINNER = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]
PULSE   = ["▪","▫","▪","▫"]
WAVE    = ["▁","▂","▃","▄","▅","▆","▇","█","▇","▆","▅","▄","▃","▂","▁"]


# ── helpers ───────────────────────────────────────────────────────────────────

def _vis(s: str) -> int:
    return len(re.sub(r'\x1b\[[0-9;]*m', '', s))

def _clamp(s: str, n: int) -> str:
    return s[:n] if len(s) > n else s

def _pad(s: str, n: int) -> str:
    return s + " " * max(0, n - _vis(s))

def _tool(name: str) -> str:
    c = TOOL_COLOR.get(name, FG)
    i = TOOL_ICON.get(name, "·")
    return f"{c}{term.bold}{i} {name}{term.normal}"

def _age_color(secs: float) -> str:
    if secs < 10:  return GREEN
    if secs < 30:  return YELLOW
    return RED

def _age_str(secs: float) -> str:
    s   = int(secs)
    col = _age_color(secs)
    txt = f"{s}s" if s < 60 else f"{s//60}m{s%60:02d}s"
    return f"{col}{txt}{term.normal}"

def _age_bar(secs: float, w: int = 12) -> str:
    ratio  = min(secs / 60.0, 1.0)
    filled = int(ratio * w)
    col    = _age_color(secs)
    return f"{col}{'█' * filled}{'░' * (w - filled)} {int(ratio*100):3d}%{term.normal}"

def _uptime(secs: int) -> str:
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── focus enum ────────────────────────────────────────────────────────────────

FOCUS_SESSIONS = 0
FOCUS_QUEUE    = 1


# ── state ─────────────────────────────────────────────────────────────────────

@dataclass
class UIState:
    queue:    RequestQueue
    registry: SessionRegistry
    history:  list[HistoryEntry] = field(default_factory=list)

    # cursors
    focus:       int = FOCUS_QUEUE   # which pane has keyboard focus
    q_cursor:    int = 0             # cursor in pending queue
    s_cursor:    int = 0             # cursor in sessions list

    # message composer
    composing:   bool = False
    message_buf: str  = ""

    # counters / animation
    tick:    int   = 0
    allowed: int   = 0
    denied:  int   = 0
    start:   float = field(default_factory=time.time)
    dirty:   bool  = True
    kitty_ok: bool = False           # set by daemon after checking

    @property
    def spinner(self) -> str:
        return SPINNER[self.tick % len(SPINNER)]

    @property
    def wave(self) -> str:
        return WAVE[self.tick % len(WAVE)]


# ── renderer ──────────────────────────────────────────────────────────────────

class Renderer:
    def __init__(self, state: UIState) -> None:
        self.state = state

    # ── top-level draw ────────────────────────────────────────────────────────

    def draw(self) -> None:
        st = self.state
        h  = term.height
        w  = term.width

        # column widths
        lw = max(26, min(30, w // 5))      # sessions pane
        mw = max(28, min(36, w // 4))      # queue pane
        rw = w - lw - mw - 2              # detail pane (2 dividers)

        out: list[str] = []
        E = out.append

        E(term.home + BG + term.clear)

        self._draw_banner(E, h, w, st)
        self._draw_columns(E, h, w, lw, mw, rw, st)
        self._draw_footer(E, h, w, st)

        if st.composing:
            self._draw_composer(E, h, w, st)

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    # ── banner ────────────────────────────────────────────────────────────────

    def _draw_banner(self, E, h, w, st):
        n    = len(st.queue.pending)
        spin = f"{BLUE}{st.spinner}{term.normal}" if n else f"{GREEN}●{term.normal}"
        up   = _uptime(int(time.time() - st.start))

        E(term.move(0) + BG2 + DIM + "┏" + "━"*(w-2) + "┛" + term.normal)

        title = f"  ⬡  CLAUDE PERMISSION MANAGER"
        stats = (f"  {spin}  {BLUE}{term.bold}{n}{term.normal} pending"
                 f"  {DIM}·{term.normal}  {GREEN}✓ {st.allowed}{term.normal}"
                 f"  {RED}✗ {st.denied}{term.normal}"
                 f"  {DIM}· {up}{term.normal}  ")

        gap = max(0, w - len(title) - _vis(stats) - 2)
        E(term.move(1) + BG2 + BLUE + term.bold + title + term.normal + " "*gap + stats)
        E(term.move(2) + BG2 + DIM + "┗" + "━"*(w-2) + "┛" + term.normal)

    # ── three-column layout ───────────────────────────────────────────────────

    def _draw_columns(self, E, h, w, lw, mw, rw, st):
        content_h = h - 5   # banner(3) + footer(2)

        # header row
        lf = f"{BLUE}{term.bold}" if st.focus == FOCUS_SESSIONS else DIM
        mf = f"{BLUE}{term.bold}" if st.focus == FOCUS_QUEUE    else DIM
        rf = DIM

        sessions = st.registry.active()
        sq_label = f"SESSIONS ({len(sessions)})"
        qq_label = f"QUEUE ({len(st.queue.pending)})"

        E(term.move(3) + BG2
          + lf + f" {sq_label:<{lw-2}}" + term.normal
          + DIM + "│" + term.normal
          + mf + f" {qq_label:<{mw-2}}" + term.normal
          + DIM + "│" + term.normal
          + rf + f" DETAIL" + term.normal)
        E(term.move(4) + DIM + "─"*lw + "┼" + "─"*mw + "┼" + "─"*rw + term.normal)

        # content rows
        for row in range(5, h - 2):
            ri = row - 5
            lc = self._session_cell(ri, lw, content_h, st, sessions)
            mc = self._queue_cell(ri, mw, content_h, st)
            rc = self._detail_cell(ri, rw, content_h, st)
            E(term.move(row)
              + lc + DIM + "│" + term.normal
              + mc + DIM + "│" + term.normal
              + rc)

    def _session_cell(self, ri, w, total_h, st, sessions) -> str:
        # split left pane: sessions top, history bottom
        hist_h    = min(6, len(st.history) + 2)
        sess_h    = total_h - hist_h - 1   # -1 for divider
        inner     = w - 2

        if ri < sess_h:
            # sessions area
            idx = ri // 3
            sub = ri % 3
            if idx < len(sessions):
                s   = sessions[idx]
                sel = (idx == st.s_cursor and st.focus == FOCUS_SESSIONS)
                bg  = BG3 if sel else BG
                arr = f"{BLUE}▶{term.normal}" if sel else " "
                if sub == 0:
                    txt = f"{arr} {CYAN}{term.bold}{s.short_id()}{term.normal}  {DIM}{s.age_str()}{term.normal}"
                elif sub == 1:
                    txt = f"  {DIM}{s.short_cwd()}{term.normal}"
                else:
                    txt = f"  {DIM}{s.tool_count} tool calls{term.normal}"
                return bg + _pad(txt, w) + term.normal
            return BG + " "*w + term.normal

        if ri == sess_h:
            return BG2 + DIM + "─"*w + term.normal

        # history area
        hi = ri - sess_h - 1
        if hi == 0:
            label = f"HISTORY ({len(st.history)})"
            return BG2 + DIM + f" {label:<{inner}}" + term.normal + " "
        elif hi - 1 < len(st.history):
            e    = list(reversed(st.history))[hi - 1]
            icon = f"{GREEN}✓{term.normal}" if e.decision == "allow" else f"{RED}✗{term.normal}"
            line = f" {icon} {_tool(e.tool_name)} {DIM}{_clamp(e.command_summary, inner-10)}{term.normal}"
            return BG + _pad(line, w) + term.normal
        return BG + " "*w + term.normal

    def _queue_cell(self, ri, w, total_h, st) -> str:
        inner   = w - 2
        pending = st.queue.pending

        if not pending:
            if ri == total_h // 2:
                msg = f"{DIM}{st.spinner} waiting{term.normal}"
                return BG + _pad(f"  {msg}", w) + term.normal
            return BG + " "*w + term.normal

        idx = ri // 3
        sub = ri % 3
        if idx < len(pending):
            item = pending[idx]
            r    = item.request
            age  = time.time() - r.timestamp
            sel  = (idx == st.q_cursor and st.focus == FOCUS_QUEUE)
            bg   = BG3 if sel else BG
            arr  = f"{BLUE}▶{term.normal}" if sel else " "
            if sub == 0:
                line = f"{arr} {_tool(r.tool_name)}  {_age_str(age)}"
            elif sub == 1:
                line = f"  {DIM}{_clamp(r.summary_command(), inner-2)}{term.normal}"
            else:
                line = f"  {DIM}{r.short_session()[:6]}  {r.cwd.replace(os.path.expanduser('~'),'~')[-16:]}{term.normal}"
            return bg + _pad(line, w) + term.normal

        return BG + " "*w + term.normal

    def _detail_cell(self, ri, w, total_h, st) -> str:
        inner   = w - 2
        pending = st.queue.pending

        if not pending:
            lines = self._idle_detail(inner, st)
            return BG + _pad(lines[ri] if ri < len(lines) else "", w) + term.normal

        item  = pending[min(st.q_cursor, len(pending)-1)]
        r     = item.request
        age   = time.time() - r.timestamp
        cwd   = r.cwd.replace(os.path.expanduser("~"), "~")

        # build all detail lines once
        dl = self._build_detail_lines(r, age, cwd, inner, st)
        if ri < len(dl):
            return BG + _pad(dl[ri], w) + term.normal
        return BG + " "*w + term.normal

    def _build_detail_lines(self, r, age, cwd, inner, st) -> list[str]:
        dl: list[str] = [""]

        # session badge
        badge = f"{term.on_color_rgb(12,31,56)}{BLUE}{term.bold}  {r.short_session()[:8]}  {term.normal}"
        dl.append(f" {badge}  {_tool(r.tool_name)}")
        dl.append("")

        # meta
        def row(label, val):
            return f"  {DIM}{label:<9}{term.normal}{val}"

        dl.append(row("CWD",     f"{DIM}{_clamp(cwd, inner-12)}{term.normal}"))
        dl.append(row("Waiting", _age_str(age)))
        dl.append(row("",        _age_bar(age, min(inner-16, 16))))
        dl.append("")

        # command box
        try:
            cmd_lines = json.dumps(r.tool_input, indent=2).splitlines()
        except Exception:
            cmd_lines = [str(r.tool_input)]
        bw = min(inner - 4, max(max(len(l) for l in cmd_lines) if cmd_lines else 10, 18))
        dl.append(f"  {DIM}╭{'─'*bw}╮{term.normal}")
        for cl in cmd_lines[:6]:
            dl.append(f"  {DIM}│{term.normal} {FG}{_pad(_clamp(cl, bw-2), bw-2)}{term.normal}{DIM}│{term.normal}")
        if len(cmd_lines) > 6:
            dl.append(f"  {DIM}│{term.normal} {DIM}{_clamp(f'… {len(cmd_lines)-6} more lines', bw-2):<{bw-2}}{term.normal}{DIM}│{term.normal}")
        dl.append(f"  {DIM}╰{'─'*bw}╯{term.normal}")
        dl.append("")

        # action row
        ab = f"{term.on_color_rgb(15,44,28)}{GREEN}{term.bold} ✓ ALLOW [A] {term.normal}"
        db = f"{term.on_color_rgb(56,18,22)}{RED}{term.bold}  ✗ DENY [D] {term.normal}"
        dl.append(f"  {ab}   {db}")
        dl.append("")

        # message hint
        if st.kitty_ok:
            dl.append(f"  {DIM}M  →  send message to this session{term.normal}")
        else:
            dl.append(f"  {DIM}Kitty remote control unavailable{term.normal}")
            dl.append(f"  {DIM}(enable allow_remote_control in kitty.conf){term.normal}")

        return dl

    def _idle_detail(self, inner, st) -> list[str]:
        lines = [
            "",
            f"  {BLUE}{term.bold}⬡  Permission Manager Active{term.normal}",
            "",
            f"  {DIM}Listening on{term.normal}",
            f"  {CYAN}/tmp/claude-perm-{os.environ.get('USER','user')}.sock{term.normal}",
            "",
            f"  {DIM}Bash · Edit · Write · Agent calls{term.normal}",
            f"  {DIM}will appear here for approval.{term.normal}",
            "",
            f"  {GREEN}{st.spinner}  Listening…{term.normal}",
        ]
        return lines

    # ── composer overlay ──────────────────────────────────────────────────────

    def _draw_composer(self, E, h, w, st):
        bw  = min(w - 8, 70)
        col = (w - bw - 4) // 2
        row = h - 10

        sessions  = st.registry.active()
        sel_sess  = sessions[st.s_cursor].short_id() if sessions else "—"

        E(term.move(row,   col) + BG2 + DIM + "╭" + "─"*(bw) + "╮" + term.normal)
        E(term.move(row+1, col) + BG2 + BLUE + term.bold
          + f"│  ✉  Send message → session {sel_sess}" + " "*(bw - 31 - len(sel_sess)) + "│"
          + term.normal)
        E(term.move(row+2, col) + BG2 + DIM + "├" + "─"*(bw) + "┤" + term.normal)

        buf_display = _clamp(st.message_buf, bw - 4)
        cursor_col  = col + 3 + len(buf_display)
        E(term.move(row+3, col) + BG3
          + "│  " + FG + buf_display + " "*(bw - 2 - len(buf_display)) + DIM + "│"
          + term.normal)

        E(term.move(row+4, col) + BG2 + DIM + "├" + "─"*(bw) + "┤" + term.normal)
        hint = f"  Enter send  ·  Esc cancel  ·  Backspace delete"
        E(term.move(row+5, col) + BG2 + DIM + "│" + _pad(hint, bw) + "│" + term.normal)
        E(term.move(row+6, col) + BG2 + DIM + "╰" + "─"*(bw) + "╯" + term.normal)

        # position cursor in input line
        E(term.move(row+3, cursor_col) + term.normal)

    # ── footer ────────────────────────────────────────────────────────────────

    def _draw_footer(self, E, h, w, st):
        lf = f"{BLUE}Tab{term.normal}"
        keys = (f"  {lf} switch pane"
                f"  {DIM}↑↓ / jk{term.normal} navigate"
                f"  {GREEN}A{term.normal} allow"
                f"  {RED}D{term.normal} deny"
                f"  {CYAN}M{term.normal} message"
                f"  {DIM}Q{term.normal} quit")
        E(term.move(h-2) + BG2 + _pad(keys, w) + term.normal)
        E(term.move(h-1) + BG2 + " "*w + term.normal)
