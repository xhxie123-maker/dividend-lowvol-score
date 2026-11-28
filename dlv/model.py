# -*- coding: utf-8 -*-
"""六维评分模型：指标构建、百分位标准化、评级、风险预警。

维度与子项分值严格对齐参考模型：
  股息吸引力 20（6/5/4/3/2）
  分红质量与可持续性 20（4/4/5/4/3）
  低波动与风险控制 20（3/4/4/3/4/2）
  估值合理性 15（4/4/4/3）
  盈利与财务质量 15（3/3/3/2/2/2）
  市场环境与资金趋势 10（2/2/2/2/2）
标准化统一用窗口内百分位排名，方向按经济学含义取正/反向。
状态：live 直接可得 / proxy 代理可得 / missing 无源。
"""

import bisect
import math
from datetime import datetime, timedelta

# (键, 名称, 权重, [(子项键, 名称, 分值, 状态)])
DIMENSIONS = [
    ("div_attraction", "股息吸引力", 20, [
        ("cur_yield", "当前股息率", 6, "live"),
        ("avg3_yield", "3 年平均股息率", 5, "live"),
        ("yield_pct5", "股息率历史分位数", 4, "live"),
        ("spread", "股债收益差", 3, "live"),
        ("fwd_yield", "预期股息率", 2, "proxy"),
    ]),
    ("div_quality", "分红质量与可持续性", 20, [
        ("consec_years", "连续现金分红年限", 4, "live"),
        ("payout", "现金分红支付率", 4, "proxy"),
        ("fcf_cover", "自由现金流覆盖率", 5, "proxy"),
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
    ("valuation", "估值合理性", 14, [
        ("pe_pct", "PE 历史分位数", 5, "live"),
        ("pb_pct", "PB 历史分位数", 4, "missing"),
        ("erp", "股权风险溢价", 5, "live"),
    ]),
    ("financial", "盈利与财务质量", 14, [
        ("roe", "ROE", 3, "proxy"),
        ("roic", "ROIC", 3, "proxy"),
        ("ocf_np", "经营现金流/净利润", 2.5, "proxy"),
        ("debt_ratio", "资产负债率", 2, "proxy"),
        ("np_stability", "净利润增长稳定性", 1.5, "proxy"),
        ("interest_cover", "利息保障倍数", 2, "proxy"),
    ]),
    ("market_env", "市场环境与资金趋势", 12, [
        ("bond10y", "10 年期国债收益率", 2, "live"),
        ("rs", "红利低波相对强弱", 1.5, "live"),
        ("flow", "ETF 资金流向", 2, "live"),
        ("mkt_vol", "市场整体波动率", 2, "live"),
        ("crowding", "红利风格拥挤度", 1.5, "proxy"),
        ("style_rel", "红利/成长相对强弱", 3, "live"),
    ]),
]

STATUS_LABEL = {"live": "已实现", "proxy": "代理", "missing": "待接入"}


def num_fmt(x, nd=2):
    return "—" if x is None else "{:.{}f}".format(x, nd)


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def stdev(vals):
    vals = [v for v in vals if v is not None]
    if len(vals) < 2:
        return None
    mu = sum(vals) / len(vals)
    return math.sqrt(sum((v - mu) ** 2 for v in vals) / (len(vals) - 1))


def pct_rank(series, i, window, direction=1, min_obs=30):
    cur = series[i]
    if cur is None:
        return None
    seg = [v for v in series[max(0, i - window + 1):i + 1] if v is not None]
    if len(seg) < min_obs:
        return None
    below = sum(1 for v in seg if v <= cur)
    p = below / float(len(seg)) * 100.0
    return p if direction > 0 else 100.0 - p


def rolling_vol(closes, i, k):
    if i < k:
        return None
    seg = [math.log(closes[j] / closes[j - 1])
           for j in range(i - k + 1, i + 1) if closes[j - 1] > 0 and closes[j] > 0]
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
    win = closes[i - k + 1:i + 1]
    peak, mdd = win[0], 0.0
    for c in win:
        peak = max(peak, c)
        mdd = min(mdd, c / peak - 1.0)
    return mdd


def consec_div_years(yearly_div):
    years = sorted(yearly_div)
    if not years:
        return 0
    cur, n = years[-1], 1
    while str(int(cur) - 1) in yearly_div:
        cur = str(int(cur) - 1)
        n += 1
    return n


# ---------------------------------------------------------------------------
# 序列构建
# ---------------------------------------------------------------------------
def style_map(div_bars, grw_bars):
    """红利(000922) vs 成长(000918) 相对强弱：0.5×20 日 + 0.5×60 日超额。"""
    cd = {b["date"]: b["close"] for b in div_bars if b.get("close")}
    cg = {b["date"]: b["close"] for b in grw_bars if b.get("close")}
    common = sorted(set(cd) & set(cg))
    out = {}
    for i in range(60, len(common)):
        d = common[i]
        r20 = (cd[d] / cd[common[i - 20]] - 1.0) - (cg[d] / cg[common[i - 20]] - 1.0)
        r60 = (cd[d] / cd[common[i - 60]] - 1.0) - (cg[d] / cg[common[i - 60]] - 1.0)
        out[d] = 0.5 * r20 + 0.5 * r60
    return out


def build_series(etf, bars, raw_bars, nav, shares, bench, ext, windows):
    """ext: {'bond': {date:yield}, 'pe': {date:pe}, 'fin': {date:{...}}}"""
    dates = [b["date"] for b in bars]
    closes = [b["close"] for b in bars]
    amounts = [b["amount"] for b in bars]
    raw_close = {b["date"]: b["close"] for b in raw_bars}
    nav_dates = sorted(nav)
    yearly_div = {}
    for d, v in nav.items():
        if v["fhfcz"]:
            yearly_div[d[:4]] = yearly_div.get(d[:4], 0.0) + v["fhfcz"]

    bond_sorted = sorted(ext.get("bond") or {})
    bond_map = ext.get("bond") or {}
    pe_sorted = sorted(ext.get("pe") or {})
    pe_map = ext.get("pe") or {}
    fin_map = ext.get("fin") or {}
    style_map = ext.get("style") or {}
    style_dates = sorted(style_map)
    bench_map = {b["date"]: b["close"] for b in bench}
    bench_dates = sorted(bench_map)
    bench_amount = {b["date"]: b["amount"] for b in bench}
    bench_closes_seq = [b["close"] for b in bench]
    bench_vol_map = {}
    for i, b in enumerate(bench):
        v = rolling_vol(bench_closes_seq, i, 20)
        if v is not None:
            bench_vol_map[b["date"]] = v
    bench_vol_dates = sorted(bench_vol_map)

    rows = []
    for i, d in enumerate(dates):
        price = raw_close.get(d, closes[i])
        row = {"date": d, "close": closes[i], "close_raw": price, "amount": amounts[i]}
        j = bisect.bisect_right(nav_dates, d) - 1
        row["nav"] = nav[nav_dates[j]]["dwjz"] if j >= 0 else None
        row["nav_date"] = nav_dates[j] if j >= 0 else None
        row["premium"] = (price / row["nav"] - 1.0) if row["nav"] else None

        start = (datetime.strptime(d, "%Y-%m-%d") - timedelta(days=365)).strftime("%Y-%m-%d")
        row["ttm_div"] = None
        if nav_dates and nav_dates[0] <= start:
            row["ttm_div"] = sum(v["fhfcz"] for k, v in nav.items()
                                 if start < k <= d and v["fhfcz"])
        row["ttm_yield"] = (row["ttm_div"] / price) if (row["ttm_div"] is not None and price) else None

        row["vol20"] = rolling_vol(closes, i, 20)
        row["vol60"] = rolling_vol(closes, i, 60)
        row["vol120"] = rolling_vol(closes, i, 120)
        row["vol250"] = rolling_vol(closes, i, 250)
        row["downside250"] = rolling_downside(closes, i, 250)
        row["mdd250"] = rolling_mdd(closes, i, 250)
        row["dd250"] = closes[i] / max(closes[max(0, i - 249):i + 1]) - 1.0
        row["r20"] = closes[i] / closes[i - 20] - 1.0 if i >= 20 else None
        row["r60"] = closes[i] / closes[i - 60] - 1.0 if i >= 60 else None
        row["r120"] = closes[i] / closes[i - 120] - 1.0 if i >= 120 else None
        row["amount20"] = sum(amounts[i - 19:i + 1]) / 20.0 if i >= 20 else None

        kb = bisect.bisect_right(bond_sorted, d) - 1
        row["bond10y"] = bond_map[bond_sorted[kb]] if kb >= 0 else None

        kb2 = bisect.bisect_right(bench_dates, d) - 1
        row["bench_amount20"] = (sum(bench_amount[bench_dates[x]]
                                     for x in range(max(0, kb2 - 19), kb2 + 1) if x >= 0)
                                 / max(1, min(20, kb2 + 1))) if kb2 >= 0 else None
        kv = bisect.bisect_right(bench_vol_dates, d) - 1
        row["mkt_vol"] = bench_vol_map[bench_vol_dates[kv]] if kv >= 0 else None
        row["bench_r120"] = (bench_closes_seq[kb2] / bench_closes_seq[kb2 - 120] - 1.0) \
            if kb2 >= 120 else None
        row["excess120"] = (None if (row["r120"] is None or row["bench_r120"] is None)
                            else row["r120"] - row["bench_r120"])
        kn = bisect.bisect_right(style_dates, d) - 1
        row["style_rel"] = style_map[style_dates[kn]] if kn >= 0 else None

        sv = shares.get(d)
        row["shares"] = sv
        row["flow20"] = None
        if sv is not None and i >= 20 and shares.get(dates[i - 20]):
            base = shares[dates[i - 20]]
            row["flow20"] = (sv / base - 1.0) if base else None

        kp = bisect.bisect_right(pe_sorted, d) - 1
        row["pe"] = pe_map[pe_sorted[kp]] if kp >= 0 else None

        fin = fin_map.get(d) or {}
        for key in ("roe", "roa", "ocf_np", "icr", "debt", "np_growth", "payout", "np_stab"):
            row["fin_" + key] = fin.get(key)
        row["fin_coverage"] = fin.get("coverage")
        rows.append(row)

    # 股息率衍生
    y = [r["ttm_yield"] for r in rows]
    for i, r in enumerate(rows):
        seg = [v for v in y[max(0, i - 749):i + 1] if v is not None]
        r["yield_avg3"] = (sum(seg) / len(seg)) if len(seg) >= 120 else None
        r["yield_pct5"] = pct_rank(y, i, windows["percentile_window"], 1, 120)
        r["consec_years"] = consec_div_years(yearly_div)
        r["dps_growth"] = None
        r["div_cv"] = None
        if r["ttm_yield"] is not None and r["bond10y"] is not None:
            r["spread"] = r["ttm_yield"] - r["bond10y"] / 100.0
        else:
            r["spread"] = None
        if r["pe"] and r["pe"] > 0 and r["bond10y"] is not None:
            r["erp"] = 1.0 / r["pe"] - r["bond10y"] / 100.0
        else:
            r["erp"] = None

    # 每股股息增长率（TTM 口径，年化）
    ttm = [r["ttm_div"] for r in rows]
    for i, r in enumerate(rows):
        base = None
        for b in range(max(0, i - 750), i):
            if ttm[b] is not None:
                base = b
                break
        if base is not None and ttm[i] and ttm[base] and ttm[base] > 0:
            span = (i - base) / 250.0
            if span >= 0.8:
                r["dps_growth"] = (ttm[i] / ttm[base]) ** (1.0 / span) - 1.0
        r["fwd_yield"] = None
        if r["ttm_yield"] is not None:
            g = clamp(r["dps_growth"] or 0.0, -0.3, 0.3)
            r["fwd_yield"] = r["ttm_yield"] * (1.0 + g)

    # 分红稳定性（完整年度 CV，不足 3 年用 TTM 序列 CV）
    cur_year = rows[-1]["date"][:4] if rows else ""
    full_years = sorted(k for k in yearly_div if k < cur_year)
    for i, r in enumerate(rows):
        if len(full_years) >= 3:
            vals = [yearly_div[k] for k in full_years[-5:]]
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

    # 相对沪深300 超额
    for r in rows:
        k2 = bisect.bisect_right(bench_dates, r["date"]) - 1

        def br(k):
            if k2 < k or k2 < 0:
                return None
            return bench_map[bench_dates[k2]] / bench_map[bench_dates[k2 - k]] - 1.0

        b20, b60 = br(20), br(60)
        r["excess20"] = None if (r["r20"] is None or b20 is None) else r["r20"] - b20
        r["excess60"] = None if (r["r60"] is None or b60 is None) else r["r60"] - b60
    return rows


# ---------------------------------------------------------------------------
# 评分
# ---------------------------------------------------------------------------
def style_score(series, i, window, windows):
    """红利/成长相对强弱子项：趋势分位 + 拥挤闸。

    顺向：红利 60 日越强于成长，分数越高（红利占优阶段本 ETF 通常跑赢）。
    拥挤闸：分位 > crowd_pct 时封顶 crowd_cap，避免在风格极度拥挤时继续给高分。
    """
    p = pct_rank(series, i, window, 1, 120)
    if p is None:
        return None
    if p > windows.get("style_crowd_pct", 85.0):
        return min(p, windows.get("style_crowd_cap", 40.0))
    return p


def score_series(rows, windows):
    win = windows["percentile_window"]
    short = windows["short_window"]

    def col(k):
        return [r.get(k) for r in rows]

    cy = col("ttm_yield")
    a3 = col("yield_avg3")
    spread = col("spread")
    fwd = col("fwd_yield")
    pe_all = col("pe")
    pe_series = pe_all if any(v is not None for v in pe_all) else [r["close_raw"] for r in rows]

    mvol_series = col("mkt_vol")

    vols = {k: col(k) for k in ("vol20", "vol60", "vol120", "vol250", "mdd250", "downside250")}
    fin = {k: col("fin_" + k) for k in ("roe", "roa", "ocf_np", "icr", "debt", "np_growth", "payout", "np_stab")}
    rs_series = col("excess120")
    style_series = col("style_rel")
    flow = col("flow20")
    crowd = [None if (r.get("amount20") is None or r.get("bench_amount20") in (None, 0))
             else r["amount20"] / r["bench_amount20"] for r in rows]
    bond = col("bond10y")
    erp = col("erp")

    for idx, r in enumerate(rows):
        sub = {}
        # 股息吸引力
        sub["cur_yield"] = pct_rank(cy, idx, short, 1, 60)
        sub["avg3_yield"] = pct_rank(a3, idx, win, 1, 120)
        sub["yield_pct5"] = r["yield_pct5"]
        sub["spread"] = pct_rank(spread, idx, win, 1, 120)
        sub["fwd_yield"] = pct_rank(fwd, idx, win, 1, 120)
        # 分红质量
        sub["consec_years"] = tier_years(r["consec_years"])
        sub["payout"] = payout_score(r.get("fin_payout"))
        cover = None
        if r.get("fin_ocf_np") is not None and r.get("fin_payout"):
            cover = r["fin_ocf_np"] / r["fin_payout"]
        sub["fcf_cover"] = None if cover is None else 100.0 * clamp((cover - 1.0) / 2.0, 0, 1)
        sub["dps_growth"] = None if r["dps_growth"] is None else 50.0 + 50.0 * clamp(r["dps_growth"] / 0.08, -1, 1)
        sub["stability"] = None if r["div_cv"] is None else 100.0 * clamp(1.0 - r["div_cv"] / 0.30, 0, 1)
        # 低波
        sub["vol20"] = pct_rank(vols["vol20"], idx, win, -1, 120)
        sub["vol60"] = pct_rank(vols["vol60"], idx, win, -1, 120)
        sub["vol120"] = pct_rank(vols["vol120"], idx, win, -1, 120)
        sub["vol250"] = pct_rank(vols["vol250"], idx, win, -1, 120)
        sub["mdd250"] = pct_rank(vols["mdd250"], idx, win, 1, 120)
        sub["downside"] = pct_rank(vols["downside250"], idx, win, -1, 120)
        # 估值
        sub["pe_pct"] = pct_rank(pe_series, idx, win, -1, 120)
        sub["pb_pct"] = None
        sub["yield_pct_val"] = r["yield_pct5"]
        sub["erp"] = pct_rank(erp, idx, win, 1, 120)
        # 财务
        sub["roe"] = pct_rank(fin["roe"], idx, win, 1, 60)
        sub["roic"] = pct_rank(fin["roa"], idx, win, 1, 60)
        sub["ocf_np"] = pct_rank(fin["ocf_np"], idx, win, 1, 60)
        sub["debt_ratio"] = pct_rank(fin["debt"], idx, win, -1, 60)
        sub["np_stability"] = None if r.get("fin_np_stab") is None else 100.0 * clamp(1.0 - r["fin_np_stab"] / 60.0, 0, 1)
        sub["interest_cover"] = None if r.get("fin_icr") is None else 100.0 * clamp((r["fin_icr"] - 3.0) / 12.0, 0, 1)
        # 市场
        sub["bond10y"] = pct_rank(bond, idx, win, -1, 120)
        sub["rs"] = pct_rank(rs_series, idx, win, 1, 120)
        sub["flow"] = pct_rank(flow, idx, win, 1, 60)
        sub["mkt_vol"] = pct_rank(mvol_series, idx, win, -1, 120)
        sub["crowding"] = pct_rank(crowd, idx, win, -1, 120)
        sub["style_rel"] = style_score(style_series, idx, win, windows)

        dims, cov, missing = {}, {}, []
        for key, name, dw, subs in DIMENSIONS:
            tot_w, acc = 0.0, 0.0
            for sk, sn, sw, st in subs:
                v = sub.get(sk)
                if v is None:
                    missing.append("{}.{}".format(key, sk))
                else:
                    acc += sw * v
                    tot_w += sw
            cov[key] = tot_w / float(sum(x[2] for x in subs))
            dims[key] = None if tot_w == 0 else acc / tot_w
        total_w = sum(dw for k, n, dw, s in DIMENSIONS if dims[k] is not None)
        total = sum(dw * dims[k] for k, n, dw, s in DIMENSIONS if dims[k] is not None)
        got_w = sum(sw for k, n, dw, subs in DIMENSIONS
                    for sk, sn, sw, st in subs if sub.get(sk) is not None)
        r["sub"] = sub
        r["dim"] = dims
        r["dim_cov"] = cov
        r["score"] = None if total_w == 0 else total / total_w
        r["coverage"] = got_w / 100.0
        r["missing"] = missing
    return rows


def tier_years(n):
    if n is None:
        return None
    return 100.0 if n >= 10 else 85.0 if n >= 5 else 70.0 if n >= 3 else 45.0 if n >= 2 else 25.0 if n >= 1 else 0.0


def payout_score(p):
    if p is None:
        return None
    if p < 0.9:
        return 100.0 * clamp((p - 0.15) / 0.25, 0, 1)
    return 100.0 * clamp((1.25 - p) / 0.35, 0, 1)


def rating_of(score, bands):
    for b in bands:
        if score >= b["min"]:
            return b
    return bands[-1]


# ---------------------------------------------------------------------------
# 风险预警（独立于综合分）
# ---------------------------------------------------------------------------
def risk_alerts(rows):
    r = rows[-1]
    out = []

    def add(level, title, detail, kind):
        out.append({"level": level, "title": title, "detail": detail, "kind": kind})

    y = [x["ttm_yield"] for x in rows[-250:] if x["ttm_yield"] is not None]
    if len(y) >= 60 and r["ttm_yield"] is not None:
        mu = sum(y) / len(y)
        sd = stdev(y)
        if sd and (r["ttm_yield"] - mu) / sd > 2.0:
            add("高", "股息率异常升高",
                "TTM 股息率 {:.2%} 偏离 250 日均值 {:.2%} 达 {:.1f} 倍标准差，需排查价格下跌或分红削减".format(
                    r["ttm_yield"], mu, (r["ttm_yield"] - mu) / sd), "yield_spike")

    if r["vol20"] and r["vol250"] and r["vol20"] / r["vol250"] > 1.5:
        add("高", "波动率快速上升", "20 日年化波动 {:.2%} 为 250 日 {:.2%} 的 {:.2f} 倍，超 1.5 倍线".format(
            r["vol20"], r["vol250"], r["vol20"] / r["vol250"]), "vol_jump")

    if r["dd250"] is not None and r["dd250"] < -0.15:
        add("高", "最大回撤恶化", "距 250 日高点回撤 {:.2%}，超 -15% 线".format(r["dd250"]), "drawdown")
    elif r["mdd250"] is not None and r["mdd250"] < -0.10:
        add("中", "最大回撤恶化", "250 日区间最大回撤 {:.2%}，超 -10% 观察线".format(r["mdd250"]), "drawdown")

    if r["premium"] is not None and abs(r["premium"]) > 0.01:
        add("中", "ETF 折溢价异常", "当日折溢价 {:.2%}，超 ±1% 阈值".format(r["premium"]), "premium")

    if r["consec_years"] and r["dps_growth"] is not None and r["dps_growth"] < -0.20:
        add("中", "分红政策变化", "近 3 年每份分红年化 {:.2%}，降幅超 20%，需核对公告".format(r["dps_growth"]), "div_cut")

    if r.get("fin_payout") is not None and r["fin_payout"] > 1.0:
        add("中", "支付率过高", "成分股加权分红支付率 {:.0%} 超过当期净利润，需核查可持续性".format(r["fin_payout"]), "payout_high")

    if not out:
        add("低", "无触发项", "股息率、波动率、回撤、折溢价、分红增长、支付率均未触及阈值", "ok")
    return out
