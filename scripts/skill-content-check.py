#!/usr/bin/env python3
"""Check for content-stale skills — skills whose required_commands are missing.

Reads skill YAML frontmatter, verifies required_commands exist on the host,
and reports skills whose commands are not found.

Usage:
    python3 skill-content-check.py [--skills-dir ~/.hermes/skills]
    python3 skill-content-check.py --json

Exit codes:
    0 — all checks passed
    1 — one or more content-stale skills found
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import yaml
from pathlib import Path


def _parse_frontmatter(path: Path) -> dict | None:
    """Extract YAML frontmatter from a SKILL.md file."""
    try:
        with open(path) as f:
            content = f.read()
        if not content.startswith("---"):
            return None
        end = content.find("---", 3)
        if end == -1:
            return None
        return yaml.safe_load(content[3:end]) or {}
    except Exception:
        return None


def check_skills(skills_dir: Path) -> list[dict]:
    """Scan all skills and return a list of content-stale findings."""
    findings = []
    for skill_dir in sorted(skills_dir.iterdir()):
        if not skill_dir.is_dir() or skill_dir.name.startswith("."):
            continue

        skill_md = skill_dir / "SKILL.md"
        if not skill_md.exists():
            continue

        fm = _parse_frontmatter(skill_md)
        if not fm:
            continue

        commands = fm.get("required_commands", [])
        if not commands:
            # Also check metadata.hermes.required_commands (alternate key)
            meta = fm.get("metadata", {}).get("hermes", {})
            commands = meta.get("required_commands", [])

        if not commands:
            continue

        missing = []
        for cmd in commands:
            if shutil.which(cmd) is None:
                missing.append(cmd)

        if missing:
            findings.append({
                "skill": skill_dir.name,
                "missing_commands": missing,
                "skill_path": str(skill_md),
            })

    return findings


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Check skills for missing required_commands"
    )
    parser.add_argument(
        "--skills-dir",
        default=os.path.expanduser("~/.hermes/skills"),
        help="Path to skills directory",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    skills_dir = Path(args.skills_dir)
    if not skills_dir.is_dir():
        print(f"ERROR: skills directory not found: {skills_dir}", file=sys.stderr)
        sys.exit(2)

    findings = check_skills(skills_dir)

    if args.json:
        print(json.dumps({"findings": findings, "count": len(findings)}, indent=2))
    else:
        if not findings:
            print("All skills OK — no missing commands detected.")
        else:
            print(f"{len(findings)} content-stale skill(s) found:\n")
            for f in findings:
                cmds = ", ".join(f["missing_commands"])
                print(f"  {f['skill']}: {cmds}")

    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
