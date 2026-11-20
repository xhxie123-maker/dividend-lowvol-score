# -*- coding: utf-8 -*-
"""PIT（point-in-time）修正层与口径统一。

按外部审查意见修正四类问题：
  1) 分红年限/稳定性前视：每个历史交易日只用当日及以前的分红记录；
  2) 持仓前视：历史持仓按「基金季报法定披露日」切换，不把最新一期权重套到
     全部历史（季报 ≤ 季度结束后 15 个工作日 → 取 4-30 / 8-31 / 10-31 / 次年 3-31）；
  3) 口径失真：自由现金流改用 CFO − CapEx（资本开支取上市公司现金流量表），
     覆盖率 = FCF / 现金分红总额；PE/PB 改为「前十大持仓加权」的一致口径，
     不再用 ETF 价格冒充 PE；估值维度剔除与股息吸引力重复的股息率分位；
  4) 数据可追溯：逐日记录当前生效的持仓报告期与披露日。

全部数据仍为公开免费接口：东财 F10 财务/分红、东财 K 线、新浪现金流量表。
"""

import bisect
import json
import os
import re
import urllib.request
from datetime import datetime, timedelta

from . import feed, model

# 基金定期报告法定披露截止日（按报告期末月份）
QUARTER_DEADLINE = {"03": ("-04-30", 0), "06": ("-08-31", 0),
                    "09": ("-10-31", 0), "12": ("-03-31", 1)}

SINA_CF = ("https://money.finance.sina.com.cn/corp/go.php/vDOWN_CashFlow/"
           "displaytype/4/stockid/{}/ctrl/all.phtml")


# ---------------------------------------------------------------------------
# 基础
# ---------------------------------------------------------------------------
def _gbk(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": feed.UA})
    for i in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("gbk", "replace")
        except Exception:
            if i == 2:
                raise


def _f(v):
    if v is None:
        return None
    s = str(v).replace(",", "").strip()
    if s in ("", "-", "--"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _days(a, b):
    return (datetime.strptime(b, "%Y-%m-%d") - datetime.strptime(a, "%Y-%m-%d")).days


# ---------------------------------------------------------------------------
# 持仓：按报告期抓全历史，按法定披露日切换（point-in-time）
# ---------------------------------------------------------------------------
def _parse_holdings_html(html):
    """从东财基金持仓页解析各报告期表格：{截止日: {items, total_weight}}。"""
    out = {}
    for m in re.finditer(r"截止至：<font[^>]*>(\d{4}-\d{2}-\d{2})</font>", html):
        date = m.group(1)
        start = html.find("<table", m.end())
        end = html.find("</table>", start)
        if start < 0 or end < 0:
            continue
        block = html[start:end]
        items = []
        for row in re.findall(r"<tr>.*?</tr>", block, re.S):
            cells = [re.sub(r"<[^>]+>", "", c).strip()
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
            code, name, w = None, "", None
            for i, c in enumerate(cells):
                if code is None and re.match(r"^\d{6}$", c):
                    code = c
                    name = cells[i + 1] if i + 1 < len(cells) else ""
                    continue
                if w is None and c.endswith("%"):
                    w = _f(c.replace("%", "").replace(",", ""))
            if code and w is not None:
                items.append({"code": code, "name": name, "weight": w})
        if items:
            out[date] = {"items": items,
                         "total_weight": round(sum(i["weight"] for i in items), 2)}
    return out


def holdings_history(fetcher, code):
    """逐年抓取基金前十大持仓历史，缓存为 {报告期末日: 快照}。"""
    path = os.path.join(fetcher.cache_dir, "holdall_{}.json".format(code))
    cached = {}
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
        except Exception:
            cached = {}
    now = datetime.now(feed.CST)
    if fetcher.offline and cached:
        return cached
    years = list(range(2018, now.year + 1))
    got_years = sorted({d[:4] for d in cached})
    for y in years:
        if str(y) in got_years and y < now.year:
            continue
        try:
            html = feed.http_get(feed.F10_URL,
                                 {"type": "jjcc", "code": code, "topline": "10",
                                  "year": str(y), "month": "6"},
                                 {"Referer": "https://fundf10.eastmoney.com/ccmx_{}.html".format(code)})
        except Exception:
            continue
        for d, snap in _parse_holdings_html(html).items():
            if d[:4] == str(y):
                cached[d] = snap
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cached, fh, ensure_ascii=False)
    except Exception:
        pass
    return cached


def snapshots(cached):
    """[{quarter,date(披露生效日),items,total_weight}] 按生效日升序。"""
    out = []
    for date, snap in (cached or {}).items():
        if not snap.get("items"):
            continue
        mm = date[5:7]
        suffix, plus = QUARTER_DEADLINE.get(mm, ("-12-31", 0))
        eff = "{}-{}".format(int(date[:4]) + plus, suffix.strip("-"))
        out.append({"quarter": "{}Q{}".format(date[:4], (int(mm) - 1) // 3 + 1),
                    "report_date": date, "date": eff,
                    "items": snap["items"],
                    "total_weight": snap.get("total_weight")})
    out.sort(key=lambda x: x["date"])
    return out


def as_of(snaps, date):
    if not snaps:
        return None
    k = bisect.bisect_right([s["date"] for s in snaps], date) - 1
    return snaps[k] if k >= 0 else None


# ---------------------------------------------------------------------------
# 个股数据包
# ---------------------------------------------------------------------------
def _sina_cashflow(code):
    try:
        text = _gbk(SINA_CF.format(code))
    except Exception:
        return {}, {}
    cfo, capex = {}, {}
    lines = text.splitlines()
    hdr = None
    for line in lines:
        if line.startswith("报表日期"):
            hdr = [x.strip() for x in line.split("\t")]
            break
    if not hdr:
        return {}, {}
    dates = hdr[1:]
    for line in lines:
        cells = [x for x in line.split("\t")]
        name = cells[0].strip() if cells else ""
        if name.startswith("经营活动产生的现金流量净额"):
            target = cfo
        elif name.startswith("购建固定资产") or name.startswith("购建固定资产、无形资产"):
            target = capex
        else:
            continue
        for i, d in enumerate(dates):
            if i + 1 >= len(cells):
                break
            v = _f(cells[i + 1])
            if v is not None and len(d) == 8:
                target["{}-{}-{}".format(d[:4], d[4:6], d[6:])] = v
    return cfo, capex


def _stock(fetcher, code):
    """缓存个股：财务（含 EPS/BPS/股本）、分红、现金流量、收盘价。"""
    path = os.path.join(fetcher.cache_dir, "pit", "pitstock_{}.json".format(code))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    pack = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                pack = json.load(fh)
        except Exception:
            pack = None
    if pack and fetcher.offline:
        return pack
    if pack:
        age = (datetime.now(feed.CST).replace(tzinfo=None)
               - datetime.strptime(pack.get("fetched_at", "2000-01-01"), "%Y-%m-%d")).days
        if age <= fetcher.refresh_days:
            return pack
    pack = pack or {"fin": [], "div": [], "cf": {}, "px": {}}
    pack["fin"] = _fetch_fin(code) or pack.get("fin", [])
    pack["div"] = _fetch_div(code) or pack.get("div", [])
    cfo, capex = ({}, {}) if fetcher.offline else _sina_cashflow(code)
    if cfo or capex:
        pack["cf"] = {"cfo": cfo, "capex": capex}
    if not fetcher.offline:
        px = _fetch_px(fetcher, code) or pack.get("px", {})
        if px:
            pack["px"] = px
    pack["fetched_at"] = datetime.now(feed.CST).strftime("%Y-%m-%d")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(pack, fh, ensure_ascii=False)
    except Exception:
        pass
    return pack


def _fetch_fin(code):
    try:
        raw = feed.http_get(feed.DC_URL, {
            "reportName": "RPT_F10_FINANCE_MAINFINADATA", "columns": "ALL",
            "filter": '(SECUCODE="{}")'.format(feed.secucode(code)),
            "pageSize": 60, "sortColumns": "REPORT_DATE", "sortTypes": "-1"})
        rows = ((json.loads(raw).get("result") or {}).get("data")) or []
    except Exception:
        return []
    out = []
    for r in rows:
        rd = (r.get("REPORT_DATE") or "")[:10]
        nd = (r.get("NOTICE_DATE") or "")[:10]
        if not rd or not nd:
            continue
        out.append({"report": rd, "notice": nd,
                    "roe": _f(r.get("ROEJQ")), "roa": _f(r.get("ZZCJLL")),
                    "ocf_np": _f(r.get("NCO_NETPROFIT")),
                    "icr": _f(r.get("INTEREST_COVERAGE_RATIO")),
                    "debt": _f(r.get("ZCFZL")), "np_growth": _f(r.get("PARENTNETPROFITTZ")),
                    "eps": _f(r.get("EPSJB")), "bps": _f(r.get("BPS")),
                    "shares": _f(r.get("TOTAL_SHARE"))})
    out.sort(key=lambda x: x["notice"])
    return out


def _fetch_div(code):
    try:
        raw = feed.http_get(feed.DC_URL, {
            "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
            "filter": '(SECUCODE="{}")'.format(feed.secucode(code)),
            "pageSize": 60, "sortColumns": "REPORT_DATE", "sortTypes": "-1"})
        rows = ((json.loads(raw).get("result") or {}).get("data")) or []
    except Exception:
        return []
    out = []
    for r in rows:
        cash10 = _f(r.get("PRETAX_BONUS_RMB"))
        exd = (r.get("EX_DIVIDEND_DATE") or "")[:10]
        prog = r.get("ASSIGN_PROGRESS") or ""
        if cash10 is None or cash10 <= 0:
            continue
        if "实施" not in prog and not exd:
            continue
        out.append({"ex_date": exd or (r.get("REPORT_DATE") or "")[:10],
                    "cash_ps": cash10 / 10.0})
    out.sort(key=lambda x: x["ex_date"])
    return out


def _fetch_px(fetcher, code):
    secid = ("1." if code[0] == "6" else "0.") + code
    try:
        bars = fetcher.bars(secid, 0)
    except Exception:
        return {}
    return {b["date"]: b["close"] for b in bars if b["date"] >= "2018-01-01"}


# ---------------------------------------------------------------------------
# 逐日指标
# ---------------------------------------------------------------------------
def _ttm(series, rd):
    v = series.get(rd)
    if v is None:
        return None
    if rd[5:] == "12-31":
        return v
    prev_year = str(int(rd[:4]) - 1)
    a = series.get(prev_year + "-12-31")
    b = series.get(prev_year + rd[4:])
    if a is None or b is None:
        return None
    return v + a - b


def _metrics(pack, date):
    """单只个股在 date 的 PIT 指标（只用 date 前已公告的报告）。"""
    fin = pack.get("fin") or []
    if not fin:
        return None
    notices = pack.get("_notices") or [r["notice"] for r in fin]
    k = bisect.bisect_right(notices, date) - 1
    if k < 0:
        return None
    rep = fin[k]
    by_report = pack.get("_by_report")
    if by_report is None:
        by_report = {r["report"]: r for r in fin}
        pack["_by_report"] = by_report
    cache = pack.setdefault("_cache", {})

    key = (rep["report"],)
    m = cache.get(key)
    if m is None:
        eps_ttm = _ttm({r["report"]: r.get("eps") for r in fin}, rep["report"])
        cfo_ttm = _ttm((pack.get("cf") or {}).get("cfo") or {}, rep["report"])
        capex_ttm = _ttm((pack.get("cf") or {}).get("capex") or {}, rep["report"])
        m = {"eps_ttm": eps_ttm, "cfo_ttm": cfo_ttm, "capex_ttm": capex_ttm}
        cache[key] = m

    shares = rep.get("shares")
    out = {k2: rep.get(k2) for k2 in ("roe", "roa", "ocf_np", "debt", "np_growth")}
    if rep.get("icr") is not None:
        out["icr"] = min(rep["icr"], 200.0)
    else:
        out["icr"] = None

    gs = [x["np_growth"] for x in fin[max(0, k - 7):k + 1] if x["np_growth"] is not None]
    if len(gs) >= 4:
        mu = sum(g) / len(gs) if False else sum(gs) / len(gs)
        out["np_stab"] = (sum((g - mu) ** 2 for g in gs) / len(gs)) ** 0.5
    else:
        out["np_stab"] = None

    # 每股分红 TTM（已实施、除息日 ≤ date）
    div = pack.get("div") or []
    ttm_ps = sum(x["cash_ps"] for x in div if 0 <= _days(x["ex_date"], date) <= 365)
    out["div_ps_ttm"] = ttm_ps
    eps_ttm, cfo_ttm, capex_ttm = m["eps_ttm"], m["cfo_ttm"], m["capex_ttm"]
    # 支付率 = 现金分红 / 净利润（每股口径即可，股本约去）
    out["payout"] = (ttm_ps / eps_ttm) if (eps_ttm and eps_ttm > 0 and ttm_ps) else None
    # 自由现金流覆盖率 = (CFO − CapEx) / 现金分红总额
    if cfo_ttm is not None and capex_ttm is not None and ttm_ps > 0 and shares:
        out["fcf_cover"] = (cfo_ttm - capex_ttm) / (ttm_ps * shares)
    else:
        out["fcf_cover"] = None
    # 现金流质量：自由现金流 / 净利润
    out["fcf_np"] = ((cfo_ttm - capex_ttm) / (eps_ttm * shares)
                     if (cfo_ttm is not None and capex_ttm is not None
                         and eps_ttm and eps_ttm > 0 and shares) else None)
    out["ocf_np"] = out["fcf_np"] if out["fcf_np"] is not None else out.get("ocf_np")

    px_map = pack.get("px") or {}
    price = None
    if px_map:
        pdates = pack.get("_px_dates")
        if pdates is None:
            pdates = sorted(px_map)
            pack["_px_dates"] = pdates
        j = bisect.bisect_right(pdates, date) - 1
        if j >= 0:
            price = px_map[pdates[j]]
    out["ep"] = (eps_ttm / price) if (eps_ttm and eps_ttm > 0 and price) else None
    out["pb"] = (price / rep["bps"]) if (price and rep.get("bps")) else None
    return out


def build(fetcher, cached_holdings, dates):
    """逐日 point-in-time 聚合持仓财务与估值。返回 {date: {...}}。"""
    snaps = snapshots(cached_holdings)
    if not snaps:
        return {}
    codes = []
    for s in snaps:
        for it in sorted(s["items"], key=lambda x: -x["weight"])[:15]:
            if it["code"] not in codes:
                codes.append(it["code"])
    packs = {}
    for c in codes:
        try:
            packs[c] = _stock(fetcher, c)
        except Exception:
            packs[c] = None
    snap_dates = [s["date"] for s in snaps]
    out = {}
    keys = ("roe", "roa", "ocf_np", "icr", "debt", "np_growth", "np_stab", "payout", "fcf_cover")
    for d in dates:
        k = bisect.bisect_right(snap_dates, d) - 1
        if k < 0:
            continue
        items = sorted(snaps[k]["items"], key=lambda x: -x["weight"])[:15]
        tw = sum(i["weight"] for i in items) or 1.0
        vals = {key: [] for key in keys}
        ep = 0.0
        pb_num = pb_den = 0.0
        cov = 0.0
        for it in items:
            pack = packs.get(it["code"])
            w = it["weight"] / tw
            if not pack:
                continue
            m = _metrics(pack, d)
            if not m:
                continue
            # 金融股的自由现金流口径失真，银行/保险该子项按缺失处理
            if "银行" in (it.get("name") or "") or "保险" in (it.get("name") or ""):
                m["fcf_cover"] = None
                m["fcf_np"] = None
                m["ocf_np"] = None
            cov += w
            for key in keys:
                if m.get(key) is not None:
                    vals[key].append((w, m[key]))
            if m.get("ep"):
                ep += w * m["ep"]
            if m.get("pb"):
                pb_num += w * m["pb"]
                pb_den += w
        rec = {}
        for key, pairs in vals.items():
            ws = sum(a for a, _ in pairs)
            if ws > 0:
                rec[key] = sum(a * b for a, b in pairs) / ws
        rec["pe"] = (1.0 / ep) if ep > 0 else None
        rec["pb"] = (pb_num / pb_den) if pb_den > 0 else None
        rec["coverage"] = round(cov, 4)
        rec["stocks"] = len(items)
        rec["snap"] = snaps[k]["quarter"]
        rec["snap_date"] = snaps[k]["date"]
        out[d] = rec
    return out


# ---------------------------------------------------------------------------
# 分红类指标的 PIT 修正
# ---------------------------------------------------------------------------
def fix_dividends(rows, nav):
    """连续分红年限、分红稳定性改为逐日只用当日及以前的分红记录。"""
    paid = sorted((d, v["fhfcz"]) for d, v in (nav or {}).items()
                  if v and v.get("fhfcz"))
    idx, acc = 0, {}
    for r in sorted(rows, key=lambda x: x["date"]):
        d = r["date"]
        while idx < len(paid) and paid[idx][0] <= d:
            y = paid[idx][0][:4]
            acc[y] = acc.get(y, 0.0) + paid[idx][1]
            idx += 1
        cur_year = d[:4]
        done = {y: v for y, v in acc.items() if y < cur_year and v > 0}
        prev = str(int(cur_year) - 1)
        n = 0
        if prev in done:
            y = int(cur_year) - 1
            while str(y) in done:
                n += 1
                y -= 1
        r["consec_years"] = n
        if len(done) >= 3:
            vals = [done[y] for y in sorted(done)[-5:]]
            mu = sum(vals) / len(vals)
            sd = model.stdev(vals)
            if mu > 0 and sd is not None:
                r["div_cv"] = sd / mu
    return rows


# ---------------------------------------------------------------------------
# 维度定义修正（剔除重复计分，PB 接入）
# ---------------------------------------------------------------------------
VALUATION_SUBS = [
    ("pe_pct", "PE 历史分位数（持仓加权）", 5, "live"),
    ("pb_pct", "PB 历史分位数（持仓加权）", 5, "live"),
    ("erp", "股权风险溢价（EP − 10Y）", 5, "live"),
]


def patch_dimensions():
    """估值维度去掉与股息吸引力重复的股息率分位；财务子项名称按真实口径重写。"""
    for i, (key, name, dw, subs) in enumerate(model.DIMENSIONS):
        if key == "valuation":
            model.DIMENSIONS[i] = ("valuation", "估值合理性", 15, list(VALUATION_SUBS))
        elif key == "financial":
            new = []
            for sk, sn, sw, st in subs:
                if sk == "roic":
                    new.append((sk, "ROA（ROIC 代理，无免费 ROIC）", sw, st))
                elif sk == "ocf_np":
                    new.append((sk, "自由现金流/净利润（CFO−CapEx）", sw, st))
                elif sk == "interest_cover":
                    new.append((sk, "利息保障倍数（金融股不披露）", sw, st))
                else:
                    new.append((sk, sn, sw, st))
            model.DIMENSIONS[i] = (key, name, dw, new)


def finalize(rows, windows):
    """PB 分位补算 + 总分/覆盖率重算（PB 由持仓加权 PB 序列得出）。"""
    win = windows["percentile_window"]
    pb = [r.get("pb") for r in rows]
    has_pb = any(v is not None for v in pb)
    for i, r in enumerate(rows):
        r.setdefault("sub", {})
        if has_pb:
            r["sub"]["pb_pct"] = model.pct_rank(pb, i, win, -1, 120)
    for r in rows:
        dims, cov, missing = {}, {}, []
        for key, name, dw, subs in model.DIMENSIONS:
            tot_w, acc = 0.0, 0.0
            for sk, sn, sw, st in subs:
                v = (r.get("sub") or {}).get(sk)
                if v is None:
                    missing.append("{}.{}".format(key, sk))
                else:
                    acc += sw * v
                    tot_w += sw
            total_sub_w = sum(x[2] for x in subs)
            cov[key] = tot_w / float(total_sub_w)
            dims[key] = None if tot_w == 0 else acc / tot_w
        total_w = sum(dw for k, n, dw, s in model.DIMENSIONS if dims[k] is not None)
        total = sum(dw * dims[k] for k, n, dw, s in model.DIMENSIONS if dims[k] is not None)
        got_w = sum(sw for k, n, dw, subs in model.DIMENSIONS
                    for sk, sn, sw, st in subs if (r.get("sub") or {}).get(sk) is not None)
        all_w = sum(sw for k, n, dw, subs in model.DIMENSIONS for sk, sn, sw, st in subs)
        r["dim"] = dims
        r["dim_cov"] = cov
        r["score"] = None if total_w == 0 else total / total_w
        r["coverage"] = got_w / float(all_w)
        r["missing"] = missing
    return rows


def sub_count():
    return sum(len(subs) for k, n, w, subs in model.DIMENSIONS)
