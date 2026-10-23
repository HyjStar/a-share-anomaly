"""异动判定与强度评分。

设计原则：
1. 四个条件全部为 AND；任何一个不满足都不会进入最终列表。
2. 判定基于“前复权日线”，量比基于真实的前 5 日均量重新计算，
   不使用行情快照里可能受盘中时点影响的量比。
3. 返回统一的失败原因，方便调阈值时看漏斗，而不是只看到 0 条。
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date
from typing import Any

import pandas as pd

from .config import Config
from .utils import format_amount_cn, safe_float


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def history_analysis(
    history: pd.DataFrame,
    trade_date: date,
    trend_cfg: Mapping[str, Any],
    volume_cfg: Mapping[str, Any],
) -> dict[str, Any]:
    """从日线计算趋势与放量指标。

    返回 ``ok=False`` 时 ``reason`` 说明是哪一步不满足；``metrics`` 始终尽量填充，
    方便调试和页面展示。
    """
    lookback = int(trend_cfg["lookback_days"])
    ma_period = int(trend_cfg["ma_period"])
    prior_down_days = int(trend_cfg["prior_down_days"])
    avg_days = int(volume_cfg["avg_days"])
    required = max(lookback + 1, ma_period, prior_down_days + 1, avg_days + 1)

    empty_metrics = {
        "ret_20d": None,
        "ma20_gap": None,
        "down_streak": 0,
        "volume_ratio": None,
        "amount_ratio": None,
        "turnover_ratio": None,
        "close": None,
        "amount": None,
        "trade_date_ok": False,
        "history_bars": 0,
    }
    if history is None or history.empty:
        return {"ok": False, "reason": "无历史K线", "metrics": empty_metrics}

    frame = history.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.date
    frame = (
        frame.dropna(subset=["date", "close"])
        .sort_values("date")
        .drop_duplicates("date", keep="last")
    )
    frame = frame[frame["date"] <= trade_date].reset_index(drop=True)
    if frame.empty:
        return {"ok": False, "reason": "无有效历史K线", "metrics": empty_metrics}

    metrics = dict(empty_metrics)
    metrics["history_bars"] = len(frame)
    last_date = frame.iloc[-1]["date"]
    metrics["trade_date_ok"] = last_date == trade_date
    if last_date != trade_date:
        return {
            "ok": False,
            "reason": f"当日无K线（最后为 {last_date}，停牌或数据未更新）",
            "metrics": metrics,
        }
    if len(frame) < required:
        return {
            "ok": False,
            "reason": f"历史数据不足（{len(frame)} 根，需至少 {required} 根）",
            "metrics": metrics,
        }

    today = frame.iloc[-1]
    prior = frame.iloc[:-1]
    close_today = float(today["close"])
    metrics["close"] = close_today
    metrics["amount"] = _finite(today.get("amount"))

    lookback_close = float(frame.iloc[-(lookback + 1)]["close"])
    if lookback_close <= 0:
        return {"ok": False, "reason": "历史收盘价异常", "metrics": metrics}
    ret_20d = (close_today / lookback_close - 1.0) * 100.0
    metrics["ret_20d"] = ret_20d

    ma_window = frame.tail(ma_period)["close"].astype(float)
    ma_value = float(ma_window.mean())
    if ma_value <= 0:
        return {"ok": False, "reason": "MA 计算异常", "metrics": metrics}
    ma20_gap = (close_today / ma_value - 1.0) * 100.0
    metrics["ma20_gap"] = ma20_gap

    # “收阴或下跌”：收盘低于开盘，或涨跌幅为负。优先用接口涨跌幅；
    # 缺失时用当前收盘与上一收盘比较兜底。
    down_streak = 0
    for index in range(len(prior) - 1, -1, -1):
        row = prior.iloc[index]
        pct = _finite(row.get("pct"))
        open_price = _finite(row.get("open"))
        close = _finite(row.get("close"))
        previous_close = _finite(frame.iloc[index - 1]["close"]) if index > 0 else None
        down = False
        if pct is not None:
            down = pct < 0
        elif close is not None and previous_close is not None:
            down = close < previous_close
        if open_price is not None and close is not None and close < open_price:
            down = True
        if not down:
            break
        down_streak += 1
    metrics["down_streak"] = down_streak

    avg_prior = prior.tail(avg_days)
    avg_volume = (
        float(avg_prior["volume"].mean()) if "volume" in avg_prior else float("nan")
    )
    avg_amount = (
        float(avg_prior["amount"].mean()) if "amount" in avg_prior else float("nan")
    )
    avg_turnover = (
        float(avg_prior["turnover"].mean()) if "turnover" in avg_prior else float("nan")
    )
    today_volume = _finite(today.get("volume"))
    today_amount = _finite(today.get("amount"))
    today_turnover = _finite(today.get("turnover"))

    def ratio(current: float | None, average: float) -> float | None:
        if current is None or not math.isfinite(average) or average <= 0:
            return None
        return current / average

    volume_ratio = ratio(today_volume, avg_volume)
    amount_ratio = ratio(today_amount, avg_amount)
    turnover_ratio = ratio(today_turnover, avg_turnover)
    metrics["volume_ratio"] = volume_ratio
    metrics["amount_ratio"] = amount_ratio
    metrics["turnover_ratio"] = turnover_ratio

    min_drop_pct = float(trend_cfg["min_drop_pct"])
    min_ma_discount = float(trend_cfg["ma_discount_pct"])
    ret_ok = ret_20d <= -min_drop_pct
    ma_ok = ma20_gap <= -min_ma_discount
    if not (ret_ok or ma_ok):
        return {
            "ok": False,
            "reason": f"不处于下跌趋势（20日 {ret_20d:.2f}%，MA20偏离 {ma20_gap:.2f}%）",
            "metrics": metrics,
        }
    if down_streak < prior_down_days:
        return {
            "ok": False,
            "reason": f"前 {prior_down_days} 日未连续收阴/下跌（实际连续 {down_streak} 日）",
            "metrics": metrics,
        }

    min_volume_ratio = float(volume_cfg["min_volume_ratio"])
    min_turnover_ratio = float(volume_cfg["min_turnover_ratio"])
    volume_ok = volume_ratio is not None and volume_ratio >= min_volume_ratio
    turnover_ok = bool(volume_cfg.get("use_turnover_ratio")) and (
        turnover_ratio is not None and turnover_ratio >= min_turnover_ratio
    )
    if not (volume_ok or turnover_ok):
        volume_text = f"{volume_ratio:.2f}" if volume_ratio is not None else "—"
        turnover_text = f"{turnover_ratio:.2f}" if turnover_ratio is not None else "—"
        return {
            "ok": False,
            "reason": f"未放量（量比 {volume_text}，换手率放大 {turnover_text}）",
            "metrics": metrics,
        }

    return {"ok": True, "reason": "", "metrics": metrics}


def _score(
    *,
    gain_pct: float,
    volume_ratio: float | None,
    turnover_ratio: float | None,
    fund_ratio: float,
    drop_depth: float,
    scoring_cfg: Mapping[str, Any],
    use_turnover: bool,
) -> tuple[float, dict[str, Any]]:
    """四因子 0–100 归一化后加权。

    放量因子取量比和换手率放大倍数中较高者，与“任一满足”的判定保持一致。
    """
    weights = {key: float(value) for key, value in scoring_cfg["weights"].items()}
    marks = {key: float(value) for key, value in scoring_cfg["full_marks"].items()}
    total_weight = sum(weights.values())

    vol_values = []
    if volume_ratio is not None:
        vol_values.append(volume_ratio)
    if use_turnover and turnover_ratio is not None:
        vol_values.append(turnover_ratio)
    effective_volume = max(vol_values) if vol_values else 0.0

    factors = {
        "volume": _clamp(effective_volume / marks["volume_ratio"]) * 100.0,
        "gain": _clamp(max(gain_pct, 0.0) / marks["gain_pct"]) * 100.0,
        "fund": _clamp(max(fund_ratio, 0.0) / marks["fund_ratio_pct"]) * 100.0,
        "drop": _clamp(max(drop_depth, 0.0) / marks["drop_pct"]) * 100.0,
    }
    weighted = sum(factors[key] * weights[key] for key in factors) / total_weight
    parts = {
        "factors": {key: round(value, 1) for key, value in factors.items()},
        "weights": weights,
        "effective_volume_ratio": round(effective_volume, 2),
        "contributions": {
            key: round(factors[key] * weights[key] / total_weight, 1) for key in factors
        },
    }
    return round(weighted, 1), parts


def _common_reasons(
    metrics: Mapping[str, Any],
    gain_pct: float,
    fund_ratio: float | None,
    net_inflow: float,
) -> list[str]:
    reasons = []
    ret_20d = metrics.get("ret_20d")
    ma_gap = metrics.get("ma20_gap")
    if ret_20d is not None and ret_20d <= 0:
        reasons.append(f"20日跌幅 {abs(ret_20d):.2f}%")
    if ma_gap is not None and ma_gap <= 0:
        reasons.append(f"低于MA20 {abs(ma_gap):.2f}%")
    reasons.append(f"前{int(metrics.get('down_streak', 0))}日连跌")
    volume_ratio = metrics.get("volume_ratio")
    turnover_ratio = metrics.get("turnover_ratio")
    volume_bits = []
    if volume_ratio is not None:
        volume_bits.append(f"量比 {volume_ratio:.2f}×")
    if turnover_ratio is not None:
        volume_bits.append(f"换手放大 {turnover_ratio:.2f}×")
    if volume_bits:
        reasons.append(" / ".join(volume_bits))
    reasons.append(f"涨幅 {gain_pct:+.2f}%")
    ratio_text = (
        f"{fund_ratio:.2f}%"
        if fund_ratio is not None and math.isfinite(fund_ratio)
        else "—"
    )
    reasons.append(
        f"主力净流入 {format_amount_cn(net_inflow)}（占成交额 {ratio_text}）"
    )
    return reasons


def evaluate_stock(
    row: Mapping[str, Any],
    history: pd.DataFrame,
    config: Config,
    trade_date: date,
    industry: str = "",
) -> dict[str, Any]:
    """对单只股票执行条件 1~4 的完整判定。"""
    section = config["stock"]
    gain_pct = safe_float(row.get("pct"))
    if not math.isfinite(gain_pct) or gain_pct < float(section["min_gain_pct"]):
        return {"eligible": False, "reason": "涨幅未达标", "stage": "gain"}

    history_result = history_analysis(
        history, trade_date, section["trend"], section["volume"]
    )
    if not history_result["ok"]:
        reason = history_result["reason"]
        stage = "history"
        if "下跌趋势" in reason:
            stage = "trend"
        elif "未连续" in reason:
            stage = "trend_streak"
        elif "未放量" in reason:
            stage = "volume"
        return {
            "eligible": False,
            "reason": reason,
            "stage": stage,
            "metrics": history_result["metrics"],
        }

    metrics = history_result["metrics"]
    fund_cfg = section["fund"]
    net_inflow = safe_float(row.get("fund_net_inflow"))
    fund_ratio = safe_float(row.get("fund_ratio"))
    if not math.isfinite(net_inflow):
        return {
            "eligible": False,
            "reason": "资金流数据缺失",
            "stage": "fund",
            "metrics": metrics,
        }
    if bool(fund_cfg["require_positive"]) and net_inflow <= 0:
        return {
            "eligible": False,
            "reason": "主力净流入为负",
            "stage": "fund",
            "metrics": metrics,
        }
    if not math.isfinite(fund_ratio) or fund_ratio < float(
        fund_cfg["min_net_inflow_ratio_pct"]
    ):
        return {
            "eligible": False,
            "reason": "主力净流入占比未达标",
            "stage": "fund",
            "metrics": metrics,
        }

    drop_depth = max(
        0.0,
        -float(metrics["ret_20d"]) if metrics["ret_20d"] is not None else 0.0,
        -float(metrics["ma20_gap"]) if metrics["ma20_gap"] is not None else 0.0,
    )
    score, score_parts = _score(
        gain_pct=gain_pct,
        volume_ratio=metrics["volume_ratio"],
        turnover_ratio=metrics["turnover_ratio"],
        fund_ratio=fund_ratio,
        drop_depth=drop_depth,
        scoring_cfg=config["scoring"]["stock"],
        use_turnover=bool(section["volume"]["use_turnover_ratio"]),
    )
    reasons = _common_reasons(metrics, gain_pct, fund_ratio, net_inflow)
    anomaly = {
        "code": str(row["code"]).zfill(6),
        "name": row["name"],
        "industry": industry,
        "pct": round(gain_pct, 2),
        "price": round(float(row["price"]), 2)
        if _finite(row.get("price")) is not None
        else None,
        "amount": round(float(metrics["amount"]), 2)
        if metrics["amount"] is not None
        else None,
        "volume_ratio": round(float(metrics["volume_ratio"]), 2)
        if metrics["volume_ratio"] is not None
        else None,
        "turnover_ratio": (
            round(float(metrics["turnover_ratio"]), 2)
            if metrics["turnover_ratio"] is not None
            else None
        ),
        "amount_ratio": round(float(metrics["amount_ratio"]), 2)
        if metrics["amount_ratio"] is not None
        else None,
        "main_net_inflow": round(net_inflow, 2),
        "fund_ratio": round(fund_ratio, 2),
        "ret_20d": round(float(metrics["ret_20d"]), 2),
        "ma20_gap": round(float(metrics["ma20_gap"]), 2),
        "down_streak": int(metrics["down_streak"]),
        "drop_depth": round(drop_depth, 2),
        "score": score,
        "score_parts": score_parts,
        "reasons": reasons,
        "trigger": "；".join(reasons),
    }
    return {"eligible": True, "reason": "", "anomaly": anomaly, "metrics": metrics}


def evaluate_sector(
    row: Mapping[str, Any],
    history: pd.DataFrame,
    config: Config,
    trade_date: date,
) -> dict[str, Any]:
    """对单个行业/概念板块执行完整判定。"""
    section = config["sector"]
    gain_pct = safe_float(row.get("pct"))
    if not math.isfinite(gain_pct) or gain_pct < float(section["min_gain_pct"]):
        return {"eligible": False, "reason": "板块涨幅未达标", "stage": "gain"}

    history_result = history_analysis(
        history, trade_date, section["trend"], section["volume"]
    )
    if not history_result["ok"]:
        reason = history_result["reason"]
        stage = "history"
        if "下跌趋势" in reason:
            stage = "trend"
        elif "未连续" in reason:
            stage = "trend_streak"
        elif "未放量" in reason:
            stage = "volume"
        return {
            "eligible": False,
            "reason": reason,
            "stage": stage,
            "metrics": history_result["metrics"],
        }

    metrics = history_result["metrics"]
    fund_cfg = section["fund"]
    net_inflow = safe_float(row.get("fund_net_inflow"))
    fund_ratio = safe_float(row.get("fund_ratio"))
    if not math.isfinite(net_inflow):
        return {
            "eligible": False,
            "reason": "板块资金流缺失",
            "stage": "fund",
            "metrics": metrics,
        }
    if bool(fund_cfg["require_positive"]) and net_inflow <= 0:
        return {
            "eligible": False,
            "reason": "板块主力净流入为负",
            "stage": "fund",
            "metrics": metrics,
        }
    rank = int(row.get("fund_rank") or 10**9)
    rank_ok = rank <= int(fund_cfg["rank_top_n"])
    ratio_ok = math.isfinite(fund_ratio) and fund_ratio >= float(
        fund_cfg["min_net_inflow_ratio_pct"]
    )
    if not (rank_ok or ratio_ok):
        ratio_text = f"{fund_ratio:.2f}" if math.isfinite(fund_ratio) else "—"
        return {
            "eligible": False,
            "reason": f"板块资金未居前（净流入排名 {rank}，占比 {ratio_text}%）",
            "stage": "fund",
            "metrics": metrics,
        }

    ratio_for_score = fund_ratio if math.isfinite(fund_ratio) else 0.0
    drop_depth = max(
        0.0,
        -float(metrics["ret_20d"]) if metrics["ret_20d"] is not None else 0.0,
        -float(metrics["ma20_gap"]) if metrics["ma20_gap"] is not None else 0.0,
    )
    score, score_parts = _score(
        gain_pct=gain_pct,
        volume_ratio=metrics["volume_ratio"],
        turnover_ratio=metrics["turnover_ratio"],
        fund_ratio=ratio_for_score,
        drop_depth=drop_depth,
        scoring_cfg=config["scoring"]["sector"],
        use_turnover=bool(section["volume"]["use_turnover_ratio"]),
    )
    kind = str(row.get("kind", "concept"))
    category = "行业板块" if kind == "industry" else "概念板块"
    reasons = _common_reasons(
        metrics, gain_pct, fund_ratio if math.isfinite(fund_ratio) else None, net_inflow
    )
    if rank_ok:
        reasons.append(f"资金净流入排名第 {rank}")
    anomaly = {
        "code": str(row.get("code", "")),
        "name": row["name"],
        "kind": kind,
        "category": category,
        "pct": round(gain_pct, 2),
        "price": round(float(row["price"]), 2)
        if _finite(row.get("price")) is not None
        else None,
        "volume_ratio": round(float(metrics["volume_ratio"]), 2)
        if metrics["volume_ratio"] is not None
        else None,
        "turnover_ratio": (
            round(float(metrics["turnover_ratio"]), 2)
            if metrics["turnover_ratio"] is not None
            else None
        ),
        "amount_ratio": round(float(metrics["amount_ratio"]), 2)
        if metrics["amount_ratio"] is not None
        else None,
        "main_net_inflow": round(net_inflow, 2),
        "fund_ratio": round(fund_ratio, 2) if math.isfinite(fund_ratio) else None,
        "fund_rank": rank,
        "ret_20d": round(float(metrics["ret_20d"]), 2),
        "ma20_gap": round(float(metrics["ma20_gap"]), 2),
        "down_streak": int(metrics["down_streak"]),
        "drop_depth": round(drop_depth, 2),
        "leading_stock": row.get("leading_stock") or "",
        "up_count": int(row.get("up_count") or 0),
        "down_count": int(row.get("down_count") or 0),
        "score": score,
        "score_parts": score_parts,
        "reasons": reasons,
        "trigger": "；".join(reasons),
    }
    return {"eligible": True, "reason": "", "anomaly": anomaly, "metrics": metrics}
