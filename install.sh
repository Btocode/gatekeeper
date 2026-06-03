#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.claude/bin"
SETTINGS="$HOME/.claude/settings.json"

mkdir -p "$BIN_DIR"

# Handle uninstall
if [[ "${1:-}" == "uninstall" ]]; then
    python3 - << PYEOF
import json, os

settings_path = "$SETTINGS"
try:
    with open(settings_path) as f:
        settings = json.load(f)

    # Remove hook
    hooks = settings.get("hooks", {})
    pre   = hooks.get("PreToolUse", [])
    hooks["PreToolUse"] = [e for e in pre if not any(
        "gatekeeper-hook" in h.get("command","") or "claude-perm-hook" in h.get("command","")
        for h in e.get("hooks",[])
    )]

    # Remove blanket allow rules added by install
    perms = settings.get("permissions", {})
    added = [
        "Bash(**)", "Edit(**)", "Write(**)", "Read(**)",
        "WebSearch(**)", "WebFetch(**)", "TodoWrite", "Agent(**)",
        "NotebookRead(**)", "NotebookEdit(**)",
    ]
    perms["allow"] = [r for r in perms.get("allow", []) if r not in added]

    # Restore default permission mode
    perms.pop("defaultMode", None)
    settings.pop("skipDangerousModePermissionPrompt", None)

    with open(settings_path, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")
    print("Removed hook and permission overrides from settings.json")
except Exception as e:
    print(f"Warning: could not patch settings.json: {e}")

# Remove wrapper scripts
for name in ["gatekeeper", "gatekeeper-hook", "gatekeeper-stats"]:
    path = "$BIN_DIR/" + name
    if os.path.exists(path):
        os.unlink(path)
        print(f"Removed {path}")
PYEOF
    echo ""
    echo "Uninstall complete. Claude Code's own permission dialogs are restored."
    exit 0
fi

# Install daemon wrapper (also dispatches: gatekeeper stats [days|all])
cat > "$BIN_DIR/gatekeeper" << WRAPPER
#!/usr/bin/env bash
source "$SCRIPT_DIR/.venv/bin/activate"
if [[ "\${1:-}" == "stats" ]]; then
    shift
    exec python "$SCRIPT_DIR/src/stats.py" "\$@"
fi
if [[ "\${1:-}" == "uninstall" ]]; then
    exec bash "$SCRIPT_DIR/install.sh" uninstall
fi
exec python "$SCRIPT_DIR/src/daemon.py" "\$@"
WRAPPER
chmod +x "$BIN_DIR/gatekeeper"

# Install stats wrapper (invoked as: gatekeeper stats [days|all])
cat > "$BIN_DIR/gatekeeper-stats" << WRAPPER
#!/usr/bin/env bash
source "$SCRIPT_DIR/.venv/bin/activate"
exec python "$SCRIPT_DIR/src/stats.py" "\$@"
WRAPPER
chmod +x "$BIN_DIR/gatekeeper-stats"

# Install hook wrapper (reuses same hook protocol)
cat > "$BIN_DIR/gatekeeper-hook" << WRAPPER
#!/usr/bin/env bash
source "$SCRIPT_DIR/.venv/bin/activate"
exec python "$SCRIPT_DIR/src/hook.py" "\$@"
WRAPPER
chmod +x "$BIN_DIR/gatekeeper-hook"

# Patch settings.json
python3 - << PYEOF
import json, sys

settings_path = "$SETTINGS"
hook_cmd = "$BIN_DIR/gatekeeper-hook"

with open(settings_path) as f:
    settings = json.load(f)

hooks = settings.setdefault("hooks", {})
pre   = hooks.setdefault("PreToolUse", [])

# Remove old claude-perm-hook entry if present, add kitty one
pre[:] = [e for e in pre if not any(
    "gatekeeper-hook" in h.get("command","") or "claude-perm-hook" in h.get("command","")
    for h in e.get("hooks",[])
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
echo "  Open a terminal and run:  gatekeeper"
echo "  Stats:                    gatekeeper stats [days|all]"
echo "  ↑↓ / j k  navigate   A allow   D deny   Q quit"
