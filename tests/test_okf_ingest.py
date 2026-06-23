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
