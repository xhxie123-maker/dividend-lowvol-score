# -*- coding: utf-8 -*-
"""执行口径修正的事件研究与仓位纪律回测。

相对 dlv/backtest.py 的差异（都来自外部复核意见）：
1. 成交价：信号在 t 日收盘后生成，一律以 t+1 个交易日的**开盘价**买入/卖出，
   不再用 t 日或 t+1 日收盘价（原口径存在同 day 成交的前视）。
2. 样本：20 / 60 / 120 日持有期改为**非重叠抽样**（同一档位的上一个样本结束后才允许下一个信号），
   避免重叠窗口造成伪样本量；并给出 block bootstrap 的均值置信区间。
3. 夏普：标准口径 mean(日超额) × 250 / (日波动 × √250)，无风险利率取 1.5%。
4. 成本：仓位切换按 |Δ仓位| × 单边万十 双边计费，使「按档位调仓」可成交。
"""

import bisect
import json
import math
import os
import random

from . import feed
from .action import action_of, tiers_of
from .model import stdev

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_KCACHE = {}

TIER_W = {"A": 0.85, "B": 0.60, "C": 0.40, "D": 0.20, "E": 0.05}
RF = 0.015
COST = 0.0010
HORIZONS = (20, 60, 120)
BOOT = 800
BLOCK = 5


def boot_ci(vals, seed=7, n=BOOT):
    """block bootstrap 得到均值的 95% 置信区间。"""
    if len(vals) < 5:
        return None, None
    rnd = random.Random(seed)
    means = []
    for _ in range(n):
        sample = []
        while len(sample) < len(vals):
            s = rnd.randrange(len(vals))
            sample.extend(vals[s:s + BLOCK])
        sample = sample[:len(vals)]
        means.append(sum(sample) / len(sample))
    means.sort()
    return means[int(0.025 * n)], means[int(0.975 * n)]


def open_map(bars):
    return {b["date"]: b.get("open") for b in bars if b.get("open")}


def klines(secid):
    """取带开盘价的日线（feed.bars 只保留收盘价，这里单独再取一次）。"""
    if secid in _KCACHE:
        return _KCACHE[secid]
    code = secid.split(".")[-1]
    symbol = ("sh" if code[0] == "6" else "sz") + code
    out = []
    try:
        raw = feed.http_get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                            {"param": "{},day,2019-01-01,2026-12-31,1700,".format(symbol)},
                            timeout=20)
        node = ((json.loads(raw).get("data") or {}).get(symbol) or {})
        for r in (node.get("day") or node.get("qfqday") or []):
            out.append({"date": r[0], "open": float(r[1]), "close": float(r[2])})
    except Exception as exc:
        print("[WARN] 腾讯开盘价失败 {}：{}".format(symbol, exc), flush=True)
    if not out:
        try:
            raw = feed.http_get(KLINE_URL, {
                "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101", "fqt": "1", "beg": "19900101", "end": "20500101"}, timeout=25)
            for line in ((json.loads(raw).get("data") or {}).get("klines") or []):
                f = line.split(",")
                out.append({"date": f[0], "open": float(f[1]), "close": float(f[2])})
        except Exception as exc:
            print("[WARN] 开盘价序列获取失败 {}：{}".format(secid, exc), flush=True)
    _KCACHE[secid] = out
    return out


def event_study(rows, bars, tiers, n):
    """信号 → t+1 开盘买入、持有 n 个交易日后开盘卖出，同档位样本互不重叠。"""
    opens = open_map(bars)
    dates = [b["date"] for b in bars]
    idx = {d: i for i, d in enumerate(dates)}
    book = {}
    free = {}
    for r in rows:
        i = idx.get(r["date"])
        if i is None:
            continue
        t = action_of(r.get("score"), tiers)
        if not t:
            continue
        key = t["key"]
        if i < free.get(key, -1):
            continue
        j = i + 1
        k = j + n
        if k >= len(dates):
            continue
        o1, o2 = opens.get(dates[j]), opens.get(dates[k])
        if not o1 or not o2:
            continue
        book.setdefault(key, []).append(o2 / o1 - 1.0 - 2 * COST)
        free[key] = k
    return book


def discipline(rows, bars, tiers):
    """按档位仓位纪律合成分时曲线（T+1 生效，切换按 |Δw| 计双边成本）。"""
    closes = {b["date"]: b["close"] for b in bars}
    dates = [b["date"] for b in bars]
    sc = sorted((r["date"], r.get("score")) for r in rows if r.get("score") is not None)
    sd = [d for d, _ in sc]
    eq, eq_bh, w_prev, switches = 1.0, 1.0, 0.0, 0
    curve = []
    for i in range(1, len(dates)):
        p0, p1 = closes.get(dates[i - 1]), closes.get(dates[i])
        if not p0 or not p1:
            continue
        j = bisect.bisect_left(sd, dates[i - 1])
        score = sc[j - 1][1] if j >= 1 else None
        t = action_of(score, tiers)
        w = TIER_W.get(t["key"], 0.0) if t else 0.0
        cost = abs(w - w_prev) * 2 * COST
        if abs(w - w_prev) > 1e-9:
            switches += 1
        w_prev = w
        r = p1 / p0 - 1.0
        eq *= 1.0 + w * r - cost
        eq_bh *= 1.0 + r
        curve.append((dates[i], eq, eq_bh))
    return curve, switches


def curve_metrics(curve):
    if len(curve) < 30:
        return {}
    e = [c[1] for c in curve]
    b = [c[2] for c in curve]
    n = len(e)
    rets = [e[i] / e[i - 1] - 1.0 for i in range(1, n)]
    ex = [r - RF / 250.0 for r in rets]
    sd = stdev(rets)
    sharpe = (sum(ex) / len(ex)) * 250.0 / (sd * math.sqrt(250.0)) if sd else None
    years = n / 250.0
    cagr = e[-1] ** (1.0 / years) - 1.0 if years > 0 else None
    peak, mdd = e[0], 0.0
    for v in e:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    b_hits = sum(1 for i in range(1, n) if rets[i - 1] > b[i] / b[i - 1] - 1.0)
    return {"cagr": cagr, "vol": sd * math.sqrt(250.0) if sd else None,
            "sharpe": sharpe, "mdd": mdd, "beat": b_hits / float(n - 1),
            "total": e[-1] - 1.0, "bh_total": b[-1] - 1.0}


def write(root, series, bars_map, bench, cfg_bands, data_date, cfg_tiers):
    tiers = tiers_of(cfg_tiers)
    L = ["# 执行口径回测（T+1 开盘成交 · 非重叠样本 · 含成本）", "",
         "- 数据日期：{}".format(data_date),
         "- 成交假设：信号收盘后生成，t+1 个交易日开盘价成交；单边成本万十（佣金+冲击），"
         "事件收益按买卖各一次共 20bp 扣除",
         "- 样本：20/60/120 个交易日持有，同档位非重叠抽样；置信区间为 block bootstrap（块长 5）",
         "- 夏普：mean(日超额)×250 /(日波动×√250)，无风险利率 {:.1f}%".format(RF * 100), ""]

    for etf, rows in series:
        bars = klines(etf.get("secid") or "") or [
            {"date": b["date"], "open": b["close"], "close": b["close"]}
            for b in (bars_map.get(etf["code"]) or [])]
        L.append("## {}（{}）".format(etf["name"], etf["code"]))
        L.append("")
        for n in HORIZONS:
            book = event_study(rows, bars, tiers, n)
            L.append("### 持有 {} 个交易日".format(n))
            L.append("")
            L.append("| 档位 | 样本数 | 平均净收益 | 中位数 | 胜率 | 95% 置信区间 |")
            L.append("| --- | --- | --- | --- | --- | --- |")
            for t in tiers:
                vals = book.get(t["key"]) or []
                if not vals:
                    L.append("| {} | 0 | — | — | — | — |".format(t["key"]))
                    continue
                sv = sorted(vals)
                lo, hi = boot_ci(vals)
                mid = sv[len(sv) // 2]
                L.append("| {} | {} | {:+.2%} | {:+.2%} | {:.0%} | {} |".format(
                    t["key"], len(vals), sum(vals) / len(vals), mid,
                    sum(1 for v in vals if v > 0) / float(len(vals)),
                    "—" if lo is None else "{:+.2%} ~ {:+.2%}".format(lo, hi)))
            L.append("")
        curve, switches = discipline(rows, bars, tiers)
        m = curve_metrics(curve)
        L.append("### 档位仓位纪律（T+1 生效、含切换成本）")
        L.append("")
        if m:
            L.append("- 累计收益 {:.2%}（买入持有 {:.2%}），年化 {:.2%}，年化波动 {:.2%}".format(
                m["total"], m["bh_total"], m["cagr"], m["vol"]))
            L.append("- 夏普 {:.2f}，最大回撤 {:.2%}，日胜率（跑赢持有）{:.0%}，调仓 {} 次".format(
                m["sharpe"], m["mdd"], m["beat"], switches))
            L.append("- 仓位取值：A 85% / B 60% / C 40% / D 20% / E 5%，仅为「红利低波策略预算」内的比例，"
                     "不等于全部资产仓位")
        else:
            L.append("- 样本不足，无法计算")
        L.append("")

    L.append("## 口径说明与局限")
    L.append("")
    L.append("- 仍属短样本：ETF 日线自 2020 年起（563020 于 2023-12 上市），不足以覆盖两轮完整牛熊；"
             "指数层面的十年回测需要历史成分与权重，免费源不提供，未做。")
    L.append("- 档位边界固定为 A≥70 / B 60–69 / C 45–59 / D 35–44 / E<35，不按回测最优点调参，避免过拟合。")
    L.append("- 事件研究为非重叠抽样，样本数明显少于重叠口径，显著性判断更保守。")
    L.append("")
    path = os.path.join(root, "backtest", "execution.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L))
    return path
