"""Step-up token validation branches (I6) — expiry, type confinement, garbage."""

from datetime import timedelta

import pytest
from fastapi import HTTPException

from app.core.auth import create_access_token, create_step_up_token, decode_step_up_token


def test_step_up_token_round_trips() -> None:
    token, expires_in = create_step_up_token(subject="a@x.io")
    subject, jti = decode_step_up_token(token)
    assert subject == "a@x.io"
    assert jti  # unique id for single-use enforcement
    assert expires_in > 0


def test_step_up_tokens_have_distinct_jti() -> None:
    _, _ = create_step_up_token(subject="a@x.io")
    t1, _ = create_step_up_token(subject="a@x.io")
    t2, _ = create_step_up_token(subject="a@x.io")
    assert decode_step_up_token(t1)[1] != decode_step_up_token(t2)[1]


def test_expired_step_up_token_is_rejected() -> None:
    token, _ = create_step_up_token(subject="a@x.io", expires_delta=timedelta(seconds=-1))
    with pytest.raises(HTTPException) as exc:
        decode_step_up_token(token)
    assert exc.value.status_code == 403


def test_access_token_cannot_be_used_as_step_up() -> None:
    token, _ = create_access_token(subject="a@x.io")
    with pytest.raises(HTTPException) as exc:
        decode_step_up_token(token)
    assert exc.value.status_code == 403


def test_garbage_token_is_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        decode_step_up_token("not.a.jwt")
    assert exc.value.status_code == 403
