import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

SOCKET_PATH = f"/tmp/claude-perm-{os.environ.get('USER', 'user')}.sock"
TIMEOUT_SECS = 2


@dataclass
class Request:
    id: str
    session_id: str
    tool_name: str
    tool_input: dict[str, Any]
    cwd: str
    timestamp: float = field(default_factory=time.time)
    tty_path: str   = ""     # controlling TTY of the Claude process, e.g. /dev/pts/1

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

    def to_json(self) -> str:
        return json.dumps({
            "id": self.id,
            "session_id": self.session_id,
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "cwd": self.cwd,
            "timestamp": self.timestamp,
            "tty_path": self.tty_path,
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
