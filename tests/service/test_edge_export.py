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
