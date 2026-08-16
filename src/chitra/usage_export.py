"""Export token-free per-host usage readings into a shared fleet directory.

Every fleet host writes its own usage export.  The monitor reads the shared
directory instead of reaching back into each host, so a host the monitor holds
no interactive grant to is still visible.  Exports carry percentages, reset
times, and an account identity only -- never a provider token.

The reader treats a missing or old export as a named incident
(``missing-export`` / ``stale-export``) rather than as silence.  A dead export
timer is a fault to raise, not an absence to shrug at.
"""

from __future__ import annotations

import hashlib
import json
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from chitra._fsio import parse_iso8601, write_json_atomic
from chitra.policy_config import UsagePolicy
from chitra.usage import (
    CodexSnapshotError,
    UsageSnapshot,
    UsageWindow,
    Verdict,
    codex_snapshot,
    effective_windows,
    evaluate,
    read_snapshots,
)

EXPORT_SCHEMA = "chitra.usage-export.v1"

ExportBackend = Literal["claude", "codex"]
ExportVerdict = Literal["ok", "approaching", "pause", "unknown"]
FleetVerdict = Literal["ok", "approaching", "pause", "unknown", "stale-export", "missing-export", "invalid-export"]

EXPORT_BACKENDS: tuple[ExportBackend, ...] = ("claude", "codex")

# Codex names its long window "weekly" and Claude names its own "7d".  Keeping
# each provider's own vocabulary in the file avoids implying the two windows
# are the same thing; the reader accepts either key.
LONG_WINDOW_KEY: dict[ExportBackend, str] = {"claude": "7d", "codex": "weekly"}
LONG_WINDOW_KEYS = frozenset(LONG_WINDOW_KEY.values())

# Two export intervals.  The timer runs every 15 minutes, so anything older
# than 30 minutes means at least one run was missed.
DEFAULT_STALE_AFTER_SECONDS = 1800
HISTORY_RETENTION_DAYS = 7

_SEVERITY = {"ok": 0, "approaching": 1, "pause": 2}


def default_host_name() -> str:
    """Return this host's short name, the key each export is filed under."""
    return socket.gethostname().split(".", 1)[0].strip().lower()


def policy_revision(policy: UsagePolicy) -> str:
    """Return a short stable digest of the thresholds an export was judged by.

    Two hosts reporting different verdicts at the same percentage is a policy
    skew, not a provider difference.  Carrying the revision in the file makes
    that skew visible instead of leaving it to be inferred.
    """
    payload = json.dumps(policy.model_dump(), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _window_dict(window: UsageWindow | None) -> dict[str, object] | None:
    if window is None:
        return None
    return {
        "used_pct": window.pct,
        "resets_at": window.resets_at,
        "resets_at_iso": datetime.fromtimestamp(window.resets_at, UTC).isoformat() if window.resets_at else "",
    }


def _window_from_dict(payload: object, *, field_name: str) -> UsageWindow | None:
    if payload is None:
        return None
    if not isinstance(payload, dict):
        raise ValueError(f"usage export {field_name} must be an object or null")
    used_pct = payload.get("used_pct")
    resets_at = payload.get("resets_at")
    if isinstance(used_pct, bool) or not isinstance(used_pct, (int, float)) or not 0 <= used_pct <= 100:
        raise ValueError(f"usage export {field_name}.used_pct must be a number from 0 through 100")
    if isinstance(resets_at, bool) or not isinstance(resets_at, int):
        raise ValueError(f"usage export {field_name}.resets_at must be an integer epoch")
    return UsageWindow(pct=float(used_pct), resets_at=resets_at)


@dataclass(frozen=True, slots=True)
class UsageExport:
    """One host's reading of one backend, as written to the shared tree."""

    host: str
    backend: ExportBackend
    account: str
    captured_at: str
    reading_ts: str
    five_hour: UsageWindow | None
    long_window: UsageWindow | None
    verdict: ExportVerdict
    policy_rev: str
    binding_window: str = ""
    resume_at_epoch: int = 0
    error: str = ""
    sessions_total: int = 0
    sessions_fresh: int = 0

    @property
    def long_window_key(self) -> str:
        return LONG_WINDOW_KEY[self.backend]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": EXPORT_SCHEMA,
            "host": self.host,
            "backend": self.backend,
            "account": self.account,
            "captured_at": self.captured_at,
            "reading_ts": self.reading_ts,
            "windows": {
                "5h": _window_dict(self.five_hour),
                self.long_window_key: _window_dict(self.long_window),
            },
            "verdict": self.verdict,
            "binding_window": self.binding_window,
            "resume_at_epoch": self.resume_at_epoch,
            "policy_rev": self.policy_rev,
            "error": self.error,
            "sessions_total": self.sessions_total,
            "sessions_fresh": self.sessions_fresh,
        }

    @classmethod
    def from_dict(cls, payload: object) -> UsageExport:
        """Parse one persisted export, rejecting anything off-contract."""
        if not isinstance(payload, dict):
            raise ValueError("usage export must be an object")
        if payload.get("schema") != EXPORT_SCHEMA:
            raise ValueError(f"usage export is not a {EXPORT_SCHEMA} document")
        backend = payload.get("backend")
        if backend not in EXPORT_BACKENDS:
            raise ValueError("usage export backend must be claude or codex")
        strings: dict[str, str] = {}
        for field_name in ("host", "account", "captured_at", "policy_rev"):
            value = payload.get(field_name)
            if not isinstance(value, str):
                raise ValueError(f"usage export {field_name} must be a string")
            strings[field_name] = value
        for field_name in ("reading_ts", "error"):
            value = payload.get(field_name, "")
            if not isinstance(value, str):
                raise ValueError(f"usage export {field_name} must be a string")
            strings[field_name] = value
        parse_iso8601(
            strings["captured_at"],
            invalid_message="usage export captured_at must be an ISO8601 datetime with a timezone",
            require_timezone=True,
            normalize_utc=True,
        )
        verdict = payload.get("verdict")
        if verdict not in ("ok", "approaching", "pause", "unknown"):
            raise ValueError("usage export verdict must be ok, approaching, pause, or unknown")
        windows = payload.get("windows")
        if not isinstance(windows, dict):
            raise ValueError("usage export windows must be an object")
        long_key = LONG_WINDOW_KEY[cast(ExportBackend, backend)]
        counters: dict[str, int] = {}
        for field_name in ("sessions_total", "sessions_fresh", "resume_at_epoch"):
            value = payload.get(field_name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"usage export {field_name} must be a non-negative integer")
            counters[field_name] = value
        binding_window = payload.get("binding_window", "")
        if binding_window not in ("", "5h", long_key):
            raise ValueError(f"usage export binding_window must be empty, 5h, or {long_key}")
        return cls(
            host=strings["host"],
            backend=cast(ExportBackend, backend),
            account=strings["account"],
            captured_at=strings["captured_at"],
            reading_ts=strings["reading_ts"],
            five_hour=_window_from_dict(windows.get("5h"), field_name="windows.5h"),
            long_window=_window_from_dict(windows.get(long_key), field_name=f"windows.{long_key}"),
            verdict=cast(ExportVerdict, verdict),
            policy_rev=strings["policy_rev"],
            binding_window=binding_window,
            resume_at_epoch=counters["resume_at_epoch"],
            error=strings["error"],
            sessions_total=counters["sessions_total"],
            sessions_fresh=counters["sessions_fresh"],
        )


def _binding_key(backend: ExportBackend, binding_window: str) -> str:
    """Translate the evaluator's internal window name to the file's vocabulary."""
    return LONG_WINDOW_KEY[backend] if binding_window == "7d" else binding_window


def _worst_fresh(items: list[tuple[UsageSnapshot, bool]], *, policy: UsagePolicy) -> tuple[UsageSnapshot, Verdict] | None:
    """Return the fresh snapshot carrying the host's most severe verdict."""
    scored = [(snapshot, evaluate(snapshot, policy=policy)) for snapshot, fresh in items if fresh]
    if not scored:
        return None
    return max(
        scored,
        key=lambda item: (
            _SEVERITY[item[1].level],
            item[0].five_hour.pct if item[0].five_hour is not None else 0.0,
            item[0].seven_day.pct if item[0].seven_day is not None else 0.0,
        ),
    )


def build_claude_export(
    directory: Path,
    *,
    host: str,
    policy: UsagePolicy,
    staleness_seconds: int = 1200,
    now: datetime | None = None,
) -> UsageExport:
    """Fold this host's Claude sidecar snapshots into one export record.

    The host reports its most severe fresh reading.  A host with sidecar files
    but nothing fresh reports ``unknown`` and says so in ``error`` -- the last
    known windows still travel, so the monitor sees the shape of the problem.
    """
    captured = (datetime.now(UTC) if now is None else now.astimezone(UTC)).isoformat()
    policy_rev = policy_revision(policy)
    try:
        items = read_snapshots(directory, staleness_seconds=staleness_seconds, now=now)
    except (OSError, ValueError) as exc:
        return UsageExport(
            host=host,
            backend="claude",
            account="",
            captured_at=captured,
            reading_ts="",
            five_hour=None,
            long_window=None,
            verdict="unknown",
            policy_rev=policy_rev,
            error=str(exc),
        )
    claude_items = [item for item in items if item[0].kind == "claude"]
    fresh_count = sum(1 for _, fresh in claude_items if fresh)
    chosen = _worst_fresh(claude_items, policy=policy)
    if chosen is not None:
        snapshot, verdict = chosen
        return UsageExport(
            host=host,
            backend="claude",
            account=snapshot.account,
            captured_at=captured,
            reading_ts=snapshot.ts,
            five_hour=snapshot.five_hour,
            long_window=snapshot.seven_day,
            verdict=cast(ExportVerdict, verdict.level),
            policy_rev=policy_rev,
            binding_window=_binding_key("claude", verdict.binding_window),
            resume_at_epoch=verdict.resume_at_epoch,
            sessions_total=len(claude_items),
            sessions_fresh=fresh_count,
        )
    newest = max(claude_items, key=lambda item: item[0].ts)[0] if claude_items else None
    return UsageExport(
        host=host,
        backend="claude",
        account=newest.account if newest is not None else "",
        captured_at=captured,
        reading_ts=newest.ts if newest is not None else "",
        five_hour=newest.five_hour if newest is not None else None,
        long_window=newest.seven_day if newest is not None else None,
        verdict="unknown",
        policy_rev=policy_rev,
        error=(
            f"no Claude sidecar snapshot is fresher than {staleness_seconds}s"
            if claude_items
            else f"no Claude sidecar snapshots under {directory}"
        ),
        sessions_total=len(claude_items),
        sessions_fresh=fresh_count,
    )


def build_codex_export(
    *,
    host: str,
    policy: UsagePolicy,
    codex_bin: Path | str = "codex",
    now: datetime | None = None,
) -> UsageExport:
    """Read the local Codex account and fold it into one export record.

    A Codex read that fails still produces a file.  Writing ``unknown`` with
    the reason attached is what keeps a broken read distinguishable from a
    host that simply runs no Codex lane.
    """
    captured = (datetime.now(UTC) if now is None else now.astimezone(UTC)).isoformat()
    policy_rev = policy_revision(policy)
    try:
        snapshot = codex_snapshot(codex_bin=codex_bin, now=now)
    except (CodexSnapshotError, OSError, ValueError) as exc:
        return UsageExport(
            host=host,
            backend="codex",
            account="",
            captured_at=captured,
            reading_ts="",
            five_hour=None,
            long_window=None,
            verdict="unknown",
            policy_rev=policy_rev,
            error=str(exc),
        )
    verdict = evaluate(snapshot, policy=policy)
    # Each window is filed under the key it actually is, not the slot Codex
    # reported it in. A capped account puts its weekly cap in ``primary``,
    # which chitra maps to ``five_hour``; writing that under "5h" would tell
    # the monitor a five-hour window resets three days from now.
    short, long_window = effective_windows(snapshot)
    return UsageExport(
        host=host,
        backend="codex",
        account=snapshot.account,
        captured_at=captured,
        reading_ts=snapshot.ts,
        five_hour=short,
        long_window=long_window,
        verdict=cast(ExportVerdict, verdict.level),
        policy_rev=policy_rev,
        binding_window=_binding_key("codex", verdict.binding_window),
        resume_at_epoch=verdict.resume_at_epoch,
        sessions_total=1,
        sessions_fresh=1,
    )


def history_path(host_dir: Path, export: UsageExport) -> Path:
    """Return the hourly history slot for one export."""
    stamp = parse_iso8601(export.captured_at, require_timezone=True, normalize_utc=True).strftime("%Y%m%d%H")
    return host_dir / "history" / f"{export.backend}-{stamp}.json"


def prune_history(host_dir: Path, *, now: datetime | None = None, retention_days: int = HISTORY_RETENTION_DAYS) -> int:
    """Delete history slots older than the retention window; return the count.

    Names that do not carry a parseable hour stamp are left alone: this prunes
    what it wrote, and never deletes a file it cannot explain.
    """
    history = host_dir / "history"
    if not history.is_dir():
        return 0
    current = datetime.now(UTC) if now is None else now.astimezone(UTC)
    cutoff = current - timedelta(days=retention_days)
    removed = 0
    for path in sorted(history.glob("*.json")):
        _, _, stamp = path.stem.rpartition("-")
        try:
            written = datetime.strptime(stamp, "%Y%m%d%H").replace(tzinfo=UTC)
        except ValueError:
            continue
        if written < cutoff:
            try:
                path.unlink()
            except OSError:
                continue
            removed += 1
    return removed


def write_export(fleet_dir: Path, export: UsageExport, *, keep_history: bool = True, now: datetime | None = None) -> Path:
    """Write one export atomically and mirror it into the hourly history."""
    host_dir = fleet_dir / export.host
    path = host_dir / f"{export.backend}.json"
    payload = export.to_dict()
    write_json_atomic(path, payload)
    if keep_history:
        write_json_atomic(history_path(host_dir, export), payload)
        prune_history(host_dir, now=now)
    return path


@dataclass(frozen=True, slots=True)
class FleetExportVerdict:
    """One host-and-backend line of the monitor's fleet-wide usage read."""

    host: str
    backend: str
    verdict: FleetVerdict
    captured_at: str
    age_seconds: int
    account: str
    five_hour_pct: float | None
    long_window_key: str
    long_window_pct: float | None
    binding_window: str
    resume_at_epoch: int
    policy_rev: str
    error: str
    path: str

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "backend": self.backend,
            "verdict": self.verdict,
            "captured_at": self.captured_at,
            "age_seconds": self.age_seconds,
            "account": self.account,
            "five_hour_pct": self.five_hour_pct,
            "long_window": self.long_window_key,
            "long_window_pct": self.long_window_pct,
            "binding_window": self.binding_window,
            "resume_at_epoch": self.resume_at_epoch,
            "resume_at_iso": datetime.fromtimestamp(self.resume_at_epoch, UTC).isoformat() if self.resume_at_epoch else "",
            "policy_rev": self.policy_rev,
            "error": self.error,
            "path": self.path,
        }


def _incident(
    *, host: str, backend: ExportBackend, verdict: FleetVerdict, error: str, path: Path, captured_at: str = "", age: int = 0
) -> FleetExportVerdict:
    return FleetExportVerdict(
        host=host,
        backend=backend,
        verdict=verdict,
        captured_at=captured_at,
        age_seconds=age,
        account="",
        five_hour_pct=None,
        long_window_key=LONG_WINDOW_KEY[backend],
        long_window_pct=None,
        binding_window="",
        resume_at_epoch=0,
        policy_rev="",
        error=error,
        path=str(path),
    )


def read_fleet_exports(
    fleet_dir: Path,
    *,
    stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
    now: datetime | None = None,
) -> list[FleetExportVerdict]:
    """Read every host's exports and return one verdict per host and backend.

    Both backends are reported for every host directory present.  A file that
    is absent, old, or unparseable becomes its own verdict, so a dead export
    timer surfaces as an incident on that host rather than as a quiet gap.
    """
    if stale_after_seconds < 0:
        raise ValueError("stale_after_seconds must be non-negative")
    if now is not None and now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    current = datetime.now(UTC) if now is None else now.astimezone(UTC)
    if not fleet_dir.is_dir():
        raise ValueError(f"fleet usage directory does not exist: {fleet_dir}")
    results: list[FleetExportVerdict] = []
    for host_dir in sorted(path for path in fleet_dir.iterdir() if path.is_dir()):
        for backend in EXPORT_BACKENDS:
            path = host_dir / f"{backend}.json"
            if not path.is_file():
                results.append(
                    _incident(
                        host=host_dir.name,
                        backend=backend,
                        verdict="missing-export",
                        error=f"no {backend} export has ever been written by this host",
                        path=path,
                    )
                )
                continue
            try:
                export = UsageExport.from_dict(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                results.append(
                    _incident(
                        host=host_dir.name,
                        backend=backend,
                        verdict="invalid-export",
                        error=str(exc),
                        path=path,
                    )
                )
                continue
            captured = parse_iso8601(export.captured_at, require_timezone=True, normalize_utc=True)
            age = int((current - captured).total_seconds())
            verdict: FleetVerdict = "stale-export" if age > stale_after_seconds else export.verdict
            error = export.error
            if verdict == "stale-export":
                error = f"export is {age}s old, past the {stale_after_seconds}s ceiling; check the export timer on {host_dir.name}"
            results.append(
                FleetExportVerdict(
                    host=export.host or host_dir.name,
                    backend=export.backend,
                    verdict=verdict,
                    captured_at=export.captured_at,
                    age_seconds=age,
                    account=export.account,
                    five_hour_pct=export.five_hour.pct if export.five_hour is not None else None,
                    long_window_key=export.long_window_key,
                    long_window_pct=export.long_window.pct if export.long_window is not None else None,
                    binding_window=export.binding_window,
                    resume_at_epoch=export.resume_at_epoch,
                    policy_rev=export.policy_rev,
                    error=error,
                    path=str(path),
                )
            )
    return results
