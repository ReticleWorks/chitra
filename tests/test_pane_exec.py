from __future__ import annotations

import pytest

from chitra.pane_exec import supervised_environment


def test_supervised_environment_binds_real_tmux_pane_id_without_mutating_input() -> None:
    original = {"TMUX_PANE": "%17", "CHITRA_LANE_ID": "alpha"}
    result = supervised_environment(original)

    assert result["CHITRA_PANE_ID"] == "%17"
    assert result["CHITRA_LANE_ID"] == "alpha"
    assert "CHITRA_PANE_ID" not in original


@pytest.mark.parametrize("pane_id", ["", "17", "%", "%abc"])
def test_supervised_environment_refuses_unknown_pane_identity(pane_id: str) -> None:
    with pytest.raises(ValueError, match="TMUX_PANE"):
        supervised_environment({"TMUX_PANE": pane_id})
