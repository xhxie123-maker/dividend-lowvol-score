# -*- coding: utf-8 -*-
"""产物层：历史表、当日快照、每日日志、在线看板。"""

import csv
import json
import os
from datetime import datetime

from .action import CLS as TIER_CLS, action_of, band_text, legend_rows, tiers_of
from .model import DIMENSIONS, STATUS_LABEL, clamp, num_fmt

FIELDS = ["date", "code", "short", "close", "score", "rating", "action", "coverage",
          "div_attraction", "div_quality", "low_risk", "valuation", "financial", "market_env",
          "ttm_yield", "yield_avg3", "yield_pct5", "fwd_yield", "spread", "erp", "bond10y",
          "consec_years", "dps_growth", "div_cv", "payout", "fcf_cover", "roe", "roa",
          "ocf_np", "debt_ratio", "np_stab", "interest_cover", "pe", "pe_pct",
          "vol20", "vol60", "vol120", "vol250", "mdd250", "dd250", "downside250",
          "premium", "amount20_yi", "flow20", "mkt_vol", "crowding", "r20", "r60"]


def pct(x, nd=2):
    return "—" if x is None else "{:.{}f}%".format(x * 100, nd)


def pct_raw(x, nd=1):
    """已是百分数单位的数值（如资产负债率 49.6）直接加 %。"""
    return "—" if x is None else "{:.{}f}%".format(x, nd)


def row_to_dict(etf, r, bands):
    from .model import rating_of
    d = r["dim"]
    sub = r["sub"]

    def g(x, nd=4):
        return None if x is None else round(x, nd)

    out = {
        "date": r["date"], "code": etf["code"], "short": etf["short"],
        "close": round(r["close"], 3),
        "score": None if r["score"] is None else round(r["score"], 2),
        "rating": rating_of(r["score"], bands)["label"] if r["score"] is not None else "—",
        "action": action_of(r["score"], ACTIONS)["label"] if r["score"] is not None else "—",
        "coverage": round(r["coverage"], 3),
    }
    for k in ("div_attraction", "div_quality", "low_risk", "valuation", "financial", "market_env"):
        out[k] = None if d.get(k) is None else round(d[k], 2)
    out.update({
        "ttm_yield": g(r["ttm_yield"], 5), "yield_avg3": g(r["yield_avg3"], 5),
        "yield_pct5": g(r["yield_pct5"], 1), "fwd_yield": g(r["fwd_yield"], 5),
        "spread": g(r["spread"], 5), "erp": g(r["erp"], 5), "bond10y": g(r["bond10y"], 3),
        "consec_years": r["consec_years"], "dps_growth": g(r["dps_growth"], 5),
        "div_cv": g(r["div_cv"], 5), "payout": g(r.get("fin_payout"), 4),
        "fcf_cover": g(sub.get("fcf_cover"), 1), "roe": g(r.get("fin_roe"), 2),
        "roa": g(r.get("fin_roa"), 2), "ocf_np": g(r.get("fin_ocf_np"), 4),
        "debt_ratio": g(r.get("fin_debt"), 2), "np_stab": g(r.get("fin_np_stab"), 2),
        "interest_cover": g(r.get("fin_icr"), 2), "pe": g(r["pe"] (0) if False else g(r["pe"], 2), "pe_pct": g(sub.get("pe_pct"), 1),
    })
    return out
