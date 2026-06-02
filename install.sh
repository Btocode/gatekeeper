#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.claude/bin"
SETTINGS="$HOME/.claude/settings.json"

mkdir -p "$BIN_DIR"

# Install daemon wrapper
cat > "$BIN_DIR/claude-perm-kitty" << WRAPPER
#!/usr/bin/env bash
source "$SCRIPT_DIR/.venv/bin/activate"
exec python "$SCRIPT_DIR/src/daemon.py" "\$@"
WRAPPER
chmod +x "$BIN_DIR/claude-perm-kitty"

# Install stats wrapper
cat > "$BIN_DIR/perm-stats" << WRAPPER
#!/usr/bin/env bash
source "$SCRIPT_DIR/.venv/bin/activate"
exec python "$SCRIPT_DIR/src/stats.py" "\$@"
WRAPPER
chmod +x "$BIN_DIR/perm-stats"

# Install hook wrapper (reuses same hook protocol)
cat > "$BIN_DIR/claude-perm-hook-kitty" << WRAPPER
#!/usr/bin/env bash
source "$SCRIPT_DIR/.venv/bin/activate"
exec python "$SCRIPT_DIR/src/hook.py" "\$@"
WRAPPER
chmod +x "$BIN_DIR/claude-perm-hook-kitty"

# Patch settings.json
python3 - << PYEOF
import json, sys

settings_path = "$SETTINGS"
hook_cmd = "$BIN_DIR/claude-perm-hook-kitty"

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})
pre   = hooks.setdefault("PreToolUse", [])

# Remove old claude-perm-hook entry if present, add kitty one
pre[:] = [e for e in pre if not any(
    "claude-perm-hook" in h.get("command","") for h in e.get("hooks",[])
)]

pre.append({"matcher": "", "hooks": [{"type": "command", "command": hook_cmd}]})

# Blanket allow rules — suppress Claude Code's own dialogs so Gatekeeper
# is the sole approval mechanism. Use ** to match path separators (/).
perms = settings.setdefault("permissions", {})
allow = perms.setdefault("allow", [])
needed = [
    "Bash(**)", "Edit(**)", "Write(**)", "Read(**)",
    "WebSearch(**)", "WebFetch(**)", "TodoWrite", "Agent(**)",
    "NotebookRead(**)", "NotebookEdit(**)",
]
for rule in needed:
    if rule not in allow:
        allow.append(rule)

# bypassPermissions disables Claude Code's own permission dialogs entirely so
# the Gatekeeper PreToolUse hook is the sole approval gate.  The hook still
# fires for every tool call; Claude Code's hardcoded sensitive-path prompts
# (/proc/, /sys/, ~/.bashrc, etc.) are suppressed only in this mode.
perms["defaultMode"] = "bypassPermissions"
settings["skipDangerousModePermissionPrompt"] = True

with open(settings_path, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"Registered hook → {hook_cmd}")
print(f"Blanket allow rules added ({len(needed)} rules)")
PYEOF

echo ""
echo "Install complete."
echo ""
echo "Usage:"
echo "  Open Kitty and run:  claude-perm-kitty"
echo "  ↑↓ / j k  navigate   A allow   D deny   Q quit"
