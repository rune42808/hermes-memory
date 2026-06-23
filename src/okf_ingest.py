# src/okf_ingest.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml  # PyYAML

logger = logging.getLogger(__name__)


# Custom YAML loader that keeps timestamps as raw strings instead of
# converting them to Python datetime objects (which changes the representation).
class _StrTimestampLoader(yaml.SafeLoader):
    pass

_StrTimestampLoader.add_constructor(
    "tag:yaml.org,2002:timestamp",
    lambda loader, node: loader.construct_scalar(node),
)

_SKIP_FILENAMES = {"index.md", "log.md"}
_DEFAULT_TRUST = 0.5          # MemoryStore.default_trust default
_BOOTSTRAP_TRUST = 0.9
_STANDARD_TRUST = 0.7

_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS okf_ingestion_state (
    bundle_path  TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    timestamp    TEXT,
    fact_id      INTEGER,
    ingested_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (bundle_path, file_path)
);
"""


@dataclass
class ParsedConcept:
    content: str       # "{title}: {description}"
    category: str      # "infrastructure" unless overridden in frontmatter
    tags_str: str      # comma-separated tag string for MemoryStore
    trust: float       # 0.9 (bootstrap) or 0.7 (standard)
    timestamp: str     # ISO 8601 from frontmatter, or "" if absent
    file_path: str     # bundle-relative path, e.g. "hosts/rune.md"


@dataclass
class IngestResult:
    added: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return (
            f"added={self.added} updated={self.updated} "
            f"skipped={self.skipped} errors={len(self.errors)}"
        )


def parse_okf_concept(file_path: Path, bundle_root: Path) -> ParsedConcept | None:
    """Parse an OKF concept file. Returns None on any parse failure.

    Extracts: type (required), title, description, tags, timestamp, bootstrap,
    category. Falls back to first non-heading body line if description absent.
    Skips index.md and log.md (caller should filter, but we guard here too).
    """
    if file_path.name in _SKIP_FILENAMES:
        return None

    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.debug("parse_okf_concept: cannot read %s: %s", file_path, exc)
        return None

    if not text.startswith("---"):
        return None

    parts = text.split("---", 2)
    if len(parts) < 3:
        return None

    try:
        fm = yaml.load(parts[1], Loader=_StrTimestampLoader) or {}
    except yaml.YAMLError as exc:
        logger.debug("parse_okf_concept: bad frontmatter in %s: %s", file_path, exc)
        return None

    if not fm.get("type"):
        return None

    # Title: frontmatter > filename stem
    title = (fm.get("title") or "").strip() or file_path.stem

    # Description: frontmatter > first non-heading body line
    description = (fm.get("description") or "").strip()
    if not description:
        for line in parts[2].splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                description = stripped
                break

    if not description:
        return None

    content = f"{title}: {description}"

    # Category
    category = (fm.get("category") or "infrastructure").strip()

    # Tags
    fm_tags = fm.get("tags") or []
    if isinstance(fm_tags, str):
        fm_tags = [t.strip() for t in fm_tags.split(",") if t.strip()]
    rel_path = str(file_path.relative_to(bundle_root))
    concept_type = fm.get("type", "")
    is_bootstrap = bool(fm.get("bootstrap"))
    tag_parts = (
        [str(t) for t in fm_tags]
        + [f"type:{concept_type}", "source:okf", f"bundle:{rel_path}"]
        + (["bootstrap"] if is_bootstrap else [])
    )
    tags_str = ", ".join(tag_parts)

    return ParsedConcept(
        content=content,
        category=category,
        tags_str=tags_str,
        trust=_BOOTSTRAP_TRUST if is_bootstrap else _STANDARD_TRUST,
        timestamp=str(fm.get("timestamp") or ""),
        file_path=rel_path,
    )
