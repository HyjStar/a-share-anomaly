"""通用工具：日志、时间、重试、JSON 写入与中文数字格式化。"""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

T = TypeVar("T")


def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("anomaly")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def shanghai_now(tz_name: str = "Asia/Shanghai") -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def parse_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        try:
            return value.date()
        except (TypeError, ValueError):
            pass
    text = str(value).strip()[:10]
    if not text or text in {"-", "None", "nan", "NaT"}:
        return None
    for fmt in ("%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def iso(value: date | datetime) -> str:
    return value.strftime("%Y-%m-%d")


def safe_float(value: Any, default: float = float("nan")) -> float:
    """把东方财富可能返回的 '-', '1.23%', '1,234.5' 等转成 float。"""
    if value is None:
        return default
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return default
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if text in {"", "-", "--", "None", "nan", "NaN", "null"}:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def retry_call(
    label: str,
    fn: Callable[[], T],
    *,
    retries: int = 3,
    backoff: float = 2.0,
    logger: logging.Logger | None = None,
) -> T:
    """简单指数退避重试。最后一次失败时抛出原异常。"""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 数据源异常类型不统一
            last_exc = exc
            if logger:
                logger.warning("%s 第 %s/%s 次失败: %s", label, attempt, retries, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
    assert last_exc is not None
    raise last_exc


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def dump_json(data: Any, path: Path, *, indent: int = 2) -> None:
    """写 JSON，允许 NaN 转 null，避免前端 JSON.parse 失败。"""
    text = json.dumps(data, ensure_ascii=False, indent=indent, allow_nan=False)
    atomic_write_text(path, text + "\n")


def format_amount_cn(value: float | None) -> str:
    if value is None or not math.isfinite(float(value)):
        return "—"
    amount = float(value)
    sign = "-" if amount < 0 else ""
    abs_value = abs(amount)
    if abs_value >= 1e8:
        return f"{sign}{abs_value / 1e8:.2f}亿"
    if abs_value >= 1e4:
        return f"{sign}{abs_value / 1e4:.0f}万"
    return f"{sign}{abs_value:.0f}"


def finite_or_none(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
