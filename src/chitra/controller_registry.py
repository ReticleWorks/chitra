"""controller_registry — authoritative controller-child reconciliation.

The required child set is explicit, persisted data: an operator- or
controller-authored registry document. It is never inferred from tmux
panes, pane names, terminal labels, or process visibility. Reconciliation
compares one observation cycle against that document and must:

- retain every required child in its output;
- report children absent from the observation as ``missing``, never omit
  them and never report them done;
- reject duplicate child IDs, parent mismatch against the declared
  ancestry, and any collapse of a controller row into a worker row.

The store follows the same atomic-write-then-``os.replace`` and exclusive-
``flock`` pattern as ``chitra.goals`` and ``chitra.account_registry``. A
saved registry is revision-monotonic: a stale or replayed document cannot
erase previously required children.

No LLM calls anywhere in this module.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from chitra._fsio import locked_json_store, write_json_atomic
from chitra.session_contract import (
    ChildRosterEntry,
    Identifier,
    Timestamp,
    _ContractModel,
    _timestamp,
)

SCHEMA = "chitra.controller-registry.v1"

RegistryRowKind = Literal["controller", "worker"]


class ControllerRegistryError(ValueError):
    """Raised when a registry document or observation would lose required identity."""


class RequiredChildRow(_ContractModel):
    """One explicitly declared required child of the controller tree."""

    child_id: Identifier
    parent_id: Identifier
    kind: RegistryRowKind
    ancestry: tuple[Identifier, ...]
    declared_at: Timestamp

    @field_validator("declared_at")
    @classmethod
    def validate_declared_at(cls, value: str) -> str:
        return _timestamp(value, "required_child_row.declared_at")

    @model_validator(mode="after")
    def validate_ancestry(self) -> "RequiredChildRow":
        if not self.ancestry or self.ancestry[0] != self.parent_id or self.ancestry[-1] != self.child_id:
            raise ValueError("required child ancestry must start at parent_id and end at child_id")
        if self.kind == "controller" and self.child_id == self.parent_id:
            raise ValueError("a controller row must not parent itself")
        return self


class ControllerChildRegistry(_ContractModel):
    """The authoritative, explicitly authored required-child document."""

    schema: Literal["chitra.controller-registry.v1"] = SCHEMA  # type: ignore[assignment]
    controller_id: Identifier
    revision: int = Field(ge=1)
    rows: tuple[RequiredChildRow, ...] = ()

    @field_validator("revision")
    @classmethod
    def reject_bool_revision(cls, value: int) -> int:
        if isinstance(value, bool):
            raise ValueError("registry revision must be an integer")
        return value

    @model_validator(mode="after")
    def validate_structure(self) -> "ControllerChildRegistry":
        _validate_rows(self.controller_id, self.rows)
        return self

    @classmethod
    def declare(
        cls,
        *,
        controller_id: str,
        revision: int,
        rows: tuple[RequiredChildRow, ...],
    ) -> "ControllerChildRegistry":
        """Author one registry document with explicit typed rejection."""

        _validate_controller_identity(controller_id)
        _validate_rows(controller_id, rows)
        return cls(controller_id=controller_id, revision=revision, rows=rows)

    def require_row(self, child_id: str) -> RequiredChildRow:
        row = next((candidate for candidate in self.rows if candidate.child_id == child_id), None)
        if row is None:
            raise ControllerRegistryError(f"child {child_id} is not a required registry row")
        return row


def _validate_controller_identity(controller_id: object) -> None:
    if isinstance(controller_id, str) and controller_id.strip():
        return
    raise ControllerRegistryError("controller ID must be a non-empty identifier")


def _validate_rows(controller_id: str, rows: tuple[RequiredChildRow, ...]) -> None:
    ids = [row.child_id for row in rows]
    if len(ids) != len(set(ids)):
        raise ControllerRegistryError("registry child IDs must be unique")
    if any(row.child_id == controller_id for row in rows):
        raise ControllerRegistryError("the controller itself must not appear as a registry row")
    known_controllers = {controller_id} | {row.child_id for row in rows if row.kind == "controller"}
    for row in rows:
        if row.parent_id not in known_controllers:
            raise ControllerRegistryError(
                f"row {row.child_id} names parent {row.parent_id} outside the declared controller tree"
            )


class ControllerChildReconciliation(_ContractModel):
    """One observation cycle projected onto the authoritative registry."""

    controller_id: Identifier
    registry_revision: int = Field(ge=1)
    reconciled: tuple[ChildRosterEntry, ...] = ()
    missing_child_ids: tuple[Identifier, ...] = ()
    unregistered_child_ids: tuple[Identifier, ...] = ()

    @property
    def complete(self) -> bool:
        return not self.missing_child_ids


def reconcile_controller_children(
    registry: ControllerChildRegistry,
    observed: tuple[ChildRosterEntry, ...] | list[ChildRosterEntry] = (),
) -> ControllerChildReconciliation:
    """Project one observation cycle onto the explicit required registry.

    Every required row appears exactly once in the output. Rows absent from
    ``observed`` keep their declared identity and are reported with
    retained_state ``missing``; they are never dropped and never marked as
    having produced a material result.
    """

    seen_ids: set[str] = set()
    matched: dict[str, ChildRosterEntry] = {}
    unregistered: list[str] = []
    for entry in observed:
        if entry.child_id in seen_ids:
            raise ControllerRegistryError(f"duplicate observed child ID {entry.child_id}")
        seen_ids.add(entry.child_id)
        if entry.child_id == registry.controller_id:
            raise ControllerRegistryError(
                "observed roster collapses the controller into a child row"
            )
        try:
            required = registry.require_row(entry.child_id)
        except ControllerRegistryError:
            unregistered.append(entry.child_id)
            continue
        if (entry.parent_id, entry.ancestry) != (required.parent_id, required.ancestry):
            raise ControllerRegistryError(
                f"observed parent mismatch for required child {entry.child_id}"
            )
        matched[entry.child_id] = entry

    reconciled: list[ChildRosterEntry] = []
    missing: list[str] = []
    for row in registry.rows:
        entry = matched.get(row.child_id)
        if entry is None:
            missing.append(row.child_id)
            reconciled.append(
                ChildRosterEntry(
                    child_id=row.child_id,
                    parent_id=row.parent_id,
                    ancestry=row.ancestry,
                    retained_state="missing",
                    material_result=False,
                )
            )
        else:
            reconciled.append(entry)

    return ControllerChildReconciliation(
        controller_id=registry.controller_id,
        registry_revision=registry.revision,
        reconciled=tuple(reconciled),
        missing_child_ids=tuple(missing),
        unregistered_child_ids=tuple(unregistered),
    )


def controller_registry_path(root: Path | None = None) -> Path:
    """Return the persistent registry document path under state root."""
    from chitra.state_paths import state_dir

    return (state_dir() if root is None else root) / "controller_registry.json"


def load_controller_registry(root: Path | None = None) -> ControllerChildRegistry | None:
    """Load the stored registry; a missing store has no declared children."""
    path = controller_registry_path(root)
    try:
        payload: Any = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        raise ControllerRegistryError("controller_registry.json is not a chitra.controller-registry.v1 document")
    rows_raw = payload.get("rows")
    if not isinstance(rows_raw, list):
        raise ControllerRegistryError("controller_registry.json rows must be a list")
    try:
        return ControllerChildRegistry.from_dict(
            {
                "controller_id": payload.get("controller_id"),
                "revision": payload.get("revision"),
                "rows": rows_raw,
            }
        )
    except (TypeError, ValueError) as exc:
        raise ControllerRegistryError(f"stored controller registry is invalid: {exc}") from exc


def save_controller_registry(root: Path | None, registry: ControllerChildRegistry) -> None:
    """Atomically persist the registry with monotonic revision enforcement.

    A write whose rows omit any previously stored required child raises
    instead of landing: stale or replayed documents can add requirements or
    advance the revision, but they can never erase required children.
    """

    path = controller_registry_path(root)
    with locked_json_store(path):
        stored = load_controller_registry(root)
        if stored is not None:
            stored_ids = {row.child_id for row in stored.rows}
            incoming_ids = {row.child_id for row in registry.rows}
            dropped = sorted(stored_ids - incoming_ids)
            if dropped:
                raise ControllerRegistryError(
                    f"registry write would drop required children: {', '.join(dropped)}"
                )
            if registry.revision <= stored.revision:
                raise ControllerRegistryError(
                    "registry revision must strictly increase over the stored document"
                )
        payload = {
            "schema": SCHEMA,
            "controller_id": str(registry.controller_id),
            "revision": int(registry.revision),
            "rows": [row.to_dict() for row in registry.rows],
        }
        write_json_atomic(path, payload, fsync=True)


__all__ = [
    "SCHEMA",
    "ControllerChildReconciliation",
    "ControllerChildRegistry",
    "ControllerRegistryError",
    "RequiredChildRow",
    "RegistryRowKind",
    "controller_registry_path",
    "load_controller_registry",
    "reconcile_controller_children",
    "save_controller_registry",
]