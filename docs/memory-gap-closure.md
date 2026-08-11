# Memory Gap Closure Plan

**Type:** Draft
**Status:** Needs Review (scromp)
**Authors:** Rune & Clomp (co-authored)
**Trigger:** Cross-review of dshnayder/kube-agents Hindsight proposal (PR #634) + mastersingh24 review
**Date:** 2026-08-11

## Context

Three independent sources converged on the same gap: our memory subsystem has zero measurement and two known interface deficiencies.

1. **dshnayder/kube-agents Hindsight proposal** — a retrieval-backed memory design with per-rung A/B metrics (gold recall, contamination, ranking, context tokens). They measured theirs against a synthetic 1,664-record corpus. We have no equivalent data.

2. **mastersingh24 PR #634 review** — called out the proposal's own benchmark as insufficient (compared only to flat-file, not Honcho) and flagged the 882-line multi-tenancy wrapper as self-inflicted complexity. Reinforced that measurement without honest baselines is marketing.

3. **Our own fact_store** — `retrieval_count` is always 0 (the field exists in the schema but the hybrid retrieval path never increments it). We can't answer "how often is a fact retrieved?" or "which facts are dead weight?"

Additionally, two interface gaps were identified:

4. **"Read names its outcome"** — the Hindsight proposal identified a failure mode where models silently convert "not in what I retrieved" into "not recorded anywhere." Our fact_store has the same deficiency: empty results carry no trace of what was searched.

5. **Skill content staleness** — the Curator detects time-based staleness (idle hours) but not content staleness. A skill loaded daily that references a tool no longer installed goes undetected.

---

## Scope

This plan addresses the **measurement, interface, and staleness gaps** in our homelab memory stack. It does NOT address:

- Multi-tenant / per-user scoping (single-human homelab)
- Semantic dedup (deferred until facts cross 500)
- Hindsight-style LLM consolidation (their own experiment caused identifier collapse: 162→50 distinct IDs in 3 cycles)
- Token budget mechanisms (the `limit` parameter already exists; 10 is correct for our scale)

---

## Plan

### 0. Rename the plugin

**Issue:** The plugin directory is `holographic/` in both `~/.hermes/hermes-agent/plugins/memory/` and the source repo at `sackheads/hermes-memory`. The retrieval engine is FTS5 + Jaccard + optional HRR — "holographic" is misleading. Tracked as [romar#43](https://github.com/sackheads/romar/issues/43).

**Fix:** `s/holographic/hermes-memory/` in the plugin directory and `__init__.py` import paths. No behavior change.

**Effort:** 5 minutes.

---

### 1. "Read names its outcome" — include query in tool responses

**What:** When fact_store returns empty results, the response should say what was searched. This prevents the model from silently converting "not in my retrieval results" into "not recorded anywhere."

**Where:** `__init__.py` handler — `_handle_fact_store()`. All retrieval actions (search, probe, related, reason) return `{"results": [...], "count": N}` without naming what was searched.

**Fix:** Add the input parameters to every response body:

```python
# search
json.dumps({"results": results, "count": len(results), "searched": args["query"]})

# probe
json.dumps({"results": results, "count": len(results), "probed": args["entity"]})

# related
json.dumps({"results": results, "count": len(results), "related_to": args["entity"]})

# reason
json.dumps({"results": results, "count": len(results), "reasoning_from": args["entities"]})
```

When results are empty, the model sees `{"results": [], "count": 0, "searched": "nats config"}` instead of `{"results": [], "count": 0}`.

**Effort:** 10 minutes. Four lines changed.

---

### 2. Fix `retrieval_count` tracking

**Bug:** The hybrid retrieval path in `retrieval.py` — `FactRetriever.search()`, `probe()`, `related()`, `reason()` — returns candidates but never increments `retrieval_count`. Only `store.search_facts()` (store.py:231) increments it, and nothing calls that path from the plugin tool handler. Every fact shows `retrieval_count: 0` regardless of actual usage.

**Fix:** Add a post-query increment to each retrieval method, mirroring the existing pattern in `store.search_facts()`:

```python
if results:
    ids = [r["fact_id"] for r in results]
    placeholders = ",".join("?" * len(ids))
    self.store._conn.execute(
        f"UPDATE facts SET retrieval_count = retrieval_count + 1 WHERE fact_id IN ({placeholders})",
        ids,
    )
    self.store._conn.commit()
```

**Why this is a dependency:** The inventory script (Phase 3) depends on accurate `retrieval_count` to identify cold facts. Without this fix, every fact appears dead regardless of actual retrieval frequency.

**Effort:** 15 minutes.

---

### 3. Fact store inventory script (weekly cron)

**What:** A `no_agent` cron job that queries `memory_store.db` directly and emits a human-readable weekly digest. No LLM — pure data dump.

**Path:** `~/.hermes/scripts/fact-store-inventory.py`

**Metrics emitted:**

| Metric | Why |
|---|---|
| Total facts, per category, per trust tier | Baseline. Are we growing? |
| `retrieval_count = 0` facts | Dead-fact detection — candidates for removal |
| Trust score distribution | Are we accumulating 0.5-trust drift? |
| Fact age percentiles (p50, p90, max) | Is the store aging or refreshing? |
| Facts added this week vs. last week | Growth rate |
| `⚠ dedup threshold: N/500 facts` | When to revisit the semantic dedup decision |

**Cron schedule:** Weekly, Sunday 9am EST, delivered to origin:

```
hermes cron create \
  --schedule "0 9 * * 0" \
  --name "fact-store-inventory" \
  --no-agent \
  --script ~/.hermes/scripts/fact-store-inventory.py
```

**DB access:** Read-only. Opens `~/.hermes/memory_store.db`, runs COUNT/GROUP BY queries, formats output.

**Effort:** 30 minutes.

**Dependency:** Phase 2 must land first (`retrieval_count` must be accurate).

---

### 4. Spool pipeline sanity check (free with #3)

**What:** The NATS → spool → ingest pipeline (`nats-listener.py` → `memory_spool.jsonl` → `ingest-memory-spool` hook → `memory_store.db`) is invisible. A companion check added to the same inventory script:

- Count spool lines (`wc -l ~/.hermes/memory_spool.jsonl`)
- Count DB rows
- Flag if spool has lines older than N hours (indicates the ingest hook isn't running)

**Deliverable:** Added to the weekly digest script for zero marginal effort.

---

### 5. Skill content staleness (enhance the Curator)

**What's already good:** The Curator tracks `use_count`, `view_count`, `patch_count`, `last_used_at` per skill. Auto-transitions to `stale` based on `min_idle_hours`. Archives dead skills. Latest run: 4 marked stale, 3 reactivated, 100 checked.

**What's missing:** Time-based staleness ≠ content staleness. A skill loaded daily whose `required_commands` reference a tool no longer installed (e.g., `helm` after a migration) is time-active but content-dead. The `required_commands` and `required_environment_variables` fields already exist in skill frontmatter but are never verified.

**Fix:** Add an optional `--content-check` pass to the Curator's auto-transition phase. For each active skill:

1. Parse YAML frontmatter for `required_commands`
2. Check `which <cmd>` on the host
3. Flag skill as `content-stale` if any command is missing

**Output:** New line in Curator REPORT.md:

```
- content-stale (commands missing): 2 (microk8s-janitor: helm not found, garage-janitor: garage not found)
```

**Safety:** Content-stale is a separate flag from time-stale. Content-stale skills are flagged for review but not auto-archived (too aggressive). The Curator continues to handle time-based staleness independently.

**Effort:** ~1 hour.

---

## Execution order

| # | Phase | Value | Effort | Dependency |
|---|---|---|---|---|
| 0 | Rename plugin | Low (correctness) | 5min | None |
| 1 | Read names its outcome | Medium (correctness) | 10min | None |
| 2 | Fix retrieval_count | **High** (enables #3) | 15min | None |
| 3 | Inventory script + cron | **High** (visibility) | 30min | #2 (needs accurate counts) |
| 4 | Spool sanity check | Low (monitoring) | Free | #3 |
| 5 | Curator content checks | Medium (staleness) | ~1h | Curator infra |

**Total:** ~2 hours. Phases 0–2 are code changes in the hermes-memory plugin and should be one PR. Phase 3 is a standalone script. Phase 5 is a Curator enhancement.

---

## What stays deferred

- **Semantic dedup** — hash-based dedup sufficient below 500 facts. Inventory script (#3) will flag when threshold approaches.
- **Hindsight-style LLM consolidation** — their own experiment caused identifier collapse (162→50 distinct IDs in 3 cycles). We're not replicating that failure.
- **Multi-tenant scoping** — single-human homelab. Not needed.
- **Recall budget / `max_tokens`** — `limit` parameter already exists; 10 is correct for our scale.
- **Out-of-band identifiers** — clean architecturally but no practical benefit at 120 facts with two agents.

---

## Open questions

1. Should `retrieval_count` track retrievals per fact as a histogram (how many facts get 1 retrieval vs. 10 vs. 100), or is a simple counter sufficient?

2. Should the inventory script also detect fact contradictions (two facts about the same entity making conflicting claims)? The `contradict` action already exists in the retriever — we'd just need to run it on the weekly digest and flag high-scoring contradictions.

3. Where exactly does the fact_store tool handler live at runtime? The plugin source is at `/shared/agents/common/projects/active/hermes-memory/src/plugins/memory/holographic/` but the copy in `~/.hermes/` may be at a different path. Need to verify before implementing #1 and #2.

---

## References

- [dshnayder/kube-agents PR #634](https://github.com/gke-labs/kube-agents/pull/634) — Hindsight memory proposal
- [mastersingh24 review](https://github.com/gke-labs/kube-agents/pull/634#pullrequestreview-4908432202) — PR review calling for comparative benchmark
- [romar#43](https://github.com/sackheads/romar/issues/43) — Plugin naming issue
- Plugin source: `/shared/agents/common/projects/active/hermes-memory/src/plugins/memory/holographic/`
- Plugin runtime copy: `~/.hermes/hermes-agent/plugins/memory/holographic/`
