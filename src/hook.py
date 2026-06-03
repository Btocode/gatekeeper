#!/usr/bin/env python3
import json
import os
import select
import socket
import sys
import time
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
    # GATEKEEPER_TIMEOUT: how long to wait for the user's decision in the TUI.
    # Default: 300s (5 min). Set to 0 for indefinite.
    wait_timeout = int(os.environ.get("GATEKEEPER_TIMEOUT", "300")) or None

    # Retry the connect up to 3 times with short back-off.  Two tool calls that
    # fire simultaneously can race on the event loop's accept queue; a brief
    # retry is enough to let the daemon catch up without falling back to the
    # terminal prompt.
    last_exc: Exception = RuntimeError("unknown")
    for attempt in range(3):
        if attempt:
            time.sleep(0.4)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(TIMEOUT_SECS)
            sock.connect(SOCKET_PATH)
            sock.settimeout(wait_timeout)
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
        except ConnectionRefusedError:
            raise  # nothing listening — don't retry, propagate immediately
        except Exception as exc:
            last_exc = exc
        finally:
            sock.close()

    raise last_exc


def decide_exit(response: dict[str, Any]) -> int | tuple[int, str]:
    if response.get("decision") == "deny":
        reason = response.get("reason", "Denied by user via gatekeeper")
        return 2, reason
    return 0


_TERMINAL_TIMEOUT = 30   # seconds before auto-deny when no input


def ask_in_terminal(request: Request) -> int | tuple[int, str]:
    """Fallback: ask directly in the Claude terminal when daemon is not running."""
    tool = request.tool_name
    cmd  = request.summary_command()
    try:
        with open("/dev/tty", "r+") as tty:
            tty.write(f"\n\033[33m[Permission required]\033[0m {tool}: {cmd}\n")
            tty.write(f"Allow? [Y/n]  (auto-denies in {_TERMINAL_TIMEOUT}s if no input) ")
            tty.flush()
            ready, _, _ = select.select([tty], [], [], _TERMINAL_TIMEOUT)
            if ready:
                answer = tty.readline().strip().lower()
                if answer in ("", "y", "yes"):
                    return 0
                return 2, f"Denied in terminal: {tool}({cmd})"
            tty.write(f"\n\033[31mAuto-denied: no response within {_TERMINAL_TIMEOUT}s\033[0m\n")
            tty.flush()
            return 2, f"Auto-denied (timeout): {tool}({cmd})"
    except Exception:
        # Can't open /dev/tty — deny rather than silently allow.
        return 2, f"Permission check failed (no tty): {tool}({cmd})"


# All tools with side effects — anything not listed here auto-allows
NEEDS_PERMISSION = {"Bash", "Edit", "Write", "NotebookEdit", "Agent"}


def main() -> None:
    try:
        hook_input = read_hook_input()
        request    = make_request(hook_input)

        if request.tool_name not in NEEDS_PERMISSION:
            sys.exit(0)

        try:
            response = send_request(request)
            result   = decide_exit(response)
        except ConnectionRefusedError:
            # Nothing is listening on the socket — daemon not running or stale
            # socket file left behind after a crash.  Fall back to terminal.
            result = ask_in_terminal(request)
        except Exception:
            if os.path.exists(SOCKET_PATH):
                # Socket exists but daemon unreachable after retries — deny safely.
                # User is likely watching the gatekeeper TUI; a hidden terminal
                # prompt would block Claude indefinitely.
                result = 2, (f"Gatekeeper unreachable for {request.tool_name}"
                             f"({request.summary_command()[:40]}) — denied")
            else:
                # No socket at all — daemon was never started.
                result = ask_in_terminal(request)

        if isinstance(result, tuple):
            code, message = result
            print(message)
            sys.exit(code)
        sys.exit(result)
    except Exception as e:
        print(f"Gatekeeper hook error: {e}")
        sys.exit(2)


if __name__ == "__main__":
    main()
