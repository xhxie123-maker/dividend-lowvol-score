# 数据文件说明

## `daily_scores.csv`

- 48 列，每行 = 一个标的在一个交易日的完整评分快照；主键 `(date, code)`
- `rating` 为五档评级（描述用），`action` 为五档操作建议（A/B/C/D/E，执行用；E = 强卖出）
- 新增列自 **2025-11-17** 起有值：成分股财务（`roe` / `roa` / `ocf_np` / `debt_ratio` / `np_stab` / `payout` / `financial`）、估值与宏观（`bond10y` / `erp` / `spread` / `fwd_yield` / `payout` / `fcf_cover` / `pe`）、资金（`flow20` / `interest_cover`）
- 2025-11-17 **及更早**的行来自旧引擎，17 列为空属预期（旧引擎未接入这些子项，覆盖率 85%）；这些行的 `action` 列也为空，用 `score` 自行映射即可。做时序统计时请按 `coverage` 列筛样本，或直接从 `2025-11-17` 之后取
- `coverage` 为**子项级**加权覆盖率（已实现子项权重 ÷ 100），代理项不计入已实现

## `history/<date>.json`

单日全量快照（含每只标的的指标字典、维度拆解、子项状态表），供程序回读与外部引用；`2026-11-13` / `2026-11-16` 为旧引擎产物。

## `run_history.jsonl`

每次运行追加一行：运行时间、数据日期、标的数、覆盖率、是否离线、是否跑回测。用于排查「哪次运行改了什么」，不参与评分。

## `cache/`

按数据源落盘的原始缓存，可安全删除（删除后下次运行自动重建，仅多一点网络请求）：

| 文件 | 内容 | 来源 |
| --- | --- | --- |
| `nav_<code>.csv` | 单位净值 + 每份分红（`fhfcz`） | `api.fund.eastmoney.com/f10/lsjz` |
| `shares_<code>.csv` | 场内份额快照（按抓取日累积） | `push2.eastmoney.com/api/qt/stock/get` (`f84`) |
| `bond10y.csv` | 中国 10 年期国债收益率 | `RPTA_WEB_TREASURYYIELD` 列 `EMM00166466` |
| `pe_H30269.csv` | 中证红利低波指数 PE | `csindex.com.cn` `index-perf` 字段 `peg` |
| `holdings_<code>.json` | 各季度前十大持仓与权重 | `fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc` |
| `fund_<stock>.json` | 个股财务指标 + 分红明细（3 天刷新） | `RPT_F10_FINANCE_MAINFINADATA`、`RPT_SHAREBONUS_DET` |

注意：份额、个股财务、10Y 国债均为**按抓取日累积**的快照型数据，历史行不可回溯伪造；因此刚部署时 `flow20` 等子项会是 missing，随每日运行逐步补齐。
