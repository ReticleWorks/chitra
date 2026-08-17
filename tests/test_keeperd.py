"""Tests for the keeper supervisor's pure decision logic."""

from __future__ import annotations

import json

from chitra.keeperd import (
    ACTIVE_TURN_RE,
    CHROME_LINE_RE,
    Keeper,
    substantive_digest,
)


class TestSubstantiveDigest:
    def test_spinner_and_ansi_churn_is_not_progress(self) -> None:
        a = b"\x1b[31m* Thinking...\x1b[0m  hello world 42"
        b = b"\x1b[32m+ Thinking....\x1b[0m  hello?? world--42"
        assert substantive_digest(a) == substantive_digest(b)

    def test_new_text_is_progress(self) -> None:
        a = b"hello world"
        b = b"hello world plus new output"
        assert substantive_digest(a) != substantive_digest(b)


class TestActiveTurnDetection:
    def test_completed_turn_summary_is_not_active(self) -> None:
        assert not ACTIVE_TURN_RE.search("✳ Brewed for 24s")
        assert not ACTIVE_TURN_RE.search("✻ Cooked for 17m 39s")

    def test_live_spinner_with_elapsed_is_active(self) -> None:
        assert ACTIVE_TURN_RE.search("✽ Tempering… (4m 13s · ↓ 2.0k tokens)")
        assert ACTIVE_TURN_RE.search("esc to interrupt")


class TestChromeStripping:
    def test_statusline_and_timers_are_chrome(self) -> None:
        for line in (
            "  ⏵⏵ auto mode on (shift+tab to cycle)",
            "   You've used 78% of your weekly limit · resets Aug 22",
            "────────",
            "✻ Brewed for 24s",
            "globalVersion: 2.1.226",
        ):
            assert CHROME_LINE_RE.search(line), line

    def test_real_output_is_not_chrome(self) -> None:
        assert not CHROME_LINE_RE.search("● Heartbeat 6")


class TestComposerParsing:
    def test_placeholder_is_not_a_draft(self) -> None:
        content = "› Find and fix a bug in @filename\n  model line"
        assert Keeper._composer_text(content) == ""

    def test_real_text_is_a_draft(self) -> None:
        content = "❯ Reply with exactly STEER_OK and no other text.\n───"
        assert "STEER_OK" in Keeper._composer_text(content)

    def test_empty_prompt(self) -> None:
        assert Keeper._composer_text("❯ \n───") == ""


class TestTranscriptConsumed:
    @staticmethod
    def _line(rtype: str, text: str) -> str:
        return json.dumps({"type": rtype, "message": {"role": rtype, "content": text}})

    def test_user_record_plus_turn_start_is_consumed(self) -> None:
        content = "\n".join(
            [
                self._line("user", "Continue toward the recorded goal: do the thing"),
                self._line("assistant", "on it"),
            ]
        )
        assert Keeper.transcript_consumed(content, "Continue toward the recorded goal")

    def test_marker_in_assistant_echo_is_not_consumed(self) -> None:
        content = self._line("assistant", "someone said Continue toward the recorded goal")
        assert not Keeper.transcript_consumed(content, "Continue toward the recorded goal")

    def test_user_record_without_turn_start_is_not_consumed(self) -> None:
        content = self._line("user", "Continue toward the recorded goal: do the thing")
        assert not Keeper.transcript_consumed(content, "Continue toward the recorded goal")
