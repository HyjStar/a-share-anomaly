"""AkShare 数据源适配层。

东财接口返回的中文列名和字段在不同版本中会轻微变化，本模块集中做一次
“列名发现 + 数值清洗”，下游判定逻辑只依赖稳定的英文列。
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd

from .config import Config
from .utils import retry_call, safe_float

SPOT_COLUMNS = [
    "code",
    "name",
    "price",
    "pct",
    "volume",
    "amount",
    "volume_ratio",
    "turnover",
]
FUND_COLUMNS = ["code", "fund_net_inflow", "fund_ratio", "fund_pct"]
HISTORY_COLUMNS = [
    "date",
    "open",
    "close",
    "high",
    "low",
    "volume",
    "amount",
    "turnover",
    "pct",
]
SECTOR_QUOTE_COLUMNS = [
    "code",
    "name",
    "price",
    "pct",
    "turnover",
    "up_count",
    "down_count",
    "leading_stock",
    "leading_stock_pct",
]
SECTOR_FUND_COLUMNS = ["name", "pct", "fund_net_inflow", "fund_ratio", "leading_stock"]


class DataSourceError(RuntimeError):
    """数据抓取失败，不能生成当日结果。"""


def _find_column(
    df: pd.DataFrame, exact: list[str], contains: list[str] | None = None
) -> str:
    for name in exact:
        if name in df.columns:
            return name
    for fragment in contains or []:
        for column in df.columns:
            if fragment in str(column):
                return str(column)
    raise DataSourceError(f"数据列缺失，期望 {exact}，实际列: {list(df.columns)}")


def _codes(series: pd.Series) -> pd.Series:
    return (
        series.astype(str).str.replace(r"\.0$", "", regex=True).str.strip().str.zfill(6)
    )


def _numbers(series: pd.Series) -> pd.Series:
    return series.map(safe_float).astype(float)


def _text(series: pd.Series) -> pd.Series:
    return series.astype(str).str.strip().replace({"nan": "", "None": "", "NaN": ""})


class AkShareDataSource:
    """对 AkShare 的薄封装：重试、节流、列名标准化。"""

    def __init__(
        self, config: Config, logger: logging.Logger, use_curl_transport: bool = False
    ):
        self.config = config
        self.logger = logger
        self.interval = float(config["app"]["request_interval_seconds"])
        if use_curl_transport:
            from tools.curl_requests_patch import patch_requests_with_curl

            patch_requests_with_curl()
            logger.warning("已启用 curl 传输适配器（仅用于 Python TLS 被拦截的网络）")
        try:
            import akshare as ak
        except ImportError as exc:
            raise DataSourceError(
                "未安装 akshare，请先执行 pip install -r requirements.txt"
            ) from exc
        self.ak = ak
        self.retries = int(config["app"]["request_retries"])
        self.backoff = float(config["app"]["request_retry_backoff_seconds"])

    def _call(self, label: str, fn: Callable[[], Any]) -> Any:
        return retry_call(
            label,
            fn,
            retries=self.retries,
            backoff=self.backoff,
            logger=self.logger,
        )

    def _throttle(self) -> None:
        # 加一点随机抖动，降低固定节奏触发风控的概率。
        time.sleep(self.interval * random.uniform(0.8, 1.25))

    # ------------------------------------------------------------------ 交易日历

    def fetch_trade_calendar(self) -> list[date]:
        df = self._call("交易日历", self.ak.tool_trade_date_hist_sina)
        if df is None or df.empty:
            raise DataSourceError("交易日历为空")
        column = _find_column(df, ["trade_date", "日期"])
        dates: list[date] = []
        for value in df[column]:
            parsed = pd.to_datetime(value, errors="coerce")
            if pd.notna(parsed):
                dates.append(parsed.date())
        return sorted(set(dates))

    # ------------------------------------------------------------------ 当日快照

    def fetch_stock_spot(self) -> pd.DataFrame:
        raw = self._call("全市场个股行情", self.ak.stock_zh_a_spot_em)
        if raw is None or raw.empty:
            raise DataSourceError("全市场个股行情为空（可能非交易日或接口暂时不可用）")
        self.logger.info("全市场个股行情：%s 行", len(raw))
        columns = {
            "code": _find_column(raw, ["代码"]),
            "name": _find_column(raw, ["名称"]),
            "price": _find_column(raw, ["最新价"]),
            "pct": _find_column(raw, ["涨跌幅"]),
            "volume": _find_column(raw, ["成交量"]),
            "amount": _find_column(raw, ["成交额"]),
            "volume_ratio": _find_column(raw, ["量比"]),
            "turnover": _find_column(raw, ["换手率"]),
        }
        out = pd.DataFrame(
            {
                "code": _codes(raw[columns["code"]]),
                "name": _text(raw[columns["name"]]),
                "price": _numbers(raw[columns["price"]]),
                "pct": _numbers(raw[columns["pct"]]),
                "volume": _numbers(raw[columns["volume"]]),
                "amount": _numbers(raw[columns["amount"]]),
                "volume_ratio": _numbers(raw[columns["volume_ratio"]]),
                "turnover": _numbers(raw[columns["turnover"]]),
            }
        )
        out = out.drop_duplicates("code", keep="last")
        out.loc[out["price"] <= 0, "price"] = float("nan")
        return out.reset_index(drop=True)

    def fetch_stock_fund_flow(self) -> pd.DataFrame:
        raw = self._call(
            "个股主力资金流",
            lambda: self.ak.stock_individual_fund_flow_rank(indicator="今日"),
        )
        if raw is None or raw.empty:
            raise DataSourceError("个股资金流排名为空")
        self.logger.info("个股资金流排名：%s 行", len(raw))
        code_col = _find_column(raw, ["代码"])
        net_col = _find_column(raw, [], ["今日主力净流入-净额", "主力净流入-净额"])
        ratio_col = _find_column(
            raw, [], ["今日主力净流入-净占比", "主力净流入-净占比"]
        )
        pct_col = _find_column(raw, ["今日涨跌幅"], ["涨跌幅"])
        out = pd.DataFrame(
            {
                "code": _codes(raw[code_col]),
                "fund_net_inflow": _numbers(raw[net_col]),
                "fund_ratio": _numbers(raw[ratio_col]),
                "fund_pct": _numbers(raw[pct_col]),
            }
        )
        return out.drop_duplicates("code", keep="last").reset_index(drop=True)

    # ------------------------------------------------------------------ 板块快照

    def fetch_sector_quotes(self, kind: str) -> pd.DataFrame:
        if kind == "industry":
            raw = self._call("行业板块行情", self.ak.stock_board_industry_name_em)
        else:
            raw = self._call("概念板块行情", self.ak.stock_board_concept_name_em)
        if raw is None or raw.empty:
            raise DataSourceError(f"{kind} 板块行情为空")
        self.logger.info(
            "%s板块行情：%s 行", "行业" if kind == "industry" else "概念", len(raw)
        )
        columns = {
            "code": _find_column(raw, ["板块代码"]),
            "name": _find_column(raw, ["板块名称"]),
            "price": _find_column(raw, ["最新价"]),
            "pct": _find_column(raw, ["涨跌幅"]),
            "turnover": _find_column(raw, ["换手率"]),
            "up_count": _find_column(raw, ["上涨家数"]),
            "down_count": _find_column(raw, ["下跌家数"]),
            "leading_stock": _find_column(raw, ["领涨股票"]),
            "leading_stock_pct": _find_column(raw, ["领涨股票-涨跌幅"]),
        }
        out = pd.DataFrame(
            {
                "code": _text(raw[columns["code"]]),
                "name": _text(raw[columns["name"]]),
                "price": _numbers(raw[columns["price"]]),
                "pct": _numbers(raw[columns["pct"]]),
                "turnover": _numbers(raw[columns["turnover"]]),
                "up_count": _numbers(raw[columns["up_count"]]),
                "down_count": _numbers(raw[columns["down_count"]]),
                "leading_stock": _text(raw[columns["leading_stock"]]),
                "leading_stock_pct": _numbers(raw[columns["leading_stock_pct"]]),
            }
        )
        out["kind"] = kind
        return out.drop_duplicates("code", keep="last").reset_index(drop=True)

    def fetch_sector_fund_flow(self, kind: str) -> pd.DataFrame:
        sector_type = "行业资金流" if kind == "industry" else "概念资金流"
        raw = self._call(
            f"{sector_type}排名",
            lambda: self.ak.stock_sector_fund_flow_rank(
                indicator="今日", sector_type=sector_type
            ),
        )
        if raw is None or raw.empty:
            raise DataSourceError(f"{sector_type}为空")
        self.logger.info("%s：%s 行", sector_type, len(raw))
        name_col = _find_column(raw, ["名称", "板块名称"])
        # 不同 AkShare 小版本可能带/不带“今日”前缀。
        pct_col = _find_column(raw, ["今日涨跌幅"], ["涨跌幅"])
        net_col = _find_column(raw, [], ["主力净流入-净额"])
        ratio_col = _find_column(raw, [], ["主力净流入-净占比"])
        lead_col = _find_column(raw, ["今日主力净流入最大股"], ["主力净流入最大股"])
        out = pd.DataFrame(
            {
                "name": _text(raw[name_col]),
                "pct": _numbers(raw[pct_col]),
                "fund_net_inflow": _numbers(raw[net_col]),
                "fund_ratio": _numbers(raw[ratio_col]),
                "leading_stock": _text(raw[lead_col]),
            }
        )
        out["kind"] = kind
        return out.drop_duplicates("name", keep="last").reset_index(drop=True)

    # ------------------------------------------------------------------ 历史 K 线

    def verify_trade_date(self, trade_date: date) -> bool:
        """用两只大市值股票确认今天已有日线。

        AkShare 的实时快照在极端情况下可能返回上一交易日数据；如果直接把它
        记成今天，会污染历史统计。收盘后 15:30 一般已经落盘。
        """
        for code in ("000001", "600519"):
            try:
                history = self.fetch_stock_history(code, trade_date)
            except Exception as exc:  # noqa: BLE001 - 只用一只票做探测，失败换下一只
                self.logger.warning("交易日校验 %s 失败: %s", code, exc)
                continue
            if (
                not history.empty
                and pd.to_datetime(history["date"]).max().date() == trade_date
            ):
                return True
        return False

    def fetch_stock_history(self, code: str, trade_date: date) -> pd.DataFrame:
        days = int(self.config["app"]["history_calendar_days"])
        start = (pd.Timestamp(trade_date) - pd.Timedelta(days=days)).strftime("%Y%m%d")
        end = trade_date.strftime("%Y%m%d")
        self._throttle()
        raw = self._call(
            f"{code} 历史行情",
            lambda: self.ak.stock_zh_a_hist(
                symbol=code,
                period="daily",
                start_date=start,
                end_date=end,
                adjust="qfq",
            ),
        )
        return self._normalize_history(raw, label=f"{code} 历史行情")

    def fetch_sector_history(
        self, kind: str, name: str, trade_date: date
    ) -> pd.DataFrame:
        days = int(self.config["app"]["history_calendar_days"])
        start = (pd.Timestamp(trade_date) - pd.Timedelta(days=days)).strftime("%Y%m%d")
        end = trade_date.strftime("%Y%m%d")
        self._throttle()
        if kind == "industry":
            raw = self._call(
                f"{name} 板块历史",
                lambda: self.ak.stock_board_industry_hist_em(
                    symbol=name,
                    start_date=start,
                    end_date=end,
                    period="daily",
                    adjust="",
                ),
            )
        else:
            raw = self._call(
                f"{name} 板块历史",
                lambda: self.ak.stock_board_concept_hist_em(
                    symbol=name,
                    start_date=start,
                    end_date=end,
                    period="daily",
                    adjust="",
                ),
            )
        return self._normalize_history(raw, label=f"{name} 板块历史")

    def _normalize_history(self, raw: pd.DataFrame | None, label: str) -> pd.DataFrame:
        if raw is None or raw.empty:
            return pd.DataFrame(columns=HISTORY_COLUMNS)
        columns = {
            "date": _find_column(raw, ["日期"]),
            "open": _find_column(raw, ["开盘"]),
            "close": _find_column(raw, ["收盘"]),
            "high": _find_column(raw, ["最高"]),
            "low": _find_column(raw, ["最低"]),
            "volume": _find_column(raw, ["成交量"]),
            "amount": _find_column(raw, ["成交额"]),
            "turnover": _find_column(raw, ["换手率"]),
            "pct": _find_column(raw, ["涨跌幅"]),
        }
        out = pd.DataFrame({key: raw[col] for key, col in columns.items()})
        out["date"] = pd.to_datetime(out["date"], errors="coerce").dt.date
        for key in (
            "open",
            "close",
            "high",
            "low",
            "volume",
            "amount",
            "turnover",
            "pct",
        ):
            out[key] = _numbers(out[key])
        out = (
            out.dropna(subset=["date", "close"])
            .sort_values("date")
            .reset_index(drop=True)
        )
        if out.empty:
            self.logger.warning("%s 无有效历史数据", label)
        return out

    # ------------------------------------------------------------------ 个股行业

    def fetch_stock_industry(self, code: str) -> str:
        self._throttle()
        raw = self._call(
            f"{code} 个股资料",
            lambda: self.ak.stock_individual_info_em(symbol=code, timeout=20),
        )
        if raw is None or raw.empty:
            return ""
        item_col = _find_column(raw, ["item"])
        value_col = _find_column(raw, ["value"])
        for item, value in zip(raw[item_col].astype(str), raw[value_col]):
            if item.strip() in {"行业", "所属行业"}:
                text = str(value).strip()
                return "" if text.lower() in {"nan", "none"} else text
        return ""
