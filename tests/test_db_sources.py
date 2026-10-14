"""The database evidence plugs stream canonical events and fail closed.

Devin's ``sessions.db`` and Hermes's ``state.db`` back the
``devin-db:<session>``/``hermes-db:<session>`` and profile-qualified Hermes
URI bindings the hybrid watcher writes. These tests use fixture databases:
row order, join ids, cursor incrementality, replay-stable event ids, and
the fail-closed contract when the session or the database is absent.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from chitra.adapter.contract import LaunchRefused, StreamUnavailable, Unsupported
from chitra.adapter.plugs.db_sources import DevinDbPlug, HermesDbPlug
from chitra.journal.models import CanonicalType
from chitra.journal.reader import DEFAULT_MAX_BATCH_BYTES
from chitra.transcript_bindings import BoundTranscript, TranscriptBinding


def _binding(path: str, *, client: str, lane: str = "lane-a") -> BoundTranscript:
    binding = TranscriptBinding(
        session_ref=lane,
        lane=lane,
        path=path,
        client=client,
        client_version="v1",
        instance="hybrid-shadow",
    )
    return BoundTranscript(binding=binding, path=path)


def _devin_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY);"
        "CREATE TABLE message_nodes (row_id INTEGER PRIMARY KEY, node_id TEXT,"
        " session_id TEXT, created_at REAL, chat_message TEXT);"
    )
    conn.execute("INSERT INTO sessions (id) VALUES ('s-1')")
    messages = [
        {"message_id": "m1", "role": "user", "content": "do the task",
         "metadata": {"is_user_input": True}},
        {"message_id": "m2", "role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "name": "exec_command", "arguments": '{"cmd":"ls"}'}]},
        {"message_id": "m3", "role": "tool", "tool_call_id": "c1",
         "name": "exec_command", "content": "ok"},
        {"message_id": "m4", "role": "assistant", "content": "done"},
    ]
    for index, message in enumerate(messages, 1):
        conn.execute(
            "INSERT INTO message_nodes (row_id, node_id, session_id, created_at, chat_message)"
            " VALUES (?,?,?,?,?)",
            (index, f"n{index}", "s-1", 1790000000.0 + index, json.dumps(message)),
        )
    conn.commit()
    conn.close()


def test_devin_db_streams_canonical_events_and_advances_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "sessions.db"
    _devin_db(database)
    monkeypatch.setenv("CHITRA_DEVIN_DB", str(database))
    plug = DevinDbPlug()
    bound = _binding("devin-db:s-1", client="devin")
    handle = plug.handle_for_binding(bound, session_ref="lane-a")
    assert handle.native_session_id == "s-1"

    batch = plug.events(handle)
    assert [event.normalized_type for event in batch.events] == [
        CanonicalType.USER_INPUT,
        CanonicalType.TOOL_CALL,
        CanonicalType.TOOL_RESULT,
        CanonicalType.FINAL_RESPONSE,
    ]
    # The fields _event_matches_binding enforces all align with the URI row.
    assert all(
        event.client == "devin"
        and event.session_id == "s-1"
        and event.transcript.path == "devin-db:s-1"
        and event.lane == "lane-a"
        and event.goal_ref == "lane-a"
        for event in batch.events
    )
    assert batch.events[1].payload["call_id"] == "c1"
    assert batch.events[2].native_join_id == "c1"
    cursor = batch.cursor
    assert cursor == "4"

    conn = sqlite3.connect(database)
    conn.execute(
        "INSERT INTO message_nodes (row_id, node_id, session_id, created_at, chat_message)"
        " VALUES (?,?,?,?,?)",
        (5, "n5", "s-1", 1790000005.0,
         json.dumps({"message_id": "m5", "role": "assistant", "content": "more"})),
    )
    conn.commit()
    conn.close()
    second = plug.events(handle, cursor=cursor)
    assert [event.normalized_type for event in second.events] == [CanonicalType.FINAL_RESPONSE]
    assert second.cursor == "5"

    # Replay overlap is safe: event ids are stable across a full re-read.
    replay = plug.events(handle)
    assert {event.event_id for event in replay.events} == (
        {event.event_id for event in batch.events}
        | {event.event_id for event in second.events}
    )


def test_devin_db_resumes_after_oversized_row_without_losing_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "sessions.db"
    _devin_db(database)
    large_text = "x" * (DEFAULT_MAX_BATCH_BYTES + 1)
    conn = sqlite3.connect(database)
    for row_id, content in ((5, large_text), (6, "after oversized row")):
        conn.execute(
            "INSERT INTO message_nodes (row_id, node_id, session_id, created_at, chat_message)"
            " VALUES (?,?,?,?,?)",
            (row_id, f"n{row_id}", "s-1", 1790000000.0 + row_id,
             json.dumps({"message_id": f"m{row_id}", "role": "assistant", "content": content})),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("CHITRA_DEVIN_DB", str(database))
    plug = DevinDbPlug()
    handle = plug.handle_for_binding(_binding("devin-db:s-1", client="devin"), session_ref="lane-a")

    first = plug.events(handle)
    assert first.cursor == "5"
    assert first.events[-1].payload["text"] == large_text

    second = plug.events(handle, cursor=first.cursor)
    assert second.cursor == "6"
    assert second.events[0].payload["text"] == "after oversized row"
    assert len(first.events) + len(second.events) == 6

    exhausted = plug.events(handle, cursor=second.cursor)
    assert exhausted.events == ()
    assert exhausted.cursor == second.cursor


def test_devin_db_fails_closed_when_session_or_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "sessions.db"
    _devin_db(database)
    monkeypatch.setenv("CHITRA_DEVIN_DB", str(database))
    plug = DevinDbPlug()
    missing = plug.handle_for_binding(_binding("devin-db:ghost", client="devin"), session_ref="lane-a")
    with pytest.raises(StreamUnavailable):
        plug.events(missing)
    monkeypatch.setenv("CHITRA_DEVIN_DB", str(tmp_path / "absent.db"))
    live = plug.handle_for_binding(_binding("devin-db:s-1", client="devin"), session_ref="lane-a")
    with pytest.raises(StreamUnavailable):
        plug.events(live)


def _hermes_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,"
        " content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,"
        " timestamp REAL, finish_reason TEXT, reasoning_content TEXT);"
    )
    conn.execute("INSERT INTO sessions (id) VALUES ('classic-kangaroo')")
    rows = [
        ("user", "integrate the checks", None, None, None, 1790000001.0, None, None),
        ("assistant", "", None, '[{"id":"t1","function":{"name":"shell","arguments":"{\\"cmd\\":\\"pwd\\"}"}}]',
         None, 1790000002.0, None, None),
        ("tool", "/home/x", "t1", None, "shell", 1790000003.0, None, None),
        ("assistant", "all wired", None, None, None, 1790000004.0, "stop", None),
    ]
    for index, (role, content, call_id, calls, name, stamp, finish, thinking) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO messages (id, session_id, role, content, tool_call_id, tool_calls,"
            " tool_name, timestamp, finish_reason, reasoning_content) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (index, "classic-kangaroo", role, content, call_id, calls, name, stamp, finish, thinking),
        )
    conn.commit()
    conn.close()


def test_hermes_db_streams_canonical_events_and_advances_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.db"
    _hermes_db(database)
    monkeypatch.setenv("CHITRA_HERMES_DB", str(database))
    plug = HermesDbPlug()
    bound = _binding("hermes-db:classic-kangaroo", client="hermes")
    handle = plug.handle_for_binding(bound, session_ref="lane-a")
    assert handle.native_session_id == "classic-kangaroo"

    batch = plug.events(handle)
    assert [event.normalized_type for event in batch.events] == [
        CanonicalType.USER_INPUT,
        CanonicalType.TOOL_CALL,
        CanonicalType.TOOL_RESULT,
        CanonicalType.FINAL_RESPONSE,
    ]
    assert all(
        event.client == "hermes"
        and event.session_id == "classic-kangaroo"
        and event.transcript.path == "hermes-db:classic-kangaroo"
        for event in batch.events
    )
    assert batch.events[1].payload["call_id"] == "t1"
    assert batch.events[2].native_join_id == "t1"
    assert batch.cursor == "4"
    second = plug.events(handle, cursor=batch.cursor)
    assert second.events == ()
    assert second.cursor == "4"

    # A profile URI resolves beside the selected root DB and can distinguish
    # the worker DB even when the root DB has the same session id.
    profile_database = tmp_path / "profiles" / "public-worker" / "state.db"
    profile_database.parent.mkdir(parents=True)
    _hermes_db(profile_database)
    worker = plug.handle_for_binding(
        _binding("hermes-db:public-worker/classic-kangaroo", client="hermes"),
        session_ref="lane-a",
    )
    worker_batch = plug.events(worker)
    assert worker.locator["profile"] == "public-worker"
    assert worker.native_session_id == "classic-kangaroo"
    assert [event.payload_digest for event in worker_batch.events] == [
        event.payload_digest for event in batch.events]
    assert {event.event_id for event in worker_batch.events}.isdisjoint(
        event.event_id for event in batch.events)


def test_hermes_db_fails_closed_when_session_or_db_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "state.db"
    _hermes_db(database)
    monkeypatch.setenv("CHITRA_HERMES_DB", str(database))
    plug = HermesDbPlug()
    missing = plug.handle_for_binding(
        _binding("hermes-db:ghost", client="hermes"), session_ref="lane-a"
    )
    with pytest.raises(StreamUnavailable):
        plug.events(missing)
    monkeypatch.setenv("CHITRA_HERMES_DB", str(tmp_path / "absent.db"))
    live = plug.handle_for_binding(
        _binding("hermes-db:classic-kangaroo", client="hermes"), session_ref="lane-a"
    )
    with pytest.raises(StreamUnavailable):
        plug.events(live)

    # A missing profile binding must not silently fall back to the configured
    # root DB, even when that DB contains the same session id.
    missing_profile = plug.handle_for_binding(
        _binding("hermes-db:missing-worker/classic-kangaroo", client="hermes"),
        session_ref="lane-a",
    )
    monkeypatch.setenv("CHITRA_HERMES_DB", str(database))
    with pytest.raises(StreamUnavailable):
        plug.events(missing_profile)
    with pytest.raises(StreamUnavailable):
        plug.handle_for_binding(
            _binding("hermes-db:../state/classic-kangaroo", client="hermes"),
            session_ref="lane-a",
        )


def test_db_plugs_refuse_delivery_and_lifecycle() -> None:
    for plug in (DevinDbPlug(), HermesDbPlug()):
        with pytest.raises(Unsupported):
            plug.send(None, None)  # type: ignore[arg-type]
        with pytest.raises(LaunchRefused):
            plug.start(None)  # type: ignore[arg-type]
        with pytest.raises(Unsupported):
            plug.stop(None)  # type: ignore[arg-type]
