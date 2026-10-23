# 数据目录

- `anomaly.db`：SQLite 主库。保存每日异动、异动标的区间 K 线、交易日历、行业缓存。
- `daily/YYYY-MM-DD.json`：每日异动的可读副本，便于审计和备份。

注意：`stock_daily` / `sector_daily` 不是全市场快照，只保存异动标的和 20 日观察期内标的的
必要 K 线，用于计算区间涨幅，避免仓库随每天全市场数据快速膨胀。

运行 `python run.py scan` 后自动生成。GitHub Actions 会把本目录和 `docs/`
一起提交回仓库，从而让区间统计跨天累积。
