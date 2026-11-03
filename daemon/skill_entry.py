"""Skill entry discovery and metadata shared by both distributions."""
import re
from pathlib import Path

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
SKILL_FILENAMES = ("SKILL.md", "skill.md", "CLAUDE.md", "claude.md")


def find_skill_file(root: Path):
    """Return the first supported entry; this does not validate its contents."""
    for name in SKILL_FILENAMES:
        path = Path(root) / name
        if path.is_file():
            return path
    return None
