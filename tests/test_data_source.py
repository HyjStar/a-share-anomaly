"""AkShare 返回列名 → 内部稳定列的适配测试（不联网）。"""

from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import pandas as pd

from anomaly.data_source import AkShareDataSource
from anomaly.utils import setup_logging
from tests.helpers import test_config


class DummyAk:
    def stock_zh_a_spot_em(self):
        return pd.DataFrame(
            [
                {
                    "序号": 1,
                    "代码": "000001",
                    "名称": "平安银行",
                    "最新价": 11.2,
                    "涨跌幅": 4.5,
                    "成交量": 1234567.0,
                    "成交额": 1380000000.0,
                    "量比": 2.8,
                    "换手率": 1.7,
                },
                {
                    "序号": 2,
                    "代码": "600519",
                    "名称": "贵州茅台",
                    "最新价": 1500.0,
                    "涨跌幅": -1.2,
                    "成交量": 30000.0,
                    "成交额": 4500000000.0,
                    "量比": 0.9,
                    "换手率": 0.3,
                },
            ]
        )

    def stock_individual_fund_flow_rank(self, indicator="今日"):
        return pd.DataFrame(
            [
                {
                    "序号": 1,
                    "代码": "000001",
                    "名称": "平安银行",
                    "今日涨跌幅": 4.5,
                    "今日主力净流入-净额": 138000000.0,
                    "今日主力净流入-净占比": 10.0,
                }
            ]
        )

    def stock_board_industry_name_em(self):
        return pd.DataFrame(
            [
                {
                    "排名": 1,
                    "板块名称": "银行",
                    "板块代码": "BK0475",
                    "最新价": 3000.0,
                    "涨跌幅": 2.4,
                    "换手率": 1.1,
                    "上涨家数": 30,
                    "下跌家数": 3,
                    "领涨股票": "平安银行",
                    "领涨股票-涨跌幅": 4.5,
                }
            ]
        )

    def stock_board_concept_name_em(self):
        return pd.DataFrame(
            [
                {
                    "排名": 1,
                    "板块名称": "测试概念",
                    "板块代码": "BK9999",
                    "最新价": 1000.0,
                    "涨跌幅": 3.0,
                    "换手率": 2.0,
                    "上涨家数": 10,
                    "下跌家数": 1,
                    "领涨股票": "平安银行",
                    "领涨股票-涨跌幅": 9.0,
                }
            ]
        )

    def stock_sector_fund_flow_rank(self, indicator="今日", sector_type="行业资金流"):
        return pd.DataFrame(
            [
                {
                    "序号": 1,
                    "名称": "银行",
                    "今日涨跌幅": 2.4,
                    "今日主力净流入-净额": 500000000.0,
                    "今日主力净流入-净占比": 4.2,
                    "今日主力净流入最大股": "平安银行",
                }
            ]
        )

    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust):
        return pd.DataFrame(
            [
                {
                    "日期": end_date,
                    "开盘": 10.0,
                    "收盘": 11.2,
                    "最高": 11.3,
                    "最低": 9.9,
                    "成交量": 1234567.0,
                    "成交额": 1380000000.0,
                    "振幅": 5.0,
                    "涨跌幅": 4.5,
                    "涨跌额": 0.48,
                    "换手率": 1.7,
                    "股票代码": symbol,
                }
            ]
        )

    def stock_board_industry_hist_em(
        self, symbol, start_date, end_date, period, adjust
    ):
        return self._board_history(end_date)

    def stock_board_concept_hist_em(self, symbol, start_date, end_date, period, adjust):
        return self._board_history(end_date)

    @staticmethod
    def _board_history(end_date):
        return pd.DataFrame(
            [
                {
                    "日期": end_date,
                    "开盘": 990.0,
                    "收盘": 1000.0,
                    "最高": 1010.0,
                    "最低": 980.0,
                    "成交量": 1000000.0,
                    "成交额": 500000000.0,
                    "振幅": 3.0,
                    "涨跌幅": 3.0,
                    "涨跌额": 30.0,
                    "换手率": 2.0,
                }
            ]
        )

    def stock_individual_info_em(self, symbol, timeout=None):
        return pd.DataFrame(
            [
                {"item": "行业", "value": "银行"},
                {"item": "上市时间", "value": "19910403"},
            ]
        )

    def tool_trade_date_hist_sina(self):
        return pd.DataFrame([{"trade_date": date(2026, 10, 22)}])


class DataSourceNormalizationTest(unittest.TestCase):
    def setUp(self):
        config = test_config()
        config.data["app"]["request_interval_seconds"] = 0
        config.data["app"]["request_retries"] = 1
        self.provider = AkShareDataSource(config, setup_logging(False))
        self.provider.ak = DummyAk()

    def test_spot_and_fund(self):
        spot = self.provider.fetch_stock_spot()
        self.assertEqual(
            spot.columns.tolist()[:8],
            [
                "code",
                "name",
                "price",
                "pct",
                "volume",
                "amount",
                "volume_ratio",
                "turnover",
            ],
        )
        self.assertEqual(spot.iloc[0]["code"], "000001")
        fund = self.provider.fetch_stock_fund_flow()
        self.assertAlmostEqual(fund.iloc[0]["fund_net_inflow"], 138000000.0)
        self.assertAlmostEqual(fund.iloc[0]["fund_ratio"], 10.0)

    def test_sector_quotes_and_funds(self):
        industry = self.provider.fetch_sector_quotes("industry")
        concept = self.provider.fetch_sector_quotes("concept")
        self.assertEqual(industry.iloc[0]["code"], "BK0475")
        self.assertEqual(concept.iloc[0]["name"], "测试概念")
        fund = self.provider.fetch_sector_fund_flow("industry")
        self.assertAlmostEqual(fund.iloc[0]["fund_net_inflow"], 500000000.0)
        self.assertAlmostEqual(fund.iloc[0]["fund_ratio"], 4.2)

    def test_history_and_calendar(self):
        history = self.provider.fetch_stock_history("000001", date(2026, 10, 22))
        self.assertEqual(history.iloc[-1]["date"], date(2026, 10, 22))
        self.assertAlmostEqual(history.iloc[-1]["close"], 11.2)
        self.assertTrue(self.provider.verify_trade_date(date(2026, 10, 22)))
        calendar = self.provider.fetch_trade_calendar()
        self.assertIn(date(2026, 10, 22), calendar)

    def test_board_history_and_industry(self):
        history = self.provider.fetch_sector_history(
            "concept", "测试概念", date(2026, 10, 22)
        )
        self.assertAlmostEqual(history.iloc[-1]["close"], 1000.0)
        self.assertEqual(self.provider.fetch_stock_industry("000001"), "银行")

    def test_retry_eventually_raises(self):
        class Broken(DummyAk):
            def stock_zh_a_spot_em(self):
                raise RuntimeError("network down")

        self.provider.ak = Broken()
        with patch("time.sleep", return_value=None), self.assertRaises(RuntimeError):
            self.provider.fetch_stock_spot()


if __name__ == "__main__":
    unittest.main()
