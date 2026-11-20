# -*- coding: utf-8 -*-
"""价格源切换（腾讯 / 新浪）与持仓加权 PE、PB 的归一化修正。

背景两个问题：
1. 东财 push2his 个股 K 线在云端批量拉取近百只成分股时会被限流（SSL 断连），
   导致大量成分股没有价格，PE/PB 只由极少数股票算出。
2. 原聚合把 ep 直接相加、却按「有价格的权重」以外的口径输出，
   覆盖不全时 1/ep 会被放大成几十倍（例如 PE=161），必须按覆盖权重重新归一化。

这里重写 build：价格改走腾讯（失败回退新浪、再回退东财），
ep / pb 均按「有价格的权重」归一化，并对聚合结果做合理性区间校验。
"""

import bisect
import json
import os
import time

from . import feed, pit

TX_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SINA_URL = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            "CN_MarketData.getKLineData")
PE_RANGE = (3.0, 60.0)
PB_RANGE = (0.3, 15.0)
KEYS = ("roe", "roa", "ocf_np", "icr", "debt", "np_growth", "np_stab", "payout", "fcf_cover")


def _symbol(code):
    return ("sh" if code[0] == "6" else "sz") + code


def tx_px(code):
    """腾讯前复权日线收盘价，约 1700 个交易日。"""
    symbol = _symbol(code)
    for i in range(3):
        try:
            raw = feed.http_get(TX_URL, {"param": "{},day,2019-01-01,2026-12-31,1700,"
                                         .format(symbol)}, timeout=20)
            node = ((json.loads(raw).get("data") or {}).get(symbol) or {})
            rows = node.get("day") or node.get("qfqday") or []
            out = {}
            for r in rows:
                try:
                    out[r[0]] = float(r[2])
                except (IndexError, TypeError, ValueError):
                    continue
            if out:
                return out
        except Exception:
            time.sleep(2 + 3 * i)
    return {}


def sina_px(code):
    """新浪日线（约 1000 个交易日），腾讯不可用时的备选。"""
    try:
        raw = feed.http_get(SINA_URL, {"symbol": _symbol(code), "scale": "240",
                                       "ma": "no", "datalen": "1023"}, timeout=20)
        rows = json.loads(raw)
        return {r["day"]: float(r["close"]) for r in rows if r.get("close")}
    except Exception:
        return {}


def _em_px(fetcher, code):
    secid = ("1." if code[0] == "6" else "0.") + code
    try:
        return {b["date"]: b["close"] for b in fetcher.bars(secid, 0)
                if b["date"] >= "2018-01-01"}
    except Exception:
        return {}


def _fetch_px(fetcher, code):
    """价格抓取链：腾讯 → 新浪 → 东财。"""
    for fn in (tx_px, sina_px):
        px = fn(code)
        if px:
            return px
    return _em_px(fetcher, code)


def invalidate_price_cache(fetcher):
    """清掉旧价格缓存，强制按新抓取链重取（只做一次）。"""
    folder = os.path.join(fetcher.cache_dir, "pit")
    if not os.path.isdir(folder):
        return
    for name in os.listdir(folder):
        if not name.startswith("pitstock_") or not name.endswith(".json"):
            continue
        path = os.path.join(folder, name)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                pack = json.load(fh)
        except Exception:
            continue
        if pack.get("px"):
            pack["px"] = {}
            pack["fetched_at"] = "2000-01-01"
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(pack, fh, ensure_ascii=False)
            except Exception:
                pass


def build(fetcher, cached_holdings, dates):
    """逐日 point-in-time 聚合持仓财务与估值（ep / pb 按覆盖权重归一化）。"""
    snaps = pit.snapshots(cached_holdings)
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
            packs[c] = pit._stock(fetcher, c)
        except Exception:
            packs[c] = None
    snap_dates = [s["date"] for s in snaps]
    out = {}
    for d in dates:
        k = bisect.bisect_right(snap_dates, d) - 1
        if k < 0:
            continue
        items = sorted(snaps[k]["items"], key=lambda x: -x["weight"])[:15]
        tw = sum(i["weight"] for i in items) or 1.0
        vals = {key: [] for key in KEYS}
        ep, ep_w = 0.0, 0.0
        pb_num, pb_den = 0.0, 0.0
        cov = 0.0
        for it in items:
            pack = packs.get(it["code"])
            w = it["weight"] / tw
            if not pack:
                continue
            m = pit._metrics(pack, d)
            if not m:
                continue
            if "银行" in (it.get("name") or "") or "保险" in (it.get("name") or ""):
                m["fcf_cover"] = None
                m["fcf_np"] = None
                m["ocf_np"] = None
            cov += w
            for key in KEYS:
                if m.get(key) is not None:
                    vals[key].append((w, m[key]))
            if m.get("ep"):
                ep += w * m["ep"]
                ep_w += w
            if m.get("pb"):
                pb_num += w * m["pb"]
                pb_den += w
        rec = {}
        for key, pairs in vals.items():
            ws = sum(a for a, _ in pairs)
            if ws > 0:
                rec[key] = sum(a * b for a, b in pairs) / ws
        pe = (ep_w / ep) if (ep > 0 and ep_w > 0) else None
        pb = (pb_num / pb_den) if pb_den > 0 else None
        if pe is not None and not (PE_RANGE[0] <= pe <= PE_RANGE[1]):
            rec["pe_raw"] = round(pe, 2)
            pe = None
        if pb is not None and not (PB_RANGE[0] <= pb <= PB_RANGE[1]):
            rec["pb_raw"] = round(pb, 2)
            pb = None
        rec["pe"] = pe
        rec["pb"] = pb
        rec["pe_cover"] = round(ep_w, 4)
        rec["pb_cover"] = round(pb_den, 4)
        rec["coverage"] = round(cov, 4)
        rec["stocks"] = len(items)
        rec["snap"] = snaps[k]["quarter"]
        rec["snap_date"] = snaps[k]["date"]
        out[d] = rec
    return out


def _save_pack(fetcher, code, pack):
    path = os.path.join(fetcher.cache_dir, "pit", "pitstock_{}.json".format(code))
    try:
        data = {k: v for k, v in pack.items()
                if k not in ("_cache", "_by_report", "_notices", "_px_dates")}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
    except Exception:
        pass


def patch_stock():
    """缓存里价格缺失或过短时强制重取，避免旧缓存把 PE/PB 锁死在少数股票上。"""
    orig = getattr(pit, "_stock_orig", pit._stock)
    pit._stock_orig = orig
    if getattr(orig, "_px_guarded", False):
        return

    def stock(fetcher, code):
        pack = orig(fetcher, code)
        px = pack.get("px") or {}
        if len(px) < 200 and not getattr(fetcher, "offline", False):
            new_px = _fetch_px(fetcher, code)
            if len(new_px) > len(px):
                pack["px"] = new_px
                pack.pop("_px_dates", None)
                _save_pack(fetcher, code, pack)
        return pack

    stock._px_guarded = True
    pit._stock = stock


def install(fetcher=None, clear_px=True):
    """在主编排里调用一次：替换价格源 + 替换聚合实现。"""
    pit._fetch_px = _fetch_px
    patch_stock()
    if fetcher is not None and clear_px:
        marker = os.path.join(fetcher.cache_dir, "pit", ".pxfix_v2")
        if not os.path.exists(marker):
            invalidate_price_cache(fetcher)
            try:
                os.makedirs(os.path.dirname(marker), exist_ok=True)
                with open(marker, "w", encoding="utf-8") as fh:
                    fh.write("price source switched to tencent/sina; ep/pb normalized\n")
            except Exception:
                pass
    pit.build = build
