"""端到端流水线测试（使用假数据源，不访问网络）。"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from anomaly.config import Config
from anomaly.data_source import DataSourceError
from anomaly.pipeline import AnomalyPipeline
from anomaly.storage import Storage
from anomaly.utils import setup_logging
from tests.helpers import test_config
from tests.test_detect import make_history

ROOT = Path(__file__).resolve().parents[1]


class FakeProvider:
    def __init__(self):
        self.trade_date = date(2026, 10, 22)

    def fetch_trade_calendar(self):
        return list(pd.bdate_range(end="2026-10-22", periods=30).date)

    def verify_trade_date(self, trade_date):
        return trade_date == self.trade_date

    def fetch_stock_spot(self):
        return pd.DataFrame(
            [
                {
                    "code": "000001",
                    "name": "测试股份",
                    "price": 10.4,
                    "pct": 4.0,
                    "volume": 3_000_000.0,
                    "amount": 500_000_000.0,
                    "volume_ratio": 3.0,
                    "turnover": 3.0,
                },
                {
                    "code": "000002",
                    "name": "不涨股份",
                    "price": 8.0,
                    "pct": 1.0,
                    "volume": 1_000_000.0,
                    "amount": 300_000_000.0,
                    "volume_ratio": 1.0,
                    "turnover": 1.0,
                },
            ]
        )

    def fetch_stock_fund_flow(self):
        return pd.DataFrame(
            [
                {
                    "code": "000001",
                    "fund_net_inflow": 60_000_000.0,
                    "fund_ratio": 12.0,
                    "fund_pct": 4.0,
                },
                {
                    "code": "000002",
                    "fund_net_inflow": 5_000_000.0,
                    "fund_ratio": 1.7,
                    "fund_pct": 1.0,
                },
            ]
        )

    def fetch_stock_history(self, code, trade_date):
        return make_history(trade_date)

    def fetch_stock_industry(self, code):
        return "银行"

    def fetch_sector_quotes(self, kind):
        if kind == "industry":
            return pd.DataFrame(
                columns=[
                    "code",
                    "name",
                    "price",
                    "pct",
                    "turnover",
                    "up_count",
                    "down_count",
                    "leading_stock",
                    "leading_stock_pct",
                    "kind",
                ]
            )
        return pd.DataFrame(
            [
                {
                    "code": "BK0001",
                    "name": "测试概念",
                    "price": 1000.0,
                    "pct": 3.5,
                    "turnover": 2.5,
                    "up_count": 20,
                    "down_count": 2,
                    "leading_stock": "测试股份",
                    "leading_stock_pct": 8.0,
                    "kind": "concept",
                }
            ]
        )

    def fetch_sector_fund_flow(self, kind):
        if kind == "industry":
            return pd.DataFrame(
                columns=[
                    "name",
                    "pct",
                    "fund_net_inflow",
                    "fund_ratio",
                    "leading_stock",
                    "kind",
                ]
            )
        return pd.DataFrame(
            [
                {
                    "name": "测试概念",
                    "pct": 3.5,
                    "fund_net_inflow": 200_000_000.0,
                    "fund_ratio": 3.0,
                    "leading_stock": "测试股份",
                    "kind": "concept",
                }
            ]
        )

    def fetch_sector_history(self, kind, name, trade_date):
        return make_history(trade_date)


class PipelineIntegrationTest(unittest.TestCase):
    @staticmethod
    def _make_config(temp: Path) -> Config:
        base = test_config()
        data = copy.deepcopy(base.data)
        data["app"]["database"] = str(temp / "data" / "anomaly.db")
        data["app"]["daily_archive_dir"] = str(temp / "data" / "daily")
        data["app"]["docs_dir"] = str(temp / "docs")
        data["app"]["template"] = str(ROOT / "web" / "template.html")
        data["app"]["verify_trade_date"] = True
        data["sector"]["include_industry"] = False
        return Config(data, temp / "config.yaml")

    def test_pipeline_persists_and_builds_site(self):
        with tempfile.TemporaryDirectory() as tempdir:
            temp = Path(tempdir)
            config = self._make_config(temp)
            pipeline = AnomalyPipeline(
                config,
                setup_logging(False),
                provider=FakeProvider(),
            )
            try:
                result = pipeline.run(
                    now=datetime(2026, 10, 22, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
                )
            finally:
                pipeline.close()

            self.assertEqual(result["trade_date"], "2026-10-22")
            self.assertEqual(result["stocks"], 1)
            self.assertEqual(result["sectors"], 1)
            self.assertTrue((temp / "docs" / "index.html").exists())
            self.assertTrue((temp / "data" / "daily" / "2026-10-22.json").exists())
            html = (temp / "docs" / "index.html").read_text(encoding="utf-8")
            self.assertIn("测试股份", html)
            self.assertIn("测试概念", html)
            payload = json.loads(
                (temp / "docs" / "data.json").read_text(encoding="utf-8")
            )
            stock_row = next(
                row
                for row in payload["summary"]["5"]["stocks"]
                if row["code"] == "000001"
            )
            self.assertTrue(stock_row["range_available"])
            self.assertIsNotNone(stock_row["range_return_pct"])

            storage = Storage(temp / "data" / "anomaly.db")
            try:
                self.assertEqual(storage.latest_run_date(), date(2026, 10, 22))
                self.assertEqual(
                    len(
                        storage.get_anomalies(
                            "stock", date(2026, 10, 22), date(2026, 10, 22)
                        )
                    ),
                    1,
                )
                self.assertEqual(
                    len(
                        storage.get_anomalies(
                            "sector", date(2026, 10, 22), date(2026, 10, 22)
                        )
                    ),
                    1,
                )
            finally:
                storage.close()

    def test_all_candidate_history_failures_abort_without_saving(self):
        class BrokenHistoryProvider(FakeProvider):
            def fetch_stock_history(self, code, trade_date):
                raise RuntimeError("历史接口故障")

        with tempfile.TemporaryDirectory() as tempdir:
            temp = Path(tempdir)
            config = self._make_config(temp)
            pipeline = AnomalyPipeline(
                config,
                setup_logging(False),
                provider=BrokenHistoryProvider(),
            )
            try:
                with self.assertRaises(DataSourceError):
                    pipeline.run(
                        now=datetime(
                            2026, 10, 22, 21, 0, tzinfo=ZoneInfo("Asia/Shanghai")
                        )
                    )
            finally:
                pipeline.close()

            storage = Storage(temp / "data" / "anomaly.db")
            try:
                self.assertIsNone(storage.latest_run_date())
            finally:
                storage.close()


if __name__ == "__main__":
    unittest.main()
