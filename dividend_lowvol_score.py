#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
红利低波 ETF 每日监测评分系统
================================

监测标的
  - 515450 红利低波50ETF南方（南方标普中国A股大盘红利低波50ETF）
  - 563020 红利低波ETF易方达（易方达中证红利低波动ETF）

评分模型（7 维度，加权 0-100 分）
  趋势 25 / 动量 20 / 波动 20 / 回撤 15 / 估值 10 / 红利 5 / 交易质量 5
  某一维度数据缺失时，按剩余维度权重重新归一化，并在日志中标注覆盖率。

数据来源（全部为公开免费接口，无证书、无账号依赖）
  - 日线（前复权）：push2his.eastmoney.com  K 线接口
  - ETF 净值、每份分红：api.fund.eastmoney.com / f10/lsjz
  - 基准：沪深300 指数（000300）

每日产出（均提交回 GitHub 仓库）
  - data/daily_scores.csv              全部标的逐日评分（滚动积存）
  - data/history/YYYY-MM-DD.json       当日完整快照（原始指标 + 子分项）
  - logs/YYYY-MM-DD.md                 当日独立日志存档
  - docs/index.html                    GitHub Pages 看板

运行：python dividend_lowvol_score.py [--offline]
"""

import argparse
import bisect
import csv
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.abspath(__file__))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"

_FORCE_CURL = False


# ----------------------------------------------------------------------------
# 网络层：优先 urllib，受限环境自动回退到系统 curl
# ----------------------------------------------------------------------------
def _curl_get(url, headers=None, timeout=25):
    cmd = ["curl", "-sS", "-L", "--max-time", str(timeout), "-A", UA]
    for k, v in (headers or {}).items():
        cmd += ["-H", "{}: {}".format(k, v)]
    cmd.append(url)
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError("curl 请求失败 rc={} {}".format(res.returncode, res.stderr.strip()[:200]))
    return res.stdout


def http_get(url, params=None, headers=None, timeout=25, tries=3):
    """返回文本。若 urllib 被环境阻断，一次性切换到 curl，避免每日无谓重试。"""
    global _FORCE_CURL
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    if not _FORCE_CURL:
        for i in range(tries):
            try:
                req = urllib.request.Request(
                    url, headers=dict({"User-Agent": UA}, **(headers or {})))
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read().decode("utf-8", "replace")
            except Exception:
                time.sleep(1 + i)
        _FORCE_CURL = True
    for i in range(tries):
        try:
            return _curl_get(url, headers, timeout)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(2 + i)


# ----------------------------------------------------------------------------
# 数值工具
# ----------------------------------------------------------------------------
def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def scale(x, lo, hi):
    """线性映射到 0-100：x<=lo 得 0，x>=hi 得 100。"""
    if x is None:
        return None
    if hi == lo:
        return 50.0
    return 100.0 * clamp((x - lo) / (hi - lo), 0.0, 1.0)


def center(x, span):
    """以 0 为中心映射到 0-100：x=0 得 50，x>=span 得 100，x<=-span 得 0。"""
    if x is None:
        return None
    return 50.0 + 50.0 * clamp(x / span, -1.0, 1.0)


def mean(vals):
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return sum(vals) / len(vals)


def stdev(vals):
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1))


def pct(x, nd=2):
    return "—" if x is None else "{:.{}f}%".format(x * 100, nd)


def num(x, nd=2):
    return "—" if x is None else "{:.{}f}".format(x, nd)


# ----------------------------------------------------------------------------
# 数据抓取
# ----------------------------------------------------------------------------
def fetch_kline(secid, fqt=1):
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": str(fqt), "beg": "19900101", "end": "20500101",
    }
    js = json.loads(http_get(KLINE_URL, params))
    data = js.get("data") or {}
    bars = []
    for line in data.get("klines", []):
        f = line.split(",")
        bars.append({
            "date": f[0], "open": float(f[1]), "close": float(f[2]),
            "high": float(f[3]), "low": float(f[4]),
            "volume": float(f[5]), "amount": float(f[6]),
        })
    if not bars:
        raise RuntimeError("K 线为空：{}".format(secid))
    return data.get("name") or secid, bars


def nav_cache_path(code):
    return os.path.join(ROOT, "data", "cache", "nav_{}.csv".format(code))


def load_nav_cache(code):
    path = nav_cache_path(code)
    rows = {}
    if not os.path.exists(path):
        return rows
    with open(path, "r", encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            rows[row["date"]] = {
                "dwjz": float(row["dwjz"]) if row["dwjz"] else None,
                "ljjz": float(row["ljjz"]) if row["ljjz"] else None,
                "fhfcz": float(row["fhfcz"]) if row["fhfcz"] else 0.0,
            }
    return rows


def save_nav_cache(code, rows):
    path = nav_cache_path(code)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    keys = sorted(rows)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "dwjz", "ljjz", "fhfcz"])
        for k in keys:
            r = rows[k]
            w.writerow([k, "" if r["dwjz"] is None else r["dwjz"],
                        "" if r["ljjz"] is None else r["ljjz"], r["fhfcz"]])


def fetch_nav_page(code, page):
    raw = http_get(
        NAV_URL,
        {"fundCode": code, "pageIndex": page, "pageSize": 20},
        {"Referer": "https://fundf10.eastmoney.com/"},
    )
    js = json.loads(raw)
    block = js.get("Data") or {}
    out = []
    for item in block.get("LSJZList") or []:
        d = (item.get("FSRQ") or "").strip()
        if not d:
            continue
        out.append((d, {
            "dwjz": float(item["DWJZ"]) if item.get("DWJZ") else None,
            "ljjz": float(item["LJJZ"]) if item.get("LJJZ") else None,
            "fhfcz": float(item["FHFCZ"]) if item.get("FHFCZ") else 0.0,
        }))
    return out


def update_nav_cache(code, need_from, offline=False):
    """增量更新净值缓存；首次运行会向前翻页直到覆盖 need_from。"""
    rows = load_nav_cache(code)
    if offline or (rows and min(rows) <= need_from):
        if rows:
            return rows
    page = 1
    earliest = min(rows) if rows else "9999-99-99"
    while page <= 120:
        batch = fetch_nav_page(code, page)
        if not batch:
            break
        for d, v in batch:
            rows.setdefault(d, v)
            if rows[d]["dwjz"] is None and v["dwjz"] is not None:
                rows[d] = v
        earliest = min(rows)
        if earliest <= need_from or len(batch) < 20:
            break
        page += 1
        time.sleep(0.15)
    save_nav_cache(code, rows)
    return rows


# ----------------------------------------------------------------------------
# 指标计算
# ----------------------------------------------------------------------------
def build_raw_series(bars, raw_bars, nav, cfg):
    """逐日计算原始指标（不做评分）。
    bars: 前复权（用于收益/均线/波动/回撤）
    raw_bars: 不复权（用于折溢价、分红率，与基金净值口径一致）
    """
    dates = [b["date"] for b in bars]
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    amounts = [b["amount"] for b in bars]
    raw_close = {b["date"]: b["close"] for b in raw_bars}
    nav_dates = sorted(nav)
    val_window = cfg["windows"]["valuation_days"]

    series = []
    for i, d in enumerate(dates):
        n = i + 1
        row = {"date": d, "close": closes[i], "amount": amounts[i]}

        def ret(k):
            if i - k < 0:
                return None
            return closes[i] / closes[i - k] - 1.0

        def ma(k):
            if n < k:
                return None
            return sum(closes[i - k + 1:i + 1]) / k

        row["r20"] = ret(20)
        row["r60"] = ret(60)
        row["r120"] = ret(120)
        row["r250"] = ret(250)
        row["ma20"] = ma(20)
        row["ma60"] = ma(60)
        row["ma120"] = ma(120)
        row["ma250"] = ma(250)

        # MA20 五日斜率
        if n >= 25:
            prev_ma20 = sum(closes[i - 24:i - 4]) / 20
            row["ma20_slope5"] = row["ma20"] / prev_ma20 - 1.0
        else:
            row["ma20_slope5"] = None

        # 波动率（年化）
        def vol(k):
            if n < k + 1:
                return None
            seg = [math.log(closes[j] / closes[j - 1])
                   for j in range(i - k + 1, i + 1) if closes[j - 1] > 0]
            sd = stdev(seg)
            return None if sd is None else sd * math.sqrt(252)

        row["vol20"] = vol(20)
        row["vol60"] = vol(60)

        # 下行波动（60 日）
        if n >= 61:
            seg = [math.log(closes[j] / closes[j - 1])
                   for j in range(i - 59, i + 1)
                   if closes[j - 1] > 0 and closes[j] / closes[j - 1] < 1.0]
            sd = stdev(seg)
            row["downside_vol60"] = None if sd is None else sd * math.sqrt(252)
        else:
            row["downside_vol60"] = None

        # 回撤
        if n >= 250:
            peak = max(closes[i - 249:i + 1])
            row["dd250"] = closes[i] / peak - 1.0
            hi = max(highs[i - 249:i + 1])
            row["dd250_high"] = closes[i] / hi - 1.0
        else:
            peak = max(closes)
            row["dd250"] = closes[i] / peak - 1.0
            row["dd250_high"] = None

        if n >= 60:
            window = closes[i - 59:i + 1]
            peak, mdd = window[0], 0.0
            for c in window:
                peak = max(peak, c)
                mdd = min(mdd, c / peak - 1.0)
            row["maxdd60"] = mdd
        else:
            row["maxdd60"] = None

        # 估值位置：收盘价在近 valuation_days 个交易日区间的分位
        seg = closes[max(0, i - val_window + 1):i + 1]
        if len(seg) >= 120:
            below = sum(1 for c in seg if c <= closes[i])
            row["val_pct"] = below / float(len(seg)) * 100.0
            row["val_window_n"] = len(seg)
        else:
            row["val_pct"] = None
            row["val_window_n"] = len(seg)

        # 折溢价：不复权收盘价 vs 同日单位净值
        row["nav"] = None
        row["premium"] = None
        price_raw = raw_close.get(d, closes[i])
        row["close_raw"] = price_raw
        j = len(nav_dates) - 1
        while j >= 0 and nav_dates[j] > d:
            j -= 1
        if j >= 0:
            nd = nav_dates[j]
            nav_v = nav[nd]["dwjz"]
            if nav_v and (datetime.strptime(d, "%Y-%m-%d") -
                          datetime.strptime(nd, "%Y-%m-%d")).days <= 7:
                row["nav"] = nav_v
                row["nav_date"] = nd
                row["premium"] = price_raw / nav_v - 1.0

        # 近 12 个月每份分红合计（TTM）
        row["ttm_div"] = None
        earliest_nav = nav_dates[0] if nav_dates else None
        if earliest_nav:
            start = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
            if earliest_nav <= start:
                total = sum(nav[k]["fhfcz"] for k in nav_dates
                            if start < k <= d and nav[k]["fhfcz"])
                row["ttm_div"] = total
                row["ttm_div_yield"] = total / price_raw

        # 成交额
        if n >= 20:
            row["amount20"] = sum(amounts[i - 19:i + 1]) / 20.0
        else:
            row["amount20"] = None
        if n >= 60:
            row["amount60"] = sum(amounts[i - 59:i + 1]) / 60.0
            row["amount_ratio"] = amounts[i] / row["amount60"] if row["amount60"] else None
        else:
            row["amount60"] = None
            row["amount_ratio"] = None

        series.append(row)

    # 折溢价 20 日均值
    for i, row in enumerate(series):
        seg = [s["premium"] for s in series[max(0, i - 19):i + 1] if s["premium"] is not None]
        row["premium20"] = sum(seg) / len(seg) if seg else None

    # 股息率分位（TTM 现金分红率在近 val_window 日中的位置，越高=相对便宜）
    for i, row in enumerate(series):
        seg = [s["ttm_div_yield"] for s in series[max(0, i - val_window + 1):i + 1]
               if s.get("ttm_div_yield") is not None]
        if row.get("ttm_div_yield") is not None and len(seg) >= 60:
            below = sum(1 for v in seg if v <= row["ttm_div_yield"])
            row["div_yield_pct"] = below / float(len(seg)) * 100.0
        else:
            row["div_yield_pct"] = None

    return series


def attach_benchmark(series, bench_bars):
    bmap = {b["date"]: b["close"] for b in bench_bars}
    bdates = sorted(bmap)
    for row in series:
        d = row["date"]

        def bench_ret(k):
            # 取 <= d 的最近一个基准交易日，再回溯 k 个交易日
            k2 = bisect.bisect_right(bdates, d) - 1
            if k2 < 0 or k2 - k < 0:
                return None
            return bmap[bdates[k2]] / bmap[bdates[k2 - k]] - 1.0

        rb20 = bench_ret(20)
        rb60 = bench_ret(60)
        row["excess20"] = None if (row["r20"] is None or rb20 is None) else row["r20"] - rb20
        row["excess60"] = None if (row["r60"] is None or rb60 is None) else row["r60"] - rb60
    return series


# ----------------------------------------------------------------------------
# 评分
# ----------------------------------------------------------------------------
def score_row(row):
    """返回 (子分字典, 综合分, 覆盖率说明)。"""
    sub = {}

    # 趋势
    t1 = center(None if row["ma20"] is None else (row["close"] / row["ma20"] - 1.0), 0.03)
    t2 = None if (row["ma20"] is None or row["ma60"] is None) else center(row["ma20"] / row["ma60"] - 1.0, 0.02)
    t3 = center(row.get("ma20_slope5"), 0.03)
    t4 = None if row["ma120"] is None else center(row["close"] / row["ma120"] - 1.0, 0.05)
    sub["trend"] = mean([t1, t2, t3, t4])

    # 动量
    m1 = center(row["r20"], 0.10)
    m2 = center(row["r60"], 0.20)
    m3 = center(row["excess20"], 0.08)
    m4 = center(row["excess60"], 0.15)
    sub["momentum"] = mean([m1, m2, m3, m4])

    # 波动（越低越好；红利低波常态波动 8%-20%，按此区间标定）
    v1 = None if row["vol20"] is None else scale(20.0 - row["vol20"] * 100.0, 0.0, 12.0)
    v2 = None if row["vol60"] is None else scale(18.0 - row["vol60"] * 100.0, 0.0, 11.0)
    v3 = None if row["downside_vol60"] is None else scale(13.0 - row["downside_vol60"] * 100.0, 0.0, 10.0)
    sub["volatility"] = mean([v1, v2, v3])

    # 回撤（越浅越好）
    d1 = None if row["dd250"] is None else scale(row["dd250"], -0.12, 0.0)
    d2 = None if row["maxdd60"] is None else scale(row["maxdd60"], -0.08, 0.0)
    sub["drawdown"] = mean([d1, d2])

    # 估值：价格区间分位（低分位得分高） + 股息率历史分位（高分位得分高）
    vp = None if row["val_pct"] is None else 100.0 - row["val_pct"]
    yp = row.get("div_yield_pct")
    sub["valuation"] = mean([vp, yp])

    # 红利：TTM 现金分红率（2% 以下偏弱，5% 以上满分）
    sub["dividend"] = None if row.get("ttm_div_yield") is None else scale(row["ttm_div_yield"], 0.02, 0.05)

    # 交易质量：流动性 + 成交活跃度 + 折溢价
    l1 = None if row["amount20"] is None else scale(row["amount20"] / 1e8, 0.0, 2.0)
    l2 = None if row.get("amount_ratio") is None else center(row["amount_ratio"] - 1.0, 0.5)
    l3 = None if row.get("premium20") is None else scale(0.006 - abs(row["premium20"]), 0.0, 0.006)
    sub["trading"] = mean([l1, l2, l3])

    weights = CONFIG["weights"]
    total_w = 0.0
    acc = 0.0
    for k in weights:
        if sub.get(k) is not None:
            acc += weights[k] * sub[k]
            total_w += weights[k]
    score = None if total_w == 0 else acc / total_w
    missing = [k for k in weights if sub.get(k) is None]
    return sub, score, total_w / sum(weights.values()), missing


def rating_of(score):
    for band in CONFIG["rating_bands"]:
        if score >= band["min"]:
            return band
    return CONFIG["rating_bands"][-1]


def narrative(row, sub, score):
    bits = []
    if row["ma20"] and row["ma60"]:
        if row["close"] > row["ma20"] > row["ma60"]:
            bits.append("价格站上 MA20 与 MA60，短中期趋势同向")
        elif row["close"] < row["ma20"] < row["ma60"]:
            bits.append("价格位于 MA20 与 MA60 下方，短中期趋势偏空")
        elif row["close"] > row["ma20"]:
            bits.append("价格回到 MA20 上方，短期有修复迹象")
        else:
            bits.append("价格跌破 MA20，短期动能走弱")
    if row["r60"] is not None:
        bits.append("近 60 日{}".format("上涨 " + pct(row["r60"]) if row["r60"] >= 0
                                        else "下跌 " + pct(abs(row["r60"]))))
    if row["excess60"] is not None:
        bits.append("相对沪深300 {}".format("跑赢 " + pct(row["excess60"]) if row["excess60"] >= 0
                                             else "跑输 " + pct(abs(row["excess60"]))))
    if row["vol20"] is not None:
        bits.append("20 日年化波动 {}（60 日 {}）".format(pct(row["vol20"]), pct(row["vol60"])))
    if row["dd250"] is not None:
        bits.append("距 250 日高点回撤 {}".format(pct(row["dd250"])))
    if row["val_pct"] is not None:
        bits.append("价格处于近 {} 日区间 {} 分位".format(row.get("val_window_n"), num(row["val_pct"], 0)))
    if row.get("div_yield_pct") is not None:
        bits.append("股息率处于近 {} 日 {} 分位".format(row.get("val_window_n"), num(row["div_yield_pct"], 0)))
    if row.get("premium20") is not None:
        bits.append("近 20 日平均折溢价 {}".format(pct(row["premium20"])))
    if row.get("ttm_div_yield") is not None:
        bits.append("近 12 个月现金分红率 {}".format(pct(row["ttm_div_yield"])))
    rating = rating_of(score)
    bits.append("综合评分 {}（{}）：{}".format(num(score, 1), rating["label"], rating["action"]))
    return "；".join(bits) + "。"


# ----------------------------------------------------------------------------
# 主流程
# ----------------------------------------------------------------------------
def compute_etf(etf, bench_bars, offline=False):
    _, bars = fetch_kline(etf["secid"], fqt=1)
    _, raw_bars = fetch_kline(etf["secid"], fqt=0)
    need_from = (datetime.now(CST) - timedelta(days=CONFIG["windows"]["nav_lookback_days"])) \
        .strftime("%Y-%m-%d")
    nav = update_nav_cache(etf["code"], need_from, offline=offline)
    series = build_raw_series(bars, raw_bars, nav, CONFIG)
    series = attach_benchmark(series, bench_bars)
    for row in series:
        sub, score, cover, missing = score_row(row)
        row["sub"] = sub
        row["score"] = score
        row["coverage"] = cover
        row["missing"] = missing
    return series


def write_scores_csv(all_series):
    path = os.path.join(ROOT, "data", "daily_scores.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fields = ["date", "code", "short", "close", "score", "rating", "coverage",
              "trend", "momentum", "volatility", "drawdown", "valuation",
              "dividend", "trading", "r20", "r60", "r120", "r250",
              "excess20", "excess60", "vol20", "vol60", "dd250", "val_pct",
              "div_yield_pct", "premium", "premium20", "ttm_div_yield", "amount20_yi"]
    fresh_rows = []
    for etf, series in all_series:
        for row in series[-CONFIG["windows"]["backfill_days"]:]:
            if row["score"] is not None:
                fresh_rows.append((row["date"], etf, row))

    old = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            old = list(csv.DictReader(fh))
    if fresh_rows:
        cutoff = min(r[0] for r in fresh_rows)
        old = [r for r in old if r["date"] < cutoff]

    def fmt(row, etf):
        sub = row["sub"]
        return {
            "date": row["date"], "code": etf["code"], "short": etf["short"],
            "close": round(row["close"], 3), "score": round(row["score"], 2),
            "rating": rating_of(row["score"])["label"],
            "coverage": round(row["coverage"], 3),
            "trend": None if sub["trend"] is None else round(sub["trend"], 2),
            "momentum": None if sub["momentum"] is None else round(sub["momentum"], 2),
            "volatility": None if sub["volatility"] is None else round(sub["volatility"], 2),
            "drawdown": None if sub["drawdown"] is None else round(sub["drawdown"], 2),
            "valuation": None if sub["valuation"] is None else round(sub["valuation"], 2),
            "dividend": None if sub["dividend"] is None else round(sub["dividend"], 2),
            "trading": None if sub["trading"] is None else round(sub["trading"], 2),
            "r20": None if row["r20"] is None else round(row["r20"], 5),
            "r60": None if row["r60"] is None else round(row["r60"], 5),
            "r120": None if row["r120"] is None else round(row["r120"], 5),
            "r250": None if row["r250"] is None else round(row["r250"], 5),
            "excess20": None if row["excess20"] is None else round(row["excess20"], 5),
            "excess60": None if row["excess60"] is None else round(row["excess60"], 5),
            "vol20": None if row["vol20"] is None else round(row["vol20"], 5),
            "vol60": None if row["vol60"] is None else round(row["vol60"], 5),
            "dd250": None if row["dd250"] is None else round(row["dd250"], 5),
            "val_pct": None if row["val_pct"] is None else round(row["val_pct"], 2),
            "div_yield_pct": None if row.get("div_yield_pct") is None else round(row["div_yield_pct"], 2),
            "premium": None if row.get("premium") is None else round(row["premium"], 5),
            "premium20": None if row.get("premium20") is None else round(row["premium20"], 5),
            "ttm_div_yield": None if row.get("ttm_div_yield") is None else round(row["ttm_div_yield"], 5),
            "amount20_yi": None if row["amount20"] is None else round(row["amount20"] / 1e8, 3),
        }

    new_rows = []
    for d, etf, row in fresh_rows:
        new_rows.append((d, etf["code"], fmt(row, etf)))

    merged = list(old)
    for _, _, row in new_rows:
        merged.append({k: ("" if v is None else v) for k, v in row.items()})
    merged.sort(key=lambda r: (r["date"], r["code"]))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in merged:
            w.writerow({k: r.get(k, "") for k in fields})
    return merged


def write_snapshot(all_series, data_date, run_at):
    path = os.path.join(ROOT, "data", "history", "{}.json".format(data_date))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"run_at": run_at, "data_date": data_date, "etfs": []}
    for etf, series in all_series:
        row = series[-1]
        clean = {k: v for k, v in row.items()
                 if k not in ("sub", "score", "coverage", "missing")}
        payload["etfs"].append({
            "code": etf["code"], "name": etf["name"], "short": etf["short"],
            "index": etf["index"],
            "score": None if row["score"] is None else round(row["score"], 2),
            "rating": rating_of(row["score"])["label"],
            "sub": {k: (None if v is None else round(v, 2)) for k, v in row["sub"].items()},
            "coverage": round(row["coverage"], 3),
            "missing": row["missing"],
            "metrics": {k: v for k, v in clean.items()},
        })
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)
    return path


DIM_LABEL = {
    "trend": "趋势", "momentum": "动量", "volatility": "波动",
    "drawdown": "回撤", "valuation": "估值", "dividend": "红利", "trading": "交易质量",
}


def series_tail(series, date, k):
    """返回该日期之前 k 条（含当日）的评分序列。"""
    out = []
    for row in series:
        if row["date"] <= date and row["score"] is not None:
            out.append(row)
    return out[-k:]


def write_log(all_series, data_date, run_at, errors):
    path = os.path.join(ROOT, "logs", "{}.md".format(data_date))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines = []
    add = lines.append
    add("# 红利低波 ETF 每日监测评分 · {}".format(data_date))
    add("")
    add("- 生成时间：{}（北京时间）".format(run_at))
    add("- 数据日期：{}（最新交易日收盘）".format(data_date))
    add("- 监测标的：{}".format("、".join("{} {}".format(e["short"], e["code"]) for e, _ in all_series)))
    add("- 数据来源：东方财富日线（前复权）、基金净值与分红披露、沪深300 指数")
    add("")
    add("## 一、评分总览")
    add("")
    add("| 标的 | 代码 | 收盘价 | 综合评分 | 评级 | 较前一日 | 数据覆盖率 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for etf, series in all_series:
        row = series[-1]
        prev = series_tail(series, row["date"], 2)
        delta = None if len(prev) < 2 else prev[-1]["score"] - prev[-2]["score"]
        d_txt = "—" if delta is None else ("{:+.1f}".format(delta))
        add("| {} | {} | {} | {} | {} | {} | {} |".format(
            etf["short"], etf["code"], num(row["close"], 3),
            num(row["score"], 1), rating_of(row["score"])["label"], d_txt,
            "{:.0f}%".format(row["coverage"] * 100)))
    add("")

    add("## 二、标的评分拆解")
    add("")
    for etf, series in all_series:
        row = series[-1]
        sub = row["sub"]
        add("### {}（{}）".format(etf["short"], etf["code"]))
        add("")
        add("| 维度 | 权重 | 得分 | 状态 |")
        add("| --- | --- | --- | --- |")
        for k, w in CONFIG["weights"].items():
            v = sub.get(k)
            if v is None:
                add("| {} | {}% | 缺失 | 权重已按比例再分配 |".format(DIM_LABEL[k], w))
            else:
                level = "优" if v >= 70 else ("良" if v >= 55 else ("中性" if v >= 45 else "弱"))
                add("| {} | {}% | {} | {} |".format(DIM_LABEL[k], w, num(v, 1), level))
        add("")
        add("关键指标：")
        add("")
        add("- 收盘价 {} / 单位净值 {}（{}）".format(
            num(row["close"], 3), num(row.get("nav"), 4), row.get("nav_date") or "缺失"))
        add("- 20 / 60 / 120 日收益：{} / {} / {}".format(pct(row["r20"]), pct(row["r60"]), pct(row["r120"])))
        add("- 相对沪深300 超额：20 日 {}，60 日 {}".format(pct(row["excess20"]), pct(row["excess60"])))
        add("- MA20 {} / MA60 {} / MA120 {}".format(num(row["ma20"], 3), num(row["ma60"], 3), num(row["ma120"], 3)))
        add("- 年化波动：20 日 {}，60 日 {}，60 日下行波动 {}".format(
            pct(row["vol20"]), pct(row["vol60"]), pct(row["downside_vol60"])))
        add("- 回撤：距 250 日高点 {}，60 日最大回撤 {}".format(pct(row["dd250"]), pct(row["maxdd60"])))
        add("- 估值位置：价格处于近 {} 日区间 {} 分位；股息率处于近 {} 日 {} 分位".format(
            row.get("val_window_n"), num(row["val_pct"], 0),
            row.get("val_window_n"), num(row.get("div_yield_pct"), 0)))
        add("- 折溢价：当日 {}，近 20 日均值 {}".format(pct(row.get("premium")), pct(row.get("premium20"))))
        add("- 近 12 个月现金分红率：{}（每份合计 {} 元）".format(
            pct(row.get("ttm_div_yield")), num(row.get("ttm_div"), 4)))
        add("- 20 日均成交额 {} 亿元，当日成交额 / 60 日均值 {} 倍".format(
            num(row["amount20"] / 1e8 if row["amount20"] else None, 2),
            num(row.get("amount_ratio"), 2)))
        add("")
        add("解读：{}".format(narrative(row, sub, row["score"])))
        add("")

    add("## 三、横向对比与结论")
    add("")
    if len(all_series) >= 2:
        ranked = sorted([(s[-1]["score"], e, s) for e, s in all_series],
                        key=lambda t: t[0], reverse=True)
        top, second = ranked[0], ranked[1]
        diff = top[0] - second[0]
        add("- 评分排序：{}（{}） > {}（{}）".format(
            top[1]["short"], num(top[0], 1), second[1]["short"], num(second[0], 1)))
        if diff >= 5:
            add("- 两者相差 {} 分，月度级别上更偏向评分较高的 {}；另一只可作为补充或等待评分收敛。".format(
                num(diff, 1), top[1]["short"]))
        else:
            add("- 两者仅相差 {} 分，评分层面不构成明显优劣，可结合费率、规模与跟踪误差决定（易方达 563020 综合费率 0.20%，低于南方 515450 的 0.60%）。".format(num(diff, 1)))
        add("")
        add("| 维度 | {} | {} | 差值 |".format(top[1]["short"], second[1]["short"]))
        add("| --- | --- | --- | --- |")
        for k in CONFIG["weights"]:
            a = top[2][-1]["sub"].get(k)
            b = second[2][-1]["sub"].get(k)
            add("| {} | {} | {} | {} |".format(
                DIM_LABEL[k], num(a, 1), num(b, 1),
                "—" if (a is None or b is None) else "{:+.1f}".format(a - b)))
        add("")
    if errors:
        add("## 四、数据异常")
        add("")
        for e in errors:
            add("- {}".format(e))
        add("")
    add("## 五、口径与免责")
    add("")
    add("- 评分为相对强弱打分，不是买卖建议；分值是策略内部排序工具。")
    add("- 估值维度 = 价格区间分位（反向）与股息率历史分位（正向）的均值，非指数 PE；折溢价为不复权收盘价对同日单位净值。")
    add("- 缺失维度按剩余权重归一化，因此跨日比较时请关注日志中的覆盖率。")
    add("- 每日日志独立存档，历史评分见 `data/daily_scores.csv`，快照见 `data/history/`。")
    add("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    return path


def sparkline(values, width=260, height=64, color="#0f766e"):
    if len(values) < 2:
        return ""
    n = len(values)
    lo, hi = min(values), max(values)
    pad = max(2.0, (hi - lo) * 0.15)
    d0, d1 = lo - pad, hi + pad
    span = (d1 - d0) or 1.0

    def ypos(v):
        return height - 6 - ((clamp(v, d0, d1) - d0) / span) * (height - 12)

    step = width / float(n - 1)
    pts = [(i * step, ypos(v)) for i, v in enumerate(values)]
    poly = " ".join("{:.1f},{:.1f}".format(x, y) for x, y in pts)
    area = "{:.1f},{:.1f} ".format(pts[0][0], height) + poly + \
           " {:.1f},{:.1f}".format(pts[-1][0], height)
    last = pts[-1]
    mid = ypos(sum(values) / float(n))
    return (
        '<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none" class="spark">'
        '<line x1="0" y1="{mid:.1f}" x2="{w}" y2="{mid:.1f}" stroke="#94a3b8" stroke-width="1" stroke-dasharray="4 4" opacity="0.55"/>'
        '<polygon points="{area}" fill="{color}" opacity="0.10"/>'
        '<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2.4" stroke-linejoin="round"/>'
        '<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.4" fill="{color}"/>'
        '</svg>'
    ).format(w=width, h=height, mid=mid, area=area, poly=poly,
             color=color, cx=last[0], cy=last[1])


def write_dashboard(all_series, data_date, run_at, repo_slug, scores_rows):
    path = os.path.join(ROOT, "docs", "index.html")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cards = []
    for etf, series in all_series:
        row = series[-1]
        sub = row["sub"]
        rating = rating_of(row["score"])
        tail = series_tail(series, row["date"], 30)
        vals = [r["score"] for r in tail]
        prev = series_tail(series, row["date"], 2)
        delta = None if len(prev) < 2 else prev[-1]["score"] - prev[-2]["score"]
        d1 = "—" if delta is None else "{:+.1f}".format(delta)
        bars = []
        for k, w in CONFIG["weights"].items():
            v = sub.get(k)
            pctv = 0 if v is None else v
            label = "缺失" if v is None else num(v, 1)
            bars.append(
                '<div class="dim"><div class="dim-h"><span>{name}</span>'
                '<span class="dim-v">{val}</span></div>'
                '<div class="dim-bar"><i style="width:{pctv:.1f}%"></i></div></div>'.format(
                    name=DIM_LABEL[k], val=label, pctv=pctv))
        badge_cls = {"强势": "s5", "偏强": "s4", "中性": "s3", "偏弱": "s2", "弱势": "s1"}[rating["label"]]
        cards.append(
            '<section class="card"><header><div><h2>{short}</h2>'
            '<p class="sub">{code} · {index}</p></div>'
            '<span class="badge {bc}">{lab}</span></header>'
            '<div class="score-row"><div class="score">{sc}</div>'
            '<div class="score-meta"><span class="delta">较前一日 {d1}</span>'
            '<span>收盘价 {close}</span><span>覆盖率 {cov:.0f}%</span></div></div>'
            '<div class="spark-wrap">{spark}</div>'
            '<p class="spark-cap">近 {nd} 日区间 {lo} – {hi}（虚线为均值 {avg}）</p>'
            '<p class="action">{act}</p>'
            '<div class="dims">{bars}</div></section>'.format(
                short=etf["short"], code=etf["code"], index=etf["index"],
                bc=badge_cls, lab=rating["label"], sc=num(row["score"], 1), d1=d1,
                close=num(row["close"], 3), cov=row["coverage"] * 100,
                spark=sparkline(vals),
                nd=len(vals), lo=num(min(vals), 1), hi=num(max(vals), 1),
                avg=num(sum(vals) / len(vals), 1),
                act=rating["action"], bars="".join(bars)))
    # 对比表
    names = [e["short"] for e, _ in all_series]
    comp = ['<table class="tbl"><thead><tr><th>维度</th><th>权重</th>' +
            "".join("<th>{}</th>".format(n) for n in names) + "</tr></thead><tbody>"]
    for k, w in CONFIG["weights"].items():
        cells = []
        vals = [s[-1]["sub"].get(k) for _, s in all_series]
        mx = max([v for v in vals if v is not None] or [0])
        for v in vals:
            cls = "best" if (v is not None and v == mx) else ""
            cells.append('<td class="{}">{}</td>'.format(cls, "—" if v is None else num(v, 1)))
        comp.append('<tr><td>{}</td><td>{}%</td>{}</tr>'.format(DIM_LABEL[k], w, "".join(cells)))
    comp.append('<tr class="total"><td>综合评分</td><td>100%</td>' +
                "".join('<td>{}</td>'.format(num(s[-1]["score"], 1)) for _, s in all_series) +
                "</tr></tbody></table>")

    # 近 30 日评分明细
    dates = sorted({r["date"] for r in scores_rows})[-30:]
    hist = ['<table class="tbl small"><thead><tr><th>交易日</th>' +
            "".join("<th>{}</th>".format(n) for n in names) + "</tr></thead><tbody>"]
    for d in reversed(dates):
        cells = []
        for etf, _ in all_series:
            v = next((r for r in scores_rows if r["date"] == d and r["code"] == etf["code"]), None)
            cells.append("<td>{}</td>".format("—" if not v else "{:.1f} ({})".format(float(v["score"]), v["rating"])))
        hist.append("<tr><td>{}</td>{}</tr>".format(d, "".join(cells)))
    hist.append("</tbody></table>")

    logs = sorted([f[:-3] for f in os.listdir(os.path.join(ROOT, "logs")) if f.endswith(".md")])
    logs = [l for l in logs if l[:4].isdigit()][-12:][::-1]
    log_items = "".join('<li><a href="https://github.com/{}/blob/main/logs/{}.md">{} 日志</a></li>'.format(repo_slug, l, l) for l in logs)

    html = DASH_TEMPLATE \
        .replace("{{DATA_DATE}}", data_date) \
        .replace("{{RUN_AT}}", run_at) \
        .replace("{{CARDS}}", "".join(cards)) \
        .replace("{{COMP}}", "".join(comp)) \
        .replace("{{HIST}}", "".join(hist)) \
        .replace("{{LOGS}}", log_items) \
        .replace("{{REPO}}", repo_slug)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    return path


DASH_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>红利低波 ETF 每日评分 · {{DATA_DATE}}</title>
<style>
:root{--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--bg:#f7f6f2;--teal:#0f766e;--amber:#b45309}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 48px}
h1{font-size:24px;margin:0 0 6px;letter-spacing:.5px}
.meta{color:var(--muted);font-size:13px;margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin:22px 0}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px 18px 8px;box-shadow:0 1px 2px rgba(15,23,42,.04)}
.card header{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.card h2{font-size:17px;margin:0}
.card .sub{color:var(--muted);font-size:12px;margin:2px 0 0}
.badge{font-size:12px;padding:3px 10px;border-radius:999px;white-space:nowrap}
.s5{background:#dcfce7;color:#166534}.s4{background:#e0f2f1;color:#115e59}
.s3{background:#f1f5f9;color:#475569}.s2{background:#fef3c7;color:#92400e}
.s1{background:#fee2e2;color:#991b1b}
.score-row{display:flex;align-items:flex-end;gap:14px;margin:14px 0 2px}
.score{font-size:46px;font-weight:700;color:var(--teal);line-height:1}
.score-meta{display:flex;flex-direction:column;font-size:12px;color:var(--muted);gap:2px}
.delta{color:var(--ink);font-weight:600}
.spark-wrap{margin:8px 0 0}
.spark-cap{font-size:11px;color:var(--muted);margin:2px 0 6px}
.spark{display:block}
.action{font-size:13px;color:#334155;background:#f8fafc;border-left:3px solid var(--teal);padding:8px 10px;border-radius:6px;margin:6px 0 12px}
.dims{display:grid;grid-template-columns:1fr 1fr;gap:8px 18px;padding-bottom:12px}
.dim-h{display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}
.dim-v{color:var(--ink);font-weight:600}
.dim-bar{height:6px;background:#eef2f6;border-radius:4px;overflow:hidden}
.dim-bar i{display:block;height:100%;background:linear-gradient(90deg,#0f766e,#34a89b)}
h3{font-size:15px;margin:26px 0 10px}
.tbl{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:13px}
.tbl th,.tbl td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:center}
.tbl th{background:#f8fafc;font-weight:600;color:#334155}
.tbl td:first-child,.tbl th:first-child{text-align:left}
.tbl .best{color:var(--teal);font-weight:700}
.tbl .total td{font-weight:700;background:#f8fafc}
.tbl.small td,.tbl.small th{padding:6px 10px}
.logs{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 18px}
.logs ul{margin:6px 0;padding-left:18px;columns:2;font-size:13px}
.logs a{color:var(--teal);text-decoration:none}
.note{color:var(--muted);font-size:12px;margin-top:26px;border-top:1px solid var(--line);padding-top:14px}
</style>
</head>
<body>
<div class="wrap">
<h1>红利低波 ETF 每日监测评分</h1>
<p class="meta">数据日期 {{DATA_DATE}} · 生成时间 {{RUN_AT}}（北京时间） · 每交易日 08:20 自动更新</p>
<div class="cards">{{CARDS}}</div>
<h3>维度横向对比</h3>
{{COMP}}
<h3>近 30 个交易日评分</h3>
{{HIST}}
<h3>日志存档</h3>
<div class="logs"><ul>{{LOGS}}</ul></div>
<p class="note">模型：趋势 25% · 动量 20% · 波动 20% · 回撤 15% · 估值 10% · 红利 5% · 交易质量 5%。数据来自东方财富公开接口（日线前复权、单位净值、每份分红）与沪深300 指数；缺失维度按剩余权重归一化。评分是相对强弱排序工具，不构成投资建议。源码与逐日日志：<a href="https://github.com/{{REPO}}">github.com/{{REPO}}</a></p>
</div>
</body>
</html>
"""


def main():
    global CONFIG
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="仅用本地缓存数据计算（调试用）")
    args = ap.parse_args()

    with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as fh:
        CONFIG = json.load(fh)

    now = datetime.now(CST)
    run_at = now.strftime("%Y-%m-%d %H:%M")
    errors = []

    bench_name, bench_bars = fetch_kline(CONFIG["benchmark"]["secid"])
    all_series = []
    for etf in CONFIG["etfs"]:
        try:
            series = compute_etf(etf, bench_bars, offline=args.offline)
            all_series.append((etf, series))
            print("[OK] {} {} 最新 {} 评分 {}".format(
                etf["code"], etf["short"], series[-1]["date"], num(series[-1]["score"], 1)), flush=True)
        except Exception as exc:
            errors.append("{} {} 抓取或计算失败：{}".format(etf["code"], etf["short"], exc))
            print("[ERR] {} {}".format(etf["code"], exc), file=sys.stderr, flush=True)

    if not all_series:
        print("全部标的失败，终止", file=sys.stderr)
        return 1

    data_date = max(s[-1]["date"] for _, s in all_series)
    if data_date != now.strftime("%Y-%m-%d"):
        errors.append("最新交易日为 {}，与运行日 {} 不一致（可能为非交易日或数据延迟）".format(
            data_date, now.strftime("%Y-%m-%d")))

    rows = write_scores_csv(all_series)
    write_snapshot(all_series, data_date, run_at)
    write_log(all_series, data_date, run_at, errors)
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "xhxie123-maker/dividend-lowvol-score")
    write_dashboard(all_series, data_date, run_at, repo_slug, rows)

    run_log = os.path.join(ROOT, "data", "run_history.jsonl")
    os.makedirs(os.path.dirname(run_log), exist_ok=True)
    with open(run_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "run_at": run_at, "data_date": data_date,
            "benchmark": bench_name,
            "etfs": [{"code": e["code"], "score": round(s[-1]["score"], 2)} for e, s in all_series],
            "errors": errors,
        }, ensure_ascii=False) + "\n")

    print("完成：数据日期 {}，标的 {} 只，日志 logs/{}.md".format(data_date, len(all_series), data_date), flush=True)
    return 0


if __name__ == "__main__":
    CONFIG = {}
    sys.exit(main())
