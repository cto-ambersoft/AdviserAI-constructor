"""T3 (S1/S2): changing or deleting an exchange API key is a critical action and
must be gated behind a fresh 2FA step-up, exactly like creating one. Asserts the
``require_step_up`` dependency is actually wired onto the PATCH/DELETE routes
(the audit found them on plain ``CurrentUser``).
"""

from fastapi.routing import APIRoute

from app.api.deps import require_step_up
from app.main import app

_ACCOUNT_ITEM = "/api/v1/exchange/accounts/{account_id}"
_ACCOUNTS = "/api/v1/exchange/accounts"


def _route(path: str, method: str) -> APIRoute:
    for route in app.routes:
        if (
            isinstance(route, APIRoute)
            and route.path == path
            and method in route.methods
        ):
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


def test_update_account_requires_step_up() -> None:
    assert require_step_up in _dependency_calls(_route(_ACCOUNT_ITEM, "PATCH"))


def test_delete_account_requires_step_up() -> None:
    assert require_step_up in _dependency_calls(_route(_ACCOUNT_ITEM, "DELETE"))


def test_create_account_still_requires_step_up() -> None:
    # Regression guard for the already-gated create path.
    assert require_step_up in _dependency_calls(_route(_ACCOUNTS, "POST"))


def test_encrypt_oracle_is_removed() -> None:
    # T4 (S6): the unauthenticated /exchange/encrypt crypto-oracle must not exist.
    paths = {route.path for route in app.routes if isinstance(route, APIRoute)}
    assert "/api/v1/exchange/encrypt" not in paths
