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
import time
from datetime import datetime, timedelta

from dlv import backtest as bt
from dlv import feed, model, pit, pitbt, pxfix, report
from dlv.action import action_of

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_config():
    with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as fh:
        return json.load(fh)


def coverage_lines(etf_states):
    lines = []
    total = pit.sub_count()
    for etf, states in etf_states:
        real = {st: 0 for st in ("live", "proxy", "missing")}
        for st in states.values():
            real[st] += 1
        miss = [sn for key, name, dw, subs in model.DIMENSIONS
                for sk, sn, sw, s in subs if states.get((key, sk)) == "missing"]
        lines.append("{}：{} 个子项，已实现 {} / 代理 {} / 待接入 {}（{}）".format(
            etf["short"], total, real["live"], real["proxy"], real["missing"],
            "、".join(miss) or "无"))
    lines.append("数据源：日线、净值与分红（东财基金 F10）、场内份额（东财行情）、"
                 "10Y 国债收益率（东财国债报表）、成分股财务与分红（东财 F10 报表）、"
                 "资本开支与经营现金流（新浪财经现金流量表），全部为公开免费接口。")
    lines.append("代理口径：成分股财务按前十大持仓加权，持仓按基金季报法定披露日切换（point-in-time）；"
                 "PE / PB 统一用前十大持仓加权口径反算（PE = 1 / Σ权重·EP，PB = Σ权重·PB），"
                 "不再用 ETF 价格代理；自由现金流覆盖率 = (CFO − CapEx) / 现金分红总额；"
                 "ROIC 仍以总资产净利率（ROA）代理，金融股不披露利息保障倍数。")
    return lines


def _retry(fn, *a, **kw):
    """免费接口偶发 TLS/限流失败时的重试包装（退避 20 秒）。"""
    for i in range(4):
        try:
            return fn(*a, **kw)
        except Exception as exc:
            if i == 3:
                raise
            print("[WARN] {} 第 {} 次失败，20 秒后重试：{}".format(
                getattr(fn, "__name__", fn), i + 1, exc), file=sys.stderr, flush=True)
            time.sleep(20)


def write_minimal_log(rows_list, data_date, run_at, errors, cov_lines=None, tiers=None):
    """只有部分标的成功时写单标的完整日志（八节版需要两只标的对比，这里逐项降级）。"""
    path = os.path.join(ROOT, "logs", "{}.md".format(data_date))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    L = ["# 红利低波 ETF 监测评分 · {}".format(data_date), "",
         "- 生成时间：{}（北京时间）".format(run_at),
         "- 注意：本次仅 {} 只标的产出数据，横向对比（第五节）按缺失处理，其余各节正常给出".format(
             len(rows_list)), ""]
    for etf, rows in rows_list:
        last = rows[-1]
        prev = [x for x in rows if x["score"] is not None][-2:]
        delta = None if len(prev) < 2 else prev[-1]["score"] - prev[-2]["score"]
        action = None if last["score"] is None else action_of(last["score"], tiers or [])
        L.append("## {}（{}）".format(etf["name"], etf["code"]))
        L.append("")
        L.append("| 收盘价 | 综合评分 | 五档操作建议 | 较前一日 | 数据覆盖率 |")
        L.append("| --- | --- | --- | --- | --- |")
        L.append("| {} | {} | {} | {} | {:.0f}% |".format(
            model.num_fmt(last.get("close_raw"), 3), model.num_fmt(last["score"], 1),
            "—" if not action else action["label"],
            "—" if delta is None else "{:+.1f}".format(delta), last["coverage"] * 100))
        L.append("")
        L.append("### 维度得分")
        L.append("")
        L.append("| 维度 | 权重 | 得分 | 维度覆盖率 |")
        L.append("| --- | --- | --- | --- |")
        for k, n, w, subs in model.DIMENSIONS:
            L.append("| {} | {} | {} | {:.0f}% |".format(
                n, w, model.num_fmt(last["dim"].get(k), 1), last["dim_cov"][k] * 100))
        L.append("")
        L.append("### 子项明细")
        L.append("")
        for k, n, w, subs in model.DIMENSIONS:
            parts = []
            for sk, sn, sw, st in subs:
                v = last["sub"].get(sk)
                parts.append("{} {}".format(sn, "—" if v is None else model.num_fmt(v, 1)))
            L.append("- {}：{}".format(n, "；".join(parts)))
        L.append("")
        if action:
            L.append("### 操作建议")
            L.append("")
            L.append("- {}，建议仓位 {}".format(action["label"], action["position"]))
            L.append("- {}".format(action["action"]))
            L.append("")
    if cov_lines:
        L.append("## 指标覆盖与数据源")
        L.append("")
        for line in cov_lines:
            L.append("- {}".format(line))
        L.append("")
    if errors:
        L.append("## 异常与缺失")
        L.append("")
        for e in errors:
            L.append("- {}".format(e))
        L.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L))


def _tencent_secid(secid, qfq=True):
    """腾讯行情日线（secid 形如 1.600019 / 1.000300）。"""
    code = secid.split(".")[-1]
    prefix = "sh" if secid.startswith("1.") else "sz"
    symbol = prefix + code
    params = {"param": "{},day,2019-01-01,2026-12-31,1900,{}".format(
        symbol, "qfq" if qfq else "")}
    raw = feed.http_get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                        params, timeout=25)
    node = ((json.loads(raw).get("data") or {}).get(symbol) or {})
    rows = (node.get("qfqday") if qfq else node.get("day")) or node.get("day") or []
    out = []
    for r in rows:
        try:
            close = float(r[2])
            vol = float(r[5])
        except (IndexError, TypeError, ValueError):
            continue
        out.append({"date": r[0], "close": close, "amount": vol * 100.0 * close})
    if not out:
        raise RuntimeError("腾讯日线为空 {}".format(symbol))
    return out


def _tencent_bars(etf, qfq=True):
    """东财 K 线失败时的备用日线：腾讯行情（成交额用成交量×收盘价估算）。"""
    return _tencent_secid(etf["secid"], qfq)


def bars_by_secid(fetcher, secid, fqt, label, errors):
    """先走东财（带重试），失败再退腾讯；都失败则返回空并记录。"""
    try:
        return _retry(fetcher.bars, secid, fqt)
    except Exception as exc:
        errors.append("{} 东财日线失败，改用腾讯行情（成交额为估算）：{}".format(
            label, str(exc).strip()[:120]))
        print("[WARN] {} 东财日线失败，退腾讯：{}".format(label, exc),
              file=sys.stderr, flush=True)
        try:
            return _tencent_secid(secid, qfq=(fqt == 1))
        except Exception as exc2:
            errors.append("{} 备用源（腾讯）也失败，相关指标按缺失处理：{}".format(
                label, str(exc2).strip()[:120]))
            print("[ERR] {} 备用源也失败：{}".format(label, exc2),
                  file=sys.stderr, flush=True)
            return []


def bars_of(fetcher, etf, fqt, errors):
    """先走东财（带重试），失败再退腾讯，保证两只标的都能出数据。"""
    bars = bars_by_secid(fetcher, etf["secid"], fqt,
                         "{} {}".format(etf["code"], etf["short"]), errors)
    if not bars:
        raise RuntimeError("{} 日线不可用（东财与腾讯均失败）".format(etf["code"]))
    return bars


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--refresh-days", type=int, default=3)
    args = ap.parse_args()

    cfg = load_config()
    windows = cfg["windows"]
    tiers = cfg.get("action_tiers")
    pit.patch_dimensions()
    report.set_globals(cfg["rating_bands"], tiers)
    fetcher = feed.Fetcher(ROOT, offline=args.offline, refresh_days=args.refresh_days)
    pxfix.install(fetcher)

    now = datetime.now(feed.CST)
    run_at = now.strftime("%Y-%m-%d %H:%M")
    errors = []

    bench = bars_by_secid(fetcher, cfg["benchmark"]["secid"], 1,
                          "基准指数 {}".format(cfg["benchmark"].get("name", "")), errors)
    if not bench:
        errors.append("基准指数不可用，相对强弱、贝塔等市场环境子项按缺失处理")
    bond = fetcher.bond10y()
    if not bond:
        errors.append("10 年期国债收益率未取到，股债收益差与股权风险溢价按缺失处理")

    all_series, payload, alerts_map, etf_states, bars_map = [], [], {}, [], {}
    for etf in cfg["etfs"]:
        try:
            bars = bars_of(fetcher, etf, 1, errors)
            raw_bars = bars_of(fetcher, etf, 0, errors)
            need_from = (now - timedelta(days=windows["nav_lookback_days"])).strftime("%Y-%m-%d")
            nav = _retry(fetcher.nav, etf["code"], need_from)
            shares = fetcher.shares(etf["code"], etf["secid"], bars[-1]["date"])
            cached = _retry(pit.holdings_history, fetcher, etf["code"])
            snaps = pit.snapshots(cached)
            hold = snaps[-1] if snaps else None
            dates = [b["date"] for b in bars]
            fin = _retry(pit.build, fetcher, cached, dates)
            pe_map = {d: rec["pe"] for d, rec in fin.items() if rec.get("pe")}
            pb_map = {d: rec["pb"] for d, rec in fin.items() if rec.get("pb")}
            rows = model.build_series(etf, bars, raw_bars, nav, shares, bench,
                                      {"bond": bond, "pe": pe_map, "fin": fin}, windows)
            for r in rows:
                r["pb"] = pb_map.get(r["date"])
            pit.fix_dividends(rows, nav)
            rows = model.score_series(rows, windows)
            pit.finalize(rows, windows)
            all_series.append((etf, rows))
            bars_map[etf["code"]] = bars
            alerts_map[etf["code"]] = model.risk_alerts(rows)
            last = rows[-1]
            states = {}
            for key, name, dw, subs in model.DIMENSIONS:
                for sk, sn, sw, st in subs:
                    states[(key, sk)] = "missing" if last["sub"].get(sk) is None else st
            if not pe_map:
                states[("valuation", "pe_pct")] = "proxy"
            etf_states.append((etf, states))
            print("[OK] {} {} {} 评分 {} 覆盖率 {:.0f}%{}{}".format(
                etf["code"], etf["short"], last["date"],
                model.num_fmt(last["score"], 1), last["coverage"] * 100,
                "" if hold else "（无持仓数据）",
                "" if pe_map else "（PE 退化，估值维度不可用）"), flush=True)
            payload.append({
                "code": etf["code"], "name": etf["name"], "short": etf["short"], "index": etf["index"],
                "score": None if last["score"] is None else round(last["score"], 2),
                "rating": model.rating_of(last["score"], cfg["rating_bands"])["label"] if last["score"] else "—",
                "coverage": round(last["coverage"], 3),
                "data_date": last["date"],
                "holdings": {"quarter": hold["quarter"], "effective": hold["date"],
                             "total_weight": hold["total_weight"],
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
                            if k not in ("sub", "dim", "dim_cov", "score", "coverage", "missing", "snap")
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
    if len(all_series) >= 2:
        try:
            report.write_log(ROOT, all_series, merged_alerts, data_date, run_at, errors, cov_lines, etf_states)
        except Exception as exc:
            errors.append("八节日志渲染失败：{}".format(exc))
            print("[WARN] 日志渲染失败：{}".format(exc), file=sys.stderr, flush=True)
            write_minimal_log(all_series, data_date, run_at, errors, cov_lines, tiers)
    else:
        write_minimal_log(all_series, data_date, run_at, errors, cov_lines, tiers)
    repo = os.environ.get("GITHUB_REPOSITORY", "xhxie123-maker/dividend-lowvol-score")
    try:
        report.write_dashboard(ROOT, all_series, alerts_map, data_date, run_at, repo, scores, cov_lines)
    except Exception as exc:
        errors.append("看板渲染失败：{}".format(exc))
        print("[WARN] 看板渲染失败：{}".format(exc), file=sys.stderr, flush=True)

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
            path2 = pitbt.write(ROOT, all_series, bars_map, bench, cfg["rating_bands"], data_date, tiers)
            print("[OK] 执行口径回测 {}".format(os.path.relpath(path2, ROOT)), flush=True)
        except Exception as exc:
            errors.append("回测失败：{}".format(exc))
            print("[ERR] 回测 {}".format(exc), file=sys.stderr, flush=True)

    print("完成：数据日期 {}，标的 {} 只，日志 logs/{}.md".format(data_date, len(all_series), data_date),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
