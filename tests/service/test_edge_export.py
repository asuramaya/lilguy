"""build_trends() feeds dist/data/trends.json in the Cloudflare Pages
export. Verified live against production (2026-09-01): a real deploy once
shipped an empty-but-well-formed {"weekly": [], "top_movers": []} to
lilguy.win/data/trends.json after a transient DB hiccup during
build_trends()'s query -- the same query worked fine against the same
database moments later, so the bug wasn't the SQL, it was treating
"couldn't query" and "genuinely nothing happened" as the same shape. A
client can't tell those apart, so a failed query must not produce a
publishable trends.json at all.
"""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "service"))

from edge_export import build_trends  # noqa: E402


def test_a_failed_trends_query_returns_none_not_an_empty_stand_in():
    with patch("edge_export.cursor", side_effect=RuntimeError("connection refused")):
        assert build_trends() is None


def test_prune_removes_shards_for_closed_postings_and_keeps_live_ones(tmp_path):
    # Regression: shards were only ever written, never removed, so the
    # directory ratcheted past Cloudflare Pages' 20,000-file ceiling
    # (22,015 shards vs 11,018 open postings) and every publish failed.
    from edge_export import prune_description_shards

    (tmp_path / "live1.json").write_text("{}")
    (tmp_path / "live2.json").write_text("{}")
    (tmp_path / "closed1.json").write_text("{}")
    (tmp_path / "closed2.json").write_text("{}")
    (tmp_path / "notes.txt").write_text("not a shard")

    pruned = prune_description_shards(tmp_path, {"live1", "live2"})

    assert pruned == 2
    assert sorted(p.name for p in tmp_path.iterdir()) == ["live1.json", "live2.json", "notes.txt"]
