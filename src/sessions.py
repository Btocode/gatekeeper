"""
Session registry — tracks active Claude Code sessions seen via the hook.
Also handles sending messages to sessions via Kitty remote control.
"""
import json
import os
import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class Session:
    session_id: str
    cwd: str
    first_seen: float = field(default_factory=time.time)
    last_seen:  float = field(default_factory=time.time)
    tool_count: int   = 0

    def short_id(self) -> str:
        return self.session_id[:8]

    def short_cwd(self) -> str:
        cwd = self.cwd.replace(os.path.expanduser("~"), "~")
        return cwd[-28:] if len(cwd) > 28 else cwd

    def age_str(self) -> str:
        s = int(time.time() - self.last_seen)
        if s < 60:   return f"{s}s ago"
        if s < 3600: return f"{s//60}m ago"
        return f"{s//3600}h ago"

    def is_active(self) -> bool:
        return (time.time() - self.last_seen) < 300  # 5 min


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def touch(self, session_id: str, cwd: str) -> Session:
        if session_id not in self._sessions:
            self._sessions[session_id] = Session(session_id=session_id, cwd=cwd)
        s = self._sessions[session_id]
        s.last_seen  = time.time()
        s.cwd        = cwd
        s.tool_count += 1
        return s

    def active(self) -> list[Session]:
        return sorted(
            [s for s in self._sessions.values() if s.is_active()],
            key=lambda s: s.last_seen,
            reverse=True,
        )

    def all(self) -> list[Session]:
        return sorted(self._sessions.values(), key=lambda s: s.last_seen, reverse=True)


# ── Kitty remote control ──────────────────────────────────────────────────────

def kitty_windows() -> list[dict]:
    """Return list of Kitty window dicts from `kitty @ ls`."""
    try:
        out = subprocess.run(
            ["kitty", "@", "ls"],
            capture_output=True, text=True, timeout=2
        )
        if out.returncode != 0:
            return []
        data = json.loads(out.stdout)
        windows = []
        for tab_group in data:
            for tab in tab_group.get("tabs", []):
                for win in tab.get("windows", []):
                    windows.append(win)
        return windows
    except Exception:
        return []


def send_to_kitty_window(window_id: int, text: str) -> bool:
    """Send text to a specific Kitty window. Returns True on success."""
    try:
        result = subprocess.run(
            ["kitty", "@", "send-text", "--match", f"id:{window_id}", text + "\n"],
            capture_output=True, timeout=3
        )
        return result.returncode == 0
    except Exception:
        return False


def find_kitty_window_for_session(session_id: str, cwd: str) -> int | None:
    """
    Try to find the Kitty window running a Claude session.
    Matches by cwd appearing in the window's foreground process working directory.
    """
    try:
        windows = kitty_windows()
        for win in windows:
            # Check window title or foreground process
            title = win.get("title", "")
            fg    = win.get("foreground_processes", [])
            for proc in fg:
                proc_cwd = proc.get("cwd", "")
                proc_cmd = " ".join(proc.get("cmdline", []))
                if cwd and proc_cwd.startswith(cwd[:20]):
                    return win.get("id")
                if "claude" in proc_cmd.lower():
                    return win.get("id")
        return None
    except Exception:
        return None


def kitty_available() -> bool:
    try:
        r = subprocess.run(["kitty", "@", "ls"], capture_output=True, timeout=2)
        return r.returncode == 0
    except Exception:
        return False
