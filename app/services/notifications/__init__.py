"""Outbound user notifications (Telegram, phase 1).

The trade engine persists every event to ``auto_trade_events`` via
``AutoTradeService._emit_event``. This package treats that table as a durable
outbox: a periodic dispatcher reads new notifiable events, formats them, and
delivers them over Telegram — without touching the trading hot path.
"""
