"""
Session registry and X11 message injection.

TTY detection: read /proc/PPID/stat field 6 (tty_nr) to get the
Claude process's controlling terminal — works even when the hook
itself has no controlling terminal.

Window matching: find all windows for the terminal emulator PID, then
match against session CWD in the window title. Falls back to any
non-daemon window if no title match.
"""
import os
import re
import subprocess
import time
from dataclasses import dataclass, field


# ── session ───────────────────────────────────────────────────────────────────

@dataclass
class Session:
    session_id:   str
    cwd:          str
    tty_path:     str = ""
    terminal_pid: int = 0
    first_seen:   float = field(default_factory=time.time)
    last_seen:    float = field(default_factory=time.time)
    tool_count:   int   = 0

    def short_id(self)  -> str: return self.session_id[:8]
    def tty_label(self) -> str: return self.tty_path.replace("/dev/", "") if self.tty_path else "?"
    def age_str(self)   -> str:
        s = int(time.time() - self.last_seen)
        if s < 60:   return f"{s}s ago"
        if s < 3600: return f"{s//60}m ago"
        return f"{s//3600}h ago"
    def is_active(self) -> bool:
        return (time.time() - self.last_seen) < 300
    def short_cwd(self) -> str:
        cwd = self.cwd.replace(os.path.expanduser("~"), "~")
        return cwd[-28:] if len(cwd) > 28 else cwd


class SessionRegistry:
    def __init__(self) -> None:
        self._map: dict[str, Session] = {}

    def touch(self, session_id: str, cwd: str,
              tty_path: str = "", terminal_pid: int = 0) -> Session:
        if session_id not in self._map:
            self._map[session_id] = Session(
                session_id=session_id, cwd=cwd,
                tty_path=tty_path, terminal_pid=terminal_pid,
            )
        s = self._map[session_id]
        s.last_seen   = time.time()
        s.cwd         = cwd
        s.tool_count += 1
        if tty_path:     s.tty_path     = tty_path
        if terminal_pid: s.terminal_pid = terminal_pid
        return s

    def active(self) -> list[Session]:
        return sorted(
            [s for s in self._map.values() if s.is_active()],
            key=lambda s: s.last_seen, reverse=True,
        )


# ── TTY detection (called from hook) ──────────────────────────────────────────

def detect_tty_from_parent(ppid: int) -> str:
    """
    Read the controlling TTY of process `ppid` via /proc/ppid/stat field 6.
    Returns a path like '/dev/pts/2' or '' if none.
    """
    try:
        with open(f"/proc/{ppid}/stat") as f:
            raw = f.read()
        # Format: pid (comm) state ppid pgroup session tty_nr ...
        # comm may contain spaces, so split on last ')' first
        after_comm = raw.rsplit(")", 1)[1].split()
        tty_nr = int(after_comm[4])          # field 6 overall
        major  = os.major(tty_nr)
        minor  = os.minor(tty_nr)
        if major == 136:                     # pts devices
            return f"/dev/pts/{minor}"
        if major == 4:                       # tty devices
            return f"/dev/tty{minor}"
    except Exception:
        pass
    return ""


TERMINAL_NAMES = frozenset({
    "gnome-terminal-", "gnome-terminal", "kitty", "alacritty",
    "xterm", "konsole", "xfce4-terminal", "tilix", "terminator",
    "foot", "wezterm",
})


def find_terminal_pid(from_pid: int) -> int:
    """Walk up the process tree from `from_pid` until a terminal emulator is found."""
    pid = from_pid
    for _ in range(8):
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "ppid=,comm="],
                capture_output=True, text=True, timeout=1,
            )
            parts = r.stdout.strip().split(None, 1)
            if len(parts) < 2:
                break
            ppid, comm = int(parts[0]), parts[1].strip()
            if any(t in comm for t in TERMINAL_NAMES):
                return pid
            pid = ppid
        except Exception:
            break
    return 0


# ── X11 window helpers ────────────────────────────────────────────────────────

def _x11_windows_for_pid(pid: int) -> list[tuple[int, str]]:
    """Return [(window_id, title), ...] for all X11 windows owned by `pid`."""
    try:
        from Xlib import display, X
        d         = display.Display()
        root      = d.screen().root
        pid_atom  = d.intern_atom("_NET_WM_PID")
        name_atom = d.intern_atom("_NET_WM_NAME")
        results   = []

        def _walk(win) -> None:
            try:
                pp = win.get_full_property(pid_atom, X.AnyPropertyType)
                if pp and pp.value and int(pp.value[0]) == pid:
                    np    = win.get_full_property(name_atom, X.AnyPropertyType)
                    title = (np.value.decode("utf-8", "ignore") if np else "")
                    results.append((win.id, title))
            except Exception:
                pass
            try:
                for child in win.query_tree().children:
                    _walk(child)
            except Exception:
                pass

        _walk(root)
        return results
    except Exception:
        return []


def _x11_inject(window_id: int, text: str) -> bool:
    """Send `text` to X11 window as synthetic key events."""
    try:
        from Xlib import display, X, XK, protocol
        d   = display.Display()
        win = d.create_resource_object("window", window_id)

        def _key(ks: int, shift: bool = False) -> None:
            kc  = d.keysym_to_keycode(ks)
            st  = X.ShiftMask if shift else 0
            kw  = dict(time=X.CurrentTime, root=d.screen().root, window=win,
                       same_screen=1, child=X.NONE,
                       root_x=0, root_y=0, event_x=0, event_y=0,
                       state=st, detail=kc)
            win.send_event(protocol.event.KeyPress(**kw),   event_mask=X.KeyPressMask)
            win.send_event(protocol.event.KeyRelease(**kw), event_mask=X.KeyReleaseMask)

        for ch in text:
            if ch == "\n":
                _key(XK.XK_Return)
            elif ch == " ":
                _key(XK.XK_space)
            else:
                ks    = XK.string_to_keysym(ch)
                shift = ch.isupper() or ch in '!"#$%&\'()*+:<>?@^_{|}~'
                if ks == 0:
                    ks = XK.string_to_keysym(ch.lower())
                _key(ks, shift)

        d.flush()
        return True
    except Exception:
        return False


# ── send message ──────────────────────────────────────────────────────────────

def send_message_to_session(session: Session, text: str) -> tuple[bool, str]:
    """
    Find the terminal window for this session and inject `text` as keystrokes.
    Matching strategy:
      1. All windows for the terminal emulator PID
      2. Prefer window whose title contains part of the session CWD
      3. Exclude the daemon's own window (title contains 'claude-perm')
      4. Fall back to first eligible window
    """
    term_pid = session.terminal_pid
    if not term_pid and session.tty_path:
        term_pid = find_terminal_pid_from_tty(session.tty_path)
    if not term_pid:
        return False, "no terminal PID"

    windows = _x11_windows_for_pid(term_pid)
    if not windows:
        return False, f"no X11 windows for PID {term_pid}"

    # Exclude the daemon window and other perm-manager windows
    eligible = [(wid, title) for wid, title in windows
                if "claude-perm" not in title.lower()
                and "perm-manager" not in title.lower()]

    if not eligible:
        return False, "only daemon window found"

    # Score each window:
    #  +5  non-generic title (not just "Terminal" or blank)
    #  +1  each CWD segment found in title
    #  +3  title contains "claude" (likely a Claude Code session tab)
    GENERIC = {"terminal", "bash", "zsh", "sh", ""}
    cwd_parts = [p for p in
                 session.cwd.replace(os.path.expanduser("~"), "~").split("/") if p]
    best_wid, best_score = eligible[0][0], -999
    for wid, title in eligible:
        tl = title.lower().strip()
        score  = 0
        score += 0 if tl in GENERIC else 5
        score += sum(1 for p in cwd_parts if p.lower() in tl)
        score += 3 if "claude" in tl else 0
        if score > best_score:
            best_wid, best_score = wid, score

    ok = _x11_inject(best_wid, text + "\n")
    if ok:
        return True, f"x11:win/{best_wid}"
    return False, f"x11 inject failed for win/{best_wid}"


def find_terminal_pid_from_tty(tty_path: str) -> int:
    """Find terminal emulator PID from any process on the given TTY."""
    tty_name = tty_path.replace("/dev/", "")
    try:
        r = subprocess.run(
            ["ps", "-t", tty_name, "-o", "pid="],
            capture_output=True, text=True, timeout=2,
        )
        for pid_str in r.stdout.strip().splitlines():
            pid = int(pid_str.strip())
            term = find_terminal_pid(pid)
            if term:
                return term
    except Exception:
        pass
    return 0


def kitty_available() -> bool:
    return os.path.exists(os.path.expanduser("~/.local/kitty.app/bin/kitty"))
