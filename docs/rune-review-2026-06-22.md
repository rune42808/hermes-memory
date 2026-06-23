# Code Review: Compaction Recovery via OKF-Seeded Holographic Memory

**Reviewer:** Rune
**Date:** 2026-06-22
**Subject:** Review of the hermes-memory project — OKF ingestor, compaction hook, and bootstrap injection for post-compaction recovery.

---

## Design Assessment

**Verdict: Solid architecture, well-integrated.** The design uses the right integration point (`on_session_switch(reason="compression")`), leverages the existing holographic store (no new DB, no new schema), and the shadow-mode rollout path is a mature safety pattern. The split between "Clomp maintains the OKF bundle on NFS" and "the ingestor upserts into the fact store" is clean separation of concerns.

---

## Implementation Review

### Task 1-3: OKF Ingestor (`src/okf_ingest.py`)

**✅ `_StrTimestampLoader`** — Correctly prevents YAML from converting ISO timestamps to Python `datetime` objects, which would break the string-based comparison in `_ingest_file`. Good defensive catch.

**⚠️ Trust delta math (line 221-223)**
```python
trust_delta = concept.trust - self._store.default_trust
```
This assumes `default_trust` is 0.5. If a user changes `default_trust` in `config.yaml` (valid config key), bootstrap facts at 0.9 would end up at `0.9 - 0.3 = 0.6` or `0.9 - 0.7 = 0.2` instead of their intended 0.9. The ingestor should use a direct trust setter rather than computing a delta from a configurable baseline.

Suggested fix: add a `set_fact_trust(fact_id, trust)` method to `MemoryStore`, or adjust the existing `update_fact` to accept a `trust` parameter alongside `trust_delta`.

**✅ `_ingest_file` idempotency** — Timestamp-based diffing correctly distinguishes new, updated, and unchanged files. The `okf_ingestion_state` table stores per-file timestamps so re-ingestion of unchanged files is a no-op.

**⚠️ `__main__` CLI stubs (lines 259-306)** — ~48 lines of module stubs (`hermes_constants`, `hermes_state`, `hermes_cli`, `agent.memory_provider`, `tools.registry`) to make the CLI work standalone. The try/except fallback pattern correctly lets a real Hermes install take precedence, but these stubs are untested and could drift from the real Hermes module interfaces. Low risk since they only run when Hermes is NOT installed (CI, smoke tests), but worth noting.

### Task 4-5: Plugin Changes (`src/plugins/memory/holographic/__init__.py`)

**✅ `infrastructure` in category enum** — One-line change, correctly added alongside `user_pref`, `project`, `tool`, `general`. No schema migration needed since `category` is already a free-form text column in `facts` (the enum is only a tool-level constraint, not a DB constraint).

**✅ `on_session_switch` hook** — Correctly checks `kwargs.get("reason") == "compression"`, sets `_post_compaction = True`. Non-blocking by design — no store access in the hook path. Flag semantics: set on `on_session_switch`, consumed on first `system_prompt_block()` call.

**✅ `_build_bootstrap_block`** — Queries `list_facts(category="infrastructure", min_trust=0.7, limit=15)`, ordered by trust descending. Shadow mode logs at INFO without injecting. Empty store returns "" gracefully. Exceptions caught and logged.

**⚠️ `system_prompt_block` reaches into `_store._conn` directly (line 203)**
```python
total = self._store._conn.execute(
    "SELECT COUNT(*) FROM facts"
).fetchone()[0]
```
This accesses a private attribute of `MemoryStore`. If the store's internal connection management is refactored (context manager, connection pool, renamed attribute), this breaks silently. The count could live as a public `fact_count()` method on `MemoryStore` instead. Minor — low refactor risk.

### Test Coverage

| Test Class | Tests | Notes |
|---|---|---|
| `TestParseOkfConcept` | 12 | Full frontmatter, missing fields, fallbacks, SKIP files, category override |
| `TestOKFIngestor` | 8 | Add, update, skip unchanged, changed timestamp, dry-run, missing bundle, skip index/log |
| `TestCLI` | 2 | Dry-run prints summary, missing bundle exits nonzero |
| `TestInfrastructureCategory` | 2 | fact_store accepts `infrastructure`, category in schema enum |
| `TestConfigKeys` | 2 | Default values, keys in schema |
| `TestCompactionHook` | 3 | Compression sets flag, non-compression doesn't, no reason doesn't |
| `TestBootstrapInjection` | 7 | Block appears after compression, absent without, flag cleared once, empty store, shadow mode, limit respected, uninitialized store |

No critical gaps. Test isolation (tmp_path for DB, fixtures for Provider) is clean. No mocks of the MemoryStore — uses real SQLite — which validates the end-to-end path.

---

## Deployment Concerns

### Order matters

1. **Apply plugin diff** — update the active `__init__.py` on both diffuser and mink
2. **Add config keys** — `okf_bundle_path`, `bootstrap_shadow: true`, `bootstrap_inject_limit` in `plugins.hermes-memory-store` on both hosts
3. **Restart both gateways** — plugin changes take effect on next startup
4. **Author the OKF bundle** — Clomp creates markdown files in `/shared/agents/common/infrastructure/`
5. **Run the ingestor** — `python -m okf_ingest /shared/agents/common/infrastructure/ --dry-run` first, then without `--dry-run`
6. **Verify in shadow mode** — run a few days with `bootstrap_shadow: true`, check logs for the BOOTSTRAP:shadow entries
7. **Flip `bootstrap_shadow: false`** — enable live injection

### Cross-profile note

The config keys go under `plugins.hermes-memory-store`. If Rune and Clomp use different Hermes profiles, each profile's `config.yaml` needs the keys added independently. The OKF bundle is shared (NFS), the memory_store.db is per-profile.

### Symmetric fix rule

The plugin changes apply to both my active plugin and Clomp's. Testing on one host and assuming it works on the other is fine here — same Hermes version, same plugin code, same NFS.

---

## Edge Cases and Questions

**Q: What if NFS is down when the ingestor runs?**
A: The ingestor handles this gracefully — `bundle_root.exists()` returns False, returns `IngestResult` with an error entry. No crash. And the facts are already in the SQLite store from the last successful run, so compaction recovery (which reads from the store, not NFS) is unaffected.

**Q: Multiple compressions in quick succession?**
A: The `_post_compaction` flag is set on `on_session_switch` and cleared on the first `system_prompt_block()` call. If a second compression fires before the first `system_prompt_block()`, the second `on_session_switch` overwrites the flag — but this is correct, since the agent only cares about the latest session.

**Q: What if `list_facts` doesn't support `category` filtering?**
A: The design spec says retrieval.py already supports it, and the test suite exercises it against the real store. Verified.

**Q: Conflict with the existing cross-channel awareness system?**
A: No conflict. Compaction recovery injects `infrastructure` facts into the system prompt. Cross-channel awareness injects `checkpoint` facts (from other sessions) into the system prompt. They're additive — bootstrap block prepends, checkpoint block is part of prefetch. Both are in `system_prompt_block()`. The only interaction is token budget: if both fire simultaneously, the agent gets more context. This is fine — the bootstrap block is small (<500 chars for 15 facts).

---

## Summary

| Severity | Item | Action |
|---|---|---|
| **Important** | Trust delta math breaks if `default_trust` is customized | Fix before deploying — add `set_fact_trust()` or use a direct trust parameter in `update_fact()` |
| Minor | `system_prompt_block` accesses `_store._conn` directly | Consider adding a `fact_count()` public method to `MemoryStore` |
| Note | CLI stub code (~48 lines) is brittle | Acceptable — try/except guards, only runs when Hermes not installed |
| Note | `bootstrap_shadow` should be `true` for first deployment cycle | Already in the rollout plan |

**Recommended: Greenlight after the trust-delta fix.** Everything else is structurally sound. Clomp should start authoring the OKF bundle in parallel.
# Correction: Trust Delta Math

I was wrong in my initial review. The trust delta approach in `okf_ingest.py` lines 221-223 is **correct and robust**.

My concern was:
> "If a user changes `default_trust` in `config.yaml`, bootstrap facts at 0.9 would end up at 0.6 instead of 0.9."

This is not true because:

1. `add_fact()` creates the fact at `self.default_trust` (store.py line 169)
2. `trust_delta = target - self._store.default_trust`
3. `update_fact(fact_id, trust_delta=X)` applies `current_trust + X`

Since the fact was just created at `default_trust`, the math is:
`default_trust + (target - default_trust) = target`

No matter what `default_trust` is configured to (0.3, 0.5, 0.7), the bootstrap facts always land at exactly 0.9 and standard facts at 0.7.

**Verdict: No code change needed. The implementation is sound.**

— Rune, 2026-06-22
