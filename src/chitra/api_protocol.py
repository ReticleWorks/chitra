"""Pure validation, predicates, and schema for Chitra's local JSON API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, cast

from chitra.agent_runtime import STATUS_EVENT_TYPE, StatusEvent
from chitra.agent_status import AGENT_STATES

API_PROTOCOL_VERSION = 1
MAX_WAIT_MS = 86_400_000
MAX_SUBSCRIPTIONS = 64
MAX_PREDICATE_DEPTH = 8
MAX_PREDICATE_TERMS = 64
PREDICATE_FIELDS = frozenset(
    {
        "type",
        "seq",
        "pane_id",
        "target",
        "session_ref",
        "lane_id",
        "agent",
        "agent_status",
        "source",
        "authority",
        "revision",
    }
)
PredicateOp = Literal["all", "any", "not", "eq", "in", "exists"]


class ProtocolError(ValueError):
    """A request cannot be validated against the public protocol."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class EventPredicate:
    """One bounded recursive predicate over documented event fields."""

    op: PredicateOp
    field: str | None = None
    value: object = None
    values: tuple[object, ...] = ()
    filters: tuple[EventPredicate, ...] = ()

    def matches(self, event: dict[str, object]) -> bool:
        if self.op == "all":
            return all(child.matches(event) for child in self.filters)
        if self.op == "any":
            return any(child.matches(event) for child in self.filters)
        if self.op == "not":
            return not self.filters[0].matches(event)
        assert self.field is not None
        if self.op == "exists":
            return self.field in event and event[self.field] is not None
        if self.op == "eq":
            return event.get(self.field) == self.value
        return event.get(self.field) in self.values


@dataclass(frozen=True, slots=True)
class EventSubscription:
    """A typed event filter plus an optional composable predicate."""

    event_type: str
    pane_id: str | None = None
    session_ref: str | None = None
    lane_id: str | None = None
    agent: str | None = None
    agent_status: str | None = None
    where: EventPredicate | None = None

    def matches(self, event: StatusEvent) -> bool:
        payload = event.to_dict()
        if payload["type"] != self.event_type:
            return False
        direct = {
            "pane_id": self.pane_id,
            "session_ref": self.session_ref,
            "lane_id": self.lane_id,
            "agent": self.agent,
            "agent_status": self.agent_status,
        }
        if any(expected is not None and payload.get(field) != expected for field, expected in direct.items()):
            return False
        return self.where is None or self.where.matches(payload)


def _object(value: object, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError("invalid_params", f"{name} must be an object")
    return cast(dict[str, Any], value)


def _text(raw: dict[str, Any], key: str, *, name: str, required: bool = False) -> str | None:
    value = raw.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value:
        raise ProtocolError("invalid_params", f"{name}.{key} must be a non-empty string")
    return value


def parse_predicate(value: object, *, depth: int = 0) -> EventPredicate:
    """Parse the all/any/not/eq/in/exists predicate language."""
    if depth >= MAX_PREDICATE_DEPTH:
        raise ProtocolError("invalid_params", f"predicate exceeds depth {MAX_PREDICATE_DEPTH}")
    raw = _object(value, name="predicate")
    op = _text(raw, "op", name="predicate", required=True)
    if op not in ("all", "any", "not", "eq", "in", "exists"):
        raise ProtocolError("invalid_params", "predicate.op must be all, any, not, eq, in, or exists")
    if op in ("all", "any"):
        if set(raw) != {"op", "filters"}:
            raise ProtocolError("invalid_params", f"predicate {op} accepts only op and filters")
        filters = raw.get("filters")
        if not isinstance(filters, list) or not filters or len(filters) > MAX_PREDICATE_TERMS:
            raise ProtocolError(
                "invalid_params",
                f"predicate {op}.filters must contain 1-{MAX_PREDICATE_TERMS} entries",
            )
        return EventPredicate(
            op=cast(PredicateOp, op),
            filters=tuple(parse_predicate(child, depth=depth + 1) for child in filters),
        )
    if op == "not":
        if set(raw) != {"op", "filter"}:
            raise ProtocolError("invalid_params", "predicate not accepts only op and filter")
        return EventPredicate(op="not", filters=(parse_predicate(raw.get("filter"), depth=depth + 1),))
    field = _text(raw, "field", name="predicate", required=True)
    assert field is not None
    if field not in PREDICATE_FIELDS:
        raise ProtocolError("invalid_params", f"predicate field is not supported: {field}")
    if op == "exists":
        if set(raw) != {"op", "field"}:
            raise ProtocolError("invalid_params", "predicate exists accepts only op and field")
        return EventPredicate(op="exists", field=field)
    if op == "eq":
        if set(raw) != {"op", "field", "value"}:
            raise ProtocolError("invalid_params", "predicate eq accepts only op, field, and value")
        return EventPredicate(op="eq", field=field, value=raw.get("value"))
    if set(raw) != {"op", "field", "values"}:
        raise ProtocolError("invalid_params", "predicate in accepts only op, field, and values")
    values = raw.get("values")
    if not isinstance(values, list) or not values or len(values) > MAX_PREDICATE_TERMS:
        raise ProtocolError("invalid_params", f"predicate in.values must contain 1-{MAX_PREDICATE_TERMS} entries")
    return EventPredicate(op="in", field=field, values=tuple(values))


def parse_subscriptions(params: object) -> tuple[EventSubscription, ...]:
    raw = _object(params, name="params")
    if set(raw) != {"subscriptions"}:
        raise ProtocolError("invalid_params", "events.subscribe params must contain only subscriptions")
    values = raw.get("subscriptions")
    if not isinstance(values, list) or not values or len(values) > MAX_SUBSCRIPTIONS:
        raise ProtocolError("invalid_params", f"subscriptions must contain 1-{MAX_SUBSCRIPTIONS} entries")
    subscriptions: list[EventSubscription] = []
    allowed = {"type", "pane_id", "session_ref", "lane_id", "agent", "agent_status", "where"}
    for index, value in enumerate(values):
        item = _object(value, name=f"subscriptions[{index}]")
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise ProtocolError("invalid_params", f"subscriptions[{index}] has unsupported fields: {', '.join(unknown)}")
        event_type = _text(item, "type", name=f"subscriptions[{index}]", required=True)
        if event_type != STATUS_EVENT_TYPE:
            raise ProtocolError("invalid_params", f"unsupported subscription type: {event_type}")
        agent_status = _text(item, "agent_status", name=f"subscriptions[{index}]")
        if agent_status is not None and agent_status not in AGENT_STATES:
            raise ProtocolError("invalid_params", "agent_status filter is invalid")
        predicate = parse_predicate(item["where"]) if "where" in item else None
        subscriptions.append(
            EventSubscription(
                event_type=event_type,
                pane_id=_text(item, "pane_id", name=f"subscriptions[{index}]"),
                session_ref=_text(item, "session_ref", name=f"subscriptions[{index}]"),
                lane_id=_text(item, "lane_id", name=f"subscriptions[{index}]"),
                agent=_text(item, "agent", name=f"subscriptions[{index}]"),
                agent_status=agent_status,
                where=predicate,
            )
        )
    return tuple(subscriptions)


def api_schema() -> dict[str, object]:
    """Return the bundled discovery document for requests and wire shapes."""
    state_enum = list(AGENT_STATES)
    request_base = {
        "type": "object",
        "required": ["id", "method", "params"],
        "properties": {
            "id": {"type": "string", "minLength": 1},
            "method": {"type": "string"},
            "params": {"type": "object"},
        },
        "additionalProperties": False,
    }
    event_shape = {
        "type": "object",
        "required": ["type", "seq", "pane_id", "agent", "agent_status", "revision"],
        "properties": {
            "type": {"const": STATUS_EVENT_TYPE},
            "seq": {"type": "integer", "minimum": 1},
            "pane_id": {"type": "string"},
            "session_ref": {"type": ["string", "null"]},
            "lane_id": {"type": "string"},
            "agent": {"type": "string"},
            "agent_status": {"enum": state_enum},
            "source": {"type": ["string", "null"]},
            "revision": {"type": "integer", "minimum": 1},
        },
    }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Chitra local socket API",
        "protocol_version": API_PROTOCOL_VERSION,
        "transport": {"framing": "newline-delimited JSON", "local": "Unix domain socket"},
        "methods": {
            "ping": {"request": request_base, "result_type": "pong"},
            "api.schema": {"request": request_base, "result_type": "api_schema"},
            "pane.report_agent": {
                "request": request_base,
                "params": {
                    "required": ["pane_id", "source", "agent", "state"],
                    "state": {"enum": ["idle", "working", "blocked"]},
                    "optional": ["session_ref"],
                },
                "result_type": "agent_report",
            },
            "pane.clear_agent_authority": {"request": request_base, "result_type": "agent_authority_clear"},
            "agent.explain": {"request": request_base, "result_type": "agent_explain"},
            "agent.wait": {
                "request": request_base,
                "params": {
                    "required": ["pane_id", "until"],
                    "until": {"enum": state_enum},
                    "optional": ["timeout_ms"],
                    "timeout_ms": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT_MS, "default": MAX_WAIT_MS},
                },
                "result_type": "agent_wait",
            },
            "events.subscribe": {
                "request": request_base,
                "params": {
                    "required": ["subscriptions"],
                    "typed_fields": ["type", "pane_id", "session_ref", "lane_id", "agent", "agent_status"],
                    "predicate_ops": ["all", "any", "not", "eq", "in", "exists"],
                },
                "result_type": "event_subscription",
            },
            "server.snapshot": {"request": request_base, "result_type": "session_snapshot"},
            "server.handoff.prepare": {"request": request_base, "result_type": "handoff_manifest"},
            "server.handoff.commit": {"request": request_base, "result_type": "handoff_committed"},
            "server.handoff.abort": {"request": request_base, "result_type": "handoff_aborted"},
        },
        "success_response": {
            "type": "object",
            "required": ["id", "result"],
            "properties": {"id": {"type": "string"}, "result": {"type": "object", "required": ["type"]}},
        },
        "error_response": {
            "type": "object",
            "required": ["id", "error"],
            "properties": {
                "id": {"type": ["string", "null"]},
                "error": {
                    "type": "object",
                    "required": ["code", "message"],
                    "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
                },
            },
        },
        "emitted_event": event_shape,
        "subscription_event": {
            "type": "object",
            "required": ["id", "event"],
            "properties": {"id": {"type": "string"}, "event": event_shape},
        },
    }
