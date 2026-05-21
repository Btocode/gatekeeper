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


# ── danger detection ──────────────────────────────────────────────────────────

# Commands that are always safe to auto-approve — their arguments are never checked
_SAFE_CMDS = frozenset({
    # Search / read
    'grep', 'egrep', 'fgrep', 'rg', 'ag', 'find', 'locate', 'fd',
    'cat', 'head', 'tail', 'less', 'more', 'bat',
    'ls', 'lls', 'dir', 'tree',
    'echo', 'printf', 'print',
    'wc', 'sort', 'uniq', 'cut', 'tr', 'tee', 'xargs', 'awk', 'sed',
    'diff', 'patch', 'stat', 'file', 'readlink', 'realpath', 'basename', 'dirname',
    'man', 'help', 'which', 'whereis', 'type', 'command',
    'pwd', 'date', 'whoami', 'id', 'uname', 'hostname', 'env', 'printenv',
    'ps', 'top', 'htop', 'df', 'du', 'free', 'uptime', 'lsof', 'netstat', 'ss',
    # Build / package (installs are fine, only uninstall is dangerous)
    'npm', 'npx', 'yarn', 'pnpm', 'bun',
    'pip', 'pip3', 'pip3.11', 'pipenv', 'poetry',
    'make', 'cmake', 'cargo', 'go',
    'mvn', 'gradle',
    # Network read-only
    'curl', 'wget', 'http', 'httpie', 'dig', 'nslookup', 'ping', 'traceroute',
    # Archive
    'tar', 'zip', 'unzip', 'gzip', 'gunzip', 'bzip2', 'xz',
    # SCM (git/gh read ops — dangerous git subcommands handled separately)
    'gh',
    # Misc
    'jq', 'yq', 'jo', 'python', 'python3', 'node', 'ruby', 'perl', 'lua',
    'sleep', 'wait', 'true', 'false', 'test',
    'source', '.', 'export', 'cd', 'pushd', 'popd',
    'tput', 'clear', 'reset',
    'touch', 'mkdir', 'cp', 'mv', 'ln',   # non-destructive filesystem ops
})

# Executables that are always dangerous regardless of arguments
_DANGER_CMDS = frozenset({
    'rm', 'rmdir', 'shred',
    'ssh', 'scp', 'rsync', 'sftp',
    'sudo', 'su', 'doas',
    'dd', 'mkfs', 'fdisk', 'parted',
    'kill', 'killall', 'pkill',
    'passwd', 'usermod', 'userdel', 'useradd',
    'iptables', 'ufw', 'nftables', 'firewall-cmd',
})

# Commands that are dangerous only with specific subcommands
_DANGER_SUBCMDS: dict[str, frozenset] = {
    'systemctl': frozenset({'stop', 'disable', 'mask', 'kill', 'restart', 'reload'}),
    'service':   frozenset({'stop', 'restart', 'kill'}),
    'docker':    frozenset({'rm', 'rmi', 'kill', 'stop', 'prune', 'system'}),
    'kubectl':   frozenset({'delete', 'replace', 'patch', 'drain', 'cordon'}),
    'terraform': frozenset({'apply', 'destroy'}),
    'ansible':   frozenset({'*'}),       # all ansible subcommands
    'npm':       frozenset({'uninstall', 'rm', 'remove', 'prune'}),
    'pip':       frozenset({'uninstall'}),
    'apt':       frozenset({'remove', 'purge', 'autoremove'}),
    'apt-get':   frozenset({'remove', 'purge', 'autoremove'}),
    'chmod':     frozenset({'*'}),
    'chown':     frozenset({'*'}),
}

# Sensitive file path prefixes — writing here always requires approval
_SENSITIVE_PATHS = (
    '/etc/', '/usr/', '/bin/', '/sbin/', '/boot/', '/lib/', '/lib64/',
    '/var/', '/sys/', '/proc/', '/dev/',
    os.path.expanduser('~/.ssh/'),
    os.path.expanduser('~/.aws/'),
    os.path.expanduser('~/.gnupg/'),
    os.path.expanduser('~/.config/systemd/'),
)


def _dangerous_bash(command: str) -> tuple[bool, str]:
    """Check a bash command string. Only checks executables, not quoted args."""
    # Strip quoted strings to avoid flagging grep/find args like grep "DELETE FROM"
    clean = re.sub(r'"[^"]*"', '', command)
    clean = re.sub(r"'[^']*'", '', clean)

    # Split by shell operators into individual segments
    segments = re.split(r'\|{1,2}|&&|;|\n', clean)

    for seg in segments:
        tokens = seg.strip().lstrip('(').lstrip('!').split()
        if not tokens:
            continue

        cmd = tokens[0].lstrip('(').lstrip('!')

        # Skip known-safe commands entirely
        if cmd in _SAFE_CMDS:
            continue

        # Always dangerous executables
        if cmd in _DANGER_CMDS:
            return True, f"dangerous executable: {cmd}"

        # Commands dangerous only with specific subcommands
        if cmd in _DANGER_SUBCMDS and len(tokens) > 1:
            sub = tokens[1]
            allowed = _DANGER_SUBCMDS[cmd]
            if '*' in allowed or sub in allowed:
                return True, f"dangerous: {cmd} {sub}"

        # Git — only dangerous with destructive flags
        if cmd == 'git' and len(tokens) > 1:
            sub  = tokens[1]
            rest = ' '.join(tokens[2:])
            if sub == 'push'  and '--force' in rest: return True, "git push --force"
            if sub == 'reset' and '--hard'  in rest: return True, "git reset --hard"
            if sub == 'clean' and '-f'      in rest: return True, "git clean -f"
            if sub == 'branch' and re.search(r'-[dD]', rest): return True, "git branch -D"

        # Heredoc / process substitution writing to /dev/
        if re.search(r'>\s*/dev/', seg):
            return True, "redirect to /dev/"

    return False, ""


def is_dangerous(tool_name: str, tool_input: dict[str, Any]) -> tuple[bool, str]:
    """
    Return (True, reason) if this tool call must not be auto-approved.
    For Bash: checks the executable and subcommand, NOT quoted arguments.
    For Edit/Write: only checks the file path, never the content.
    """
    if tool_name == "Bash":
        return _dangerous_bash(tool_input.get("command", ""))

    if tool_name in ("Edit", "Write"):
        path = tool_input.get("file_path", "")
        if any(path.startswith(p) for p in _SENSITIVE_PATHS):
            return True, f"sensitive path: {path}"
        return False, ""

    if tool_name == "Agent":
        # Only flag obvious dangerous prompts — keep it narrow
        prompt = tool_input.get("prompt", "")
        if re.search(r'\b(rm -rf|DROP TABLE|DELETE FROM|sudo)\b', prompt, re.IGNORECASE):
            return True, "dangerous instruction in agent prompt"
        return False, ""

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
    session_id:    str
    cwd:           str
    tty_path:      str   = ""
    terminal_pid:  int   = 0
    pinned_window: int   = 0      # explicitly linked X11 window_id (0 = none)
    first_seen:    float = field(default_factory=time.time)
    last_seen:     float = field(default_factory=time.time)
    tool_count:    int   = 0
    last_tool_at:  float = field(default_factory=time.time)  # last hook call
    waiting_input: bool  = False  # Claude finished and is waiting for user input
    session_updated_at: int = 0   # last updatedAt from session file (ms timestamp)

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

    def unpin_window(self, session_id: str) -> None:
        """Remove the window link for a session."""
        if session_id in self._map:
            self._map[session_id].pinned_window = 0
        self._window_map.pop(session_id, None)
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
        s.last_seen    = time.time()
        s.last_tool_at = time.time()
        s.cwd          = cwd
        s.tool_count  += 1
        s.waiting_input = False   # reset on any tool call
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

        # d.sync() waits until the X server has processed ALL events (including
        # the final Return key) before we restore focus — without this the Enter
        # can land in the wrong window because focus is restored too soon.
        d.sync()
        _time.sleep(0.05)

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

SESSIONS_DIR    = os.path.expanduser("~/.claude/sessions")
# How long after the last tool call before we consider Claude is waiting for input
WAITING_THRESHOLD = 8.0   # seconds


def poll_waiting_sessions(registry: "SessionRegistry") -> None:
    """
    Read ~/.claude/sessions/*.json and detect sessions where Claude has
    finished responding and is waiting for user input.

    Heuristic: session file's updatedAt changed more recently than our
    last tool call for that session, AND at least WAITING_THRESHOLD seconds
    have passed since the last tool call.
    """
    import json as _json

    if not os.path.isdir(SESSIONS_DIR):
        return

    for fname in os.listdir(SESSIONS_DIR):
        if not fname.endswith(".json"):
            continue
        pid_str = fname[:-5]
        if not pid_str.isdigit():
            continue

        try:
            with open(os.path.join(SESSIONS_DIR, fname)) as f:
                data = _json.load(f)
        except Exception:
            continue

        session_id  = data.get("sessionId", "")
        updated_ms  = data.get("updatedAt", 0)
        status      = data.get("status", "")

        if not session_id or session_id not in registry._map:
            continue

        s = registry._map[session_id]

        # Skip if session file hasn't changed since we last saw it
        if updated_ms <= s.session_updated_at:
            continue

        s.session_updated_at = updated_ms
        updated_sec = updated_ms / 1000.0
        now         = time.time()

        # If updatedAt is recent AND no tool call in WAITING_THRESHOLD seconds
        # → Claude responded and is likely waiting for user input
        since_tool   = now - s.last_tool_at
        since_update = now - updated_sec

        if since_tool >= WAITING_THRESHOLD and since_update < 120:
            s.waiting_input = True
        else:
            s.waiting_input = False


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
