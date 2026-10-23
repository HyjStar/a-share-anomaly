"""配置加载与校验。

配置文件是唯一的阈值入口，代码中不散落魔法数字。加载后会做基础范围校验，
尽早暴露 typo，而不是等跑完数据才得到 0 条异动。
"""

from __future__ import annotations

import re
from datetime import time
from pathlib import Path
from typing import Any

import yaml


class ConfigError(ValueError):
    """配置内容不合法。"""


class Config:
    """轻量配置对象，保留字典访问方式，便于测试与打印。"""

    def __init__(self, data: dict[str, Any], path: Path):
        self.data = data
        self.path = path
        self.base_dir = path.resolve().parent
        self.warnings: list[str] = []

    def __getitem__(self, key: str) -> Any:
        return self.data[key]

    def __repr__(self) -> str:
        return f"Config(path={str(self.path)!r})"

    def path_for(self, key: str) -> Path:
        """把配置中的相对路径解析到配置文件所在目录。"""
        value = self.data["app"][key]
        path = Path(value)
        return path if path.is_absolute() else self.base_dir / path

    @property
    def min_run_time(self) -> time:
        raw = str(self.data["app"]["min_run_time"])
        try:
            hour, minute = (int(part) for part in raw.split(":", 1))
            return time(hour, minute)
        except (TypeError, ValueError) as exc:
            raise ConfigError(
                f"app.min_run_time 必须是 HH:MM 格式，收到 {raw!r}"
            ) from exc

    @property
    def timezone(self) -> str:
        return str(self.data["app"]["timezone"])


def _require_keys(mapping: dict[str, Any], keys: set[str], where: str) -> None:
    missing = keys - mapping.keys()
    if missing:
        raise ConfigError(f"{where} 缺少配置项: {', '.join(sorted(missing))}")


def _warn_unknown_keys(
    mapping: dict[str, Any], keys: set[str], where: str
) -> list[str]:
    unknown = mapping.keys() - keys
    if not unknown:
        return []
    return [f"未知配置项 {where}.{key}（已忽略）" for key in sorted(unknown)]


def _positive_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{where} 必须是正数，收到 {value!r}")
    return float(value)


def _non_negative_number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ConfigError(f"{where} 必须是非负数，收到 {value!r}")
    return float(value)


def _validate_scoring(section: dict[str, Any], where: str) -> None:
    _require_keys(section, {"weights", "full_marks"}, where)
    weights = section["weights"]
    marks = section["full_marks"]
    required_weights = {"volume", "gain", "fund", "drop"}
    required_marks = {"volume_ratio", "gain_pct", "fund_ratio_pct", "drop_pct"}
    _require_keys(weights, required_weights, f"{where}.weights")
    _require_keys(marks, required_marks, f"{where}.full_marks")
    for key in required_weights:
        _non_negative_number(weights[key], f"{where}.weights.{key}")
    if sum(float(weights[key]) for key in required_weights) <= 0:
        raise ConfigError(f"{where}.weights 之和必须大于 0")
    for key in required_marks:
        _positive_number(marks[key], f"{where}.full_marks.{key}")


def _validate_rule_section(
    section: dict[str, Any], where: str, *, is_sector: bool
) -> None:
    _require_keys(
        section,
        {"min_gain_pct", "trend", "volume", "fund"},
        where,
    )
    _non_negative_number(section["min_gain_pct"], f"{where}.min_gain_pct")

    trend = section["trend"]
    _require_keys(
        trend,
        {
            "lookback_days",
            "min_drop_pct",
            "ma_period",
            "ma_discount_pct",
            "prior_down_days",
        },
        f"{where}.trend",
    )
    for key in ("lookback_days", "ma_period", "prior_down_days"):
        value = trend[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"{where}.trend.{key} 必须是正整数，收到 {value!r}")
    _non_negative_number(trend["min_drop_pct"], f"{where}.trend.min_drop_pct")
    _non_negative_number(trend["ma_discount_pct"], f"{where}.trend.ma_discount_pct")

    volume = section["volume"]
    _require_keys(
        volume,
        {"avg_days", "min_volume_ratio", "min_turnover_ratio", "use_turnover_ratio"},
        f"{where}.volume",
    )
    if not isinstance(volume["avg_days"], int) or volume["avg_days"] < 1:
        raise ConfigError(f"{where}.volume.avg_days 必须是正整数")
    _positive_number(volume["min_volume_ratio"], f"{where}.volume.min_volume_ratio")
    _positive_number(volume["min_turnover_ratio"], f"{where}.volume.min_turnover_ratio")
    if not isinstance(volume["use_turnover_ratio"], bool):
        raise ConfigError(f"{where}.volume.use_turnover_ratio 必须是 true/false")

    fund = section["fund"]
    _require_keys(
        fund, {"require_positive", "min_net_inflow_ratio_pct"}, f"{where}.fund"
    )
    if not isinstance(fund["require_positive"], bool):
        raise ConfigError(f"{where}.fund.require_positive 必须是 true/false")
    _non_negative_number(
        fund["min_net_inflow_ratio_pct"], f"{where}.fund.min_net_inflow_ratio_pct"
    )

    if is_sector:
        _require_keys(fund, {"rank_top_n"}, f"{where}.fund")
        if not isinstance(fund["rank_top_n"], int) or fund["rank_top_n"] < 1:
            raise ConfigError(f"{where}.fund.rank_top_n 必须是正整数")
    else:
        _require_keys(
            section,
            {"min_price", "min_amount", "exclude_name_patterns", "prefilter"},
            where,
        )
        _non_negative_number(section["min_price"], f"{where}.min_price")
        _non_negative_number(section["min_amount"], f"{where}.min_amount")
        prefilter = section["prefilter"]
        _require_keys(
            prefilter,
            {"enabled", "min_spot_volume_ratio", "min_spot_turnover_pct"},
            f"{where}.prefilter",
        )
        if not isinstance(prefilter["enabled"], bool):
            raise ConfigError(f"{where}.prefilter.enabled 必须是 true/false")
        _positive_number(
            prefilter["min_spot_volume_ratio"],
            f"{where}.prefilter.min_spot_volume_ratio",
        )
        _positive_number(
            prefilter["min_spot_turnover_pct"],
            f"{where}.prefilter.min_spot_turnover_pct",
        )
        for pattern in section["exclude_name_patterns"]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(
                    f"{where}.exclude_name_patterns 中的正则非法: {pattern!r}"
                ) from exc


def load_config(path: str | Path = "config.yaml") -> Config:
    config_path = Path(path)
    if not config_path.exists():
        raise ConfigError(f"配置文件不存在: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("配置文件顶层必须是对象")

    warnings: list[str] = []
    for section in ("app", "site", "stock", "sector", "scoring"):
        _require_keys(raw, {section}, "顶层")
        if not isinstance(raw[section], dict):
            raise ConfigError(f"{section} 必须是对象")

    app = raw["app"]
    app_keys = {
        "timezone",
        "database",
        "daily_archive_dir",
        "docs_dir",
        "template",
        "min_run_time",
        "verify_trade_date",
        "request_retries",
        "request_retry_backoff_seconds",
        "request_interval_seconds",
        "history_calendar_days",
        "max_stock_candidates",
        "max_sector_candidates",
        "web_history_days",
        "data_retention_days",
    }
    _require_keys(app, app_keys, "app")
    warnings.extend(_warn_unknown_keys(app, app_keys, "app"))
    for key in (
        "request_retries",
        "history_calendar_days",
        "max_stock_candidates",
        "max_sector_candidates",
        "web_history_days",
        "data_retention_days",
    ):
        value = app[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"app.{key} 必须是正整数，收到 {value!r}")
    _non_negative_number(
        app["request_retry_backoff_seconds"], "app.request_retry_backoff_seconds"
    )
    _non_negative_number(
        app["request_interval_seconds"], "app.request_interval_seconds"
    )
    if not isinstance(app["verify_trade_date"], bool):
        raise ConfigError("app.verify_trade_date 必须是 true/false")

    site = raw["site"]
    site_keys = {"title", "subtitle", "disclaimer"}
    _require_keys(site, site_keys, "site")
    warnings.extend(_warn_unknown_keys(site, site_keys, "site"))

    stock_keys = {
        "min_gain_pct",
        "min_price",
        "min_amount",
        "exclude_name_patterns",
        "prefilter",
        "trend",
        "volume",
        "fund",
    }
    sector_keys = {
        "min_gain_pct",
        "include_industry",
        "include_concept",
        "exclude_name_patterns",
        "fund",
        "trend",
        "volume",
    }
    warnings.extend(_warn_unknown_keys(raw["stock"], stock_keys, "stock"))
    warnings.extend(_warn_unknown_keys(raw["sector"], sector_keys, "sector"))
    _validate_rule_section(raw["stock"], "stock", is_sector=False)
    _validate_rule_section(raw["sector"], "sector", is_sector=True)
    for key in ("include_industry", "include_concept"):
        if not isinstance(raw["sector"][key], bool):
            raise ConfigError(f"sector.{key} 必须是 true/false")

    for pattern in raw["sector"]["exclude_name_patterns"]:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise ConfigError(
                f"sector.exclude_name_patterns 中的正则非法: {pattern!r}"
            ) from exc
    _require_keys(raw["scoring"], {"stock", "sector"}, "scoring")
    warnings.extend(_warn_unknown_keys(raw["scoring"], {"stock", "sector"}, "scoring"))
    _validate_scoring(raw["scoring"]["stock"], "scoring.stock")
    _validate_scoring(raw["scoring"]["sector"], "scoring.sector")

    # 非致命提示在 CLI 层统一打印，这里先挂到对象上。
    config = Config(raw, config_path)
    config.warnings = warnings
    return config


def threshold_chips(config: Config) -> list[str]:
    """生成页面顶部可读的阈值摘要。"""
    stock = config["stock"]
    sector = config["sector"]
    return [
        f"个股涨幅 ≥ {stock['min_gain_pct']:g}%",
        f"板块涨幅 ≥ {sector['min_gain_pct']:g}%",
        f"量比 ≥ {stock['volume']['min_volume_ratio']:g}×",
        f"主力净流入占比 ≥ {stock['fund']['min_net_inflow_ratio_pct']:g}%",
        f"20日跌幅 ≥ {stock['trend']['min_drop_pct']:g}%",
        f"连跌 ≥ {stock['trend']['prior_down_days']}日",
    ]
