"""Public road-map and joined-session report surface.

The canonical wire types remain in :mod:`chitra.session_contract`.  This
module keeps the short, product-facing import path for the read-only view.
"""

from .session_view import (
    JOINED_SESSION_VIEW_SCHEMA,
    SESSION_VIEW_SCHEMA,
    GoalProjection,
    JoinedLaneView,
    JoinedSessionProjection,
    JoinedSessionView,
    build_joined_session_view,
    joined_session_view,
    project_joined_session,
    render_joined_session,
    render_joined_session_view,
    render_progress_view,
)

__all__ = [
    "GoalProjection",
    "JOINED_SESSION_VIEW_SCHEMA",
    "SESSION_VIEW_SCHEMA",
    "JoinedLaneView",
    "JoinedSessionProjection",
    "JoinedSessionView",
    "build_joined_session_view",
    "joined_session_view",
    "project_joined_session",
    "render_joined_session",
    "render_joined_session_view",
    "render_progress_view",
]
