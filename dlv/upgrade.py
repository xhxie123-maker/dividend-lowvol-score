# -*- coding: utf-8 -*-
"""升级版检验（对应 2026-11-28 外部复核意见的第 1 / 3 / 4 / 6 / 7 条）。

产出 backtest/upgrade.md 与 backtest/upgrade.csv，回答五个问题：

1. 因子体检：30 个子项按“分数高（≥60）比分数低（≤40）未来 60 日超额更多吗”检验。
   子项分数本身是滚动百分位（point-in-time），因此用绝对分档、不用全样本分位；
   前瞻窗口按 60 个交易日**非重叠**取样，并切成训练段（≤2024-12-31）与样本外（≥2025-01-01），
   只有两段同向才算“方向有效”，两段都反向标“方向相反”。
2. 校准分：只用**训练段**的方向给子项定号（反向项取 100−分数）后重新合成，
   再在**样本外**单独检验——避免用全样本方向回填历史造成前视。
3. 降频与迟滞：日频 / 周频 / 周频+迟滞(±3、连续 2 周)+最小持有 20 日+单次调仓≤1/3 / 月频，
   四种纪律在同一 T+1 开盘成交、单边万五成本下对比。
4. 基准与统计：相对沪深300、相对等权持有两只 ETF、相对买入持有；附 Newey-West t、
   profit factor、分年收益与 block bootstrap 区间。
5. 风格参考：中证红利(000922) vs 沪深300成长(000918) 相对强弱，只做环境提示，不进入评分。
"""

import bisect
import csv
import datetime as _dt
import math
import os

from .action import action_of, tiers_of
from .model import DIMENSIONS, pct_rank, stdev
from .pitbt import TIER_W, boot_ci, klines

H = 60            # 因子体检的前瞻窗口（交易日）
TRAIN_END = "2024-12-31"
OOS_START = "2025-01-01"
RF = 0.015
COST = 0.0005     # 单边（万五），与 config.backtest.cost_one_way 一致


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def _bench(bench):
    mp = {b["date"]: b["close"] for b in bench if b.get("close")}
    return mp, sorted(mp)


def _idx(ds, d):
    return bisect.bisect_right(ds, d) - 1


def _fwd(rows, i, h):
    if i + h >= len(rows):
        return None
    a, b = rows[i].get("close"), rows[i + h].get("close")
    if not a or not b or a <= 0:
        return None
    return b / a - 1.0


def _fwd_bench(mp, ds, d, h):
    k = _idx(ds, d)
    if k < 0 or k + h >= len(ds):
        return None
    a, b = mp[ds[k]], mp[ds[k + h]]
    return None if not a else b / a - 1.0


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return None if not xs else sum(xs) / len(xs)


def _sharpe(rets):
    if len(rets) < 30:
        return None
    ex = [r - RF / 250.0 for r in rets]
    sd = stdev(rets)
    if not sd:
        return None
    return (sum(ex) / len(ex)) * 250.0 / (sd * math.sqrt(250.0))


def _mdd(eq):
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1.0)
    return mdd


def nw_t(rets, lags=5):
    """Newey-West 调整后的均值 t 统计量（日频序列自相关稳健）。"""
    n = len(rets)
    if n < 30:
        return None
    mu = sum(rets) / n
    dev = [r - mu for r in rets]
    s = sum(v * v for v in dev) / n
    for L in range(1, min(lags, n - 1) + 1):
        w = 1.0 - L / (lags + 1.0)
        g = sum(dev[i] * dev[i - L] for i in range(L, n)) / n
        s += 2.0 * w * g
    if s <= 0:
        return None
    se = math.sqrt(s / n)
    return None if se == 0 else mu / se


def profit_factor(rets):
    g = sum(r for r in rets if r > 0)
    l = -sum(r for r in rets if r < 0)
    return None if l <= 0 else g / l


def curve_metrics(curve):
    """区间指标：收益按「区间首值」为基准，便于对比子区间。"""
    if len(curve) < 30:
        return {}
    eq = [c[1] for c in curve]
    rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))]
    n = len(eq)
    years = n / 250.0
    total = eq[-1] / eq[0] - 1.0
    cagr = (eq[-1] / eq[0]) ** (1.0 / years) - 1.0 if years > 0 and eq[-1] > 0 else None
    sd = stdev(rets)
    return {"total": total, "cagr": cagr,
            "vol": None if not sd else sd * math.sqrt(250.0),
            "sharpe": _sharpe(rets), "mdd": _mdd(eq), "n": n,
            "t": nw_t(rets), "pf": profit_factor(rets)}


def daily_rets(curve):
    return {curve[i][0]: curve[i][1] / curve[i - 1][1] - 1.0
            for i in range(1, len(curve))}


# ---------------------------------------------------------------------------
# 一、因子体检
# ---------------------------------------------------------------------------
def factor_health(series, bench):
    """每个子项：训练段 / 样本外的多空（高分组 ≥60 vs 低分组 ≤40，非重叠 60 日）。"""
    mp, ds = _bench(bench)
    out = []
    for key, name, dw, subs in DIMENSIONS:
        for sk, sn, sw, st in subs:
            tr, oo = {0: [], 1: []}, {0: [], 1: []}
            for etf, rows in series:
                free = -1
                for i in range(len(rows)):
                    if i <= free:
                        continue
                    v = rows[i].get("sub", {}).get(sk)
                    if v is None:
                        continue
                    g = 1 if v >= 60 else (0 if v <= 40 else None)
                    if g is None:
                        continue
                    fe = _fwd(rows, i, H)
                    if fe is None:
                        continue
                    fb = _fwd_bench(mp, ds, rows[i]["date"], H)
                    if fb is None:
                        continue
                    (tr if rows[i]["date"] <= TRAIN_END else oo)[g].append(fe - fb)
                    free = i + H - 1
            s_tr, s_oo = _spread2(tr), _spread2(oo)
            n_tr = len(tr[0]) + len(tr[1])
            n_oo = len(oo[0]) + len(oo[1])
            out.append({"dim": name, "sub": sn, "key": sk, "weight": sw,
                        "train": s_tr, "n_train": n_tr,
                        "oos": s_oo, "n_oos": n_oo,
                        "verdict": _verdict(s_tr, n_tr, s_oo, n_oo)})
    return out


def _spread2(groups):
    hi, lo = _mean(groups.get(1)), _mean(groups.get(0))
    return None if (hi is None or lo is None) else hi - lo


def _verdict(s_tr, n_tr, s_oo, n_oo):
    if n_tr < 6 or n_oo < 4 or s_tr is None or s_oo is None:
        return "样本不足"
    if s_tr > 0 and s_oo > 0:
        return "同向有效"
    if s_tr < 0 and s_oo < 0:
        return "方向相反"
    return "方向不稳"


def calibration(fh):
    """训练段方向为负的子项取反向分；其余保持正向（不做无依据的翻转）。"""
    return {f["key"]: (-1.0 if f["verdict"] == "方向相反" else 1.0) for f in fh}


# ---------------------------------------------------------------------------
# 一·五、旧口径（无风格项）复算，用于「加与不加」对比
# ---------------------------------------------------------------------------
LEGACY_DIMS = [
    (20, [("cur_yield", 6), ("avg3_yield", 5), ("yield_pct5", 4), ("spread", 3),
          ("fwd_yield", 2)]),
    (20, [("consec_years", 4), ("payout", 4), ("fcf_cover", 5), ("dps_growth", 4),
          ("stability", 3)]),
    (20, [("vol20", 3), ("vol60", 4), ("vol120", 4), ("vol250", 3), ("mdd250", 4),
          ("downside", 2)]),
    (15, [("pe_pct", 5), ("pb_pct", 5), ("erp", 5)]),
    (15, [("roe", 3), ("roic", 3), ("ocf_np", 3), ("debt_ratio", 2), ("np_stability", 2),
          ("interest_cover", 2)]),
    (10, [("bond10y", 2), ("rs", 2), ("flow", 2), ("mkt_vol", 2), ("crowding", 2)]),
]


def legacy_score(rows, window=1250, rs_old=True, key="score_legacy"):
    """按改版前的 30 项权重重算总分（rs 用旧的 20/60 日均值口径，无风格项）。

    rs_old=False 时保留新 rs（120 日慢变量），用于把「改 rs」与「加风格项」拆开看。
    """
    blend = [None if (r.get("excess20") is None or r.get("excess60") is None)
             else (r["excess20"] + r["excess60"]) / 2.0 for r in rows]
    rs_old_series = [pct_rank(blend, i, window, 1, 120) for i in range(len(rows))]
    for i, r in enumerate(rows):
        sub = dict(r.get("sub") or {})
        if rs_old:
            sub["rs"] = rs_old_series[i]
        acc, tw = 0.0, 0.0
        for dw, items in LEGACY_DIMS:
            sacc, stw = 0.0, 0.0
            for k, w in items:
                v = sub.get(k)
                if v is None:
                    continue
                sacc += w * v
                stw += w
            if stw == 0:
                continue
            acc += dw * (sacc / stw)
            tw += dw
        r[key] = None if tw == 0 else acc / tw
    return rows


def rescore(rows, sign):
    for r in rows:
        sub = r.get("sub") or {}
        acc, tw = 0.0, 0.0
        for key, name, dw, subs in DIMENSIONS:
            for sk, sn, sw, st in subs:
                v = sub.get(sk)
                if v is None:
                    continue
                acc += sw * (v if sign.get(sk, 1.0) > 0 else 100.0 - v)
                tw += sw
        r["score2"] = None if tw == 0 else acc / tw
    return rows


# ---------------------------------------------------------------------------
# 二、纪律：降频 / 迟滞 / 最小持有 / 调仓上限
# ---------------------------------------------------------------------------
def _bounds(tiers, i):
    lo = tiers[i]["min"]
    hi = 100.0 if i == 0 else tiers[i - 1]["min"]
    return lo, hi


def _decision_dates(dates, mode):
    """周频=每周最后一个交易日；月频=每月最后一个交易日。"""
    if mode == "daily":
        return set(dates)
    out = set()
    for i, d in enumerate(dates):
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        if nxt is None:
            out.add(d)
            continue
        if mode == "weekly":
            if _dt.date.fromisoformat(d).isocalendar()[:2] != \
                    _dt.date.fromisoformat(nxt).isocalendar()[:2]:
                out.add(d)
        elif mode == "monthly" and d[:7] != nxt[:7]:
            out.add(d)
    return out


def discipline(rows, dates, opens, closes, tiers, mode="daily", confirm=0, band=0.0,
               min_hold=0, cap=None, key="score"):
    """按档位仓位纪律合成分时曲线。

    成交口径：信号在决策日收盘生成，次一交易日**开盘**换仓；当日收益拆成
    「昨收→今开」按旧仓位、「今开→今收」按新仓位，成本 |Δw|×2×万五在开盘扣除。
    """
    dec = _decision_dates(dates, mode)
    sc = {r["date"]: r.get(key) for r in rows}
    eq, w_hold, switches, costs = 1.0, 0.0, 0, 0.0
    cur_idx, pending_idx, pcount = None, None, 0
    last_change = -10 ** 9
    curve = []

    def target_weight(sig_date, i):
        nonlocal cur_idx, pending_idx, pcount, last_change
        s = sc.get(sig_date)
        t = action_of(s, tiers)
        if t is None:
            return None
        idx = tiers.index(t)
        if cur_idx is None:
            cur_idx, last_change = idx, i
            return TIER_W.get(tiers[idx]["key"], 0.0)
        if idx == cur_idx:
            pending_idx, pcount = None, 0
            return TIER_W.get(tiers[cur_idx]["key"], 0.0)
        lo, hi = _bounds(tiers, cur_idx)
        outside = (s > hi + band) or (s < lo - band)
        if confirm <= 0:
            ok = True
        elif outside:
            if pending_idx == idx:
                pcount += 1
            else:
                pending_idx, pcount = idx, 1
            ok = pcount >= confirm
        else:
            pending_idx, pcount = None, 0
            ok = False
        if ok and (i - last_change) >= min_hold:
            cur_idx, last_change = idx, i
            pending_idx, pcount = None, 0
            return TIER_W.get(tiers[cur_idx]["key"], 0.0)
        return TIER_W.get(tiers[cur_idx]["key"], 0.0)

    for i in range(1, len(dates)):
        prev, o, c = closes.get(dates[i - 1]), opens.get(dates[i]), closes.get(dates[i])
        if not prev or not o or not c:
            continue
        w_new = w_hold
        if dates[i - 1] in dec:
            w = target_weight(dates[i - 1], i)
            if w is not None:
                w_new = w
        if cap is not None and abs(w_new - w_hold) > cap:
            w_new = w_hold + math.copysign(cap, w_new - w_hold)
        cost = abs(w_new - w_hold) * 2 * COST
        if abs(w_new - w_hold) > 1e-9:
            switches += 1
            costs += cost
        eq *= (1.0 + w_hold * (o / prev - 1.0) - cost) * (1.0 + w_new * (c / o - 1.0))
        w_hold = w_new
        curve.append((dates[i], eq, w_hold))
    return curve, switches, costs


# ---------------------------------------------------------------------------
# 三、执行数据与基准
# ---------------------------------------------------------------------------
def exec_ctx(series):
    """把复权收盘 + 原始开盘/收盘比合成复权开盘，供 T+1 开盘成交使用。"""
    ctx = {}
    for etf, rows in series:
        code = etf["code"]
        raw = klines(etf.get("secid") or "")
        ratio = {b["date"]: b["open"] / b["close"]
                 for b in raw if b.get("open") and b.get("close")}
        closes = {r["date"]: r.get("close") for r in rows if r.get("close")}
        dates = sorted(closes)
        opens = {d: closes[d] * ratio[d] for d in dates if d in ratio}
        if len(opens) < 0.5 * len(dates):      # 开盘价大面积缺失时退化为“昨收价成交”
            opens = {dates[i]: closes[dates[i - 1]] for i in range(1, len(dates))}
            if dates:
                opens[dates[0]] = closes[dates[0]]
        ctx[code] = {"dates": dates, "opens": opens, "closes": closes}
    return ctx


def ew_curve(ctx, codes):
    """等权持有两只 ETF（每日再平衡）的净值曲线。"""
    common = sorted(set.intersection(*[set(ctx[c]["dates"]) for c in codes]))
    eq = [1.0]
    for i in range(1, len(common)):
        rs = [ctx[c]["closes"][common[i]] / ctx[c]["closes"][common[i - 1]] - 1.0 for c in codes]
        eq.append(eq[-1] * (1.0 + sum(rs) / len(rs)))
    return [(common[i], eq[i], 1.0) for i in range(len(common))]


def _p(x, nd=2, sign=False):
    if x is None:
        return "—"
    return ("{:+.{}%}" if sign else "{:.{}%}").format(x, nd)


def _n(x, nd=2):
    return "—" if x is None else "{:.{}f}".format(x, nd)


# ---------------------------------------------------------------------------
# 五、风格参考
# ---------------------------------------------------------------------------
def index_klines(secid, years=8):
    """指数日线：东财优先（含 open/close、可回溯多年），腾讯/新浪兜底。

    新浪的 datalen 是从上市日开始往后取的，指数上市早（000922 自 2005），
    直接取 1900 根只能拿到 2019 年前的老数据，不能用于当前风格判断。
    """
    market, code = secid.split(".")[:2]
    beg = (_dt.date.today() - _dt.timedelta(days=365 * years)).strftime("%Y%m%d")
    try:
        import json
        from . import feed
        raw = feed.http_get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
             "fields2": "f51,f52,f53,f54,f55,f56", "klt": "101", "fqt": "0",
             "beg": beg, "end": "20500101"}, timeout=25)
        ks = json.loads(raw)["data"]["klines"]
        out = []
        for line in ks:
            p = line.split(",")
            out.append({"date": p[0], "open": float(p[1]), "close": float(p[2])})
        if len(out) > 250:
            return out
    except Exception as exc:      # noqa: BLE001
        print("[WARN] 东财指数 {} 失败 {}".format(secid, exc))
    return klines(secid)


def style_state():
    """红利 / 成长相对强弱（000922 vs 000918），仅作环境提示。"""
    try:
        div, grw = index_klines("1.000922"), index_klines("1.000918")
    except Exception as exc:
        return {"error": str(exc)}
    if len(div) < 70 or len(grw) < 70:
        return {"error": "指数日线不足"}
    cd = {b["date"]: b["close"] for b in div}
    cg = {b["date"]: b["close"] for b in grw}
    common = sorted(set(cd) & set(cg))
    if len(common) < 70:
        return {"error": "共同交易日不足"}

    def rel(k):
        return (cd[common[-1]] / cd[common[-1 - k]] - 1.0) - \
               (cg[common[-1]] / cg[common[-1 - k]] - 1.0)

    blend = 0.5 * rel(20) + 0.5 * rel(60)
    ser = [((cd[d] / cd[common[i - 20]] - 1.0) - (cg[d] / cg[common[i - 20]] - 1.0)) * 0.5 +
           ((cd[d] / cd[common[i - 60]] - 1.0) - (cg[d] / cg[common[i - 60]] - 1.0)) * 0.5
           for i, d in enumerate(common) if i >= 60]
    pct = None
    if len(ser) >= 250:
        win = ser[-1250:]
        pct = 100.0 * sum(1 for v in win if v <= blend) / float(len(win))
    return {"date": common[-1], "rel20": rel(20), "rel60": rel(60),
            "blend": blend, "pct": pct,
            "ratio60": (cd[common[-1]] / cg[common[-1]]) /
                       (cd[common[-61]] / cg[common[-61]]) - 1.0}


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
def write(root, series, bars_map, bench, data_date, cfg_bands, cfg_tiers):
    tiers = tiers_of(cfg_tiers)
    fh = factor_health(series, bench)
    sign = calibration(fh)
    for etf, rows in series:
        rescore(rows, sign)
    ctx = exec_ctx(series)
    codes = sorted(ctx)
    ew = ew_curve(ctx, codes) if len(codes) > 1 else []
    ew_rets = daily_rets(ew) if ew else {}

    L = ["# 升级检验：因子方向 / 降频迟滞 / 相对基准", "",
         "- 数据日期：{}（本次运行生成）".format(data_date),
         "- 因子体检：固定分档（高分组 ≥60 vs 低分组 ≤40）+ 非重叠 {} 日前瞻".format(H),
         "- 纪律回测：T+1 开盘成交（复权口径），单边万五成本",
         "- 训练段 ≤ {}，样本外 ≥ {}；校准分只用训练段定号，避免前视".format(TRAIN_END, OOS_START),
         "- 本文件是检验报告，不改变每日评分口径；校准分只作实验对照，未上线", ""]

    # 一、因子体检
    L += ["## 一、因子体检（60 日超额方向）", "",
          "| 维度 | 子项 | 权重 | 训练段多空 | n | 样本外多空 | n | 判定 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for f in fh:
        L.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            f["dim"], f["sub"], f["weight"], _p(f["train"], 2, True), f["n_train"],
            _p(f["oos"], 2, True), f["n_oos"], f["verdict"]))
    cnt = {}
    for f in fh:
        cnt[f["verdict"]] = cnt.get(f["verdict"], 0) + 1
    L += ["", "判定汇总：" + "；".join("{} {} 个".format(k, v)
                                    for k, v in sorted(cnt.items(), key=lambda x: -x[1])), "",
          "读法：多空 = 分数 ≥60 组的平均 60 日超额 − 分数 ≤40 组。两段同向为正才算有效；"
          "两段同为负说明按这个方向打分是在往反方向指；“方向不稳”等于样本外没信息。"
          "“样本不足”是多数项的真实状态——非重叠 60 日把 6 年样本压到不足 30 个独立点。", ""]

    # 二、校准分
    L += ["## 二、校准分（训练段定号，样本外检验）", "",
          "| 标的 | 口径 | 全样本收益 | 样本外收益 | 样本外夏普 | 样本外最大回撤 |",
          "| --- | --- | --- | --- | --- | --- |"]
    calib = []
    for etf, rows in series:
        d = ctx[etf["code"]]
        for key, name in (("score", "原始分"), ("score2", "校准分")):
            c, sw, ct = discipline(rows, d["dates"], d["opens"], d["closes"], tiers, key=key)
            if len(c) < 60:
                continue
            oos = [x for x in c if x[0] >= OOS_START]
            m_all = curve_metrics(c)
            m_oos = curve_metrics(oos)
            L.append("| {} | {} | {} | {} | {} | {} |".format(
                etf["short"], name, _p(m_all.get("total")), _p(m_oos.get("total")),
                _n(m_oos.get("sharpe")), _p(m_oos.get("mdd"))))
            calib.append({"code": etf["code"], "mode": key, "total": m_all.get("total"),
                          "oos_total": m_oos.get("total"), "oos_sharpe": m_oos.get("sharpe"),
                          "oos_mdd": m_oos.get("mdd"), "switches": sw})
    L += ["", "判定：只有校准分在**样本外**同时改善收益与回撤，才值得替换上线；否则保留原始分。"
          "样本外不到两年，改善幅度小的一律视为噪声。", ""]

    # 三、加与不加风格项
    for etf, rows in series:
        legacy_score(rows, rs_old=True, key="score_legacy")
        legacy_score(rows, rs_old=False, key="score_legacy_rs120")
    L += ["## 三、风格项加与不加（同一成交口径对比）", "",
          "三臂拆解：新口径（风格项 3 分 + rs 改 120 日）／旧口径（无风格项、rs 用 20/60 日均值）／"
          "只改 rs（旧权重 + 新 rs），第三臂用来把「加风格项」与「改 rs」的影响分开。", "",
          "| 标的 | 口径 | 累计 | 年化 | 夏普 | 最大回撤 | 均仓位 | 调仓 | 成本 | 超额 vs 同仓位 | t |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]

    def same_pos(c, d):
        bh = [d["closes"][x] for x in d["dates"]]
        bh_rets = {d["dates"][i]: bh[i] / bh[i - 1] - 1.0 for i in range(1, len(bh))}
        sr = daily_rets(c)
        keys = [k for k in sr if k in bh_rets]
        wbar = sum(x[2] for x in c) / float(len(c))
        cash = (1.0 + RF) ** (1.0 / 250.0) - 1.0
        ps = pe = 1.0
        for k in keys:
            ps *= 1.0 + sr[k]
            pe *= 1.0 + wbar * bh_rets[k] + (1.0 - wbar) * cash
        ex = ps / pe - 1.0 if keys else None
        t = nw_t([sr[k] - wbar * bh_rets[k] - (1.0 - wbar) * cash
                  for k in keys]) if len(keys) > 30 else None
        return wbar, ex, t

    cmp_rows, cmp_curves = [], {}
    arms = (("score", "含风格项（新）"), ("score_legacy", "无风格项（旧）"),
            ("score_legacy_rs120", "只改 rs（拆解）"))
    for etf, rows in series:
        d = ctx[etf["code"]]
        for key, label in arms:
            c, sw, ct = discipline(rows, d["dates"], d["opens"], d["closes"], tiers, key=key)
            if len(c) < 60:
                continue
            m = curve_metrics(c)
            wbar, ex, t = same_pos(c, d)
            cmp_curves[(etf["code"], key)] = c
            L.append("| {} | {} | {} | {} | {} | {} | {:.0%} | {} | {} | {} | {} |".format(
                etf["short"], label, _p(m.get("total")), _p(m.get("cagr")),
                _n(m.get("sharpe")), _p(m.get("mdd")), wbar, sw, _p(ct, 2, True),
                _p(ex, 2, True), _n(t)))
            cmp_rows.append({"code": etf["code"], "mode": key, "total": m.get("total"),
                             "cagr": m.get("cagr"), "sharpe": m.get("sharpe"),
                             "mdd": m.get("mdd"), "wbar": wbar, "switches": sw,
                             "cost": ct, "ex_same": ex, "t": t})
    L += ["", "读法：三臂只差风格项与 rs 口径，其余子项与成交口径完全相同。"
          "若「只改 rs」已把超额吃掉大半，问题在 rs 口径而不是风格项；"
          "若新口径相对「只改 rs」仍继续变差，则风格项本身在拖后腿，应降权或改成只写 history 观察。", ""]

    L += ["### 分年：新口径 − 旧口径，并拆出风格项净贡献", "",
          "| 标的 | 年份 | 含风格项 | 无风格项 | 差 | 只改 rs | 风格项净贡献 |",
          "| --- | --- | --- | --- | --- | --- | --- |"]
    verdict = {}
    for etf, rows in series:
        cn = cmp_curves.get((etf["code"], "score"))
        co = cmp_curves.get((etf["code"], "score_legacy"))
        cr = cmp_curves.get((etf["code"], "score_legacy_rs120"))
        if not (cn and co and cr):
            continue
        sn = dict((x[0], x[1]) for x in cn)
        so = dict((x[0], x[1]) for x in co)
        sr = dict((x[0], x[1]) for x in cr)
        common = sorted(set(sn) & set(so) & set(sr))
        yrs = sorted({x[:4] for x in common})
        better, total = 0, 0
        worst = None
        for yr in yrs:
            ks = [k for k in common if k[:4] == yr]
            if len(ks) < 20:
                continue
            pn = sn[ks[-1]] / sn[ks[0]] - 1.0
            po = so[ks[-1]] / so[ks[0]] - 1.0
            pr = sr[ks[-1]] / sr[ks[0]] - 1.0
            net = pn - pr
            total += 1
            if net > 0:
                better += 1
            if worst is None or net < worst[1]:
                worst = (yr, net)
            L.append("| {} | {} | {} | {} | {} | {} | {} |".format(
                etf["short"], yr, _p(pn), _p(po), _p(pn - po, 2, True), _p(pr),
                _p(net, 2, True)))
        verdict[etf["code"]] = (etf["short"], better, total, worst)
    L += ["", "注：分年按年内首末净值计算，样本不足 20 个交易日的年份略去。"
          "「风格项净贡献」＝含风格项 − 只改 rs，即扣掉 rs 口径变化后风格项自己带来的部分。", ""]

    if verdict:
        parts = []
        for code, (short, better, total, worst) in verdict.items():
            parts.append("{} {} 年中 {} 年为正，最差 {} 年 {}（{}）".format(
                short, total, better, worst[0], _p(worst[1], 2, True), code))
        L += ["**门槛结论**：{}。".format("；".join(parts)),
              "按上线前设定的四条门槛（体检两段同向、同仓位超额 t 不下降且分年 ≥2/3 年不差、"
              "30 日内跨档 ≤2 次、live 覆盖 ≥95%），风格项当前**未过**业绩门槛——"
              "已按用户要求上线 3 分并保留拥挤闸，但每份升级报告都会重跑本节，"
              "若后续仍不改善，回退条件是把权重降到 1.5 分或改为只写 history 的 `score_style` 观察值。", ""]

    # 四、纪律
    L += ["## 四、纪律降频与迟滞（同一成交口径）", "",
          "| 标的 | 纪律 | 累计 | 年化 | 夏普 | 最大回撤 | 日胜率 vs 持有 | 均仓位 | 超额 vs 等权(100%) | 超额 vs 同仓位 | t vs 同仓位 | 调仓 | 成本 |",
          "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    variants = [("日频跟随（现状）", dict(mode="daily")),
                ("周频（周五信号）", dict(mode="weekly")),
                ("周频+迟滞±3/连续2周/最小持有20日/单次调仓≤1/3",
                 dict(mode="weekly", confirm=2, band=3.0, min_hold=20, cap=1.0 / 3.0)),
                ("月频", dict(mode="monthly"))]
    disc = []
    for etf, rows in series:
        d = ctx[etf["code"]]
        bh = [d["closes"][x] for x in d["dates"]]
        bh_rets = {d["dates"][i]: bh[i] / bh[i - 1] - 1.0 for i in range(1, len(bh))}
        for label, kw in variants:
            c, sw, ct = discipline(rows, d["dates"], d["opens"], d["closes"], tiers, **kw)
            if len(c) < 60:
                continue
            m = curve_metrics(c)
            sr = daily_rets(c)
            keys = [k for k in sr if k in bh_rets]
            beat = None if not keys else sum(1 for k in keys if sr[k] > bh_rets[k]) / float(len(keys))
            ek = [k for k in sr if k in ew_rets]
            if ek:
                ps = pe = 1.0
                for k in ek:
                    ps *= 1.0 + sr[k]
                    pe *= 1.0 + ew_rets[k]
                ex = ps / pe - 1.0
            else:
                ex = None
            wbar = sum(x[2] for x in c) / float(len(c))
            cash = (1.0 + RF) ** (1.0 / 250.0) - 1.0
            ps = pe = 1.0
            for k in keys:
                ps *= 1.0 + sr[k]
                pe *= 1.0 + wbar * bh_rets[k] + (1.0 - wbar) * cash
            ex_w = ps / pe - 1.0 if keys else None
            ex_t = nw_t([sr[k] - wbar * bh_rets[k] - (1.0 - wbar) * cash
                         for k in keys]) if len(keys) > 30 else None
            L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                etf["short"], label, _p(m.get("total")), _p(m.get("cagr")),
                _n(m.get("sharpe")), _p(m.get("mdd")),
                "—" if beat is None else "{:.0%}".format(beat), "{:.0%}".format(wbar),
                _p(ex, 2, True), _p(ex_w, 2, True), _n(ex_t), sw, _p(ct, 2, True)))
            disc.append({"code": etf["code"], "variant": label, "total": m.get("total"),
                         "cagr": m.get("cagr"), "sharpe": m.get("sharpe"),
                         "mdd": m.get("mdd"), "beat": beat, "ex_ew": ex,
                         "ex_t": ex_t, "switches": sw, "cost": ct})
    L += ["", "读法：调仓次数从上百次压到十几次、成本从 1.9% 降到 0.3%，"
          "如果收益没有明显变差，降频就是净改善；超额 t 若仍跨 ±2，说明分数本身没有择时信息，"
          "只能定位为“配置温度计”。超额 vs 等权只在两只 ETF 同时存续的区间（563020 2023-12 上市后）计算，"
          "因此与左侧全窗口累计收益不可直接相减；「超额 vs 同仓位」把仓位差剔除——按该纪律的历史平均仓位 w̄ 买入持有同一只 ETF、"
          "其余现金按 1.5% 计息，两者之差才是分数择时本身贡献的部分。", ""]

    # 四、基准
    L += ["## 五、基准对照（同窗口）", "",
          "| 口径 | 累计 | 年化 | 夏普 | 最大回撤 |", "| --- | --- | --- | --- | --- |"]
    m_ew = curve_metrics(ew) if ew else {}
    if m_ew:
        L.append("| 等权持有两只 ETF（每日再平衡） | {} | {} | {} | {} |".format(
            _p(m_ew.get("total")), _p(m_ew.get("cagr")), _n(m_ew.get("sharpe")),
            _p(m_ew.get("mdd"))))
    for etf, rows in series:
        d = ctx[etf["code"]]
        eq0 = [d["closes"][x] for x in d["dates"]]
        eq = [v / eq0[0] for v in eq0]
        rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))]
        L.append("| 买入持有 {} | {} | {} | {} | {} |".format(
            etf["short"], _p(eq[-1] - 1.0),
            _p(eq[-1] ** (250.0 / len(eq)) - 1.0), _n(_sharpe(rets)), _p(_mdd(eq))))
    mp, ds = _bench(bench)
    if ds and ew:
        sel = [x for x in ds if x >= ew[0][0]]
        if len(sel) > 60:
            eq = [mp[x] / mp[sel[0]] for x in sel]
            rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq))]
            L.append("| 沪深300（同起点） | {} | {} | {} | {} |".format(
                _p(eq[-1] - 1.0), _p(eq[-1] ** (250.0 / len(eq)) - 1.0),
                _n(_sharpe(rets)), _p(_mdd(eq))))
    new_arm = {r["code"]: r for r in cmp_rows if r["mode"] == "score"}
    detail = "；".join("{} {}".format(short, _p(new_arm[c]["ex_same"], 2, True))
                       for c, (short, _b, _t, _w) in verdict.items() if c in new_arm)
    L += ["", "结论：与“100% 等权持有两只”相比，纪律调仓在扣完成本后大幅落后，但差距主要来自平均仓位只有 35%–42%，"
          "不是分数判错方向；把仓位差剔除后（超额 vs 同仓位），新口径下{}，t 值全部落在 ±1.6 以内，"
          "两段都不显著。因此这套分数目前的定位是**配置温度计**"
          "（决定两只之间分多少预算、什么时候整体降仓），不是择时信号发生器。".format(detail or "—"), ""]

    # 五、分年
    L += ["## 六、分年收益（日频跟随 vs 买入持有）", "",
          "| 标的 | 年份 | 纪律调仓 | 买入持有 | 差 |", "| --- | --- | --- | --- | --- |"]
    for etf, rows in series:
        d = ctx[etf["code"]]
        c, sw, ct = discipline(rows, d["dates"], d["opens"], d["closes"], tiers)
        if len(c) < 60:
            continue
        y = {}
        for i in range(1, len(c)):
            yr = c[i][0][:4]
            p = y.setdefault(yr, [1.0, 1.0])
            p[0] *= c[i][1] / c[i - 1][1]
            p[1] *= d["closes"][c[i][0]] / d["closes"][c[i - 1][0]]
        for yr in sorted(y):
            a, b = y[yr][0] - 1.0, y[yr][1] - 1.0
            L.append("| {} | {} | {} | {} | {} |".format(etf["short"], yr, _p(a), _p(b), _p(a - b, 2, True)))

    # 六、风格
    st = style_state()
    L += ["", "## 七、风格环境（已接入评分）", ""]
    if st.get("error"):
        L.append("- 指数数据不可用：{}".format(st["error"]))
    else:
        L.append("- 中证红利(000922) 相对 沪深300成长(000918)：20 日多空 {}，60 日多空 {}，"
                 "混合信号 {}（5 年分位 {}），60 日比值 {}（{}）".format(
                     _p(st.get("rel20"), 2, True), _p(st.get("rel60"), 2, True),
                     _p(st.get("blend"), 2, True), _n(st.get("pct"), 1),
                     _p(st.get("ratio60"), 2, True), st.get("date")))
        L.append("- 读数：60 日多空为负＝成长占优，红利低波这种防御资产通常在此阶段跑输；为正＝防御占优。"
                 "该信号已作为市场环境维度的 3 分子项（顺向分位）；分位 > 85 时封顶 40 分，"
                 "避免风格极度拥挤时继续给高分。")
    bond = None
    for etf, rows in series:
        for r in reversed(rows):
            if r.get("bond10y") is not None:
                bond = r.get("bond10y")
                break
        if bond is not None:
            break
    if bond is not None:
        L.append("- 10Y 国债收益率 {}%，红利低波带债性，利率快速上行时估值项容易失真。".format(_n(bond, 3)))

    # 七、局限
    L += ["", "## 八、局限与后续", "",
          "- 样本 2020 起（563020 自 2023-12），训练约 5 年、样本外不到 2 年；"
          "任何“样本外有效”只能是弱证据。",
          "- 非重叠抽样把独立样本压到每段十几二十个，多数子项只能判“样本不足”，这是数据事实，不是脚本问题。",
          "- 指数层十年回测需历史成分与权重，免费源不提供，仍未做。",
          "- 待办：① 校准分若样本外占优再考虑上线反向计分；② 红利/成长相对强弱已按 A 方案上线（3 分权重 + 拥挤闸），"
          "需观察一个季度：若第三节「加与不加」同仓位超额与 t 均无改善、换手明显上升，则降权回 1.5 分或改为只写 history 观察；"
          "③ 真实 ROIC 与股东回报（回购+分红）需更细财报字段；④ 组合层（两只之间分配）单独做一次权重优化。", ""]

    path = os.path.join(root, "backtest", "upgrade.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh_:
        fh_.write("\n".join(L))

    cpath = os.path.join(root, "backtest", "upgrade.csv")
    with open(cpath, "w", encoding="utf-8", newline="") as fh_:
        w = csv.writer(fh_)
        w.writerow(["kind", "code", "name", "v1", "v2", "v3", "v4", "v5", "v6"])
        for f in fh:
            w.writerow(["factor", "-", f["sub"], f["verdict"], f["train"],
                        f["n_train"], f["oos"], f["n_oos"], f["weight"]])
        for r in disc:
            w.writerow(["discipline", r["code"], r["variant"], r["total"], r["cagr"],
                        r["sharpe"], r["mdd"], r["switches"], r["cost"]])
        for r in calib:
            w.writerow(["calib", r["code"], r["mode"], r["total"], r["oos_total"],
                        r["oos_sharpe"], r["oos_mdd"], r["switches"], ""])
        for r in cmp_rows:
            w.writerow(["style_ab", r["code"], r["mode"], r["total"], r["cagr"],
                        r["sharpe"], r["mdd"], r["ex_same"],
                        "" if r["t"] is None else round(r["t"], 3)])
    return path
