"""SQLite 留存与区间统计集成测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from anomaly.stats import build_web_payload
from anomaly.storage import Storage
from tests.helpers import test_config

ROOT = Path(__file__).resolve().parents[1]


def stock_item(
    code: str, name: str, score: float, pct: float = 4.2, industry: str = "银行"
) -> dict:
    return {
        "code": code,
        "name": name,
        "industry": industry,
        "pct": pct,
        "price": 11.0,
        "amount": 500_000_000.0,
        "volume_ratio": 3.1,
        "turnover_ratio": 2.5,
        "amount_ratio": 3.0,
        "main_net_inflow": 60_000_000.0,
        "fund_ratio": 12.0,
        "ret_20d": -12.5,
        "ma20_gap": -7.0,
        "down_streak": 4,
        "drop_depth": 12.5,
        "score": score,
        "score_parts": {"factors": {"volume": 62, "gain": 42, "fund": 60, "drop": 62}},
        "reasons": ["20日跌幅 12.50%", "量比 3.10×"],
        "trigger": "20日跌幅 12.50%；量比 3.10×",
    }


def sector_item(
    code: str = "BK0001", name: str = "测试概念", score: float = 66.0
) -> dict:
    return {
        "code": code,
        "name": name,
        "kind": "concept",
        "category": "概念板块",
        "pct": 3.5,
        "price": 1000.0,
        "volume_ratio": 2.8,
        "turnover_ratio": 2.2,
        "main_net_inflow": 300_000_000.0,
        "fund_ratio": 4.5,
        "fund_rank": 3,
        "ret_20d": -10.0,
        "ma20_gap": -6.0,
        "down_streak": 3,
        "drop_depth": 10.0,
        "leading_stock": "测试股份",
        "up_count": 20,
        "down_count": 2,
        "score": score,
        "score_parts": {"factors": {"volume": 56, "gain": 43, "fund": 22, "drop": 50}},
        "reasons": ["20日跌幅 10.00%", "量比 2.80×"],
        "trigger": "20日跌幅 10.00%；量比 2.80×",
    }


class StatsIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.tempdir.name) / "test.db")
        self.config = test_config()
        self.trade_dates = [
            date(2026, 10, 16),
            date(2026, 10, 19),
            date(2026, 10, 20),
            date(2026, 10, 21),
            date(2026, 10, 22),
            date(2026, 10, 23),
        ]
        self.storage.save_calendar(self.trade_dates)

    def tearDown(self):
        self.storage.close()
        self.tempdir.cleanup()

    def _save(
        self,
        day: date,
        stocks,
        *,
        sectors=None,
        closes=None,
        sector_closes=None,
    ):
        self.storage.save_day(
            trade_date=day,
            finished_at=datetime(day.year, day.month, day.day, 21, 0),
            stocks=stocks,
            sectors=sectors or [],
            meta={"test": True},
        )
        if closes:
            self.storage.upsert_stock_bars(
                [{**row, "trade_date": day.isoformat()} for row in closes]
            )
        if sector_closes:
            self.storage.upsert_sector_bars(
                [{**row, "trade_date": day.isoformat()} for row in sector_closes]
            )

    def test_summary_counts_and_range_return(self):
        self._save(
            date(2026, 10, 16),
            [],
            closes=[
                {
                    "code": "000001",
                    "name": "测试股份",
                    "close": 10.0,
                    "pct": 0.0,
                    "amount": 1.0,
                }
            ],
            sector_closes=[
                {
                    "code": "BK0001",
                    "name": "测试概念",
                    "kind": "concept",
                    "close": 900.0,
                    "pct": 0.0,
                }
            ],
        )
        self._save(
            date(2026, 10, 21),
            [stock_item("000001", "测试股份", 50.0)],
            closes=[
                {
                    "code": "000001",
                    "name": "测试股份",
                    "close": 11.0,
                    "pct": 4.0,
                    "amount": 1.0,
                }
            ],
        )
        self._save(
            date(2026, 10, 23),
            [
                stock_item("000001", "测试股份", 70.0),
                stock_item("000002", "另一只", 40.0, pct=3.1),
            ],
            sectors=[sector_item()],
            closes=[
                {
                    "code": "000001",
                    "name": "测试股份",
                    "close": 12.0,
                    "pct": 4.2,
                    "amount": 1.0,
                },
                {
                    "code": "000002",
                    "name": "另一只",
                    "close": 8.0,
                    "pct": 3.1,
                    "amount": 1.0,
                },
            ],
            sector_closes=[
                {
                    "code": "BK0001",
                    "name": "测试概念",
                    "kind": "concept",
                    "close": 1000.0,
                    "pct": 3.5,
                }
            ],
        )
        payload = build_web_payload(
            self.storage,
            self.config,
            now=datetime(2026, 10, 23, 21, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertTrue(payload["meta"]["has_data"])
        self.assertEqual(payload["meta"]["latest_trade_date"], "2026-10-23")
        self.assertEqual(payload["today"]["trade_date"], "2026-10-23")
        self.assertEqual(len(payload["today"]["stocks"]), 2)
        self.assertEqual(len(payload["today"]["sectors"]), 1)

        window = payload["summary"]["5"]
        self.assertEqual(window["window_days"], 5)
        self.assertEqual(window["sessions_available"], 2)
        self.assertAlmostEqual(window["coverage_pct"], 40.0, places=1)
        stock = next(row for row in window["stocks"] if row["code"] == "000001")
        self.assertEqual(stock["appearances"], 2)
        self.assertAlmostEqual(stock["cumulative_score"], 120.0, places=1)
        self.assertEqual(stock["last_date"], "2026-10-23")
        self.assertTrue(stock["range_available"])
        self.assertAlmostEqual(stock["range_return_pct"], 20.0, places=2)
        self.assertEqual(stock["range_start_date"], "2026-10-16")

        sector = next(row for row in window["sectors"] if row["code"] == "BK0001")
        self.assertEqual(sector["appearances"], 1)
        self.assertAlmostEqual(
            sector["range_return_pct"], (1000.0 / 900.0 - 1) * 100, places=2
        )

        self.assertEqual(len(payload["history"]), 3)
        dates = [entry["trade_date"] for entry in payload["history"]]
        self.assertEqual(dates, ["2026-10-23", "2026-10-21", "2026-10-16"])

    def test_same_day_rerun_does_not_double_count(self):
        self._save(date(2026, 10, 23), [stock_item("000001", "测试股份", 50.0)])
        self._save(date(2026, 10, 23), [stock_item("000001", "测试股份", 80.0)])
        payload = build_web_payload(
            self.storage,
            self.config,
            now=datetime(2026, 10, 23, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        window = payload["summary"]["5"]
        row = next(item for item in window["stocks"] if item["code"] == "000001")
        self.assertEqual(row["appearances"], 1)
        self.assertAlmostEqual(row["cumulative_score"], 80.0, places=1)

    def test_empty_database_payload(self):
        payload = build_web_payload(
            self.storage,
            self.config,
            now=datetime(2026, 10, 23, 22, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )
        self.assertFalse(payload["meta"]["has_data"])
        self.assertEqual(payload["today"]["stocks"], [])
        self.assertEqual(payload["summary"]["20"]["stocks"], [])


if __name__ == "__main__":
    unittest.main()
