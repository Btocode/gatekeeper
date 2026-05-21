# claude-perm-kitty

A terminal dashboard that aggregates **all Claude Code permission prompts** from every active session into one place — so you never have to switch terminals to approve a tool call.

![Python](https://img.shields.io/badge/python-3.11%2B-blue)
![Platform](https://img.shields.io/badge/platform-Linux%20%28X11%29-lightgrey)

```
+----------------------------------------------------+----------------------------------+
| SESSIONS(2)          | QUEUE(1)        | DETAIL                            |
|                      |                 |                                   |
| > a8ed1d57 [linked]  | > Bash  12s     |  Session  a8ed1d57               |
|   ~/myproject        |   npm install   |  Tool     Bash                    |
|   pts/2  3 calls     |                 |  CWD      ~/myproject             |
|                      |                 |  Waiting  12s [####----]  33%    |
|   b73f7ccc [auto]    |                 |                                   |
|   ~/other  7 calls   |                 |  Command                          |
|                      |                 |   npm install --save-dev jest     |
| HISTORY(14)          |                 |                                   |
|  A Bash git status   |                 |   A:ALLOW     D:DENY              |
|  A Edit src/auth.py  |                 |                                   |
|                      |                 |   M  send message to this session |
+----------------------------------------------------+----------------------------------+
  Tab pane  jk nav  A allow/auto  D deny  M message  L link  Q quit
```

## Features

- **Single approval terminal** — all Claude sessions route here
- **Session tracking** — shows all active Claude sessions immediately on start
- **Auto-approve** — mark safe sessions to silently allow routine tool calls
- **Danger guard** — `rm`, SQL mutations, `ssh`, `sudo`, `--force` always require manual approval even on auto sessions
- **Message injection** — send messages to a specific Claude session directly from the dashboard
- **Per-session window linking** — `L` to link a session to its terminal tab
- **Daily logs** — `perm-stats` shows allow/deny history and auto-approve rates
- **Terminal fallback** — if the daemon isn't running, prompts appear inline in the Claude terminal

## Requirements

- **Linux** with X11 (DISPLAY set)
- **Python 3.11+**
- **Claude Code CLI**
- **gnome-terminal** (or compatible terminal emulator — Kitty, Alacritty, etc.)

## Install

```bash
git clone https://github.com/YOUR_USERNAME/claude-perm-kitty
cd claude-perm-kitty
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
bash install.sh
```

`install.sh` does three things:
1. Creates wrapper scripts in `~/.claude/bin/`
2. Registers the `PreToolUse` hook in `~/.claude/settings.json`
3. Adds blanket allow rules so Claude Code's own dialogs are suppressed (our hook becomes the sole gatekeeper)

## Usage

Open a dedicated terminal and run:

```bash
claude-perm-kitty
```

Start your Claude Code sessions normally anywhere. Every `Bash`, `Edit`, `Agent` call routes to the dashboard.

### Keyboard shortcuts

| Key | Context | Action |
|-----|---------|--------|
| `Tab` | anywhere | Switch focus between Sessions / Queue pane |
| `↑↓` / `j k` | any pane | Navigate |
| `A` | Queue pane | Allow selected request |
| `A` | Sessions pane | Toggle auto-approve for selected session |
| `D` | Queue pane | Deny selected request |
| `M` | anywhere | Send a message to selected session |
| `L` | Sessions pane | Link session to a terminal window |
| `Q` | anywhere | Quit |

### Session linking

Linking tells the daemon which terminal window belongs to which session, so `M` sends messages to exactly the right place:

1. `Tab` to the Sessions pane, navigate to a session
2. Press `L` — an overlay appears
3. Switch to the Claude terminal tab you want to link
4. The daemon detects the focus change and links it automatically

### Auto-approve

Mark a session as auto-approve (`A` in the Sessions pane) to silently allow all routine tool calls without prompting. The following are **always blocked** from auto-approve regardless:

- `rm`, `rmdir`, `shred`, `truncate`
- `DROP TABLE`, `DELETE FROM`, `TRUNCATE`, `UPDATE ... SET`
- `ssh`, `scp`, `rsync`, `sudo`, `kubectl delete`, `terraform destroy`
- `git push --force`, `git reset --hard`
- Writes to `/etc/`, `/usr/`, `~/.ssh/`, `~/.aws/`

### Stats

```bash
perm-stats        # today
perm-stats 7      # last 7 days
perm-stats all    # all time
```

Logs live in `~/.claude/perm-logs/YYYY-MM-DD.log`.

## How it works

```
Claude session A    Claude session B    Claude session C
      │                   │                   │
  PreToolUse hook     PreToolUse hook     PreToolUse hook
  (blocks Claude)     (blocks Claude)     (blocks Claude)
      └───────────────────┼───────────────────┘
                          │  Unix socket
                          ▼
               claude-perm-kitty daemon
               (your dedicated terminal)
```

A `PreToolUse` hook fires before every tool call. It connects to the daemon's Unix socket at `/tmp/claude-perm-$USER.sock`, sends the request, and waits. The daemon shows it in the UI. When you press `A` or `D`, the daemon sends the decision back and the hook exits — Claude proceeds or stops.

If the daemon isn't running the hook falls back to a `Y/n` prompt in the Claude terminal.

## Uninstall

Remove the `PreToolUse` hook entry from `~/.claude/settings.json` and the `permissions.allow` block added by the installer.

## License

MIT
