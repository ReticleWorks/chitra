"""Focused hostile tests for the authoritative controller-child registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from chitra.controller_registry import (
    SCHEMA,
    ControllerChildRegistry,
    ControllerRegistryError,
    RequiredChildRow,
    controller_registry_path,
    load_controller_registry,
    reconcile_controller_children,
    save_controller_registry,
)
from chitra.session_contract import ChildRosterEntry

DECLARED_AT = "2026-08-24T00:00:00+00:00"


def _row(child_id: str, parent_id: str, kind: str, ancestry: tuple[str, ...]) -> RequiredChildRow:
    return RequiredChildRow(
        child_id=child_id,
        parent_id=parent_id,
        kind=kind,  # type: ignore[arg-type]
        ancestry=ancestry,
        declared_at=DECLARED_AT,
    )


def _registry(rows: tuple[RequiredChildRow, ...], revision: int = 2) -> ControllerChildRegistry:
    return ControllerChildRegistry.declare(controller_id="ctrl-1", revision=revision, rows=rows)


def _observed(child_id: str, parent_id: str, *, retained: str = "retained", material: bool = False) -> ChildRosterEntry:
    return ChildRosterEntry(
        child_id=child_id,
        parent_id=parent_id,
        ancestry=(parent_id, child_id),
        retained_state=retained,  # type: ignore[arg-type]
        material_result=material,
    )


NESTED_ROWS = (
    _row("sub-ctrl-1", "ctrl-1", "controller", ("ctrl-1", "sub-ctrl-1")),
    _row("worker-1", "ctrl-1", "worker", ("ctrl-1", "worker-1")),
    _row("worker-2", "sub-ctrl-1", "worker", ("sub-ctrl-1", "worker-2")),
)


def test_reconciliation_retains_every_required_child_and_reports_missing() -> None:
    registry = _registry(NESTED_ROWS)
    report = reconcile_controller_children(registry, (_observed("worker-1", "ctrl-1"),))
    assert [entry.child_id for entry in report.reconciled] == ["sub-ctrl-1", "worker-1", "worker-2"]
    assert report.missing_child_ids == ("sub-ctrl-1", "worker-2")
    missing_states = {entry.child_id: entry.retained_state for entry in report.reconciled}
    assert missing_states["sub-ctrl-1"] == "missing"
    assert missing_states["worker-2"] == "missing"
    assert not report.complete


def test_unobserved_children_are_never_reported_done() -> None:
    registry = _registry(NESTED_ROWS)
    report = reconcile_controller_children(registry, ())
    assert all(not entry.material_result for entry in report.reconciled)
    assert all(entry.retained_state == "missing" for entry in report.reconciled)


def test_empty_registry_creates_no_requirements_from_observations() -> None:
    """The required set is explicit data only; observations never invent it."""

    registry = _registry(())
    report = reconcile_controller_children(registry, (_observed("stray-worker", "elsewhere"),))
    assert report.reconciled == ()
    assert report.unregistered_child_ids == ("stray-worker",)
    assert report.complete


def test_duplicate_child_ids_are_rejected() -> None:
    duplicate = (_row("worker-1", "ctrl-1", "worker", ("ctrl-1", "worker-1")),) * 2
    with pytest.raises(ControllerRegistryError, match="unique"):
        _registry(duplicate)


def test_parent_outside_the_declared_tree_is_rejected() -> None:
    orphan = (_row("worker-9", "ghost-controller", "worker", ("ghost-controller", "worker-9")),)
    with pytest.raises(ControllerRegistryError, match="outside the declared controller tree"):
        _registry(orphan)


def test_worker_row_may_not_claim_a_worker_parent() -> None:
    rows = (
        _row("worker-1", "ctrl-1", "worker", ("ctrl-1", "worker-1")),
        _row("worker-2", "worker-1", "worker", ("worker-1", "worker-2")),
    )
    with pytest.raises(ControllerRegistryError, match="outside the declared controller tree"):
        _registry(rows)


def test_controller_collapsed_into_a_worker_row_is_rejected() -> None:
    collapsed = (
        _row("sub-ctrl-1", "ctrl-1", "worker", ("ctrl-1", "sub-ctrl-1")),
        _row("worker-2", "sub-ctrl-1", "worker", ("sub-ctrl-1", "worker-2")),
    )
    with pytest.raises(ControllerRegistryError):
        _registry(collapsed)


def test_controller_itself_as_child_row_is_rejected() -> None:
    self_row = (_row("ctrl-1", "ctrl-1", "worker", ("ctrl-1", "ctrl-1")),)
    with pytest.raises(ControllerRegistryError, match="must not appear as a registry row"):
        _registry(self_row)


def test_observed_roster_may_not_collapse_the_controller_into_a_child_row() -> None:
    registry = _registry(NESTED_ROWS)
    collapse = (
        ChildRosterEntry(
            child_id="ctrl-1",
            parent_id="sub-ctrl-1",
            ancestry=("sub-ctrl-1", "ctrl-1"),
            retained_state="retained",
            material_result=False,
        ),
    )
    with pytest.raises(ControllerRegistryError, match="collapses the controller"):
        reconcile_controller_children(registry, collapse)


def test_duplicate_observed_child_ids_are_rejected() -> None:
    registry = _registry(NESTED_ROWS)
    with pytest.raises(ControllerRegistryError, match="duplicate observed child"):
        reconcile_controller_children(registry, (_observed("worker-1", "ctrl-1"),) * 2)


def test_observed_parent_mismatch_with_declared_ancestry_is_rejected() -> None:
    registry = _registry(NESTED_ROWS)
    mismatched = (ChildRosterEntry(
        child_id="worker-2",
        parent_id="ctrl-1",
        ancestry=("ctrl-1", "worker-2"),
        retained_state="retained",
        material_result=False,
    ),)
    with pytest.raises(ControllerRegistryError, match="parent mismatch"):
        reconcile_controller_children(registry, mismatched)


def test_store_round_trip_survives_reload(tmp_path: Path) -> None:
    registry = _registry(NESTED_ROWS)
    save_controller_registry(tmp_path, registry)
    reloaded = load_controller_registry(tmp_path)
    assert reloaded == registry
    document = controller_registry_path(tmp_path).read_text(encoding="utf-8")
    assert SCHEMA in document


def test_stale_or_replayed_document_cannot_erase_required_children(tmp_path: Path) -> None:
    full = _registry(NESTED_ROWS)
    save_controller_registry(tmp_path, full)
    replayed_subset = ControllerChildRegistry.declare(
        controller_id="ctrl-1",
        revision=99,
        rows=(NESTED_ROWS[0], NESTED_ROWS[1]),
    )
    with pytest.raises(ControllerRegistryError, match="would drop required children"):
        save_controller_registry(tmp_path, replayed_subset)
    survivor = load_controller_registry(tmp_path)
    assert survivor is not None
    assert {row.child_id for row in survivor.rows} == {"sub-ctrl-1", "worker-1", "worker-2"}


def test_replay_at_equal_or_lower_revision_is_rejected(tmp_path: Path) -> None:
    registry = _registry(NESTED_ROWS, revision=5)
    save_controller_registry(tmp_path, registry)
    with pytest.raises(ControllerRegistryError, match="strictly increase"):
        save_controller_registry(tmp_path, registry)
    older = _registry(NESTED_ROWS[:1], revision=4)
    with pytest.raises((ControllerRegistryError,), match="drop|required"):
        save_controller_registry(tmp_path, older)


def test_new_revision_may_add_but_not_remove_requirements(tmp_path: Path) -> None:
    save_controller_registry(tmp_path, _registry(NESTED_ROWS, revision=1))
    grown = ControllerChildRegistry.declare(
        controller_id="ctrl-1",
        revision=2,
        rows=(*NESTED_ROWS, _row("worker-3", "ctrl-1", "worker", ("ctrl-1", "worker-3"))),
    )
    save_controller_registry(tmp_path, grown)
    reloaded = load_controller_registry(tmp_path)
    assert reloaded is not None
    assert [row.child_id for row in reloaded.rows] == ["sub-ctrl-1", "worker-1", "worker-2", "worker-3"]


def test_corrupt_store_document_fails_closed(tmp_path: Path) -> None:
    path = controller_registry_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"schema": "chitra.controller-registry.v0", "rows": []}', encoding="utf-8")
    with pytest.raises(ControllerRegistryError, match="not a chitra.controller-registry.v1"):
        load_controller_registry(tmp_path)
