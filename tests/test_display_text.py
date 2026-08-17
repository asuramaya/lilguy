"""to_display_text keeps the structure strip_html deliberately destroys."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scraper"))

from connectors.util import strip_html, to_display_text  # noqa: E402


def test_bullets_survive_as_bullets():
    html = "<ul><li>Ship code</li><li>Talk to users</li></ul>"
    out = to_display_text(html)
    assert "• Ship code" in out
    assert "• Talk to users" in out
    # ...and each on its own line, which is the entire point.
    assert out.count("\n") >= 1


def test_paragraphs_become_line_breaks():
    out = to_display_text("<p>First para.</p><p>Second para.</p>")
    assert "First para." in out and "Second para." in out
    assert "\n" in out
    assert "First para.Second para." not in out


def test_br_becomes_a_line_break():
    assert "\n" in to_display_text("Line one<br>Line two")


def test_headings_do_not_run_into_the_next_block():
    out = to_display_text("<h2>Responsibilities</h2><p>Do the thing.</p>")
    assert "ResponsibilitiesDo the thing." not in out
    assert "Responsibilities" in out and "Do the thing." in out


def test_double_escaped_entities_are_fully_unescaped():
    # Confirmed live on Greenhouse: content arrives double-escaped, so a
    # single unescape leaves literal "&nbsp;" visible to the reader.
    assert "&nbsp;" not in to_display_text("<p>A&amp;nbsp;B</p>")
    assert "&amp;" not in to_display_text("<p>Tom &amp;amp; Jerry</p>")


def test_output_is_text_not_markup():
    # We store text precisely so rendering it can never be an injection
    # vector -- no tag may survive.
    out = to_display_text('<p>Hi</p><script>alert("x")</script>')
    assert "<" not in out and ">" not in out


def test_excess_blank_lines_from_nested_blocks_are_collapsed():
    out = to_display_text("<div><div><p>A</p></div></div><div><p>B</p></div>")
    assert "\n\n\n" not in out


def test_horizontal_whitespace_collapses_but_newlines_do_not():
    out = to_display_text("<p>lots     of     space</p><p>next</p>")
    assert "lots of space" in out
    assert "\n" in out


def test_it_differs_from_strip_html_in_the_way_that_matters():
    html = "<ul><li>One</li><li>Two</li></ul>"
    # strip_html is for matching: flat, single-spaced, no structure.
    assert "\n" not in strip_html(html)
    # to_display_text is for reading.
    assert "\n" in to_display_text(html)


def test_long_input_is_bounded():
    assert len(to_display_text("<p>" + "x" * 100000 + "</p>")) <= 40000


def test_empty_and_none_are_safe():
    assert to_display_text("") == ""
    assert to_display_text(None) == ""
