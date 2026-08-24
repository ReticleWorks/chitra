"""Cross-process acceptance proof for governed Tophand close and resume.

The test uses the pinned Adapter and Fleet sources through their production
Python boundaries.  Local sleeper processes and command shims stand in for a
tmux pane and its target command.  No host service, credential, provider, or
network endpoint is contacted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

ADAPTER_ROOT = Path(os.environ.get("ADAPTER_SOURCE_ROOT", "/private/tmp/adapter-tophand-resume-red-base-20260824"))
FLEET_ROOT = Path(os.environ.get("FLEET_SOURCE_ROOT", "/private/tmp/fleet-tophand-resume-red-base-20260824"))
LANE = "probe-lane"
SESSION_REF = "tophand:probe-lane:0.0"
GOAL_ID = "goal-a"
INSTANCE_ID = "instance-a"
GENERATION = 1


def _write_executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _process_start_token(pid: int) -> str:
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart="],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0 and completed.stdout.strip()
    return f"ps:{completed.stdout.strip()}"


def _owner_process(process: subprocess.Popen[str], token: str) -> dict[str, object]:
    completed = subprocess.run(
        ["ps", "-p", str(process.pid), "-o", "uid=", "-o", "gid=", "-o", "comm="],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0 and completed.stdout.strip()
    uid, gid, executable = completed.stdout.strip().split(None, 2)
    return {
        "pid": process.pid,
        "uid": int(uid),
        "gid": int(gid),
        "start_token": token,
        "comm": Path(executable).name,
        "exe": executable,
    }


def _seed_lane(state_root: Path, *, provider_pid: int, process_token: str) -> None:
    state_dir = state_root / "tophand" / LANE
    state_dir.mkdir(parents=True)
    snapshot = {
        "goal": "Close and resume the enrolled lane",
        "done_when": "The same physical provider session resumes once",
        "intent": "Preserve the governed lane",
        "scope": "One local acceptance lane",
        "source": "cross-process-acceptance",
        "roadmap": [{"id": "resume", "status": "pending"}],
        "progress": {"percentage": 0, "completed_steps": 0, "total_steps": 1},
        "problems": [],
        "open_problems": [],
        "resolved_problems": [],
    }
    canonical_snapshot = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    goal_bytes = (canonical_snapshot + "\n").encode()
    goal_path = state_dir / "lane-goal.json"
    goal_path.write_bytes(goal_bytes)
    goal_path.chmod(0o600)
    receipt = {
        "schema": "chitra.lane-launch.v1",
        "lane_id": LANE,
        "session_ref": SESSION_REF,
        "goal_version": 1,
        "goal_snapshot": snapshot,
        "goal_snapshot_sha256": hashlib.sha256(canonical_snapshot.encode()).hexdigest(),
        "goal_snapshot_provenance": {
            "schema": "chitra.lane-goal-provenance.v1",
            "source": "lane-goal-ledger",
            "authored_by": "lane",
            "source_path": str(goal_path),
            "source_sha256": hashlib.sha256(goal_bytes).hexdigest(),
            "source_mode": 0o600,
            "readback_verified": True,
        },
        "provider_instance_id": INSTANCE_ID,
        "provider_generation": GENERATION,
        "provider_session_id": SESSION_REF,
        "backend": "codex",
        "model": "gpt-5.6-sol",
        "effort": "medium",
        "provider_pid": provider_pid,
        "process_start_token": process_token,
        "identity_env": {
            "CHITRA_LANE_ID": LANE,
            "CHITRA_SESSION_REF": SESSION_REF,
            "CHITRA_PANE_TARGET": f"{LANE}:0.0",
            "CHITRA_PROCESS_START_TOKEN": process_token,
        },
        "ownership": {
            "schema": "chitra.lane-ownership.v1",
            "status": "owned",
            "authoritative": True,
            "lane_id": LANE,
            "session_ref": SESSION_REF,
            "provider_pid": provider_pid,
            "owner_pid": provider_pid,
            "process_start_token": process_token,
            "observed_process": {
                "pid": provider_pid,
                "pane_target": f"{LANE}:0.0",
                "process_start_token": process_token,
            },
        },
    }
    (state_dir / "lane-launch.json").write_text(
        json.dumps(receipt, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    transcript = state_dir / "tmux-transcript.log"
    transcript.write_text("governed checkpoint\n", encoding="utf-8")
    digest = hashlib.sha256(transcript.read_bytes()).hexdigest()
    (state_dir / "checkpoints.jsonl").write_text(
        json.dumps(
            {
                "schema": "chitra.lane-checkpoint.v1",
                "checkpoint_ref": digest,
                "lane_id": LANE,
                "provider_thread_ref": SESSION_REF,
                "transcript_sha256": digest,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )


def _command_shims(
    tmp_path: Path,
    *,
    provider_pid: int,
    process_token: str,
    new_owner: dict[str, object],
) -> tuple[Path, Path, Path]:
    fake_tmux = _write_executable(
        tmp_path / "tmux",
        "#!/bin/sh\n"
        'if [ -f "$RESUMED_MARKER" ]; then PID=$NEW_PID TOKEN=$NEW_TOKEN; else PID=$FAKE_PID TOKEN=$FAKE_TOKEN; fi\n'
        'case " $* " in\n'
        '  *" list-sessions "*) printf \'%s\\n\' "$FAKE_SESSION" ;;\n'
        '  *" list-panes "*)\n'
        '    if [ -f "$STOPPED_MARKER" ] && [ ! -f "$RESUMED_MARKER" ]; then exit 1; fi\n'
        '    if [ ! -f "$RESUMED_MARKER" ] && ! kill -0 "$FAKE_PID" 2>/dev/null; then exit 1; fi\n'
        '    printf \'%s\\t0\\t0\\t%s\\tpython\\t%s\\n\' "$FAKE_SESSION" "$PID" "$TOKEN" ;;\n'
        '  *" has-session "*) exit 1 ;;\n'
        '  *" capture-pane "*) [ ! -f "$CAPTURE_MARKER" ] || printf \'continued\\n\' ;;\n'
        '  *" send-keys "*)\n'
        '    printf \'%s\\n\' "$SEND_EVENT_JSON" >> "$TRANSCRIPT_PATH"\n'
        '    printf \'continued\' > "$CAPTURE_MARKER" ;;\n'
        "  *) exit 0 ;;\n"
        "esac\n",
    )
    target = _write_executable(
        tmp_path / "chitra-lane-session",
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            f"""
            import json
            import os
            import sys
            from pathlib import Path

            action = sys.argv[-1]
            if action == "stop":
                count = Path(os.environ["STOP_COUNT"])
                count.write_text(str(int(count.read_text() if count.exists() else "0") + 1))
                Path(os.environ["STOPPED_MARKER"]).write_text("stopped")
                raise SystemExit(0)
            if action == "start":
                count = Path(os.environ["RESUME_COUNT"])
                count.write_text(str(int(count.read_text() if count.exists() else "0") + 1))
                Path(os.environ["RESUMED_MARKER"]).write_text("resumed")
                raise SystemExit(0)
            if action != "resume":
                print(json.dumps({{
                    "state": "running" if Path(os.environ["RESUMED_MARKER"]).exists() else "closed",
                    "provider_instance_id": {INSTANCE_ID!r},
                    "provider_generation": {GENERATION},
                    "provider_session_id": {SESSION_REF!r},
                    "process_start_token": {new_owner["start_token"]!r},
                }}))
                raise SystemExit(0)
            request = json.loads(sys.stdin.read())
            count = Path(os.environ["RESUME_COUNT"])
            count.write_text(str(int(count.read_text() if count.exists() else "0") + 1))
            Path(os.environ["RESUMED_MARKER"]).write_text("resumed")
            operation = request["operation"]
            print(json.dumps({{
                "status": "consumed", "accepted": True, "consumed": True,
                "operation_id": operation["operation_id"],
                "kind": "create_or_resume",
                "lane_id": operation["lane_id"],
                "goal_id": request["goal_id"],
                "provider_handle": operation["provider_handle"],
                "idempotency_key": operation["idempotency_key"],
                "payload_digest": operation["payload_digest"],
                "provider_instance_id": operation["provider_instance_id"],
                "provider_generation": operation["provider_generation"],
                "provider_session_id": request["provider_session_id"],
                "session_ref": request["provider_session_id"],
                "provider_pid": {new_owner["pid"]},
                "process_start_token": {new_owner["start_token"]!r},
            }}, sort_keys=True, separators=(",", ":")))
            """
        ),
    )
    forced = _write_executable(
        tmp_path / "forced-command.py",
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import importlib.util
            import io
            import os
            import subprocess
            import sys
            from contextlib import redirect_stdout
            from importlib.machinery import SourceFileLoader
            from pathlib import Path
            from types import SimpleNamespace

            source = Path(os.environ["FLEET_SOURCE_ROOT"]) / "roles/base/files/chitra-codexman-ssh"
            loader = SourceFileLoader("cross_process_fleet_wrapper", str(source))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            module.REPO = Path(os.environ["TEST_REPO"])
            module.LANE_RUNTIME_ROOT = Path(os.environ["RUNTIME_ROOT"])
            module.LANE_STATE_ROOT = Path(os.environ["STATE_ROOT"])
            module.BOOMTOWN_LANE_STATE_ROOT = Path(os.environ["STATE_ROOT"])
            module.TMUX = os.environ["FAKE_TMUX"]
            module.LANE_SESSION_COMMAND = Path(os.environ["LANE_SESSION_COMMAND"])
            module.os.uname = lambda: SimpleNamespace(nodename="tophand")
            module.target_allowed = lambda *_args: True
            module._sync_lane_credentials = lambda *_args: True
            module._attach_transcript = lambda *_args: None
            original_run = subprocess.run

            def mapped_run(argv, *args, **kwargs):
                command = list(argv)
                if command and command[0] == "tmux":
                    command[0] = module.TMUX
                return original_run(command, *args, **kwargs)

            module.subprocess.run = mapped_run
            original_owner_alive = module.owner_alive
            module.owner_alive = lambda pid, token: (
                False
                if Path(os.environ["STOPPED_MARKER"]).exists()
                and not Path(os.environ["RESUMED_MARKER"]).exists()
                and pid == int(os.environ["FAKE_PID"])
                else original_owner_alive(pid, token)
            )
            os.environ["SSH_ORIGINAL_COMMAND"] = " ".join(sys.argv[1:])
            output = io.StringIO()
            with redirect_stdout(output):
                result = module.main()
            if (
                result == 0
                and os.environ.get("LOSE_FLEET_RESUME_REPLY") == "1"
                and sys.argv[1] == "chitra-lane-resume"
                and not Path(os.environ["LOST_REPLY_MARKER"]).exists()
            ):
                Path(os.environ["LOST_REPLY_MARKER"]).write_text("lost")
                os._exit(91)
            sys.stdout.write(output.getvalue())
            raise SystemExit(result)
            """
        ),
    )
    return fake_tmux, target, forced


def _environment(
    tmp_path: Path,
    *,
    fake_tmux: Path,
    target: Path,
    provider_pid: int,
    process_token: str,
) -> dict[str, str]:
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join((str(ADAPTER_ROOT), os.environ.get("PYTHONPATH", ""))),
        "ADAPTER_SOURCE_ROOT": str(ADAPTER_ROOT),
        "FLEET_SOURCE_ROOT": str(FLEET_ROOT),
        "TEST_REPO": str(repo),
        "STATE_ROOT": str(tmp_path / "state"),
        "RUNTIME_ROOT": str(tmp_path / "runtime"),
        "FAKE_TMUX": str(fake_tmux),
        "FAKE_SESSION": LANE,
        "FAKE_PID": str(provider_pid),
        "FAKE_TOKEN": process_token,
        "NEW_PID": str(provider_pid),
        "NEW_TOKEN": process_token,
        "RESUMED_MARKER": str(tmp_path / "resumed-marker"),
        "STOPPED_MARKER": str(tmp_path / "stopped-marker"),
        "LOST_REPLY_MARKER": str(tmp_path / "lost-reply-marker"),
        "CAPTURE_MARKER": str(tmp_path / "capture-marker"),
        "TRANSCRIPT_PATH": str(tmp_path / "state" / "tophand" / LANE / "tmux-transcript.log"),
        "SEND_EVENT_JSON": "",
        "LANE_SESSION_COMMAND": str(target),
        "STOP_COUNT": str(tmp_path / "stop-count"),
        "RESUME_COUNT": str(tmp_path / "resume-count"),
    }


def _governed_close_request(state_root: Path, process_token: str) -> dict[str, object]:
    checkpoint_ref = hashlib.sha256((state_root / "tophand" / LANE / "tmux-transcript.log").read_bytes()).hexdigest()
    checkpoint = {
        "schema_name": "chitra.governed-close-checkpoint.v1",
        "schema_version": 1,
        "checkpoint_ref": checkpoint_ref,
        "lane": LANE,
        "goal_id": GOAL_ID,
        "goal_version": 1,
        "session_ref": SESSION_REF,
        "provider_binding": {
            "kind": "tophand",
            "handle": "thread-a",
            "provider_session_id": SESSION_REF,
            "instance_id": INSTANCE_ID,
            "generation": GENERATION,
        },
        "provenance": {"kind": "governed-completion-checkpoint", "owner": "chitra"},
        "signature": "a" * 64,
    }
    checkpoint_receipt_sha256 = hashlib.sha256(
        json.dumps(checkpoint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "close_token": "close-token-a",
        "operation": _close_operation(
            checkpoint_ref=checkpoint_ref,
            checkpoint_receipt_sha256=checkpoint_receipt_sha256,
            process_start_token=process_token,
        ),
        "checkpoint_ref": checkpoint_ref,
        "checkpoint_receipt": checkpoint,
        "checkpoint_receipt_sha256": checkpoint_receipt_sha256,
        "checkpoint_verifier": "chitra.detect.rescue.verify_checkpoint_receipt_signature",
        "provider_session_id": SESSION_REF,
        "archive": True,
    }


def _adapter_child(tmp_path: Path, forced: Path) -> Path:
    return _write_executable(
        tmp_path / "adapter-child.py",
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            f"""
            import json
            import sys
            from pathlib import Path
            from tools.support.chitra_adapter.tophand_adapter import ProviderEvidence, TophandAdapter, TophandCommandTransport

            request = json.loads(sys.stdin.read())
            method = sys.argv[1]
            operation = request.get("operation", {{}})
            process_start_token = operation.get(
                "process_start_token", request.get("process_start_token")
            )
            transport = TophandCommandTransport(
                (sys.executable, {str(forced)!r}), lane_id={LANE!r},
                session_ref={SESSION_REF!r}, goal_id={GOAL_ID!r}, goal_version=1,
                provider_session_id={SESSION_REF!r},
                process_start_token=process_start_token,
                forced_surface=True,
            )
            evidence = ProviderEvidence(Path(sys.argv[2]), {LANE!r})
            adapter = TophandAdapter(
                transport, lane_id={LANE!r}, goal_id={GOAL_ID!r},
                goal_version=1, session_ref={SESSION_REF!r},
                provider_session_id={SESSION_REF!r}, provider_handle="thread-a",
                provider_instance_id={INSTANCE_ID!r}, provider_generation={GENERATION},
                process_start_token=process_start_token,
                evidence=evidence,
            )
            result = adapter.status() if method == "status" else getattr(adapter, method)(request)
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            """
        ),
    )


def _crash_window_child(tmp_path: Path) -> Path:
    return _write_executable(
        tmp_path / "adapter-crash-child.py",
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import hashlib
            import hmac
            import json
            import os
            import sys
            from pathlib import Path

            from tools.support.chitra_adapter.tophand_adapter import ProviderEvidence, TophandAdapter

            mode = sys.argv[1]
            evidence = ProviderEvidence(Path(sys.argv[2]), "probe-lane")
            request = json.loads(sys.stdin.read())
            target_state = Path(os.environ["TARGET_STATE"])
            transport_log = Path(os.environ["TRANSPORT_LOG"])

            def log(value):
                with transport_log.open("a", encoding="utf-8") as writer:
                    writer.write(json.dumps(value, sort_keys=True) + "\\n")

            if mode == "seed":
                evidence.append_pending(request)
                raise SystemExit(0)

            class Transport:
                def create_or_resume(self, value):
                    log({"verb": "create_or_resume"})
                    raise AssertionError("an attempted resume must reconcile, not launch")

                def reconcile_operation(self, value):
                    log({
                        "verb": "reconcile-resume",
                        "operation_id": value.get("operation_id"),
                        "resume_after_close": value.get("resume_after_close"),
                        "close_operation_id": value.get("close_operation_id"),
                        "context_ref": value.get("context_ref"),
                        "resume_token": value.get("resume_token"),
                    })
                    if target_state.exists():
                        return json.loads(target_state.read_text(encoding="utf-8"))
                    operation = request["operation"]
                    receipt = {
                        "schema": "chitra.lane-reopen.v1",
                        "operation_id": operation["operation_id"],
                        "close_operation_id": request["close_operation_id"],
                        "lane_id": operation["lane_id"],
                        "goal_id": request["goal_id"],
                        "goal_version": request["goal_version"],
                        "session_ref": request["session_ref"],
                        "provider_session_id": request["provider_session_id"],
                        "provider_handle": operation["provider_handle"],
                        "provider_instance_id": operation["provider_instance_id"],
                        "provider_generation": operation["provider_generation"],
                        "checkpoint_ref": request["context_ref"],
                        "prior_owner_process": request["owner_process"],
                        "owner_process": json.loads(os.environ["NEW_OWNER"]),
                        "created_new_lane": False,
                        "created_new_session": False,
                        "auth_token": request["resume_token"],
                        "observed_at": "2026-08-24T12:01:00+00:00",
                        "evidence": "durable target reopen before reply",
                    }
                    unsigned = json.dumps(receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                    receipt["receipt_hmac"] = hmac.new(
                        request["resume_token"].encode(), unsigned.encode(), hashlib.sha256
                    ).hexdigest()
                    result = {
                        "status": "consumed",
                        "accepted": True,
                        "consumed": True,
                        "process_start_token": receipt["owner_process"]["start_token"],
                        "reopen_receipt": receipt,
                    }
                    target_state.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
                    count = Path(os.environ["PHYSICAL_REOPEN_COUNT"])
                    count.write_text(str(int(count.read_text() if count.exists() else "0") + 1))
                    if os.environ.get("LOSE_FLEET_REPLY") == "1":
                        os._exit(91)
                    return result

                def send(self, value):
                    operation = value["operation"]
                    log({"verb": "send", "process_start_token": operation["process_start_token"]})
                    return {
                        "status": "consumed",
                        "accepted": True,
                        "consumed": True,
                        "provider_instance_id": operation["provider_instance_id"],
                        "provider_generation": operation["provider_generation"],
                        "provider_session_id": operation["provider_session_id"],
                        "process_start_token": operation["process_start_token"],
                    }

            adapter = TophandAdapter(
                Transport(), lane_id="probe-lane", goal_id="goal-a", goal_version=1,
                session_ref="tophand:probe-lane:0.0", provider_session_id="tophand:probe-lane:0.0",
                provider_handle="thread-a", provider_instance_id="instance-a", provider_generation=1,
                process_start_token=(os.environ["NEW_PROCESS_TOKEN"] if mode == "send" else None),
                evidence=evidence,
            )
            result = getattr(adapter, "create_or_resume" if mode == "resume" else "send")(request)
            print(json.dumps(result, sort_keys=True, separators=(",", ":")))
            """
        ),
    )


def _packaged_close_projection_child(tmp_path: Path) -> Path:
    return _write_executable(
        tmp_path / "packaged-close-projection-child.py",
        "#!/usr/bin/env python3\n"
        + textwrap.dedent(
            """
            import hashlib
            import json
            import sys
            from datetime import UTC, datetime
            from pathlib import Path

            from chitra.governed_close import _close_payload, _write_checkpoint
            from chitra.provider_protocol import CloseRequest
            from chitra.recovery import RecoveryEngine
            from chitra.recovery_provider import _PackagedTophandProvider, _tophand_operation_dict
            from chitra.session_contract import JoinedLaneRecord, ProviderCapabilities, ProviderIdentity
            from tools.support.chitra_adapter.tophand_adapter import TophandAdapter

            state_root = Path(sys.argv[1])
            capture_path = Path(sys.argv[2])
            record = JoinedLaneRecord(
                lane_id="probe-lane",
                goal_id="goal-a",
                goal_version=1,
                session_ref="tophand:probe-lane:0.0",
                provider=ProviderIdentity(
                    kind="tophand",
                    handle="thread-a",
                    provider_session_id="tophand:probe-lane:0.0",
                    instance_id="instance-a",
                    generation=1,
                    capabilities=ProviderCapabilities(
                        status=True, checkpoint=True, close=True
                    ),
                ),
            )
            reference = _write_checkpoint(state_root, record)
            bound = record.model_copy(update={"checkpoint_reference": reference})
            receipt = json.loads(
                (state_root / "checkpoints" / f"{reference}.json").read_text()
            )
            receipt_digest = hashlib.sha256(
                json.dumps(
                    receipt, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ).encode()
            ).hexdigest()
            operation = RecoveryEngine(state_root=state_root)._operation(
                bound,
                "close",
                _close_payload(bound, checkpoint_receipt_sha256=receipt_digest),
                datetime(2026, 8, 24, 12, tzinfo=UTC),
            )
            request = CloseRequest(
                operation=operation,
                archive=True,
                checkpoint_receipt=receipt,
                checkpoint_receipt_sha256=receipt_digest,
                checkpoint_verifier=(
                    "chitra.detect.rescue.verify_checkpoint_receipt_signature"
                ),
                close_token=json.loads(operation.payload)["close_token"],
            )

            class Captured(Exception):
                pass

            class Transport:
                def close(self, value):
                    capture_path.write_text(
                        json.dumps(value, sort_keys=True, separators=(",", ":"))
                    )
                    raise Captured

            adapter = TophandAdapter(
                Transport(),
                lane_id=record.lane_id,
                goal_id=record.goal_id,
                goal_version=record.goal_version,
                session_ref=record.session_ref,
                provider_session_id=record.provider.provider_session_id,
                provider_handle=record.provider.handle,
                provider_instance_id=record.provider.instance_id,
                provider_generation=record.provider.generation,
            )
            provider = _PackagedTophandProvider(
                adapter, state_root=state_root, result_sink=lambda _value: None
            )
            try:
                provider._call("close", request, operation)
            except Captured:
                pass
            else:
                raise AssertionError("close did not reach the Adapter transport")

            projected = json.loads(capture_path.read_text())
            durable = json.loads(operation.payload)
            assert projected["operation"] == _tophand_operation_dict(operation)
            assert projected["payload"] == durable
            assert projected["checkpoint_receipt"] == receipt
            assert projected["checkpoint_receipt_sha256"] == receipt_digest
            assert projected["checkpoint_verifier"] == request.checkpoint_verifier
            assert projected["close_token"] == durable["close_token"]
            assert projected["checkpoint_ref"] == durable["checkpoint_ref"]
            print(json.dumps(projected, sort_keys=True, separators=(",", ":")))
            """
        ),
    )


def _operation(kind: str, operation_id: str, token: str, payload_digest: str) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "kind": kind,
        "lane_id": LANE,
        "provider_handle": "thread-a",
        "provider_session_id": SESSION_REF,
        "idempotency_key": f"idem-{operation_id}",
        "payload_digest": payload_digest,
        "provider_instance_id": INSTANCE_ID,
        "provider_generation": GENERATION,
        "process_start_token": token,
        "created_at": "2026-08-24T12:00:00+00:00",
        "attempt": 1,
        "payload": f"{kind}-payload",
    }


def test_packaged_chitra_close_projection_reaches_adapter_without_fixture_enrichment(
    tmp_path: Path,
) -> None:
    child = _packaged_close_projection_child(tmp_path)
    chitra_root = Path(__file__).resolve().parents[1]
    environment = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(
            (
                str(chitra_root / "src"),
                str(ADAPTER_ROOT),
                os.environ.get("PYTHONPATH", ""),
            )
        ),
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(child),
            str(tmp_path / "chitra-state"),
            str(tmp_path / "captured-close.json"),
        ],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _close_operation(
    *, checkpoint_ref: str, checkpoint_receipt_sha256: str, process_start_token: str
) -> dict[str, object]:
    payload = {
        "archive": True,
        "checkpoint_ref": checkpoint_ref,
        "lane_id": LANE,
        "goal_id": GOAL_ID,
        "goal_version": 1,
        "session_ref": SESSION_REF,
        "provider_handle": "thread-a",
        "provider_session_id": SESSION_REF,
        "provider_instance_id": INSTANCE_ID,
        "provider_generation": GENERATION,
        "process_start_token": process_start_token,
        "checkpoint_receipt_sha256": checkpoint_receipt_sha256,
        "close_token": "close-token-a",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {
        **_operation(
            "close",
            "close-a",
            process_start_token,
            hashlib.sha256(encoded.encode()).hexdigest(),
        ),
        "payload": encoded,
    }


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses POSIX forced-command wrappers")
def test_governed_close_returns_exact_resumable_evidence_from_a_fresh_process(
    tmp_path: Path,
) -> None:
    provider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], text=True)
    successor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], text=True)
    try:
        token = _process_start_token(provider.pid)
        _seed_lane(tmp_path / "state", provider_pid=provider.pid, process_token=token)
        new_owner = _owner_process(successor, _process_start_token(successor.pid))
        fake_tmux, target, forced = _command_shims(tmp_path, provider_pid=provider.pid, process_token=token, new_owner=new_owner)
        environment = _environment(
            tmp_path,
            fake_tmux=fake_tmux,
            target=target,
            provider_pid=provider.pid,
            process_token=token,
        )
        checkpoint_ref = hashlib.sha256((tmp_path / "state/tophand" / LANE / "tmux-transcript.log").read_bytes()).hexdigest()
        checkpoint = {
            "schema_name": "chitra.governed-close-checkpoint.v1",
            "schema_version": 1,
            "checkpoint_ref": checkpoint_ref,
            "lane": LANE,
            "goal_id": GOAL_ID,
            "goal_version": 1,
            "session_ref": SESSION_REF,
            "provider_binding": {
                "kind": "tophand",
                "handle": "thread-a",
                "provider_session_id": SESSION_REF,
                "instance_id": INSTANCE_ID,
                "generation": GENERATION,
            },
            "provenance": {"kind": "governed-completion-checkpoint", "owner": "chitra"},
            "signature": "a" * 64,
        }
        checkpoint_receipt_sha256 = hashlib.sha256(
            json.dumps(checkpoint, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        request_fields = {
            "close_token": "close-token-a",
            "checkpoint_ref": checkpoint_ref,
            "checkpoint_receipt": checkpoint,
            "checkpoint_receipt_sha256": checkpoint_receipt_sha256,
            "checkpoint_verifier": "chitra.detect.rescue.verify_checkpoint_receipt_signature",
            "provider_session_id": SESSION_REF,
            "archive": True,
        }
        request = {
            "operation": _close_operation(
                checkpoint_ref=checkpoint_ref,
                checkpoint_receipt_sha256=checkpoint_receipt_sha256,
                process_start_token=token,
            ),
            **request_fields,
        }
        child = _adapter_child(tmp_path, forced)
        completed = subprocess.run(
            [sys.executable, str(child), "close", str(tmp_path / "adapter-evidence")],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
        assert result["status"] == "consumed"
        assert result["state"] == "closed"
        assert result["same_provider_thread"] is True
        assert result["later_resume_supported"] is True
        assert result["checkpoint_ref"] == checkpoint_ref
    finally:
        provider.terminate()
        successor.terminate()
        provider.wait(timeout=5)
        successor.wait(timeout=5)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses POSIX process identities")
def test_attempted_resume_reconciles_lost_reply_once_then_send_uses_rotated_token(
    tmp_path: Path,
) -> None:
    prior = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], text=True)
    time.sleep(1.1)
    successor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], text=True)
    try:
        prior_owner = _owner_process(prior, _process_start_token(prior.pid))
        new_owner = _owner_process(successor, _process_start_token(successor.pid))
        resume_fields = {
            "session_ref": SESSION_REF,
            "provider_session_id": SESSION_REF,
            "context_ref": "checkpoint-a",
            "goal_id": GOAL_ID,
            "goal_version": 1,
            "resume_after_close": True,
            "close_operation_id": "close-a",
            "owner_process": prior_owner,
            "resume_token": "resume-token-a",
        }
        resume_digest = hashlib.sha256(json.dumps(resume_fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        resume_operation = {
            **_operation("create_or_resume", "resume-a", "unused", resume_digest),
            "process_start_token": None,
            "payload": json.dumps(
                resume_fields,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ),
        }
        resume_request = {"operation": resume_operation, **resume_fields}
        attempted = {
            "schema": "chitra.tophand.operation.v1",
            **{key: value for key, value in resume_operation.items() if key != "payload"},
            "attempted": True,
        }
        child = _crash_window_child(tmp_path)
        evidence = tmp_path / "adapter-evidence"
        environment = {
            **os.environ,
            "PYTHONPATH": os.pathsep.join((str(ADAPTER_ROOT), os.environ.get("PYTHONPATH", ""))),
            "TARGET_STATE": str(tmp_path / "target-reopen.json"),
            "TRANSPORT_LOG": str(tmp_path / "transport.jsonl"),
            "PHYSICAL_REOPEN_COUNT": str(tmp_path / "physical-reopen-count"),
            "NEW_OWNER": json.dumps(new_owner),
            "NEW_PROCESS_TOKEN": str(new_owner["start_token"]),
        }

        seeded = subprocess.run(
            [sys.executable, str(child), "seed", str(evidence)],
            input=json.dumps(attempted),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert seeded.returncode == 0, seeded.stderr
        pending = json.loads((evidence / "pending-operations" / f"{LANE}.jsonl").read_text())
        assert pending["attempted"] is True
        metadata_only = set(resume_fields) - {"provider_session_id"}
        assert not metadata_only.intersection(pending)

        lost = subprocess.run(
            [sys.executable, str(child), "resume", str(evidence)],
            input=json.dumps(resume_request),
            capture_output=True,
            text=True,
            env={**environment, "LOSE_FLEET_REPLY": "1"},
            check=False,
        )
        assert lost.returncode == 91
        assert (tmp_path / "physical-reopen-count").read_text() == "1"

        reconciled = subprocess.run(
            [sys.executable, str(child), "resume", str(evidence)],
            input=json.dumps(resume_request),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert reconciled.returncode == 0, reconciled.stderr
        result = json.loads(reconciled.stdout)
        assert result["status"] == "consumed"
        assert result["reopen_receipt"]["owner_process"] == new_owner
        assert result["process_start_token"] == new_owner["start_token"]

        replayed = subprocess.run(
            [sys.executable, str(child), "resume", str(evidence)],
            input=json.dumps(resume_request),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert replayed.returncode == 0, replayed.stderr
        assert json.loads(replayed.stdout) == result
        assert (tmp_path / "physical-reopen-count").read_text() == "1"

        send_operation = _operation("send", "send-after-resume", str(new_owner["start_token"]), "send-digest")
        sent = subprocess.run(
            [sys.executable, str(child), "send", str(evidence)],
            input=json.dumps({"operation": send_operation, "text": "continue"}),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert sent.returncode == 0, sent.stderr
        send_result = json.loads(sent.stdout)
        assert send_result["status"] == "consumed"
        assert send_result["process_start_token"] == new_owner["start_token"]
        log = [json.loads(line) for line in (tmp_path / "transport.jsonl").read_text().splitlines()]
        assert [entry["verb"] for entry in log].count("create_or_resume") == 0
        assert [entry["verb"] for entry in log].count("reconcile-resume") == 2
        assert log[-1] == {"verb": "send", "process_start_token": new_owner["start_token"]}
    finally:
        prior.terminate()
        successor.terminate()
        prior.wait(timeout=5)
        successor.wait(timeout=5)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses POSIX forced-command wrappers")
def test_same_session_resume_uses_structured_owner_authenticated_receipt_and_no_duplicate_launch(
    tmp_path: Path,
) -> None:
    provider = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], text=True)
    time.sleep(1.1)
    successor = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], text=True)
    try:
        token = _process_start_token(provider.pid)
        prior_owner = _owner_process(provider, token)
        new_owner = _owner_process(successor, _process_start_token(successor.pid))
        _seed_lane(tmp_path / "state", provider_pid=provider.pid, process_token=token)
        fake_tmux, target, forced = _command_shims(tmp_path, provider_pid=provider.pid, process_token=token, new_owner=new_owner)
        environment = _environment(
            tmp_path,
            fake_tmux=fake_tmux,
            target=target,
            provider_pid=provider.pid,
            process_token=token,
        )
        environment.update(
            {
                "NEW_PID": str(successor.pid),
                "NEW_TOKEN": str(new_owner["start_token"]),
            }
        )
        child = _adapter_child(tmp_path, forced)
        evidence = tmp_path / "adapter-evidence"
        closed = subprocess.run(
            [sys.executable, str(child), "close", str(evidence)],
            input=json.dumps(_governed_close_request(tmp_path / "state", token)),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert closed.returncode == 0, closed.stderr
        close_result = json.loads(closed.stdout)
        assert close_result["status"] == "consumed"
        assert close_result["state"] == "closed"
        assert close_result["owner_process"] == prior_owner
        assert (tmp_path / "stop-count").read_text() == "1"
        provider.terminate()
        provider.wait(timeout=5)
        assert provider.poll() is not None
        resume_fields = {
            "session_ref": SESSION_REF,
            "provider_session_id": SESSION_REF,
            "context_ref": close_result["checkpoint_ref"],
            "goal_id": GOAL_ID,
            "goal_version": 1,
            "resume_after_close": True,
            "close_operation_id": "close-a",
            "owner_process": prior_owner,
            "resume_token": "resume-token-a",
        }
        digest = hashlib.sha256(json.dumps(resume_fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        request = {
            "operation": {
                **_operation("create_or_resume", "resume-a", "unused", digest),
                "process_start_token": None,
                "payload": json.dumps(
                    resume_fields,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ),
            },
            **resume_fields,
        }
        lost = subprocess.run(
            [sys.executable, str(child), "create_or_resume", str(evidence)],
            input=json.dumps(request),
            capture_output=True,
            text=True,
            env={**environment, "LOSE_FLEET_RESUME_REPLY": "1"},
            check=False,
        )
        assert lost.returncode != 0
        assert (tmp_path / "lost-reply-marker").exists(), lost.stderr
        assert (tmp_path / "lost-reply-marker").read_text() == "lost"
        assert (tmp_path / "resume-count").read_text() == "1"
        fleet_state = tmp_path / "state" / "tophand" / LANE
        assert json.loads((fleet_state / "lane-resume-attempt.json").read_text())["state"] == "consumed"
        assert json.loads((fleet_state / "lane-reopen.json").read_text())["schema"] == "chitra.lane-reopen.v1"

        outcomes = []
        for _ in range(2):
            completed = subprocess.run(
                [sys.executable, str(child), "create_or_resume", str(evidence)],
                input=json.dumps(request),
                capture_output=True,
                text=True,
                env=environment,
                check=False,
            )
            assert completed.returncode == 0, completed.stderr
            outcomes.append(json.loads(completed.stdout))

        first, restarted = outcomes
        receipt = first["reopen_receipt"]
        assert first["status"] == "consumed"
        assert receipt["schema"] == "chitra.lane-reopen.v1"
        assert receipt["operation_id"] == "resume-a"
        assert receipt["close_operation_id"] == resume_fields["close_operation_id"]
        assert receipt["checkpoint_ref"] == resume_fields["context_ref"]
        assert receipt["lane_id"] == LANE
        assert receipt["goal_id"] == GOAL_ID
        assert receipt["goal_version"] == 1
        assert receipt["session_ref"] == SESSION_REF
        assert receipt["provider_session_id"] == SESSION_REF
        assert receipt["provider_handle"] == "thread-a"
        assert receipt["provider_instance_id"] == INSTANCE_ID
        assert receipt["provider_generation"] == GENERATION
        assert receipt["prior_owner_process"] == prior_owner
        assert receipt["owner_process"] == new_owner
        assert set(prior_owner) == {"pid", "uid", "gid", "start_token", "comm", "exe"}
        assert set(new_owner) == {"pid", "uid", "gid", "start_token", "comm", "exe"}
        assert new_owner["start_token"] != prior_owner["start_token"]
        assert receipt["auth_token"] == resume_fields["resume_token"]
        unsigned = {key: value for key, value in receipt.items() if key not in {"receipt_hmac", "signature"}}
        expected_hmac = hmac.new(
            str(resume_fields["resume_token"]).encode(),
            json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
            hashlib.sha256,
        ).hexdigest()
        assert hmac.compare_digest(receipt["receipt_hmac"], expected_hmac)
        assert receipt["created_new_lane"] is False
        assert receipt["created_new_session"] is False
        assert restarted == first
        assert (tmp_path / "resume-count").read_text() == "1"
        assert any((evidence / "provider-results").glob("*.jsonl"))

        observed = subprocess.run(
            [sys.executable, str(child), "status", str(evidence)],
            input=json.dumps({"process_start_token": str(new_owner["start_token"])}),
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )
        assert observed.returncode == 0, observed.stderr
        status = json.loads(observed.stdout)
        assert status["state"] == "running"
        assert status["process_start_token"] == new_owner["start_token"]

        send_operation = _operation(
            "send", "send-after-resume", str(new_owner["start_token"]), "send-digest"
        )
        send_event = {
            "event_id": "event-send-after-resume",
            "cursor": "cursor-send-after-resume",
            "sequence": 1,
            "operation_id": "send-after-resume",
            "lane_id": LANE,
            "session_ref": SESSION_REF,
            "provider_session_id": SESSION_REF,
            "provider_instance_id": INSTANCE_ID,
            "provider_generation": GENERATION,
            "nonce": "nonce-send-after-resume",
            "status": "consumed",
            "payload": {"text": "continue"},
        }
        sent = subprocess.run(
            [sys.executable, str(child), "send", str(evidence)],
            input=json.dumps({"operation": send_operation, "text": "continue"}),
            capture_output=True,
            text=True,
            env={**environment, "SEND_EVENT_JSON": json.dumps(send_event, separators=(",", ":"))},
            check=False,
        )
        assert sent.returncode == 0, sent.stderr
        send_result = json.loads(sent.stdout)
        assert send_result["status"] == "consumed"
        assert send_result["process_start_token"] == new_owner["start_token"]
        assert (tmp_path / "stop-count").read_text() == "1"
        assert (tmp_path / "resume-count").read_text() == "1"
    finally:
        if provider.poll() is None:
            provider.terminate()
        successor.terminate()
        if provider.poll() is None:
            provider.wait(timeout=5)
        successor.wait(timeout=5)
