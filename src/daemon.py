#!/usr/bin/env python3
"""
Claude Permission Manager — Kitty edition.

Three-pane TUI: Sessions | Queue | Detail
Kitty remote control for sending messages to active Claude sessions.
"""
import asyncio
import json
import os
import sys
import time
from datetime import datetime

LOG_FILE = os.path.expanduser("~/.claude/perm-manager.log")

import blessed

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.protocol import HistoryEntry, Request, SOCKET_PATH
from src.server import RequestQueue, serve_unix_socket
from src.sessions import (SessionRegistry, kitty_available, send_message_to_session,
                          list_injectable_windows, discover_running_sessions)
from src.ui import FOCUS_QUEUE, FOCUS_SESSIONS, Renderer, UIState, term


async def _key(loop: asyncio.AbstractEventLoop):
    return await loop.run_in_executor(None, lambda: term.inkey(timeout=0.05))


def _get_focused_window() -> int:
    """Return the currently focused X11 window ID, or 0 on failure."""
    try:
        from Xlib import display, X
        d    = display.Display()
        atom = d.intern_atom("_NET_ACTIVE_WINDOW")
        prop = d.screen().root.get_full_property(atom, X.AnyPropertyType)
        if prop and prop.value:
            return int(prop.value[0])
    except Exception:
        pass
    return 0


def _get_own_window() -> int:
    """Return the X11 window ID of this process (the daemon terminal)."""
    try:
        from Xlib import display, X
        d        = display.Display()
        pid_atom = d.intern_atom("_NET_WM_PID")

        def _walk(win) -> int:
            try:
                p = win.get_full_property(pid_atom, X.AnyPropertyType)
                if p and p.value and int(p.value[0]) == os.getpid():
                    return win.id
            except Exception:
                pass
            try:
                for child in win.query_tree().children:
                    r = _walk(child)
                    if r:
                        return r
            except Exception:
                pass
            return 0

        return _walk(d.screen().root)
    except Exception:
        return 0


def _log(entry: dict) -> None:
    try:
        with open(LOG_FILE, "a") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(), **entry}) + "\n")
    except Exception:
        pass


async def run() -> None:
    loop     = asyncio.get_event_loop()
    queue    = RequestQueue()
    registry = SessionRegistry()
    state    = UIState(queue=queue, registry=registry)
    renderer = Renderer(state)

    state.kitty_ok        = await loop.run_in_executor(None, kitty_available)
    state.linking           = False
    state.link_start_window = 0

    # Pre-populate sessions from running Claude processes
    await loop.run_in_executor(None, discover_running_sessions, registry)
    state.dirty = True
    state.link_wins    = []
    state.link_cursor  = 0
    state.link_session = ""

    # ── socket server ─────────────────────────────────────────────────────────

    async def on_request(request: Request, writer: asyncio.StreamWriter) -> None:
        registry.touch(request.session_id, request.cwd,
                       request.tty_path, request.terminal_pid)
        state.dirty = True

    server = await serve_unix_socket(SOCKET_PATH, queue, on_request)

    # ── resolve ───────────────────────────────────────────────────────────────

    async def resolve(decision: str, reason: str = "") -> None:
        if not queue.pending or state.q_cursor >= len(queue.pending):
            return
        item = queue.pending[state.q_cursor]
        r    = item.request
        resp: dict = {"decision": decision}
        if decision == "deny":
            resp["reason"] = reason or "Denied by user via claude-perm-daemon"
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
        _log({"type": "decision", "decision": decision, "reason": reason,
              "session": r.session_id[:8], "tool": r.tool_name,
              "command": r.summary_command()})
        state.history.append(HistoryEntry(
            session_id=r.session_id,
            tool_name=r.tool_name,
            command_summary=r.summary_command(),
            decision=decision,
        ))
        registry.touch(r.session_id, r.cwd, r.tty_path)
        queue.remove(r.id)
        if state.q_cursor >= len(queue.pending) and state.q_cursor > 0:
            state.q_cursor -= 1
        state.dirty = True

    # ── send message to session ────────────────────────────────────────────────

    async def send_message(session, text: str) -> tuple[bool, str]:
        return await loop.run_in_executor(
            None, send_message_to_session, session, text
        )

    # ── main loop ─────────────────────────────────────────────────────────────

    last_draw = 0.0

    with term.fullscreen(), term.hidden_cursor(), term.cbreak():
        sys.stdout.write(term.home + term.clear)
        sys.stdout.flush()
        renderer.draw()

        try:
            while True:
                k   = await _key(loop)
                now = time.time()

                state.tick += 1
                if state.tick % 8 == 0:
                    state.dirty = True   # drive animations + age updates

                # ── periodic session rescan (every 30s) ───────────────────────
                if state.tick % 600 == 0:   # 600 * 50ms = 30s
                    await loop.run_in_executor(None, discover_running_sessions, registry)

                # ── focus-to-link mode ────────────────────────────────────────
                if state.linking:
                    # Wait until focus moves to a DIFFERENT window than when L was pressed
                    focused = await loop.run_in_executor(None, _get_focused_window)
                    if focused and focused != state.link_start_window:
                        # User switched to a non-daemon window — link it
                        registry.pin_window(state.link_session, focused)
                        _log({"type": "link", "session": state.link_session,
                              "window": focused})
                        state.linking = False
                        state.dirty   = True
                    if k:
                        ks = str(k)
                        if k.name == "KEY_ESCAPE" or ks == "q":
                            state.linking = False
                            state.dirty   = True
                    if state.dirty or (now - last_draw) > 0.1:
                        renderer.draw()
                        renderer.draw_link_overlay(state)
                        last_draw   = now
                        state.dirty = False
                    continue

                # ── composer mode ─────────────────────────────────────────────
                if state.composing:
                    if k:
                        ks = str(k)
                        if k.name == "KEY_ESCAPE":
                            state.composing   = False
                            state.message_buf = ""
                            state.dirty = True
                        elif k.name in ("KEY_ENTER", "\n", "\r") or ks in ("\n", "\r"):
                            msg = state.message_buf.strip()
                            if msg:
                                sessions = registry.active()
                                if sessions:
                                    s = sessions[min(state.s_cursor, len(sessions)-1)]
                                    ok, method = await send_message(s, msg)
                                    _log({"type": "message", "text": msg,
                                          "session": s.short_id(), "tty": s.tty_path,
                                          "ok": ok, "method": method})
                            state.composing   = False
                            state.message_buf = ""
                            state.dirty = True
                        elif k.name == "KEY_BACKSPACE" or ks == "\x7f":
                            state.message_buf = state.message_buf[:-1]
                            state.dirty = True
                        elif ks and ks.isprintable():
                            state.message_buf += ks
                            state.dirty = True
                    if state.dirty or (now - last_draw) > 0.1:
                        renderer.draw()
                        last_draw   = now
                        state.dirty = False
                    continue

                # ── normal mode ───────────────────────────────────────────────
                if not k:
                    if state.dirty or (now - last_draw) > 0.5:
                        renderer.draw()
                        last_draw   = now
                        state.dirty = False
                    continue

                ks = str(k)

                if ks in ("q", "Q") or k.name == "KEY_ESCAPE":
                    break

                elif k.name == "KEY_TAB" or ks == "\t":
                    state.focus  = FOCUS_QUEUE if state.focus == FOCUS_SESSIONS else FOCUS_SESSIONS
                    state.dirty  = True

                elif k.name in ("KEY_UP",) or ks == "k":
                    if state.focus == FOCUS_QUEUE:
                        if state.q_cursor > 0:
                            state.q_cursor -= 1
                    else:
                        sessions = registry.active()
                        if state.s_cursor > 0:
                            state.s_cursor -= 1
                    state.dirty = True

                elif k.name in ("KEY_DOWN",) or ks == "j":
                    if state.focus == FOCUS_QUEUE:
                        if state.q_cursor < len(queue.pending) - 1:
                            state.q_cursor += 1
                    else:
                        sessions = registry.active()
                        if state.s_cursor < len(sessions) - 1:
                            state.s_cursor += 1
                    state.dirty = True

                elif ks in ("a", "A"):
                    await resolve("allow")

                elif ks in ("d", "D"):
                    await resolve("deny")

                elif ks in ("l", "L"):
                    sessions = registry.active()
                    if sessions:
                        s = sessions[min(state.s_cursor, len(sessions)-1)]
                        state.linking            = True
                        state.link_session       = s.session_id
                        state.link_start_window  = await loop.run_in_executor(None, _get_focused_window)
                        state.dirty              = True

                elif ks in ("m", "M"):
                    sessions = registry.active()
                    if sessions or queue.pending:
                        state.composing   = True
                        state.message_buf = ""
                        state.focus       = FOCUS_SESSIONS
                        state.dirty       = True

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
