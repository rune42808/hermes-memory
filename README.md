# hermes-memory-store

A [Hermes](https://hermes-agent.nousresearch.com) memory plugin that gives agents a structured, queryable fact store backed by SQLite and [Holographic Reduced Representations](https://en.wikipedia.org/wiki/Holographic_reduced_representation) (HRR). Built to solve one specific problem: agents losing coherent working knowledge after context compaction.

## The Problem

Long-running Hermes agents (especially infra/SRE workloads) can hit context compression mid-session and effectively lose their mind — forgetting what host they run on, what clusters exist, what was decided an hour ago. Skills and markdown files help but require the agent to re-read them proactively. This plugin makes important facts *push* rather than *pull*.

## How It Works

The plugin registers as a `MemoryProvider` and exposes two tools to the agent:

- **`fact_store`** — CRUD + algebraic retrieval over a structured fact store
- **`fact_feedback`** — rate a fact as helpful/unhelpful, which adjusts its trust score

Facts are stored in SQLite with:
- Full-text search (FTS5) for keyword retrieval
- Entity extraction and resolution (facts are linked to the entities they mention)
- HRR vectors per fact (requires `numpy`) enabling algebraic queries
- Trust scores that drift up/down based on feedback

On every turn, the plugin's `prefetch()` runs a hybrid retrieval (FTS5 + Jaccard + HRR cosine similarity) against the user's message and injects relevant facts into the context block.

### Compaction Recovery

When Hermes compresses context, the plugin detects the `compression` session-switch event and — at the start of the next system prompt — re-injects the top-N high-trust `infrastructure` category facts. This bootstraps the agent back to knowing where it lives and what it manages without manual intervention.

### Cross-Channel Awareness

Facts tagged with `source_session:` are treated as latent context from other concurrent sessions. These surface in `prefetch()` under a separate "Other Active Conversations" block and are suppressed if the current venue is more public than the session they came from.

## Installation

Drop the `src/plugins/memory/holographic/` directory into your Hermes plugins path and register it in `config.yaml`:

```yaml
plugins:
  hermes-memory-store:
    db_path: $HERMES_HOME/memory_store.db
    auto_extract: false
    default_trust: 0.5
    min_trust_threshold: 0.3
    hrr_dim: 1024
    bootstrap_inject_limit: 15
    bootstrap_min_trust: 0.7
    bootstrap_shadow: false
    okf_bundle_path: /shared/agents/common/infrastructure/
```

`numpy` is optional. Without it, HRR operations fall back to FTS5+Jaccard retrieval and `probe`/`related`/`reason`/`contradict` degrade to keyword search. Install it if you want the full algebraic layer.

## The `fact_store` Tool

| Action | Purpose |
|---|---|
| `add` | Store a fact. Requires `content`. Optional `category`, `tags`. |
| `search` | Keyword lookup across content and tags. |
| `probe` | All facts about a specific entity (algebraic, not keyword). |
| `related` | Facts structurally connected to an entity. |
| `reason` | Facts connected to **all** of a list of entities simultaneously — compositional AND query. |
| `contradict` | Find fact pairs making conflicting claims about the same entities. |
| `update` | Modify content, tags, category, or adjust trust by delta. |
| `remove` | Delete a fact by ID. |
| `list` | Browse facts by category/trust, sorted by trust descending. |

Categories: `user_pref`, `project`, `tool`, `general`, `infrastructure`.

The distinction between `search` (keyword), `probe` (entity recall), and `reason` (multi-entity intersection) matters. For example:

```
# Who is peppi and what does he do on the backend?
fact_store(action="reason", entities=["peppi", "backend"])

# What do we know about the rune host?
fact_store(action="probe", entity="rune")

# Find anything about deploy processes
fact_store(action="search", query="deploy process")
```

## OKF Ingestion

[OKF bundles](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers) are directories of YAML-frontmatter markdown files describing infrastructure concepts (hosts, clusters, services, etc.). The `okf_ingest.py` script walks a bundle and upserts its facts into the store.

```bash
python -m src.okf_ingest /shared/agents/common/infrastructure/

# dry run
python -m src.okf_ingest /shared/agents/common/infrastructure/ --dry-run --verbose

# explicit db path
python -m src.okf_ingest /path/to/bundle --db /path/to/memory_store.db
```

Ingestion is idempotent — files are tracked by path+timestamp and skipped if unchanged. Files marked `bootstrap: true` in frontmatter get trust 0.9 (vs 0.7 for standard), making them first in line for compaction recovery injection.

## HRR Internals

Phase vectors represent concepts as angles in [0, 2π). The algebra:

- **bind** (circular convolution) — associates two concepts; result is quasi-orthogonal to both
- **unbind** (circular correlation) — retrieves one concept given the other
- **bundle** (circular mean) — merges multiple concepts; result is similar to each

Each fact is encoded as:
```
bind(encode_text(content), ROLE_CONTENT) + Σ bind(encode_atom(entity), ROLE_ENTITY)
```

This enables `probe` to ask "unbind ROLE_ENTITY×entity from the memory bank — what content comes out?" without any keyword matching. Atoms are generated deterministically from SHA-256, so vectors are stable across processes and machines.

Memory bank SNR degrades as `sqrt(dim / n_facts)` — below SNR 2.0 (n_facts > dim/4), the plugin logs a warning. Default dim=1024 handles ~256 facts cleanly per category bank.

## Configuration Reference

| Key | Default | Description |
|---|---|---|
| `db_path` | `$HERMES_HOME/memory_store.db` | SQLite path. Supports `$HERMES_HOME` and `~` expansion. |
| `auto_extract` | `false` | Auto-extract facts from conversation at session end using regex patterns. |
| `default_trust` | `0.5` | Starting trust score for new facts. |
| `min_trust_threshold` | `0.3` | Prefetch ignores facts below this score. |
| `hrr_dim` | `1024` | HRR vector dimensions. Higher = more capacity, more memory. |
| `okf_bundle_path` | `/shared/agents/common/infrastructure/` | Default bundle for OKF ingestion. |
| `bootstrap_inject_limit` | `15` | Max facts re-injected after compaction. |
| `bootstrap_min_trust` | `0.7` | Minimum trust for compaction re-injection. |
| `bootstrap_shadow` | `false` | Log what would inject without actually injecting. Useful for tuning. |
