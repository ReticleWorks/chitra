"""Focused tests for exact canonical-choice evidence."""

from __future__ import annotations

import pytest

from chitra.canonical_choices import CanonicalChoice, CanonicalChoicesPolicy


def _policy() -> CanonicalChoicesPolicy:
    return CanonicalChoicesPolicy(
        choices={
            "legacy-doc": CanonicalChoice(
                kind="deprecated_path",
                subject="/workspace/project/legacy.md",
                canonical_value="/workspace/project/current.md",
            )
        }
    )


def test_registry_key_is_validated_independently_from_subject() -> None:
    policy = _policy()
    assert policy.choices["legacy-doc"].subject == "/workspace/project/legacy.md"

    with pytest.raises(ValueError, match="stable registry key"):
        CanonicalChoicesPolicy(
            choices={
                "legacy doc": CanonicalChoice(
                    kind="deprecated_path",
                    subject="/workspace/project/legacy.md",
                    canonical_value="/workspace/project/current.md",
                )
            }
        )
