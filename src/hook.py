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


def make_request(hook_input: dict[str, Any]) -> Request:
    return Request(
        id=str(uuid.uuid4()),
        session_id=hook_input.get("session_id", "unknown"),
        tool_name=hook_input.get("tool_name", "unknown"),
        tool_input=hook_input.get("tool_input", {}),
        cwd=hook_input.get("cwd", os.getcwd()),
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


def main() -> None:
    try:
        hook_input = read_hook_input()
        request = make_request(hook_input)
        response = send_request(request)
        result = decide_exit(response)
        if isinstance(result, tuple):
            code, message = result
            print(message)
            sys.exit(code)
        sys.exit(result)
    except Exception:
        sys.exit(0)


if __name__ == "__main__":
    main()
