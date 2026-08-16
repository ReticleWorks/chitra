"""Tests for token-free per-host usage exports and the fleet-wide read."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import chitra.usage_export as usage_export
from chitra.policy_config import UsagePolicy
from chitra.usage import CodexSnapshotError, UsageSnapshot, UsageWindow, main
from chitra.usage_export import (
    EXPORT_SCHEMA,
    UsageExport,
    build_claude_export,
    build_codex_export,
    policy_revision,
    prune_history,
    read_fleet_exports,
    write_export,
)

NOW = datetime(2026, 8, 16, 13, 0, tzinfo=UTC)
POLICY = UsagePolicy()


def _claude_snapshot(
    *,
    five_hour_pct: float,
    seven_day_pct: float,
    ts: str = "2026-08-16T12:58:00+00:00",
    session_id: str = "lane-1",
    account: str = "agent@example.com",
) -> UsageSnapshot:
    return UsageSnapshot(
        kind="claude",
        ts=ts,
        session_id=session_id,
        tmux_session="fleet-1",
        five_hour=UsageWindow(pct=five_hour_pct, resets_at=1_755_600_000),
        seven_day=UsageWindow(pct=seven_day_pct, resets_at=1_755_660_000),
        account=account,
    )


def _write_snapshot(directory: Path, snapshot: UsageSnapshot) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{snapshot.session_id}.json").write_text(json.dumps(snapshot.to_dict()), encoding="utf-8")


def _export(
    *,
    host: str = "tophand",
    backend: str = "codex",
    verdict: str = "ok",
    captured_at: str = "2026-08-16T12:55:00+00:00",
) -> UsageExport:
    return UsageExport(
        host=host,
        backend="codex" if backend == "codex" else "claude",
        account="agent@example.com",
        captured_at=captured_at,
        reading_ts=captured_at,
        five_hour=UsageWindow(pct=12.0, resets_at=1_755_600_000),
        long_window=UsageWindow(pct=34.0, resets_at=1_755_660_000),
        verdict="pause" if verdict == "pause" else "ok",
        policy_rev=policy_revision(POLICY),
        binding_window="weekly" if backend == "codex" and verdict == "pause" else "",
        resume_at_epoch=1_755_660_000 if verdict == "pause" else 0,
        sessions_total=1,
        sessions_fresh=1,
    )


def test_export_round_trips_and_rejects_off_contract_documents() -> None:
    export = _export()
    payload = export.to_dict()
    assert payload["schema"] == EXPORT_SCHEMA
    assert set(payload["windows"]) == {"5h", "weekly"}
    assert UsageExport.from_dict(payload) == export

    for broken in (
        {},
        {**payload, "schema": "chitra.usage.v1"},
        {**payload, "backend": "gemini"},
        {**payload, "verdict": "stale-export"},
        {**payload, "captured_at": "not-a-time"},
        {**payload, "captured_at": "2026-08-16T12:55:00"},
        {**payload, "windows": {"5h": {"used_pct": 101, "resets_at": 1}, "weekly": None}},
        {**payload, "windows": {"5h": {"used_pct": 10, "resets_at": True}, "weekly": None}},
        {**payload, "sessions_total": -1},
        {**payload, "binding_window": "7d"},
        {**payload, "host": 1},
    ):
        with pytest.raises(ValueError):
            UsageExport.from_dict(broken)


def test_export_carries_no_provider_token() -> None:
    """The whole point of pushing a file is that it crosses hosts safely."""
    text = json.dumps(_export().to_dict())
    for forbidden in ("token", "id_token", "api_key", "authorization", "secret", "bearer"):
        assert forbidden not in text.lower()


def test_claude_export_reports_the_most_severe_fresh_session(tmp_path: Path) -> None:
    directory = tmp_path / "usage"
    _write_snapshot(directory, _claude_snapshot(five_hour_pct=5, seven_day_pct=5, session_id="quiet"))
    _write_snapshot(directory, _claude_snapshot(five_hour_pct=97, seven_day_pct=40, session_id="hot"))

    export = build_claude_export(directory, host="tophand", policy=POLICY, now=NOW)

    assert export.verdict == "pause"
    assert export.binding_window == "5h"
    assert export.resume_at_epoch == 1_755_600_000
    assert export.sessions_total == 2
    assert export.sessions_fresh == 2
    assert export.error == ""


def test_claude_export_without_a_fresh_session_is_unknown_and_says_why(tmp_path: Path) -> None:
    directory = tmp_path / "usage"
    _write_snapshot(directory, _claude_snapshot(five_hour_pct=44, seven_day_pct=8, ts="2026-08-16T09:00:00+00:00"))

    export = build_claude_export(directory, host="tophand", policy=POLICY, now=NOW)

    assert export.verdict == "unknown"
    assert export.sessions_fresh == 0
    assert "fresher than" in export.error
    # Last known windows still travel, so the monitor sees the shape of the gap.
    assert export.five_hour is not None
    assert export.five_hour.pct == 44


def test_claude_export_with_no_snapshots_at_all_is_unknown(tmp_path: Path) -> None:
    export = build_claude_export(tmp_path / "absent", host="tophand", policy=POLICY, now=NOW)

    assert export.verdict == "unknown"
    assert export.sessions_total == 0
    assert "no Claude sidecar snapshots" in export.error


def test_codex_export_writes_a_record_even_when_the_read_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(**_kwargs: object) -> UsageSnapshot:
        raise CodexSnapshotError("codex app-server did not respond within 45 seconds")

    monkeypatch.setattr(usage_export, "codex_snapshot", explode)

    export = build_codex_export(host="tophand", policy=POLICY, now=NOW)

    assert export.verdict == "unknown"
    assert "did not respond" in export.error
    assert export.five_hour is None


def test_codex_export_evaluates_the_reading(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = UsageSnapshot(
        kind="codex",
        ts=NOW.isoformat(),
        session_id="codex-account",
        tmux_session="",
        five_hour=UsageWindow(pct=20.0, resets_at=1_755_600_000),
        seven_day=UsageWindow(pct=99.0, resets_at=1_755_660_000),
        account="agent@example.com",
    )
    monkeypatch.setattr(usage_export, "codex_snapshot", lambda **_kwargs: snapshot)

    export = build_codex_export(host="tophand", policy=POLICY, now=NOW)

    assert export.verdict == "pause"
    assert export.binding_window == "weekly"
    assert export.resume_at_epoch == 1_755_660_000


def test_write_export_is_atomic_and_keeps_bounded_history(tmp_path: Path) -> None:
    export = _export(captured_at=NOW.isoformat())

    path = write_export(tmp_path, export, now=NOW)

    assert path == tmp_path / "tophand" / "codex.json"
    assert json.loads(path.read_text(encoding="utf-8"))["schema"] == EXPORT_SCHEMA
    assert (tmp_path / "tophand" / "history" / "codex-2026081613.json").is_file()
    assert not list(tmp_path.glob("**/*.tmp"))

    history = tmp_path / "tophand" / "history"
    (history / "codex-2026080101.json").write_text("{}", encoding="utf-8")
    (history / "codex-keepme.json").write_text("{}", encoding="utf-8")

    assert prune_history(tmp_path / "tophand", now=NOW) == 1
    assert not (history / "codex-2026080101.json").exists()
    # A name this code did not write is never deleted.
    assert (history / "codex-keepme.json").is_file()
    assert (history / "codex-2026081613.json").is_file()


def test_fleet_read_flags_stale_missing_and_invalid_exports(tmp_path: Path) -> None:
    write_export(tmp_path, _export(host="twinridge", captured_at=NOW.isoformat()), keep_history=False, now=NOW)
    write_export(
        tmp_path,
        _export(host="tophand", captured_at=(NOW - timedelta(minutes=45)).isoformat()),
        keep_history=False,
        now=NOW,
    )
    (tmp_path / "trinity").mkdir()
    (tmp_path / "trinity" / "claude.json").write_text("{not json", encoding="utf-8")

    verdicts = {(item.host, item.backend): item for item in read_fleet_exports(tmp_path, now=NOW)}

    assert verdicts[("twinridge", "codex")].verdict == "ok"
    assert verdicts[("twinridge", "codex")].age_seconds == 0
    # Claude never exported on these hosts: an absence, named.
    assert verdicts[("twinridge", "claude")].verdict == "missing-export"
    assert verdicts[("tophand", "codex")].verdict == "stale-export"
    assert "check the export timer on tophand" in verdicts[("tophand", "codex")].error
    assert verdicts[("trinity", "claude")].verdict == "invalid-export"
    assert verdicts[("trinity", "codex")].verdict == "missing-export"


def test_fleet_read_never_returns_a_silent_unknown_for_a_dead_timer(tmp_path: Path) -> None:
    """A host that stops exporting must read as an incident, not as quiet."""
    write_export(
        tmp_path,
        _export(host="tophand", captured_at=(NOW - timedelta(hours=48)).isoformat()),
        keep_history=False,
        now=NOW,
    )

    verdicts = [item for item in read_fleet_exports(tmp_path, now=NOW) if item.backend == "codex"]

    assert [item.verdict for item in verdicts] == ["stale-export"]
    assert verdicts[0].age_seconds == 48 * 3600


def test_fleet_read_carries_the_pause_resume_time(tmp_path: Path) -> None:
    write_export(
        tmp_path,
        _export(host="tophand", verdict="pause", captured_at=NOW.isoformat()),
        keep_history=False,
        now=NOW,
    )

    codex = next(item for item in read_fleet_exports(tmp_path, now=NOW) if item.backend == "codex")

    assert codex.verdict == "pause"
    assert codex.binding_window == "weekly"
    assert codex.to_dict()["resume_at_iso"] == datetime.fromtimestamp(1_755_660_000, UTC).isoformat()


def test_fleet_read_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        read_fleet_exports(tmp_path / "absent", now=NOW)


def test_policy_revision_tracks_threshold_changes() -> None:
    assert policy_revision(POLICY) == policy_revision(UsagePolicy())
    assert policy_revision(POLICY) != policy_revision(UsagePolicy(pause_5h_pct=90.0))


def test_export_cli_writes_both_backends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    claude_dir = tmp_path / "usage"
    _write_snapshot(claude_dir, _claude_snapshot(five_hour_pct=10, seven_day_pct=10, ts=NOW.isoformat()))

    def explode(**_kwargs: object) -> UsageSnapshot:
        raise CodexSnapshotError("codex binary was not found: codex")

    monkeypatch.setattr(usage_export, "codex_snapshot", explode)
    fleet_dir = tmp_path / "fleet"

    assert main(["export", "--fleet-dir", str(fleet_dir), "--host", "tophand", "--dir", str(claude_dir)]) == 0

    assert (fleet_dir / "tophand" / "claude.json").is_file()
    codex = json.loads((fleet_dir / "tophand" / "codex.json").read_text(encoding="utf-8"))
    assert codex["verdict"] == "unknown"
    assert "codex binary was not found" in codex["error"]
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["backend"] for line in lines] == ["claude", "codex"]


def test_evaluate_fleet_dir_cli_renders_one_line_per_host_and_backend(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_export(tmp_path, _export(host="tophand", captured_at=datetime.now(UTC).isoformat()), keep_history=False)

    assert main(["evaluate", "--fleet-dir", str(tmp_path)]) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [(line["host"], line["backend"], line["verdict"]) for line in lines] == [
        ("tophand", "claude", "missing-export"),
        ("tophand", "codex", "ok"),
    ]


def test_evaluate_rejects_both_directory_flags(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["evaluate", "--dir", str(tmp_path), "--fleet-dir", str(tmp_path)]) == 1
    assert "not both" in capsys.readouterr().err


def test_evaluate_requires_one_directory_flag(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["evaluate"]) == 1
    assert "--fleet-dir" in capsys.readouterr().err
