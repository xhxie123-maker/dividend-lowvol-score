# -*- coding: utf-8 -*-
"""数据接入层：全部使用公开免费接口，全部带本地缓存。

实测可用接口：
  日线            push2his.eastmoney.com/api/qt/stock/kline/get
  单位净值/每份分红 api.fund.eastmoney.com/f10/lsjz（需 Referer，pageSize=20）
  场内份额         push2.eastmoney.com/api/qt/stock/get (f84)
  10Y 国债收益率   datacenter-web.eastmoney.com RPTA_WEB_TREASURYYIELD（列 EMM00166466）
  指数 PE          中证指数 csindex-home/perf/index-perf（字段 peg）
  前十大持仓       fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc（需 Referer）
  个股财务指标     RPT_F10_FINANCE_MAINFINADATA
  个股分红         RPT_SHAREBONUS_DET（PRETAX_BONUS_RMB 为每 10 股派现，元）
"""

import bisect
import csv
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
QUOTE_URL = "https://push2.eastmoney.com/api/qt/stock/get"
DC_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
CS_PERF = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
F10_URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"

BOND_COL = "EMM00166466"
_FORCE_CURL = False


def _curl_get(url, headers=None, timeout=25):
    cmd = ["curl", "-sS", "-L", "--max-time", str(timeout), "-A", UA]
    for k, v in (headers or {}).items():
        cmd += ["-H", "{}: {}".format(k, v)]
    cmd.append(url)
    res = subprocess.run(cmd, capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError("curl 失败 rc={} {}".format(res.returncode, (res.stderr or "")[:150]))
    return res.stdout


def http_get(url, params=None, headers=None, timeout=25, tries=3):
    global _FORCE_CURL
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    if not _FORCE_CURL:
        for i in range(tries):
            try:
                req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
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
            time.sleep(1.5 + i)


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def secucode(code):
    return code + (".SH" if code.startswith(("6", "5", "9")) else ".SZ")


class Fetcher(object):
    def __init__(self, root, offline=False, refresh_days=3):
        self.root = root
        self.offline = offline
        self.refresh_days = refresh_days
        self.cache_dir = os.path.join(root, "data", "cache")
        os.makedirs(self.cache_dir, exist_ok=True)
        self.notes = []

    def note(self, msg):
        self.notes.append(msg)

    # ------------------------- 通用数值序列缓存 -------------------------
    def _load_map(self, name):
        path = os.path.join(self.cache_dir, name)
        out = {}
        if not os.path.exists(path):
            return out
        with open(path, "r", encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                out[row["date"]] = row.get("value", "")
        return out

    def _save_map(self, name, data):
        path = os.path.join(self.cache_dir, name)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "value"])
            for d in sorted(data):
                w.writerow([d, "" if data[d] is None else data[d]])

    def _to_floats(self, m):
        return {d: float(v) for d, v in m.items() if v not in ("", None)}

    # ------------------------- 行情 -------------------------
    def bars(self, secid, fqt=1):
        params = {"secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
                  "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                  "klt": "101", "fqt": str(fqt), "beg": "19900101", "end": "20500101"}
        js = json.loads(http_get(KLINE_URL, params))
        data = js.get("data") or {}
        out = []
        for line in data.get("klines", []):
            f = line.split(",")
            out.append({"date": f[0], "close": float(f[2]), "amount": float(f[6])})
        if not out:
            raise RuntimeError("K 线为空 {}".format(secid))
        return out

    # ------------------------- 净值与每份分红 -------------------------
    def nav(self, code, need_from):
        path = os.path.join(self.cache_dir, "nav_{}.csv".format(code))
        rows = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    rows[row["date"]] = {"dwjz": _f(row["dwjz"]),
                                         "fhfcz": _f(row["fhfcz"]) or 0.0}
        if self.offline and rows:
            return rows
        page = 1
        while page <= 140:
            raw = http_get(NAV_URL, {"fundCode": code, "pageIndex": page, "pageSize": 20},
                           {"Referer": "https://fundf10.eastmoney.com/"})
            batch = (json.loads(raw).get("Data") or {}).get("LSJZList") or []
            if not batch:
                break
            for item in batch:
                d = (item.get("FSRQ") or "").strip()
                if d and d not in rows:
                    rows[d] = {"dwjz": _f(item.get("DWJZ")),
                               "fhfcz": _f(item.get("FHFCZ")) or 0.0}
            if min(rows) <= need_from or len(batch) < 20:
                break
            page += 1
            time.sleep(0.12)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "dwjz", "fhfcz"])
            for d in sorted(rows):
                w.writerow([d, "" if rows[d]["dwjz"] is None else rows[d]["dwjz"], rows[d]["fhfcz"]])
        return rows

    # ------------------------- 场内份额 -------------------------
    def shares(self, code, secid, date):
        path = os.path.join(self.cache_dir, "shares_{}.csv".format(code))
        rows = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", newline="") as fh:
                for row in csv.DictReader(fh):
                    rows[row["date"]] = _f(row["shares"])
        if self.offline and rows:
            return rows
        try:
            js = json.loads(http_get(QUOTE_URL, {"secid": secid, "fields": "f84,f85"}))
            v = (js.get("data") or {}).get("f84")
            if v:
                rows[date] = float(v)
        except Exception as exc:
            self.note("份额抓取失败 {}：{}".format(code, exc))
        with open(path, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "shares"])
            for d in sorted(rows):
                w.writerow([d, "" if rows[d] is None else rows[d]])
        return rows

    # ------------------------- 10Y 国债收益率 -------------------------
    def bond10y(self):
        cached = self._load_map("bond10y.csv")
        if self.offline and cached:
            return self._to_floats(cached)
        try:
            raw = http_get(DC_URL, {"reportName": "RPTA_WEB_TREASURYYIELD", "columns": "ALL",
                                    "pageSize": 4000, "sortColumns": "SOLAR_DATE", "sortTypes": "-1"})
            for row in ((json.loads(raw).get("result") or {}).get("data")) or []:
                d = (row.get("SOLAR_DATE") or "")[:10]
                v = row.get(BOND_COL)
                if d and v not in (None, "", "-"):
                    cached[d] = v
            self._save_map("bond10y.csv", cached)
        except Exception as exc:
            self.note("10Y 国债抓取失败：{}".format(exc))
        return self._to_floats(cached)

    # ------------------------- 指数 PE（中证官方） -------------------------
    def index_pe(self, index_code):
        name = "pe_{}.csv".format(index_code)
        cached = self._load_map(name)
        if self.offline and cached:
            return self._to_floats(cached)
        now = datetime.now(CST)
        try:
            raw = http_get(CS_PERF, {"indexCode": index_code,
                                     "startDate": (now - timedelta(days=3600)).strftime("%Y%m%d"),
                                     "endDate": now.strftime("%Y%m%d")})
            for row in (json.loads(raw).get("data") or []):
                d = row.get("tradeDate") or ""
                if len(d) == 8:
                    d = "{}-{}-{}".format(d[:4], d[4:6], d[6:])
                v = row.get("peg")
                if d and v:
                    cached[d] = v
            self._save_map(name, cached)
        except Exception as exc:
            self.note("指数 PE 抓取失败 {}：{}".format(index_code, exc))
        return self._to_floats(cached)

    # ------------------------- 前十大持仓 -------------------------
    def holdings(self, code):
        path = os.path.join(self.cache_dir, "holdings_{}.json".format(code))
        cached = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                cached = json.load(fh)
        if self.offline and cached:
            return cached
        now = datetime.now(CST)
        for back in range(0, 8):
            y, m = now.year, now.month - back * 3
            while m <= 0:
                m += 12
                y -= 1
            key = "{}-Q{}".format(y, (m - 1) // 3 + 1)
            if key in cached:
                continue
            try:
                html = http_get(F10_URL, {"type": "jjcc", "code": code, "topline": "10",
                                          "year": str(y), "month": str(m)},
                                {"Referer": "https://fundf10.eastmoney.com/ccmx_{}.html".format(code)})
            except Exception:
                continue
            if "content" not in html:
                continue
            items, total = [], 0.0
            for row in re.findall(r"<tr>.*?</tr>", html, re.S):
                cells = [re.sub(r"<[^>]+>", "", c).strip()
                         for c in re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)]
                if len(cells) < 8 or not re.match(r"^\d{6}$", cells[1]):
                    continue
                w = _f(cells[6].replace("%", ""))
                if w is None:
                    continue
                items.append({"code": cells[1], "name": cells[2], "weight": w})
                total += w
            if items:
                cached[key] = {"quarter": key, "total_weight": round(total, 2),
                               "items": items, "fetched_at": now.strftime("%Y-%m-%d")}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(cached, fh, ensure_ascii=False, indent=1)
        return cached

    # ------------------------- 个股财务 / 分红 -------------------------
    def _fund_cache(self, code):
        path = os.path.join(self.cache_dir, "fund_{}.json".format(code))
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            stamp = datetime.strptime(data.get("fetched_at", "2000-01-01"), "%Y-%m-%d").date()
            if (datetime.now(CST).date() - stamp).days <= self.refresh_days:
                return data
        except Exception:
            return None
        return None

    def stock_bundle(self, code):
        cached = self._fund_cache(code)
        if cached or self.offline:
            return cached or {"fin": [], "div": []}
        fin, div = [], []
        try:
            raw = http_get(DC_URL, {"reportName": "RPT_F10_FINANCE_MAINFINADATA", "columns": "ALL",
                                    "filter": '(SECUCODE="%s")' % secucode(code),
                                    "pageSize": 40, "sortColumns": "REPORT_DATE", "sortTypes": "-1"})
            for r in ((json.loads(raw).get("result") or {}).get("data")) or []:
                rd, nd = (r.get("REPORT_DATE") or "")[:10], (r.get("NOTICE_DATE") or "")[:10]
                if not rd or not nd:
                    continue
                fin.append({"report": rd, "notice": nd, "type": r.get("REPORT_TYPE") or "",
                            "roe": _f(r.get("ROEJQ")), "roa": _f(r.get("ZZCJLL")),
                            "ocf_np": _f(r.get("NCO_NETPROFIT")), "icr": _f(r.get("INTEREST_COVERAGE_RATIO")),
                            "debt": _f(r.get("ZCFZL")), "np_growth": _f(r.get("PARENTNETPROFITTZ")),
                            "eps": _f(r.get("EPSJB"))})
        except Exception as exc:
            self.note("{} 财务抓取失败：{}".format(code, exc))
        try:
            raw = http_get(DC_URL, {"reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
                                    "filter": '(SECUCODE="%s")' % secucode(code),
                                    "pageSize": 40, "sortColumns": "REPORT_DATE", "sortTypes": "-1"})
            for r in ((json.loads(raw).get("result") or {}).get("data")) or []:
                cash10 = _f(r.get("PRETAX_BONUS_RMB"))
                exd = (r.get("EX_DIVIDEND_DATE") or "")[:10]
                prog = r.get("ASSIGN_PROGRESS") or ""
                if cash10 is None or cash10 <= 0:
                    continue
                if "实施" not in prog and not exd:
                    continue
                div.append({"report": (r.get("REPORT_DATE") or "")[:10],
                            "ex_date": exd or (r.get("REPORT_DATE") or "")[:10],
                            "cash_ps": cash10 / 10.0})
        except Exception as exc:
            self.note("{} 分红抓取失败：{}".format(code, exc))
        fin.sort(key=lambda x: x["notice"])
        div.sort(key=lambda x: x["ex_date"])
        payload = {"fin": fin, "div": div, "fetched_at": datetime.now(CST).strftime("%Y-%m-%d")}
        with open(os.path.join(self.cache_dir, "fund_{}.json".format(code)), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        return payload


def latest_holdings(cached):
    """取最近一期已披露的前十大持仓。"""
    if not cached:
        return None
    return cached[sorted(cached)[-1]]


def financial_proxy(fetcher, hold, dates):
    """按前十大持仓权重聚合成分股财务，逐日 point-in-time（只用已公告报告）。"""
    if not hold or not hold.get("items"):
        return {}
    items = hold["items"]
    total_w = sum(i["weight"] for i in items) or 1.0
    stocks = []
    for it in items:
        bundle = fetcher.stock_bundle(it["code"])
        if not bundle or not bundle.get("fin"):
            continue
        stocks.append({"code": it["code"], "name": it["name"],
                       "w": it["weight"] / total_w,
                       "fin": bundle["fin"], "div": bundle.get("div") or [],
                       "notice": [r["notice"] for r in bundle["fin"]],
                       "exd": [r["ex_date"] for r in bundle.get("div") or []]})
    if not stocks:
        return {}

    keys = ("roe", "roa", "ocf_np", "icr", "debt", "np_growth", "payout", "np_stab")
    out = {}
    for d in dates:
        vals = {k: [] for k in keys}
        covered = 0.0
        for st in stocks:
            k = bisect.bisect_right(st["notice"], d) - 1
            if k < 0:
                continue
            rep = st["fin"][k]
            hit = False
            for key, field in (("roe", "roe"), ("roa", "roa"), ("ocf_np", "ocf_np"),
                               ("debt", "debt"), ("np_growth", "np_growth")):
                v = rep.get(field)
                if v is not None:
                    vals[key].append((st["w"], v))
                    hit = True
            if rep.get("icr") is not None:
                vals["icr"].append((st["w"], min(rep["icr"], 200.0)))
            # 净利润增长稳定性：过去 8 期同比增速的标准差
            gs = [x["np_growth"] for x in st["fin"][max(0, k - 7):k + 1] if x["np_growth"] is not None]
            if len(gs) >= 4:
                mu = sum(gs) / len(gs)
                sd = (sum((g - mu) ** 2 for g in gs) / len(gs)) ** 0.5
                vals["np_stab"].append((st["w"], sd))
            # 支付率：TTM 每份分红 / 最近年报 EPS
            if st["div"] and rep.get("eps") and rep["eps"] > 0:
                ks = bisect.bisect_right(st["exd"], d) - 1
                if ks >= 0:
                    ttm = sum(x["cash_ps"] for x in st["div"]
                              if 0 <= (datetime.strptime(d, "%Y-%m-%d") -
                                       datetime.strptime(x["ex_date"], "%Y-%m-%d")).days <= 365)
                    if ttm > 0:
                        vals["payout"].append((st["w"], ttm / rep["eps"]))
            if hit:
                covered += st["w"]
        rec = {}
        for key, pairs in vals.items():
            wsum = sum(p[0] for p in pairs)
            if wsum > 0:
                rec[key] = sum(p[0] * p[1] for p in pairs) / wsum
        rec["coverage"] = round(covered, 4)
        rec["stocks"] = len(stocks)
        out[d] = rec
    return out
