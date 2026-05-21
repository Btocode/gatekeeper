#!/usr/bin/env python3
"""
Claude Permission Manager — Kitty edition.

Uses blessed for direct 24-bit terminal rendering (optimised for Kitty).
Runs an asyncio Unix socket server alongside a non-blocking key input loop.

Keys:
  ↑ / k   cursor up          ↓ / j   cursor down
  A        allow              D       deny (with optional message)
  M        compose message → send to Claude session
  Q / Esc  quit
"""
import asyncio
import json
import os
import sys
import time

import blessed

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.protocol import HistoryEntry, Request, SOCKET_PATH
from src.server import RequestQueue, serve_unix_socket
from src.ui import Renderer, UIState, term


async def _read_key(loop: asyncio.AbstractEventLoop) -> blessed.keyboard.Keystroke | None:
    return await loop.run_in_executor(None, lambda: term.inkey(timeout=0.05))


def _read_line_from_tty(prompt: str) -> str:
    """Open /dev/tty directly to read a line — works even when stdin is a pipe."""
    with open("/dev/tty", "r+") as tty:
        tty.write(prompt)
        tty.flush()
        return tty.readline().rstrip("\n")


async def run() -> None:
    loop  = asyncio.get_event_loop()
    queue = RequestQueue()
    state = UIState(queue=queue)
    renderer = Renderer(state)

    # ── socket server ─────────────────────────────────────────────────────────

    async def on_request(request: Request, writer: asyncio.StreamWriter) -> None:
        state.dirty = True

    server = await serve_unix_socket(SOCKET_PATH, queue, on_request)

    # ── resolve ───────────────────────────────────────────────────────────────

    async def resolve(decision: str, custom_reason: str = "") -> None:
        if not queue.pending or state.cursor >= len(queue.pending):
            return
        item = queue.pending[state.cursor]
        r    = item.request
        resp: dict = {"decision": decision}
        if decision == "deny":
            resp["reason"] = custom_reason or "Denied by user via claude-perm-daemon"
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

    # ── input overlay helpers ──────────────────────────────────────────────────

    def _show_input_overlay(title: str, hint: str) -> None:
        """Draw a simple input prompt overlay in the bottom portion of the screen."""
        h, w = term.height, term.width
        box_w = min(w - 8, 72)
        col   = (w - box_w) // 2
        r     = h - 6

        from src.ui import BG2, BG3, BLUE, DIM, FG, YELLOW
        lines = [
            f"{BG2}{BLUE}  ╭{'─' * (box_w - 2)}╮  {term.normal}",
            f"{BG2}{BLUE}  │{term.normal}{BG3}{YELLOW} {title:<{box_w - 3}}{term.normal}{BG2}{BLUE}│  {term.normal}",
            f"{BG2}{BLUE}  │{term.normal}{BG3}{DIM} {hint:<{box_w - 3}}{term.normal}{BG2}{BLUE}│  {term.normal}",
            f"{BG2}{BLUE}  │{term.normal}{BG3}{FG} >{' ' * (box_w - 4)}{term.normal}{BG2}{BLUE}│  {term.normal}",
            f"{BG2}{BLUE}  ╰{'─' * (box_w - 2)}╯  {term.normal}",
        ]
        for i, line in enumerate(lines):
            sys.stdout.write(term.move(r + i, col) + line)
        # position cursor inside the input box
        sys.stdout.write(term.move(r + 3, col + 5))
        sys.stdout.flush()

    async def prompt_input(title: str, hint: str) -> str | None:
        """
        Temporarily exit cbreak, show an overlay, read a line, restore cbreak.
        Returns None if cancelled (empty input or Ctrl-C).
        """
        # Draw overlay before leaving cbreak
        _show_input_overlay(title, hint)
        # Read line using /dev/tty in executor (blocking)
        try:
            text = await loop.run_in_executor(
                None,
                lambda: _read_line_from_tty("")
            )
            return text.strip() or None
        except Exception:
            return None

    # ── main loop ─────────────────────────────────────────────────────────────

    last_draw = 0.0

    with term.fullscreen(), term.hidden_cursor(), term.cbreak():
        sys.stdout.write(term.home + term.clear)
        sys.stdout.flush()
        renderer.draw()

        try:
            while True:
                key = await _read_key(loop)
                now = time.time()

                # tick animations every ~50ms call
                state.tick += 1
                if state.tick % 10 == 0:
                    if queue.pending or not state.history:
                        state.dirty = True

                if key:
                    k = str(key)

                    if k in ("q", "Q") or key.name == "KEY_ESCAPE":
                        break

                    elif k in ("a", "A"):
                        await resolve("allow")

                    elif k in ("d", "D"):
                        if queue.pending:
                            # Ask for optional custom denial reason
                            reason = await prompt_input(
                                "Deny with reason",
                                "Type reason (Enter to use default, Ctrl-C to cancel)"
                            )
                            state.dirty = True
                            await resolve("deny", reason or "")

                    elif k in ("m", "M"):
                        # Send a free-form message to the selected session
                        if queue.pending:
                            item = queue.pending[min(state.cursor, len(queue.pending) - 1)]
                            sid  = item.request.short_session()
                            msg  = await prompt_input(
                                f"Message → session {sid}",
                                "This will be sent as the deny reason to Claude"
                            )
                            state.dirty = True
                            if msg:
                                await resolve("deny", msg)

                    elif key.name in ("KEY_UP",) or k == "k":
                        if state.cursor > 0:
                            state.cursor -= 1
                            state.dirty = True

                    elif key.name in ("KEY_DOWN",) or k == "j":
                        if state.cursor < len(queue.pending) - 1:
                            state.cursor += 1
                            state.dirty = True

                if state.dirty or (now - last_draw) > 0.5:
                    renderer.draw()
                    last_draw   = now
                    state.dirty = False

        finally:
            server.close()
            try:
                await server.wait_closed()
            except Exception:
                pass
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)
            sys.stdout.write(term.normal + term.home + term.clear)
            sys.stdout.flush()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
