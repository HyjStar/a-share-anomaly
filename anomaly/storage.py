"""SQLite 持久化层。

同一交易日重跑会覆盖当天异动，并按 (日期, 代码) upsert 区间 K 线，保证区间统计
按“出现日期”去重，不会因为重跑把次数翻倍。所有区间统计都从本地库读取，
构建网页不需要联网。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .utils import iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    trade_date TEXT PRIMARY KEY,
    finished_at TEXT NOT NULL,
    stock_count INTEGER NOT NULL DEFAULT 0,
    sector_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'success',
    meta_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS stock_anomalies (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    industry TEXT,
    pct REAL,
    price REAL,
    amount REAL,
    volume_ratio REAL,
    turnover_ratio REAL,
    amount_ratio REAL,
    main_net_inflow REAL,
    fund_ratio REAL,
    ret_20d REAL,
    ma20_gap REAL,
    down_streak INTEGER,
    drop_depth REAL,
    score REAL,
    trigger_text TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
);

CREATE INDEX IF NOT EXISTS idx_stock_anomalies_date ON stock_anomalies (trade_date);
CREATE INDEX IF NOT EXISTS idx_stock_anomalies_code ON stock_anomalies (code);

CREATE TABLE IF NOT EXISTS sector_anomalies (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    category TEXT,
    pct REAL,
    price REAL,
    volume_ratio REAL,
    turnover_ratio REAL,
    main_net_inflow REAL,
    fund_ratio REAL,
    fund_rank INTEGER,
    ret_20d REAL,
    ma20_gap REAL,
    down_streak INTEGER,
    drop_depth REAL,
    score REAL,
    leading_stock TEXT,
    trigger_text TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (trade_date, code)
);

CREATE INDEX IF NOT EXISTS idx_sector_anomalies_date ON sector_anomalies (trade_date);
CREATE INDEX IF NOT EXISTS idx_sector_anomalies_code ON sector_anomalies (code);

CREATE TABLE IF NOT EXISTS stock_daily (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    close REAL,
    pct REAL,
    amount REAL,
    PRIMARY KEY (trade_date, code)
);

CREATE INDEX IF NOT EXISTS idx_stock_daily_code_date ON stock_daily (code, trade_date);

CREATE TABLE IF NOT EXISTS sector_daily (
    trade_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    kind TEXT,
    close REAL,
    pct REAL,
    PRIMARY KEY (trade_date, code)
);

CREATE INDEX IF NOT EXISTS idx_sector_daily_code_date ON sector_daily (code, trade_date);

CREATE TABLE IF NOT EXISTS trade_calendar (
    trade_date TEXT PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS stock_industry (
    code TEXT PRIMARY KEY,
    industry TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class Storage:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ 日历与行业缓存

    def save_calendar(self, dates: Iterable[date]) -> None:
        rows = [(iso(item),) for item in sorted(set(dates))]
        if not rows:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT OR IGNORE INTO trade_calendar (trade_date) VALUES (?)", rows
            )

    def get_trading_dates_ending(self, end_date: date, count: int) -> list[date]:
        rows = self.conn.execute(
            "SELECT trade_date FROM trade_calendar WHERE trade_date <= ? "
            "ORDER BY trade_date DESC LIMIT ?",
            (iso(end_date), count),
        ).fetchall()
        if rows:
            dates = [date.fromisoformat(row["trade_date"]) for row in rows]
            if len(dates) < count:
                # 日历尚未覆盖足够历史时，用实际成功运行日补齐（通常仅新装首日）。
                extras = self.conn.execute(
                    "SELECT trade_date FROM runs WHERE trade_date <= ? ORDER BY trade_date DESC",
                    (iso(end_date),),
                ).fetchall()
                for row in extras:
                    value = date.fromisoformat(row["trade_date"])
                    if value not in dates:
                        dates.append(value)
                        if len(dates) >= count:
                            break
            return sorted(dates)
        return sorted(self.get_run_dates(limit=count, end_date=end_date))

    def get_previous_trade_date(self, day: date) -> date | None:
        row = self.conn.execute(
            "SELECT trade_date FROM trade_calendar WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1",
            (iso(day),),
        ).fetchone()
        if row:
            return date.fromisoformat(row["trade_date"])
        row = self.conn.execute(
            "SELECT trade_date FROM runs WHERE trade_date < ? ORDER BY trade_date DESC LIMIT 1",
            (iso(day),),
        ).fetchone()
        return date.fromisoformat(row["trade_date"]) if row else None

    def get_industries(self, codes: Iterable[str]) -> dict[str, str]:
        codes = list({str(code).zfill(6) for code in codes})
        if not codes:
            return {}
        placeholders = ",".join("?" for _ in codes)
        rows = self.conn.execute(
            f"SELECT code, industry FROM stock_industry WHERE code IN ({placeholders})",
            codes,
        ).fetchall()
        return {row["code"]: row["industry"] for row in rows}

    def save_industry(self, code: str, industry: str) -> None:
        if not industry:
            return
        with self.conn:
            self.conn.execute(
                "INSERT INTO stock_industry (code, industry, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(code) DO UPDATE SET industry = excluded.industry, updated_at = excluded.updated_at",
                (
                    str(code).zfill(6),
                    industry,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )

    # ------------------------------------------------------------------ 保存当日结果

    def save_day(
        self,
        *,
        trade_date: date,
        finished_at: datetime,
        stocks: list[dict[str, Any]],
        sectors: list[dict[str, Any]],
        meta: dict[str, Any],
        daily_archive: Path | None = None,
    ) -> None:
        """保存当天异动。收盘 K 线由 upsert_*_bars 单独维护。"""
        day = iso(trade_date)
        stock_rows = [self._stock_row(day, item) for item in stocks]
        sector_rows = [self._sector_row(day, item) for item in sectors]

        with self.conn:
            self.conn.execute(
                "DELETE FROM stock_anomalies WHERE trade_date = ?", (day,)
            )
            self.conn.execute(
                "DELETE FROM sector_anomalies WHERE trade_date = ?", (day,)
            )
            self.conn.executemany(
                """
                INSERT INTO stock_anomalies (
                    trade_date, code, name, industry, pct, price, amount, volume_ratio,
                    turnover_ratio, amount_ratio, main_net_inflow, fund_ratio, ret_20d,
                    ma20_gap, down_streak, drop_depth, score, trigger_text, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                stock_rows,
            )
            self.conn.executemany(
                """
                INSERT INTO sector_anomalies (
                    trade_date, code, name, kind, category, pct, price, volume_ratio,
                    turnover_ratio, main_net_inflow, fund_ratio, fund_rank, ret_20d,
                    ma20_gap, down_streak, drop_depth, score, leading_stock, trigger_text, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                sector_rows,
            )
            self.conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(trade_date, finished_at, stock_count, sector_count, status, meta_json) "
                "VALUES (?, ?, ?, ?, 'success', ?)",
                (
                    day,
                    finished_at.isoformat(timespec="seconds"),
                    len(stocks),
                    len(sectors),
                    json.dumps(meta, ensure_ascii=False, allow_nan=False),
                ),
            )

        if daily_archive is not None:
            payload = {
                "trade_date": day,
                "finished_at": finished_at.isoformat(timespec="seconds"),
                "meta": meta,
                "stocks": stocks,
                "sectors": sectors,
            }
            from .utils import atomic_write_text

            atomic_write_text(
                daily_archive,
                json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
                + "\n",
            )

    def upsert_stock_bars(self, rows: list[dict[str, Any]]) -> None:
        """只保存异动股/在观察期内的股票 K 线，而不是每天全市场快照。"""
        values = []
        for row in rows:
            close = row.get("close")
            if close is None:
                continue
            values.append(
                (
                    row["trade_date"],
                    str(row["code"]),
                    row.get("name") or "",
                    float(close),
                    row.get("pct"),
                    row.get("amount"),
                )
            )
        if not values:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO stock_daily (trade_date, code, name, close, pct, amount) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )

    def upsert_sector_bars(self, rows: list[dict[str, Any]]) -> None:
        values = []
        for row in rows:
            close = row.get("close")
            if close is None:
                continue
            values.append(
                (
                    row["trade_date"],
                    str(row["code"]),
                    row.get("name") or "",
                    row.get("kind") or "",
                    float(close),
                    row.get("pct"),
                )
            )
        if not values:
            return
        with self.conn:
            self.conn.executemany(
                "INSERT OR REPLACE INTO sector_daily (trade_date, code, name, kind, close, pct) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                values,
            )

    def get_anomaly_codes_since(self, kind: str, start: date, end: date) -> list[str]:
        table = "stock_anomalies" if kind == "stock" else "sector_anomalies"
        rows = self.conn.execute(
            f"SELECT DISTINCT code FROM {table} WHERE trade_date BETWEEN ? AND ?",
            (iso(start), iso(end)),
        ).fetchall()
        return [row["code"] for row in rows]

    @staticmethod
    def _stock_row(day: str, item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            day,
            item["code"],
            item["name"],
            item.get("industry") or "",
            item.get("pct"),
            item.get("price"),
            item.get("amount"),
            item.get("volume_ratio"),
            item.get("turnover_ratio"),
            item.get("amount_ratio"),
            item.get("main_net_inflow"),
            item.get("fund_ratio"),
            item.get("ret_20d"),
            item.get("ma20_gap"),
            item.get("down_streak"),
            item.get("drop_depth"),
            item.get("score"),
            item.get("trigger"),
            json.dumps(item, ensure_ascii=False, allow_nan=False),
        )

    @staticmethod
    def _sector_row(day: str, item: dict[str, Any]) -> tuple[Any, ...]:
        return (
            day,
            item["code"],
            item["name"],
            item["kind"],
            item.get("category"),
            item.get("pct"),
            item.get("price"),
            item.get("volume_ratio"),
            item.get("turnover_ratio"),
            item.get("main_net_inflow"),
            item.get("fund_ratio"),
            item.get("fund_rank"),
            item.get("ret_20d"),
            item.get("ma20_gap"),
            item.get("down_streak"),
            item.get("drop_depth"),
            item.get("score"),
            item.get("leading_stock") or "",
            item.get("trigger"),
            json.dumps(item, ensure_ascii=False, allow_nan=False),
        )

    # ------------------------------------------------------------------ 查询

    def latest_run_date(self) -> date | None:
        row = self.conn.execute(
            "SELECT trade_date FROM runs WHERE status = 'success' ORDER BY trade_date DESC LIMIT 1"
        ).fetchone()
        return date.fromisoformat(row["trade_date"]) if row else None

    def get_run_dates(
        self, limit: int = 60, end_date: date | None = None
    ) -> list[date]:
        if end_date is None:
            rows = self.conn.execute(
                "SELECT trade_date FROM runs WHERE status = 'success' ORDER BY trade_date DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT trade_date FROM runs WHERE status = 'success' AND trade_date <= ? "
                "ORDER BY trade_date DESC LIMIT ?",
                (iso(end_date), limit),
            ).fetchall()
        return sorted(
            (date.fromisoformat(row["trade_date"]) for row in rows), reverse=True
        )

    def get_run_dates_between(self, start: date, end: date) -> list[date]:
        rows = self.conn.execute(
            "SELECT trade_date FROM runs WHERE status = 'success' AND trade_date BETWEEN ? AND ? "
            "ORDER BY trade_date",
            (iso(start), iso(end)),
        ).fetchall()
        return [date.fromisoformat(row["trade_date"]) for row in rows]

    def get_run(self, day: date) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM runs WHERE trade_date = ?", (iso(day),)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["meta"] = json.loads(result.pop("meta_json") or "{}")
        return result

    def get_anomalies(
        self, kind: str, start: date | None = None, end: date | None = None
    ) -> list[dict[str, Any]]:
        table = "stock_anomalies" if kind == "stock" else "sector_anomalies"
        clauses = []
        params: list[Any] = []
        if start:
            clauses.append("trade_date >= ?")
            params.append(iso(start))
        if end:
            clauses.append("trade_date <= ?")
            params.append(iso(end))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT trade_date, payload_json FROM {table} {where} ORDER BY trade_date, score DESC",
            params,
        ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["trade_date"] = row["trade_date"]
            results.append(payload)
        return results

    def get_closes(
        self,
        kind: str,
        codes: Iterable[str],
        *,
        asof: date,
        baseline: date | None,
    ) -> dict[str, dict[str, Any]]:
        """返回每只标的最新收盘价和区间起始收盘价。

        baseline 是窗口开始前一个交易日。若系统当天没有收盘快照，则返回 None，
        不做“近似”计算，以免展示误导性的区间涨幅。
        """
        codes = list({str(code) for code in codes})
        if not codes:
            return {}
        table = "stock_daily" if kind == "stock" else "sector_daily"
        placeholders = ",".join("?" for _ in codes)
        rows = self.conn.execute(
            f"SELECT code, trade_date, close FROM {table} "
            f"WHERE code IN ({placeholders}) AND trade_date <= ? ORDER BY code, trade_date",
            [*codes, iso(asof)],
        ).fetchall()
        grouped: dict[str, list[tuple[str, float]]] = {}
        for row in rows:
            if row["close"] is None:
                continue
            grouped.setdefault(row["code"], []).append(
                (row["trade_date"], float(row["close"]))
            )

        result: dict[str, dict[str, Any]] = {}
        baseline_text = iso(baseline) if baseline else None
        for code in codes:
            series = grouped.get(code, [])
            latest = series[-1] if series else None
            baseline_row = None
            if baseline_text:
                for item in series:
                    if item[0] <= baseline_text:
                        baseline_row = item
                    else:
                        break
            range_return = None
            if latest and baseline_row and baseline_row[1] > 0:
                range_return = (latest[1] / baseline_row[1] - 1.0) * 100.0
            result[code] = {
                "latest_close": latest[1] if latest else None,
                "latest_date": latest[0] if latest else None,
                "baseline_close": baseline_row[1] if baseline_row else None,
                "baseline_date": baseline_row[0] if baseline_row else None,
                "range_return_pct": range_return,
            }
        return result

    # ------------------------------------------------------------------ 维护

    def prune_daily_before(self, cutoff: date) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM stock_daily WHERE trade_date < ?", (iso(cutoff),)
            )
            self.conn.execute(
                "DELETE FROM sector_daily WHERE trade_date < ?", (iso(cutoff),)
            )

    def prune_daily_before_days(self, latest: date, days: int) -> None:
        self.prune_daily_before(latest - timedelta(days=days))

    def stats(self) -> dict[str, Any]:
        stock_total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM stock_anomalies"
        ).fetchone()["n"]
        sector_total = self.conn.execute(
            "SELECT COUNT(*) AS n FROM sector_anomalies"
        ).fetchone()["n"]
        run_total = self.conn.execute("SELECT COUNT(*) AS n FROM runs").fetchone()["n"]
        return {
            "stock_anomaly_rows": stock_total,
            "sector_anomaly_rows": sector_total,
            "run_days": run_total,
        }
