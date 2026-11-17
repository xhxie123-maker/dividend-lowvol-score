# -*- coding: utf-8 -*-
"""红利低波 ETF 监测评分：六维 100 分模型 + 免费数据源 + 回测。

用法：
  python dividend_lowvol_score.py              # 抓数、评分、生成产物
  python dividend_lowvol_score.py --backtest   # 追加跑回测
  python dividend_lowvol_score.py --offline    # 只用本地缓存
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta

from dlv import backtest as bt
from dlv import feed, model, report

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_config():
    with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def coverage_lines(etf_states):
    lines = []
    for etf, states in etf_states:
        real = {st: 0 for st in ("live", "proxy", "missing")}
        for st in states.values():
            real[st] += 1
        miss = [sn for key, name, dw, subs in model.DIMENSIONS
                for sk, sn, sw, s in subs if states.get((key, sk)) == "missing"]
        lines.append("{}：34 个子项，已实现 {} / 代理 {} / 待接入 {}（{}）".format(
            etf["short"], real["live"], real["proxy"], real["missing"],
            "、".join(miss) or "无"))
    lines.append("数据源：日线、净值与分红（东财基金 F10）、场内份额（东财行情）、"
                 "10Y 国债收益率（东财国债报表）、指数 PE（中证指数官网）、"
                 "成分股财务与分红（东财 F10 报表），全部为公开免费接口。")
    lines.append("代理口径：成分股财务为前十大持仓加权（季度快照、按公告日 point-in-time）；"
                 "ROIC 以总资产净利率代理；PB 分位无免费日频序列，仍待接入。")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--refresh-days", type=int, default=3)
    args = ap.parse_args()

    cfg = load_config()
    windows = cfg["windows"]
    tiers = cfg.get("action_tiers")
    report.set_globals(cfg["rating_bands"], tiers)
    fetcher = feed.Fetcher(ROOT, offline=args.offline, refresh_days=args.refresh_days)

    now = datetime.now(feed.CST)
    run_at = now.strftime("%Y-%m-%d %H:%M")
    errors = []

    bench = fetcher.bars(cfg["benchmark"]["secid"], 1)
    bond = fetcher.bond10y()
    if not bond:
        errors.append("10 年期国债收益率未取到，股债收益差与股权风险溢价按缺失处理")

    all_series, payload, alerts_map, etf_states = [], [], {}, []
    for etf in cfg["etfs"]:
        try:
            bars = fetcher.bars(etf["secid"], 1)
            raw_bars = fetcher.bars(etf["secid"], 0)
            need_from = (now - timedelta(days=windows["nav_lookback_days"])).strftime("%Y-%m-%d")
            nav = fetcher.nav(etf["code"], need_from)
            shares = fetcher.shares(etf["code"], etf["secid"], bars[-1]["date"])
            hold = feed.latest_holdings(fetcher.holdings(etf["code"]))
            fin = feed.financial_proxy(fetcher, hold, [b["date"] for b in bars])
            pe = fetcher.index_pe(etf["pe_index"]) if etf.get("pe_index") else {}
            rows = model.build_series(etf, bars, raw_bars, nav, shares, bench,
                                      {"bond": bond, "pe": pe, "fin": fin}, windows)
            rows = model.score_series(rows, windows)
            all_series.append((etf, rows))
            alerts_map[etf["code"]] = model.risk_alerts(rows)
            last = rows[-1]
            states = {}
            for key, name, dw, subs in model.DIMENSIONS:
                for sk, sn, sw, st in subs:
                    states[(key, sk)] = "missing" if last["sub"].get(sk) is None else st
            if not pe:
                states[("valuation", "pe_pct")] = "proxy"
            etf_states.append((etf, states))
            print("[OK] {} {} {} 评分 {} 覆盖率 {:.0f}%{}{}".format(
                etf["code"], etf["short"], last["date"],
                model.num_fmt(last["score"], 1), last["coverage"] * 100,
                "" if hold else "（无持仓数据）",
                "" if pe else "（PE 用价格代理）"), flush=True)
            payload.append({
                "code": etf["code"], "name": etf["name"], "short": etf["short"], "index": etf["index"],
                "score": None if last["score"] is None else round(last["score"], 2),
                "rating": model.rating_of(last["score"], cfg["rating_bands"])["label"] if last["score"] else "—",
                "coverage": round(last["coverage"], 3),
                "data_date": last["date"],
                "holdings": {"quarter": hold["quarter"], "total_weight": hold["total_weight"],
                             "count": len(hold["items"])} if hold else None,
                "dimensions": [{"key": k, "name": n, "weight": w,
                                "score": None if last["dim"].get(k) is None else round(last["dim"][k], 2),
                                "coverage": round(last["dim_cov"][k], 3),
                                "subs": [{"key": sk, "name": sn, "weight": sw,
                                          "status": states.get((k, sk), st),
                                          "score": None if last["sub"].get(sk) is None else round(last["sub"][sk], 2)}
                                         for sk, sn, sw, st in subs]}
                               for k, n, w, subs in model.DIMENSIONS],
                "alerts": alerts_map[etf["code"]],
                "metrics": {k: (round(v, 6) if isinstance(v, float) else v)
                            for k, v in last.items()
                            if k not in ("sub", "dim", "dim_cov", "score", "coverage", "missing")
                            and not isinstance(v, (list, dict))},
            })
        except Exception as exc:
            errors.append("{} {} 抓取或计算失败：{}".format(etf["code"], etf["short"], exc))
            print("[ERR] {} {}".format(etf["code"], exc), file=sys.stderr, flush=True)

    for note in fetcher.notes:
        errors.append(note)

    if not all_series:
        print("全部标的失败，终止", file=sys.stderr)
        return 1

    data_date = max(rows[-1]["date"] for _, rows in all_series)
    if data_date != now.strftime("%Y-%m-%d"):
        errors.append("最新交易日 {} 与运行日 {} 不一致（非交易日或数据延迟）".format(
            data_date, now.strftime("%Y-%m-%d")))

    scores = report.write_csv(ROOT, all_series, cfg["rating_bands"], windows["backfill_days"])
    report.write_snapshot(ROOT, payload, data_date, run_at)

    merged_alerts = []
    for etf, rows in all_series:
        for a in alerts_map.get(etf["code"], []):
            merged_alerts.append(dict(a, title="{}：{}".format(etf["short"], a["title"])))
    cov_lines = coverage_lines(etf_states)
    report.write_log(ROOT, all_series, merged_alerts, data_date, run_at, errors, cov_lines, etf_states)
    repo = os.environ.get("GITHUB_REPOSITORY", "xhxie123-maker/dividend-lowvol-score")
    report.write_dashboard(ROOT, all_series, alerts_map, data_date, run_at, repo, scores, cov_lines)

    run_log = os.path.join(ROOT, "data", "run_history.jsonl")
    os.makedirs(os.path.dirname(run_log), exist_ok=True)
    with open(run_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_at": run_at, "data_date": data_date,
                             "etfs": [{"code": e["code"],
                                       "score": None if rows[-1]["score"] is None else round(rows[-1]["score"], 2),
                                       "coverage": round(rows[-1]["coverage"], 3)}
                                      for e, rows in all_series],
                             "errors": errors}, ensure_ascii=False) + "\n")

    if args.backtest:
        try:
            path = bt.write(ROOT, all_series, bench, cfg["rating_bands"], data_date, tiers)[0]
            print("[OK] 回测报告 {}".format(os.path.relpath(path, ROOT)), flush=True)
        except Exception as exc:
            errors.append("回测失败：{}".format(exc))
            print("[ERR] 回测 {}".format(exc), file=sys.stderr, flush=True)

    print("完成：数据日期 {}，标的 {} 只，日志 logs/{}.md".format(data_date, len(all_series), data_date),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
