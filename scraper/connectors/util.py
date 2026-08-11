import html
import re

_TAG_RE = re.compile(r"<[^>]+>")


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
    text = raw or ""
    for _ in range(3):
        unescaped = html.unescape(text)
        if unescaped == text:
            break
        text = unescaped
    text = _TAG_RE.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]
