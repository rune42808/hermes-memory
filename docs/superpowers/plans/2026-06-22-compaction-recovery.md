# Compaction Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Seed Holographic memory from an OKF bundle and re-inject key infrastructure facts after context compression.

**Architecture:** An `OKFIngestor` walks `/shared/agents/common/infrastructure/`, parses OKF concept files, and upserts facts into the Holographic store with an `infrastructure` category. A new `on_session_switch` hook in `HolographicMemoryProvider` detects compression events and sets a flag; `system_prompt_block()` checks that flag and prepends a Bootstrap Context block of high-trust infrastructure facts to the session system prompt.

**Tech Stack:** Python 3.11+, SQLite (via `sqlite3` stdlib), PyYAML, pytest, unittest.mock

## Global Constraints

- PyYAML is required (`import yaml`) — add to requirements if not present
- All DB writes go through the existing `MemoryStore` API (`add_fact`, `update_fact`) except `okf_ingestion_state` which `OKFIngestor` owns directly via `store._conn`
- Never raise from `on_session_switch` or `system_prompt_block()` — exceptions must be caught and logged
- `bootstrap_shadow: true` must log but produce no injection output
- Follow the existing `holographic/__init__.py` patterns for config loading and error handling
- OKF reserved filenames (`index.md`, `log.md`) are always skipped
- Default bundle path: `/shared/agents/common/infrastructure/`
- Default DB path: `~/.hermes/memory_store.db` (resolved via `get_hermes_home()`)

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `src/okf_ingest.py` | `ParsedConcept`, `IngestResult`, `parse_okf_concept()`, `OKFIngestor`, CLI |
| Create | `tests/conftest.py` | Mock Hermes imports so MemoryStore is importable without a Hermes install |
| Create | `tests/test_okf_ingest.py` | Tests for `parse_okf_concept()` and `OKFIngestor` |
| Create | `tests/test_holographic_compaction.py` | Tests for compaction hook and bootstrap injection |
| Modify | `src/plugins/memory/holographic/__init__.py` | `infrastructure` category, 4 config keys, `_post_compaction` flag, `on_session_switch`, `_build_bootstrap_block`, updated `system_prompt_block` |

The `src/plugins/memory/holographic/__init__.py` referenced throughout is the copy at  
`/Users/bnaylor/agents/common/projects/cross-channel-awareness/src/plugins/memory/holographic/__init__.py`.  
Work on that file in-place; the `hermes-memory/src/` tree holds only new files.

---

## Task 1: Test harness + OKF concept parser

**Files:**
- Create: `tests/conftest.py`
- Create: `src/okf_ingest.py` (parser portion only)
- Create: `tests/test_okf_ingest.py` (parser tests only)

**Interfaces:**
- Produces:
  - `parse_okf_concept(file_path: Path, bundle_root: Path) -> ParsedConcept | None`
  - `class ParsedConcept` with fields: `content: str`, `category: str`, `tags_str: str`, `trust: float`, `timestamp: str`, `file_path: str`
  - `class IngestResult` with fields: `added: int`, `updated: int`, `skipped: int`, `errors: list[str]`

- [ ] **Step 1: Create `tests/conftest.py`**

```python
# tests/conftest.py
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

# --- Mock Hermes imports so MemoryStore is importable without a Hermes install ---

_hermes_constants = types.ModuleType("hermes_constants")
_hermes_constants.get_hermes_home = lambda: Path("/tmp/hermes_test_home")
_hermes_constants.display_hermes_home = lambda: "/tmp/hermes_test_home"
sys.modules["hermes_constants"] = _hermes_constants

_hermes_state = types.ModuleType("hermes_state")
_hermes_state.apply_wal_with_fallback = lambda conn, db_label="": None
sys.modules["hermes_state"] = _hermes_state

_hermes_cli = types.ModuleType("hermes_cli")
_hermes_cli_config = types.ModuleType("hermes_cli.config")
_hermes_cli_config.cfg_get = lambda cfg, *keys, default=None: default
sys.modules["hermes_cli"] = _hermes_cli
sys.modules["hermes_cli.config"] = _hermes_cli_config

_agent = types.ModuleType("agent")
_agent_mp = types.ModuleType("agent.memory_provider")

class MemoryProvider:
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

_agent_mp.MemoryProvider = MemoryProvider
sys.modules["agent"] = _agent
sys.modules["agent.memory_provider"] = _agent_mp

_tools = types.ModuleType("tools")
_tools_registry = types.ModuleType("tools.registry")
_tools_registry.tool_error = lambda msg: f'{{"error": "{msg}"}}'
sys.modules["tools"] = _tools
sys.modules["tools.registry"] = _tools_registry

# Add project src/ and the holographic plugin tree to sys.path
_here = Path(__file__).parent
sys.path.insert(0, str(_here.parent / "src"))
sys.path.insert(0, str(Path("/Users/bnaylor/agents/common/projects/cross-channel-awareness/src")))
```

- [ ] **Step 2: Write `src/okf_ingest.py` — data classes and parser only**

```python
# src/okf_ingest.py
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml  # PyYAML

logger = logging.getLogger(__name__)

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
        fm = yaml.safe_load(parts[1]) or {}
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
```

- [ ] **Step 3: Write parser tests in `tests/test_okf_ingest.py`**

```python
# tests/test_okf_ingest.py
import textwrap
from pathlib import Path
import pytest
from okf_ingest import parse_okf_concept, ParsedConcept, _BOOTSTRAP_TRUST, _STANDARD_TRUST


@pytest.fixture
def bundle(tmp_path):
    """Return a bundle root directory."""
    return tmp_path


def write_concept(bundle: Path, rel_path: str, content: str) -> Path:
    p = bundle / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


class TestParseOkfConcept:
    def test_full_frontmatter(self, bundle):
        f = write_concept(bundle, "hosts/rune.md", """\
            ---
            type: Host
            title: rune-host
            description: Rune runs on hive.local, a Proxmox VM.
            tags: [infrastructure, host]
            timestamp: 2026-06-22T00:00:00Z
            bootstrap: true
            ---
            Body text here.
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.content == "rune-host: Rune runs on hive.local, a Proxmox VM."
        assert result.category == "infrastructure"
        assert result.trust == _BOOTSTRAP_TRUST
        assert "bootstrap" in result.tags_str
        assert "source:okf" in result.tags_str
        assert "type:Host" in result.tags_str
        assert result.timestamp == "2026-06-22T00:00:00Z"
        assert result.file_path == "hosts/rune.md"

    def test_no_bootstrap_flag(self, bundle):
        f = write_concept(bundle, "clusters/prod.md", """\
            ---
            type: Cluster
            title: prod-cluster
            description: Production k8s cluster on romar.
            tags: [k8s]
            timestamp: 2026-06-01T00:00:00Z
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.trust == _STANDARD_TRUST
        assert "bootstrap" not in result.tags_str

    def test_missing_description_falls_back_to_body(self, bundle):
        f = write_concept(bundle, "hosts/clomp.md", """\
            ---
            type: Host
            title: clomp-host
            ---
            Clomp runs on nest.local with 4 cores.
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.content == "clomp-host: Clomp runs on nest.local with 4 cores."

    def test_heading_lines_skipped_in_body_fallback(self, bundle):
        f = write_concept(bundle, "hosts/clomp.md", """\
            ---
            type: Host
            title: clomp-host
            ---
            # Overview
            Clomp runs on nest.local with 4 cores.
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert "Overview" not in result.content
        assert "nest.local" in result.content

    def test_missing_type_returns_none(self, bundle):
        f = write_concept(bundle, "bad.md", """\
            ---
            title: No Type
            description: This has no type field.
            ---
        """)
        assert parse_okf_concept(f, bundle) is None

    def test_no_frontmatter_returns_none(self, bundle):
        f = write_concept(bundle, "plain.md", "Just plain text, no frontmatter.\n")
        assert parse_okf_concept(f, bundle) is None

    def test_empty_description_and_empty_body_returns_none(self, bundle):
        f = write_concept(bundle, "empty.md", """\
            ---
            type: Host
            title: ghost
            ---
        """)
        assert parse_okf_concept(f, bundle) is None

    def test_skip_index_md(self, bundle):
        f = write_concept(bundle, "index.md", """\
            ---
            type: Index
            title: Root Index
            description: Bundle root.
            ---
        """)
        assert parse_okf_concept(f, bundle) is None

    def test_skip_log_md(self, bundle):
        f = write_concept(bundle, "log.md", """\
            ---
            type: Log
            title: Log
            description: Change log.
            ---
        """)
        assert parse_okf_concept(f, bundle) is None

    def test_category_override(self, bundle):
        f = write_concept(bundle, "prefs/editor.md", """\
            ---
            type: Preference
            title: editor
            description: Uses neovim with LazyVim config.
            category: user_pref
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.category == "user_pref"

    def test_title_falls_back_to_stem(self, bundle):
        f = write_concept(bundle, "hosts/rune.md", """\
            ---
            type: Host
            description: Runs on hive.local.
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert result.content.startswith("rune: ")

    def test_tags_string_and_list_forms(self, bundle):
        f = write_concept(bundle, "c.md", """\
            ---
            type: Cluster
            title: c
            description: A cluster.
            tags: k8s, prod
            ---
        """)
        result = parse_okf_concept(f, bundle)
        assert result is not None
        assert "k8s" in result.tags_str
        assert "prod" in result.tags_str
```

- [ ] **Step 4: Run tests — expect all to pass**

```bash
cd /Users/bnaylor/src/hermes-memory
python -m pytest tests/test_okf_ingest.py -v
```

Expected: 12 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/bnaylor/src/hermes-memory
git init  # if not yet a repo
git add tests/conftest.py src/okf_ingest.py tests/test_okf_ingest.py
git commit -m "feat: OKF concept parser (ParsedConcept, parse_okf_concept)"
```

---

## Task 2: OKFIngestor — schema, walk, upsert

**Files:**
- Modify: `src/okf_ingest.py` (add `OKFIngestor` class)
- Modify: `tests/test_okf_ingest.py` (add `OKFIngestor` tests)

**Interfaces:**
- Consumes: `ParsedConcept`, `IngestResult`, `_STATE_SCHEMA` from Task 1; `MemoryStore` from `plugins.memory.holographic.store`
- Produces:
  - `class OKFIngestor(store: MemoryStore)`
  - `OKFIngestor.ingest_bundle(bundle_path: str | Path, *, dry_run: bool = False) -> IngestResult`

- [ ] **Step 1: Write the failing tests first**

Append to `tests/test_okf_ingest.py`:

```python
import sqlite3
import textwrap
from plugins.memory.holographic.store import MemoryStore


@pytest.fixture
def tmp_db(tmp_path):
    """Return a MemoryStore backed by a temp SQLite file."""
    db = tmp_path / "test_memory.db"
    return MemoryStore(db_path=str(db))


@pytest.fixture
def bundle_with_facts(bundle):
    """Bundle with two concepts — one bootstrap, one standard."""
    write_concept(bundle, "hosts/rune.md", """\
        ---
        type: Host
        title: rune-host
        description: Runs on hive.local, Proxmox VM.
        tags: [host]
        timestamp: 2026-06-22T00:00:00Z
        bootstrap: true
        ---
    """)
    write_concept(bundle, "clusters/prod.md", """\
        ---
        type: Cluster
        title: prod-cluster
        description: Production k8s on romar.
        tags: [k8s]
        timestamp: 2026-06-01T00:00:00Z
        ---
    """)
    return bundle


class TestOKFIngestor:
    def test_ingest_adds_facts(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        result = ingestor.ingest_bundle(bundle_with_facts)
        assert result.added == 2
        assert result.updated == 0
        assert result.skipped == 0
        assert result.errors == []

    def test_facts_have_infrastructure_category(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        ingestor.ingest_bundle(bundle_with_facts)
        facts = tmp_db.list_facts(category="infrastructure")
        assert len(facts) == 2

    def test_bootstrap_fact_has_high_trust(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor, _BOOTSTRAP_TRUST
        ingestor = OKFIngestor(tmp_db)
        ingestor.ingest_bundle(bundle_with_facts)
        facts = tmp_db.list_facts(category="infrastructure", min_trust=0.85)
        assert len(facts) == 1
        assert abs(facts[0]["trust_score"] - _BOOTSTRAP_TRUST) < 0.01

    def test_second_run_skips_unchanged(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        ingestor.ingest_bundle(bundle_with_facts)
        result2 = ingestor.ingest_bundle(bundle_with_facts)
        assert result2.added == 0
        assert result2.skipped == 2

    def test_changed_timestamp_triggers_update(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        ingestor.ingest_bundle(bundle_with_facts)
        # Overwrite one file with a new timestamp
        write_concept(bundle_with_facts, "hosts/rune.md", """\
            ---
            type: Host
            title: rune-host
            description: Now runs on hive2.local after migration.
            tags: [host]
            timestamp: 2026-06-23T00:00:00Z
            bootstrap: true
            ---
        """)
        result2 = ingestor.ingest_bundle(bundle_with_facts)
        assert result2.updated == 1
        assert result2.skipped == 1
        # Verify content was updated
        facts = tmp_db.list_facts(category="infrastructure")
        contents = {f["content"] for f in facts}
        assert any("hive2.local" in c for c in contents)

    def test_dry_run_does_not_write(self, tmp_db, bundle_with_facts):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        result = ingestor.ingest_bundle(bundle_with_facts, dry_run=True)
        assert result.added == 2      # counted but not written
        facts = tmp_db.list_facts(category="infrastructure")
        assert len(facts) == 0        # nothing actually stored

    def test_missing_bundle_path_returns_error(self, tmp_db, tmp_path):
        from okf_ingest import OKFIngestor
        ingestor = OKFIngestor(tmp_db)
        result = ingestor.ingest_bundle(tmp_path / "nonexistent")
        assert len(result.errors) == 1
        assert result.added == 0

    def test_skips_index_and_log(self, tmp_db, bundle):
        from okf_ingest import OKFIngestor
        write_concept(bundle, "index.md", "---\ntype: Index\ntitle: i\ndescription: d\n---\n")
        write_concept(bundle, "log.md", "---\ntype: Log\ntitle: l\ndescription: d\n---\n")
        ingestor = OKFIngestor(tmp_db)
        result = ingestor.ingest_bundle(bundle)
        assert result.added == 0
        assert result.skipped == 0  # skipped filenames aren't counted as skipped
```

- [ ] **Step 2: Run tests — expect failures**

```bash
cd /Users/bnaylor/src/hermes-memory
python -m pytest tests/test_okf_ingest.py::TestOKFIngestor -v
```

Expected: `ImportError: cannot import name 'OKFIngestor'`

- [ ] **Step 3: Implement `OKFIngestor` in `src/okf_ingest.py`**

Append after the `parse_okf_concept` function:

```python
class OKFIngestor:
    """Walk an OKF bundle and upsert facts into the Holographic store."""

    def __init__(self, store) -> None:
        self._store = store
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._store._conn.executescript(_STATE_SCHEMA)
        self._store._conn.commit()

    def ingest_bundle(
        self, bundle_path: str | Path, *, dry_run: bool = False
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
    ) -> str | None:
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
```

- [ ] **Step 4: Run tests — expect all to pass**

```bash
cd /Users/bnaylor/src/hermes-memory
python -m pytest tests/test_okf_ingest.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/okf_ingest.py tests/test_okf_ingest.py
git commit -m "feat: OKFIngestor — walk OKF bundle and upsert into Holographic store"
```

---

## Task 3: CLI entry point

**Files:**
- Modify: `src/okf_ingest.py` (add `__main__` block)
- Modify: `tests/test_okf_ingest.py` (add CLI tests)

**Interfaces:**
- Consumes: `OKFIngestor`, `IngestResult` from Task 2; `MemoryStore` from holographic store
- Produces: runnable `python -m hermes_memory.okf_ingest [bundle_path] [--db path] [--dry-run] [--verbose]`

- [ ] **Step 1: Write the failing CLI test**

Append to `tests/test_okf_ingest.py`:

```python
import subprocess
import sys


class TestCLI:
    def test_dry_run_prints_summary(self, bundle_with_facts, tmp_path):
        db = tmp_path / "cli_test.db"
        result = subprocess.run(
            [sys.executable, "-m", "okf_ingest",
             str(bundle_with_facts), "--db", str(db), "--dry-run"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent / "src"),
        )
        assert result.returncode == 0
        assert "added=2" in result.stdout

    def test_missing_bundle_exits_nonzero(self, tmp_path):
        db = tmp_path / "cli_test.db"
        result = subprocess.run(
            [sys.executable, "-m", "okf_ingest",
             str(tmp_path / "no_such_dir"), "--db", str(db)],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent / "src"),
        )
        assert result.returncode != 0
```

- [ ] **Step 2: Run — expect failure**

```bash
python -m pytest tests/test_okf_ingest.py::TestCLI -v
```

Expected: subprocess exits nonzero (no `__main__` block yet).

- [ ] **Step 3: Add `__main__` block to `src/okf_ingest.py`**

Append at the end of the file:

```python
if __name__ == "__main__":
    import argparse
    import sys

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
```

- [ ] **Step 4: Run tests — expect all to pass**

```bash
python -m pytest tests/test_okf_ingest.py -v
```

Expected: all tests PASS.

- [ ] **Step 5: Manual smoke test (dry-run)**

```bash
cd /Users/bnaylor/src/hermes-memory/src
python -m okf_ingest /shared/agents/common/infrastructure/ --dry-run --verbose
```

Expected: summary line printed, no DB written, no crash. (If bundle path doesn't exist yet, create a test file first — see Step 6.)

- [ ] **Step 6: Create a minimal seed file to verify end-to-end**

```bash
mkdir -p /shared/agents/common/infrastructure/
cat > /shared/agents/common/infrastructure/rune-host.md << 'EOF'
---
type: Host
title: rune-host
description: Rune runs on hive.local, a Proxmox VM — 8 cores, 32GB RAM, NVMe root.
tags: [infrastructure, host]
timestamp: 2026-06-22T00:00:00Z
bootstrap: true
---

Primary Hermes agent host. Managed via Proxmox at hive.proxmox.local.
EOF
python -m okf_ingest /shared/agents/common/infrastructure/ --dry-run
```

Expected: `added=1 updated=0 skipped=0 errors=0`

- [ ] **Step 7: Commit**

```bash
cd /Users/bnaylor/src/hermes-memory
git add src/okf_ingest.py tests/test_okf_ingest.py
git commit -m "feat: CLI entry point for okf_ingest (python -m okf_ingest)"
```

---

## Task 4: `infrastructure` category + config keys

**Files:**
- Modify: `/Users/bnaylor/agents/common/projects/cross-channel-awareness/src/plugins/memory/holographic/__init__.py`
- Create: `tests/test_holographic_compaction.py`

**Interfaces:**
- Consumes: `HolographicMemoryProvider` from `holographic/__init__.py`
- Produces: `infrastructure` in the category enum; 4 new config keys readable via `self._config.get()`

- [ ] **Step 1: Write the failing test**

Create `tests/test_holographic_compaction.py`:

```python
# tests/test_holographic_compaction.py
"""Tests for compaction recovery: infrastructure category, config, on_session_switch, bootstrap injection."""

import pytest
from pathlib import Path
from plugins.memory.holographic import HolographicMemoryProvider
from plugins.memory.holographic.store import MemoryStore


@pytest.fixture
def tmp_store(tmp_path):
    return MemoryStore(db_path=str(tmp_path / "mem.db"))


@pytest.fixture
def provider(tmp_store):
    config = {
        "db_path": str(tmp_store.db_path),
        "bootstrap_inject_limit": 5,
        "bootstrap_min_trust": 0.7,
        "bootstrap_shadow": False,
    }
    p = HolographicMemoryProvider(config=config)
    p.initialize("test-session-001")
    return p


class TestInfrastructureCategory:
    def test_infrastructure_accepted_by_fact_store(self, provider, tmp_store):
        """fact_store(action='add') with category='infrastructure' must not return an error."""
        import json
        result = provider.handle_tool_call("fact_store", {
            "action": "add",
            "content": "test host: runs on hive.local",
            "category": "infrastructure",
        })
        data = json.loads(result)
        assert "fact_id" in data
        assert data.get("status") == "added"

    def test_infrastructure_in_schema_enum(self, provider):
        schemas = provider.get_tool_schemas()
        fact_store_schema = next(s for s in schemas if s["name"] == "fact_store")
        category_enum = fact_store_schema["parameters"]["properties"]["category"]["enum"]
        assert "infrastructure" in category_enum


class TestConfigKeys:
    def test_default_config_values(self):
        p = HolographicMemoryProvider(config={})
        assert p._config.get("bootstrap_inject_limit", 15) == 15
        assert p._config.get("bootstrap_min_trust", 0.7) == 0.7
        assert p._config.get("bootstrap_shadow", False) == False
        assert p._config.get("okf_bundle_path", "/shared/agents/common/infrastructure/") == "/shared/agents/common/infrastructure/"

    def test_config_keys_in_schema(self):
        p = HolographicMemoryProvider(config={})
        schema = p.get_config_schema()
        keys = {entry["key"] for entry in schema}
        assert "okf_bundle_path" in keys
        assert "bootstrap_inject_limit" in keys
        assert "bootstrap_min_trust" in keys
        assert "bootstrap_shadow" in keys
```

- [ ] **Step 2: Run — expect failures**

```bash
python -m pytest tests/test_holographic_compaction.py::TestInfrastructureCategory tests/test_holographic_compaction.py::TestConfigKeys -v
```

Expected: `infrastructure` not in enum; config keys missing from schema.

- [ ] **Step 3: Add `infrastructure` to category enum**

In `/Users/bnaylor/agents/common/projects/cross-channel-awareness/src/plugins/memory/holographic/__init__.py`, find:

```python
"category": {"type": "string", "enum": ["user_pref", "project", "tool", "general"]},
```

Replace with:

```python
"category": {"type": "string", "enum": ["user_pref", "project", "tool", "general", "infrastructure"]},
```

- [ ] **Step 4: Add 4 config keys to `get_config_schema()`**

In `HolographicMemoryProvider.get_config_schema()`, add after the existing entries:

```python
{"key": "okf_bundle_path",        "description": "OKF bundle path for infrastructure facts",           "default": "/shared/agents/common/infrastructure/"},
{"key": "bootstrap_inject_limit", "description": "Max facts injected after context compression",        "default": "15"},
{"key": "bootstrap_min_trust",    "description": "Minimum trust score for post-compaction injection",   "default": "0.7"},
{"key": "bootstrap_shadow",       "description": "Log what would inject without injecting (shadow mode)", "default": "false", "choices": ["true", "false"]},
```

- [ ] **Step 5: Run tests — expect all to pass**

```bash
python -m pytest tests/test_holographic_compaction.py::TestInfrastructureCategory tests/test_holographic_compaction.py::TestConfigKeys -v
```

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/bnaylor/src/hermes-memory
git add tests/test_holographic_compaction.py
cd /Users/bnaylor/agents/common/projects/cross-channel-awareness
git add src/plugins/memory/holographic/__init__.py
git commit -m "feat: add infrastructure category and bootstrap config keys to Holographic plugin"
```

---

## Task 5: Compaction recovery hook + bootstrap injection

**Files:**
- Modify: `/Users/bnaylor/agents/common/projects/cross-channel-awareness/src/plugins/memory/holographic/__init__.py`
- Modify: `tests/test_holographic_compaction.py`

**Interfaces:**
- Consumes: `HolographicMemoryProvider`, config keys, `MemoryStore.list_facts()` from prior tasks
- Produces:
  - `HolographicMemoryProvider.on_session_switch(new_session_id, *, reason="", **kwargs) -> None`
  - `HolographicMemoryProvider._build_bootstrap_block() -> str`
  - `HolographicMemoryProvider.system_prompt_block()` — extended to prepend bootstrap block when `_post_compaction` is set

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_holographic_compaction.py`:

```python
class TestCompactionHook:
    def test_compression_reason_sets_flag(self, provider):
        assert provider._post_compaction is False
        provider.on_session_switch(
            "new-session-002",
            parent_session_id="test-session-001",
            reason="compression",
        )
        assert provider._post_compaction is True

    def test_non_compression_reason_does_not_set_flag(self, provider):
        provider.on_session_switch("new-session-002", reason="resume")
        assert provider._post_compaction is False

    def test_no_reason_does_not_set_flag(self, provider):
        provider.on_session_switch("new-session-002")
        assert provider._post_compaction is False


class TestBootstrapInjection:
    def _seed_infra_facts(self, store):
        """Add two infrastructure facts directly to the store."""
        fid1 = store.add_fact(
            "rune-host: Runs on hive.local, Proxmox VM.",
            category="infrastructure",
            tags="bootstrap, type:Host",
        )
        store.update_fact(fid1, trust_delta=0.4)   # → 0.9
        fid2 = store.add_fact(
            "prod-cluster: Production k8s on romar.",
            category="infrastructure",
            tags="type:Cluster",
        )
        store.update_fact(fid2, trust_delta=0.2)   # → 0.7
        return fid1, fid2

    def test_bootstrap_block_appears_after_compression(self, provider, tmp_store):
        self._seed_infra_facts(tmp_store)
        provider.on_session_switch("new-session-002", reason="compression")
        block = provider.system_prompt_block()
        assert "Bootstrap Context" in block
        assert "rune-host" in block
        assert "prod-cluster" in block

    def test_bootstrap_block_absent_without_compression(self, provider, tmp_store):
        self._seed_infra_facts(tmp_store)
        block = provider.system_prompt_block()
        assert "Bootstrap Context" not in block

    def test_flag_cleared_after_first_call(self, provider, tmp_store):
        self._seed_infra_facts(tmp_store)
        provider.on_session_switch("new-session-002", reason="compression")
        provider.system_prompt_block()  # consumes the flag
        assert provider._post_compaction is False
        block2 = provider.system_prompt_block()
        assert "Bootstrap Context" not in block2

    def test_empty_store_produces_no_bootstrap_block(self, provider):
        provider.on_session_switch("new-session-002", reason="compression")
        block = provider.system_prompt_block()
        assert "Bootstrap Context" not in block

    def test_shadow_mode_logs_but_does_not_inject(self, tmp_store, caplog):
        import logging
        config = {
            "db_path": str(tmp_store.db_path),
            "bootstrap_shadow": True,
            "bootstrap_inject_limit": 5,
            "bootstrap_min_trust": 0.7,
        }
        p = HolographicMemoryProvider(config=config)
        p.initialize("shadow-session")
        fid = tmp_store.add_fact(
            "rune-host: Runs on hive.local.", category="infrastructure", tags="bootstrap"
        )
        tmp_store.update_fact(fid, trust_delta=0.4)
        p.on_session_switch("shadow-session-002", reason="compression")
        with caplog.at_level(logging.INFO, logger="plugins.memory.holographic"):
            block = p.system_prompt_block()
        assert "Bootstrap Context" not in block
        assert any("BOOTSTRAP:shadow" in r.message for r in caplog.records)

    def test_inject_limit_respected(self, provider, tmp_store):
        config = {
            "db_path": str(tmp_store.db_path),
            "bootstrap_inject_limit": 2,
            "bootstrap_min_trust": 0.7,
            "bootstrap_shadow": False,
        }
        p = HolographicMemoryProvider(config=config)
        p.initialize("limit-session")
        for i in range(5):
            fid = tmp_store.add_fact(
                f"fact-{i}: Infrastructure fact number {i}.",
                category="infrastructure",
                tags="bootstrap",
            )
            tmp_store.update_fact(fid, trust_delta=0.4)
        p.on_session_switch("limit-session-002", reason="compression")
        block = p.system_prompt_block()
        fact_lines = [l for l in block.splitlines() if l.strip().startswith("-")]
        assert len(fact_lines) <= 2
```

- [ ] **Step 2: Run — expect failures**

```bash
python -m pytest tests/test_holographic_compaction.py::TestCompactionHook tests/test_holographic_compaction.py::TestBootstrapInjection -v
```

Expected: `AttributeError: 'HolographicMemoryProvider' has no attribute '_post_compaction'`

- [ ] **Step 3: Add `_post_compaction` flag to `HolographicMemoryProvider.__init__`**

In `HolographicMemoryProvider.__init__`, after the existing assignments, add:

```python
        self._post_compaction: bool = False
        self._post_compaction_parent: str = ""
```

- [ ] **Step 4: Add `on_session_switch` method to `HolographicMemoryProvider`**

Add after `sync_turn`:

```python
    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        if kwargs.get("reason") == "compression":
            self._post_compaction = True
            self._post_compaction_parent = parent_session_id
```

- [ ] **Step 5: Add `_build_bootstrap_block` method**

Add after `on_session_switch`:

```python
    def _build_bootstrap_block(self) -> str:
        """Fetch top infrastructure facts and format as a Bootstrap Context block.

        Returns "" in shadow mode (facts are only logged) or when store is empty.
        """
        shadow = bool(self._config.get("bootstrap_shadow", False))
        limit = int(self._config.get("bootstrap_inject_limit", 15))
        min_trust = float(self._config.get("bootstrap_min_trust", 0.7))

        try:
            facts = self._store.list_facts(
                category="infrastructure", min_trust=min_trust, limit=limit
            )
        except Exception as exc:
            logger.debug("_build_bootstrap_block: list_facts failed: %s", exc)
            return ""

        if not facts:
            return ""

        if shadow:
            logger.info(
                "BOOTSTRAP:shadow post-compaction would inject %d infrastructure facts",
                len(facts),
            )
            for f in facts:
                logger.info(
                    "BOOTSTRAP:shadow [%.1f] %.80s",
                    f.get("trust_score", 0),
                    f.get("content", ""),
                )
            return ""

        lines = [
            "## Bootstrap Context (Post-Compaction Recovery)",
            "",
            "Context was just compressed. Key infrastructure facts re-injected:",
            "",
        ]
        for f in facts:
            trust = f.get("trust_score", 0)
            content = f.get("content", "")
            lines.append(f"- [{trust:.1f}] {content}")

        return "\n".join(lines)
```

- [ ] **Step 6: Extend `system_prompt_block` to prepend bootstrap block**

Replace the existing `system_prompt_block` method body with:

```python
    def system_prompt_block(self) -> str:
        if not self._store:
            return ""

        bootstrap_block = ""
        if self._post_compaction:
            self._post_compaction = False
            try:
                bootstrap_block = self._build_bootstrap_block()
            except Exception as exc:
                logger.debug("system_prompt_block: bootstrap build failed: %s", exc)

        try:
            total = self._store._conn.execute(
                "SELECT COUNT(*) FROM facts"
            ).fetchone()[0]
        except Exception:
            total = 0

        if total == 0:
            memory_block = (
                "# Holographic Memory\n"
                "Active. Empty fact store — proactively add facts the user would expect you to remember.\n"
                "Use fact_store(action='add') to store durable structured facts about people, projects, preferences, decisions.\n"
                "Use fact_feedback to rate facts after using them (trains trust scores)."
            )
        else:
            memory_block = (
                f"# Holographic Memory\n"
                f"Active. {total} facts stored with entity resolution and trust scoring.\n"
                f"Use fact_store to search, probe entities, reason across entities, or add facts.\n"
                f"Use fact_feedback to rate facts after using them (trains trust scores)."
            )

        if bootstrap_block:
            return bootstrap_block + "\n\n" + memory_block
        return memory_block
```

- [ ] **Step 7: Run all tests — expect all to pass**

```bash
cd /Users/bnaylor/src/hermes-memory
python -m pytest tests/ -v
```

Expected: all tests PASS. Look specifically for:
- `TestCompactionHook` — 3 tests PASS
- `TestBootstrapInjection` — 6 tests PASS

- [ ] **Step 8: Commit**

```bash
cd /Users/bnaylor/src/hermes-memory
git add tests/test_holographic_compaction.py
cd /Users/bnaylor/agents/common/projects/cross-channel-awareness
git add src/plugins/memory/holographic/__init__.py
git commit -m "feat: compaction recovery hook — bootstrap infra facts re-injected after context compression"
```

---

## Rollout Checklist

Once all tasks pass, follow this sequence to deploy on Rune:

- [ ] Run `python -m okf_ingest --dry-run` against `/shared/agents/common/infrastructure/` to verify bundle parses cleanly
- [ ] Run without `--dry-run` to seed the store; verify with `fact_store(action='list', category='infrastructure')`
- [ ] Set `bootstrap_shadow: true` in `~/.hermes/config.yaml` and restart Rune's gateway
- [ ] Trigger a manual compaction on Rune (or wait for one to occur naturally)
- [ ] Check logs for `BOOTSTRAP:shadow post-compaction would inject N infrastructure facts`
- [ ] When shadow log looks correct, set `bootstrap_shadow: false` and restart
- [ ] Verify the Bootstrap Context block appears in Rune's resumed session system prompt
