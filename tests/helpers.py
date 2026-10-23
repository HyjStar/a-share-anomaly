"""测试专用的稳定配置。

生产 `config.yaml` 允许用户按市场体感调阈值；测试不应该因为用户调参而失败，
因此这里显式固定测试所依赖的默认口径。
"""

from __future__ import annotations

from pathlib import Path

from anomaly.config import Config, load_config

ROOT = Path(__file__).resolve().parents[1]


def test_config() -> Config:
    config = load_config(ROOT / "config.yaml")
    stock = config.data["stock"]
    stock.update(
        {
            "min_gain_pct": 3.0,
            "min_price": 2.0,
            "min_amount": 30_000_000,
            "exclude_name_patterns": ["ST", "退", "^N", "^C"],
        }
    )
    stock["prefilter"] = {
        "enabled": True,
        "min_spot_volume_ratio": 1.2,
        "min_spot_turnover_pct": 1.5,
    }
    stock["trend"] = {
        "lookback_days": 20,
        "min_drop_pct": 8.0,
        "ma_period": 20,
        "ma_discount_pct": 5.0,
        "prior_down_days": 3,
    }
    stock["volume"] = {
        "avg_days": 5,
        "min_volume_ratio": 2.0,
        "min_turnover_ratio": 1.8,
        "use_turnover_ratio": True,
    }
    stock["fund"] = {"require_positive": True, "min_net_inflow_ratio_pct": 5.0}

    sector = config.data["sector"]
    sector.update(
        {"min_gain_pct": 2.0, "include_industry": True, "include_concept": True}
    )
    sector["exclude_name_patterns"] = ["昨日", "连板", "涨停", "跌停", "ST板块"]
    sector["trend"] = dict(stock["trend"])
    sector["volume"] = dict(stock["volume"])
    sector["fund"] = {
        "require_positive": True,
        "min_net_inflow_ratio_pct": 2.0,
        "rank_top_n": 20,
    }
    config.data["scoring"] = {
        "stock": {
            "weights": {"volume": 30, "gain": 20, "fund": 30, "drop": 20},
            "full_marks": {
                "volume_ratio": 5.0,
                "gain_pct": 10.0,
                "fund_ratio_pct": 20.0,
                "drop_pct": 20.0,
            },
        },
        "sector": {
            "weights": {"volume": 30, "gain": 20, "fund": 30, "drop": 20},
            "full_marks": {
                "volume_ratio": 5.0,
                "gain_pct": 8.0,
                "fund_ratio_pct": 20.0,
                "drop_pct": 20.0,
            },
        },
    }
    return config
