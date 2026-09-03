"""Structural check: the boardd page cannot scroll horizontally at 390 px.

Not a pixel test. We assert the structural properties that make horizontal
overflow impossible in this codebase:
- the page declares a proper viewport meta;
- body forbids horizontal overflow and nothing widens it back;
- no fixed pixel width anywhere in the CSS exceeds 390 px;
- wide content (the raw-source mono block) scrolls inside its own container;
- the lane grid and the drawer collapse for narrow screens.
"""

import re

from boardd.config import PKG_DIR

STATIC = PKG_DIR / "static"
CSS = (STATIC / "style.css").read_text()
HTML = (STATIC / "index.html").read_text()
JS = (STATIC / "app.js").read_text()


def test_viewport_meta():
    assert 'content="width=device-width, initial-scale=1"' in HTML


def test_body_forbids_horizontal_scroll():
    body_rule = re.search(r"\nbody \{(.*?)\}", CSS, re.S).group(1)
    assert "overflow-x: hidden" in body_rule


def test_no_fixed_width_wider_than_390():
    # \b alone is not enough: 'max-width' would still match 'width'.
    for prop, value in re.findall(r"(?<![a-z-])(width|min-width)\s*:\s*([^;)]+);", CSS):
        for px in re.findall(r"(\d+(?:\.\d+)?)px", value):
            assert float(px) <= 390, f"{prop}: {value} exceeds 390px"


def test_wide_content_scrolls_in_its_own_container():
    raw_rule = re.search(r"\.rawbox \.raw \{(.*?)\}", CSS, re.S).group(1)
    assert "overflow-x: auto" in raw_rule


def test_lane_grid_collapses_to_one_column():
    m = re.search(r"@media \(max-width: 720px\) \{ \.lanegrid \{(.*?)\}", CSS, re.S)
    assert m and "1fr" in m.group(1) and "1fr 1fr" not in m.group(1)


def test_drawer_fits_narrow_screens():
    drawer_rule = re.search(r"\n\.drawer \{(.*?)\}", CSS, re.S).group(1)
    assert "min(33rem, 100%)" in drawer_rule


def test_no_inline_pixel_widths_in_markup_or_js():
    for text, name in ((HTML, "index.html"), (JS, "app.js")):
        assert not re.search(r"width\s*:\s*\d{3,}px", text), f"wide inline width in {name}"


def test_lane_writes_target_the_items_own_monitor():
    """The "all" combined view tags every lane/needs-you item with its own
    monitor id (app.py's _combined_view); every ack/answer call must send
    it, or a write silently targets the default monitor's lane instead."""
    calls = re.findall(r"postLaneAction\(item\.lane_id,[^;]*?\)(?=[;,])", JS)
    assert len(calls) == 5
    for call in calls:
        assert "item.monitor" in call, call
