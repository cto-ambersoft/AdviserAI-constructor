import httpx

from app.services.notifications.formatting import (
    NOTIFIABLE_EVENTS,
    format_event,
    toggle_for_event,
)
from app.services.notifications.telegram import (
    TelegramClient,
    TelegramSendStatus,
)

# ─────────────────────────────── formatter ────────────────────────────────


def test_format_open_long_includes_symbol_side_and_levels() -> None:
    text = format_event(
        event_type="position_opened",
        payload={
            "symbol": "BTC/USDT",
            "trend": "LONG",
            "entry_price": 64250.0,
            "quantity": 0.012,
            "sl_price": 63600.0,
            "tp_price": 65500.0,
            "confidence_pct": 78.0,
        },
        message="Position opened from signal.",
    )
    assert "BTC/USDT" in text
    assert "LONG" in text
    assert "🟢" in text
    assert "64,250" in text
    assert "0.012" in text
    assert "78%" in text


def test_format_open_short_uses_red_marker() -> None:
    text = format_event(
        event_type="position_opened",
        payload={"symbol": "ETH/USDT", "trend": "SHORT", "entry_price": 3000.0},
        message="Position opened from signal.",
    )
    assert "SHORT" in text
    assert "🔴" in text


def test_format_close_falls_back_to_message_when_symbol_absent() -> None:
    # position_closed_on_opposite_trend carries no "symbol" key.
    text = format_event(
        event_type="position_closed_on_opposite_trend",
        payload={
            "close_reason": "opposite_trend",
            "close_price": 64000.0,
            "position_side": "LONG",
        },
        message="Position closed due to opposite trend.",
    )
    assert "closed" in text.lower()
    assert "64,000" in text


def test_format_escapes_html_special_characters() -> None:
    text = format_event(
        event_type="strategy_auto_paused",
        payload={"reason": "win<rate> & dd"},
        message="Strategy auto-paused: win<rate> & dd",
    )
    assert "<rate>" not in text  # raw angle brackets must be escaped
    assert "&lt;rate&gt;" in text
    assert "&amp;" in text


def test_every_notifiable_event_formats_without_error() -> None:
    # Empty payloads must not raise — defensive against missing keys.
    for event_type in NOTIFIABLE_EVENTS:
        text = format_event(event_type=event_type, payload={}, message=None)
        assert isinstance(text, str)
        assert text.strip()


def test_toggle_for_event_maps_families() -> None:
    assert toggle_for_event("position_opened") == "open"
    assert toggle_for_event("position_synced_open_from_exchange") == "open"
    assert toggle_for_event("position_manual_closed") == "close"
    assert toggle_for_event("multi_tp_reconciled_via_rest") == "close"
    assert toggle_for_event("kill_switch_triggered") == "risk"
    assert toggle_for_event("portfolio_dd_halt") == "risk"
    assert toggle_for_event("some_unrelated_event") is None


def test_portfolio_dd_halt_is_notifiable_risk_event() -> None:
    # P1-T3 — the portfolio-DD halt must reach the Telegram outbox via the
    # ``risk`` toggle (notify_on_risk).
    assert "portfolio_dd_halt" in NOTIFIABLE_EVENTS
    assert toggle_for_event("portfolio_dd_halt") == "risk"


def test_format_portfolio_dd_halt_surfaces_drawdown_threshold_and_count() -> None:
    text = format_event(
        event_type="portfolio_dd_halt",
        payload={
            "source": "portfolio_dd",
            "worst_dd_pct": 25.0,
            "threshold_pct": 10.0,
            "paused_count": 2,
            "breaching_config_id": 7,
        },
        message=None,
    )
    assert "25" in text  # worst drawdown
    assert "10" in text  # threshold
    assert "2" in text  # paused strategy count
    assert "drawdown" in text.lower() or "halt" in text.lower()


def test_format_portfolio_dd_halt_includes_window_days() -> None:
    # S3 — surface that the drawdown is a historical N-day figure so operators
    # aren't misled into thinking it is live open-position drawdown.
    text = format_event(
        event_type="portfolio_dd_halt",
        payload={
            "worst_dd_pct": 25.0,
            "threshold_pct": 10.0,
            "paused_count": 2,
            "window_days": 30,
        },
        message=None,
    )
    assert "30" in text


def test_format_portfolio_dd_halt_escapes_non_numeric_payload() -> None:
    # S1 — invariant: every interpolated value is numeric (_num) or escaped (_esc);
    # a non-numeric value in a numeric field must never inject raw HTML into the
    # Telegram message.
    text = format_event(
        event_type="portfolio_dd_halt",
        payload={"worst_dd_pct": "<b>x</b>", "threshold_pct": 10.0, "paused_count": 2},
        message=None,
    )
    assert "<b>x</b>" not in text
    assert "&lt;b&gt;" in text


# ──────────────────────────────── client ──────────────────────────────────


def _client_with(handler: object) -> TelegramClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return TelegramClient(token="123:abc", transport=transport)


async def test_send_message_ok_returns_sent_and_targets_send_message() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = request.content
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = _client_with(handler)
    result = await client.send_message(chat_id=42, text="hi")
    assert result.status is TelegramSendStatus.SENT
    assert result.ok is True
    assert "/bot123:abc/sendMessage" in str(seen["url"])


async def test_send_message_429_returns_rate_limited_with_retry_after() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": "Too Many Requests",
                "parameters": {"retry_after": 17},
            },
        )

    client = _client_with(handler)
    result = await client.send_message(chat_id=42, text="hi")
    assert result.status is TelegramSendStatus.RATE_LIMITED
    assert result.retry_after == 17


async def test_send_message_403_returns_forbidden() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked"},
        )

    client = _client_with(handler)
    result = await client.send_message(chat_id=42, text="hi")
    assert result.status is TelegramSendStatus.FORBIDDEN


async def test_send_message_500_returns_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    client = _client_with(handler)
    result = await client.send_message(chat_id=42, text="hi")
    assert result.status is TelegramSendStatus.ERROR


async def test_get_me_returns_username() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"username": "my_trade_bot"}})

    client = _client_with(handler)
    username = await client.get_me_username()
    assert username == "my_trade_bot"
