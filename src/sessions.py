"""
Session registry — tracks active Claude Code sessions seen via the hook.
Sends messages to sessions by writing directly to their PTY device.
Falls back to Kitty remote control if PTY is unavailable.
"""
import json
import os
import subprocess
import time
from dataclasses import dataclass, field

KITTY_BIN = os.path.expanduser("~/.local/kitty.app/bin/kitty")


@dataclass
class Session:
    session_id: str
    cwd:        str
    tty_path:   str   = ""
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
        return (time.time() - self.last_seen) < 300

    def tty_label(self) -> str:
        if self.tty_path:
            return self.tty_path.replace("/dev/", "")
        return "—"


class SessionRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def touch(self, session_id: str, cwd: str, tty_path: str = "") -> Session:
        if session_id not in self._sessions:
            self._sessions[session_id] = Session(
                session_id=session_id, cwd=cwd, tty_path=tty_path
            )
        s = self._sessions[session_id]
        s.last_seen   = time.time()
        s.cwd         = cwd
        s.tool_count += 1
        if tty_path:
            s.tty_path = tty_path
        return s

    def active(self) -> list[Session]:
        return sorted(
            [s for s in self._sessions.values() if s.is_active()],
            key=lambda s: s.last_seen, reverse=True,
        )


# ── message sending ───────────────────────────────────────────────────────────

def send_message_to_session(session: Session, text: str) -> tuple[bool, str]:
    """
    Send text to a Claude session's terminal.
    Tries PTY write first, then Kitty remote control.
    Returns (success, method_used).
    """
    # 1. Direct PTY write (most reliable, works with any terminal)
    if session.tty_path:
        try:
            with open(session.tty_path, "w") as tty:
                tty.write(text + "\n")
                tty.flush()
            return True, f"pty:{session.tty_path}"
        except Exception as e:
            pass

    # 2. Kitty remote control fallback
    if os.path.exists(KITTY_BIN):
        win_id = _find_kitty_window(session.cwd)
        if win_id is not None:
            try:
                r = subprocess.run(
                    [KITTY_BIN, "@", "--to", "unix:/tmp/kitty-remote",
                     "send-text", "--match", f"id:{win_id}", text + "\n"],
                    capture_output=True, timeout=3
                )
                if r.returncode == 0:
                    return True, f"kitty:window/{win_id}"
            except Exception:
                pass

    return False, "no channel available"


def _find_kitty_window(cwd: str) -> int | None:
    """Find Kitty window ID whose foreground process cwd matches."""
    try:
        r = subprocess.run(
            [KITTY_BIN, "@", "ls"],
            capture_output=True, text=True, timeout=2
        )
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        for tab_group in data:
            for tab in tab_group.get("tabs", []):
                for win in tab.get("windows", []):
                    for proc in win.get("foreground_processes", []):
                        proc_cwd = proc.get("cwd", "")
                        proc_cmd = " ".join(proc.get("cmdline", []))
                        if cwd and proc_cwd.startswith(cwd[:20]):
                            return win.get("id")
                        if "claude" in proc_cmd.lower():
                            return win.get("id")
    except Exception:
        pass
    return None


def kitty_available() -> bool:
    return os.path.exists(KITTY_BIN)
