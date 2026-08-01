"""Lifecycle regressions for the normative enumeration annex."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from chitra.enumeration_gate import (
    AGGREGATE_NOUN_TERMS,
    AdoptionGateError,
    NormativeAnnexItem,
    lint_aggregate_nouns,
    review_adoption,
    review_close_inventory,
)
from chitra.goal_enforcement import freeze_goal
from chitra.goals import GoalRecord, close_goal, get_goal, main, redirect_goal, upsert_goal


def _goal(*, annex: tuple[NormativeAnnexItem, ...]) -> GoalRecord:
    return GoalRecord(
        session_ref="localhost:f8:0.0",
        intent="Deliver every operator-required live client without silently narrowing the source inventory.",
        goal="Validate the complete required FetchCore live client inventory safely.",
        done_when="Both client X and client Y pass live validation.",
        scope="FetchCore live clients and their validation evidence only.",
        source="task-file:/tmp/f8.md",
        status="working",
        normative_annex=annex,
    )


def _two_required_clients() -> tuple[NormativeAnnexItem, ...]:
    return (
        NormativeAnnexItem(id="client-x", text="Client X passes live validation"),
        NormativeAnnexItem(id="client-y", text="Client Y passes live validation"),
    )


def test_f8_shape_close_fails_closed_when_second_required_client_becomes_follow_on() -> None:
    review = review_close_inventory(
        _two_required_clients(),
        {"client-x"},
        "Client X passed live validation; client-y is follow-on work.",
    )

    assert review.accepted is False
    assert review.missing_item_ids == ("client-y",)
    assert review.reclassified_item_ids == ("client-y",)
    assert "client-y" in "; ".join(review.issues)


def test_same_close_passes_only_after_operator_acknowledged_goal_revision_descopes_client_y(tmp_path: Path) -> None:
    original = upsert_goal(tmp_path, _goal(annex=_two_required_clients()))
    revised_annex = (
        original.normative_annex[0],
        replace(
            original.normative_annex[1],
            status="descoped",
            reason="operator removed client Y from the required live matrix",
            operator_ack=True,
        ),
    )

    revised = redirect_goal(
        tmp_path,
        original.session_ref,
        reason="operator explicitly descoped client Y",
        done_when="Client X passes live validation with its cited probe.",
        normative_annex=revised_annex,
    )
    review = review_close_inventory(
        revised.normative_annex,
        {"client-x"},
        "Client X passed live validation; client-y is follow-on work under the acknowledged revision.",
    )

    assert revised.goal_version == 2
    assert revised.goal_history[-1]["normative_annex"]
    assert review.accepted is True
    assert close_goal(
        tmp_path,
        revised.session_ref,
        delivered_item_ids=("client-x",),
        close_claim="Client X delivered; client-y is follow-on under the acknowledged revision.",
    ) == revised


def test_aggregate_noun_adoption_is_rejected_without_count_or_annex(tmp_path: Path) -> None:
    review = review_adoption("representative consumers pass live validation")

    assert review.accepted is False
    assert "representative" in review.aggregate_terms
    assert "consumers" in review.aggregate_terms
    with pytest.raises(AdoptionGateError, match="neither enumerates"):
        upsert_goal(
            tmp_path,
            GoalRecord(
                session_ref="localhost:rejected:0.0",
                goal="Validate every required consumer against the live system.",
                done_when="representative consumers pass live validation",
                source="task-file:/tmp/f8.md",
                status="working",
            ),
        )


def test_explicit_inline_enumeration_or_count_pinning_annex_is_accepted() -> None:
    inline = review_adoption("both consumer A and consumer B pass live validation")
    carried_annex = (
        NormativeAnnexItem(id="consumer-a", text="Consumer A passes live validation", status="carried"),
        NormativeAnnexItem(id="consumer-b", text="Consumer B passes live validation", status="carried"),
    )
    annexed = review_adoption("representative consumers pass live validation", carried_annex)

    assert inline.accepted is True
    assert annexed.accepted is True


def test_adoption_diff_names_required_annex_item_collapsed_out_of_done_when() -> None:
    review = review_adoption("Client X passes live validation.", _two_required_clients())

    assert review.accepted is False
    assert review.uncovered_item_ids == ("client-y",)
    assert "client-y" in "; ".join(review.issues)


@pytest.mark.parametrize("term", [*AGGREGATE_NOUN_TERMS, "consumers", "clients", "integrations"])
def test_aggregate_noun_lint_vocabulary_is_active(term: str) -> None:
    assert term in lint_aggregate_nouns(f"{term} pass live validation")


def test_normative_annex_is_bound_under_frozen_goal_contract_hash() -> None:
    original = _goal(annex=_two_required_clients())
    changed = replace(
        original,
        normative_annex=(
            original.normative_annex[0],
            replace(original.normative_annex[1], text="Client Y passes the production live validation probe"),
        ),
    )

    original_frozen = freeze_goal(original)
    changed_frozen = freeze_goal(changed)

    assert original_frozen.contract_id != changed_frozen.contract_id
    assert original_frozen.normative_annex[1].text == "Client Y passes live validation"


def test_cli_repeatable_annex_items_persist_and_close_requires_the_delivered_inventory(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    set_args = [
        "set",
        "--root",
        str(tmp_path),
        "--session-ref",
        "localhost:cli-f8:0.0",
        "--goal",
        "Validate the complete required live client inventory safely.",
        "--done-when",
        "Both client X and client Y pass live validation.",
        "--source",
        "task-file:/tmp/f8.md",
        "--annex-item",
        "client-x=Client X passes live validation",
        "--annex-item",
        "client-y=Client Y passes live validation",
    ]

    assert main(set_args) == 0
    capsys.readouterr()
    stored = get_goal(tmp_path, "localhost:cli-f8:0.0")
    assert stored is not None
    assert tuple(item.id for item in stored.normative_annex) == ("client-x", "client-y")
    assert main(
        [
            "close",
            "--root",
            str(tmp_path),
            "--session-ref",
            stored.session_ref,
            "--delivered-item",
            "client-x",
            "--close-claim",
            "client-y is follow-on work",
        ]
    ) == 1
    assert "client-y" in capsys.readouterr().err
    assert get_goal(tmp_path, stored.session_ref) == stored
    assert main(
        [
            "close",
            "--root",
            str(tmp_path),
            "--session-ref",
            stored.session_ref,
            "--delivered-item",
            "client-x",
            "--delivered-item",
            "client-y",
        ]
    ) == 0
