#!/usr/bin/env python3
"""
Claude Permission Manager — Kitty edition.

Uses blessed for direct 24-bit terminal rendering (optimised for Kitty).
Runs an asyncio Unix socket server alongside a non-blocking key input loop.
"""
import asyncio
import json
import os
import signal
import sys
import time

import blessed

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.protocol import HistoryEntry, Request, SOCKET_PATH
from src.server import RequestQueue, serve_unix_socket
from src.ui import Renderer, UIState, term


# ── key handling ─────────────────────────────────────────────────────────────

async def _read_key(loop: asyncio.AbstractEventLoop) -> blessed.keyboard.Keystroke | None:
    """Non-blocking key read — runs term.inkey in a thread so asyncio stays live."""
    return await loop.run_in_executor(None, lambda: term.inkey(timeout=0.05))


# ── main app ─────────────────────────────────────────────────────────────────

async def run() -> None:
    loop  = asyncio.get_event_loop()
    queue = RequestQueue()
    state = UIState(queue=queue)
    renderer = Renderer(state)

    # ── socket server ────────────────────────────────────────────────────────

    async def on_request(request: Request, writer: asyncio.StreamWriter) -> None:
        state.dirty = True

    server = await serve_unix_socket(SOCKET_PATH, queue, on_request)

    # ── resolve a request ────────────────────────────────────────────────────

    async def resolve(decision: str) -> None:
        if not queue.pending or state.cursor >= len(queue.pending):
            return
        item  = queue.pending[state.cursor]
        r     = item.request
        resp  = {"decision": decision}
        if decision == "deny":
            resp["reason"] = "Denied by user via claude-perm-daemon"
        if item.writer and not item.writer.is_closing():
            try:
                item.writer.write((json.dumps(resp) + "\n").encode())
                await item.writer.drain()
                item.writer.close()
                await item.writer.wait_closed()
            except Exception:
                pass
        if decision == "allow":
            state.allowed += 1
        else:
            state.denied += 1
        state.history.append(HistoryEntry(
            session_id=r.session_id,
            tool_name=r.tool_name,
            command_summary=r.summary_command(),
            decision=decision,
        ))
        queue.remove(r.id)
        if state.cursor >= len(queue.pending) and state.cursor > 0:
            state.cursor -= 1
        state.dirty = True

    # ── terminal setup ───────────────────────────────────────────────────────

    last_draw  = 0.0
    tick_count = 0

    with term.fullscreen(), term.hidden_cursor(), term.cbreak():
        sys.stdout.write(term.home + term.clear)
        sys.stdout.flush()
        renderer.draw()

        try:
            while True:
                key = await _read_key(loop)
                now = time.time()

                # periodic spinner + age refresh (every ~0.5s)
                tick_count += 1
                if tick_count % 10 == 0:
                    state.spinner = (state.spinner + 1) % 10
                    if queue.pending:
                        state.dirty = True

                if key:
                    k = str(key)
                    if k in ("q", "Q") or key.name == "KEY_ESCAPE":
                        break
                    elif k in ("a", "A"):
                        await resolve("allow")
                    elif k in ("d", "D"):
                        await resolve("deny")
                    elif key.name in ("KEY_UP", "KEY_SUP") or k == "k":
                        if state.cursor > 0:
                            state.cursor -= 1
                            state.dirty = True
                    elif key.name in ("KEY_DOWN", "KEY_SDOWN") or k == "j":
                        if state.cursor < len(queue.pending) - 1:
                            state.cursor += 1
                            state.dirty = True

                if state.dirty or (now - last_draw) > 1.0:
                    renderer.draw()
                    last_draw  = now
                    state.dirty = False

        finally:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
            # restore terminal
            sys.stdout.write(term.normal + term.home + term.clear)
            sys.stdout.flush()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
