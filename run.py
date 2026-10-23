"""命令行入口。

用法：
    python run.py scan     # 收盘后抓取、判定、落库、生成网页
    python run.py build    # 仅用本地数据库重新生成网页
    python run.py check    # 校验配置并打印关键阈值
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from anomaly.config import ConfigError, load_config, threshold_chips
from anomaly.data_source import DataSourceError
from anomaly.pipeline import AnomalyPipeline, RunSkipped
from anomaly.storage import Storage
from anomaly.utils import setup_logging, shanghai_now
from anomaly.web import build_site


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python run.py",
        description="A股异动监测：收盘后抓取全市场数据，判断下跌趋势中的放量资金异动。",
    )
    parser.add_argument("--version", action="version", version="a-share-anomaly 1.0.0")
    sub = parser.add_subparsers(dest="command")

    scan = sub.add_parser("scan", help="抓取当日数据、判定、落库并生成网页")
    scan.add_argument(
        "--config", default=str(ROOT / "config.yaml"), help="配置文件路径"
    )
    scan.add_argument(
        "--force", action="store_true", help="跳过 15:05 的时间检查（仍校验交易日）"
    )
    scan.add_argument(
        "--skip-date-verification",
        action="store_true",
        help="跳过日线落盘校验（不推荐；仅在确认数据已更新时使用）",
    )
    scan.add_argument(
        "--curl-transport",
        action="store_true",
        help="用系统 curl 代理 Python 请求，适配拦截 Python TLS 的公司网络",
    )
    scan.add_argument("--verbose", action="store_true", help="打印调试日志")

    build = sub.add_parser("build", help="仅从本地 SQLite 重新生成网页")
    build.add_argument(
        "--config", default=str(ROOT / "config.yaml"), help="配置文件路径"
    )
    build.add_argument("--verbose", action="store_true")

    check = sub.add_parser("check", help="校验配置与文件路径")
    check.add_argument(
        "--config", default=str(ROOT / "config.yaml"), help="配置文件路径"
    )
    return parser


def _load(path: str):
    config = load_config(path)
    for warning in config.warnings:
        print(f"[配置提示] {warning}")
    return config


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    try:
        config = _load(args.config)
    except ConfigError as exc:
        print(f"配置错误: {exc}", file=sys.stderr)
        return 2

    if args.command == "check":
        from importlib.metadata import PackageNotFoundError, version

        print(f"配置文件: {config.path.resolve()}")
        print(f"数据库:   {config.path_for('database')}")
        print(f"网页模板: {config.path_for('template')}")
        print(f"输出目录: {config.path_for('docs_dir')}")
        print("阈值: " + " | ".join(threshold_chips(config)))
        try:
            print(f"AkShare:  {version('akshare')}")
        except PackageNotFoundError:
            print("AkShare:  未安装（请执行 pip install -r requirements.txt）")
            return 1
        if not config.path_for("template").exists():
            print("网页模板不存在，请检查 app.template。", file=sys.stderr)
            return 1
        print("配置校验通过。")
        return 0

    logger = setup_logging(verbose=getattr(args, "verbose", False))
    if args.command == "build":
        storage = Storage(config.path_for("database"))
        try:
            result = build_site(
                config, storage, logger, now=shanghai_now(config.timezone)
            )
        finally:
            storage.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if args.command == "scan":
        pipeline = AnomalyPipeline(
            config,
            logger,
            use_curl_transport=bool(getattr(args, "curl_transport", False)),
        )
        try:
            result = pipeline.run(
                force=bool(getattr(args, "force", False)),
                skip_date_verification=bool(
                    getattr(args, "skip_date_verification", False)
                ),
            )
        except RunSkipped as exc:
            logger.info("跳过：%s", exc)
            return 0
        except (DataSourceError, ConfigError) as exc:
            logger.error("运行失败：%s", exc)
            return 2
        except KeyboardInterrupt:
            logger.warning("用户中断")
            return 130
        except Exception:
            logger.exception("运行异常")
            return 1
        finally:
            pipeline.close()
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
