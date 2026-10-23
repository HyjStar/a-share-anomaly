"""静态网页构建：单文件 HTML + 可复用 data.json。

页面不请求任何外部 CDN，也不依赖 Flask。手机浏览器、file:// 直接打开、
GitHub Pages / Nginx 静态托管都能使用。
"""

from __future__ import annotations

import html
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config
from .stats import build_web_payload
from .storage import Storage
from .utils import atomic_write_text, dump_json


def _json_for_script(data: Any) -> str:
    text = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    # JSON 嵌在 <script> 里时，< / > / & 必须转义，防止数据中的字符串提前闭合标签。
    return (
        text.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def render_html(payload: dict[str, Any], template_path: Path) -> str:
    template = template_path.read_text(encoding="utf-8")
    if "__DATA_JSON__" not in template or "__TITLE__" not in template:
        raise ValueError("网页模板缺少 __DATA_JSON__ 或 __TITLE__ 占位符")
    title = str(payload.get("meta", {}).get("title") or "A股异动监测")
    return template.replace("__TITLE__", html.escape(title)).replace(
        "__DATA_JSON__", _json_for_script(payload)
    )


def build_site(
    config: Config,
    storage: Storage,
    logger: logging.Logger,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    docs_dir = config.path_for("docs_dir")
    template_path = config.path_for("template")
    if not template_path.exists():
        raise FileNotFoundError(f"网页模板不存在: {template_path}")

    payload = build_web_payload(storage, config, now=now)
    rendered = render_html(payload, template_path)
    docs_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_text(docs_dir / "index.html", rendered)
    dump_json(payload, docs_dir / "data.json")
    # GitHub Pages 默认用 Jekyll；空文件即可关闭不必要的处理。
    atomic_write_text(docs_dir / ".nojekyll", "")
    meta = payload.get("meta", {})
    logger.info(
        "网页已生成：%s（个股 %s，板块 %s，历史 %s 天）",
        docs_dir / "index.html",
        meta.get("counts", {}).get("stock", 0),
        meta.get("counts", {}).get("sector", 0),
        meta.get("history_days", 0),
    )
    return {
        "index": str(docs_dir / "index.html"),
        "data": str(docs_dir / "data.json"),
        "stock_count": meta.get("counts", {}).get("stock", 0),
        "sector_count": meta.get("counts", {}).get("sector", 0),
        "latest_trade_date": meta.get("latest_trade_date"),
    }
