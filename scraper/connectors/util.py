import html
import re

_TAG_RE = re.compile(r"<[^>]+>")

# Tags that end a visual block. Job descriptions are almost entirely
# headings, paragraphs and bullet lists, so these carry nearly all of the
# structure a reader needs.
_BLOCK_END_RE = re.compile(
    r"</(?:p|div|li|ul|ol|h[1-6]|tr|table|section|article|blockquote)\s*>|<br\s*/?>",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"<li\b[^>]*>", re.IGNORECASE)


def _unescape_fully(raw: str) -> str:
    """Some ATS content fields (confirmed on Greenhouse) store the HTML
    DOUBLE-escaped, e.g. the literal "&amp;nbsp;" rather than "&nbsp;" or
    a real space, so a single html.unescape() peels one layer and leaves
    visible entity text behind. Loop until it stabilizes rather than
    guessing which encoding a given source used."""
    text = raw or ""
    for _ in range(3):
        unescaped = html.unescape(text)
        if unescaped == text:
            return text
        text = unescaped
    return text


def strip_html(raw: str, max_len: int = 600) -> str:
    """Minimal HTML-to-text: good enough for keyword search, not for display.

    Unescape first, then strip tags — some ATS content fields (confirmed on
    Greenhouse) store the HTML DOUBLE-escaped, e.g. the literal string
    "&amp;nbsp;" rather than "&nbsp;" or a real space, so a single
    html.unescape() only peels off one layer and leaves visible entity
    text behind. Unescaping in a loop until it stabilizes handles both
    single- and double-escaped input without guessing which one a given
    source uses.
    """
    text = _unescape_fully(raw)
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def to_display_text(raw: str, max_len: int = 40000) -> str:
    """HTML to text for READING, unlike strip_html which is for matching.

    strip_html collapses every run of whitespace into a single space and
    truncates at 600 characters -- correct for keyword search, useless
    for display: a job description is almost entirely headings and bullet
    lists, and flattening it produces one unreadable wall of text.

    This keeps the block structure as plain-text line breaks and bullets,
    so the frontend can render it with `white-space: pre-wrap` and no
    sanitizer. Deliberately emits TEXT, not HTML: these strings come from
    third-party job boards, and storing markup we later inject would mean
    owning an XSS surface and an allowlist forever. Text costs some
    fidelity (no links, no bold) and buys immunity.

    max_len is a sanity bound, not a display budget -- real descriptions
    run a few thousand characters; 40k is far past the longest plausible
    one and exists so a pathological page can't put a megabyte in a row.
    """
    text = _unescape_fully(raw)
    # Mark list items before tags are stripped, so bullets survive.
    text = _LIST_ITEM_RE.sub("\n• ", text)
    text = _BLOCK_END_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    # Collapse horizontal whitespace only -- newlines are the structure
    # this function exists to preserve.
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    # Three or more blank lines is always noise from nested block tags.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()[:max_len]
