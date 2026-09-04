"""Structural check: the board reflows at 390 px instead of scrolling sideways.

Not a pixel test. The board is agenttrail's canvas, which does not reflow —
so patch 6 hides it below 600 px and promotes the escalation stack to a
full-width queue with the session cards under it. These assert the
structural properties that make that true, and that the PWA shell the
deleted cockpit used to carry now rides on the board page itself.
"""

import re

from boardd.config import PKG_DIR

BOARD = (PKG_DIR / "vendor" / "agenttrail" / "public" / "index.html").read_text()
MOBILE_RULES = re.search(r"@media\(max-width:600px\)\{(.*?)\}\n", BOARD, re.S)


def test_viewport_meta():
    assert 'content="width=device-width, initial-scale=1"' in BOARD


def test_narrow_screens_get_their_own_media_query():
    assert MOBILE_RULES, "no max-width:600px media query on the board page"


def test_the_canvas_and_file_tree_step_aside():
    body = MOBILE_RULES.group(1)
    for selector in (".sidebar", ".graph-view", ".runs", ".minimap", ".canvas-tools"):
        assert selector in body, f"{selector} still shown at 390px"
    assert "display:none!important" in body


def test_the_escalation_stack_becomes_the_full_width_queue():
    body = MOBILE_RULES.group(1)
    stack = re.search(r"\.esc-stack\{([^}]*)\}", body).group(1)
    assert "position:static" in stack  # out of the floating right edge
    assert "max-width:none" in stack
    panel = re.search(r"\.esc-panel\{([^}]*)\}", body).group(1)
    assert "width:100vw" in panel


def test_the_m_override_matches_the_media_query():
    """`?m=1` must reflow a wide window the same way, or the mobile view
    cannot be screenshotted or debugged from a desktop browser."""
    assert "body.m .esc-stack{position:static" in BOARD
    assert "get('m')==='1'" in BOARD


def test_session_cards_keep_the_canvas_card_anatomy():
    """Same three lines the canvas node draws: kicker, title, n-of-m tasks."""
    renderer = re.search(r"function renderMobileCards\(\)\{(.*?)\n(?=function )", BOARD, re.S).group(1)
    assert "m-head" in renderer and "m-title" in renderer
    assert "of ${list.length} tasks" in renderer
    assert "statusLabel(p.status)" in renderer


def test_installable_shell_rides_on_the_board_page():
    assert '<link rel="manifest" href="/static/manifest.webmanifest">' in BOARD
    assert "navigator.serviceWorker.register('/static/sw.js')" in BOARD


def test_the_right_hand_board_is_hideable():
    """Trey's L540 ask. The Runs overlay button is the toggle, and run
    cards start hidden — verified here so a re-vendor cannot drop it."""
    assert "const overlays={activity:true,runs:false}" in BOARD
    assert "id=\"ov-runs\"" in BOARD and "toggleOverlay('runs')" in BOARD
    assert "body.hide-runs .runs{display:none!important}" in BOARD
