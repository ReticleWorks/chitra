"""Separate-process proof for target-owned lane registration.

The test deliberately crosses the same boundaries used in production:
Chitra's dispatchd entrypoint, the Adapter registration CLI, and Fleet's
forced-command wrapper.  It uses temporary state and a synthetic provider;
no host service or provider is contacted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_ROOT = Path(
    os.environ.get("ADAPTER_SOURCE_ROOT", "/private/tmp/adapter-orb-repair-20260824")
)
FLEET_ROOT = Path(
    os.environ.get("FLEET_SOURCE_ROOT", "/private/tmp/fleet-registration-repair-20260824")
)
LANE = "probe-lane"
SESSION_REF = "tophand:probe-lane:0.0"
PROCESS = {"pane_pid": 4242, "boot_id": "boot-a", "start_ticks": 77}
PROCESS_TOKEN = "boot-a:77"


def _executable(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _adapter_cli(tmp_path: Path) -> Path:
    return _executable(
        tmp_path / "chitra-lane-registration",
        "#!/bin/sh\n"
        f"exec {sys.executable} {ADAPTER_ROOT / 'tools/support/chitra_adapter/lane_registration.py'} \"$@\"\n",
    )


def _binding(*, goal_id: str = "goal-a") -> dict[str, object]:
    return {
        "schema": "chitra.tophand.operation.v1",
        "operation_id": "create-1",
        "kind": "create_or_resume",
        "lane_id": LANE,
        "provider_handle": "thread-a",
        "provider_session_id": SESSION_REF,
        "idempotency_key": "idem-1",
        "payload_digest": "a" * 64,
        "provider_instance_id": "instance-a",
        "provider_generation": 1,
        "process_start_token": PROCESS_TOKEN,
        "created_at": "2026-08-24T12:00:00+00:00",
        "attempt": 1,
        "attempted": False,
        "goal_id": goal_id,
        "session_ref": SESSION_REF,
    }


def _reconcile_script(
    path: Path, *, adapter_cli: Path, state_root: Path, count_path: Path, goal_id: str
) -> Path:
    return _executable(
        path,
        textwrap.dedent(
            f"""
            import importlib.util, json, os, sys
            from datetime import UTC, datetime
            from importlib.machinery import SourceFileLoader
            from pathlib import Path

            fleet = Path({str(FLEET_ROOT)!r})
            loader = SourceFileLoader("fleet_reconcile_child", str(fleet / "roles/base/files/chitra-codexman-ssh"))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            module.REPO = fleet
            module.LANE_REGISTRATION_INTERVAL_SECONDS = 10
            module.LANE_STATE_ROOT = Path({str(state_root)!r})
            module.BOOMTOWN_LANE_STATE_ROOT = Path({str(state_root)!r})
            module.LANE_RUNTIME_ROOT = Path({str(state_root)!r}) / "run"
            module.LANE_REGISTRATION_COMMAND = Path({str(adapter_cli)!r})
            module.os.uname = lambda: type("U", (), {{"nodename": "tophand"}})()
            paths = module._lane_paths({LANE!r})
            binding = json.loads({json.dumps(_binding(goal_id=goal_id))!r})
            authority = {{
                "host": "tophand",
                "account": "ubuntu",
                "facts_revision": "facts-a",
                "target_uuid": "target-a",
                "route": "chitra-dispatch-grant",
            }}
            identity = {{
                "target": "{LANE}:0.0",
                "pane_pid": {PROCESS["pane_pid"]},
                "process_start": {{"boot_id": {PROCESS["boot_id"]!r}, "start_ticks": {PROCESS["start_ticks"]}}},
            }}
            count = Path({str(count_path)!r})
            def start():
                current = int(count.read_text() if count.exists() else "0")
                count.write_text(str(current + 1))
                return identity
            result = module._reconcile_lane_registration(
                paths,
                start=start,
                session={LANE!r},
                session_ref={SESSION_REF!r},
                binding=binding,
                authority=authority,
                now=datetime.now(UTC),
            )
            print(json.dumps(result, sort_keys=True))
            """
        ),
    )


def _enroll_script(path: Path, *, chitra_state: Path) -> Path:
    return _executable(
        path,
        textwrap.dedent(
            f"""
            import sys
            from datetime import UTC, datetime
            from pathlib import Path

            chitra_root = Path({str(ROOT)!r})
            sys.path[:0] = [str(chitra_root / "src"), str(chitra_root / "tests")]
            from chitra.goals import GoalRecord, upsert_goal
            from _goal_fixtures import enrollment_fields

            now_text = datetime.now(UTC).isoformat()
            stored = upsert_goal(
                Path({str(chitra_state)!r}),
                GoalRecord(
                    session_ref={SESSION_REF!r}, lane_id={LANE!r},
                    intent="Run the enrolled lane goal safely", goal="Run the enrolled lane goal safely",
                    done_when="The enrolled lane records a successful run",
                    source="separate-process-contract", status="working",
                    enrolled_at=now_text, **enrollment_fields("The enrolled lane records a successful run"),
                    now="Run the lane", last_verified="", created_at=now_text, updated_at=now_text,
                ),
            )
            print(stored.goal_id)
            """
        ),
    )


def _forced_registration_script(
    path: Path, *, adapter_cli: Path, state_root: Path
) -> Path:
    return _executable(
        path,
        textwrap.dedent(
            f"""
            import importlib.util, os, sys
            from importlib.machinery import SourceFileLoader
            from pathlib import Path

            fleet = Path({str(FLEET_ROOT)!r})
            loader = SourceFileLoader("fleet_forced_child", str(fleet / "roles/base/files/chitra-codexman-ssh"))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            module.REPO = fleet
            module.LANE_STATE_ROOT = Path({str(state_root)!r})
            module.BOOMTOWN_LANE_STATE_ROOT = Path({str(state_root)!r})
            module.LANE_RUNTIME_ROOT = Path({str(state_root)!r}) / "run"
            module.LANE_REGISTRATION_COMMAND = Path({str(adapter_cli)!r})
            module.os.uname = lambda: type("U", (), {{"nodename": "tophand"}})()
            module._lane_process_identity = lambda *_args: {{
                "target": "{LANE}:0.0",
                "pane_pid": {PROCESS["pane_pid"]},
                "process_start": {{"boot_id": {PROCESS["boot_id"]!r}, "start_ticks": {PROCESS["start_ticks"]}}},
            }}
            os.environ["SSH_ORIGINAL_COMMAND"] = "chitra-lane-registration {LANE}"
            raise SystemExit(module.main())
            """
        ),
    )


def _chitra_script(
    path: Path,
    *,
    lanes_file: Path,
    count_path: Path,
    forced_registration: Path,
    chitra_state: Path,
) -> Path:
    return _executable(
        path,
        textwrap.dedent(
            f"""
            import json, os, sys
            from datetime import UTC, datetime, timedelta
            from pathlib import Path
            from types import SimpleNamespace

            chitra_root = Path({str(ROOT)!r})
            adapter_root = Path({str(ADAPTER_ROOT)!r})
            sys.path[:0] = [str(chitra_root / "src"), str(chitra_root / "tests"), str(adapter_root)]
            from chitra.dispatchd import run_lanes_once
            import chitra.dispatchd as dispatchd
            import chitra.recovery_provider as recovery_provider
            from chitra.goals import GoalRecord, load_goals, upsert_goal
            from chitra.provider_protocol import ProviderName
            from chitra.operating_facts import OperatingFactsProvenance, OperatingFactsSnapshot
            from chitra.session_contract import OperatingFact, ProviderCapabilities, ProviderOperationResult
            from _goal_fixtures import enrollment_fields
            from tools.support.chitra_adapter.tophand_adapter import TophandCommandTransport

            now = datetime.now(UTC)
            facts = (
                OperatingFact(
                    name="fleet.provider-capabilities",
                    value={{
                        "lane_registration_authority": {{
                            "schema": "chitra.lane-registration.v1",
                            "source": "target-owned-launcher",
                            "mode": 0o600,
                        }},
                        "tophand": {{"capabilities": ["create_or_resume"]}},
                    }},
                    state="known", source="fleet", revision="facts-a",
                    observed_at=now.isoformat(), freshness="current",
                    fresh_until=(now + timedelta(minutes=5)).isoformat(), within_authority=True,
                ),
                OperatingFact(
                    name="fleet.placement",
                    value={{"host": "tophand", "account": "ubuntu"}},
                    state="known", source="fleet", revision="facts-a",
                    observed_at=now.isoformat(), freshness="current",
                    fresh_until=(now + timedelta(minutes=5)).isoformat(), within_authority=True,
                ),
                OperatingFact(
                    name="fleet.routing",
                    value={{"dispatch_target": {{"host": "tophand", "user": "ubuntu"}}}},
                    state="known", source="fleet", revision="facts-a",
                    observed_at=now.isoformat(), freshness="current",
                    fresh_until=(now + timedelta(minutes=5)).isoformat(), within_authority=True,
                ),
                OperatingFact(
                    name="fleet.credential-readiness",
                    value={{"ready": True}},
                    state="known", source="fleet", revision="facts-a",
                    observed_at=now.isoformat(), freshness="current",
                    fresh_until=(now + timedelta(minutes=5)).isoformat(), within_authority=True,
                ),
                OperatingFact(
                    name="fleet.access",
                    value={{"dispatch": True}},
                    state="known", source="fleet", revision="facts-a",
                    observed_at=now.isoformat(), freshness="current",
                    fresh_until=(now + timedelta(minutes=5)).isoformat(), within_authority=True,
                ),
                OperatingFact(
                    name="fleet.capacity",
                    value={{"available": True}},
                    state="known", source="fleet", revision="facts-a",
                    observed_at=now.isoformat(), freshness="current",
                    fresh_until=(now + timedelta(minutes=5)).isoformat(), within_authority=True,
                ),
                OperatingFact(
                    name="fleet.versions",
                    value={{"chitra": "test"}},
                    state="known", source="fleet", revision="facts-a",
                    observed_at=now.isoformat(), freshness="current",
                    fresh_until=(now + timedelta(minutes=5)).isoformat(), within_authority=True,
                ),
            )
            facts_snapshot = OperatingFactsSnapshot(observed_at=now.isoformat(), facts=facts)
            facts_snapshot = OperatingFactsSnapshot(
                observed_at=facts_snapshot.observed_at,
                facts=facts_snapshot.facts,
                provenance=OperatingFactsProvenance(
                    source_path="/tmp/synthetic-operating-facts.json",
                    source_sha256="a" * 64,
                    source_mode=0o600,
                    snapshot_sha256=facts_snapshot.content_digest,
                    snapshot_mode=0o600,
                    readback_verified=True,
                    readback_at=now.isoformat(),
                ),
            )
            dispatchd.read_operating_facts = lambda _sources=None: facts_snapshot

            class RegistrationTransport:
                @classmethod
                def from_environment(cls, lane_id, session_ref=None, goal_id=None):
                    return TophandCommandTransport(
                        (sys.executable, {str(forced_registration)!r}),
                        lane_id=lane_id, session_ref=session_ref, goal_id=goal_id,
                        forced_surface=True,
                    )

            recovery_provider._packaged_tophand_transport = RegistrationTransport

            class FakeProvider:
                provider_name = ProviderName.TOPHAND
                capabilities = ProviderCapabilities.from_supported(("create_or_resume",))
                def create_or_resume(self, request):
                    operation = request.operation
                    count = Path({str(count_path)!r})
                    # Keep provider calls in a separate file from Fleet's start count.
                    call_path = count.with_name("provider-create-count")
                    current = int(call_path.read_text() if call_path.exists() else "0")
                    call_path.write_text(str(current + 1))
                    return ProviderOperationResult(
                        operation_id=operation.operation_id, kind=operation.kind,
                        lane_id=operation.lane_id, provider_handle=operation.provider_handle,
                        provider_session_id=operation.provider_session_id,
                        process_start_token=operation.process_start_token,
                        idempotency_key=operation.idempotency_key,
                        payload_digest=operation.payload_digest,
                        provider_instance_id=operation.provider_instance_id,
                        provider_generation=operation.provider_generation,
                        provider_pid={PROCESS["pane_pid"]}, owner_pid={PROCESS["pane_pid"]},
                        observed_process={{"pid": {PROCESS["pane_pid"]}, "owner_pid": {PROCESS["pane_pid"]}, "boot_id": {PROCESS["boot_id"]!r}, "start_ticks": {PROCESS["start_ticks"]}, "process_start_token": operation.process_start_token}},
                        status="consumed", accepted=True, consumed=True,
                        observed_at=datetime.now(UTC).isoformat(), evidence="synthetic provider",
                    )
                def status(self): raise RuntimeError("unused")
                def send(self, request): raise RuntimeError("unused")
                def read_updates(self, cursor=None): raise RuntimeError("unused")
                def checkpoint(self, request): raise RuntimeError("unused")
                def usage(self): raise RuntimeError("unused")
                def cancel_current_turn(self, request): raise RuntimeError("unused")
                def close(self, request): raise RuntimeError("unused")

            def factory(**_kwargs):
                return FakeProvider()
            root = Path({str(chitra_state)!r})
            root.mkdir(parents=True, exist_ok=True)
            if not load_goals(root):
                now_text = datetime.now(UTC).isoformat()
                upsert_goal(root, GoalRecord(
                    session_ref={SESSION_REF!r}, lane_id={LANE!r},
                    intent="Run the enrolled lane goal safely", goal="Run the enrolled lane goal safely", done_when="The enrolled lane records a successful run",
                    source="separate-process-contract", status="working",
                    enrolled_at=now_text, **enrollment_fields("The enrolled lane records a successful run"),
                    now="Run the lane", last_verified="", created_at=now_text, updated_at=now_text,
                ))
            noop = lambda *_args, **_kwargs: None
            result = run_lanes_once(
                Path({str(lanes_file)!r}),
                provider_factories={{"tophand": factory}},
                pending_sink=noop, cursor_sink=noop, result_sink=noop, event_sink=noop,
                checkpoint_verifier=lambda *_args: True,
                cancel_verifier=lambda *_args: True,
                facts_reader=lambda _record: facts_snapshot.facts,
            )
            print(json.dumps({{"lanes": sorted(result), "provider_create_count": Path({str(count_path)!r}).with_name("provider-create-count").read_text() if Path({str(count_path)!r}).with_name("provider-create-count").exists() else "0"}}))
            """
        ),
    )


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, capture_output=True, text=True, check=False)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses POSIX forced-command wrappers")
def test_separate_process_dispatch_registration_restart_heartbeat_and_expiry(tmp_path: Path) -> None:
    adapter_cli = _adapter_cli(tmp_path)
    fleet_state = tmp_path / "fleet-state"
    start_count = tmp_path / "fleet-start-count"
    lanes_file = tmp_path / "lanes.yaml"
    chitra_state = tmp_path / "chitra-state"
    lanes_file.write_text(
        "lanes:\n"
        f"  - id: {LANE}\n"
        "    account: chitra\n"
        "    uid: 1000\n"
        f"    home: {tmp_path / 'home'}\n"
        f"    workdir: {tmp_path}\n"
        f"    config_dir: {tmp_path / 'config'}\n"
        f"    state_dir: {chitra_state}\n"
        f"    tmux_socket: {tmp_path / 'tmux.sock'}\n"
        f"    tmux_session: {LANE}\n"
        "    credentials:\n"
        f"      claude_credentials: {tmp_path / 'credentials.json'}\n"
        f"      ssh_dispatch_key: {tmp_path / 'id_ed25519'}\n"
        "    enabled: true\n"
        "    target_host: tophand\n"
        "    target_account: ubuntu\n",
        encoding="utf-8",
    )
    enrolled = _run([
        sys.executable,
        str(_enroll_script(tmp_path / "enroll.py", chitra_state=chitra_state)),
    ])
    assert enrolled.returncode == 0, enrolled.stderr + enrolled.stdout
    goal_id = enrolled.stdout.strip().splitlines()[-1]
    assert goal_id.startswith("goal-")
    reconcile = _reconcile_script(
        tmp_path / "reconcile.py",
        adapter_cli=adapter_cli,
        state_root=fleet_state,
        count_path=start_count,
        goal_id=goal_id,
    )
    for _ in range(2):
        completed = _run([sys.executable, str(reconcile)])
        assert completed.returncode == 0, completed.stderr + completed.stdout
    assert start_count.read_text() == "1"

    registration_path = fleet_state / "tophand" / LANE / "lane-registration.json"
    assert registration_path.is_file()
    registration = json.loads(registration_path.read_text())
    assert registration["lifecycle"] == "running"
    assert registration["process"] == {
        "tmux_pane_pid": PROCESS["pane_pid"],
        "boot_id": PROCESS["boot_id"],
        "start_ticks": PROCESS["start_ticks"],
    }
    assert registration["observation"]["owner_pid"] == PROCESS["pane_pid"]

    # Fleet's launch receipt is deliberately absent.  The forced registration
    # command must still verify the target-owned record.
    forced = _forced_registration_script(
        tmp_path / "forced-registration.py",
        adapter_cli=adapter_cli,
        state_root=fleet_state,
    )
    verified = _run([sys.executable, str(forced)])
    assert verified.returncode == 0, verified.stderr + verified.stdout
    assert json.loads(verified.stdout)["registration"]["registration_sha256"]

    chitra_child = _chitra_script(
        tmp_path / "chitra-dispatch.py",
        lanes_file=lanes_file,
        count_path=start_count,
        forced_registration=forced,
        chitra_state=chitra_state,
    )
    first = _run([sys.executable, str(chitra_child)])
    assert first.returncode == 0, first.stderr + first.stdout
    second = _run([sys.executable, str(chitra_child)])
    assert second.returncode == 0, second.stderr + second.stdout
    assert json.loads(second.stdout)["provider_create_count"] == "1"

    # Exercise the Adapter lease boundary in a different process, then make
    # the target-owned record expire.  Chitra must not recreate the lane.
    process_input = tmp_path / "process.json"
    process_input.write_text(json.dumps({"process": {
        "tmux_pane_pid": PROCESS["pane_pid"],
        "boot_id": PROCESS["boot_id"],
        "start_ticks": PROCESS["start_ticks"],
    }}), encoding="utf-8")
    heartbeat = _run([
        sys.executable,
        str(ADAPTER_ROOT / "tools/support/chitra_adapter/lane_registration.py"),
        "--heartbeat", "--process", str(process_input), "--output", str(registration_path),
    ])
    assert heartbeat.returncode == 0, heartbeat.stderr + heartbeat.stdout
    time.sleep(11)
    expire = _run([
        sys.executable, "-c",
        "from datetime import UTC; "
        f"import sys; sys.path.insert(0, {str(ADAPTER_ROOT / 'tools/support/chitra_adapter')!r}); "
        "import lane_registration as r; "
        f"r.expire(__import__('pathlib').Path({str(registration_path)!r}))",
    ])
    assert expire.returncode == 0, expire.stderr + expire.stdout
    expired = json.loads(registration_path.read_text())
    assert expired["lifecycle"] == "expired"
    third = _run([sys.executable, str(chitra_child)])
    assert third.returncode == 0, third.stderr + third.stdout
    assert json.loads(third.stdout)["provider_create_count"] == "1"
