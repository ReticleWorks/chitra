"""Monitor discovery: root-glob half (temp dir tree) and unit half (fake systemctl)."""

import subprocess

from boardd.discovery import ROOT_PREFIX, discover_state_roots, discover_units


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


def test_discover_units_matches_deployed_names(monkeypatch):
    """UNIT_TEMPLATES must match the real fleet unit names in
    packaging/systemd/ownership.json (polyphony-chitra-<role>@), not a bare
    <role>@ pattern that never appears on a deployed host."""
    fake_stdout = (
        "polyphony-chitra-watchd@folio.service     loaded active   running Chitra watchd (folio)\n"
        "polyphony-chitra-triaged@folio.service    loaded active   running Chitra triaged (folio)\n"
        "polyphony-chitra-dispatchd@folio.service  loaded inactive dead    Chitra dispatchd (folio)\n"
        "polyphony-chitra-sweepd@boomtown.service  loaded active   running Chitra sweepd (boomtown)\n"
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    found = discover_units()
    assert found == {"folio": "active", "boomtown": "active"}


def test_discover_units_reads_the_active_column_not_the_line(monkeypatch):
    """A failed unit must be reported failed, and a unit whose *name*
    contains "failed" must not be.

    systemd prefixes a status glyph to any row that is not plain
    loaded+active, and `--plain` does not strip it. The old pattern anchored
    the unit name at `^`, so the bulleted row below matched nothing and the
    failed instance disappeared from the picker. A looser fix — searching the
    line for "failed" — would then mis-report `@failed-lane`, which is an
    ordinary running lane.
    """
    fake_stdout = (
        "● polyphony-chitra-watchd@folio.service        loaded failed     failed      Chitra watchd (folio)\n"
        "polyphony-chitra-watchd@failed-lane.service    loaded active     running     Chitra watchd (failed-lane)\n"
        "polyphony-chitra-sweepd@boomtown.service       loaded activating auto-restart Chitra sweepd (boomtown)\n"
        "polyphony-chitra-triaged@ghost.service         not-found inactive dead        Chitra triaged (ghost)\n"
        "Loaded units listed. Pass --all to see loaded but inactive units, too.\n"
    )

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=fake_stdout, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert discover_units() == {
        "folio": "failed",
        "failed-lane": "active",
        "boomtown": "activating",
        "ghost": "inactive",
    }
