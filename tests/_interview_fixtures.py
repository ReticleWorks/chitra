"""Shared valid-interview fixture for tests that build a GoalRecord directly.

``check_specification`` (chitra.goals) now hard-gates on the SHORT INTERVIEW
(see chitra.goals.INTERVIEW_QUESTION_IDS): a record with no interview block
fails the check. Test helpers across the suite build minimal GoalRecords for
purposes unrelated to the interview gate itself (reviewer plumbing, dispatch
routing, rate-limit holds, ...); they import this constant rather than each
inventing their own four-question interview payload.
"""

from __future__ import annotations

VALID_INTERVIEW: tuple[dict[str, str], ...] = (
    {
        "question": "intent",
        "answer": "Deliver the work this test fixture stands in for, on the operator's behalf.",
        "provenance": "operator:deliver the work this fixture stands in for",
    },
    {
        "question": "done_when",
        "answer": "The fixture's stated done_when condition is met and verified.",
        "provenance": "source:tests/_interview_fixtures.py",
    },
    {
        "question": "out_of_scope",
        "answer": "Anything outside the fixture's stated scope is out of scope.",
        "provenance": "operator:anything outside the stated scope is out of scope",
    },
    {
        "question": "constraints",
        "answer": "No constraints beyond the fixture's own stated source and scope.",
        "provenance": "source:tests/_interview_fixtures.py",
    },
)
