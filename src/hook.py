#!/usr/bin/env python3
import json
import os
import socket
import sys
import uuid
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.protocol import Request, SOCKET_PATH, TIMEOUT_SECS


def read_hook_input() -> dict[str, Any]:
    return json.load(sys.stdin)


def _detect_tty_and_terminal() -> tuple[str, int]:
    """
    Detect the Claude process's PTY and the terminal emulator PID.
    - TTY: read /proc/PPID/stat field 6 (tty_nr) — works even when hook has no TTY
    - Terminal PID: walk process tree upward from PPID until a terminal is found
    """
    import subprocess
    from src.sessions import detect_tty_from_parent, find_terminal_pid

    ppid     = os.getppid()
    tty_path = detect_tty_from_parent(ppid)
    term_pid = find_terminal_pid(ppid)
    return tty_path, term_pid


def make_request(hook_input: dict[str, Any]) -> Request:
    tty_path, term_pid = _detect_tty_and_terminal()
    return Request(
        id=str(uuid.uuid4()),
        session_id=hook_input.get("session_id", "unknown"),
        tool_name=hook_input.get("tool_name", "unknown"),
        tool_input=hook_input.get("tool_input", {}),
        cwd=hook_input.get("cwd", os.getcwd()),
        tty_path=tty_path,
        terminal_pid=term_pid,
    )


def send_request(request: Request) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(TIMEOUT_SECS)
        sock.connect(SOCKET_PATH)
        sock.settimeout(None)  # wait as long as needed for user to respond
        sock.sendall(request.to_json().encode())
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        if not buf.strip():
            raise ValueError("Empty response from daemon")
        return json.loads(buf.strip())
    finally:
        sock.close()


def decide_exit(response: dict[str, Any]) -> int | tuple[int, str]:
    if response.get("decision") == "deny":
        reason = response.get("reason", "Denied by user via claude-perm-daemon")
        return 2, reason
    return 0


def ask_in_terminal(request: Request) -> int | tuple[int, str]:
    """Fallback: ask directly in the Claude terminal when daemon is not running."""
    tool = request.tool_name
    cmd  = request.summary_command()
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(f"\n\033[33m[Permission required]\033[0m {tool}: {cmd}\n")
            tty.write("Allow? [Y/n] ")
            tty.flush()
            answer = tty.readline().strip().lower()
        if answer in ("", "y", "yes"):
            return 0
        return 2, f"Denied in terminal: {tool}({cmd})"
    except Exception:
        return 0  # can't open tty — allow


# Only these tools have side effects — everything else auto-allows
NEEDS_PERMISSION = {"Bash", "Edit", "NotebookEdit", "Agent"}


def main() -> None:
    try:
        hook_input = read_hook_input()
        request    = make_request(hook_input)

        if request.tool_name not in NEEDS_PERMISSION:
            sys.exit(0)
        try:
            response = send_request(request)
            result   = decide_exit(response)
        except Exception:
            # Daemon not reachable — fall back to terminal prompt
            result = ask_in_terminal(request)
        if isinstance(result, tuple):
            code, message = result
            print(message)
            sys.exit(code)
        sys.exit(result)
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
