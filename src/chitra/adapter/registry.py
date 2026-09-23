"""Plug registry and lane-handle persistence.

Plug modules are imported lazily so importing the registry (dispatchd,
monitord) never pulls in transport code it does not call. Plug instances are
cached per name so their pooled event readers survive across calls.

Plugs that exist on the roadmap but are not built in this phase resolve to
``HarnessUnavailable`` with the reason — never a silent fallthrough to tmux.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from chitra._fsio import locked_json_store, write_json_atomic
from chitra.adapter.contract import HarnessUnavailable, LaneHandle, LanePlug

HANDLE_FILENAME = "lane-handle.json"

_PLUG_FACTORIES: dict[str, Callable[[], LanePlug]] = {}


def _register_builtins() -> None:
    from chitra.adapter.plugs.amp_orb import AmpOrbPlug
    from chitra.adapter.plugs.claude_tmux import ClaudeTmuxPlug
    from chitra.adapter.plugs.codex_tmux import CodexTmuxPlug
    from chitra.adapter.plugs.opencode_tmux import OpenCodeTmuxPlug

    _PLUG_FACTORIES.update(
        {
            ClaudeTmuxPlug.name: ClaudeTmuxPlug,
            CodexTmuxPlug.name: CodexTmuxPlug,
            OpenCodeTmuxPlug.name: OpenCodeTmuxPlug,
            AmpOrbPlug.name: AmpOrbPlug,
        }
    )


# Plugs named in the adapter proposal but deliberately unbuilt in phase 1.
_UNBUILT: dict[str, str] = {
    "dsh": "dsh's headless loop never surfaces a pending order to its task runner — it cannot consume one",
    "openhands": "OpenHands is not installed on this fleet",
    "prime": "the prime plug is phase 3, not built yet",
    "unreal": "the unreal plug is phase 3, not built yet",
}

# Journal client identity -> plug that serves it. File-transcript clients all
# land on their tmux plug; ``amp`` lands on the orb plug.
_CLIENT_PLUGS: dict[str, str] = {
    "claude": "claude-tmux",
    "codex": "codex-tmux",
    "opencode": "opencode-tmux",
    "amp": "amp-orb",
}

_instances: dict[str, LanePlug] = {}
_registered = False


def plug(name: str) -> LanePlug:
    """Return the registered plug for ``name``; unknown/unbuilt names raise."""
    global _registered
    if not _registered:
        _register_builtins()
        _registered = True
    if name in _UNBUILT:
        raise HarnessUnavailable(f"lane plug {name!r} is not built: {_UNBUILT[name]}")
    factory = _PLUG_FACTORIES.get(name)
    if factory is None:
        raise HarnessUnavailable(f"no lane plug named {name!r}")
    instance = _instances.get(name)
    if instance is None:
        instance = factory()
        _instances[name] = instance
    return instance


def plug_for_client(client: object) -> LanePlug:
    """Resolve the plug that owns a binding's ``client`` identity."""
    name = _CLIENT_PLUGS.get(str(client))
    if name is None:
        raise HarnessUnavailable(f"no lane plug serves client {client!r}")
    return plug(name)


def plug_names() -> tuple[str, ...]:
    """Names of every built plug (unbuilt roadmap plugs excluded)."""
    global _registered
    if not _registered:
        _register_builtins()
        _registered = True
    return tuple(sorted(_PLUG_FACTORIES))


def handle_path(state_dir: Path) -> Path:
    """Where a lane's durable handle lives under the lane state dir."""
    return state_dir / HANDLE_FILENAME


def save_handle(state_dir: Path, handle: LaneHandle) -> Path:
    """Persist ``handle`` so a later process can reach the same lane."""
    path = handle_path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    with locked_json_store(path):
        write_json_atomic(path, handle.to_dict(), fsync=True)
    return path


def load_handle(state_dir: Path) -> LaneHandle | None:
    """Load a persisted lane handle; ``None`` when none was saved."""
    path = handle_path(state_dir)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"lane handle cannot be read: {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"lane handle is not valid JSON: {path}: {exc}") from exc
    return LaneHandle.from_dict(payload)
