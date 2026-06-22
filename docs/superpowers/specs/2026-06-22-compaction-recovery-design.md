# Compaction Recovery via OKF-Seeded Holographic Memory

**Status:** Design approved  
**Date:** 2026-06-22  
**Author:** scromp + Claude (architect)  
**Project:** hermes-memory  

---

## Problem

Rune (DeepSeek v4, infra/SRE agent) loses working knowledge during context
compaction — forgetting what host he runs on, what clusters are under
management, and what was in progress. Recovery currently requires several hours
of manual corrections and re-prompting.

The existing Holographic memory system handles cross-channel context (via
CheckpointTrigger + prefetch), but it is not seeded with stable infrastructure
facts, and nothing re-injects them after a compaction event.

---

## Goals

1. Give Rune (and Clomp) a curated, agent-maintained library of infrastructure
   facts on an NFS share in OKF format.
2. Automatically seed those facts into the Holographic fact store.
3. Automatically re-inject the most important facts into the resumed session
   immediately after context compression.

---

## Non-Goals

- Replacing the existing cross-channel awareness (checkpoint/prefetch) system.
- Auto-watching the OKF bundle for changes (agents re-run the ingestor
  explicitly when they update the library).
- Cross-platform identity or multi-user support.

---

## Architecture

```
/shared/agents/common/infrastructure/   ← OKF bundle on NFS, curated by Rune + Clomp
        ↓  (OKFIngestor, run on-demand by agents or CLI)
~/.hermes/memory_store.db               ← Holographic fact store
  facts: category=infrastructure
  facts: tagged bootstrap                ← trust=0.9, float to top
  facts: untagged bootstrap              ← trust=0.7
        ↓  (on_session_switch reason=compression)
system_prompt_block()                   ← Bootstrap Context block injected into new session
```

Two new components, both delivered in the `hermes-memory` package:

1. **`OKFIngestor`** — walks the OKF bundle, upserts facts into Holographic.
2. **`HolographicMemoryProvider` additions** — post-compaction hook + bootstrap
   injection in `system_prompt_block()`.

---

## Component 1: OKF Ingestor

### Inputs

- Bundle root: `/shared/agents/common/infrastructure/` (default; configurable)
- Target DB: `~/.hermes/memory_store.db` (default; configurable)

### Per-Concept Processing

For each `.md` file in the bundle (skipping `index.md`, `log.md`):

1. Parse YAML frontmatter — extract `type`, `title`, `description`, `tags`,
   `timestamp`, and the `bootstrap` extension key.
2. Build fact content: `"{title}: {description}"`. If `description` is absent,
   fall back to the first non-heading line of the body.
3. Derive category: defaults to `infrastructure` for all concepts in this
   bundle. Agents may override per-file with a `category` frontmatter key.
4. Build tags: OKF `tags` list + `type:<value>` + `source:okf` +
   `bundle:<relative-path>` + `bootstrap` (if `bootstrap: true` in frontmatter).
5. Set trust: `bootstrap: true` → 0.9, otherwise 0.7.
6. Upsert: `store.add_fact()` for new facts (dedup by UNIQUE content
   constraint); `store.update_fact()` for changed facts.

### Freshness Tracking

A lightweight `okf_ingestion_state` table in `memory_store.db`:

```sql
CREATE TABLE IF NOT EXISTS okf_ingestion_state (
    bundle_path  TEXT NOT NULL,
    file_path    TEXT NOT NULL,
    timestamp    TEXT,           -- ISO 8601 from OKF frontmatter
    fact_id      INTEGER,
    ingested_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (bundle_path, file_path)
);
```

On each ingestor run, files whose `timestamp` matches the stored value are
skipped. Files with a changed or absent `timestamp` are re-ingested.

### CLI

```
python -m hermes_memory.okf_ingest [bundle_path] [--db path] [--dry-run] [--verbose]
```

Prints a summary: N added, N updated, N skipped, N errors. `--dry-run` parses
and reports without writing anything.

### OKF Library Conventions (for agents authoring facts)

- Use `bootstrap: true` for facts that must survive compaction: host identity,
  cluster inventory, escalation paths, critical config.
- Use a short `description` (one sentence). This becomes the retrievable fact.
- `type` values are free-form; suggested values for this bundle:
  `Host`, `Cluster`, `Service`, `Playbook`, `Runbook`, `Reference`.

Example concept:

```markdown
---
type: Host
title: rune-host
description: Rune runs on hive.local, a Proxmox VM — 8 cores, 32GB RAM, NVMe root.
tags: [infrastructure, host]
timestamp: 2026-06-22T00:00:00Z
bootstrap: true
---

Primary Hermes agent host. Managed via Proxmox at hive.proxmox.local.
```

---

## Component 2: Compaction Recovery Hook

### Hook

New method on `HolographicMemoryProvider`:

```python
def on_session_switch(
    self,
    new_session_id: str,
    *,
    parent_session_id: str = "",
    reason: str = "",
    **kwargs,
) -> None:
    if reason == "compression":
        self._post_compaction = True
        self._post_compaction_parent = parent_session_id
```

No store access — sets a flag only. Non-blocking by design.

Hermes fires `on_session_switch(reason="compression")` after context
compression completes, when the agent rotates to a new `session_id`. This hook
is the intended integration point per Clomp's notes on the ABC.

### Bootstrap Injection

`system_prompt_block()` is extended to check `self._post_compaction`. If set:

1. Query: `list_facts(category='infrastructure', min_trust=bootstrap_min_trust,
   limit=bootstrap_inject_limit)` — ordered by trust descending, so
   `bootstrap`-flagged facts float to the top.
2. Format as a **Bootstrap Context** block and prepend it before the normal
   memory status line.
3. Clear `self._post_compaction`.

Example output injected into system prompt:

```
## Bootstrap Context (Post-Compaction Recovery)

Context was just compressed. Key infrastructure facts re-injected:

- [0.9] rune-host: Rune runs on hive.local, a Proxmox VM — 8 cores, 32GB RAM
- [0.9] k8s-clusters: Three clusters — prod (romar), staging (hive), dev (local)
- [0.7] oncall-escalation: Page scromp for P0; Clomp handles scheduling/triage

# Holographic Memory
Active. 42 facts stored...
```

### Edge Cases

| Condition | Behavior |
|-----------|----------|
| Store not initialized when hook fires | Flag sets; `system_prompt_block()` returns empty bootstrap block |
| No infrastructure facts in store | Bootstrap block omitted; normal memory block shown |
| `system_prompt_block()` called multiple times | Flag cleared on first call; subsequent calls normal |
| Compression fires before first ingestor run | Bootstrap block omitted; no facts to inject |

---

## Configuration

New keys under `plugins.hermes-memory-store` in `~/.hermes/config.yaml`:

```yaml
plugins:
  hermes-memory-store:
    # existing keys unchanged
    okf_bundle_path: /shared/agents/common/infrastructure/
    bootstrap_inject_limit: 15      # max facts injected post-compaction
    bootstrap_min_trust: 0.7        # minimum trust to qualify for injection
    bootstrap_shadow: false         # true = log what would inject, don't inject
```

These are also surfaced in `get_config_schema()` for `/config`.

---

## Data Model Changes

One addition to the existing `FACT_STORE_SCHEMA` tool definition: add
`infrastructure` to the `category` enum alongside `user_pref`, `project`,
`tool`, `general`.

No schema changes to the `facts` table — `infrastructure` is a new value for
the existing `category` column.

---

## Rollout Plan

| Step | What | Notes |
|------|------|-------|
| 1 | Create `hermes-memory/` package structure | `okf_ingest.py`, updated plugin `__init__.py`, `okf_ingestion_state` schema migration |
| 2 | Add `infrastructure` to category enum | One-line change in `holographic/__init__.py` |
| 3 | Implement `OKFIngestor` + CLI | `--dry-run` first — verify parsing before writing |
| 4 | Seed the library | Rune + Clomp author initial OKF concepts, run ingestor, verify with `fact_store(action='list', category='infrastructure')` |
| 5 | Add compaction hook + bootstrap block to `HolographicMemoryProvider` | Set `bootstrap_shadow: true` first — log what *would* inject, don't inject yet |
| 6 | Trigger a manual compaction on Rune | Verify shadow log shows expected bootstrap block |
| 7 | Flip `bootstrap_shadow: false` | Rune runs in production with recovery enabled |

---

## Files to Create / Modify

### New Files

| File | Purpose |
|------|---------|
| `src/okf_ingest.py` | `OKFIngestor` class + `okf_ingestion_state` schema + CLI entry point (`python -m hermes_memory.okf_ingest`) |

### Modified Files

| File | What changes |
|------|-------------|
| `src/plugins/memory/holographic/__init__.py` | `on_session_switch`, `system_prompt_block` bootstrap block, `infrastructure` category, config keys |

### Unchanged

| File | Why |
|------|-----|
| `src/plugins/memory/holographic/store.py` | No schema changes needed |
| `src/plugins/memory/holographic/retrieval.py` | `list_facts` already supports category filter |
| `src/gateway/checkpoint_trigger.py` | Cross-channel path unchanged |

---

## Open Questions

None — all design questions resolved during review.
