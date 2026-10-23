"""收盘后异动扫描主流水线。

为控制免费接口请求量，采用“两级漏斗”：
  全市场快照 + 全市场资金流排名
    -> 涨幅/流动性/资金/量比预筛
    -> 仅对少量候选拉历史 K 线
    -> 趋势 + 连续下跌 + 真实量比 + 资金比例复核
"""

from __future__ import annotations

import math
from collections import Counter
from datetime import date, datetime
from typing import Any

import pandas as pd

from .config import Config, threshold_chips
from .data_source import AkShareDataSource, DataSourceError
from .detect import evaluate_sector, evaluate_stock
from .storage import Storage
from .utils import iso, shanghai_now
from .web import build_site


class RunSkipped(RuntimeError):
    """非交易日、未到收盘时间等正常跳过情形。"""


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


class AnomalyPipeline:
    def __init__(
        self,
        config: Config,
        logger,
        *,
        use_curl_transport: bool = False,
        provider: AkShareDataSource | None = None,
        storage: Storage | None = None,
    ):
        self.config = config
        self.logger = logger
        self.provider = provider or AkShareDataSource(
            config, logger, use_curl_transport
        )
        self._storage = storage
        self._owns_storage = storage is None

    @property
    def storage(self) -> Storage:
        if self._storage is None:
            self._storage = Storage(self.config.path_for("database"))
        return self._storage

    def close(self) -> None:
        if self._owns_storage and self._storage is not None:
            self._storage.close()
            self._storage = None

    # ------------------------------------------------------------------ 入口

    def run(
        self,
        *,
        force: bool = False,
        skip_date_verification: bool = False,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        started = now or shanghai_now(self.config.timezone)
        trade_date = started.date()
        self._check_run_window(started, force=force)

        calendar: list[date] = []
        try:
            calendar = self.provider.fetch_trade_calendar()
        except Exception as exc:  # noqa: BLE001 - 日历失败不应阻止后续用日线校验
            self.logger.warning("交易日历抓取失败，将使用日线校验兜底: %s", exc)

        calendar_set = set(calendar)
        calendar_says_open = bool(calendar) and trade_date in calendar_set
        verify = (
            bool(self.config["app"]["verify_trade_date"]) and not skip_date_verification
        )
        verified = False

        # 日历缺失/滞后时，用当日日线做二次判断；节假日拿不到当日 K 线会正常跳过。
        if calendar and not calendar_says_open:
            if not verify:
                raise RunSkipped(f"{trade_date} 不在交易日历中，跳过（周末/节假日）")
            self.logger.info("交易日历未包含 %s，使用日线复核…", trade_date)
            if not self.provider.verify_trade_date(trade_date):
                raise RunSkipped(
                    f"{trade_date} 不在交易日历中且无当日日线，按非交易日跳过"
                )
            self.logger.warning("交易日历未包含今日，但日线已出现，继续运行")
            verified = True

        if verify and not verified:
            self.logger.info("校验 %s 的收盘日线是否已更新…", trade_date)
            if not self.provider.verify_trade_date(trade_date):
                if calendar_says_open:
                    raise DataSourceError(
                        f"{trade_date} 是交易日，但日线尚未更新。请等待几分钟后重跑，"
                        "避免把上一交易日快照记成今天。"
                    )
                raise DataSourceError(
                    "交易日历不可用，且无法通过今日日线确认行情已落盘。"
                    "为避免误记数据，本次不写入；请稍后重跑。"
                )
        elif not verify and not calendar:
            if trade_date.weekday() >= 5:
                raise RunSkipped("周末，跳过")
            self.logger.warning("未校验交易日且交易日历不可用，将按工作日处理")

        if calendar:
            self.storage.save_calendar(calendar)
        # 即使接口日历短暂缺当天，成功跑完后也把当天纳入本地日历。
        self.storage.save_calendar([trade_date])

        self.logger.info("开始抓取 %s 收盘数据", trade_date)
        spot = self.provider.fetch_stock_spot()
        stock_fund = self.provider.fetch_stock_fund_flow()
        stock_frame, stock_funnel = self._prepare_stocks(spot, stock_fund)
        stocks, stock_funnel, stock_bars = self._scan_stocks(
            stock_frame, stock_funnel, trade_date
        )

        sector_quotes = self._fetch_sector_quotes()
        sectors, sector_funnel, sector_bars = self._scan_sectors(
            sector_quotes, trade_date
        )

        warnings: list[str] = []
        if stock_funnel["truncated"]:
            warnings.append(
                f"个股候选超过上限，仅校验了前 {self.config['app']['max_stock_candidates']} 只"
            )
        if sector_funnel["truncated"]:
            warnings.append(
                f"板块候选超过上限，仅校验了前 {self.config['app']['max_sector_candidates']} 个"
            )
        if stock_funnel["history_errors"]:
            warnings.append(
                f"{stock_funnel['history_errors']} 只候选股历史 K 线抓取失败"
            )
        if sector_funnel["history_errors"]:
            warnings.append(
                f"{sector_funnel['history_errors']} 个候选板块历史 K 线抓取失败"
            )
        for label, funnel in (("个股", stock_funnel), ("板块", sector_funnel)):
            checked = funnel.get("history_checked", 0)
            errors = funnel.get("history_errors", 0)
            if checked and errors == checked:
                raise DataSourceError(
                    f"{label}候选历史 K 线全部抓取失败（{errors}/{checked}），"
                    "本次不写入，避免把数据源故障误判为“没有异动”。"
                )

        stocks.sort(key=lambda item: (item["score"], item["pct"]), reverse=True)
        sectors.sort(key=lambda item: (item["score"], item["pct"]), reverse=True)
        stock_bars.extend(
            self._watchlist_bars(
                kind="stock",
                trade_date=trade_date,
                current_codes={item["code"] for item in stocks},
                spot=spot,
                sector_quotes=sector_quotes,
            )
        )
        sector_bars.extend(
            self._watchlist_bars(
                kind="sector",
                trade_date=trade_date,
                current_codes={item["code"] for item in sectors},
                spot=spot,
                sector_quotes=sector_quotes,
            )
        )

        finished = shanghai_now(self.config.timezone)
        meta = {
            "trade_date": iso(trade_date),
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_seconds": round(
                max(0.0, (finished - started).total_seconds()), 1
            ),
            "data_source": "AkShare / 东方财富",
            "calendar_days": len(calendar),
            "date_verified": bool(verify),
            "thresholds": threshold_chips(self.config),
            "funnel": {"stocks": stock_funnel, "sectors": sector_funnel},
            "warnings": warnings,
            "stock_snapshot_rows": len(spot),
            "sector_snapshot_rows": int(
                sum(len(frame) for frame in sector_quotes.values())
            ),
            "bar_rows": {"stocks": len(stock_bars), "sectors": len(sector_bars)},
        }
        archive = self.config.path_for("daily_archive_dir") / f"{iso(trade_date)}.json"
        self.storage.save_day(
            trade_date=trade_date,
            finished_at=finished,
            stocks=stocks,
            sectors=sectors,
            meta=meta,
            daily_archive=archive,
        )
        self.storage.upsert_stock_bars(stock_bars)
        self.storage.upsert_sector_bars(sector_bars)
        retention = int(self.config["app"]["data_retention_days"])
        self.storage.prune_daily_before_days(trade_date, retention)
        site = build_site(self.config, self.storage, self.logger, now=finished)

        result = {
            "trade_date": iso(trade_date),
            "stocks": len(stocks),
            "sectors": len(sectors),
            "archive": str(archive),
            "site": site,
            "funnel": meta["funnel"],
            "warnings": warnings,
        }
        self.logger.info(
            "扫描完成：个股 %s 只，板块 %s 个；页面 %s",
            len(stocks),
            len(sectors),
            site["index"],
        )
        return result

    def _check_run_window(self, now: datetime, *, force: bool) -> None:
        if now.weekday() >= 5:
            raise RunSkipped("周末，跳过")
        earliest = self.config.min_run_time
        if now.time() < earliest and not force:
            raise RunSkipped(
                f"当前 {now.strftime('%H:%M')}，未到收盘后允许运行时间 {earliest.strftime('%H:%M')}；"
                "如确认数据已收盘，可加 --force"
            )

    # ------------------------------------------------------------------ 个股

    def _prepare_stocks(
        self,
        spot: pd.DataFrame,
        fund: pd.DataFrame,
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        section = self.config["stock"]
        frame = spot.merge(
            fund[["code", "fund_net_inflow", "fund_ratio", "fund_pct"]],
            on="code",
            how="left",
        )
        official_ratio = pd.to_numeric(frame["fund_ratio"], errors="coerce")
        amount = pd.to_numeric(frame["amount"], errors="coerce")
        calculated_ratio = (frame["fund_net_inflow"] / amount * 100.0).where(amount > 0)
        frame["fund_ratio"] = calculated_ratio.where(
            calculated_ratio.notna(), official_ratio
        )
        frame["fund_net_inflow"] = pd.to_numeric(
            frame["fund_net_inflow"], errors="coerce"
        )
        frame["pct"] = pd.to_numeric(frame["pct"], errors="coerce")
        frame["price"] = pd.to_numeric(frame["price"], errors="coerce")
        frame["amount"] = amount
        frame["volume_ratio"] = pd.to_numeric(frame["volume_ratio"], errors="coerce")
        frame["turnover"] = pd.to_numeric(frame["turnover"], errors="coerce")

        name_excluded = pd.Series(False, index=frame.index)
        for pattern in section["exclude_name_patterns"]:
            name_excluded |= (
                frame["name"].astype(str).str.contains(pattern, regex=True, na=False)
            )

        basic = (
            (~name_excluded)
            & (frame["price"] >= float(section["min_price"]))
            & (frame["amount"] >= float(section["min_amount"]))
        )
        gain = basic & (frame["pct"] >= float(section["min_gain_pct"]))
        require_positive = bool(section["fund"]["require_positive"])
        fund_ok = (
            frame["fund_ratio"] >= float(section["fund"]["min_net_inflow_ratio_pct"])
        ) & frame["fund_net_inflow"].notna()
        if require_positive:
            fund_ok &= frame["fund_net_inflow"] > 0
        funded = gain & fund_ok

        prefilter = section["prefilter"]
        if bool(prefilter["enabled"]):
            volume_ok = (
                frame["volume_ratio"].isna()
                | (frame["volume_ratio"] <= 0)
                | (frame["volume_ratio"] >= float(prefilter["min_spot_volume_ratio"]))
            )
            turnover_ok = frame["turnover"].isna() | (
                frame["turnover"] >= float(prefilter["min_spot_turnover_pct"])
            )
            filtered = funded & (volume_ok | turnover_ok)
        else:
            filtered = funded

        candidates = frame[filtered].copy()
        candidates["_prelim"] = (
            candidates["pct"].fillna(0)
            + candidates["fund_ratio"].fillna(0) * 0.4
            + candidates["volume_ratio"].fillna(0) * 2.0
        )
        candidates = candidates.sort_values("_prelim", ascending=False).reset_index(
            drop=True
        )
        cap = int(self.config["app"]["max_stock_candidates"])
        truncated = max(0, len(candidates) - cap)
        candidates = candidates.head(cap)
        funnel = {
            "spot_rows": len(spot),
            "after_basic": int(basic.sum()),
            "after_gain": int(gain.sum()),
            "after_fund": int(funded.sum()),
            "after_prefilter": int(filtered.sum()),
            "history_checked": 0,
            "eligible": 0,
            "truncated": truncated,
            "failure_stages": {},
            "history_errors": 0,
            "error_samples": [],
        }
        self.logger.info(
            "个股漏斗：全市场 %s -> 基础 %s -> 涨幅 %s -> 资金 %s -> 量比预筛 %s",
            funnel["spot_rows"],
            funnel["after_basic"],
            funnel["after_gain"],
            funnel["after_fund"],
            funnel["after_prefilter"],
        )
        return candidates, funnel

    def _scan_stocks(
        self,
        candidates: pd.DataFrame,
        funnel: dict[str, Any],
        trade_date: date,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        failures: Counter[str] = Counter()
        anomalies: list[dict[str, Any]] = []
        bars: list[dict[str, Any]] = []
        for _, row in candidates.iterrows():
            record = row.to_dict()
            code = str(record["code"]).zfill(6)
            name = str(record["name"])
            funnel["history_checked"] += 1
            try:
                history = self.provider.fetch_stock_history(code, trade_date)
            except Exception as exc:  # noqa: BLE001 - 单只候选失败不应中断全市场扫描
                funnel["history_errors"] += 1
                failures["history_error"] += 1
                if len(funnel["error_samples"]) < 20:
                    funnel["error_samples"].append(
                        f"{code} {name} 历史K线: {str(exc)[:140]}"
                    )
                self.logger.warning("%s %s 历史K线失败: %s", code, name, exc)
                continue
            result = evaluate_stock(record, history, self.config, trade_date)
            if result["eligible"]:
                anomalies.append(result["anomaly"])
                bars.extend(self._history_bars(history, code=code, name=name))
            else:
                stage = result.get("stage", "unknown")
                failures[stage] += 1
                self.logger.debug("%s %s 未通过: %s", code, name, result.get("reason"))
        funnel["failure_stages"] = dict(failures)
        funnel["eligible"] = len(anomalies)
        self._attach_industries(anomalies)
        self.logger.info(
            "个股复核：检查 %s 只，异动 %s 只；未通过 %s",
            funnel["history_checked"],
            len(anomalies),
            dict(failures),
        )
        return anomalies, funnel, bars

    def _attach_industries(self, anomalies: list[dict[str, Any]]) -> None:
        if not anomalies:
            return
        cached = self.storage.get_industries(item["code"] for item in anomalies)
        for item in anomalies:
            code = item["code"]
            if code in cached:
                item["industry"] = cached[code]
                continue
            try:
                industry = self.provider.fetch_stock_industry(code)
            except Exception as exc:  # noqa: BLE001 - 行业标签不是判定必需
                self.logger.warning("%s 行业查询失败: %s", code, exc)
                industry = ""
            if industry:
                self.storage.save_industry(code, industry)
                cached[code] = industry
                item["industry"] = industry
            else:
                item["industry"] = ""

    # ------------------------------------------------------------------ 板块

    def _fetch_sector_quotes(self) -> dict[str, pd.DataFrame]:
        quotes: dict[str, pd.DataFrame] = {}
        section = self.config["sector"]
        if bool(section["include_industry"]):
            quotes["industry"] = self.provider.fetch_sector_quotes("industry")
        if bool(section["include_concept"]):
            quotes["concept"] = self.provider.fetch_sector_quotes("concept")
        return quotes

    def _scan_sectors(
        self,
        quotes: dict[str, pd.DataFrame],
        trade_date: date,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
        section = self.config["sector"]
        frames: list[pd.DataFrame] = []
        for kind, frame in quotes.items():
            fund = self.provider.fetch_sector_fund_flow(kind)
            fund = fund.copy()
            fund["fund_rank"] = (
                fund["fund_net_inflow"]
                .rank(method="min", ascending=False)
                .fillna(10**9)
                .astype(int)
            )
            merged = frame.merge(
                fund[
                    [
                        "name",
                        "pct",
                        "fund_net_inflow",
                        "fund_ratio",
                        "fund_rank",
                        "leading_stock",
                    ]
                ],
                on="name",
                how="left",
                suffixes=("", "_fund"),
            )
            merged["pct"] = merged["pct"].where(
                merged["pct"].notna(), merged["pct_fund"]
            )
            quote_lead = merged["leading_stock"].fillna("").astype(str).str.strip()
            fund_lead = merged["leading_stock_fund"].fillna("").astype(str).str.strip()
            merged["leading_stock"] = quote_lead.where(quote_lead.ne(""), fund_lead)
            merged["up_count"] = (
                pd.to_numeric(merged["up_count"], errors="coerce").fillna(0).astype(int)
            )
            merged["down_count"] = (
                pd.to_numeric(merged["down_count"], errors="coerce")
                .fillna(0)
                .astype(int)
            )
            merged["kind"] = kind
            frames.append(merged)
        if not frames:
            return (
                [],
                {
                    "quotes": 0,
                    "after_gain": 0,
                    "after_fund": 0,
                    "history_checked": 0,
                    "eligible": 0,
                    "truncated": 0,
                    "failure_stages": {},
                    "history_errors": 0,
                    "error_samples": [],
                },
                [],
            )
        frame = pd.concat(frames, ignore_index=True)
        name_excluded = pd.Series(False, index=frame.index)
        for pattern in section["exclude_name_patterns"]:
            name_excluded |= (
                frame["name"].astype(str).str.contains(pattern, regex=True, na=False)
            )
        base = (~name_excluded) & frame["pct"].notna()
        gain = base & (frame["pct"] >= float(section["min_gain_pct"]))
        net = pd.to_numeric(frame["fund_net_inflow"], errors="coerce")
        ratio = pd.to_numeric(frame["fund_ratio"], errors="coerce")
        rank = pd.to_numeric(frame["fund_rank"], errors="coerce").fillna(10**9)
        require_positive = bool(section["fund"]["require_positive"])
        fund_ok = net.notna() & (
            (rank <= int(section["fund"]["rank_top_n"]))
            | (ratio >= float(section["fund"]["min_net_inflow_ratio_pct"]))
        )
        if require_positive:
            fund_ok &= net > 0
        funded = gain & fund_ok
        candidates = frame[funded].copy()
        candidates["_prelim"] = (
            candidates["pct"].fillna(0) + candidates["fund_ratio"].fillna(0) * 0.4
        )
        candidates = candidates.sort_values("_prelim", ascending=False).reset_index(
            drop=True
        )
        cap = int(self.config["app"]["max_sector_candidates"])
        truncated = max(0, len(candidates) - cap)
        candidates = candidates.head(cap)
        funnel = {
            "quotes": len(frame),
            "after_gain": int(gain.sum()),
            "after_fund": int(funded.sum()),
            "history_checked": 0,
            "eligible": 0,
            "truncated": truncated,
            "failure_stages": {},
            "history_errors": 0,
            "error_samples": [],
        }
        self.logger.info(
            "板块漏斗：全部 %s -> 涨幅 %s -> 资金 %s",
            funnel["quotes"],
            funnel["after_gain"],
            funnel["after_fund"],
        )
        failures: Counter[str] = Counter()
        anomalies: list[dict[str, Any]] = []
        bars: list[dict[str, Any]] = []
        for _, row in candidates.iterrows():
            record = row.to_dict()
            name = str(record["name"])
            kind = str(record["kind"])
            code = str(record.get("code") or "")
            record["fund_rank"] = (
                int(record["fund_rank"]) if _finite(record.get("fund_rank")) else 10**9
            )
            funnel["history_checked"] += 1
            try:
                history = self.provider.fetch_sector_history(kind, name, trade_date)
            except Exception as exc:  # noqa: BLE001
                funnel["history_errors"] += 1
                failures["history_error"] += 1
                if len(funnel["error_samples"]) < 20:
                    funnel["error_samples"].append(f"{name} 板块历史: {str(exc)[:140]}")
                self.logger.warning("%s 板块历史失败: %s", name, exc)
                continue
            result = evaluate_sector(record, history, self.config, trade_date)
            if result["eligible"]:
                anomalies.append(result["anomaly"])
                bars.extend(
                    self._history_bars(
                        history, code=code, name=name, kind=kind, limit=60
                    )
                )
            else:
                stage = result.get("stage", "unknown")
                failures[stage] += 1
                self.logger.debug("%s 板块未通过: %s", name, result.get("reason"))
        funnel["failure_stages"] = dict(failures)
        funnel["eligible"] = len(anomalies)
        self.logger.info(
            "板块复核：检查 %s 个，异动 %s 个；未通过 %s",
            funnel["history_checked"],
            len(anomalies),
            dict(failures),
        )
        return anomalies, funnel, bars

    # ------------------------------------------------------------------ 区间 K 线

    @staticmethod
    def _history_bars(
        history: pd.DataFrame,
        *,
        code: str,
        name: str,
        kind: str | None = None,
        limit: int = 60,
    ) -> list[dict[str, Any]]:
        """截取最近 N 根日线保存，用于未来 20 日窗口的区间起点。

        不保存全市场快照，只保存发生异动的标的；每天再为仍在 20 日观察期内的
        标的补一根最新收盘价，数据库和 Git 仓库都能保持小体积。
        """
        if history is None or history.empty:
            return []
        frame = history.copy()
        frame["date"] = pd.to_datetime(frame["date"], errors="coerce")
        frame = frame.dropna(subset=["date", "close"]).sort_values("date").tail(limit)
        rows: list[dict[str, Any]] = []
        for _, row in frame.iterrows():
            close = row.get("close")
            if not _finite(close) or float(close) <= 0:
                continue
            row_data = {
                "trade_date": row["date"].date().isoformat(),
                "code": str(code),
                "name": name,
                "close": float(close),
                "pct": float(row["pct"]) if _finite(row.get("pct")) else None,
            }
            if kind is None:
                row_data["amount"] = (
                    float(row["amount"]) if _finite(row.get("amount")) else None
                )
            else:
                row_data["kind"] = kind
            rows.append(row_data)
        return rows

    def _watchlist_bars(
        self,
        *,
        kind: str,
        trade_date: date,
        current_codes: set[str],
        spot: pd.DataFrame,
        sector_quotes: dict[str, pd.DataFrame],
    ) -> list[dict[str, Any]]:
        """给 20 日窗口内曾异动、但今天未异动的标的补最新收盘价。

        这些标的历史 K 线已经在首次异动时保存；今天只补一根，就能算到当前的
        区间涨幅。今天正在异动的标的已由刚抓取的历史 K 线覆盖，不重复写。
        """
        sessions = self.storage.get_trading_dates_ending(trade_date, 20)
        start = sessions[0] if sessions else trade_date
        prior_codes = set(self.storage.get_anomaly_codes_since(kind, start, trade_date))
        targets = prior_codes - current_codes
        if not targets:
            return []

        rows: list[dict[str, Any]] = []
        if kind == "stock":
            spot_by_code = {
                str(row["code"]).zfill(6): row for _, row in spot.iterrows()
            }
            for code in targets:
                row = spot_by_code.get(code)
                if row is None:
                    continue
                close = row.get("price")
                if not _finite(close) or float(close) <= 0:
                    continue
                rows.append(
                    {
                        "trade_date": iso(trade_date),
                        "code": code,
                        "name": row.get("name") or "",
                        "close": float(close),
                        "pct": float(row["pct"]) if _finite(row.get("pct")) else None,
                        "amount": float(row["amount"])
                        if _finite(row.get("amount"))
                        else None,
                    }
                )
        else:
            quote_by_code: dict[str, dict[str, Any]] = {}
            for frame in sector_quotes.values():
                for _, row in frame.iterrows():
                    close = row.get("price")
                    if not _finite(close) or float(close) <= 0:
                        continue
                    quote_by_code[str(row.get("code") or "")] = {
                        "name": row.get("name") or "",
                        "kind": row.get("kind") or "",
                        "close": float(close),
                        "pct": float(row["pct"]) if _finite(row.get("pct")) else None,
                    }
            for code in targets:
                row = quote_by_code.get(code)
                if row is None:
                    continue
                rows.append({"trade_date": iso(trade_date), "code": code, **row})
        return rows
