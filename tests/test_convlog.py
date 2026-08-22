"""Tests for deterministic operator-brief rendering and conversation logging."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from chitra.convlog import (
    BriefValidationError,
    OperatorBrief,
    append_directive,
    append_entry,
    append_operator_brief,
    append_resolution,
    append_ruling,
    append_session_message,
    entries_for_thread,
    list_threads,
    main,
    open_thread,
    pending_threads,
    read_entries,
    read_stored_brief,
    render_brief,
    render_group,
    validate_brief,
)
from chitra.evidence import EvidenceHandle


class _AcceptingResolver:
    """Stub resolver for tests that exercise structure, not evidence lookup."""

    def resolve(self, handle: EvidenceHandle) -> str | None:
        return None


class _RejectingResolver:
    def __init__(self, reason: str) -> None:
        self.reason = reason

    def resolve(self, handle: EvidenceHandle) -> str | None:
        return self.reason


def _attempt(action: str, result: str, evidence: object) -> dict[str, object]:
    return {"action": action, "result": result, "evidence": evidence}


def _write_ledger(path: Path, *order_ids: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        {"order_id": order_id, "session_ref": "host-b:feeds:0.0", "tag": "follow-up", "sent_at": "2026-08-22T09:00:00+00:00"}
        for order_id in order_ids
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_self_transcript(path: Path, *, command: str, exit_code: int, tool_use_id: str = "tu_capture_1") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"type": "tool_use", "id": tool_use_id, "name": "Bash", "input": {"command": command}},
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": f"pane text\nexit code: {exit_code}"},
    ]
    path.write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")


def _real_evidence_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Write a real ledger and self transcript the default resolver can check."""
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-feeds-layout-1")
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-tmux-capture feeds-reader --pane harness-pi", exit_code=0)
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))
    return state


def _payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "session_ref": "host-b:feeds:0.0",
        "program": "Feeds digest redesign (F2)",
        "subject": "Feeds digest compiler",
        "progress": "implementation-ready; final interface choice pending",
        "stage": "The implementation is ready for the final interface choice.",
        "category": "decision",
        "decision": "Should the digest ship as one combined feed?",
        "recommendation": "Ship one combined feed because the tested readers preferred it.",
        "recommendation_basis": "research",
        "options": [
            {"label": "Combined feed", "consequence": "Readers get one ranked digest."},
            {"label": "Separate feeds", "consequence": "Readers choose a source first."},
        ],
        "exhaustion": {
            "reason": "attempts_exhausted",
            "attempts": [
                {
                    "action": "Shipped the combined-feed prototype to the reader test",
                    "result": "it passed the preference check.",
                    "evidence": {"kind": "command", "ref": "chitra-tmux-capture feeds-reader --pane harness-pi", "exit_status": 0},
                },
                {
                    "action": "Asked the work session to pick a default feed layout",
                    "result": "it deferred the product call.",
                    "evidence": {"kind": "order", "ref": "ord-feeds-layout-1"},
                },
            ],
            "residual_blocker": "Only the operator can pick between one combined feed and separate feeds.",
        },
        "source_quote": ["The combined prototype passed the reader test.", "I need the operator's product decision."],
        "source_ref": "transcripts/feeds.jsonl",
    }
    payload.update(changes)
    return payload


def _brief(**changes: object) -> OperatorBrief:
    return validate_brief(_payload(**changes), evidence_resolver=_AcceptingResolver())


def test_valid_brief_round_trip_render_log_and_read_back(tmp_path: Path) -> None:
    brief = _brief()
    thread_id = open_thread(
        tmp_path / "conversation.jsonl", brief=brief, raw_text="Full raw session message.", evidence_resolver=_AcceptingResolver()
    )

    entries = entries_for_thread(tmp_path / "conversation.jsonl", thread_id)
    assert [entry.kind for entry in entries] == ["session_msg", "operator_brief"]
    assert entries[0].payload == {"text": "Full raw session message.", "source_ref": brief.source_ref}
    assert entries[1].schema_ == "chitra.convlog.v2"
    assert validate_brief(entries[1].payload["brief"], evidence_resolver=_AcceptingResolver()) == brief
    assert entries[1].payload["brief"]["subject"] == "Feeds digest compiler"
    assert entries[1].payload["brief"]["progress"] == "implementation-ready; final interface choice pending"
    assert entries[1].payload["rendered"] == render_brief(brief)
    assert not list(tmp_path.glob("*.tmp"))


def test_v1_entry_loads_with_empty_grounding_fields(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    legacy_brief = _payload()
    legacy_brief.pop("subject")
    legacy_brief.pop("progress")
    legacy_entry = {
        "schema": "chitra.convlog.v1",
        "thread_id": "legacy-thread",
        "seq": 1,
        "kind": "operator_brief",
        "at": "2026-07-11T12:00:00+00:00",
        "session_ref": "host-b:feeds:0.0",
        "payload": {"brief": legacy_brief, "rendered": "legacy rendered brief"},
    }
    path.write_text(json.dumps(legacy_entry) + "\n", encoding="utf-8")

    entries = read_entries(path)

    assert entries[0].schema_ == "chitra.convlog.v1"
    loaded_brief = validate_brief(entries[0].payload["brief"], evidence_resolver=_AcceptingResolver())
    assert loaded_brief.subject == ""
    assert loaded_brief.progress == ""
    assert render_brief(loaded_brief).splitlines()[0] == "This is Feeds digest redesign (F2) (host-b:feeds:0.0)."


@pytest.mark.parametrize("program", ["F2", "fix-6", "1-109", "host-b:F2:1"])
def test_program_rejects_bare_codenames_and_session_refs(program: str) -> None:
    with pytest.raises(BriefValidationError, match="plain-language program"):
        validate_brief(_payload(program=program))


def test_program_accepts_plain_language_name_with_codename() -> None:
    assert _brief().program == "Feeds digest redesign (F2)"


def test_decision_without_recommendation_requires_research_first() -> None:
    with pytest.raises(BriefValidationError, match="monitor does the research first"):
        _brief(recommendation="")


def _v2_entry(brief_payload: dict[str, object], thread_id: str = "pre-wp4") -> dict[str, object]:
    return {
        "schema": "chitra.convlog.v2",
        "thread_id": thread_id,
        "seq": 1,
        "kind": "operator_brief",
        "at": "2026-07-11T12:00:00+00:00",
        "session_ref": brief_payload["session_ref"],
        "payload": {"brief": brief_payload, "rendered": "stored rendering"},
    }


def test_decision_brief_requires_exhaustion_record() -> None:
    with pytest.raises(BriefValidationError, match="exhaustion record"):
        _brief(exhaustion=None)


def test_missing_exhaustion_message_leads_with_attempts_exhausted() -> None:
    with pytest.raises(BriefValidationError) as excinfo:
        _brief(exhaustion=None)
    message = str(excinfo.value)
    assert message.index("attempts_exhausted") < message.index("credential")
    assert message.index("credential") < message.index("irreversible_consent")
    assert message.index("irreversible_consent") < message.index("operator_decision")


def test_invalid_brief_on_write_path_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"

    with pytest.raises(BriefValidationError, match="exhaustion record"):
        open_thread(path, brief=_brief(exhaustion=None), raw_text="raw")

    assert read_entries(path) == []


def test_pre_wp4_decision_brief_still_surfaces_after_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    legacy = _payload()
    del legacy["exhaustion"]
    path.write_text(json.dumps(_v2_entry(legacy)) + "\n", encoding="utf-8")

    with pytest.raises(BriefValidationError, match="exhaustion record"):
        validate_brief(dict(legacy))

    entries = read_entries(path)
    assert len(entries) == 1
    threads = list_threads(path)
    assert [thread.thread_id for thread in threads] == ["pre-wp4"]
    assert [thread.thread_id for thread in pending_threads(path)] == ["pre-wp4"]
    assert threads[0].latest_brief.decision == "Should the digest ship as one combined feed?"
    assert threads[0].latest_brief.exhaustion is None


def test_stored_records_load_leniently_under_tightened_rules(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    stored = _payload()
    stored["exhaustion"] = {"reason": "credential", "attempts": [], "residual_blocker": "Needs key"}
    stored["progress"] = "Waiting on the vendor contract; you must sign the amendment today."
    path.write_text(json.dumps(_v2_entry(stored)) + "\n", encoding="utf-8")

    entries = read_entries(path)

    assert len(entries) == 1
    threads = list_threads(path)
    assert threads[0].pending is True
    assert threads[0].latest_brief.exhaustion is not None
    assert threads[0].latest_brief.exhaustion.residual_blocker == "Needs key"


def test_read_stored_brief_skips_write_path_gates() -> None:
    payload = _payload(
        category="milestone", decision=None, recommendation="Please run the migration by hand tonight.", options=[], exhaustion=None
    )

    loaded = read_stored_brief(dict(payload))

    assert loaded.decision is None
    with pytest.raises(BriefValidationError, match="operator-directed ask"):
        validate_brief(dict(payload))


@pytest.mark.parametrize(
    ("field", "text"),
    [
        ("recommendation", "Please install the patched build on host-b tonight."),
        ("stage", "The vendor amendment is out for signature; you must sign it today."),
        ("progress", "The migration is staged; operator needs to approve the window."),
    ],
)
def test_smuggled_ask_in_any_decisionless_field_is_rejected(field: str, text: str) -> None:
    kwargs: dict[str, object] = {"category": "milestone", "decision": None, "recommendation": "", "options": [], "exhaustion": None}
    kwargs[field] = text
    with pytest.raises(BriefValidationError, match="operator-directed ask") as excinfo:
        _brief(**kwargs)
    assert field in str(excinfo.value)


def test_clean_decisionless_brief_passes_the_ask_scan() -> None:
    brief = _brief(
        category="incident",
        decision=None,
        recommendation="The retry queue drained on its own; no action is pending.",
        options=[],
        exhaustion=None,
    )

    assert brief.decision is None


def test_smuggled_ask_scan_never_reads_source_quote() -> None:
    brief = _brief(
        category="milestone",
        decision=None,
        recommendation="",
        source_quote=["Can you confirm the deploy window?"],
        options=[],
        exhaustion=None,
    )

    assert brief.source_quote == ["Can you confirm the deploy window?"]


def test_smuggled_ask_scan_skips_decision_bearing_briefs() -> None:
    brief = _brief(progress="Waiting on the vendor contract; you must sign the amendment today.")

    assert brief.decision is not None


def test_valid_attempts_exhausted_brief_passes_and_renders_tried() -> None:
    brief = _brief()

    assert brief.exhaustion is not None
    assert brief.exhaustion.reason == "attempts_exhausted"
    rendered = render_brief(brief)
    assert "TRIED:" in rendered
    assert "- Shipped the combined-feed prototype to the reader test -> it passed the preference check." in rendered
    assert "Only the operator can pick between one combined feed and separate feeds." in rendered


def test_single_attempt_attempts_exhausted_fails() -> None:
    with pytest.raises(BriefValidationError, match="at least 2 distinct attempts"):
        _brief(
            exhaustion={
                "reason": "attempts_exhausted",
                "attempts": [{"action": "Ran the reprovision command by hand again", "result": "blocked by the held lock."}],
                "residual_blocker": "The held lock still blocks every retry.",
            }
        )


@pytest.mark.parametrize(
    "attempt",
    [
        {"action": "   ", "result": "the pipeline refused the token outright."},
        {"action": "Asked the work session for an explicit ruling", "result": ""},
        {"action": "Presented the retry token twice more"},
    ],
)
def test_attempt_action_and_result_must_be_non_blank(attempt: dict[str, str]) -> None:
    with pytest.raises(BriefValidationError):
        _brief(
            exhaustion={
                "reason": "attempts_exhausted",
                "attempts": [attempt, {"action": "Shipped the fixed build to staging overnight", "result": "it deployed cleanly."}],
                "residual_blocker": "The held lock still blocks every retry.",
            }
        )


def test_legacy_string_attempt_coerces_on_the_last_arrow() -> None:
    attempt = "Retried the deploy twice by hand after the first refusal -> second try -> it hit the same lock."
    payload = _payload(
        exhaustion={
            "reason": "attempts_exhausted",
            "attempts": [attempt, {"action": "Shipped the fixed build to staging overnight", "result": "it deployed cleanly."}],
            "residual_blocker": "The held lock still blocks every retry.",
        },
        recommendation_basis="research",
    )

    brief = OperatorBrief.model_validate(payload)

    assert brief.exhaustion is not None
    assert brief.exhaustion.attempts[0].action == "Retried the deploy twice by hand after the first refusal -> second try"
    assert brief.exhaustion.attempts[0].result == "it hit the same lock."
    assert brief.exhaustion.attempts[0].evidence is None
    with pytest.raises(BriefValidationError, match="evidence handle missing"):
        validate_brief(payload, evidence_resolver=_AcceptingResolver())


def test_attempt_without_evidence_handle_is_rejected() -> None:
    with pytest.raises(BriefValidationError, match="attempt 1: evidence handle missing"):
        _brief(
            exhaustion={
                "reason": "attempts_exhausted",
                "attempts": [{"action": "Ran the reprovision command by hand again", "result": "blocked by the held lock."}],
                "residual_blocker": "The held lock still blocks every retry.",
            }
        )


def test_duplicate_attempts_are_rejected() -> None:
    repeated = _attempt("ran the reprovision command", "blocked by the held lock", {"kind": "order", "ref": "ord-lock-1"})
    with pytest.raises(BriefValidationError, match="distinct"):
        _brief(
            exhaustion={
                "reason": "attempts_exhausted",
                "attempts": [repeated, repeated],
                "residual_blocker": "The held lock still blocks every retry.",
            }
        )


def test_dedup_normalizes_case_and_whitespace_before_comparing() -> None:
    first = _attempt("Ran the deploy script once more", "it hit the held lock.", {"kind": "order", "ref": "ord-deploy-1"})
    second = _attempt("ran THE deploy script ONCE  MORE", "it hit the held lock.", {"kind": "order", "ref": "ord-deploy-2"})
    third = _attempt("Asked the work session for a ruling", "it punted back to us.", {"kind": "order", "ref": "ord-ask-1"})

    with pytest.raises(BriefValidationError, match="without case or extra whitespace"):
        _brief(
            exhaustion={
                "reason": "attempts_exhausted",
                "attempts": [first, second, third],
                "residual_blocker": "The held lock still blocks every retry.",
            }
        )

    brief = _brief(
        exhaustion={
            "reason": "attempts_exhausted",
            "attempts": [first, third],
            "residual_blocker": "The held lock still blocks every retry.",
        }
    )
    assert brief.exhaustion is not None


@pytest.mark.parametrize(
    "attempt", ["Ran the deploy script once more\nit hit the held lock.", "Ran the deploy script once more\rit hit the held lock."]
)
def test_newlines_in_attempts_are_rejected(attempt: str) -> None:
    with pytest.raises(BriefValidationError, match="one line"):
        _brief(
            exhaustion={
                "reason": "attempts_exhausted",
                "attempts": [attempt, "Shipped the fixed build to staging overnight -> it deployed cleanly."],
                "residual_blocker": "The held lock still blocks every retry.",
            }
        )


def test_newline_in_residual_blocker_is_rejected() -> None:
    blocker = "Only the operator can pick between the two feeds.\rOr can they?"
    with pytest.raises(BriefValidationError, match="one line"):
        _brief(exhaustion={"reason": "attempts_exhausted", "attempts": _payload()["exhaustion"]["attempts"], "residual_blocker": blocker})  # type: ignore[arg-type]


def test_exhaustion_fields_are_evidence_and_skip_the_jargon_filter() -> None:
    attempts = [
        _attempt(
            "Nudged the lane about the held deploy lock",
            "the nudge found the lane still blocked by the lock.",
            {"kind": "order", "ref": "ord-lock-nudge-1"},
        ),
        "Ran the reprovision command by hand again -> blocked by the held lock",
    ]
    payload = _payload(
        exhaustion={
            "reason": "attempts_exhausted",
            "attempts": attempts,
            "residual_blocker": "Still stuck on the same held deploy lock",
        }
    )

    brief = OperatorBrief.model_validate(payload)

    assert [attempt.action for attempt in brief.exhaustion.attempts] == [
        attempts[0]["action"],
        "Ran the reprovision command by hand again",
    ]  # type: ignore[index]
    assert [attempt.result for attempt in brief.exhaustion.attempts] == [
        attempts[0]["result"],
        "blocked by the held lock",
    ]  # type: ignore[index]
    with pytest.raises(BriefValidationError, match="evidence handle missing"):
        validate_brief(payload, evidence_resolver=_AcceptingResolver())


def test_credential_reason_with_truthful_record_passes_without_keywords() -> None:
    attempt = _attempt(
        "Presented the retry token to the registry",
        "the registry refused it outright.",
        {"kind": "verb_refusal", "ref": "chitra-registry-push"},
    )
    blocker = "Only a human account can approve this access today."
    brief = _brief(recommendation_basis="research", exhaustion={"reason": "credential", "attempts": [attempt], "residual_blocker": blocker})

    assert brief.exhaustion is not None
    assert brief.exhaustion.attempts[0].action == "Presented the retry token to the registry"
    rendered = render_brief(brief)
    assert "TRIED:" in rendered
    assert "- Presented the retry token to the registry -> the registry refused it outright." in rendered
    assert f"OPERATOR-ONLY BECAUSE: {blocker}" in rendered


def test_credential_reason_requires_one_observed_refusal() -> None:
    with pytest.raises(BriefValidationError, match="requires at least 1 attempt"):
        _brief(exhaustion={"reason": "credential", "attempts": [], "residual_blocker": "The credential gate needs a human."})

    stuffed = "credential credential credential"
    with pytest.raises(BriefValidationError, match="requires at least 1 attempt"):
        _brief(exhaustion={"reason": "credential", "attempts": [], "residual_blocker": stuffed})


def test_credential_reason_validates_present_attempts_identically() -> None:
    with pytest.raises(BriefValidationError, match="must be non-empty"):
        _brief(
            exhaustion={
                "reason": "credential",
                "attempts": ["Tried the access path once more without recording what came back"],
                "residual_blocker": "Only a human account can approve this access today.",
            }
        )


@pytest.mark.parametrize(
    ("basis", "matches"),
    [("operator-preference", None), ("research", r'"operator-preference"')],
)
def test_operator_decision_reason_binds_to_recommendation_basis(basis: str, matches: str | None) -> None:
    blocker = "Picking the vendor name is a pure preference call nobody else can make."
    exhaustion = {
        "reason": "operator_decision",
        "attempts": [],
        "residual_blocker": blocker,
    }
    if matches is None:
        brief = _brief(recommendation="", recommendation_basis=basis, exhaustion=exhaustion)
        assert f"OPERATOR-ONLY BECAUSE: {blocker}" in render_brief(brief)
    else:
        with pytest.raises(BriefValidationError, match=matches):
            _brief(recommendation_basis=basis, exhaustion=exhaustion)


def test_irreversible_consent_reason_allows_missing_attempts_and_needs_a_real_blocker() -> None:
    blocker = "Sending the announcement email cannot be undone once it leaves the outbox."
    brief = _brief(exhaustion={"reason": "irreversible_consent", "attempts": [], "residual_blocker": blocker})

    assert brief.exhaustion is not None
    rendered = render_brief(brief)
    assert "TRIED" not in rendered
    assert f"OPERATOR-ONLY BECAUSE: {blocker}" in rendered

    with pytest.raises(BriefValidationError, match="at least 20 characters"):
        _brief(exhaustion={"reason": "irreversible_consent", "attempts": [], "residual_blocker": "Cannot undo."})


def test_credential_reason_with_attempts_renders_tried_and_operator_only_line() -> None:
    attempt = _attempt(
        "Presented the retry token to the registry",
        "the registry refused it outright.",
        {"kind": "verb_refusal", "ref": "chitra-registry-push"},
    )
    blocker = "Only a human account can approve this access today."
    brief = _brief(exhaustion={"reason": "credential", "attempts": [attempt], "residual_blocker": blocker})
    rendered = render_brief(brief).splitlines()

    assert rendered[rendered.index("TRIED:") + 1] == "- Presented the retry token to the registry -> the registry refused it outright."
    assert f"OPERATOR-ONLY BECAUSE: {blocker}" in rendered
    assert rendered.index("TRIED:") < rendered.index(f"OPERATOR-ONLY BECAUSE: {blocker}") < rendered.index("— from the session, verbatim —")


def test_decisionless_brief_may_omit_exhaustion() -> None:
    brief = _brief(category="milestone", decision=None, recommendation="", options=[], exhaustion=None)

    assert brief.exhaustion is None
    assert "TRIED" not in render_brief(brief)


def test_operator_preference_basis_allows_no_recommendation() -> None:
    brief = _brief(recommendation="", recommendation_basis="operator-preference")
    assert "Recommendation: your call — no research applies." in render_brief(brief)


@pytest.mark.parametrize("quotes", [[], ["one"] * 5, ["x" * 401]])
def test_source_quote_bounds_are_enforced(quotes: list[str]) -> None:
    with pytest.raises(BriefValidationError):
        _brief(source_quote=quotes)


def test_category_decision_requires_decision_but_milestone_may_ask() -> None:
    with pytest.raises(BriefValidationError, match="category is decision"):
        _brief(decision=None)

    milestone = _brief(category="milestone")
    assert render_brief(milestone).splitlines()[1].startswith("🔴 ")


def test_render_snapshots() -> None:
    assert render_brief(_brief()) == (
        "This is Feeds digest redesign (F2) (host-b:feeds:0.0) working on Feeds digest compiler: "
        "implementation-ready; final interface choice pending.\n"
        "🔴 Feeds digest redesign (F2) (host-b:feeds:0.0) — needs you: Should the digest ship as one combined feed?\n"
        "Stage: The implementation is ready for the final interface choice.\n"
        "Recommendation: Ship one combined feed because the tested readers preferred it.\n"
        "Options (reply by number):\n"
        "  1. Combined feed — Readers get one ranked digest.\n"
        "  2. Separate feeds — Readers choose a source first.\n"
        "TRIED:\n"
        "- Shipped the combined-feed prototype to the reader test -> it passed the preference check.\n"
        "- Asked the work session to pick a default feed layout -> it deferred the product call.\n"
        "Only the operator can pick between one combined feed and separate feeds.\n"
        "— from the session, verbatim —\n"
        "> The combined prototype passed the reader test.\n"
        "> I need the operator's product decision."
    )
    fyi = _brief(category="fyi", decision=None, recommendation="", options=[], exhaustion=None)
    assert render_brief(fyi) == (
        "This is Feeds digest redesign (F2) (host-b:feeds:0.0) working on Feeds digest compiler: "
        "implementation-ready; final interface choice pending.\n"
        "🟦 Feeds digest redesign (F2) (host-b:feeds:0.0) — fyi; nothing to answer yet.\n"
        "Stage: The implementation is ready for the final interface choice.\n"
        "— from the session, verbatim —\n"
        "> The combined prototype passed the reader test.\n"
        "> I need the operator's product decision."
    )


def test_decisionless_brief_says_nothing_is_ready_to_answer() -> None:
    brief = _brief(category="milestone", decision=None, recommendation="I will return with a recommendation.", options=[])

    rendered = render_brief(brief)

    assert "nothing to answer yet" in rendered
    assert "needs you:" not in rendered


def test_render_group_numbers_briefs(tmp_path: Path) -> None:
    first = _brief()
    second = _brief(session_ref="host-b:other:0.0", program="Other program (F3)")
    first_thread = open_thread(tmp_path / "conversation.jsonl", brief=first, raw_text="first", evidence_resolver=_AcceptingResolver())
    second_thread = open_thread(tmp_path / "conversation.jsonl", brief=second, raw_text="second", evidence_resolver=_AcceptingResolver())
    grouped = render_group(pending_threads(tmp_path / "conversation.jsonl"), now=datetime.now(UTC))

    assert first_thread in {thread.thread_id for thread in pending_threads(tmp_path / "conversation.jsonl")}
    assert second_thread in {thread.thread_id for thread in pending_threads(tmp_path / "conversation.jsonl")}
    assert grouped.startswith("[1] — open 0m\n  This is")
    assert "\n  🔴" in grouped
    assert "\n\n[2] — open 0m\n  This is" in grouped


def test_cli_four_rung_lifecycle_show_list_and_pending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "conversation.jsonl"
    state = _real_evidence_state(tmp_path, monkeypatch)
    transcript_arg = ["--self-transcript", str(state / "self-transcript.jsonl")]
    json_path = tmp_path / "brief.json"
    json_path.write_text(json.dumps(_payload()), encoding="utf-8")
    assert (
        main(
            [
                "brief",
                "--convlog-path",
                str(path),
                "--session-ref",
                "host-b:feeds:0.0",
                "--json",
                str(json_path),
                "--raw",
                "raw",
                *transcript_arg,
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert captured.out.startswith("This is")
    thread_id = captured.err.strip().removeprefix("thread=")

    assert main(["pending", "--convlog-path", str(path)]) == 0
    assert "[1] — open" in capsys.readouterr().out
    assert main(["rule", "--convlog-path", str(path), "--thread", thread_id, "--text", "Ship option 1."]) == 0
    assert (
        main(["directive", "--convlog-path", str(path), "--thread", thread_id, "--text", "Ship the combined feed.", "--order-id", "ord-1"])
        == 0
    )
    assert main(["show", "--convlog-path", str(path), "--thread", thread_id]) == 0
    shown = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["kind"] for line in shown] == ["session_msg", "operator_brief", "operator_ruling", "lane_directive"]
    assert main(["list", "--convlog-path", str(path)]) == 0
    assert "\truled\t" in capsys.readouterr().out
    assert main(["pending", "--convlog-path", str(path)]) == 0
    assert capsys.readouterr().out == "No pending decisions.\n"


def test_batch_rule_and_revision_make_latest_brief_authoritative(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "conversation.jsonl"
    state = _real_evidence_state(tmp_path, monkeypatch)
    first = open_thread(path, brief=_brief(), raw_text="first", evidence_resolver=_AcceptingResolver())
    second = open_thread(
        path,
        brief=_brief(session_ref="host-b:second:0.0", program="Second program (F3)"),
        raw_text="second",
        evidence_resolver=_AcceptingResolver(),
    )

    assert main(["rule", "--convlog-path", str(path), "--thread", first, "--thread", second, "--text", "Proceed.", "--via", "slack"]) == 0
    assert pending_threads(path) == []
    revised = _brief(decision="Should the combined feed include alerts?", recommendation="No; keep alerts separate.")
    revised_path = tmp_path / "revised.json"
    revised_path.write_text(json.dumps(revised.model_dump()), encoding="utf-8")
    assert (
        main(
            [
                "brief",
                "--convlog-path",
                str(path),
                "--thread",
                first,
                "--session-ref",
                revised.session_ref,
                "--json",
                str(revised_path),
                "--self-transcript",
                str(state / "self-transcript.jsonl"),
            ]
        )
        == 0
    )
    assert "thread=" + first in capsys.readouterr().err
    pending = pending_threads(path)
    assert [thread.thread_id for thread in pending] == [first]
    assert pending[0].latest_brief.decision == "Should the combined feed include alerts?"


def test_decisionless_followup_does_not_retire_pending_ask(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    thread_id = open_thread(path, brief=_brief(), raw_text="raw", evidence_resolver=_AcceptingResolver())
    follow_up = _brief(
        category="milestone", decision=None, recommendation="I will return with a recommendation.", options=[], exhaustion=None
    )
    append_operator_brief(path, thread_id=thread_id, brief=follow_up)

    threads = list_threads(path)

    assert [thread.thread_id for thread in pending_threads(path)] == [thread_id]
    assert threads[0].state == "pending"
    assert threads[0].latest_brief.decision == "Should the digest ship as one combined feed?"


def test_newer_decision_bearing_brief_replaces_pending_ask(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    thread_id = open_thread(path, brief=_brief(), raw_text="raw", evidence_resolver=_AcceptingResolver())
    revised = _brief(decision="Should the combined feed include alerts?", recommendation="No; keep alerts separate.")
    append_operator_brief(path, thread_id=thread_id, brief=revised, evidence_resolver=_AcceptingResolver())

    threads = list_threads(path)

    assert [thread.thread_id for thread in pending_threads(path)] == [thread_id]
    assert threads[0].latest_brief.decision == "Should the combined feed include alerts?"
    assert threads[0].latest_brief_at > threads[0].opened_at


def test_decisionless_only_thread_is_never_pending(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    brief = _brief(category="fyi", decision=None, recommendation="", options=[], exhaustion=None)
    open_thread(path, brief=brief, raw_text="raw")

    assert list_threads(path)[0].latest_brief.decision is None
    assert pending_threads(path) == []


def test_malformed_lines_are_skipped_and_sequence_is_monotonic(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    thread_id = "abc123"
    append_session_message(path, thread_id=thread_id, session_ref="host-b:f:0", text="raw", source_ref="source")
    path.write_text(path.read_text(encoding="utf-8") + "not json\n", encoding="utf-8")
    append_entry(path, thread_id=thread_id, kind="operator_ruling", session_ref="host-b:f:0", payload={"text": "ok", "via": "chat"})

    assert [entry.seq for entry in read_entries(path)] == [1, 2]


def test_pending_age_rendering_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    brief = _brief()
    append_session_message(path, thread_id="age-test", session_ref=brief.session_ref, text="raw", source_ref=brief.source_ref)
    append_entry(
        path,
        thread_id="age-test",
        kind="operator_brief",
        session_ref=brief.session_ref,
        payload={"brief": brief.model_dump(), "rendered": render_brief(brief)},
        at="2026-07-11T10:00:00+00:00",
    )
    now = datetime(2026, 7, 11, 13, 30, tzinfo=UTC)
    assert "[1] — open 3h" in render_group(pending_threads(path), now=now)


def test_direct_helpers_complete_four_rungs(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    thread_id = open_thread(path, brief=_brief(), raw_text="raw", evidence_resolver=_AcceptingResolver())
    append_ruling(path, thread_id=thread_id, text="yes")
    append_directive(path, thread_id=thread_id, text="do it", order_id=None)
    assert list_threads(path)[0].pending is False


@pytest.mark.parametrize("state", ["moot", "superseded"])
def test_event_resolution_retires_thread_with_truthful_basis(tmp_path: Path, state: str) -> None:
    path = tmp_path / "conversation.jsonl"
    thread_id = open_thread(path, brief=_brief(), raw_text="raw", evidence_resolver=_AcceptingResolver())
    append_resolution(
        path,
        thread_id=thread_id,
        state=state,  # type: ignore[arg-type]
        basis="The incident ended before an operator ruling was needed.",
        citation="evidence/incident-health.json#healthy",
        authority="The monitor applied the incident-resolution rule.",
    )

    thread = list_threads(path)[0]
    assert thread.pending is False
    assert thread.state == state
    assert pending_threads(path) == []
    assert thread.entries[-1].payload["citation"] == "evidence/incident-health.json#healthy"


def test_cli_can_mark_multiple_threads_moot(tmp_path: Path) -> None:
    path = tmp_path / "conversation.jsonl"
    first = open_thread(path, brief=_brief(), raw_text="first", evidence_resolver=_AcceptingResolver())
    second = open_thread(
        path,
        brief=_brief(session_ref="host-b:other:0.0", program="Other program (F3)"),
        raw_text="second",
        evidence_resolver=_AcceptingResolver(),
    )
    assert (
        main(
            [
                "retire",
                "--convlog-path",
                str(path),
                "--thread",
                first,
                "--thread",
                second,
                "--state",
                "moot",
                "--basis",
                "Events resolved both questions.",
                "--citation",
                "evidence/current-state.md",
                "--authority",
                "The monitor applied the operator's standing guidance.",
            ]
        )
        == 0
    )
    assert [thread.state for thread in list_threads(path)] == ["moot", "moot"]


def _fabricated_15_payload(*, order_ref: object) -> dict[str, object]:
    """The critique's finding 1.5 incident: invented capture plus invented ledger order."""
    return _payload(
        decision="Should I ask you to run the npm install on tophand, or wait for the package pin?",
        recommendation="Ask you for the install because the pin blocks every retry.",
        exhaustion={
            "reason": "attempts_exhausted",
            "attempts": [
                _attempt(
                    "Ran chitra-tmux-capture harness-pi over the grant",
                    "the pane shows the dependency prompt.",
                    {"kind": "command", "ref": "chitra-tmux-capture harness-pi", "exit_status": 0},
                ),
                _attempt(
                    "Queued follow-up message to harness-pi",
                    "no new turn in its transcript after ten minutes.",
                    {"kind": "order", "ref": order_ref},
                ),
            ],
            "residual_blocker": "The install needs a shell on tophand that the grant does not expose.",
        },
    )


def test_fabricated_brief_from_finding_15_is_rejected_at_write_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-unrelated-1")
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-status --json", exit_code=0)
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))

    with pytest.raises(BriefValidationError) as excinfo:
        validate_brief(_fabricated_15_payload(order_ref="9f2c"), self_transcript=state / "self-transcript.jsonl")

    assert "attempt 2: order 9f2c not found in ledger" in str(excinfo.value)


def test_same_brief_with_real_order_and_capture_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-npm-followup-1")
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-tmux-capture harness-pi", exit_code=0)
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))

    brief = validate_brief(_fabricated_15_payload(order_ref="ord-npm-followup-1"), self_transcript=state / "self-transcript.jsonl")

    assert brief.exhaustion is not None and len(brief.exhaustion.attempts) == 2


def test_command_evidence_with_wrong_exit_status_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-feeds-layout-1")
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-tmux-capture feeds-reader --pane harness-pi", exit_code=3)
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))
    payload = _payload()

    with pytest.raises(BriefValidationError, match=r"command 'chitra-tmux-capture feeds-reader --pane harness-pi' exited 3"):
        validate_brief(payload, self_transcript=state / "self-transcript.jsonl")


def test_command_evidence_without_any_capture_in_the_transcript_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-feeds-layout-1")
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-status --json", exit_code=0)
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))

    with pytest.raises(BriefValidationError, match="no Bash tool use mentioning 'chitra-tmux-capture feeds-reader"):
        validate_brief(_payload(), self_transcript=state / "self-transcript.jsonl")


def test_command_evidence_without_a_recorded_result_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-feeds-layout-1")
    (state / "self-transcript.jsonl").write_text(
        json.dumps(
            {"type": "tool_use", "id": "tu_1", "name": "Bash", "input": {"command": "chitra-tmux-capture feeds-reader --pane harness-pi"}}
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))

    with pytest.raises(BriefValidationError, match="no recorded result in self transcript"):
        validate_brief(_payload(), self_transcript=state / "self-transcript.jsonl")


def test_transcript_evidence_checks_path_and_line_digest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-x")
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))
    quoted = "the lane answered: the feed layout is the operator's product call."
    transcript = tmp_path / "lane-session.jsonl"
    transcript.write_text(quoted + "\n", encoding="utf-8")
    digest = hashlib.sha256(quoted.encode("utf-8")).hexdigest()

    attempt = _attempt(
        "Read the lane session transcript",
        "it deferred the product call.",
        {"kind": "transcript", "ref": str(transcript), "sha256": digest},
    )
    payload = _payload(
        exhaustion={"reason": "attempts_exhausted", "attempts": [attempt], "residual_blocker": "Only the operator can pick."}
    )
    payload["exhaustion"]["attempts"].append(
        _attempt("Shipped the combined-feed prototype", "it passed the preference check.", {"kind": "order", "ref": "ord-x"})
    )

    assert validate_brief(payload).exhaustion is not None

    payload["exhaustion"]["attempts"][0]["evidence"]["sha256"] = "0" * 64  # type: ignore[index]
    with pytest.raises(BriefValidationError, match=f"no line in {transcript} hashes to"):
        validate_brief(payload)

    payload["exhaustion"]["attempts"][0]["evidence"]["ref"] = str(tmp_path / "missing.jsonl")  # type: ignore[index]
    with pytest.raises(BriefValidationError, match="transcript not found"):
        validate_brief(payload)


@pytest.mark.parametrize("exit_code,expected", [(1, None), (0, "did not fail in self transcript; no refusal was observed")])
def test_verb_refusal_evidence_requires_an_observed_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_code: int, expected: str | None
) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-feeds-layout-1")
    _write_self_transcript(
        state / "self-transcript.jsonl", command="chitra grant chitra-registry-push --token retry-1", exit_code=exit_code
    )
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))
    attempt = _attempt(
        "Presented the retry token to the registry",
        "the registry refused it outright.",
        {"kind": "verb_refusal", "ref": "chitra-registry-push"},
    )
    payload = _payload(
        recommendation_basis="research",
        exhaustion={
            "reason": "credential",
            "attempts": [attempt],
            "residual_blocker": "Only a human account can approve this access today.",
        },
    )
    if expected is None:
        assert validate_brief(payload, self_transcript=state / "self-transcript.jsonl").exhaustion is not None
    else:
        with pytest.raises(BriefValidationError, match=f"grant verb 'chitra-registry-push' {expected}"):
            validate_brief(payload, self_transcript=state / "self-transcript.jsonl")


@pytest.mark.parametrize("digest", [None, "deadbeef"])
def test_transcript_evidence_requires_a_valid_sha256(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, digest: str | None) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-x")
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))
    transcript = tmp_path / "lane-session.jsonl"
    transcript.write_text("the lane deferred the product call.\n", encoding="utf-8")
    evidence: dict[str, object] = {"kind": "transcript", "ref": str(transcript)}
    if digest is not None:
        evidence["sha256"] = digest
    attempt = _attempt("Read the lane session transcript", "it deferred the product call.", evidence)
    payload = _payload(
        exhaustion={"reason": "attempts_exhausted", "attempts": [attempt], "residual_blocker": "Only the operator can pick."}
    )
    payload["exhaustion"]["attempts"].append(  # type: ignore[index]
        _attempt("Shipped the combined-feed prototype", "it passed the preference check.", {"kind": "order", "ref": "ord-x"})
    )

    with pytest.raises(BriefValidationError, match="sha256"):
        validate_brief(payload)


def test_transcript_evidence_requires_an_absolute_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-x")
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))
    quoted = "the lane answered: the feed layout is the operator's product call."
    (tmp_path / "lane-session.jsonl").write_text(quoted + "\n", encoding="utf-8")
    digest = hashlib.sha256(quoted.encode("utf-8")).hexdigest()
    attempt = _attempt(
        "Read the lane session transcript",
        "it deferred the product call.",
        {"kind": "transcript", "ref": "lane-session.jsonl", "sha256": digest},
    )
    payload = _payload(
        exhaustion={"reason": "attempts_exhausted", "attempts": [attempt], "residual_blocker": "Only the operator can pick."}
    )
    payload["exhaustion"]["attempts"].append(  # type: ignore[index]
        _attempt("Shipped the combined-feed prototype", "it passed the preference check.", {"kind": "order", "ref": "ord-x"})
    )

    with pytest.raises(BriefValidationError, match="absolute"):
        validate_brief(payload)


def test_tool_result_before_its_tool_use_in_one_record_is_not_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-feeds-layout-1")
    command = "chitra-tmux-capture feeds-reader --pane harness-pi"
    reordered = {
        "events": [
            {"type": "tool_result", "tool_use_id": "tu_capture_1", "content": "pane text\nexit code: 0"},
            {"type": "tool_use", "id": "tu_capture_1", "name": "Bash", "input": {"command": command}},
        ]
    }
    (state / "self-transcript.jsonl").write_text(json.dumps(reordered) + "\n", encoding="utf-8")
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))

    with pytest.raises(BriefValidationError, match="no recorded result in self transcript"):
        validate_brief(_payload(), self_transcript=state / "self-transcript.jsonl")


def test_verb_refusal_requires_a_bash_tool_use_not_any_tool_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-feeds-layout-1")
    events = [
        {
            "type": "tool_use",
            "id": "tu_note_1",
            "name": "TodoWrite",
            "input": {"todos": ["retry chitra-registry-push with token retry-1"]},
        },
        {"type": "tool_result", "tool_use_id": "tu_note_1", "content": "todos updated\nexit code: 1"},
    ]
    (state / "self-transcript.jsonl").write_text("".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))
    attempt = _attempt(
        "Presented the retry token to the registry",
        "the registry refused it outright.",
        {"kind": "verb_refusal", "ref": "chitra-registry-push"},
    )
    payload = _payload(
        exhaustion={
            "reason": "credential",
            "attempts": [attempt],
            "residual_blocker": "Only a human account can approve this access today.",
        },
    )

    with pytest.raises(BriefValidationError, match="no Bash tool use mentioning 'chitra-registry-push'"):
        validate_brief(payload, self_transcript=state / "self-transcript.jsonl")


def test_rejecting_resolver_reason_is_reported_with_attempt_index() -> None:
    with pytest.raises(BriefValidationError, match="attempt 2: ledger exploded"):
        validate_brief(_payload(), evidence_resolver=_RejectingResolver("ledger exploded"))


def test_default_resolver_reports_missing_self_transcript_for_command_evidence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-feeds-layout-1")
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))
    monkeypatch.delenv("CHITRA_SELF_TRANSCRIPT", raising=False)

    with pytest.raises(BriefValidationError, match="--self-transcript or set CHITRA_SELF_TRANSCRIPT"):
        validate_brief(_payload())


def test_env_var_self_transcript_is_honored_by_the_default_resolver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = tmp_path / "state"
    _write_ledger(state / "ledger.jsonl", "ord-feeds-layout-1")
    _write_self_transcript(state / "self-transcript.jsonl", command="chitra-tmux-capture feeds-reader --pane harness-pi", exit_code=0)
    monkeypatch.setenv("CHITRA_STATE_DIR", str(state))
    monkeypatch.setenv("CHITRA_SELF_TRANSCRIPT", str(state / "self-transcript.jsonl"))

    assert validate_brief(_payload()).exhaustion is not None
