"""Root-glob half of monitor discovery: no systemd needed, just a temp dir tree."""

from boardd.discovery import ROOT_PREFIX, discover_state_roots


def test_discover_state_roots_from_temp_dir_tree(tmp_path):
    bare = tmp_path / ROOT_PREFIX
    bare.mkdir()
    (bare / "goals.json").write_text("{}")

    boomtown = tmp_path / f"{ROOT_PREFIX}-boomtown"
    boomtown.mkdir()
    (boomtown / "goals.json").write_text("{}")

    # A root directory that exists but has never had a goals.json written —
    # a unit that started but hasn't produced state yet. Not a monitor.
    empty = tmp_path / f"{ROOT_PREFIX}-empty"
    empty.mkdir()

    # A same-prefixed but unrelated directory (no "-" boundary) must not
    # match, even with a goals.json inside it.
    unrelated = tmp_path / f"{ROOT_PREFIX}extra-thing"
    unrelated.mkdir()
    (unrelated / "goals.json").write_text("{}")

    found = discover_state_roots(tmp_path)
    assert found == {"monitor": bare, "boomtown": boomtown}


def test_discover_state_roots_missing_base_is_empty(tmp_path):
    assert discover_state_roots(tmp_path / "does-not-exist") == {}
