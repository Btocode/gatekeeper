#!/usr/bin/env python3
"""
gatekeeper stats — show permission grant statistics from daily logs.
Usage:
  gatekeeper stats           # today
  gatekeeper stats 7         # last 7 days
  gatekeeper stats all       # everything
"""
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import date, timedelta

LOG_DIR = os.path.expanduser("~/.claude/perm-logs")


def load_days(days: int | None) -> list[dict]:
    if not os.path.isdir(LOG_DIR):
        return []

    files = sorted(os.listdir(LOG_DIR))
    if days is not None:
        cutoff = (date.today() - timedelta(days=days - 1)).isoformat()
        files  = [f for f in files if f.replace(".log", "") >= cutoff]

    entries = []
    for fname in files:
        path = os.path.join(LOG_DIR, fname)
        try:
            with open(path) as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            pass
    return entries


def show_stats(entries: list[dict]) -> None:
    auto   = [e for e in entries if e["type"] == "auto_allow"]
    manual = [e for e in entries if e["type"] == "decision"]
    danger = [e for e in entries if e["type"] == "decision"
              and "DANGER" in e.get("command", "")]
    total  = len(auto) + len(manual)

    W = 52
    print("=" * W)
    print(" GATEKEEPER STATS")
    print("=" * W)
    print(f"  Total decisions : {total}")
    if total:
        print(f"  Auto-approved   : {len(auto):4d}  ({len(auto)*100//total:3d}%)")
        print(f"  Manual reviewed : {len(manual):4d}  ({len(manual)*100//total:3d}%)")
        manual_allows = sum(1 for e in manual if e.get("decision") == "allow")
        manual_denies = sum(1 for e in manual if e.get("decision") == "deny")
        print(f"    ↳ allowed     : {manual_allows}")
        print(f"    ↳ denied      : {manual_denies}")

    # By day
    by_day: dict[str, dict] = defaultdict(lambda: {"auto": 0, "manual": 0})
    for e in auto:
        by_day[e["ts"][:10]]["auto"] += 1
    for e in manual:
        by_day[e["ts"][:10]]["manual"] += 1

    if len(by_day) > 1:
        print()
        print("  By day:")
        for day in sorted(by_day):
            a = by_day[day]["auto"]
            m = by_day[day]["manual"]
            print(f"    {day}  auto={a:3d}  manual={m:3d}")

    # Auto by session
    if auto:
        print()
        print("  Auto-approved by session:")
        for sess, n in Counter(e["session"] for e in auto).most_common(8):
            print(f"    {sess}  {n:3d} calls")

    # Auto by tool
    if auto:
        print()
        print("  Auto-approved by tool:")
        for tool, n in Counter(e["tool"] for e in auto).most_common():
            print(f"    {tool:<16} {n}")

    # Most auto-approved commands
    if auto:
        print()
        print("  Top auto-approved commands (last 5):")
        for e in auto[-5:]:
            print(f"    [{e['ts'][11:19]}] {e['tool']}: {e['command'][:44]}")

    print("=" * W)


def main() -> None:
    arg = sys.argv[1] if len(sys.argv) > 1 else "1"
    days = None if arg == "all" else int(arg)
    entries = load_days(days)
    if not entries:
        print("No log entries found.")
        return
    span = f"last {days} day(s)" if days else "all time"
    print(f"  Period: {span}  ({len(entries)} log entries)\n")
    show_stats(entries)


if __name__ == "__main__":
    main()
