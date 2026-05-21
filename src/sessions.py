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
from typing import Any


# ── danger patterns ───────────────────────────────────────────────────────────
# Commands matching any of these are NEVER auto-approved regardless of session
# settings. They must go through manual approval every time.

_DANGER_PATTERNS: list[re.Pattern] = [p for p in (re.compile(x, re.IGNORECASE) for x in [
    # Filesystem destruction
    r'\brm\b',
    r'\brmdir\b',
    r'\bshred\b',
    r'\btruncate\b',
    r'>\s*/dev/',                   # overwrite device files

    # Database destructive ops
    r'\bDROP\s+(TABLE|DATABASE|SCHEMA|INDEX|VIEW|FUNCTION|PROCEDURE|TRIGGER)\b',
    r'\bDELETE\s+FROM\b',
    r'\bTRUNCATE\b',
    r'\bALTER\s+TABLE\b',
    r'\bDROP\s+COLUMN\b',
    r'\bUPDATE\b.+\bSET\b',        # UPDATE ... SET (broad but catches most mutations)
    r'\bmongo.*drop\b',
    r'\bredis-cli.*flushall\b',
    r'\bredis-cli.*flushdb\b',

    # SSH / remote access
    r'\bssh\b',
    r'\bscp\b',
    r'\brsync\b',
    r'\bsftp\b',
    r'\bansible\b',
    r'\bterraform\s+(apply|destroy)\b',
    r'\bkubectl\s+(delete|apply|replace|patch|drain|cordon)\b',
    r'\bdocker\s+(rm|rmi|kill|stop|prune|system\s+prune)\b',

    # Server / service management
    r'\bsystemctl\s+(stop|disable|mask|kill|restart)\b',
    r'\bservice\s+\w+\s+(stop|restart|kill)\b',
    r'\bkill\b',
    r'\bkillall\b',
    r'\bpkill\b',

    # Privilege escalation
    r'\bsudo\b',
    r'\bsu\s+',
    r'\bchmod\b',
    r'\bchown\b',

    # Package / environment destructive
    r'\bapt(-get)?\s+remove\b',
    r'\bapt(-get)?\s+purge\b',
    r'\bpip\s+uninstall\b',
    r'\bnpm\s+uninstall\b',

    # Network / firewall
    r'\biptables\b',
    r'\bufw\b',
    r'\bnftables\b',

    # Disk / partition
    r'\bdd\s+',
    r'\bmkfs\b',
    r'\bfdisk\b',
    r'\bparted\b',
    r'\bformat\b',

    # Git destructive
    r'\bgit\s+(push\s+.*--force|reset\s+--hard|clean\s+-f|branch\s+-[dD])\b',
])]


def is_dangerous(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    """
    Return (True, reason) if this tool call must not be auto-approved.
    Checks the command/content against known destructive patterns.
    """
    # Extract the text to check based on tool type
    text = ""
    if tool_name == "Bash":
        text = tool_input.get("command", "")
    elif tool_name in ("Edit", "Write"):
        # Check file path for sensitive locations
        path = tool_input.get("file_path", "")
        sensitive = ["/etc/", "/usr/", "/bin/", "/sbin/", "/boot/",
                     "/var/", "/sys/", "/proc/", "~/.ssh/", "~/.aws/"]
        for s in sensitive:
            if path.startswith(s) or path.startswith(os.path.expanduser(s)):
                return True, f"write to sensitive path: {path}"
        # Also check content for SQL mutations
        text = tool_input.get("new_string", "") or tool_input.get("content", "")
    elif tool_name == "Agent":
        text = str(tool_input.get("prompt", ""))

    if not text:
        return False, ""

    for pattern in _DANGER_PATTERNS:
        m = pattern.search(text)
        if m:
            return True, f"matched dangerous pattern: {m.group(0)!r}"

    return False, ""
from dataclasses import dataclass, field


# ── session ───────────────────────────────────────────────────────────────────

WINDOW_MAP_FILE    = os.path.expanduser("~/.claude/perm-window-map.json")
AUTO_APPROVE_FILE  = os.path.expanduser("~/.claude/perm-auto-approve.json")


def load_auto_approve() -> set[str]:
    try:
        import json
        with open(AUTO_APPROVE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_auto_approve(s: set[str]) -> None:
    import json
    try:
        with open(AUTO_APPROVE_FILE, "w") as f:
            json.dump(list(s), f, indent=2)
    except Exception:
        pass


def load_window_map() -> dict[str, int]:
    """Load persisted session_id → window_id mappings."""
    try:
        import json
        with open(WINDOW_MAP_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_window_map(m: dict[str, int]) -> None:
    import json
    try:
        with open(WINDOW_MAP_FILE, "w") as f:
            json.dump(m, f, indent=2)
    except Exception:
        pass


@dataclass
class Session:
    session_id:   str
    cwd:          str
    tty_path:     str = ""
    terminal_pid: int = 0
    pinned_window: int = 0    # explicitly linked X11 window_id (0 = none)
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

    def __init__(self) -> None:
        self._map: dict[str, Session] = {}
        self._window_map: dict[str, int] = load_window_map()
        self.auto_approve: set[str] = load_auto_approve()

    def toggle_auto_approve(self, session_id: str) -> bool:
        """Toggle auto-approve for a session. Returns new state (True = enabled)."""
        if session_id in self.auto_approve:
            self.auto_approve.discard(session_id)
        else:
            self.auto_approve.add(session_id)
        save_auto_approve(self.auto_approve)
        return session_id in self.auto_approve

    def is_auto_approve(self, session_id: str) -> bool:
        return session_id in self.auto_approve

    def pin_window(self, session_id: str, window_id: int) -> None:
        """Explicitly link a session to an X11 window and persist it."""
        if session_id in self._map:
            self._map[session_id].pinned_window = window_id
        self._window_map[session_id] = window_id
        save_window_map(self._window_map)

    def touch(self, session_id: str, cwd: str,
              tty_path: str = "", terminal_pid: int = 0) -> Session:
        if session_id not in self._map:
            pinned = self._window_map.get(session_id, 0)
            self._map[session_id] = Session(
                session_id=session_id, cwd=cwd,
                tty_path=tty_path, terminal_pid=terminal_pid,
                pinned_window=pinned,
            )
        s = self._map[session_id]
        s.last_seen   = time.time()
        s.cwd         = cwd
        s.tool_count += 1
        if tty_path:     s.tty_path     = tty_path
        if terminal_pid: s.terminal_pid = terminal_pid
        return s

    def get_by_id(self, session_id: str) -> "Session | None":
        return self._map.get(session_id)

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
    """
    Inject text into an X11 window using XTEST — indistinguishable from real
    keyboard input (GTK/VTE won't reject it unlike synthetic send_event).
    Briefly focuses the target window, types, then restores focus.
    """
    try:
        import time as _time
        from Xlib import display, X, XK
        from Xlib.ext import xtest

        d    = display.Display()
        win  = d.create_resource_object("window", window_id)

        # Save current focus
        prev_focus = d.get_input_focus().focus

        # Focus target window
        win.set_input_focus(X.RevertToParent, X.CurrentTime)
        d.flush()
        _time.sleep(0.05)   # let focus settle

        def _fake(ks: int, shift: bool = False) -> None:
            kc = d.keysym_to_keycode(ks)
            if kc == 0:
                return
            if shift:
                xtest.fake_input(d, X.KeyPress,   50)   # Left Shift keycode
            xtest.fake_input(d, X.KeyPress,   kc)
            xtest.fake_input(d, X.KeyRelease, kc)
            if shift:
                xtest.fake_input(d, X.KeyRelease, 50)
            d.flush()

        for ch in text:
            if ch == "\n":
                _fake(XK.XK_Return)
            elif ch == " ":
                _fake(XK.XK_space)
            else:
                ks    = XK.string_to_keysym(ch)
                shift = ch.isupper() or ch in '!"#$%&\'()*+:<>?@^_{|}~'
                if ks == 0:
                    ks = XK.string_to_keysym(ch.lower())
                if ks:
                    _fake(ks, shift)

        _time.sleep(0.02)

        # Restore previous focus
        try:
            if prev_focus not in (X.PointerRoot, X.NONE):
                prev_focus.set_input_focus(X.RevertToParent, X.CurrentTime)
                d.flush()
        except Exception:
            pass

        return True
    except Exception as e:
        return False


# ── send message ──────────────────────────────────────────────────────────────

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")


def discover_running_sessions(registry: "SessionRegistry") -> None:
    """
    Read Claude's own session files from ~/.claude/sessions/ to get real
    session IDs, CWDs, and statuses. Falls back to ps scan if dir missing.
    """
    import json as _json

    loaded = False
    if os.path.isdir(SESSIONS_DIR):
        for fname in os.listdir(SESSIONS_DIR):
            if not fname.endswith(".json"):
                continue
            pid_str = fname[:-5]
            if not pid_str.isdigit():
                continue
            pid = int(pid_str)
            try:
                with open(os.path.join(SESSIONS_DIR, fname)) as f:
                    data = _json.load(f)

                session_id = data.get("sessionId", f"pid:{pid}")
                cwd        = data.get("cwd", "")
                status     = data.get("status", "")

                # Skip exited sessions
                if status == "exited":
                    continue

                # Verify process is still running
                if not os.path.exists(f"/proc/{pid}"):
                    continue

                tty_path = detect_tty_from_parent(pid)
                term_pid = find_terminal_pid(pid)
                registry.touch(session_id, cwd, tty_path, term_pid)
                loaded = True
            except Exception:
                continue

    if not loaded:
        # Fallback: ps scan with synthetic IDs
        try:
            r = subprocess.run(
                ["ps", "-C", "claude", "-o", "pid=,tty="],
                capture_output=True, text=True, timeout=3,
            )
            for line in r.stdout.strip().splitlines():
                parts = line.split()
                if len(parts) < 2:
                    continue
                pid_str, tty = parts[0].strip(), parts[1].strip()
                if not pid_str.isdigit() or tty == "?":
                    continue
                pid = int(pid_str)
                try:
                    cwd      = os.readlink(f"/proc/{pid}/cwd")
                    tty_path = f"/dev/{tty}"
                    term_pid = find_terminal_pid(pid)
                    registry.touch(f"pid:{pid}", cwd, tty_path, term_pid)
                except Exception:
                    continue
        except Exception:
            pass


def list_injectable_windows(terminal_pid: int) -> list[tuple[int, str]]:
    """Return all non-daemon windows for the terminal emulator, for user to pick from."""
    windows = _x11_windows_for_pid(terminal_pid)
    return [(wid, title) for wid, title in windows
            if "claude-perm" not in title.lower()
            and "perm-manager" not in title.lower()]


def send_message_to_session(session: Session, text: str) -> tuple[bool, str]:
    """
    Find the terminal window for this session and inject `text` as keystrokes.
    Matching strategy:
      1. All windows for the terminal emulator PID
      2. Prefer window whose title contains part of the session CWD
      3. Exclude the daemon's own window (title contains 'claude-perm')
      4. Fall back to first eligible window
    """
    # Use pinned window if set
    if session.pinned_window:
        ok = _x11_inject(session.pinned_window, text + "\n")
        return (True, f"x11:pinned/{session.pinned_window}") if ok \
               else (False, f"x11 inject failed on pinned/{session.pinned_window}")

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
