"""The boundary is the design: a platform's structured field and an
explicit location string are both the employer SAYING it. Description
text is not, and is deliberately never consulted.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "service"))

from work_arrangement import from_location, normalise  # noqa: E402


@pytest.mark.parametrize("location, expected", [
    ("Remote", "remote"),
    ("Remote - United States", "remote"),
    ("Work From Home", "remote"),
    ("Hybrid - Chicago, IL", "hybrid"),
    # BOTH words present: hybrid is the more specific claim and must win,
    # or a hybrid role is flattened into a remote one.
    ("Hybrid - Remote/Chicago", "hybrid"),
    ("On-site, Boston", "onsite"),
    ("In Person - NYC", "onsite"),
    ("Austin, TX", ""),
    ("", ""),
    (None, ""),
])
def test_location_strings_that_state_an_arrangement(location, expected):
    assert from_location(location) == expected


def test_a_place_name_containing_a_keyword_is_not_a_false_positive():
    # Anchored to whole words. There are real places called Remote.
    assert from_location("Remoteville, TX") == ""
    assert from_location("Hybridge Park, NJ") == ""


@pytest.mark.parametrize("value, expected", [
    ("Hybrid", "hybrid"),
    ("Remote", "remote"),
    ("OnSite", "onsite"),
    ("on-site", "onsite"),
    ("In Office", "onsite"),
    ("fully_remote", "remote"),
    # An unknown value becomes blank rather than passing through: a
    # platform inventing a fourth word must not quietly add a category
    # no filter offers.
    ("Flexible", ""),
    ("", ""),
    (None, ""),
])
def test_platform_vocabularies_are_mapped_or_dropped(value, expected):
    assert normalise(value) == expected
