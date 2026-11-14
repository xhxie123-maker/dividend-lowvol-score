#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
红利低波 ETF 监测评分系统（六维模型版）
==========================================

评分体系严格按六维模型实现：
  股息吸引力 20 / 分红质量与可持续性 20 / 低波动与风险控制 20
  估值合理性 15 / 盈利与财务质量 15 / 市场环境与资金趋势 10

标准化方式：百分位排名（Percentile Rank），窗口内比较，非绝对分数。
评级带：85-100 非常有吸引力 / 70-84 较有吸引力 / 60-69 中性偏积极 /
        40-59 中性偏谨慎 / 0-39 吸引力较低。
另设独立风险预警系统，不并入综合分。

数据来源（公开免费接口）
  日线前复权/不复权：push2his.eastmoney.com K 线接口
  ETF 单位净值与每份分红：api.fund.eastmoney.com/f10/lsjz
  ETF 份额：push2.eastmoney.com 行情接口（用于资金流向，自首次运行起累积）
  基准：沪深300（000300）

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
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"

_FORCE_CURL = False


# ---------------------------------------------------------------------------
# 网络层
# ---------------------------------------------------------------------------
def _curl_get(url, headers=None, timeout=25):
    cmd = ["curl", "-sS", "-L", "--max-time", str(timeout), "-A", UA]
    for k, v in (headers or {}).items():
        cmd += ["-H", "{}: {}".format(k, v)]
    cmd.append(url)
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError("curl 失败 rc={} {}".format(res.returncode, (res.stderr or "")[:200]))
    return res.stdout


def http_get(url, params=None, headers=None, timeout=25, tries=3):
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


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def stdev(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1))


def pct_rank(series, i, window, direction=1, min_obs=30):
    """窗口内百分位排名，0-100。direction=-1 表示数值越低得分越高。"""
    cur = series[i]
    if cur is None:
        return None
    seg = [v for v in series[max(0, i - window + 1):i + 1] if v is not None]
    if len(seg) < min_obs:
        return None
    below = sum(1 for v in seg if v <= cur)
    p = below / float(len(seg)) * 100.0
    return p if direction > 0 else 100.0 - p


def pct(x, nd=2):
    return "—" if x is None else "{:.{}f}%".format(x * 100, nd)


def num(x, nd=2):
    return "—" if x is None else "{:.{}f}".format(x, nd)


def md(d, days):
    return (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=days)).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 抓数
# ---------------------------------------------------------------------------
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
        bars.append({"date": f[0], "open": float(f[1]), "close": float(f[2]),
                     "high": float(f[3]), "low": float(f[4]),
                     "volume": float(f[5]), "amount": float(f[6])})
    if not bars:
        raise RuntimeError("K 线为空：{}".format(secid))
    return data.get("name") or secid, bars


def fetch_shares(secid):
    """ETF 总份额（份）。用于资金流向指标，自首次运行起逐日累积。"""
    fields = "f84,f85,f116"
    js = json.loads(http_get(QUOTE_URL, {"secid": secid, "fields": fields}))
    d = js.get("data") or {}
    v = d.get("f84") or d.get("f85")
    try:
        return float(v) if v not in (None, "-", 0) else None
    except (TypeError, ValueError):
        return None


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
                "fhfcz": float(row["fhfcz"]) if row["fhfcz"] else 0.0,
            }
    return rows


def save_nav_cache(code, rows):
    path = nav_cache_path(code)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "dwjz", "fhfcz"])
        for k in sorted(rows):
            r = rows[k]
            w.writerow([k, "" if r["dwjz"] is None else r["dwjz"], r["fhfcz"]])


def fetch_nav_page(code, page):
    raw = http_get(NAV_URL, {"fundCode": code, "pageIndex": page, "pageSize": 20},
                   {"Referer": "https://fundf10.eastmoney.com/"})
    block = (json.loads(raw).get("Data") or {})
    out = []
    for item in block.get("LSJZList") or []:
        d = (item.get("FSRQ") or "").strip()
        if d:
            out.append((d, {"dwjz": float(item["DWJZ"]) if item.get("DWJZ") else None,
                            "fhfcz": float(item["FHFCZ"]) if item.get("FHFCZ") else 0.0}))
    return out


def update_nav_cache(code, need_from, offline=False):
    rows = load_nav_cache(code)
    if offline and rows:
        return rows
    page = 1
    while page <= 120:
        batch = fetch_nav_page(code, page)
        if not batch:
            break
        for d, v in batch:
            rows.setdefault(d, v)
        earliest = min(rows)
        if earliest <= need_from or len(batch) < 20:
            break
        page += 1
        time.sleep(0.15)
    save_nav_cache(code, rows)
    return rows


def shares_cache_path(code):
    return os.path.join(ROOT, "data", "cache", "shares_{}.csv".format(code))


def update_shares_cache(code, secid, date):
    path = shares_cache_path(code)
    rows = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                rows[row["date"]] = float(row["shares"]) if row["shares"] else None
    try:
        v = fetch_shares(secid)
    except Exception:
        v = None
    if v:
        rows[date] = v
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "shares"])
        for k in sorted(rows):
            w.writerow([k, "" if rows[k] is None else rows[k]])
    return rows


# ---------------------------------------------------------------------------
# 序列构建
# ---------------------------------------------------------------------------
def rolling_vol(closes, i, k):
    if i < k:
        return None
    seg = [math.log(closes[j] / closes[j - 1])
           for j in range(i - k + 1, i + 1) if closes[j - 1] > 0]
    sd = stdev(seg)
    return None if sd is None else sd * math.sqrt(252)


def rolling_downside(closes, i, k):
    if i < k:
        return None
    seg = [math.log(closes[j] / closes[j - 1]) for j in range(i - k + 1, i + 1)
           if closes[j - 1] > 0 and closes[j] / closes[j - 1] < 1.0]
    sd = stdev(seg)
    return None if (sd is None or len(seg) < 10) else sd * math.sqrt(252)


def rolling_mdd(closes, i, k):
    if i < k - 1:
        return None
    window = closes[i - k + 1:i + 1]
    peak, mdd = window[0], 0.0
    for c in window:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1.0)
    return mdd


def build_series(etf, bars, raw_bars, nav, shares, bond10y):
    dates = [b["date"] for b in bars]
    closes = [b["close"] for b in bars]
    amounts = [b["amount"] for b in bars]
    raw_close = {b["date"]: b["close"] for b in raw_bars}
    nav_dates = sorted(nav)
    yearly_div = {}
    for d, v in nav.items():
        if v["fhfcz"]:
            yearly_div.setdefault(d[:4], 0.0)
            yearly_div[d[:4]] += v["fhfcz"]

    s = []
    for i, d in enumerate(dates):
        price = raw_close.get(d, closes[i])
        row = {"date": d, "close": closes[i], "close_raw": price,
               "amount": amounts[i]}
        j = bisect.bisect_right(nav_dates, d) - 1
        row["nav"] = nav[nav_dates[j]]["dwjz"] if j >= 0 else None
        row["nav_date"] = nav_dates[j] if j >= 0 else None
        row["premium"] = (price / row["nav"] - 1.0) if row["nav"] else None
        start = md(d, -365)
        ttm = None
        if nav_dates and nav_dates[0] <= start:
            ttm = sum(v["fhfcz"] for k, v in nav.items() if start < k <= d and v["fhfcz"])
        row["ttm_div"] = ttm
        row["ttm_yield"] = (ttm / price) if (ttm is not None and price) else None
        row["vol20"] = rolling_vol(closes, i, 20)
        row["vol60"] = rolling_vol(closes, i, 60)
        row["vol120"] = rolling_vol(closes, i, 120)
        row["vol250"] = rolling_vol(closes, i, 250)
        row["downside250"] = rolling_downside(closes, i, 250)
        row["mdd250"] = rolling_mdd(closes, i, 250)
        row["dd250"] = (closes[i] / max(closes[max(0, i - 249):i + 1]) - 1.0)
        row["r20"] = closes[i] / closes[i - 20] - 1.0 if i >= 20 else None
        row["r60"] = closes[i] / closes[i - 60] - 1.0 if i >= 60 else None
        row["amount20"] = (sum(amounts[i - 19:i + 1]) / 20.0) if i >= 20 else None
        sv = shares.get(d)
        row["shares"] = sv
        row["flow20"] = None
        if sv is not None and i >= 20 and shares.get(dates[i - 20]):
            base = shares[dates[i - 20]]
            row["flow20"] = sv / base - 1.0 if base else None
        row["bond10y"] = bond10y
        s.append(row)

    y = [r["ttm_yield"] for r in s]
    for i, r in enumerate(s):
        seg = [v for v in y[max(0, i - 749):i + 1] if v is not None]
        r["yield_avg3"] = (sum(seg) / len(seg)) if len(seg) >= 120 else None
        r["yield_pct5"] = pct_rank(y, i, CONFIG["windows"]["percentile_window"], 1, 120)
        r["consec_years"] = consec_div_years(yearly_div, r["date"])
        r["dps_growth"] = None
        r["div_cv"] = None

    ttm = [r["ttm_div"] for r in s]
    cur_year = s[-1]["date"][:4] if s else ""
    full_years = sorted(y for y in yearly_div if y < cur_year)
    for i, r in enumerate(s):
        base = None
        for b in range(max(0, i - 750), i):
            if ttm[b] is not None:
                base = b
                break
        if base is not None:
            span = (i - base) / 250.0
            if ttm[i] and ttm[base] > 0 and span >= 0.8:
                r["dps_growth"] = (ttm[i] / ttm[base]) ** (1.0 / span) - 1.0
        if len(full_years) >= 3:
            vals = [yearly_div[y] for y in full_years[-5:]]
            mu = sum(vals) / len(vals)
            sd = stdev(vals)
            if mu > 0 and sd is not None:
                r["div_cv"] = sd / mu
        else:
            seg = [v for v in ttm[max(0, i - 749):i + 1] if v is not None]
            if len(seg) >= 250:
                mu = sum(seg) / len(seg)
                sd = stdev(seg)
                if mu > 0 and sd is not None:
                    r["div_cv"] = sd / mu

    bmap = {b["date"]: b["close"] for b in BENCH_BARS}
    bdates = sorted(bmap)
    for i, r in enumerate(s):
        k2 = bisect.bisect_right(bdates, r["date"]) - 1
        def br(k):
            if k2 < 0 or k2 - k < 0:
                return None
            return bmap[bdates[k2]] / bmap[bdates[k2 - k]] - 1.0
        b20, b60 = br(20), br(60)
        r["excess20"] = None if (r["r20"] is None or b20 is None) else r["r20"] - b20
        r["excess60"] = None if (r["r60"] is None or b60 is None) else r["r60"] - b60
    return s


def consec_div_years(yearly_div, date):
    """从最近一个有分红的年度往前数连续年度数。"""
    years = sorted(yearly_div)
    if not years:
        return 0
    last = years[-1]
    n, cur = 1, last
    while str(int(cur) - 1) in yearly_div and int(cur) - 1 >= 1990:
        cur = str(int(cur) - 1)
        n += 1
    return n


# ---------------------------------------------------------------------------
# 六维评分
# ---------------------------------------------------------------------------
DIMENSIONS = [
    ("div_attraction", "股息吸引力", 20, [
        ("cur_yield", "当前股息率", 6, "live"),
        ("avg3_yield", "3 年平均股息率", 5, "live"),
        ("yield_pct5", "股息率历史分位数", 4, "live"),
        ("spread", "股债收益差", 3, "missing"),
        ("fwd_yield", "预期股息率", 2, "missing"),
    ]),
    ("div_quality", "分红质量与可持续性", 20, [
        ("consec_years", "连续现金分红年限", 4, "live"),
        ("payout", "现金分红支付率", 4, "missing"),
        ("fcf_cover", "自由现金流覆盖率", 5, "missing"),
        ("dps_growth", "每股股息增长率", 4, "live"),
        ("stability", "分红稳定性", 3, "live"),
    ]),
    ("low_risk", "低波动与风险控制", 20, [
        ("vol20", "20 日年化波动率", 3, "live"),
        ("vol60", "60 日年化波动率", 4, "live"),
        ("vol120", "120 日年化波动率", 4, "live"),
        ("vol250", "250 日年化波动率", 3, "live"),
        ("mdd250", "最大回撤", 4, "live"),
        ("downside", "下行波动率", 2, "live"),
    ]),
    ("valuation", "估值合理性", 15, [
        ("pe_pct", "PE 历史分位数", 4, "proxy"),
        ("pb_pct", "PB 历史分位数", 4, "missing"),
        ("yield_pct_val", "股息率历史分位数", 4, "live"),
        ("erp", "股权风险溢价", 3, "missing"),
    ]),
    ("financial", "盈利与财务质量", 15, [
        ("roe", "ROE", 3, "missing"),
        ("roic", "ROIC", 3, "missing"),
        ("ocf_np", "经营现金流/净利润", 3, "missing"),
        ("debt_ratio", "资产负债率", 2, "missing"),
        ("np_stability", "净利润增长稳定性", 2, "missing"),
        ("interest_cover", "利息保障倍数", 2, "missing"),
    ]),
    ("market_env", "市场环境与资金趋势", 10, [
        ("bond10y", "10 年期国债收益率", 2, "missing"),
        ("rs", "红利低波相对强弱", 2, "live"),
        ("flow", "ETF 资金流向", 2, "missing"),
        ("mkt_vol", "市场整体波动率", 2, "live"),
        ("crowding", "红利风格拥挤度", 2, "missing"),
    ]),
]

STATUS_LABEL = {"live": "已实现", "proxy": "代理", "missing": "待接入"}


def score_series(series, bench_closes):
    win = CONFIG["windows"]["percentile_window"]
    short = CONFIG["windows"]["short_window"]

    cur_yield = [r["ttm_yield"] for r in series]
    avg3 = [r["yield_avg3"] for r in series]
    yp5 = [r["yield_pct5"] for r in series]
    pe_proxy = [r["close_raw"] for r in series]
    vol20 = [r["vol20"] for r in series]
    vol60 = [r["vol60"] for r in series]
    vol120 = [r["vol120"] for r in series]
    vol250 = [r["vol250"] for r in series]
    mdd = [r["mdd250"] for r in series]
    dsw = [r["downside250"] for r in series]
    rs_series = [None if (r["excess20"] is None or r["excess60"] is None)
                 else (r["excess20"] + r["excess60"]) / 2.0 for r in series]
    flow_series = [r["flow20"] for r in series]

    mvol = [rolling_vol(bench_closes, i, 20) for i in range(len(bench_closes))]
    bdate = list(BENCH_DATES)
    for r in series:
        k2 = bisect.bisect_right(bdate, r["date"]) - 1
        r["mkt_vol"] = mvol[k2] if k2 >= 0 else None

    mvol_series = [r["mkt_vol"] for r in series]

    for i, r in enumerate(series):
        sub = {}
        sub["cur_yield"] = pct_rank(cur_yield, i, short, 1, 60)
        sub["avg3_yield"] = pct_rank(avg3, i, win, 1, 120)
        sub["yield_pct5"] = None if yp5[i] is None else yp5[i]
        sub["consec_years"] = tier_years(r["consec_years"])
        sub["dps_growth"] = growth_score(r["dps_growth"])
        sub["stability"] = None if r["div_cv"] is None else 100.0 * clamp(1.0 - r["div_cv"] / 0.30, 0, 1)
        sub["vol20"] = pct_rank(vol20, i, win, -1, 120)
        sub["vol60"] = pct_rank(vol60, i, win, -1, 120)
        sub["vol120"] = pct_rank(vol120, i, win, -1, 120)
        sub["vol250"] = pct_rank(vol250, i, win, -1, 120)
        sub["mdd250"] = pct_rank(mdd, i, win, 1, 120)
        sub["downside"] = pct_rank(dsw, i, win, -1, 120)
        sub["pe_pct"] = pct_rank(pe_proxy, i, win, -1, 120)
        sub["yield_pct_val"] = sub["yield_pct5"]
        sub["rs"] = pct_rank(rs_series, i, win, 1, 120)
        sub["flow"] = pct_rank(flow_series, i, win, 1, 60)
        sub["mkt_vol"] = pct_rank(mvol_series, i, win, -1, 120)

        dim_scores, dim_cov, missing = {}, {}, []
        for key, name, dweight, subs in DIMENSIONS:
            tot_w, acc = 0.0, 0.0
            for skeys, sname, sw, status in subs:
                v = sub.get(skeys)
                if v is None:
                    missing.append("{}_{}".format(key, skeys))
                else:
                    acc += sw * v
                    tot_w += sw
            dim_cov[key] = tot_w / float(sum(x[2] for x in subs))
            dim_scores[key] = None if tot_w == 0 else acc / tot_w
        total_w, total_acc = 0.0, 0.0
        for key, name, dweight, subs in DIMENSIONS:
            if dim_scores[key] is not None:
                total_acc += dweight * dim_scores[key]
                total_w += dweight
        r["sub"] = sub
        r["dim"] = dim_scores
        r["dim_cov"] = dim_cov
        r["score"] = None if total_w == 0 else total_acc / total_w
        r["coverage"] = total_w / 100.0
        r["missing"] = missing
    return series


def tier_years(n):
    if n is None:
        return None
    if n >= 10:
        return 100.0
    if n >= 5:
        return 85.0
    if n >= 3:
        return 70.0
    if n >= 2:
        return 45.0
    if n >= 1:
        return 25.0
    return 0.0


def growth_score(g):
    if g is None:
        return None
    return 50.0 + 50.0 * clamp(g / 0.08, -1.0, 1.0)


def rating_of(score):
    for band in CONFIG["rating_bands"]:
        if score >= band["min"]:
            return band
    return CONFIG["rating_bands"][-1]


# ---------------------------------------------------------------------------
# 风险预警（独立于综合分）
# ---------------------------------------------------------------------------
def risk_alerts(series):
    r = series[-1]
    out = []

    def add(level, title, detail):
        out.append({"level": level, "title": title, "detail": detail})

    y = [x["ttm_yield"] for x in series[-250:] if x["ttm_yield"] is not None]
    if len(y) >= 60 and r["ttm_yield"] is not None:
        mu = sum(y) / len(y)
        sd = stdev(y)
        if sd and (r["ttm_yield"] - mu) / sd > 2.0:
            add("高", "股息率异常升高", "当前 TTM 股息率 {} 偏离 250 日均值 {} 达 {:.1f} 个标准差，需排查价格下跌或预期削减分红".format(
                pct(r["ttm_yield"]), pct(mu), (r["ttm_yield"] - mu) / sd))

    if r["vol20"] is not None and r["vol250"]:
        ratio = r["vol20"] / r["vol250"]
        if ratio > 1.5:
            add("高", "波动率快速上升", "20 日年化波动 {} 为 250 日 {} 的 {:.2f} 倍，超过 1.5 倍预警线".format(
                pct(r["vol20"]), pct(r["vol250"]), ratio))

    if r["dd250"] is not None and r["dd250"] < -0.15:
        add("高", "最大回撤恶化", "距 250 日高点回撤 {}，超过 -15% 预警线".format(pct(r["dd250"])))
    elif r["mdd250"] is not None and r["mdd250"] < -0.10:
        add("中", "最大回撤恶化", "250 日区间最大回撤 {}，超过 -10% 观察线".format(pct(r["mdd250"])))

    if r["premium"] is not None and abs(r["premium"]) > 0.01:
        add("中", "ETF 折溢价异常", "当日折溢价 {}，超过 ±1% 阈值，溢价买入将承担回归成本".format(pct(r["premium"])))

    if r["consec_years"] and r["dps_growth"] is not None and r["dps_growth"] < -0.20:
        add("中", "分红政策变化", "近 3 年每份分红年化 {}，降幅超过 20%，需核对分红公告".format(pct(r["dps_growth"])))

    if r["consec_years"] == 0:
        add("中", "分红连续性中断", "当前可得区间内未记录到现金分红")

    if not out:
        add("低", "无触发项", "股息率、波动率、回撤、折溢价、分红增长均未触及预警阈值")
    return out


# ---------------------------------------------------------------------------
# 产物
# ---------------------------------------------------------------------------
FIELDS = ["date", "code", "short", "close", "score", "rating", "coverage",
          "div_attraction", "div_quality", "low_risk", "valuation", "financial", "market_env",
          "ttm_yield", "yield_avg3", "yield_pct5", "consec_years", "dps_growth", "div_cv",
          "vol20", "vol60", "vol120", "vol250", "mdd250", "dd250", "downside250",
          "premium", "amount20_yi", "flow20", "mkt_vol", "r20", "r60"]


def row_to_dict(etf, r):
    d = r["dim"]
    return {
        "date": r["date"], "code": etf["code"], "short": etf["short"],
        "close": round(r["close"], 3),
        "score": None if r["score"] is None else round(r["score"], 2),
        "rating": rating_of(r["score"])["label"] if r["score"] is not None else "—",
        "coverage": round(r["coverage"], 3),
        "div_attraction": None if d["div_attraction"] is None else round(d["div_attraction"], 2),
        "div_quality": None if d["div_quality"] is None else round(d["div_quality"], 2),
        "low_risk": None if d["low_risk"] is None else round(d["low_risk"], 2),
        "valuation": None if d["valuation"] is None else round(d["valuation"], 2),
        "financial": None if d["financial"] is None else round(d["financial"], 2),
        "market_env": None if d["market_env"] is None else round(d["market_env"], 2),
        "ttm_yield": None if r["ttm_yield"] is None else round(r["ttm_yield"], 5),
        "yield_avg3": None if r["yield_avg3"] is None else round(r["yield_avg3"], 5),
        "yield_pct5": None if r["yield_pct5"] is None else round(r["yield_pct5"], 1),
        "consec_years": r["consec_years"],
        "dps_growth": None if r["dps_growth"] is None else round(r["dps_growth"], 5),
        "div_cv": None if r["div_cv"] is None else round(r["div_cv"], 5),
        "vol20": None if r["vol20"] is None else round(r["vol20"], 5),
        "vol60": None if r["vol60"] is None else round(r["vol60"], 5),
        "vol120": None if r["vol120"] is None else round(r["vol120"], 5),
        "vol250": None if r["vol250"] is None else round(r["vol250"], 5),
        "mdd250": None if r["mdd250"] is None else round(r["mdd250"], 5),
        "dd250": None if r["dd250"] is None else round(r["dd250"], 5),
        "downside250": None if r["downside250"] is None else round(r["downside250"], 5),
        "premium": None if r["premium"] is None else round(r["premium"], 5),
        "amount20_yi": None if r["amount20"] is None else round(r["amount20"] / 1e8, 3),
        "flow20": None if r["flow20"] is None else round(r["flow20"], 5),
        "mkt_vol": None if r["mkt_vol"] is None else round(r["mkt_vol"], 5),
        "r20": None if r["r20"] is None else round(r["r20"], 5),
        "r60": None if r["r60"] is None else round(r["r60"], 5),
    }


def write_scores_csv(all_series):
    path = os.path.join(ROOT, "data", "daily_scores.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = []
    for etf, series in all_series:
        for r in series[-CONFIG["windows"]["backfill_days"]:]:
            if r["score"] is not None:
                fresh.append((r["date"], etf["code"], row_to_dict(etf, r)))
    old = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            old = list(csv.DictReader(fh))
    if fresh:
        cutoff = min(x[0] for x in fresh)
        old = [r for r in old if r["date"] < cutoff]
    merged = list(old)
    for _, _, row in fresh:
        merged.append({k: ("" if v is None else v) for k, v in row.items()})
    merged.sort(key=lambda r: (r["date"], r["code"]))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in merged:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return merged


def write_snapshot(all_payload, data_date, run_at):
    path = os.path.join(ROOT, "data", "history", "{}.json".format(data_date))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"run_at": run_at, "data_date": data_date, "etfs": all_payload},
                  fh, ensure_ascii=False, indent=2, default=str)
    return path


DIM_NAME = {k: n for k, n, w, subs in DIMENSIONS}


def write_log(all_series, alerts, data_date, run_at, errors):
    path = os.path.join(ROOT, "logs", "{}.md".format(data_date))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    L = []
    add = L.append
    add("# 红利低波 ETF 监测评分 · {}".format(data_date))
    add("")
    add("- 生成时间：{}（北京时间）".format(run_at))
    add("- 数据日期：{}（最新交易日收盘）".format(data_date))
    add("- 监测标的：{}".format("、".join("{} {}".format(e["short"], e["code"]) for e, _ in all_series)))
    add("- 评分体系：六维 100 分（股息吸引力 20 / 分红质量 20 / 低波动 20 / 估值 15 / 财务质量 15 / 市场环境 10）")
    add("")
    add("## 一、评分总览")
    add("")
    add("| 标的 | 代码 | 收盘价 | 综合评分 | 评级 | 较前一日 | 数据覆盖率 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for etf, s in all_series:
        r = s[-1]
        prev = [x for x in s if x["score"] is not None][-2:]
        delta = None if len(prev) < 2 else prev[-1]["score"] - prev[-2]["score"]
        add("| {} | {} | {} | {} | {} | {} | {:.0f}% |".format(
            etf["short"], etf["code"], num(r["close"], 3), num(r["score"], 1),
            rating_of(r["score"])["label"],
            "—" if delta is None else "{:+.1f}".format(delta), r["coverage"] * 100))
    add("")
    add("评级口径：85–100 非常有吸引力 / 70–84 较有吸引力 / 60–69 中性偏积极 / 40–59 中性偏谨慎 / 0–39 吸引力较低。")
    add("")

    add("## 二、六维拆解")
    add("")
    for etf, s in all_series:
        r = s[-1]
        add("### {}（{}）".format(etf["short"], etf["code"]))
        add("")
        add("| 维度 | 维度权重 | 维度得分 | 维度覆盖率 | 子项（已实现 / 代理 / 待接入） |")
        add("| --- | --- | --- | --- | --- |")
        for key, name, dw, subs in DIMENSIONS:
            sub_txt = "；".join("{} {} {}分".format(sn, STATUS_LABEL[st], sw)
                               for _, sn, sw, st in subs)
            add("| {} | {} | {} | {:.0f}% | {} |".format(
                name, dw, num(r["dim"][key], 1), r["dim_cov"][key] * 100, sub_txt))
        add("")
        add("关键指标：")
        add("")
        add("- 不复权收盘价 {}，单位净值 {}（{}）".format(
            num(r["close_raw"], 3), num(r["nav"], 4), r["nav_date"] or "缺失"))
        add("- TTM 现金分红率 {}（每份 {} 元），3 年平均 {}，5 年分位 {}".format(
            pct(r["ttm_yield"]), num(r["ttm_div"], 4), pct(r["yield_avg3"]), num(r["yield_pct5"], 0)))
        add("- 连续分红年限 {} 年，近 3 年每份分红年化 {}，年度分红变异系数 {}".format(
            r["consec_years"], pct(r["dps_growth"]), pct(r["div_cv"])))
        add("- 年化波动：20 日 {} / 60 日 {} / 120 日 {} / 250 日 {}".format(
            pct(r["vol20"]), pct(r["vol60"]), pct(r["vol120"]), pct(r["vol250"])))
        add("- 250 日最大回撤 {}，距 250 日高点 {}，250 日下行波动 {}".format(
            pct(r["mdd250"]), pct(r["dd250"]), pct(r["downside250"])))
        add("- 折溢价 {}，20 日均成交额 {} 亿元，相对沪深300 20/60 日超额 {} / {}".format(
            pct(r["premium"]), num(r["amount20"] / 1e8 if r["amount20"] else None, 2),
            pct(r["excess20"]), pct(r["excess60"])))
        add("")
        add("子项标准化明细（百分位排名，窗口内 0–100）：")
        add("")
        add("| 维度 | 子项 | 分值 | 当前得分 |")
        add("| --- | --- | --- | --- |")
        for key, name, dw, subs in DIMENSIONS:
            for sk, sn, sw, st in subs:
                v = r["sub"].get(sk)
                add("| {} | {} | {} | {} |".format(name, sn, sw, "—" if v is None else num(v, 1)))
        add("")

    add("## 三、风险预警")
    add("")
    add("| 级别 | 项目 | 说明 |")
    add("| --- | --- | --- |")
    for a in alerts:
        add("| {} | {} | {} |".format(a["level"], a["title"], a["detail"]))
    add("")
    add("预警项独立于综合分：综合分高不等于无风险，出现高等级预警时应单独立项核查。")
    add("")

    add("## 四、横向对比")
    add("")
    ranked = sorted([(s[-1]["score"], e, s) for e, s in all_series], key=lambda t: t[0], reverse=True)
    add("- 评分排序：{}（{}） > {}（{}）".format(
        ranked[0][1]["short"], num(ranked[0][0], 1), ranked[1][1]["short"], num(ranked[1][0], 1)))
    diff = ranked[0][0] - ranked[1][0]
    add("- 分差 {} 分。{}".format(
        num(diff, 1),
        "差距超过 5 分，月度级别上更偏向高评分标的。" if diff >= 5
        else "差距在 5 分以内，评分层面不构成明显优劣，费率差异（563020 综合 0.20% vs 515450 0.60%）值得纳入考量。"))
    add("")
    add("| 维度 | {} | {} | 差值 |".format(ranked[0][1]["short"], ranked[1][1]["short"]))
    add("| --- | --- | --- | --- |")
    for key, name, dw, subs in DIMENSIONS:
        a = ranked[0][2][-1]["dim"][key]
        b = ranked[1][2][-1]["dim"][key]
        add("| {} | {} | {} | {} |".format(
            name, num(a, 1), num(b, 1), "—" if (a is None or b is None) else "{:+.1f}".format(a - b)))
    add("")

    add("## 五、指标覆盖与数据缺口")
    add("")
    for key, name, dw, subs in DIMENSIONS:
        gaps = [sn for _, sn, sw, st in subs if st == "missing"]
        proxies = [sn for _, sn, sw, st in subs if st == "proxy"]
        if gaps or proxies:
            add("- **{}**：".format(name))
            if gaps:
                add("  - 待接入：{}".format("、".join(gaps)))
            if proxies:
                add("  - 代理实现：{}".format("、".join(proxies)))
    add("- 待接入项的共同原因：需成分股财报（支付率、FCF 覆盖、ROE/ROIC、负债率、利息保障）、PB 估值、中债 10 年期收益率、ETF 份额历史（资金流向）等上游数据；当前公开免费接口不提供或需付费授权。")
    add("- 缺失子项在维度内按剩余子项权重重归一化，因此跨日比较时请关注覆盖率。")
    add("")
    if errors:
        add("## 六、数据异常")
        add("")
        for e in errors:
            add("- {}".format(e))
        add("")
    add("## 七、口径与免责")
    add("")
    add("- 评分衡量的是红利低波资产的配置吸引力，是相对排序工具，不是买卖信号。")
    add("- 全部子项用百分位排名标准化（窗口内比较），不使用固定绝对阈值，避免量纲差异与绝对分数失真。")
    add("- 折溢价与股息率均以不复权收盘价为分母，避免 ETF 分红造成的前复权错位。")
    add("- 分红数据区分已实施分红（每份分红金额），不含已公告待实施与预测分红。")
    add("- 历史分位窗口受上市时间限制：563020 于 2023-12 上市，窗口不足 5 年时按实际可得区间计算。")
    add("- TTM 分红为滚动 12 个月已实施分红合计，会在分红到账日阶跃，股息吸引力与分红质量维度当日出现跳变属口径特性，不是数据错误。")
    add("")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L))
    return path


def sparkline(values, width=250, height=58, color="#0f766e"):
    if len(values) < 2:
        return ""
    n = len(values)
    lo, hi = min(values), max(values)
    pad = max(2.0, (hi - lo) * 0.15)
    d0, d1 = lo - pad, hi + pad
    span = (d1 - d0) or 1.0

    def y(v):
        return height - 6 - ((clamp(v, d0, d1) - d0) / span) * (height - 12)

    step = width / float(n - 1)
    pts = [(i * step, y(v)) for i, v in enumerate(values)]
    poly = " ".join("{:.1f},{:.1f}".format(a, b) for a, b in pts)
    area = "{:.1f},{:.1f} ".format(pts[0][0], height) + poly + \
           " {:.1f},{:.1f}".format(pts[-1][0], height)
    mid = y(sum(values) / float(n))
    last = pts[-1]
    return ('<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none" class="spark">'
            '<line x1="0" y1="{mid:.1f}" x2="{w}" y2="{mid:.1f}" stroke="#94a3b8" stroke-width="1" stroke-dasharray="4 4" opacity="0.55"/>'
            '<polygon points="{area}" fill="{color}" opacity="0.10"/>'
            '<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2.4" stroke-linejoin="round"/>'
            '<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.2" fill="{color}"/></svg>').format(
        w=width, h=height, mid=mid, area=area, poly=poly, color=color, cx=last[0], cy=last[1])


def write_dashboard(all_series, alerts_map, data_date, run_at, repo_slug, scores_rows):
    path = os.path.join(ROOT, "docs", "index.html")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cards = []
    for etf, s in all_series:
        r = s[-1]
        rating = rating_of(r["score"])
        vals = [x["score"] for x in s if x["score"] is not None][-30:]
        prev = [x for x in s if x["score"] is not None][-2:]
        delta = None if len(prev) < 2 else prev[-1]["score"] - prev[-2]["score"]
        dims = []
        for key, name, dw, subs in DIMENSIONS:
            v = r["dim"][key]
            dims.append('<div class="dim"><div class="dim-h"><span>{n}<i>{w}分 · 覆盖 {c:.0f}%</i></span>'
                        '<span class="dim-v">{v}</span></div><div class="dim-bar"><i style="width:{p:.1f}%"></i></div></div>'.format(
                            n=name, w=dw, c=r["dim_cov"][key] * 100,
                            v="—" if v is None else num(v, 1), p=0 if v is None else v))
        al = alerts_map.get(etf["code"], [])
        al_html = "".join('<li class="al {cls}">{lv} · {t}</li>'.format(
            cls="hi" if a["level"] == "高" else ("mid" if a["level"] == "中" else "lo"),
            lv=a["level"], t=a["title"]) for a in al)
        cls = {"非常有吸引力": "s5", "较有吸引力": "s4", "中性偏积极": "s3",
               "中性偏谨慎": "s2", "吸引力较低": "s1"}.get(rating["label"], "s3")
        cards.append(
            '<section class="card"><header><div><h2>{short}</h2><p class="sub">{code} · {index}</p></div>'
            '<span class="badge {bc}">{lab}</span></header>'
            '<div class="score-row"><div class="score">{sc}</div><div class="score-meta">'
            '<span class="delta">较前一日 {d1}</span><span>收盘价 {close}</span>'
            '<span>数据覆盖率 {cov:.0f}%</span></div></div>'
            '<div class="spark-wrap">{spark}</div>'
            '<p class="spark-cap">近 {nd} 日评分区间 {lo} – {hi}（虚线为均值 {avg}）</p>'
            '<p class="action">建议动作：{act}</p>'
            '<div class="dims">{bars}</div>'
            '<ul class="alerts">{al}</ul></section>'.format(
                short=etf["short"], code=etf["code"], index=etf["index"],
                bc=cls, lab=rating["label"], sc=num(r["score"], 1),
                d1="—" if delta is None else "{:+.1f}".format(delta),
                close=num(r["close"], 3), cov=r["coverage"] * 100,
                spark=sparkline(vals), nd=len(vals),
                lo=num(min(vals), 1), hi=num(max(vals), 1),
                avg=num(sum(vals) / len(vals), 1), act=rating["action"],
                bars="".join(dims), al=al_html))
    names = [e["short"] for e, _ in all_series]
    comp = ['<table class="tbl"><thead><tr><th>维度</th><th>权重</th>' +
            "".join("<th>{}</th>".format(n) for n in names) + "</tr></thead><tbody>"]
    for key, name, dw, subs in DIMENSIONS:
        vals = [s[-1]["dim"][key] for _, s in all_series]
        mx = max([v for v in vals if v is not None] or [0])
        cells = ['<td class="{}">{}</td>'.format(
            "best" if (v is not None and v == mx) else "", "—" if v is None else num(v, 1)) for v in vals]
        comp.append('<tr><td>{}</td><td>{}分</td>{}</tr>'.format(name, dw, "".join(cells)))
    comp.append('<tr class="total"><td>综合评分</td><td>100分</td>' +
                "".join('<td>{}</td>'.format(num(s[-1]["score"], 1)) for _, s in all_series) +
                "</tr></tbody></table>")

    dates = sorted({r["date"] for r in scores_rows})[-30:]
    hist = ['<table class="tbl small"><thead><tr><th>交易日</th>' +
            "".join("<th>{}</th>".format(n) for n in names) + "</tr></thead><tbody>"]
    for d in reversed(dates):
        cells = []
        for etf, _ in all_series:
            v = next((x for x in scores_rows if x["date"] == d and x["code"] == etf["code"]), None)
            cells.append("<td>{}</td>".format("—" if not v else "{} ({})".format(v["score"], v["rating"])))
        hist.append("<tr><td>{}</td>{}</tr>".format(d, "".join(cells)))
    hist.append("</tbody></table>")

    logs = sorted(f[:-3] for f in os.listdir(os.path.join(ROOT, "logs"))
                  if f.endswith(".md") and f[:4].isdigit())[-12:][::-1]
    log_items = "".join('<li><a href="https://github.com/{}/blob/main/logs/{}.md">{} 日志</a></li>'.format(
        repo_slug, l, l) for l in logs)

    html = DASH_TEMPLATE.replace("{{DATA_DATE}}", data_date).replace("{{RUN_AT}}", run_at) \
        .replace("{{CARDS}}", "".join(cards)).replace("{{COMP}}", "".join(comp)) \
        .replace("{{HIST}}", "".join(hist)).replace("{{LOGS}}", log_items) \
        .replace("{{REPO}}", repo_slug)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    return path


DASH_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>红利低波 ETF 监测评分 · {{DATA_DATE}}</title>
<style>
:root{--ink:#0f172a;--muted:#64748b;--line:#e2e8f0;--bg:#f7f6f2;--teal:#0f766e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.6}
.wrap{max-width:1080px;margin:0 auto;padding:28px 20px 48px}
h1{font-size:24px;margin:0 0 6px}
.meta{color:var(--muted);font-size:13px;margin:0}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px;margin:22px 0}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:18px}
.card header{display:flex;justify-content:space-between;align-items:flex-start;gap:10px}
.card h2{font-size:17px;margin:0}
.card .sub{color:var(--muted);font-size:12px;margin:2px 0 0}
.badge{font-size:12px;padding:3px 10px;border-radius:999px;white-space:nowrap}
.s5{background:#dcfce7;color:#166534}.s4{background:#e0f2f1;color:#115e59}
.s3{background:#f1f5f9;color:#475569}.s2{background:#fef3c7;color:#92400e}
.s1{background:#fee2e2;color:#991b1b}
.score-row{display:flex;align-items:flex-end;gap:14px;margin:12px 0 2px}
.score{font-size:44px;font-weight:700;color:var(--teal);line-height:1}
.score-meta{display:flex;flex-direction:column;font-size:12px;color:var(--muted)}
.delta{color:var(--ink);font-weight:600}
.spark-wrap{margin:6px 0 0}
.spark-cap{font-size:11px;color:var(--muted);margin:2px 0 8px}
.action{font-size:13px;color:#334155;background:#f8fafc;border-left:3px solid var(--teal);padding:8px 10px;border-radius:6px;margin:6px 0 12px}
.dims{display:grid;grid-template-columns:1fr;gap:8px}
.dim-h{display:flex;justify-content:space-between;font-size:12px;color:var(--muted)}
.dim-h i{font-style:normal;font-size:11px;margin-left:6px;opacity:.75}
.dim-v{color:var(--ink);font-weight:600}
.dim-bar{height:6px;background:#eef2f6;border-radius:4px;overflow:hidden}
.dim-bar i{display:block;height:100%;background:linear-gradient(90deg,#0f766e,#34a89b)}
.alerts{list-style:none;margin:12px 0 0;padding:0;font-size:12px}
.al{color:#475569;padding:2px 0}
.al.mid{color:#92400e}.al.hi{color:#991b1b;font-weight:600}
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
<h1>红利低波 ETF 监测评分</h1>
<p class="meta">数据日期 {{DATA_DATE}} · 生成时间 {{RUN_AT}}（北京时间） · 每交易日 08:20 自动更新</p>
<div class="cards">{{CARDS}}</div>
<h3>六维横向对比</h3>
{{COMP}}
<h3>近 30 个交易日评分</h3>
{{HIST}}
<h3>日志存档</h3>
<div class="logs"><ul>{{LOGS}}</ul></div>
<p class="note">评分体系：股息吸引力 20 / 分红质量与可持续性 20 / 低波动与风险控制 20 / 估值合理性 15 / 盈利与财务质量 15 / 市场环境与资金趋势 10，全部子项用窗口内百分位排名标准化。部分子项（支付率、FCF 覆盖、ROE/ROIC、PB、10 年期国债收益率、ETF 资金流）需成分股财报与债券数据，尚未接入，按剩余权重重归一化并在日志标注覆盖率。风险预警独立于综合分。评分是配置吸引力排序工具，不构成投资建议。源码与逐日日志：<a href="https://github.com/{{REPO}}">github.com/{{REPO}}</a></p>
</div>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    global CONFIG, BENCH_BARS, BENCH_DATES
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true")
    args = ap.parse_args()

    with open(os.path.join(ROOT, "config.json"), "r", encoding="utf-8") as fh:
        CONFIG = json.load(fh)

    now = datetime.now(CST)
    run_at = now.strftime("%Y-%m-%d %H:%M")
    errors = []

    _, bench_bars = fetch_kline(CONFIG["benchmark"]["secid"])
    BENCH_BARS = bench_bars
    BENCH_DATES = [b["date"] for b in bench_bars]
    bench_closes = [b["close"] for b in bench_bars]

    bond10y = CONFIG.get("bond_10y_yield")
    if bond10y is None:
        errors.append("10 年期国债收益率未接入（股债收益差、股权风险溢价、利率三项按缺失处理）")

    all_series, payload, alerts_map = [], [], {}
    for etf in CONFIG["etfs"]:
        try:
            _, bars = fetch_kline(etf["secid"], fqt=1)
            _, raw_bars = fetch_kline(etf["secid"], fqt=0)
            need_from = (now - timedelta(days=CONFIG["windows"]["nav_lookback_days"])) \
                .strftime("%Y-%m-%d")
            nav = update_nav_cache(etf["code"], need_from, offline=args.offline)
            shares = update_shares_cache(etf["code"], etf["secid"], bars[-1]["date"])
            s = build_series(etf, bars, raw_bars, nav, shares, bond10y)
            s = score_series(s, bench_closes)
            all_series.append((etf, s))
            alerts_map[etf["code"]] = risk_alerts(s)
            print("[OK] {} {} {} 评分 {}".format(
                etf["code"], etf["short"], s[-1]["date"], num(s[-1]["score"], 1)), flush=True)
            r = s[-1]
            payload.append({
                "code": etf["code"], "name": etf["name"], "short": etf["short"],
                "index": etf["index"],
                "score": None if r["score"] is None else round(r["score"], 2),
                "rating": rating_of(r["score"])["label"] if r["score"] else "—",
                "coverage": round(r["coverage"], 3),
                "dimensions": [{"key": k, "name": n, "weight": w,
                                "score": None if r["dim"][k] is None else round(r["dim"][k], 2),
                                "coverage": round(r["dim_cov"][k], 3),
                                "subs": [{"key": sk, "name": sn, "weight": sw,
                                          "status": st,
                                          "score": None if r["sub"].get(sk) is None
                                          else round(r["sub"][sk], 2)}
                                         for sk, sn, sw, st in subs]}
                               for k, n, w, subs in DIMENSIONS],
                "alerts": alerts_map[etf["code"]],
                "metrics": {k: (round(v, 5) if isinstance(v, float) else v)
                            for k, v in r.items()
                            if k not in ("sub", "dim", "dim_cov", "score", "coverage", "missing")},
            })
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
    write_snapshot(payload, data_date, run_at)
    merged_alerts = []
    for etf, s in all_series:
        for a in alerts_map.get(etf["code"], []):
            merged_alerts.append(dict(a, title="{}：{}".format(etf["short"], a["title"])))
    write_log(all_series, merged_alerts, data_date, run_at, errors)
    repo_slug = os.environ.get("GITHUB_REPOSITORY", "xhxie123-maker/dividend-lowvol-score")
    write_dashboard(all_series, alerts_map, data_date, run_at, repo_slug, rows)

    run_log = os.path.join(ROOT, "data", "run_history.jsonl")
    os.makedirs(os.path.dirname(run_log), exist_ok=True)
    with open(run_log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_at": run_at, "data_date": data_date,
                             "etfs": [{"code": e["code"],
                                       "score": None if s[-1]["score"] is None else round(s[-1]["score"], 2),
                                       "coverage": round(s[-1]["coverage"], 3)}
                                      for e, s in all_series],
                             "errors": errors}, ensure_ascii=False) + "\n")

    print("完成：数据日期 {}，标的 {} 只，日志 logs/{}.md".format(data_date, len(all_series), data_date), flush=True)
    return 0


if __name__ == "__main__":
    CONFIG = {}
    BENCH_BARS = []
    BENCH_DATES = []
    sys.exit(main())
