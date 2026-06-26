"""Run manifest for backtest reproducibility (Finding 7.1/7.2/7.3).

A backtest result is only reproducible if you can pin *exactly* what produced
it: the metric-formula code, the engine logic, the trading-cost parameters and
the input candles. This module builds a ``run_manifest`` block the engines
attach to every backtest response. ``core`` then persists it alongside the
published report (Phase B2), and Phase C replays from it.

Determinism guarantees (the acceptance criteria):

- ``metric_formula_version`` is a content hash of the metric-formula modules
  (:mod:`common` + :mod:`metrics_schema`). It is stable across processes and
  only changes when that code changes — by the same ``feature_code_hash`` idea
  the tech-model uses.
- ``candles_hash`` is a SHA-256 over the OHLCV window, computed from explicit
  byte serialisation of the index + columns (not pandas' internal hashing) so
  identical data yields an identical hash on any pandas version.

``engine_version`` is a manual semver bumped on breaking engine-logic changes;
the content hashes catch silent metric drift, the semver communicates intent.
"""

from __future__ import annotations

import hashlib
import inspect
import platform
from functools import lru_cache
from types import ModuleType
from typing import Any

import numpy as np
import pandas as pd

from app.services.backtesting import common, metrics_schema
from app.services.backtesting.cost_model import CostModel

# Bump manually when engine *logic* changes in a way that alters results.
ENGINE_VERSION = "1.0.0"

_OHLCV_COLUMNS = ("open", "high", "low", "close", "volume")


def _module_source_hash(*modules: ModuleType) -> str:
    digest = hashlib.sha256()
    for module in modules:
        digest.update(inspect.getsource(module).encode("utf-8"))
    return digest.hexdigest()


@lru_cache(maxsize=1)
def metric_formula_version() -> str:
    """Content hash of the modules that define the backtest metric formulas.

    Cached: the module source is fixed for the lifetime of the process, so the
    hash is computed once instead of re-reading + re-hashing both module sources
    on every backtest run and every metrics-schema request.
    """
    return _module_source_hash(common, metrics_schema)


def build_metric_formula_definition(
    requested_version: str | None = None,
) -> dict[str, Any]:
    """Return the metric-formula definition recoverable by version (Finding 7.3).

    A stored run carries a ``metric_formula_version`` (the content hash). This
    exposes the definition behind the *current* code's version — the metric
    catalogue (keys, labels, formula intent). When ``requested_version`` is
    given, ``matches_current`` flags whether the stored run was produced by the
    code running now; a mismatch means the formulas have drifted and the report
    cannot be byte-reproduced without checking out the matching revision.
    """
    current = metric_formula_version()
    definition: dict[str, Any] = {
        "metric_formula_version": current,
        "engine_version": ENGINE_VERSION,
        "metrics_schema": metrics_schema.METRICS_SCHEMA,
    }
    if requested_version is not None:
        definition["requested_version"] = requested_version
        definition["matches_current"] = requested_version == current
    return definition


def candles_hash(candles: pd.DataFrame) -> str:
    """SHA-256 of the OHLCV window via explicit byte serialisation.

    Hashes the index timestamps (int64 nanoseconds) plus each present OHLCV
    column as contiguous float64 bytes. Deterministic for identical data and
    independent of pandas' internal hashing, so a stored hash stays valid across
    library upgrades.
    """
    digest = hashlib.sha256()
    if isinstance(candles.index, pd.DatetimeIndex):
        index_ns = np.asarray(candles.index.values, dtype="datetime64[ns]").astype("int64")
        digest.update(np.ascontiguousarray(index_ns).tobytes())
    for column in _OHLCV_COLUMNS:
        if column not in candles.columns:
            continue
        digest.update(column.encode("utf-8"))
        values = np.ascontiguousarray(candles[column].to_numpy(dtype="float64"))
        digest.update(values.tobytes())
    return digest.hexdigest()


def _iso_or_none(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return pd.Timestamp(value).isoformat()
    except (ValueError, TypeError):
        return None


def data_window(candles: pd.DataFrame) -> dict[str, Any]:
    """Inclusive bounds and bar count of the OHLCV window."""
    bars = int(len(candles))
    start = candles.index[0] if bars else None
    end = candles.index[-1] if bars else None
    return {"start": _iso_or_none(start), "end": _iso_or_none(end), "bars": bars}


def _env_fingerprint() -> dict[str, str]:
    """Short, non-hashed runtime fingerprint for human triage of replays."""
    return {
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "numpy": np.__version__,
    }


def build_run_manifest(
    *,
    engine: str,
    candles: pd.DataFrame,
    cost: CostModel,
    seed: int | None = None,
) -> dict[str, Any]:
    """Assemble the reproducibility manifest for one backtest run."""
    return {
        "engine": engine,
        "engine_version": ENGINE_VERSION,
        "metric_formula_version": metric_formula_version(),
        "cost_model": {
            "fee_pct": cost.fee_pct,
            "slippage_pct": cost.slippage_pct,
            "funding_pct_per_bar": cost.funding_pct_per_bar,
        },
        "candles_hash": candles_hash(candles),
        "data_window": data_window(candles),
        "seed": seed,
        "env": _env_fingerprint(),
    }
