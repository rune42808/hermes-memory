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


class OKFIngestor:
    """Walk an OKF bundle and upsert facts into the Holographic store."""

    def __init__(self, store) -> None:
        self._store = store
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._store._conn.executescript(_STATE_SCHEMA)
        self._store._conn.commit()

    def ingest_bundle(
        self, bundle_path: "str | Path", *, dry_run: bool = False
    ) -> IngestResult:
        bundle_root = Path(bundle_path).expanduser().resolve()
        result = IngestResult()

        if not bundle_root.exists():
            result.errors.append(f"Bundle path does not exist: {bundle_root}")
            return result

        for md_file in sorted(bundle_root.rglob("*.md")):
            if md_file.name in _SKIP_FILENAMES:
                continue
            outcome = self._ingest_file(bundle_root, md_file, dry_run=dry_run)
            if outcome == "added":
                result.added += 1
            elif outcome == "updated":
                result.updated += 1
            elif outcome == "skipped":
                result.skipped += 1
            elif outcome is not None:
                result.errors.append(f"{md_file.name}: {outcome}")

        return result

    def _ingest_file(
        self, bundle_root: Path, file_path: Path, *, dry_run: bool
    ) -> "str | None":
        concept = parse_okf_concept(file_path, bundle_root)
        if concept is None:
            return None  # silently skip unparseable files (missing type, etc.)

        bundle_str = str(bundle_root)

        row = self._store._conn.execute(
            "SELECT timestamp, fact_id FROM okf_ingestion_state "
            "WHERE bundle_path = ? AND file_path = ?",
            (bundle_str, concept.file_path),
        ).fetchone()

        if row is not None:
            stored_ts = row[0]
            stored_id = row[1]
            if stored_ts and stored_ts == concept.timestamp:
                return "skipped"
            # Timestamp changed — update
            if not dry_run:
                self._store.update_fact(
                    stored_id,
                    content=concept.content,
                    tags=concept.tags_str,
                    category=concept.category,
                )
                self._store._conn.execute(
                    "UPDATE okf_ingestion_state "
                    "SET timestamp = ?, ingested_at = CURRENT_TIMESTAMP "
                    "WHERE bundle_path = ? AND file_path = ?",
                    (concept.timestamp, bundle_str, concept.file_path),
                )
                self._store._conn.commit()
            return "updated"

        # New concept
        if not dry_run:
            fact_id = self._store.add_fact(
                concept.content,
                category=concept.category,
                tags=concept.tags_str,
            )
            # Adjust trust from store default (0.5) to desired level
            trust_delta = concept.trust - self._store.default_trust
            if abs(trust_delta) > 0.001:
                self._store.update_fact(fact_id, trust_delta=trust_delta)
            self._store._conn.execute(
                "INSERT INTO okf_ingestion_state "
                "(bundle_path, file_path, timestamp, fact_id) VALUES (?, ?, ?, ?)",
                (bundle_str, concept.file_path, concept.timestamp, fact_id),
            )
            self._store._conn.commit()
        return "added"


if __name__ == "__main__":
    import argparse
    import sys
    import types
    from pathlib import Path

    parser = argparse.ArgumentParser(
        description="Ingest an OKF bundle into the Holographic fact store."
    )
    parser.add_argument(
        "bundle_path",
        nargs="?",
        default="/shared/agents/common/infrastructure/",
        help="Path to OKF bundle root (default: /shared/agents/common/infrastructure/)",
    )
    parser.add_argument("--db", default=None, help="Path to memory_store.db (default: ~/.hermes/memory_store.db)")
    parser.add_argument("--dry-run", action="store_true", help="Parse and report without writing")
    parser.add_argument("--verbose", action="store_true", help="Log each file processed")
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    # Stub Hermes modules when running standalone (outside a Hermes installation).
    # Uses try/except so a real Hermes install takes precedence automatically.
    try:
        import hermes_constants as _hc  # noqa: F401
    except ImportError:
        _hc_stub = types.ModuleType("hermes_constants")
        setattr(_hc_stub, "get_hermes_home", lambda: Path.home() / ".hermes")
        setattr(_hc_stub, "display_hermes_home", lambda: str(Path.home() / ".hermes"))
        sys.modules["hermes_constants"] = _hc_stub
    try:
        import hermes_state as _hs  # noqa: F401
    except ImportError:
        _hs_stub = types.ModuleType("hermes_state")
        setattr(_hs_stub, "apply_wal_with_fallback", lambda conn, db_label="": None)
        sys.modules["hermes_state"] = _hs_stub
    if "hermes_cli" not in sys.modules:
        sys.modules["hermes_cli"] = types.ModuleType("hermes_cli")
    if "hermes_cli.config" not in sys.modules:
        _cfg_stub = types.ModuleType("hermes_cli.config")
        setattr(_cfg_stub, "cfg_get", lambda cfg, *keys, default=None: default)
        sys.modules["hermes_cli.config"] = _cfg_stub
    if "agent" not in sys.modules:
        sys.modules["agent"] = types.ModuleType("agent")
    if "agent.memory_provider" not in sys.modules:
        class _MemoryProvider:
            def initialize(self, session_id, **kwargs): pass
            def system_prompt_block(self): return ""
            def prefetch(self, query, *, session_id=""): return ""
            def sync_turn(self, user_content, assistant_content, *, session_id=""): pass
            def get_tool_schemas(self): return []
            def handle_tool_call(self, tool_name, args, **kwargs): return ""
            def on_session_end(self, messages): pass
            def on_memory_write(self, action, target, content): pass
            def on_session_switch(self, new_session_id, **kwargs): pass
            def shutdown(self): pass
            def is_available(self): return True
            def save_config(self, values, hermes_home): pass
            def get_config_schema(self): return []
            @property
            def name(self): return "base"
        _amp_stub = types.ModuleType("agent.memory_provider")
        setattr(_amp_stub, "MemoryProvider", _MemoryProvider)
        sys.modules["agent.memory_provider"] = _amp_stub
    if "tools" not in sys.modules:
        sys.modules["tools"] = types.ModuleType("tools")
    if "tools.registry" not in sys.modules:
        _tr_stub = types.ModuleType("tools.registry")
        setattr(_tr_stub, "tool_error", lambda msg: f'{{"error": "{msg}"}}')
        sys.modules["tools.registry"] = _tr_stub
    from plugins.memory.holographic.store import MemoryStore

    store = MemoryStore(db_path=args.db)
    ingestor = OKFIngestor(store)

    result = ingestor.ingest_bundle(args.bundle_path, dry_run=args.dry_run)

    mode = "[dry-run] " if args.dry_run else ""
    print(f"{mode}{result}")

    if result.errors:
        for err in result.errors:
            print(f"  ERROR: {err}", file=sys.stderr)
        sys.exit(1)
