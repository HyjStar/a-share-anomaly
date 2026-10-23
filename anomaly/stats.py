"""区间统计与网页数据组装。

区间按“交易所交易日”计算，而不是按系统运行了几天计算；同时单独给出
covered_sessions，前端可以提示新系统还在积累数据，避免把覆盖不足误读成
“这段时间没有异动”。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Any

from .config import Config, threshold_chips
from .storage import Storage
from .utils import iso, shanghai_now

WINDOWS = (5, 10, 20)


def _round(value: float | None, digits: int = 2) -> float | None:
    if value is None:
        return None
    return round(float(value), digits)


def _aggregate_window(
    storage: Storage,
    config: Config,
    *,
    kind: str,
    window: int,
    latest: date,
    sessions: list[date],
    covered_sessions: list[date],
    baseline: date | None,
) -> dict[str, Any]:
    if not sessions:
        return {
            "window": window,
            "sessions": [],
            "covered_sessions": [],
            "sessions_available": 0,
            "window_days": window,
            "baseline_date": None,
            "stocks": [],
            "sectors": [],
        }

    window_start = sessions[0]
    anomalies = storage.get_anomalies(kind, window_start, latest)
    by_code: dict[str, dict[str, Any]] = {}
    for item in anomalies:
        code = str(item.get("code") or item.get("name") or "")
        if not code:
            continue
        day = date.fromisoformat(item["trade_date"])
        entry = by_code.setdefault(
            code,
            {
                "code": code,
                "name": item.get("name", ""),
                "industry": item.get("industry", ""),
                "category": item.get("category", ""),
                "kind": item.get("kind", ""),
                "dates": [],
                "score_values": [],
                "net_inflow_values": [],
                "last": None,
                "last_date": None,
            },
        )
        if day not in entry["dates"]:
            entry["dates"].append(day)
        entry["score_values"].append(float(item.get("score") or 0.0))
        entry["net_inflow_values"].append(float(item.get("main_net_inflow") or 0.0))
        if entry["last_date"] is None or day >= entry["last_date"]:
            entry["last_date"] = day
            entry["last"] = item
            entry["name"] = item.get("name", entry["name"])
            entry["industry"] = item.get("industry", entry["industry"])
            entry["category"] = item.get("category", entry["category"])
            entry["kind"] = item.get("kind", entry["kind"])

    closes = storage.get_closes(kind, by_code.keys(), asof=latest, baseline=baseline)
    rows: list[dict[str, Any]] = []
    for code, entry in by_code.items():
        close_info = closes.get(code, {})
        last = entry["last"] or {}
        range_return = close_info.get("range_return_pct")
        dates = sorted(entry["dates"])
        scores = entry["score_values"]
        rows.append(
            {
                "code": code,
                "name": entry["name"],
                "industry": entry["industry"],
                "category": entry["category"],
                "kind": entry["kind"],
                "appearances": len(dates),
                "dates": [iso(value) for value in dates],
                "first_date": iso(dates[0]) if dates else None,
                "last_date": iso(entry["last_date"]) if entry["last_date"] else None,
                "last_pct": last.get("pct"),
                "last_score": last.get("score"),
                "max_score": round(max(scores), 1) if scores else None,
                "cumulative_score": round(sum(scores), 1),
                "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
                "cumulative_net_inflow": round(sum(entry["net_inflow_values"]), 2),
                "range_return_pct": _round(range_return),
                "range_start_date": close_info.get("baseline_date"),
                "range_end_date": close_info.get("latest_date"),
                "range_available": bool(
                    close_info.get("baseline_close") and close_info.get("latest_close")
                ),
                "last": last,
            }
        )

    rows.sort(
        key=lambda row: (
            -row["appearances"],
            -row["cumulative_score"],
            -(
                row["range_return_pct"]
                if row["range_return_pct"] is not None
                else -9999
            ),
            -row["last_score"] if row["last_score"] is not None else 0,
        )
    )
    payload: dict[str, Any] = {
        "window": window,
        "sessions": [iso(item) for item in sessions],
        "covered_sessions": [iso(item) for item in covered_sessions],
        "sessions_available": len(covered_sessions),
        "window_days": len(sessions),
        "baseline_date": iso(baseline) if baseline else None,
        "coverage_pct": round(len(covered_sessions) / len(sessions) * 100.0, 1)
        if sessions
        else 0.0,
    }
    if kind == "stock":
        payload["stocks"] = rows
        payload["sectors"] = []
    else:
        payload["sectors"] = rows
        payload["stocks"] = []
    return payload


def _build_summary(
    storage: Storage,
    config: Config,
    latest: date,
    *,
    history_dates: list[date],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for window in WINDOWS:
        sessions = storage.get_trading_dates_ending(latest, window)
        if sessions:
            covered = [day for day in sessions if day in set(history_dates)]
            baseline = storage.get_previous_trade_date(sessions[0])
        else:
            covered = []
            baseline = None
        stock_window = _aggregate_window(
            storage,
            config,
            kind="stock",
            window=window,
            latest=latest,
            sessions=sessions,
            covered_sessions=covered,
            baseline=baseline,
        )
        sector_window = _aggregate_window(
            storage,
            config,
            kind="sector",
            window=window,
            latest=latest,
            sessions=sessions,
            covered_sessions=covered,
            baseline=baseline,
        )
        stock_window["sectors"] = sector_window["sectors"]
        result[str(window)] = stock_window
    return result


def _build_history(
    storage: Storage,
    config: Config,
    latest: date,
    history_dates: list[date],
) -> list[dict[str, Any]]:
    if not history_dates:
        return []
    earliest = history_dates[-1]
    stock_anomalies = storage.get_anomalies("stock", earliest, latest)
    sector_anomalies = storage.get_anomalies("sector", earliest, latest)
    stock_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sector_by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in stock_anomalies:
        stock_by_date[item["trade_date"]].append(item)
    for item in sector_anomalies:
        sector_by_date[item["trade_date"]].append(item)

    entries: list[dict[str, Any]] = []
    for day in history_dates:
        day_text = iso(day)
        run = storage.get_run(day) or {}
        stocks = sorted(
            stock_by_date.get(day_text, []),
            key=lambda item: item.get("score") or 0,
            reverse=True,
        )
        sectors = sorted(
            sector_by_date.get(day_text, []),
            key=lambda item: item.get("score") or 0,
            reverse=True,
        )
        entries.append(
            {
                "trade_date": day_text,
                "finished_at": run.get("finished_at"),
                "stock_count": len(stocks),
                "sector_count": len(sectors),
                "stocks": stocks,
                "sectors": sectors,
            }
        )
    return entries


def _empty_payload(config: Config, now: datetime) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for window in WINDOWS:
        summary[str(window)] = {
            "window": window,
            "sessions": [],
            "covered_sessions": [],
            "sessions_available": 0,
            "window_days": window,
            "baseline_date": None,
            "coverage_pct": 0.0,
            "stocks": [],
            "sectors": [],
        }
    return {
        "meta": {
            "has_data": False,
            "generated_at": now.isoformat(timespec="seconds"),
            "latest_trade_date": None,
            "latest_run_at": None,
            "data_source": "AkShare / 东方财富",
            "title": config["site"]["title"],
            "subtitle": config["site"]["subtitle"],
            "disclaimer": config["site"]["disclaimer"],
            "thresholds": threshold_chips(config),
            "score_note": "异动强度 = 放量、涨幅、主力净流入占比、跌幅深度四因子加权，0–100 分。",
            "window_note": "区间统计按交易日累计，只统计系统成功运行且落库的日期。",
            "counts": {"stock": 0, "sector": 0},
            "history_days": 0,
            "score_formula": {},
        },
        "today": {"trade_date": None, "stocks": [], "sectors": []},
        "summary": summary,
        "history": [],
    }


def build_web_payload(
    storage: Storage,
    config: Config,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or shanghai_now(config.timezone)
    latest = storage.latest_run_date()
    if latest is None:
        return _empty_payload(config, now)

    latest_run = storage.get_run(latest) or {}
    latest_meta = latest_run.get("meta") or {}
    history_limit = int(config["app"]["web_history_days"])
    history_dates = storage.get_run_dates(limit=history_limit, end_date=latest)
    today_stocks = sorted(
        storage.get_anomalies("stock", latest, latest),
        key=lambda item: item.get("score") or 0,
        reverse=True,
    )
    today_sectors = sorted(
        storage.get_anomalies("sector", latest, latest),
        key=lambda item: item.get("score") or 0,
        reverse=True,
    )
    summary = _build_summary(storage, config, latest, history_dates=history_dates)
    history = _build_history(storage, config, latest, history_dates)
    db_stats = storage.stats()

    total_events = {
        "stock": sum(len(day.get("stocks", [])) for day in history),
        "sector": sum(len(day.get("sectors", [])) for day in history),
    }
    meta = {
        "has_data": True,
        "generated_at": now.isoformat(timespec="seconds"),
        "latest_trade_date": iso(latest),
        "latest_run_at": latest_run.get("finished_at"),
        "data_source": "AkShare / 东方财富",
        "title": config["site"]["title"],
        "subtitle": config["site"]["subtitle"],
        "disclaimer": config["site"]["disclaimer"],
        "thresholds": threshold_chips(config),
        "score_note": "异动强度 = 放量、涨幅、主力净流入占比、跌幅深度四因子加权，0–100 分。",
        "window_note": "区间统计按交易所交易日计算；新系统需要逐日积累，页面会显示已覆盖的交易日数。",
        "counts": {"stock": len(today_stocks), "sector": len(today_sectors)},
        "history_days": len(history),
        "history_event_counts": total_events,
        "db_stats": db_stats,
        "latest_run": {
            "finished_at": latest_run.get("finished_at"),
            "meta": latest_meta,
        },
        "score_formula": {
            "stock": config["scoring"]["stock"],
            "sector": config["scoring"]["sector"],
        },
    }
    return {
        "meta": meta,
        "today": {
            "trade_date": iso(latest),
            "stocks": today_stocks,
            "sectors": today_sectors,
        },
        "summary": summary,
        "history": history,
    }
