"""
Gatekeeper configuration — user-defined allow/deny rules.
Stored in ~/.claude/gatekeeper-config.json.

Rules are evaluated in this order:
  1. Global block patterns (always deny, cannot be overridden)
  2. Global allow rules (tool types, bash categories, custom patterns)
  3. Danger guard (built-in safety — rm, ssh, sudo, etc.)
  4. Session auto-approve
  5. Manual approval queue
"""
import fnmatch
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

CONFIG_FILE = os.path.expanduser("~/.claude/gatekeeper-config.json")

# ── predefined bash categories ────────────────────────────────────────────────

BASH_CATEGORIES: dict[str, dict] = {
    "git_read": {
        "label": "Git — read only",
        "description": "git status, log, diff, show, branch -a",
        "patterns": ["git status*", "git log*", "git diff*", "git show*",
                     "git branch*", "git stash list*", "git remote -v*",
                     "git rev-parse*", "git describe*", "git tag*"],
    },
    "git_write": {
        "label": "Git — commits & push",
        "description": "git add, commit, push (no --force), merge, rebase",
        "patterns": ["git add*", "git commit*", "git push*", "git merge*",
                     "git rebase*", "git fetch*", "git pull*", "git stash*",
                     "git checkout*", "git switch*", "git restore*"],
    },
    "npm": {
        "label": "npm / yarn / pnpm",
        "description": "install, run scripts, build, test, lint",
        "patterns": ["npm *", "npx *", "yarn *", "pnpm *", "bun *"],
    },
    "python": {
        "label": "Python tooling",
        "description": "pip install, pytest, black, mypy, ruff, poetry",
        "patterns": ["pip install*", "pip3 install*", "pytest*", "python -m pytest*",
                     "black *", "ruff *", "mypy *", "poetry *", "uv *"],
    },
    "build": {
        "label": "Build tools",
        "description": "make, cargo, go build, gradle, mvn",
        "patterns": ["make*", "cargo *", "go build*", "go test*", "go run*",
                     "gradle*", "mvn*", "cmake*", "ninja*"],
    },
    "docker_read": {
        "label": "Docker — read only",
        "description": "docker ps, logs, images, inspect",
        "patterns": ["docker ps*", "docker logs*", "docker images*",
                     "docker inspect*", "docker stats*", "docker-compose ps*",
                     "docker-compose logs*"],
    },
    "cloud_read": {
        "label": "Cloud CLI — read only",
        "description": "aws/gcloud/az describe, list, get",
        "patterns": ["aws *describe*", "aws *list*", "aws *get*",
                     "gcloud *describe*", "gcloud *list*",
                     "az *show*", "az *list*"],
    },
    "file_read": {
        "label": "File reading",
        "description": "cat, head, tail, less, wc, stat",
        "patterns": ["cat *", "head *", "tail *", "wc *", "stat *",
                     "file *", "readlink *"],
    },
}

# ── tool type metadata ────────────────────────────────────────────────────────

TOOL_TYPES: dict[str, dict] = {
    "Read":         {"label": "Read",          "description": "Reading files (always safe)", "default": True},
    "Write":        {"label": "Write",         "description": "Creating new files",          "default": False},
    "Edit":         {"label": "Edit",          "description": "Modifying existing files",    "default": False},
    "WebSearch":    {"label": "WebSearch",     "description": "Web searches",                "default": True},
    "WebFetch":     {"label": "WebFetch",      "description": "Fetching URLs",               "default": True},
    "TodoRead":     {"label": "TodoRead",      "description": "Reading task list",           "default": True},
    "TodoWrite":    {"label": "TodoWrite",     "description": "Writing task list",           "default": True},
    "NotebookRead": {"label": "NotebookRead",  "description": "Reading notebooks",           "default": True},
    "Agent":        {"label": "Agent",         "description": "Spawning sub-agents",         "default": False},
}


# ── config dataclass ──────────────────────────────────────────────────────────

@dataclass
class GatekeeperConfig:
    # Tool types globally allowed without asking
    allowed_tools: set[str] = field(default_factory=lambda: {
        t for t, meta in TOOL_TYPES.items() if meta["default"]
    })

    # Predefined bash category keys that are globally allowed
    allowed_bash_categories: set[str] = field(default_factory=set)

    # Custom glob patterns — commands matching these are always allowed
    custom_allow_patterns: list[str] = field(default_factory=list)

    # Custom glob patterns — commands matching these are always denied
    custom_deny_patterns: list[str] = field(default_factory=list)

    # Directories where Edit/Write is always allowed (path prefix match)
    allowed_edit_dirs: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "allowed_tools":            sorted(self.allowed_tools),
            "allowed_bash_categories":  sorted(self.allowed_bash_categories),
            "custom_allow_patterns":    self.custom_allow_patterns,
            "custom_deny_patterns":     self.custom_deny_patterns,
            "allowed_edit_dirs":        self.allowed_edit_dirs,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "GatekeeperConfig":
        return cls(
            allowed_tools           = set(d.get("allowed_tools", [])),
            allowed_bash_categories = set(d.get("allowed_bash_categories", [])),
            custom_allow_patterns   = d.get("custom_allow_patterns", []),
            custom_deny_patterns    = d.get("custom_deny_patterns", []),
            allowed_edit_dirs       = d.get("allowed_edit_dirs", []),
        )


def load_config() -> GatekeeperConfig:
    try:
        with open(CONFIG_FILE) as f:
            return GatekeeperConfig.from_dict(json.load(f))
    except FileNotFoundError:
        cfg = GatekeeperConfig()
        save_config(cfg)
        return cfg
    except Exception:
        return GatekeeperConfig()


def save_config(cfg: GatekeeperConfig) -> None:
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(cfg.to_dict(), f, indent=2)
            f.write("\n")
    except Exception:
        pass


# ── rule evaluation ───────────────────────────────────────────────────────────

def _matches_any(text: str, patterns: list[str]) -> bool:
    tl = text.lower()
    for p in patterns:
        if fnmatch.fnmatch(tl, p.lower()) or tl.startswith(p.lower().rstrip("*")):
            return True
    return False


def _bash_category_patterns(cfg: GatekeeperConfig) -> list[str]:
    patterns: list[str] = []
    for cat_key in cfg.allowed_bash_categories:
        cat = BASH_CATEGORIES.get(cat_key, {})
        patterns.extend(cat.get("patterns", []))
    return patterns


def check_global_rules(
    tool_name: str,
    tool_input: dict[str, Any],
    cfg: GatekeeperConfig,
) -> tuple[str, str]:
    """
    Check global config rules.
    Returns ("allow", reason), ("deny", reason), or ("ask", "") to fall through.
    """
    command = tool_input.get("command", "")
    path    = tool_input.get("file_path", "")

    # 1. Custom deny patterns always win
    if command and _matches_any(command, cfg.custom_deny_patterns):
        return "deny", "matched custom deny pattern"
    if path and _matches_any(path, cfg.custom_deny_patterns):
        return "deny", "matched custom deny pattern (path)"

    # 2. Tool-type allow
    if tool_name in cfg.allowed_tools:
        return "allow", f"tool type {tool_name!r} globally allowed"

    # 3. Bash: category patterns + custom allow
    if tool_name == "Bash" and command:
        cat_patterns = _bash_category_patterns(cfg)
        if _matches_any(command, cat_patterns):
            return "allow", "matched allowed bash category"
        if _matches_any(command, cfg.custom_allow_patterns):
            return "allow", "matched custom allow pattern"

    # 4. Edit/Write: allowed directories
    if tool_name in ("Edit", "Write") and path:
        expanded = [os.path.expanduser(d) for d in cfg.allowed_edit_dirs]
        for d in expanded:
            if path.startswith(d):
                return "allow", f"edit in allowed directory: {d}"
        if _matches_any(path, cfg.custom_allow_patterns):
            return "allow", "matched custom allow pattern (path)"

    return "ask", ""
