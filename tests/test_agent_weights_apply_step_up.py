"""T17 (W12f): applying an agent-weight suggestion now changes runtime AI behaviour
(T9 rebinds the live config; T11 feeds ai_trend), so the apply endpoint must be
gated behind a fresh 2FA step-up — not just an authenticated session.
"""

from fastapi.routing import APIRoute

from app.api.deps import get_current_admin_user, require_step_up
from app.main import app

_APPLY = "/api/v1/ai-backtests/agent-weights/suggestions/{ai_config_id}/apply"


def _dependency_calls(route: APIRoute) -> set[object]:
    calls: set[object] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            calls.add(dep.call)
        stack.extend(dep.dependencies)
    return calls


def _apply_route() -> APIRoute:
    return next(
        r
        for r in app.routes
        if isinstance(r, APIRoute) and r.path == _APPLY and "POST" in r.methods
    )


def test_apply_weight_suggestion_requires_step_up() -> None:
    assert require_step_up in _dependency_calls(_apply_route())


def test_apply_weight_suggestion_requires_admin() -> None:
    # Review C2: applying mutates a shared ai-config, so it is admin-only.
    assert get_current_admin_user in _dependency_calls(_apply_route())
