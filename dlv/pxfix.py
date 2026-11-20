# -*- coding: utf-8 -*-
"""价格源切换（腾讯行情）与聚合 PE/PB 的合理性校验。

背景：东财 push2his K 线在云端批量拉取近百只成分股时会被限流（SSL 断连），
导致持仓加权 PE/PB 出现缺失或异常值。这里把个股价格改走腾讯免费接口，
并在聚合结果上做合理性区间校验（越界即按缺失处理，避免污染分位得分）。
"""

import json
import os

from . import feed, pit

TX_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
PE_RANGE = (3.0, 60.0)
PB_RANGE = (0.3, 15.0)


def tx_px(symbol):
    """腾讯日线（不复权收盘价），symbol 形如 sh600019 / sz000651。"""
    raw = feed.http_get(TX_URL, {"param": "{},day,2019-01-01,2026-12-31,1700,"
                                            .format(symbol)})
    js = json.loads(raw)
    node = ((js.get("data") or {}).get(symbol) or {})
    rows = node.get("day") or node.get("qfqday") or []
    out = {}
    for r in rows:
        try:
            out[r[0]] = float(r[2])
        except (IndexError, TypeError, ValueError):
            continue
    return out


def _fetch_px(fetcher, code):
    symbol = ("sh" if code[0] == "6" else "sz") + code
    try:
        px = tx_px(symbol)
    except Exception:
        px = {}
    if px:
        return px
    secid = ("1." if code[0] == "6" else "0.") + code
    try:
        return {b["date"]: b["close"] for b in fetcher.bars(secid, 0)
                if b["date"] >= "2018-01-01"}
    except Exception:
        return {}


def invalidate_price_cache(fetcher):
    """清掉旧的（东财来源）价格缓存，强制下一次按新源重取。"""
    folder = os.path.join(fetcher.cache_dir, "pit")
    if not os.path.isdir(folder):
        return
    for name in os.listdir(folder):
        if not name.endswith(".json"):
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


def install(fetcher=None, clear_px=True):
    """在主编排里调用一次：替换价格源 + 包装聚合校验。"""
    pit._fetch_px = _fetch_px
    if fetcher is not None and clear_px:
        marker = os.path.join(fetcher.cache_dir, ".pxfix_v1")
        if not os.path.exists(marker):
            invalidate_price_cache(fetcher)
            try:
                with open(marker, "w", encoding="utf-8") as fh:
                    fh.write("price source switched to tencent\n")
            except Exception:
                pass
    orig = pit.build
    if getattr(orig, "_guarded", False):
        return
    orig._guarded = True

    def build(*args, **kwargs):
        out = orig(*args, **kwargs)
        for rec in out.values():
            pe, pb = rec.get("pe"), rec.get("pb")
            if pe is not None and not (PE_RANGE[0] <= pe <= PE_RANGE[1]):
                rec["pe_raw"] = round(pe, 2)
                rec["pe"] = None
            if pb is not None and not (PB_RANGE[0] <= pb <= PB_RANGE[1]):
                rec["pb_raw"] = round(pb, 2)
                rec["pb"] = None
        return out

    pit.build = build
