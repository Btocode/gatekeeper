import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

SOCKET_PATH = f"/tmp/gatekeeper-{os.environ.get('USER', 'user')}.sock"
TIMEOUT_SECS = 2


@dataclass
class Request:
    id: str
    session_id: str
    tool_name: str
    tool_input: dict[str, Any]
    cwd: str
    timestamp:    float = field(default_factory=time.time)
    tty_path:     str   = ""   # PTY slave path, e.g. /dev/pts/1
    terminal_pid: int   = 0    # PID of terminal emulator (gnome-terminal etc.)

    def age_str(self) -> str:
        elapsed = int(time.time() - self.timestamp)
        if elapsed < 60:
            return f"{elapsed}s ago"
        return f"{elapsed // 60}m ago"

    def short_session(self) -> str:
        return self.session_id[:6] if self.session_id else "unknown"

    def summary_command(self) -> str:
        if self.tool_name == "Bash":
            return self.tool_input.get("command", "")[:60]
        if self.tool_name in ("Edit", "Write", "Read"):
            return self.tool_input.get("file_path", "")[:60]
        return json.dumps(self.tool_input)[:60]

    def persistent_allow_label(self) -> str:
        """Generate the dynamic 'Yes, always allow...' option label."""
        home = os.path.expanduser("~")
        if self.tool_name == "Bash":
            cmd = self.tool_input.get("command", "").strip()
            first_word = os.path.basename(cmd.split()[0]) if cmd else "this command"
            return f'Yes, allow "{first_word}" commands from this project'
        if self.tool_name in ("Edit", "Write"):
            path = self.tool_input.get("file_path", self.cwd)
            d = os.path.dirname(os.path.abspath(path)).replace(home, "~")
            verb = "edits in" if self.tool_name == "Edit" else "writes to"
            return f"Yes, allow {verb} {d}/ from this project"
        if self.tool_name == "NotebookEdit":
            path = self.tool_input.get("notebook_path", self.cwd)
            d = os.path.dirname(os.path.abspath(path)).replace(home, "~")
            return f"Yes, allow notebook edits in {d}/ from this project"
        if self.tool_name == "Agent":
            return "Yes, auto-approve this session from now on"
        return "Yes, always allow this from this project"

    def persistent_allow_action(self) -> tuple[str, str]:
        """Return (action_type, value) describing the persistent rule to save.

        action_type: "bash_pattern" | "edit_dir" | "auto_approve"
        """
        if self.tool_name == "Bash":
            cmd = self.tool_input.get("command", "").strip()
            first_word = os.path.basename(cmd.split()[0]) if cmd else ""
            return ("bash_pattern", f"{first_word} *") if first_word else ("bash_pattern", cmd)
        if self.tool_name in ("Edit", "Write"):
            path = self.tool_input.get("file_path", self.cwd)
            return ("edit_dir", os.path.dirname(os.path.abspath(path)))
        if self.tool_name == "NotebookEdit":
            path = self.tool_input.get("notebook_path", self.cwd)
            return ("edit_dir", os.path.dirname(os.path.abspath(path)))
        if self.tool_name == "Agent":
            return ("auto_approve", self.session_id)
        return ("bash_pattern", "")

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "cwd": self.cwd,
            "timestamp": self.timestamp,
            "tty_path":     self.tty_path,
            "terminal_pid": self.terminal_pid,
        }) + "\n"

    @classmethod
    def from_json(cls, data: bytes | str) -> "Request":
        d = json.loads(data)
        return cls(
            id=d["id"],
            session_id=d["session_id"],
            tool_name=d["tool_name"],
            tool_input=d["tool_input"],
            cwd=d["cwd"],
            timestamp=d.get("timestamp", time.time()),
            tty_path=d.get("tty_path", ""),
            terminal_pid=d.get("terminal_pid", 0),
        )


@dataclass
class HistoryEntry:
    session_id: str
    tool_name: str
    command_summary: str
    decision: str
    timestamp: float = field(default_factory=time.time)

    def short_session(self) -> str:
        return self.session_id[:6] if self.session_id else "unknown"
