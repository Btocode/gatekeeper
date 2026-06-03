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

LOG_DIR  = os.path.expanduser("~/.claude/perm-logs")
os.makedirs(LOG_DIR, exist_ok=True)


def _log_file() -> str:
    return os.path.join(LOG_DIR, f"{datetime.now().strftime('%Y-%m-%d')}.log")

import blessed

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from src.protocol import HistoryEntry, Request, SOCKET_PATH
from src.server import RequestQueue, serve_unix_socket
from src.sessions import (SessionRegistry, kitty_available, send_message_to_session,
                          list_injectable_windows, discover_running_sessions,
                          is_dangerous, poll_waiting_sessions)
from src.config import GatekeeperConfig, load_config, save_config, check_global_rules
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


_CLAUDE_SETTINGS = os.path.expanduser("~/.claude/settings.json")


def _add_to_claude_allowlist(pattern: str) -> None:
    """Append pattern to Claude Code's permissions.allow in ~/.claude/settings.json."""
    try:
        with open(_CLAUDE_SETTINGS) as f:
            data = json.load(f)
        allow: list = data.setdefault("permissions", {}).setdefault("allow", [])
        if pattern not in allow:
            allow.append(pattern)
            with open(_CLAUDE_SETTINGS, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
    except Exception:
        pass


def _log(entry: dict) -> None:
    try:
        with open(_log_file(), "a") as f:
            f.write(json.dumps({"ts": datetime.now().isoformat(), **entry}) + "\n")
    except Exception:
        pass


async def run() -> None:
    loop     = asyncio.get_event_loop()
    queue    = RequestQueue()
    registry = SessionRegistry()
    state    = UIState(queue=queue, registry=registry)
    renderer = Renderer(state)

    state.kitty_ok          = await loop.run_in_executor(None, kitty_available)
    state.linking           = False
    state.link_start_window = 0
    state.config            = load_config()
    state.settings_open     = False
    # Record which window is focused when the daemon starts — this is the
    # daemon's own terminal. Never link to it.
    state.daemon_window_id  = await loop.run_in_executor(None, _get_focused_window)

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

        # 1. Check global config rules (user-defined allow/deny)
        global_verdict, global_reason = check_global_rules(
            request.tool_name, request.tool_input, state.config
        )
        if global_verdict == "deny":
            if writer and not writer.is_closing():
                try:
                    writer.write((json.dumps({"decision": "deny",
                                              "reason": global_reason}) + "\n").encode())
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            _log({"type": "global_deny", "session": request.session_id[:8],
                  "tool": request.tool_name, "reason": global_reason})
            return
        if global_verdict == "allow":
            queue.remove(request.id)
            if writer and not writer.is_closing():
                try:
                    writer.write((json.dumps({"decision": "allow"}) + "\n").encode())
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            _log({"type": "global_allow", "session": request.session_id[:8],
                  "tool": request.tool_name, "reason": global_reason})
            return

        # 2. Auto-approve if session is flagged — but never for dangerous commands
        danger, _ = is_dangerous(request.tool_name, request.tool_input)
        if registry.is_auto_approve(request.session_id) and not danger:
            # Remove from queue immediately (server.py adds before this callback fires)
            queue.remove(request.id)
            resp = json.dumps({"decision": "allow"}) + "\n"
            if writer and not writer.is_closing():
                try:
                    writer.write(resp.encode())
                    await writer.drain()
                    writer.close()
                    await writer.wait_closed()
                except Exception:
                    pass
            _log({"type": "auto_allow", "session": request.session_id[:8],
                  "tool": request.tool_name, "command": request.summary_command()})
            return
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

    # ── session-only allow (option 3) ────────────────────────────────────────

    async def resolve_session_only() -> None:
        """Allow + add rule to in-memory config only — not saved, forgotten on restart."""
        if not queue.pending or state.q_cursor >= len(queue.pending):
            return
        request      = queue.pending[state.q_cursor].request
        action_type, value = request.persistent_allow_action()
        cfg          = state.config

        if action_type == "bash_pattern" and value:
            if value not in cfg.custom_allow_patterns:
                cfg.custom_allow_patterns.append(value)
            _log({"type": "session_allow", "kind": "bash_pattern", "value": value,
                  "session": request.session_id[:8], "tool": request.tool_name})

        elif action_type == "edit_dir" and value:
            if value not in cfg.allowed_edit_dirs:
                cfg.allowed_edit_dirs.append(value)
            _log({"type": "session_allow", "kind": "edit_dir", "value": value,
                  "session": request.session_id[:8], "tool": request.tool_name})

        elif action_type == "auto_approve":
            registry.auto_approve.add(request.session_id)
            _log({"type": "session_allow", "kind": "auto_approve",
                  "session": request.session_id[:8]})

        await resolve("allow")

    # ── persistent allow (option 2) ───────────────────────────────────────────

    async def resolve_persistent() -> None:
        """Allow the current request AND save a rule so it auto-approves next time."""
        if not queue.pending or state.q_cursor >= len(queue.pending):
            return
        request      = queue.pending[state.q_cursor].request
        action_type, value = request.persistent_allow_action()
        cfg          = state.config
        cc_pattern: str | None = None

        if action_type == "bash_pattern" and value:
            if value not in cfg.custom_allow_patterns:
                cfg.custom_allow_patterns.append(value)
            save_config(cfg)
            # "Bash(**)" in user settings already covers all bash commands — no
            # extra Claude Code allowlist entry needed for bash patterns.
            _log({"type": "persistent_allow", "kind": "bash_pattern", "value": value,
                  "session": request.session_id[:8], "tool": request.tool_name})

        elif action_type == "edit_dir" and value:
            if value not in cfg.allowed_edit_dirs:
                cfg.allowed_edit_dirs.append(value)
            save_config(cfg)
            tool = request.tool_name
            if tool == "Write":
                cc_pattern = f"Write({value}/**)"
            elif tool == "NotebookEdit":
                cc_pattern = f"NotebookEdit({value}/**)"
            else:
                cc_pattern = f"Edit({value}/**)"
            _log({"type": "persistent_allow", "kind": "edit_dir", "value": value,
                  "session": request.session_id[:8], "tool": request.tool_name})

        elif action_type == "auto_approve":
            registry.toggle_auto_approve(request.session_id)
            _log({"type": "persistent_allow", "kind": "auto_approve",
                  "session": request.session_id[:8]})

        if cc_pattern:
            _add_to_claude_allowlist(cc_pattern)

        await resolve("allow")

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
                if state.tick % 8 == 0 and not state.settings_open and not state.linking:
                    state.dirty = True   # drive animations + age updates

                # ── periodic rescan + input-waiting poll (every 4s) ───────────
                if state.tick % 80 == 0:    # 80 * 50ms = 4s
                    await loop.run_in_executor(None, poll_waiting_sessions, registry)
                    state.dirty = True
                if state.tick % 600 == 0:   # 600 * 50ms = 30s
                    await loop.run_in_executor(None, discover_running_sessions, registry)

                # ── settings mode ─────────────────────────────────────────────
                if state.settings_open:
                    cfg = state.config
                    from src.config import BASH_CATEGORIES, TOOL_TYPES
                    tool_keys = list(TOOL_TYPES.keys())
                    cat_keys  = list(BASH_CATEGORIES.keys())

                    if k:
                        ks2 = str(k)
                        if k.name == "KEY_ESCAPE" or ks2 in ("s", "S"):
                            save_config(cfg)
                            state.settings_open = False
                            state.dirty = True

                        elif k.name == "KEY_TAB" or ks2 == "\t":
                            state.settings_tab    = (state.settings_tab + 1) % 3
                            state.settings_cursor = 0
                            state.dirty = True

                        elif k.name in ("KEY_UP",) or ks2 == "k":
                            if state.settings_cursor > 0:
                                state.settings_cursor -= 1
                                state.dirty = True

                        elif k.name in ("KEY_DOWN",) or ks2 == "j":
                            max_idx = (len(tool_keys) if state.settings_tab == 0
                                       else len(cat_keys) if state.settings_tab == 1
                                       else 0) - 1
                            if state.settings_cursor < max_idx:
                                state.settings_cursor += 1
                                state.dirty = True

                        elif k.name in ("KEY_ENTER", "\n") or ks2 in ("\n", "\r", " "):
                            if state.settings_tab == 0:
                                key = tool_keys[state.settings_cursor]
                                if key in cfg.allowed_tools:
                                    cfg.allowed_tools.discard(key)
                                else:
                                    cfg.allowed_tools.add(key)
                                state.dirty = True
                            elif state.settings_tab == 1:
                                key = cat_keys[state.settings_cursor]
                                if key in cfg.allowed_bash_categories:
                                    cfg.allowed_bash_categories.discard(key)
                                else:
                                    cfg.allowed_bash_categories.add(key)
                                state.dirty = True
                            elif state.settings_tab == 2:
                                # confirm input
                                pass

                        elif state.settings_tab == 2:
                            if k.name == "KEY_BACKSPACE" or ks2 == "\x7f":
                                state.settings_input = state.settings_input[:-1]
                                state.dirty = True
                            elif ks2 == "a" and not state.settings_input:
                                state.settings_input = "ALLOW:"
                                state.dirty = True
                            elif ks2 == "b" and not state.settings_input:
                                state.settings_input = "DENY:"
                                state.dirty = True
                            elif ks2 == "d" and not state.settings_input:
                                state.settings_input = "DIR:"
                                state.dirty = True
                            elif ks2 and ks2.isprintable():
                                state.settings_input += ks2
                                state.dirty = True
                            elif k.name in ("KEY_ENTER",) or ks2 in ("\n", "\r"):
                                val = state.settings_input.strip()
                                if val.startswith("ALLOW:"):
                                    cfg.custom_allow_patterns.append(val[6:].strip())
                                elif val.startswith("DENY:"):
                                    cfg.custom_deny_patterns.append(val[5:].strip())
                                elif val.startswith("DIR:"):
                                    cfg.allowed_edit_dirs.append(val[4:].strip())
                                state.settings_input = ""
                                state.dirty = True

                    if state.dirty or (now - last_draw) > 0.5:
                        renderer.draw()
                        renderer.draw_settings(state)
                        last_draw   = now
                        state.dirty = False
                    continue

                # ── focus-to-link mode ────────────────────────────────────────
                if state.linking:
                    focused = await loop.run_in_executor(None, _get_focused_window)
                    # Accept focus change only if it's a real different window,
                    # not the daemon's own terminal, and not zero.
                    is_different  = focused and focused != state.link_start_window
                    is_own_window = focused == state.daemon_window_id
                    if is_different and not is_own_window:
                        registry.pin_window(state.link_session, focused)
                        _log({"type": "link", "session": state.link_session,
                              "window": focused})
                        state.linking = False   # ← close modal
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
                                s = registry.get_by_id(state.selected_session_id)
                                if s:
                                    ok, method = await send_message(s, msg)
                                    _log({"type": "message", "text": msg,
                                          "session": s.short_id(), "tty": s.tty_path,
                                          "ok": ok, "method": method})
                                else:
                                    _log({"type": "message", "text": msg,
                                          "session": state.selected_session_id, "ok": False,
                                          "method": "session not found"})
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
                    state.focus        = FOCUS_QUEUE if state.focus == FOCUS_SESSIONS else FOCUS_SESSIONS
                    state.action_cursor = 0
                    state.dirty        = True

                elif k.name == "KEY_UP":
                    if state.focus == FOCUS_QUEUE and queue.pending:
                        # Move action cursor upward in the detail panel
                        if state.action_cursor > 0:
                            state.action_cursor -= 1
                    else:
                        sessions = registry.active()
                        if state.s_cursor > 0:
                            state.s_cursor -= 1
                        if sessions:
                            state.selected_session_id = sessions[state.s_cursor].session_id
                    state.dirty = True

                elif k.name == "KEY_DOWN":
                    if state.focus == FOCUS_QUEUE and queue.pending:
                        # Move action cursor downward in the detail panel
                        if state.action_cursor < 2:
                            state.action_cursor += 1
                    else:
                        sessions = registry.active()
                        if state.s_cursor < len(sessions) - 1:
                            state.s_cursor += 1
                        if sessions:
                            state.selected_session_id = sessions[state.s_cursor].session_id
                    state.dirty = True

                elif ks == "k":
                    if state.focus == FOCUS_QUEUE:
                        if state.q_cursor > 0:
                            state.q_cursor -= 1
                            state.action_cursor = 0
                    else:
                        sessions = registry.active()
                        if state.s_cursor > 0:
                            state.s_cursor -= 1
                        if sessions:
                            state.selected_session_id = sessions[state.s_cursor].session_id
                    state.dirty = True

                elif ks == "j":
                    if state.focus == FOCUS_QUEUE:
                        if state.q_cursor < len(queue.pending) - 1:
                            state.q_cursor += 1
                            state.action_cursor = 0
                    else:
                        sessions = registry.active()
                        if state.s_cursor < len(sessions) - 1:
                            state.s_cursor += 1
                        if sessions:
                            state.selected_session_id = sessions[state.s_cursor].session_id
                    state.dirty = True

                elif k.name in ("KEY_ENTER",) or ks in ("\n", "\r"):
                    if state.focus == FOCUS_QUEUE and queue.pending:
                        if state.action_cursor == 0:
                            await resolve("allow")
                        elif state.action_cursor == 1:
                            await resolve_session_only()
                        else:
                            await resolve("deny")

                elif ks == "1":
                    if state.focus == FOCUS_QUEUE:
                        await resolve("allow")

                elif ks == "2":
                    if state.focus == FOCUS_QUEUE:
                        await resolve_session_only()

                elif ks == "3":
                    if state.focus == FOCUS_QUEUE:
                        await resolve("deny")

                elif ks in ("a", "A"):
                    if state.focus == FOCUS_SESSIONS and state.selected_session_id:
                        # Toggle auto-approve for selected session
                        enabled = registry.toggle_auto_approve(state.selected_session_id)
                        _log({"type": "auto_approve_toggle",
                              "session": state.selected_session_id[:8],
                              "enabled": enabled})
                        state.dirty = True
                    else:
                        await resolve("allow")

                elif ks in ("d", "D"):
                    await resolve("deny")

                elif ks in ("u", "U"):
                    if state.selected_session_id:
                        registry.unpin_window(state.selected_session_id)
                        state.dirty = True

                elif ks in ("l", "L"):
                    if state.selected_session_id:
                        state.linking            = True
                        state.link_session       = state.selected_session_id
                        state.link_start_window  = await loop.run_in_executor(None, _get_focused_window)
                        state.dirty              = True

                elif ks in ("m", "M"):
                    sessions = registry.active()
                    if sessions or queue.pending:
                        state.composing   = True
                        state.message_buf = ""
                        state.focus       = FOCUS_SESSIONS
                        state.dirty       = True

                elif ks in ("s", "S"):
                    state.settings_open     = True
                    state.settings_tab      = 0
                    state.settings_cursor   = 0
                    state.settings_input    = ""
                    state.dirty             = True

                if state.dirty or (now - last_draw) > 0.5:
                    renderer.draw()
                    if state.settings_open:
                        renderer.draw_settings(state)
                    last_draw   = now
                    state.dirty = False

        finally:
            # Close all pending socket connections
            for item in list(queue.pending):
                if item.writer and not item.writer.is_closing():
                    try:
                        item.writer.close()
                    except Exception:
                        pass

            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=2.0)
            except Exception:
                pass

            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)

            sys.stdout.write(term.normal + term.home + term.clear)
            sys.stdout.flush()


def _suppress_closed_loop(loop, context):
    """Silence the harmless 'Event loop is closed' error from StreamWriter GC."""
    exc = context.get("exception")
    if isinstance(exc, RuntimeError) and "Event loop is closed" in str(exc):
        return
    loop.default_exception_handler(context)


def main() -> None:
    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_suppress_closed_loop)
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
