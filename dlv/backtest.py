# -*- coding: utf-8 -*-
"""回测：评分分档、四档操作建议、单因子分组、择时策略、组合对照、分段验证。

样本限制（必须披露）：
  A 股 ETF 自 2020 年才有完整日线（563020 于 2023-12 上市），
  因此无法做 2015 年起的十年样本；仅能对 2020 年后的可得区间回测。
  所有信号只用 t 日及以前的数据（逐日滚动计算，天然 point-in-time）。
"""

import csv
import math
import os

from .action import action_of, band_text, range_text, tiers_of
from .model import DIMENSIONS, rating_of, stdev


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------
def f_pct(x, nd=1, sign=False):
    if x is None:
        return "—"
    fmt = "{:+." + str(nd) + "%}" if sign else "{:." + str(nd) + "%}"
    return fmt.format(x)


def f_num(x, nd=2):
    return "—" if x is None else "{:.{}f}".format(x, nd)


def metrics(equity):
    if len(equity) < 2:
        return {}
    rets = [equity[i] / equity[i - 1] - 1.0 for i in range(1, len(equity))]
    total = equity[-1] / equity[0] - 1.0
    years = len(equity) / 250.0
    cagr = (equity[-1] / equity[0]) ** (1.0 / years) - 1.0 if years > 0 else None
    vol = stdev(rets)
    ann_vol = vol * math.sqrt(250) if vol else None
    sharpe = (cagr / ann_vol) if (cagr and ann_vol) else None
    peak, mdd = equity[0], 0.0
    for v in equity:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return {"total": total, "cagr": cagr, "vol": ann_vol, "sharpe": sharpe,
            "mdd": mdd, "n": len(equity)}


def fwd(series_close, i, h):
    if i + h >= len(series_close):
        return None
    return series_close[i + h] / series_close[i] - 1.0


# ---------------------------------------------------------------------------
# 1) 评分分档 → 未来收益
# ---------------------------------------------------------------------------
def rating_band_stats(all_series, bench, bands, tiers, horizons=(20, 60, 120)):
    bench_map = {b["date"]: b["close"] for b in bench}
    bench_dates = sorted(bench_map)
    import bisect
    rows = []
    for etf, rs in all_series:
        closes = [r["close"] for r in rs]
        dates = [r["date"] for r in rs]
        for h in horizons:
            for i, r in enumerate(rs):
                if r["score"] is None:
                    continue
                fr = fwd(closes, i, h)
                k2 = bisect.bisect_right(bench_dates, dates[i]) - 1
                if fr is None or k2 < 0 or k2 + h >= len(bench_dates):
                    continue
                br = bench_map[bench_dates[k2 + h]] / bench_map[bench_dates[k2]] - 1.0
                rows.append({"code": etf["code"], "date": dates[i], "h": h,
                             "score": r["score"],
                             "rating": rating_of(r["score"], bands)["label"],
                             "action": action_of(r["score"], tiers)["label"],
                             "ret": fr, "excess": fr - br})
    out = {}
    for h in horizons:
        per = {}
        for row in rows:
            if row["h"] == h:
                per.setdefault(row["rating"], []).append(row)
        table = []
        for label in [b["label"] for b in bands]:
            items = per.get(label) or []
            if not items:
                continue
            table.append(_summarize(label, items))
        out[h] = table
    return out, rows


def _summarize(label, items):
    ex = [x["excess"] for x in items]
    return {"label": label, "n": len(items),
            "excess": sum(ex) / len(ex),
            "win": sum(1 for v in ex if v > 0) / float(len(ex)),
            "best": max(ex), "worst": min(ex)}


# ---------------------------------------------------------------------------
# 2) 四档操作建议 → 未来收益
# ---------------------------------------------------------------------------
def tier_stats(rows, tiers, horizons=(20, 60, 120)):
    out = {}
    for h in horizons:
        table = []
        for t in tiers:
            items = [x for x in rows if x["h"] == h and x.get("action") == t["label"]]
            if not items:
                continue
            row = _summarize(t["label"], items)
            row["position"] = t["position"]
            table.append(row)
        out[h] = table
    return out


# ---------------------------------------------------------------------------
# 3) 单因子五分位 → 未来超额
# ---------------------------------------------------------------------------
def factor_quintiles(all_series, bench, horizon=60):
    import bisect
    bench_map = {b["date"]: b["close"] for b in bench}
    bench_dates = sorted(bench_map)
    keys = [(k, n, sk, sn) for k, n, w, subs in DIMENSIONS for sk, sn, sw, st in subs]
    out = []
    for key, dname, sk, sname in keys:
        buckets = {i: [] for i in range(5)}
        for etf, rs in all_series:
            closes = [r["close"] for r in rs]
            dates = [r["date"] for r in rs]
            for i, r in enumerate(rs):
                v = r["sub"].get(sk)
                if v is None:
                    continue
                fr = fwd(closes, i, horizon)
                k2 = bisect.bisect_right(bench_dates, dates[i]) - 1
                if fr is None or k2 < 0 or k2 + horizon >= len(bench_dates):
                    continue
                br = bench_map[bench_dates[k2 + horizon]] / bench_map[bench_dates[k2]] - 1.0
                b = min(4, int(v // 20))
                buckets[b].append(fr - br)
        filled = [len(buckets[b]) for b in range(5)]
        if min(filled) < 20:
            continue
        means = [sum(buckets[b]) / len(buckets[b]) for b in range(5)]
        out.append({"dim": dname, "sub": sname, "q1": means[0], "q5": means[4],
                    "spread": means[4] - means[0],
                    "n": sum(filled)})
    out.sort(key=lambda x: -abs(x["spread"]))
    return out


# ---------------------------------------------------------------------------
# 4) 择时策略：评分 >= 阈值则持有，否则空仓
# ---------------------------------------------------------------------------
def timing_curve(rows, threshold, cost=0.0005):
    dates, equity, holding = [], [], False
    pos = []
    for i in range(len(rows) - 1):
        s = rows[i]["score"]
        want = (s is not None and s >= threshold)
        if want != holding:
            cost_hit = cost
            holding = want
        else:
            cost_hit = 0.0
        r = rows[i + 1]["close"] / rows[i]["close"] - 1.0
        step = (1.0 + r) * (1.0 - cost_hit) if holding else 1.0
        if not equity:
            equity.append(1.0)
        equity.append(equity[-1] * step)
        dates.append(rows[i + 1]["date"])
        pos.append(1 if holding else 0)
    return dates, equity, pos


def timing_report(all_series, bands, cost=0.0005, thresholds=(45, 50, 55, 60, 65, 70)):
    results = []
    for etf, rows in all_series:
        bh = [r["close"] for r in rows]
        bh_m = metrics(bh)
        # 全样本最优阈值
        best = None
        for th in thresholds:
            d, eq, pos = timing_curve(rows, th, cost)
            m = metrics(eq)
            m.update({"threshold": th, "exposure": sum(pos) / float(len(pos)) if pos else 0})
            results.append(dict(m, code=etf["code"], short=etf["short"], scope="full"))
            if best is None or (m["cagr"] or -9) > (best["cagr"] or -9):
                best = m
        results.append(dict(bh_m, code=etf["code"], short=etf["short"], scope="buyhold"))
    return results


def segment_stats(all_series, bands, thresholds, segments, cost=0.0005):
    out = []
    for etf, rows in all_series:
        for seg_name, start, end in segments:
            sub = [r for r in rows if start <= r["date"] <= end]
            if len(sub) < 60:
                continue
            bh = metrics([r["close"] for r in sub])
            out.append({"code": etf["code"], "short": etf["short"], "segment": seg_name,
                        "kind": "买入持有", "cagr": bh.get("cagr"), "vol": bh.get("vol"),
                        "sharpe": bh.get("sharpe"), "mdd": bh.get("mdd"), "n": bh.get("n")})
            for th in thresholds:
                d, eq, pos = timing_curve(sub, th, cost)
                if len(eq) < 2:
                    continue
                m = metrics(eq)
                out.append({"code": etf["code"], "short": etf["short"], "segment": seg_name,
                            "kind": "评分择时≥{}".format(th), "cagr": m.get("cagr"),
                            "vol": m.get("vol"), "sharpe": m.get("sharpe"),
                            "mdd": m.get("mdd"), "n": m.get("n")})
    return out


# ---------------------------------------------------------------------------
# 5) 组合对照
# ---------------------------------------------------------------------------
def portfolio_compare(all_series, bench):
    """两只 ETF 的等权、评分动态与各自的买入持有对照（共同区间）。"""
    series = {}
    for etf, rows in all_series:
        series[etf["code"]] = {r["date"]: r for r in rows}
    common = sorted(set.intersection(*[set(v) for v in series.values()])) if series else []
    bench_map = {b["date"]: b["close"] for b in bench}
    bench_common = [d for d in common if d in bench_map]
    if len(common) < 60:
        return {}, []

    codes = sorted(series)
    out = {}
    # 买入持有
    for code in codes:
        out["买入持有 " + code] = [series[code][d]["close"] for d in common]
    out["等权组合（月度再平衡）"] = None
    # 等权 / 评分动态
    eq, dyn = [1.0], [1.0]
    w_eq = [1.0 / len(codes)] * len(codes)
    w_dyn = list(w_eq)
    last_month = common[0][:7]
    daily = {code: [series[code][d]["close"] for d in common] for code in codes}
    for i in range(1, len(common)):
        month = common[i][:7]
        if month != last_month:
            last_month = month
            # 评分动态：评分差 > 5 时向高分倾斜，否则等权
            scores = [series[c][common[i - 1]]["score"] or 0.0 for c in codes]
            mx, mn = max(scores), min(scores)
            if mx - mn > 5:
                hi = codes[scores.index(mx)]
                w_dyn = [0.8 if c == hi else 0.2 / max(1, len(codes) - 1) for c in codes]
            else:
                w_dyn = [1.0 / len(codes)] * len(codes)
        r_eq = sum(w_eq[j] * (daily[codes[j]][i] / daily[codes[j]][i - 1] - 1.0) for j in range(len(codes)))
        r_dyn = sum(w_dyn[j] * (daily[codes[j]][i] / daily[codes[j]][i - 1] - 1.0) for j in range(len(codes)))
        eq.append(eq[-1] * (1.0 + r_eq))
        dyn.append(dyn[-1] * (1.0 + r_dyn))
    out["等权组合（月度再平衡）"] = eq
    out["评分动态配置（月度）"] = dyn
    if len(bench_common) >= 2:
        base = bench_map[bench_common[0]]
        out["沪深300（基准）"] = [bench_map[d] / base for d in bench_common]
    table = [dict(name=k, **metrics(v)) for k, v in out.items() if v]
    return {"common_start": common[0], "common_end": common[-1], "days": len(common)}, table


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
def write(root, all_series, bench, bands, data_date, tiers=None):
    tiers = tiers_of(tiers)
    out_dir = os.path.join(root, "backtest")
    os.makedirs(out_dir, exist_ok=True)
    band_tbl, band_rows = rating_band_stats(all_series, bench, bands, tiers)
    tier_tbl = tier_stats(band_rows, tiers)
    factors = factor_quintiles(all_series, bench, 60)
    timing = timing_report(all_series, bands)
    segments = [("训练 ≤2024-12", "2020-01-01", "2024-12-31"),
                ("验证 2025", "2025-01-01", "2025-12-31"),
                ("样本外 2026", "2026-01-01", "2026-12-31")]
    seg_tbl = segment_stats(all_series, bands, (55, 60, 65), segments)
    port_meta, port_tbl = portfolio_compare(all_series, bench)

    with open(os.path.join(out_dir, "band_stats.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["h", "rating", "n", "excess", "win", "best", "worst"])
        w.writeheader()
        for h, table in band_tbl.items():
            for row in table:
                w.writerow({"h": h, "rating": row["label"], "n": row["n"], "excess": row["excess"],
                            "win": row["win"], "best": row["best"], "worst": row["worst"]})
    with open(os.path.join(out_dir, "tier_stats.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["h", "tier", "position", "n", "excess", "win", "best", "worst"])
        w.writeheader()
        for h, table in tier_tbl.items():
            for row in table:
                w.writerow({"h": h, "tier": row["label"], "position": row["position"], "n": row["n"],
                            "excess": row["excess"], "win": row["win"],
                            "best": row["best"], "worst": row["worst"]})
    with open(os.path.join(out_dir, "samples.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["code", "date", "h", "score", "rating", "action", "ret", "excess"])
        w.writeheader()
        for row in band_rows:
            w.writerow({k: row.get(k) for k in w.fieldnames})
    with open(os.path.join(out_dir, "factor_quintile.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["dim", "sub", "q1", "q5", "spread", "n"])
        w.writeheader()
        for row in factors:
            w.writerow(row)
    with open(os.path.join(out_dir, "timing.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["code", "short", "scope", "threshold", "total", "cagr",
                                           "vol", "sharpe", "mdd", "exposure", "n"])
        w.writeheader()
        for row in timing:
            w.writerow({k: row.get(k, "") for k in w.fieldnames})
    with open(os.path.join(out_dir, "segments.csv"), "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["code", "short", "segment", "kind", "cagr", "vol",
                                           "sharpe", "mdd", "n"])
        w.writeheader()
        for row in seg_tbl:
            w.writerow(row)

    L = []

    def add(s=""):
        L.append(s)
    add("# 评分体系回测报告")
    add()
    add("- 生成时间：{}".format(data_date))
    add("- 样本：{}".format("、".join("{} {}".format(e["short"], e["code"]) for e, _ in all_series)))
    add("- 成本：单边 0.05%，信号 T 日收盘后生效、T+1 建仓，无未来数据")
    add("- **样本长度限制**：两只 ETF 分别在 2020-01、2023-12 上市，传统 2015 起十年样本不可得；"
        "本回测为 2020 年后的可得区间，563020 仅 2024 年起。结论只能作为弱证据，不能当作参数寻优依据。")
    add()
    add("## 一、评分分档 vs 未来收益")
    add()
    add("分档 = 五档评级带。数值为相对沪深300 的超额收益，正值为跑赢。")
    add()
    for h, table in band_tbl.items():
        add("**未来 {} 个交易日**".format(h))
        add()
        add("| 评级 | 样本数 | 平均超额 | 胜率 | 最好 | 最差 |")
        add("| --- | --- | --- | --- | --- | --- |")
        for row in table:
            add("| {} | {} | {:+.2%} | {:.0%} | {:+.2%} | {:+.2%} |".format(
                row["label"] + (" ※" if row["n"] < 30 else ""), row["n"], row["excess"],
                row["win"], row["best"], row["worst"]))
        add()
    add("※ 样本数 < 30，统计意义不足，仅作方向参考。")
    add()
    add("## 二、四档操作建议 vs 未来收益")
    add()
    add("四档区间：{}（A 积极配置 / B 分批建仓 / C 持有观望 / D 低配等待）。".format(band_text(tiers)))
    add()
    for h, table in tier_tbl.items():
        add("**未来 {} 个交易日**".format(h))
        add()
        add("| 档位 | 分数区间 | 参考仓位 | 样本数 | 平均超额 | 胜率 | 最好 | 最差 |")
        add("| --- | --- | --- | --- | --- | --- | --- | --- |")
        for row in table:
            i = [t["label"] for t in tiers].index(row["label"])
            add("| {} | {} | {} | {} | {:+.2%} | {:.0%} | {:+.2%} | {:+.2%} |".format(
                row["label"] + (" ※" if row["n"] < 30 else ""), range_text(tiers, i), row["position"],
                row["n"], row["excess"], row["win"], row["best"], row["worst"]))
        add()
    add("读法：若四档超额不随档位递升（历史上 D 档反而最高），说明评分更适合做相对排序与温和仓位倾斜，"
        "不能反向使用（“分数低就加仓”）。")
    add()
    add("## 三、单因子五分位（未来 60 日超额，Q5−Q1）")
    add()
    add("| 维度 | 子项 | Q1 | Q5 | 多空差 | 样本 |")
    add("| --- | --- | --- | --- | --- | --- |")
    for row in factors[:12]:
        add("| {} | {} | {:+.2%} | {:+.2%} | {:+.2%} | {} |".format(
            row["dim"], row["sub"], row["q1"], row["q5"], row["spread"], row["n"]))
    add()
    add("注：百分位得分越高 = 该指标越有利，Q5 为最有利组。多空差为正说明该指标方向与收益一致。")
    add()
    add("## 四、择时策略（评分 ≥ 阈值持有，否则空仓）")
    add()
    add("| 标的 | 阈值 | 累计收益 | 年化 | 年化波动 | 夏普 | 最大回撤 | 持仓天数占比 |")
    add("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for row in timing:
        add("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            row["short"], "买入持有" if row["scope"] == "buyhold" else row.get("threshold"),
            f_pct(row.get("total"), sign=True), f_pct(row.get("cagr")), f_pct(row.get("vol")),
            f_num(row.get("sharpe"), 2), f_pct(row.get("mdd")),
            "100%" if row["scope"] == "buyhold" else f_pct(row.get("exposure"), 0)))
    add()
    add("## 五、分段检验（训练 / 验证 / 样本外）")
    add()
    add("| 标的 | 区间 | 策略 | 年化 | 夏普 | 最大回撤 | 天数 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for row in seg_tbl:
        add("| {} | {} | {} | {} | {} | {} | {} |".format(
            row["short"], row["segment"], row["kind"],
            f_pct(row.get("cagr")), f_num(row.get("sharpe"), 2),
            f_pct(row.get("mdd")), row.get("n") or 0))
    add()
    add("## 六、组合对照")
    add()
    if port_tbl:
        add("共同区间：{} → {}，{} 个交易日。".format(port_meta["common_start"], port_meta["common_end"],
                                                 port_meta["days"]))
        add()
        add("| 组合 | 累计收益 | 年化 | 年化波动 | 夏普 | 最大回撤 |")
        add("| --- | --- | --- | --- | --- | --- |")
        for row in port_tbl:
            add("| {} | {} | {} | {} | {} | {} |".format(
                row["name"], f_pct(row.get("total"), sign=True), f_pct(row.get("cagr")),
                f_pct(row.get("vol")), f_num(row.get("sharpe"), 2), f_pct(row.get("mdd"))))) 
        add()
    else:
        add("共同区间不足 60 个交易日，跳过。")
        add()
    add("## 七、结论与使用边界")
    add()
    add("- 若某评级档的超额收益样本量 < 30，该档结论不具统计意义，只能看方向。")
    add("- 四档区间固定为 A ≥70 / B 60–69 / C 45–59 / D <45，不按回测最优点调参；"
        "四档只用于仓位纪律，不构成买卖信号。")
    add("- 择时阈值在样本内挑出的最优值不应当直接使用；上表同时给出 45–70 全网格，用于观察稳健性。")
    add("- 权重仍是金融逻辑给定，回测只用于验证方向与稳定性，不做权重再优化（样本太短，优化必然过拟合）。")
    add("- 后续可扩展：2015 年起的指数层（H30269 / 标普红利低波）日线 + 成分股历史财报，可把样本拉到 10 年。")
    add()
    path = os.path.join(out_dir, "report.md")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L))
    return path, band_tbl, tier_tbl, factors, timing, seg_tbl, port_tbl
