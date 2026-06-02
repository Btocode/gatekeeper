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
import unicodedata
from dataclasses import dataclass, field

import blessed

from src.protocol import HistoryEntry, Request
from src.server import RequestQueue
from src.sessions import Session, SessionRegistry, is_dangerous
from src.config import BASH_CATEGORIES, TOOL_TYPES, GatekeeperConfig

term = blessed.Terminal()

# ── palette (GitHub dark) ─────────────────────────────────────────────────────
BG      = term.on_color_rgb(13,  17,  23)
BG2     = term.on_color_rgb(22,  27,  34)
BG3     = term.on_color_rgb(33,  38,  45)
FG      = term.color_rgb(230, 237, 243)
DIM     = term.color_rgb(110, 118, 129)
BLUE    = term.color_rgb(88,  166, 255)
GREEN   = term.color_rgb(63,  185, 80)
RED     = term.color_rgb(248, 81,  73)
YELLOW  = term.color_rgb(210, 153, 34)
CYAN    = term.color_rgb(57,  197, 187)
MAGENTA = term.color_rgb(188, 140, 255)
ORANGE  = term.color_rgb(255, 160, 72)

# Use only safe single-width ASCII/Latin icons
TOOL_COLOR = {
    "Bash": YELLOW, "Edit": CYAN, "Write": RED,
    "Read": GREEN, "WebSearch": MAGENTA, "WebFetch": MAGENTA,
    "Agent": BLUE, "NotebookEdit": ORANGE,
}
TOOL_ICON = {
    "Bash": "$", "Edit": "~", "Write": "+", "Read": "o",
    "WebSearch": "?", "WebFetch": "?", "Agent": "*", "NotebookEdit": "#",
}

SPINNER = ["|", "/", "-", "\\"]   # ASCII spinner, always 1 col


# ── width helpers ─────────────────────────────────────────────────────────────

def _cw(c: str) -> int:
    """Display column width of a single character."""
    eaw = unicodedata.east_asian_width(c)
    return 2 if eaw in ("W", "F") else 1


def _vis(s: str) -> int:
    """Visible display width of a string (strips ANSI, counts column widths)."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", s)
    return sum(_cw(c) for c in plain)


def _clamp(s: str, n: int) -> str:
    """Clamp plain string to n display columns (no ANSI)."""
    w, out = 0, []
    for c in s:
        cw = _cw(c)
        if w + cw > n:
            break
        out.append(c)
        w += cw
    return "".join(out)


def _pad(s: str, n: int) -> str:
    """Pad string (may contain ANSI) to exactly n display columns."""
    return s + " " * max(0, n - _vis(s))


def _tool(name: str) -> str:
    c = TOOL_COLOR.get(name, FG)
    i = TOOL_ICON.get(name, ".")
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
    bar    = "#" * filled + "-" * (w - filled)
    return f"{col}[{bar}] {int(ratio*100):3d}%{term.normal}"


def _uptime(secs: int) -> str:
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


# ── focus ─────────────────────────────────────────────────────────────────────

FOCUS_SESSIONS = 0
FOCUS_QUEUE    = 1


# ── state ─────────────────────────────────────────────────────────────────────

@dataclass
class UIState:
    queue:    RequestQueue
    registry: SessionRegistry
    history:  list[HistoryEntry] = field(default_factory=list)

    focus:              int  = FOCUS_QUEUE
    q_cursor:           int  = 0
    s_cursor:           int  = 0
    selected_session_id: str = ""   # authoritative selection — survives reorder

    action_cursor: int  = 0   # 0=Yes (allow once)  1=Yes always (persistent)  2=No (deny)

    composing:    bool = False
    message_buf:  str  = ""

    settings_open:   bool = False
    settings_tab:    int  = 0    # 0=Tools 1=Bash Categories 2=Custom Patterns
    settings_cursor: int  = 0
    settings_input:  str  = ""   # for typing custom patterns
    settings_input_active: bool = False

    tick:    int   = 0
    allowed: int   = 0
    denied:  int   = 0
    start:   float = field(default_factory=time.time)
    dirty:   bool  = True
    kitty_ok: bool = False

    @property
    def spinner(self) -> str:
        return SPINNER[self.tick % len(SPINNER)]


# ── renderer ──────────────────────────────────────────────────────────────────

class Renderer:
    def __init__(self, state: UIState) -> None:
        self.state = state

    def draw(self) -> None:
        st       = self.state
        h        = term.height
        w        = term.width
        sessions = st.registry.active()

        # Invalidate session row cache on every draw (cheap to rebuild)
        self._sess_rows_dirty = True

        # Keep s_cursor tracking the selected session by ID (survives reorder)
        if st.selected_session_id and sessions:
            for i, s in enumerate(sessions):
                if s.session_id == st.selected_session_id:
                    st.s_cursor = i
                    break
        elif sessions and not st.selected_session_id:
            st.selected_session_id = sessions[0].session_id

        # Column widths — all pure ints, no ANSI involved in arithmetic
        lw = max(32, min(40, w // 4))      # sessions pane — wider
        mw = max(26, min(34, w // 5))
        rw = w - lw - mw - 2              # 2 divider chars

        out: list[str] = []
        E = out.append

        E(term.home + BG + term.clear)

        self._banner(E, w, st)
        self._columns(E, h, w, lw, mw, rw, st, sessions)
        self._footer(E, h, w, st)

        if st.composing:
            self._composer(E, h, w, st)

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    # ── banner ────────────────────────────────────────────────────────────────

    def _banner(self, E, w, st):
        n    = len(st.queue.pending)
        spin = st.spinner if n else "*"
        up   = _uptime(int(time.time() - st.start))

        # Row 0: top border
        E(term.move(0, 0) + BG2 + DIM + "+" + "-" * (w - 2) + "+" + term.normal)

        # Row 1: title + stats
        title = " GATEKEEPER"
        stats = (f" {BLUE if n else GREEN}{spin}{term.normal}"
                 f"  {BLUE}{term.bold}{n}{term.normal} pending"
                 f"  {DIM}|{term.normal}"
                 f"  {GREEN}A:{st.allowed}{term.normal}"
                 f"  {RED}D:{st.denied}{term.normal}"
                 f"  {DIM}{up}{term.normal} ")
        gap = max(0, w - len(title) - _vis(stats))
        E(term.move(1, 0) + BG2
          + BLUE + term.bold + title + term.normal
          + " " * gap + stats)

        # Row 2: bottom border
        E(term.move(2, 0) + BG2 + DIM + "+" + "-" * (w - 2) + "+" + term.normal)

    # ── column layout ─────────────────────────────────────────────────────────

    def _columns(self, E, h, w, lw, mw, rw, st, sessions):

        # Header row (row 3)
        lf = BLUE + term.bold if st.focus == FOCUS_SESSIONS else DIM
        mf = BLUE + term.bold if st.focus == FOCUS_QUEUE    else DIM

        lh = _pad(f" SESSIONS({len(sessions)})", lw)
        mh = _pad(f" QUEUE({len(st.queue.pending)})", mw)
        rh = _pad(f" DETAIL", rw)

        E(term.move(3, 0) + BG2
          + lf + lh + term.normal
          + DIM + "|" + term.normal
          + mf + mh + term.normal
          + DIM + "|" + term.normal
          + DIM + rh + term.normal)

        # Separator (row 4)
        E(term.move(4, 0) + DIM
          + "-" * lw + "+" + "-" * mw + "+" + "-" * rw
          + term.normal)

        # Content rows
        content_h = h - 7   # banner(3) + header(1) + sep(1) + footer(2)
        for ri in range(content_h):
            row = 5 + ri
            lc  = self._left_cell(ri, lw, content_h, st, sessions)
            mc  = self._mid_cell(ri, mw, content_h, st)
            rc  = self._right_cell(ri, rw, content_h, st)
            E(term.move(row, 0)
              + lc
              + DIM + "|" + term.normal
              + mc
              + DIM + "|" + term.normal
              + rc)

    # ── left cell (sessions + history) ────────────────────────────────────────

    def _build_session_rows(self, sessions, w, st) -> list[tuple[str, str]]:
        """
        Pre-build all session rows as (bg, content) tuples.
        Each session gets: header row, wrapped cwd, tty/calls, separator.
        Long text wraps onto extra lines instead of being truncated.
        """
        inner = w - 3
        rows: list[tuple[str, str]] = []

        for idx, s in enumerate(sessions):
            sel = (idx == st.s_cursor and st.focus == FOCUS_SESSIONS)
            bg  = BG3 if sel else BG
            arr = f"{BLUE}>{term.normal}" if sel else " "

            # Row 1: session ID + badges
            pin     = f" {GREEN}[linked]{term.normal}"   if s.pinned_window else ""
            auto    = f" {YELLOW}[auto]{term.normal}"    if st.registry.is_auto_approve(s.session_id) else ""
            waiting = f" {MAGENTA}[input?]{term.normal}" if s.waiting_input  else ""
            age     = f"  {DIM}{s.age_str()}{term.normal}"
            id_line = f"{arr} {CYAN}{term.bold}{s.short_id()}{term.normal}{pin}{auto}{waiting}{age}"
            rows.append((bg, id_line))

            # Row 2+: cwd — wrap if wider than inner
            cwd = s.cwd.replace(os.path.expanduser("~"), "~")
            cwd_prefix = "  "
            while cwd:
                chunk = _clamp(cwd, inner - len(cwd_prefix))
                rows.append((bg, f"{cwd_prefix}{DIM}{chunk}{term.normal}"))
                cwd = cwd[len(chunk):]
                cwd_prefix = "    "   # indent continuation lines

            # Row 3: tty + call count
            rows.append((bg, f"  {DIM}{s.tty_label()}  {s.tool_count} calls{term.normal}"))

            # Separator between sessions (not after last)
            if idx < len(sessions) - 1:
                rows.append((BG2, DIM + "-" * w + term.normal))

        return rows

    def _left_cell(self, ri, w, total_h, st, sessions) -> str:
        HIST_FIXED = min(8, total_h // 3)
        sess_h     = total_h - HIST_FIXED - 1

        # Build session rows on demand (cached via attribute)
        if not hasattr(self, '_sess_rows_cache') or self._sess_rows_dirty:
            self._sess_rows_cache = self._build_session_rows(sessions, w, st)
            self._sess_rows_dirty = False

        sess_rows = self._sess_rows_cache

        if ri < sess_h:
            if ri < len(sess_rows):
                bg, content = sess_rows[ri]
                return bg + _pad(content, w) + term.normal
            return BG + " " * w + term.normal

        if ri == sess_h:
            return BG2 + DIM + "-" * w + term.normal

        # History
        inner = w - 2
        hi = ri - sess_h - 1
        if hi == 0:
            return BG2 + _pad(f" HISTORY({len(st.history)})", w) + term.normal
        hidx = hi - 1
        if hidx < len(st.history):
            e    = list(reversed(st.history))[hidx]
            icon = f"{GREEN}A{term.normal}" if e.decision == "allow" else f"{RED}D{term.normal}"
            line = f" {icon} {_tool(e.tool_name)} {DIM}{_clamp(e.command_summary, inner - 8)}{term.normal}"
            return BG + _pad(line, w) + term.normal

        return BG + " " * w + term.normal

    # ── middle cell (pending queue) ────────────────────────────────────────────

    def _mid_cell(self, ri, w, total_h, st) -> str:
        inner   = w - 2
        pending = st.queue.pending

        if not pending:
            if ri == total_h // 2:
                msg = f" {st.spinner} waiting..."
                return BG + _pad(f"{DIM}{msg}{term.normal}", w) + term.normal
            return BG + " " * w + term.normal

        idx = ri // 3
        sub = ri % 3
        if idx < len(pending):
            item = pending[idx]
            r    = item.request
            age  = time.time() - r.timestamp
            sel  = (idx == st.q_cursor and st.focus == FOCUS_QUEUE)
            bg   = BG3 if sel else BG
            arr  = f"{BLUE}>{term.normal}" if sel else " "
            if sub == 0:
                line = f"{arr} {_tool(r.tool_name)}  {_age_str(age)}"
            elif sub == 1:
                line = f"  {DIM}{_clamp(r.summary_command(), inner - 2)}{term.normal}"
            else:
                line = f"  {DIM}{r.short_session()}  {_clamp(r.cwd.replace(os.path.expanduser('~'), '~'), inner - 10)}{term.normal}"
            return bg + _pad(line, w) + term.normal

        return BG + " " * w + term.normal

    # ── right cell (detail) ───────────────────────────────────────────────────

    def _right_cell(self, ri, w, total_h, st) -> str:
        inner   = w - 2
        pending = st.queue.pending

        if not pending:
            # If selected session is waiting for input, show its message
            sel_session = st.registry.get_by_id(st.selected_session_id)
            if sel_session and sel_session.waiting_input and sel_session.last_message:
                lines = self._waiting_lines(sel_session, inner, st)
            else:
                lines = self._idle_lines(inner, st)
            line  = lines[ri] if ri < len(lines) else ""
            return BG + _pad(line, w) + term.normal

        item = pending[min(st.q_cursor, len(pending) - 1)]
        dl   = self._detail_lines(item.request, inner, st)
        line = dl[ri] if ri < len(dl) else ""
        return BG + _pad(line, w) + term.normal

    def _detail_lines(self, r, inner, st) -> list[str]:
        age  = time.time() - r.timestamp
        cwd  = r.cwd.replace(os.path.expanduser("~"), "~")

        try:
            cmd_lines = json.dumps(r.tool_input, indent=2).splitlines()
        except Exception:
            cmd_lines = [str(r.tool_input)]

        dl: list[str] = [""]
        dl.append(f" {BLUE}{term.bold}Session{term.normal}  {CYAN}{r.short_session()[:8]}{term.normal}")
        dl.append(f" {DIM}Tool   {term.normal}  {_tool(r.tool_name)}")
        dl.append(f" {DIM}CWD    {term.normal}  {DIM}{_clamp(cwd, inner - 10)}{term.normal}")
        dl.append(f" {DIM}Age    {term.normal}  {_age_str(age)}")
        dl.append(f" {DIM}       {term.normal}  {_age_bar(age, min(inner - 18, 14))}")
        dl.append("")

        # Command — multiline, word-wrapped to fit width
        dl.append(f" {DIM}Command{term.normal}")
        max_cmd_lines = 14   # show up to 14 lines
        shown = 0
        for cl in cmd_lines:
            if shown >= max_cmd_lines:
                dl.append(f"   {DIM}... {len(cmd_lines)-shown} more lines{term.normal}")
                break
            # word-wrap long lines
            while cl:
                chunk = _clamp(cl, inner - 3)
                dl.append(f"   {FG}{chunk}{term.normal}")
                cl = cl[len(chunk):]
                shown += 1
                if shown >= max_cmd_lines:
                    break
        dl.append("")

        # Danger warning
        danger, danger_reason = is_dangerous(r.tool_name, r.tool_input)
        if danger:
            dl.append(f"  {term.on_color_rgb(80,0,0)}{RED}{term.bold} !! DANGEROUS — manual approval required !! {term.normal}")
            dl.append(f"  {RED}{_clamp(danger_reason, inner-2)}{term.normal}")
            dl.append("")

        # Action menu — Claude Code-style numbered options
        dl.append(f"  {DIM}Do you want to proceed?{term.normal}")
        dl.append("")
        options = [
            ("Yes",                           GREEN),
            (r.persistent_allow_label(),      CYAN),
            ("No",                            RED),
        ]
        for i, (label, color) in enumerate(options):
            sel   = (i == st.action_cursor)
            arrow = f"{BLUE}>{term.normal}" if sel else " "
            num   = f"{BLUE}{term.bold}{i + 1}{term.normal}"
            text  = f"{color}{term.bold if sel else ''}{_clamp(label, inner - 7)}{term.normal}"
            dl.append(f"  {arrow} {num}{DIM}.{term.normal} {text}")
        dl.append("")
        dl.append(f"  {DIM}Up/Down select  Enter confirm  M message{term.normal}")

        return dl

    def _waiting_lines(self, s, inner, st) -> list[str]:
        """Detail panel content when the selected session is waiting for input."""
        dl = [
            "",
            f" {MAGENTA}{term.bold}Claude is waiting for your input{term.normal}",
            f" {DIM}Session {s.short_id()}  {s.tty_label()}{term.normal}",
            f" {DIM}{'─' * (inner - 2)}{term.normal}",
            "",
        ]
        for line in s.last_message.splitlines():
            if not line.strip():
                dl.append("")
                continue
            while line:
                chunk = _clamp(line, inner - 2)
                dl.append(f" {FG}{chunk}{term.normal}")
                line = line[len(chunk):]
        dl += [
            "",
            f" {DIM}{'─' * (inner - 2)}{term.normal}",
            f" {CYAN}M{term.normal}{DIM} — type your response and send{term.normal}",
        ]
        return dl

    def _idle_lines(self, inner, st) -> list[str]:
        # Check if any session is waiting for input
        waiting = [s for s in st.registry.active() if s.waiting_input]
        if waiting:
            s  = waiting[0]
            dl = [
                "",
                f" {MAGENTA}{term.bold}Claude is waiting for your input{term.normal}",
                f" {DIM}Session {s.short_id()}{term.normal}",
                "",
            ]
            # Show last message, word-wrapped
            if s.last_message:
                for line in s.last_message.splitlines():
                    while line:
                        chunk = _clamp(line, inner - 2)
                        dl.append(f" {FG}{chunk}{term.normal}")
                        line = line[len(chunk):]
            dl += [
                "",
                f" {DIM}Press {CYAN}M{term.normal}{DIM} to respond{term.normal}",
            ]
            return dl

        return [
            "",
            f" {BLUE}{term.bold}GATEKEEPER{term.normal}",
            "",
            f" {DIM}Listening on:{term.normal}",
            f" {CYAN}/tmp/claude-perm-{os.environ.get('USER','user')}.sock{term.normal}",
            "",
            f" {DIM}Bash / Edit / Agent calls appear here.{term.normal}",
            "",
            f" {DIM}{st.spinner} waiting for requests...{term.normal}",
        ]

    # ── composer overlay ──────────────────────────────────────────────────────

    def _composer(self, E, h, w, st):
        sessions = st.registry.active()
        sid      = sessions[min(st.s_cursor, len(sessions)-1)].short_id() if sessions else "?"
        bw       = min(w - 6, 68)
        col      = (w - bw - 4) // 2
        row      = h - 9

        E(term.move(row,   col) + BG2 + DIM + "+" + "-" * bw + "+" + term.normal)
        E(term.move(row+1, col) + BG2 + BLUE + term.bold
          + _pad(f"|  Message -> session {sid}", bw + 1) + "|"
          + term.normal)
        E(term.move(row+2, col) + BG2 + DIM + "+" + "-" * bw + "+" + term.normal)

        buf_vis = _clamp(st.message_buf, bw - 4)
        E(term.move(row+3, col) + BG3
          + "| " + FG + _pad(buf_vis, bw - 2) + DIM + " |"
          + term.normal)

        E(term.move(row+4, col) + BG2 + DIM + "+" + "-" * bw + "+" + term.normal)
        hint = "  Enter=send  Esc=cancel  Backspace=delete"
        E(term.move(row+5, col) + BG2 + DIM + _pad("|" + hint, bw + 1) + "|" + term.normal)
        E(term.move(row+6, col) + BG2 + DIM + "+" + "-" * bw + "+" + term.normal)

        # Place cursor inside input
        E(term.move(row + 3, col + 2 + len(buf_vis)))

    # ── footer ────────────────────────────────────────────────────────────────

    def draw_settings(self, state) -> None:
        """Full-screen settings overlay."""
        h, w   = term.height, term.width
        cfg: GatekeeperConfig = state.config
        st     = state
        sw     = min(w - 4, 78)
        col    = (w - sw) // 2
        row    = 2
        inner  = sw - 4

        out: list[str] = []
        E = out.append

        # Background
        for r in range(row, h - 1):
            E(term.move(r, col) + BG2 + " " * sw + term.normal)

        # Header
        E(term.move(row,   col) + BG2 + DIM + "+" + "-" * (sw-2) + "+" + term.normal)
        tabs = ["  Tools  ", "  Bash Categories  ", "  Custom Patterns  "]
        tab_row = "| "
        for i, t in enumerate(tabs):
            if i == st.settings_tab:
                tab_row += BLUE + term.bold + t + term.normal + BG2 + " "
            else:
                tab_row += DIM + t + term.normal + " "
        E(term.move(row+1, col) + BG2 + BLUE + term.bold
          + _pad("| ✦ GATEKEEPER SETTINGS", sw-1) + "|" + term.normal)
        E(term.move(row+2, col) + BG2 + _pad(tab_row, sw-1) + DIM + "|" + term.normal)
        E(term.move(row+3, col) + BG2 + DIM + "+" + "-" * (sw-2) + "+" + term.normal)

        r = row + 4

        # ── Tab 0: Tool types ──────────────────────────────────────────────
        if st.settings_tab == 0:
            tools = list(TOOL_TYPES.items())
            for i, (tool_key, meta) in enumerate(tools):
                sel    = i == st.settings_cursor
                bg     = BG3 if sel else BG2
                check  = f"{GREEN}[x]{term.normal}" if tool_key in cfg.allowed_tools else f"{DIM}[ ]{term.normal}"
                arr    = f"{BLUE}>{term.normal}" if sel else " "
                desc   = _clamp(meta["description"], inner - 28)
                line   = f"  {arr} {check}  {term.bold}{meta['label']:<18}{term.normal}  {DIM}{desc}{term.normal}"
                E(term.move(r, col) + bg + _pad(line, sw) + term.normal)
                r += 1
            E(term.move(r, col) + BG2 + " " * sw + term.normal); r += 1
            E(term.move(r, col) + BG2 + DIM
              + _pad("|  Space/Enter = toggle", sw-1) + "|" + term.normal)

        # ── Tab 1: Bash categories ─────────────────────────────────────────
        elif st.settings_tab == 1:
            cats = list(BASH_CATEGORIES.items())
            for i, (cat_key, meta) in enumerate(cats):
                sel   = i == st.settings_cursor
                bg    = BG3 if sel else BG2
                check = f"{GREEN}[x]{term.normal}" if cat_key in cfg.allowed_bash_categories else f"{DIM}[ ]{term.normal}"
                arr   = f"{BLUE}>{term.normal}" if sel else " "
                desc  = _clamp(meta["description"], inner - 28)
                line  = f"  {arr} {check}  {term.bold}{meta['label']:<22}{term.normal}  {DIM}{desc}{term.normal}"
                E(term.move(r, col) + bg + _pad(line, sw) + term.normal)
                r += 1
            E(term.move(r, col) + BG2 + " " * sw + term.normal); r += 1
            E(term.move(r, col) + BG2 + DIM
              + _pad("|  Space/Enter = toggle", sw-1) + "|" + term.normal)

        # ── Tab 2: Custom patterns ─────────────────────────────────────────
        elif st.settings_tab == 2:
            sections = [
                ("Allow patterns (glob)", cfg.custom_allow_patterns),
                ("Deny patterns (glob)",  cfg.custom_deny_patterns),
                ("Allowed edit dirs",     cfg.allowed_edit_dirs),
            ]
            for sec_label, items in sections:
                E(term.move(r, col) + BG2 + YELLOW + term.bold
                  + _pad(f"|  {sec_label}", sw-1) + "|" + term.normal)
                r += 1
                if items:
                    for item in items:
                        line = f"|    {DIM}• {_clamp(item, inner - 4)}{term.normal}"
                        E(term.move(r, col) + BG2 + _pad(line, sw-1) + DIM + "|" + term.normal)
                        r += 1
                else:
                    E(term.move(r, col) + BG2 + DIM
                      + _pad("|    (none)", sw-1) + "|" + term.normal)
                    r += 1
                E(term.move(r, col) + BG2 + " " * sw + term.normal); r += 1

            # Input box
            E(term.move(r, col) + BG2 + DIM + "+" + "-" * (sw-2) + "+" + term.normal); r += 1
            prompt = f"| Add pattern:  {FG}{st.settings_input}{DIM}_"
            E(term.move(r, col) + BG2 + _pad(prompt, sw-1) + "|" + term.normal); r += 1
            hint = ("|  a=add allow  b=add deny  d=add dir  "
                    "Del=remove last  Enter=confirm")
            E(term.move(r, col) + BG2 + DIM + _pad(hint, sw-1) + "|" + term.normal)

        # Footer
        foot_r = h - 3
        E(term.move(foot_r, col) + BG2 + DIM + "+" + "-" * (sw-2) + "+" + term.normal)
        E(term.move(foot_r+1, col) + BG2 + DIM
          + _pad("|  Tab = next section    Esc / S = close & save", sw-1)
          + "|" + term.normal)
        E(term.move(foot_r+2, col) + BG2 + DIM + "+" + "-" * (sw-2) + "+" + term.normal)

        sys.stdout.write("".join(out))
        sys.stdout.flush()

    def _footer(self, E, h, w, st):
        if st.focus == FOCUS_SESSIONS:
            action_hints = f"  {YELLOW}A{term.normal} toggle auto  {RED}U{term.normal} unlink"
        else:
            action_hints = (f"  {GREEN}1{term.normal} allow"
                            f"  {CYAN}2{term.normal} always"
                            f"  {RED}3{term.normal} deny"
                            f"  {DIM}↑↓{term.normal} select"
                            f"  {DIM}Enter{term.normal} confirm")
        keys = (f"  {BLUE}Tab{term.normal} pane"
                f"  {DIM}jk{term.normal} queue"
                + action_hints +
                f"  {CYAN}M{term.normal} msg"
                f"  {YELLOW}L{term.normal} link"
                f"  {MAGENTA}S{term.normal} settings"
                f"  {DIM}Q{term.normal} quit")
        E(term.move(h-2, 0) + BG2 + _pad(keys, w) + term.normal)
        E(term.move(h-1, 0) + BG2 + " " * w + term.normal)

    def draw_link_overlay(self, state) -> None:
        """Overlay: instruct user to switch to their Claude terminal to link it."""
        h, w = term.height, term.width
        bw   = min(w - 6, 64)
        col  = (w - bw - 4) // 2
        row  = h // 2 - 4

        sid  = state.link_session[:8]
        out: list[str] = []
        E = out.append

        lines = [
            f"  {YELLOW}{term.bold}Linking session {sid}{term.normal}",
            "",
            f"  {FG}Switch to the Claude terminal window{term.normal}",
            f"  {FG}you want to link, then come back.{term.normal}",
            "",
            f"  {DIM}The daemon will detect the switch{term.normal}",
            f"  {DIM}and link it automatically.{term.normal}",
            "",
            f"  {DIM}Esc to cancel{term.normal}",
        ]

        E(term.move(row,   col) + BG2 + DIM + "+" + "-" * bw + "+" + term.normal)
        for i, line in enumerate(lines):
            E(term.move(row+1+i, col) + BG3 + _pad("| " + line, bw+1) + DIM + "|" + term.normal)
        E(term.move(row+1+len(lines), col) + BG2 + DIM + "+" + "-" * bw + "+" + term.normal)

        sys.stdout.write("".join(out))
        sys.stdout.flush()
