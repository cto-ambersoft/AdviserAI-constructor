"""Audit follow-up: the new mutating endpoints are critical actions and must be
gated behind a fresh 2FA step-up (same pattern as the exchange-key routes).

Covers:
- A2: PATCH /live/auto-trade/risk-config/apply-all (bulk risk write).
- D3: POST /live/auto-trade/config/{id}/rollback/{revision_id} (config rollback).
"""

from fastapi.routing import APIRoute

from app.api.deps import require_step_up
from app.main import app

_APPLY_ALL = "/api/v1/live/auto-trade/risk-config/apply-all"
_ROLLBACK = "/api/v1/live/auto-trade/config/{config_id}/rollback/{revision_id}"


def _route(path: str, method: str) -> APIRoute:
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == path and method in route.methods:
            return route
    raise AssertionError(f"route {method} {path} not found")


def _dependency_calls(route: APIRoute) -> set[object]:
    calls: set[object] = set()
    stack = list(route.dependant.dependencies)
    while stack:
        dep = stack.pop()
        if dep.call is not None:
            calls.add(dep.call)
        stack.extend(dep.dependencies)
    return calls


def test_bulk_apply_risk_config_requires_step_up() -> None:
    assert require_step_up in _dependency_calls(_route(_APPLY_ALL, "PATCH"))


def test_rollback_config_requires_step_up() -> None:
    assert require_step_up in _dependency_calls(_route(_ROLLBACK, "POST"))
