# -*- coding: utf-8 -*-
"""Deterministic PIT identity helpers for the first stock research slice."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
import hashlib
import json
import math
from typing import Any, Dict, Optional, Sequence

from data_provider.base import normalize_stock_code
from src.services.stock_code_utils import _infer_cn_exchange
from src.utils.analysis_metadata import RESEARCH_SELECTION_SOURCES


PIT_IDENTITY_VERSION = "pit-identity-v1"
ASSET_IDENTITY_VERSION = "cn-stock-asset-v1"
DATA_SNAPSHOT_IDENTITY_VERSION = "completed-daily-history-v1"
RESEARCH_SELECTION_CONTEXT_VERSION = "research-selection-context-v1"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def sha256_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_cn_stock_asset_identity(stock_code: Any, market: Any) -> Optional[Dict[str, Any]]:
    market_text = str(market or "").strip().lower()
    if market_text != "cn":
        return None
    normalized = normalize_stock_code(str(stock_code or "").strip())
    if not (normalized.isdigit() and len(normalized) == 6):
        return None
    exchange = _infer_cn_exchange(normalized)
    if not exchange:
        return None
    payload = {
        "version": ASSET_IDENTITY_VERSION,
        "market": "cn",
        "instrument_type": "stock",
        "symbol": normalized,
        "exchange": exchange,
        "calendar": "XSHG",
        "timezone": "Asia/Shanghai",
        "currency": "CNY",
    }
    return {**payload, "identity_hash": sha256_payload(payload)}


def proven_adjustment_basis(provider_identity: Any) -> Optional[str]:
    """Return an adjustment basis only for exact code-proven provider routes."""
    text = str(provider_identity or "").strip().lower()
    if not text:
        return None
    if "akshare" in text or "tencentfetcher" in text:
        return "qfq"
    return None


def build_completed_history_identity(
    frame: Any,
    *,
    stock_code: Any,
    market: Any,
    target_date: date,
    observed_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Hash the exact normalized completed-history bytes consumed by M/W/D evidence."""
    rows = []
    sources = []
    for record in frame.to_dict("records"):
        source = str(record.get("data_source") or "").strip()
        if source:
            sources.append(source)
        rows.append(
            {
                "date": _date_text(record.get("date")),
                "open": _finite_number(record.get("open")),
                "high": _finite_number(record.get("high")),
                "low": _finite_number(record.get("low")),
                "close": _finite_number(record.get("close")),
                "volume": _finite_number(record.get("volume")),
                "data_source": source or None,
            }
        )
    unique_sources = sorted(set(sources))
    provider_identity = (
        unique_sources[0]
        if len(unique_sources) == 1 and len(sources) == len(rows)
        else None
    )
    payload = {
        "version": DATA_SNAPSHOT_IDENTITY_VERSION,
        "market": str(market or "").strip().lower() or None,
        "stock_code": normalize_stock_code(str(stock_code or "").strip()),
        "target_date": target_date.isoformat(),
        "rows": rows,
    }
    observed_utc = _aware_utc_naive(observed_at)
    result = {
        "data_snapshot_identity": sha256_payload(payload),
        "data_snapshot_schema_version": DATA_SNAPSHOT_IDENTITY_VERSION,
        "provider_identity": provider_identity,
        "adjustment_basis": proven_adjustment_basis(provider_identity),
    }
    if observed_utc is not None:
        result["snapshot_observed_at"] = observed_utc.isoformat()
        result["available_at_max"] = observed_utc.isoformat()
    return result


def build_bar_sequence_identity(
    bars: Sequence[Any],
    *,
    stock_code: Any,
    market: Any,
    purpose: str,
) -> Dict[str, Any]:
    rows = []
    sources = []
    for bar in bars:
        source = str(getattr(bar, "data_source", None) or "").strip()
        if source:
            sources.append(source)
        rows.append(
            {
                "date": _date_text(getattr(bar, "date", None)),
                "open": _finite_number(getattr(bar, "open", None)),
                "high": _finite_number(getattr(bar, "high", None)),
                "low": _finite_number(getattr(bar, "low", None)),
                "close": _finite_number(getattr(bar, "close", None)),
                "volume": _finite_number(getattr(bar, "volume", None)),
                "data_source": source or None,
            }
        )
    unique_sources = sorted(set(sources))
    provider_identity = (
        unique_sources[0]
        if len(unique_sources) == 1 and len(sources) == len(rows)
        else None
    )
    payload = {
        "version": "forward-bar-sequence-v1",
        "purpose": purpose,
        "market": str(market or "").strip().lower() or None,
        "stock_code": normalize_stock_code(str(stock_code or "").strip()),
        "rows": rows,
    }
    return {
        "data_snapshot_identity": sha256_payload(payload),
        "provider_identity": provider_identity,
        "adjustment_basis": proven_adjustment_basis(provider_identity),
    }


def build_specified_codes_selection_context(
    *,
    raw_selection_source: Any = None,
    query_source: Any = None,
) -> Dict[str, Any]:
    return normalize_research_selection_context(
        {
            "selection_source": "SPECIFIED_CODES",
            "request_origin": str(raw_selection_source or "").strip() or None,
            "query_source": str(query_source or "").strip() or None,
        }
    )


def build_auto_screen_selection_context(provenance: Any) -> Dict[str, Any]:
    source = dict(provenance) if isinstance(provenance, Mapping) else {}
    screening = {
        key: source.get(key)
        for key in (
            "strategy",
            "strategy_version",
            "run_id",
            "market",
            "snapshot_count",
            "snapshot_source",
            "after_filter_count",
            "ranking_mode",
            "selected_count",
        )
        if source.get(key) not in (None, "", [], {})
    }
    selected = []
    for candidate in source.get("selected_candidates") or []:
        if not isinstance(candidate, Mapping):
            continue
        selected.append(
            {
                key: candidate.get(key)
                for key in ("rank", "code", "score", "screen_score", "risk_level", "industry")
                if candidate.get(key) not in (None, "", [], {})
            }
        )
    if selected:
        screening["selected_candidates"] = selected
    return normalize_research_selection_context(
        {"selection_source": "AUTO_SCREEN", "screening": screening}
    )


def normalize_research_selection_context(value: Any) -> Dict[str, Any]:
    payload = dict(value) if isinstance(value, Mapping) else {}
    route = str(payload.get("selection_source") or "SPECIFIED_CODES").strip().upper()
    if route not in RESEARCH_SELECTION_SOURCES:
        route = "SPECIFIED_CODES"
    normalized: Dict[str, Any] = {
        "schema_version": RESEARCH_SELECTION_CONTEXT_VERSION,
        "selection_source": route,
    }
    for key in ("request_origin", "query_source", "universe_snapshot_id"):
        text = str(payload.get(key) or "").strip()
        if text:
            normalized[key] = text
    screening = payload.get("screening")
    if route == "AUTO_SCREEN" and isinstance(screening, Mapping):
        normalized["screening"] = json.loads(canonical_json(dict(screening)))
    normalized["selection_context_hash"] = sha256_payload(normalized)
    return normalized


def _aware_utc_naive(value: Optional[datetime]) -> Optional[datetime]:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _finite_number(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _date_text(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value or "").strip()
    return text[:10] if text else None


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        return enum_value
    raise TypeError(f"unsupported PIT identity value: {type(value).__name__}")
