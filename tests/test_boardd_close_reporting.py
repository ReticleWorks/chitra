"""Read-only boardd projection of close and resume facts."""

from __future__ import annotations

from pathlib import Path

from _joined_report_fixtures import joined_report_record
from boardd.state import build_view
from boardd.translate import TranslationCache
from chitra.joined_lane import JoinedLaneStore
from chitra.session_contract import CloseArchiveResult, OperationReference, ProviderCapabilities


def test_boardd_preserves_close_and_resume_facts_without_provider_payload(tmp_path: Path) -> None:
    base = joined_report_record()
    provider = base.provider.model_copy(
        update={
            "capabilities": ProviderCapabilities.from_supported(
                ("create_or_resume", "send", "read_updates", "checkpoint", "close", "resume_after_close")
            )
        }
    )
    record = base.model_copy(
        update={
            "provider": provider,
            "lifecycle": "inactive",
            "checkpoint_reference": "checkpoint-ramble",
            "operation_history": (
                OperationReference(
                    operation_id="close-ramble",
                    idempotency_key="close-ramble-idem",
                    payload_digest="close-ramble-digest",
                    kind="close",
                    created_at="2026-08-23T14:19:00+00:00",
                ),
            ),
            "last_close_result": CloseArchiveResult(
                operation_id="close-ramble",
                lane_id="ramble-build",
                provider_handle="tophand-ramble",
                provider_instance_id="instance-1",
                provider_generation=1,
                idempotency_key="close-ramble-idem",
                payload_digest="close-ramble-digest",
                state="archived",
                provider_thread_ref="tophand-ramble",
                provider_session_id=None,
                same_provider_thread=True,
                later_resume_supported=True,
                checkpoint_ref="checkpoint-ramble",
                quiescent=True,
                observed_at="2026-08-23T14:20:00+00:00",
                evidence="archive receipt",
            ),
        }
    )
    JoinedLaneStore(tmp_path).create(record)
    (tmp_path / "goals.json").write_text('{"goals": []}')
    (tmp_path / "sweep-digest.json").write_text('{"events": []}')

    report = build_view(tmp_path, TranslationCache(None))["lanes"][0]["joined_session"]

    assert report["close_evidence"] == {
        "state": "archived",
        "provider_thread_ref": "tophand-ramble",
        "same_provider_thread": True,
        "later_resume_supported": True,
        "checkpoint_ref": "checkpoint-ramble",
        "quiescent": True,
        "observed_at": "2026-08-23T14:20:00+00:00",
        "evidence": {"text": "archive receipt", "raw": "archive receipt", "translated": False},
    }
    assert report["resume_state"] == "closed; same-session resume available"
    assert "payload_digest" not in str(report)
