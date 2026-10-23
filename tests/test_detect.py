"""判定与评分单元测试。"""

from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path

import pandas as pd

from anomaly.detect import evaluate_sector, evaluate_stock, history_analysis
from tests.helpers import test_config

ROOT = Path(__file__).resolve().parents[1]


def make_history(
    trade_date: date = date(2026, 10, 22),
    *,
    streak_break: bool = False,
    today_volume: float = 3_000_000,
) -> pd.DataFrame:
    """构造一段“20 日跌幅约 -9.6%、前 3 日连跌、当日放量上涨”的日线。

    共 40 根 K 线，最后一根是 trade_date。``streak_break`` 会把倒数第 4 根
    （即昨日之前的那个交易日）改成上涨，从而只剩 2 日连跌。
    """
    dates = pd.date_range(end=pd.Timestamp(trade_date), periods=40, freq="D").date
    closes = [12.0 - index * 0.02 for index in range(19)]
    closes.append(11.50)
    for index in range(20, 35):
        closes.append(11.40 - (index - 20) * (1.00 / 14))
    closes.append(10.55)  # 指数 35：中断此前的连续下跌
    if streak_break:
        closes.extend([10.65, 10.45, 10.20, 10.40])
    else:
        closes.extend([10.35, 10.20, 10.00, 10.40])
    assert len(closes) == 40

    # 开盘价围绕昨收，避免人为制造“连续 39 根阴线”。
    open_prices = [closes[0] * 1.001]
    for previous in closes[:-1]:
        open_prices.append(previous * 1.001)

    volume = [1_000_000.0] * 39 + [today_volume]
    turnover = [1.0] * 39 + [today_volume / 1_000_000.0]
    return pd.DataFrame(
        {
            "date": dates,
            "open": open_prices,
            "close": closes,
            "high": [max(o, c) * 1.01 for o, c in zip(open_prices, closes)],
            "low": [min(o, c) * 0.99 for o, c in zip(open_prices, closes)],
            "volume": volume,
            "amount": [v * 100 * c for v, c in zip(volume, closes)],
            "turnover": turnover,
            "pct": pd.Series(closes).pct_change().fillna(0.0).to_numpy() * 100,
        }
    )


def make_stock_row(**overrides):
    row = {
        "code": "000001",
        "name": "测试股份",
        "pct": 4.0,
        "price": 10.40,
        "amount": 3_000_000 * 100 * 10.40,
        "fund_net_inflow": 120_000_000.0,
        "fund_ratio": 10.0,
    }
    row.update(overrides)
    return row


class HistoryAnalysisTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = test_config()

    def test_metrics_and_pass(self):
        history = make_history()
        result = history_analysis(
            history,
            date(2026, 10, 22),
            self.config["stock"]["trend"],
            self.config["stock"]["volume"],
        )
        self.assertTrue(result["ok"], result["reason"])
        metrics = result["metrics"]
        self.assertLess(metrics["ret_20d"], -8)
        self.assertEqual(metrics["down_streak"], 3)
        self.assertAlmostEqual(metrics["volume_ratio"], 3.0, places=2)
        self.assertGreater(metrics["turnover_ratio"], 1.8)

    def test_break_three_day_streak(self):
        history = make_history(streak_break=True)
        result = history_analysis(
            history,
            date(2026, 10, 22),
            self.config["stock"]["trend"],
            self.config["stock"]["volume"],
        )
        self.assertFalse(result["ok"])
        self.assertIn("未连续", result["reason"])

    def test_volume_failure(self):
        history = make_history(today_volume=1_500_000)
        result = history_analysis(
            history,
            date(2026, 10, 22),
            self.config["stock"]["trend"],
            self.config["stock"]["volume"],
        )
        self.assertFalse(result["ok"])
        self.assertIn("未放量", result["reason"])

    def test_stale_history_rejected(self):
        history = make_history()
        result = history_analysis(
            history,
            date(2026, 10, 23),
            self.config["stock"]["trend"],
            self.config["stock"]["volume"],
        )
        self.assertFalse(result["ok"])
        self.assertIn("当日无K线", result["reason"])


class StockEvaluationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = test_config()

    def test_all_conditions_pass_and_score(self):
        result = evaluate_stock(
            make_stock_row(),
            make_history(),
            self.config,
            date(2026, 10, 22),
            industry="银行",
        )
        self.assertTrue(result["eligible"], result["reason"])
        anomaly = result["anomaly"]
        self.assertEqual(anomaly["industry"], "银行")
        self.assertGreater(anomaly["score"], 0)
        self.assertEqual(anomaly["down_streak"], 3)
        self.assertIn("量比", anomaly["trigger"])

    def test_fund_ratio_failure(self):
        result = evaluate_stock(
            make_stock_row(fund_ratio=4.9),
            make_history(),
            self.config,
            date(2026, 10, 22),
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["stage"], "fund")

    def test_gain_failure(self):
        result = evaluate_stock(
            make_stock_row(pct=2.99), make_history(), self.config, date(2026, 10, 22)
        )
        self.assertFalse(result["eligible"])
        self.assertEqual(result["stage"], "gain")

    def test_higher_volume_scores_higher(self):
        low = evaluate_stock(
            make_stock_row(),
            make_history(today_volume=2_200_000),
            self.config,
            date(2026, 10, 22),
        )
        high = evaluate_stock(
            make_stock_row(),
            make_history(today_volume=4_500_000),
            self.config,
            date(2026, 10, 22),
        )
        self.assertTrue(low["eligible"], low.get("reason"))
        self.assertTrue(high["eligible"], high.get("reason"))
        self.assertGreater(high["anomaly"]["score"], low["anomaly"]["score"])

    def test_sector_pass(self):
        row = {
            "code": "BK0001",
            "name": "测试概念",
            "kind": "concept",
            "pct": 3.2,
            "price": 1000.0,
            "fund_net_inflow": 200_000_000.0,
            "fund_ratio": 3.0,
            "fund_rank": 5,
            "leading_stock": "测试股份",
            "up_count": 20,
            "down_count": 2,
        }
        result = evaluate_sector(row, make_history(), self.config, date(2026, 10, 22))
        self.assertTrue(result["eligible"], result["reason"])
        self.assertEqual(result["anomaly"]["category"], "概念板块")
        self.assertIn("资金净流入排名第 5", result["anomaly"]["trigger"])


if __name__ == "__main__":
    unittest.main()
