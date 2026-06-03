# How I Manage All My Claude Code Sessions from a Single Terminal

I run multiple Claude Code sessions all day — one per feature, one per service, sometimes five at once.

Every session was asking me for permission in its own terminal. I'd miss requests buried in a background tab. I'd switch windows mid-thought just to approve a `git status`. I'd lose context constantly.

And there was no single place to see what Claude was doing across all of them.

So I built **Gatekeeper** — a TUI daemon that intercepts every Claude Code tool call and routes it to one unified approval dashboard.

![Gatekeeper demo](https://raw.githubusercontent.com/Btocode/gatekeeper/main/assets/gatekeeper-demo.gif)

---

## The dashboard

Three panes, one terminal:

- **Left** — all active Claude sessions, with status badges: `[auto]` means auto-approve is on, `[linked]` means it's wired to a terminal window
- **Middle** — pending permission requests with an age timer so you know what's been waiting longest
- **Right** — full request detail, danger warnings, and the numbered approval menu

Every Claude Code tool call — `Bash`, `Edit`, `Write`, `Agent` — passes through a `PreToolUse` hook before executing. The hook connects to Gatekeeper's Unix socket, sends the request, and blocks. Gatekeeper shows it in the UI. When you decide, the answer travels back and Claude proceeds or stops.

---

## Approving requests

The menu in the right pane mirrors Claude Code's own style:

```
1  Allow once
2  Always allow
3  Deny
```

`↑`/`↓` moves the cursor, `Enter` confirms. Or just press `1`, `2`, `3` directly. `A` and `D` are quick shortcuts for allow/deny.

**Option 2 — always allow** — is where it gets useful. Choosing it saves a persistent rule so the same request never surfaces again:

- `Bash` → saves the command pattern (e.g. `npm run *`) to config
- `Edit` / `Write` → saves the directory to an allowlist
- `Agent` → enables auto-approve for that session

The rule is written both to Gatekeeper's own config *and* to Claude Code's `settings.json` allowlist — so Claude Code itself won't prompt for it either.

---

## Auto-approve sessions

Press `A` in the Sessions pane to mark a session as trusted. It shows `[auto]` — routine tool calls pass silently without appearing in the queue.

But some things **always** require manual approval, no matter what:

| Category | What's blocked |
|---|---|
| File deletion | `rm`, `rmdir`, `shred` |
| Remote access | `ssh`, `scp`, `rsync` |
| Privilege escalation | `sudo`, `su` |
| Destructive git | `push --force`, `reset --hard`, `clean -f` |
| Infrastructure | `terraform apply/destroy`, `kubectl delete` |
| Sensitive paths | Writes to `/etc/`, `~/.ssh/`, `~/.aws/` |

Read-only commands — `grep`, `find`, `ls`, `cat`, `git status`, `npm install` — always pass through freely.

---

## Linking sessions to terminals

This is the feature that unlocks everything else.

Press `L` on any session in the Sessions pane. An overlay appears — switch to the Claude terminal tab (alt+tab, click, whatever), and Gatekeeper detects the focus change and links that session to that window automatically. The session shows `[linked]`.

Links persist across restarts in `~/.claude/perm-window-map.json`. You link once, it stays.

---

## Sending messages from Gatekeeper

Once a session is linked, press `M`, type your message, press `Enter`.

Gatekeeper injects the text into the linked Claude terminal using X11 XTEST — it appears **and submits automatically**, exactly as if you typed it and pressed Enter there. You never leave the Gatekeeper terminal.

This solves a problem I didn't know I had until I built it: Claude pauses mid-task and asks a clarifying question — `A / B / C?`. Normally you'd switch to that terminal, answer, switch back. With Gatekeeper, you just press `M` and type from wherever you are.

Useful for:
- Answering Claude's mid-task questions without switching windows
- Explaining why you denied a request
- Redirecting Claude to a different approach while it waits

One caveat: injection works when each Claude session is in its own terminal **window**. If multiple sessions share one window as tabs, they share the same X11 window ID — Gatekeeper can't target a specific tab. Run each session in a new window (`kitty`, `gnome-terminal --window`, etc.).

---

## Settings

Press `S` to open the settings panel. From here you can configure:

- **Tool types** — which tools (Bash, Edit, Write, Agent) Gatekeeper intercepts
- **Bash categories** — how commands are classified (read-only vs. destructive vs. network, etc.)
- **Custom patterns** — your own allow/deny rules beyond the defaults

No config file spelunking. Everything is editable from inside the dashboard.

---

## Stats

```bash
gatekeeper stats        # today
gatekeeper stats 7      # last 7 days
gatekeeper stats all    # all time
```

```
====================================================
 GATEKEEPER STATS
====================================================
  Total decisions : 177
  Auto-approved   :  16  (  9%)
  Manual reviewed : 161  ( 90%)
    allowed       : 161
    denied        :   0

  Auto-approved by session:
    b73f7ccc    7 calls
    a8ed1d57    5 calls

  Auto-approved by tool:
    Bash          11
    Edit           5
====================================================
```

Every decision is logged to `~/.claude/perm-logs/YYYY-MM-DD.log`, one file per day, kept indefinitely. Useful for auditing what Claude did across a long session or a whole project.

---

## What happens when Gatekeeper isn't running

The hook falls back to a `Y/n` prompt in the Claude terminal with a 30-second auto-deny. Nothing hangs, nothing silently passes. You can also set `GATEKEEPER_TIMEOUT=0` to always use the terminal prompt for a specific session.

---

## How it's wired up

`install.sh` does four things:

1. Installs wrapper scripts in `~/.claude/bin/`
2. Registers the `PreToolUse` hook in `~/.claude/settings.json`
3. Adds blanket `permissions.allow` rules so Claude Code doesn't double-prompt
4. Sets `permissions.defaultMode = "bypassPermissions"` — disables Claude Code's built-in dialogs entirely, making Gatekeeper the **sole** approval gate

That last point matters: Claude Code's own hardcoded prompts for sensitive paths (`/proc/`, `/sys/`, `~/.bashrc`) are suppressed in `bypassPermissions` mode. Gatekeeper handles everything instead.

---

## Installation

```bash
git clone https://github.com/Btocode/gatekeeper
cd gatekeeper
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
bash install.sh
```

Then open a dedicated terminal and run:

```bash
gatekeeper
```

Start your Claude Code sessions anywhere — other terminals, VS Code, JetBrains. Every tool call will appear in Gatekeeper.

**Requirements:** Linux + X11 + Python 3.11+

---

## Why I built this

I was working on a project with five Claude sessions running in parallel — one per subsystem. Each one was capable. But I was the bottleneck: constantly switching windows to approve `npm run build` for the fifth time that hour.

Gatekeeper changed that. Trusted sessions handle routine calls without interrupting me. Anything new or risky surfaces in the dashboard. I answer Claude's questions without leaving my main terminal. And at the end of the day, `gatekeeper stats` tells me exactly what happened.

It's open source. MIT licensed.

👉 [github.com/Btocode/gatekeeper](https://github.com/Btocode/gatekeeper)

If you run Claude Code with multiple sessions, give it a try. And if you build tools like this — follow me, more coming.

---

**Tags (dev.to):** `claudecode` `ai` `devtools` `opensource`

**Tags (Hashnode):** `claude-code` `ai` `developer-tools` `open-source` `productivity`
