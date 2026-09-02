"""filters.yaml (this fork's default, "Supply Chain") is hand-tuned data,
not code -- but its exclude_keywords earn their keep by catching a real,
measured bug: verified live against the full postings store that several
horizontal tech companies' identical "About Us" boilerplate name-drops
"supply chain"/"transportation" as one of many industries served,
wrongly pulling their entirely-unrelated Software Engineering internships
into this feed (43 of 693 matches, 6.2%, overlapping the
software-engineering preset -- nearly all from 3 companies repeating one
boilerplate sentence each). These are regression tests for that fix, not
tests of user_filter.py's matching mechanism itself (see
tests/test_user_filter.py for that).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from user_filter import load_filter, passes  # noqa: E402

ROOT = Path(__file__).parent.parent
SPEC = load_filter(str(ROOT / "filters.yaml"))


def posting(title="", description_snippet=""):
    return {"title": title, "description_snippet": description_snippet, "company": "", "location": ""}


def test_palantir_boilerplate_does_not_pass_as_supply_chain():
    p = posting(
        title="Software Engineer, Internship",
        description_snippet=(
            "Palantir builds the world's leading software for data-driven decisions and "
            "operations. By bringing the right data to the people who need it, our platforms "
            "empower our partners to develop lifesaving drugs, forecast supply chain "
            "disruptions, locate missing children."
        ),
    )
    assert not passes(p, SPEC)


def test_databricks_boilerplate_does_not_pass_as_supply_chain():
    p = posting(
        title="Software Engineering Intern",
        description_snippet=(
            "At Databricks, we are passionate about helping data teams solve the world's "
            "toughest problems -- from making the next mode of transportation a reality to "
            "accelerating the development of medical breakthroughs."
        ),
    )
    assert not passes(p, SPEC)


def test_samsara_boilerplate_does_not_pass_as_supply_chain():
    p = posting(
        title="Software Engineering Internship",
        description_snippet=(
            "Representing more than 40% of global GDP, these industries are the "
            "infrastructure of our planet, including agriculture, construction, field "
            "services, transportation, and manufacturing."
        ),
    )
    assert not passes(p, SPEC)


def test_a_real_supply_chain_posting_still_passes():
    # The exclude fix is phrase-level, not word-level -- a genuine
    # logistics posting that happens to use "supply chain" or
    # "transportation" outside one of the three exact boilerplate
    # sentences must still pass.
    assert passes(posting(title="Supply Chain Analyst Intern"), SPEC)
    assert passes(
        posting(title="Intern", description_snippet="Join our transportation planning team"),
        SPEC,
    )
