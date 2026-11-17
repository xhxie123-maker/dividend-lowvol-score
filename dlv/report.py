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
        "interest_cover": g(r.get("fin_icr"), 2), "pe": g(r["pe"], 2), "pe_pct": g(sub.get("pe_pct"), 1),
        "vol20": g(r["vol20"], 5), "vol60": g(r["vol60"], 5), "vol120": g(r["vol120"], 5),
        "vol250": g(r["vol250"], 5), "mdd250": g(r["mdd250"], 5), "dd250": g(r["dd250"], 5),
        "downside250": g(r["downside250"], 5), "premium": g(r["premium"], 5),
        "amount20_yi": None if r["amount20"] is None else round(r["amount20"] / 1e8, 3),
        "flow20": g(r["flow20"], 5), "mkt_vol": g(r["mkt_vol"], 5),
        "crowding": None if r.get("amount20") is None or not r.get("bench_amount20")
        else round(r["amount20"] / r["bench_amount20"], 4),
        "r20": g(r["r20"], 5), "r60": g(r["r60"], 5),
    })
    return out


def write_csv(root, all_series, bands, backfill):
    path = os.path.join(root, "data", "daily_scores.csv")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fresh = []
    for etf, rows in all_series:
        for r in rows[-backfill:]:
            if r["score"] is not None:
                fresh.append((r["date"], etf["code"], row_to_dict(etf, r, bands)))
    old = []
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", newline="") as fh:
            old = list(csv.DictReader(fh))
    if fresh:
        cutoff = min(x[0] for x in fresh)
        old = [r for r in old if r["date"] < cutoff]
    merged = list(old) + [{k: ("" if v is None else v) for k, v in row.items()} for _, _, row in fresh]
    merged.sort(key=lambda r: (r["date"], r["code"]))
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for r in merged:
            w.writerow({k: r.get(k, "") for k in FIELDS})
    return merged


def write_snapshot(root, payload, data_date, run_at):
    path = os.path.join(root, "data", "history", "{}.json".format(data_date))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"run_at": run_at, "data_date": data_date, "etfs": payload},
                  fh, ensure_ascii=False, indent=2, default=str)
    return path


def write_log(root, all_series, alerts, data_date, run_at, errors, coverage_stats, etf_states=None):
    states_map = {etf["code"]: st for etf, st in (etf_states or [])}
    path = os.path.join(root, "logs", "{}.md".format(data_date))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    L = []

    def add(s=""):
        L.append(s)

    add("# 红利低波 ETF 监测评分 · {}".format(data_date))
    add()
    add("- 生成时间：{}（北京时间）".format(run_at))
    add("- 数据日期：{}（最新交易日收盘）".format(data_date))
    add("- 监测标的：{}".format("、".join("{} {}".format(e["short"], e["code"]) for e, _ in all_series)))
    add("- 评分体系：六维 100 分（股息吸引力 20 / 分红质量 20 / 低波动 20 / 估值 15 / 财务质量 15 / 市场环境 10）")
    add()
    add("## 一、评分总览")
    add()
    add("| 标的 | 代码 | 收盘价 | 综合评分 | 四档操作建议 | 较前一日 | 数据覆盖率 |")
    add("| --- | --- | --- | --- | --- | --- | --- |")
    for etf, rows in all_series:
        r = rows[-1]
        prev = [x for x in rows if x["score"] is not None][-2:]
        delta = None if len(prev) < 2 else prev[-1]["score"] - prev[-2]["score"]
        add("| {} | {} | {} | {} | {} | {} | {:.0f}% |".format(
            etf["short"], etf["code"], num_fmt(r["close"], 3), num_fmt(r["score"], 1),
            "—" if r["score"] is None else action_of(r["score"], ACTIONS)["label"],
            "—" if delta is None else "{:+.1f}".format(delta), r["coverage"] * 100))
    add()
    add("四档口径：{}（区间 / 档位），动作与仓位见第四节。".format(band_text(ACTIONS)))
    add("五档评级（仅描述用）：85–100 非常有吸引力 / 70–84 较有吸引力 / 60–69 中性偏积极 / 40–59 中性偏谨慎 / 0–39 吸引力较低。")
    add()

    add("## 二、六维拆解")
    add()
    for etf, rows in all_series:
        r = rows[-1]
        add("### {}（{}）".format(etf["short"], etf["code"]))
        add()
        st = states_map.get(etf["code"], {})
        add("| 维度 | 权重 | 得分 | 维度覆盖率 | 子项（状态·分值） |")
        add("| --- | --- | --- | --- | --- |")
        for key, name, dw, subs in DIMENSIONS:
            txt = "；".join("{} {} {}分".format(sn, STATUS_LABEL[st.get((key, sk), sstatus)], sw)
                           for sk, sn, sw, sstatus in subs)
            add("| {} | {} | {} | {:.0f}% | {} |".format(
                name, dw, num_fmt(r["dim"].get(key), 1), r["dim_cov"][key] * 100, txt))
        add()
        add("关键指标：")
        add()
        add("- 不复权收盘价 {}，单位净值 {}（{}），折溢价 {}".format(
            num_fmt(r["close_raw"], 3), num_fmt(r["nav"], 4), r["nav_date"] or "缺失", pct(r["premium"])))
        add("- TTM 现金分红率 {}（每份 {} 元），3 年平均 {}，5 年分位 {}".format(
            pct(r["ttm_yield"]), num_fmt(r["ttm_div"], 4), pct(r["yield_avg3"]), num_fmt(r["yield_pct5"], 0)))
        add("- 股债收益差 {}，股权风险溢价 {}，10Y 国债 {}".format(
            pct(r["spread"]), pct(r["erp"]), "—" if r["bond10y"] is None else "{:.3f}%".format(r["bond10y"])))
        add("- 连续分红 {} 年，每股股息年化 {}，分红 CV {}".format(
            r["consec_years"], pct(r["dps_growth"]), pct(r["div_cv"])))
        add("- 成分股财务（前十大加权）：ROE {} / ROA {} / 经营现金流÷净利润 {} / 资产负债率 {} / 利息保障 {} / 支付率 {} / 净利润增速标准差 {}".format(
            num_fmt(r.get("fin_roe"), 1), num_fmt(r.get("fin_roa"), 2), num_fmt(r.get("fin_ocf_np"), 2),
            pct_raw(r.get("fin_debt")), num_fmt(r.get("fin_icr"), 1), pct(r.get("fin_payout"), 1),
            num_fmt(r.get("fin_np_stab"), 1)))
        add("- 年化波动：20 日 {} / 60 日 {} / 120 日 {} / 250 日 {}；250 日最大回撤 {}，距高点 {}，下行波动 {}".format(
            pct(r["vol20"]), pct(r["vol60"]), pct(r["vol120"]), pct(r["vol250"]),
            pct(r["mdd250"]), pct(r["dd250"]), pct(r["downside250"])))
        add("- 20 日均成交 {} 亿元，ETF 份额 20 日变化 {}，拥挤度（成交额/沪深300）{}".format(
            num_fmt(None if r["amount20"] is None else r["amount20"] / 1e8, 2),
            pct(r["flow20"]), num_fmt(row_to_dict(etf, r, BANDS)["crowding"], 4)))
        add()
        add("子项标准化得分（窗口内百分位 0–100）：")
        add()
        add("| 维度 | 子项 | 分值 | 得分 |")
        add("| --- | --- | --- | --- |")
        for key, name, dw, subs in DIMENSIONS:
            for sk, sn, sw, st in subs:
                v = r["sub"].get(sk)
                add("| {} | {} | {} | {} |".format(name, sn, sw, "—" if v is None else num_fmt(v, 1)))
        add()

    add("## 三、风险预警")
    add()
    add("| 级别 | 项目 | 说明 |")
    add("| --- | --- | --- |")
    for a in alerts:
        add("| {} | {} | {} |".format(a["level"], a["title"], a["detail"]))
    add()
    add("预警独立于综合分：综合分高不等于无风险，触发高等级预警时单独立项核查。")
    add()

    add("## 四、四档操作建议")
    add()
    add("四档把综合评分换算成仓位纪律，区间为 {}。".format(band_text(ACTIONS)))
    add()
    add("| 档位 | 分数区间 | 参考仓位 | 动作要点 |")
    add("| --- | --- | --- | --- |")
    for row in legend_rows(ACTIONS):
        add("| {} | {} | {} | {} |".format(row["label"], row["range"], row["position"], row["action"]))
    add()
    add("当前档位：")
    add()
    for etf, rows in all_series:
        r = rows[-1]
        if r["score"] is None:
            add("- {}（{}）：无评分".format(etf["short"], etf["code"]))
            continue
        t = action_of(r["score"], ACTIONS)
        add("- **{}（{}）**：综合评分 {} → {}，参考仓位 {}；{}".format(
            etf["short"], etf["code"], num_fmt(r["score"], 1), t["label"], t["position"], t["action"]))
    add()
    add("调档规则：跨档且连续 5 个交易日维持新档位才执行，单日跳档视为噪声；档位只调仓位，不改买卖时点。")
    add("回测提示：档位与未来收益并非严格单调（详见回测报告第二节），因此仓位倾斜限定在相邻档 ±20 个百分点，不做反向操作。")
    add()

    add("## 五、横向对比")
    add()
    ranked = sorted([(rows[-1]["score"], etf, rows) for etf, rows in all_series],
                    key=lambda t: t[0], reverse=True)
    add("- 评分排序：{}（{}） > {}（{}）".format(
        ranked[0][1]["short"], num_fmt(ranked[0][0], 1), ranked[1][1]["short"], num_fmt(ranked[1][0], 1)))
    diff = ranked[0][0] - ranked[1][0]
    add("- 分差 {} 分。{}".format(num_fmt(diff, 1),
                              "差距超过 5 分，同等条件下优先高评分标的。" if diff >= 5
                              else "差距在 5 分内，不构成明显优劣，费率（563020 综合 0.20% vs 515450 0.60%）与跟踪误差更关键。"))
    add()
    add("| 维度 | {} | {} | 差值 |".format(ranked[0][1]["short"], ranked[1][1]["short"]))
    add("| --- | --- | --- | --- |")
    for key, name, dw, subs in DIMENSIONS:
        a = ranked[0][2][-1]["dim"].get(key)
        b = ranked[1][2][-1]["dim"].get(key)
        add("| {} | {} | {} | {} |".format(name, num_fmt(a, 1), num_fmt(b, 1),
                                           "—" if (a is None or b is None) else "{:+.1f}".format(a - b)))
    add()

    add("## 六、指标覆盖与数据源")
    add()
    add("| 标的 | 已实现 | 代理 | 待接入 | 待接入明细 |")
    add("| --- | --- | --- | --- | --- |")
    for etf, st_map in (etf_states or []):
        cnt = {"live": 0, "proxy": 0, "missing": 0}
        miss = []
        for key, name, dw, subs in DIMENSIONS:
            for sk, sn, sw, s2 in subs:
                s = st_map.get((key, sk), s2)
                cnt[s] += 1
                if s == "missing":
                    miss.append("{}·{}".format(name, sn))
        add("| {} | {} | {} | {} | {} |".format(
            etf["short"], cnt["live"], cnt["proxy"], cnt["missing"], "、".join(miss) or "无"))
    add()
    for line in coverage_stats:
        add("- {}".format(line))
    add()
    add("## 七、数据异常")
    add()
    if errors:
        for e in errors:
            add("- {}".format(e))
    else:
        add("- 本次未发现异常（交易日一致、数据抓取与缓存均正常）。")
    add()
    add("## 八、口径与免责")
    add()
    add("- 评分衡量配置吸引力，是相对排序工具，不是买卖信号。")
    add("- 四档操作建议只换算仓位区间，不预测涨跌；实际仓位还受个人风险预算、持仓成本与账户约束。")
    add("- 子项统一用窗口内百分位排名（默认 1250 交易日，当前股息率 250 日），不使用固定绝对阈值。")
    add("- 折溢价与股息率以不复权收盘价为分母；分红只计已实施（除息）部分。")
    add("- 成分股财务为前十大持仓加权代理（季度持仓快照，按公告日 point-in-time），非全成分股口径。")
    add("- ROIC 以总资产净利率（ROA）代理；PB 分位因无免费日频序列未接入。")
    add("- TTM 分红为滚动 12 个月合计，分红到账日会阶跃，维度当日跳变属口径特性。")
    add("- ETF 当日净值披露滞后，折溢价多用前一交易日净值。")
    add()
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(L))
    return path


BANDS = []
ACTIONS = []
DIM_NAME = {}


def set_globals(bands, actions=None):
    global BANDS, ACTIONS, DIM_NAME
    BANDS = bands
    ACTIONS = tiers_of(actions)
    DIM_NAME = {k: n for k, n, w, subs in DIMENSIONS}


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
    area = "{:.1f},{:.1f} ".format(pts[0][0], height) + poly + " {:.1f},{:.1f}".format(pts[-1][0], height)
    mid = y(sum(values) / float(n))
    last = pts[-1]
    return ('<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" preserveAspectRatio="none" class="spark">'
            '<line x1="0" y1="{mid:.1f}" x2="{w}" y2="{mid:.1f}" stroke="#94a3b8" stroke-width="1" stroke-dasharray="4 4" opacity="0.55"/>'
            '<polygon points="{area}" fill="{color}" opacity="0.10"/>'
            '<polyline points="{poly}" fill="none" stroke="{color}" stroke-width="2.4" stroke-linejoin="round"/>'
            '<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3.2" fill="{color}"/></svg>').format(
        w=width, h=height, mid=mid, area=area, poly=poly, color=color, cx=last[0], cy=last[1])


def write_dashboard(root, all_series, alerts_map, data_date, run_at, repo, scores, coverage_stats):
    from .model import rating_of
    path = os.path.join(root, "docs", "index.html")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cards = []
    for etf, rows in all_series:
        r = rows[-1]
        rating = rating_of(r["score"], BANDS) if r["score"] is not None else BANDS[-1]
        tier = action_of(r["score"], ACTIONS)
        vals = [x["score"] for x in rows if x["score"] is not None][-30:]
        prev = [x for x in rows if x["score"] is not None][-2:]
        delta = None if len(prev) < 2 else prev[-1]["score"] - prev[-2]["score"]
        dims = []
        for key, name, dw, subs in DIMENSIONS:
            v = r["dim"].get(key)
            dims.append('<div class="dim"><div class="dim-h"><span>{n}<i>{w}分 · 覆盖 {c:.0f}%</i></span>'
                        '<span class="dim-v">{v}</span></div><div class="dim-bar"><i style="width:{p:.1f}%"></i></div></div>'.format(
                            n=name, w=dw, c=r["dim_cov"][key] * 100,
                            v="—" if v is None else num_fmt(v, 1), p=0 if v is None else v))
        al = alerts_map.get(etf["code"], [])
        al_html = "".join('<li class="al {cls}">{lv} · {t}</li>'.format(
            cls="hi" if a["level"] == "高" else ("mid" if a["level"] == "中" else "lo"),
            lv=a["level"], t=a["title"]) for a in al)
        lab = tier["label"] if tier else "—"
        cls = TIER_CLS.get(tier["key"] if tier else "", "s3")
        act = ("{}（参考仓位 {}）｜五档评级 {}".format(tier["action"], tier["position"], rating["label"])
               if tier else "评分缺失，不给出操作建议")
        cards.append(
            '<section class="card"><header><div><h2>{short}</h2><p class="sub">{code} · {index}</p></div>'
            '<span class="badge {bc}">{lab}</span></header>'
            '<div class="score-row"><div class="score">{sc}</div><div class="score-meta">'
            '<span class="delta">较前一日 {d1}</span><span>收盘价 {close}</span>'
            '<span>覆盖率 {cov:.0f}% · 股息率 {dy}</span></div></div>'
            '<div class="spark-wrap">{spark}</div>'
            '<p class="spark-cap">近 {nd} 日评分区间 {lo} – {hi}（虚线为均值 {avg}）</p>'
            '<p class="action">操作建议：{act}</p>'
            '<div class="dims">{bars}</div>'
            '<ul class="alerts">{al}</ul></section>'.format(
                short=etf["short"], code=etf["code"], index=etf["index"],
                bc=cls, lab=lab, sc=num_fmt(r["score"], 1),
                d1="—" if delta is None else "{:+.1f}".format(delta),
                close=num_fmt(r["close"], 3), cov=r["coverage"] * 100, dy=pct(r["ttm_yield"], 1),
                spark=sparkline(vals), nd=len(vals), lo=num_fmt(min(vals), 1), hi=num_fmt(max(vals), 1),
                avg=num_fmt(sum(vals) / len(vals), 1), act=act,
                bars="".join(dims), al=al_html))
    names = [e["short"] for e, _ in all_series]
    comp = ['<table class="tbl"><thead><tr><th>维度</th><th>权重</th>' +
            "".join("<th>{}</th>".format(n) for n in names) + "</tr></thead><tbody>"]
    for key, name, dw, subs in DIMENSIONS:
        vals = [rows[-1]["dim"].get(key) for _, rows in all_series]
        mx = max([v for v in vals if v is not None] or [0])
        cells = ['<td class="{}">{}</td>'.format("best" if (v is not None and v == mx) else "",
                                               "—" if v is None else num_fmt(v, 1)) for v in vals]
        comp.append('<tr><td>{}</td><td>{}分</td>{}</tr>'.format(name, dw, "".join(cells)))
    comp.append('<tr class="total"><td>综合评分</td><td>100分</td>' +
                "".join('<td>{}</td>'.format(num_fmt(rows[-1]["score"], 1)) for _, rows in all_series) +
                "</tr></tbody></table>")

    legend = ['<table class="tbl small"><thead><tr><th>档位</th><th>分数区间</th><th>参考仓位</th><th>动作要点</th></tr></thead><tbody>']
    for row in legend_rows(ACTIONS):
        legend.append('<tr><td>{}</td><td>{}</td><td>{}</td><td class="txt">{}</td></tr>'.format(
            row["label"], row["range"], row["position"], row["action"]))
    legend.append("</tbody></table>")

    dates = sorted({r["date"] for r in scores})[-30:]
    hist = ['<table class="tbl small"><thead><tr><th>交易日</th>' +
            "".join("<th>{}</th>".format(n) for n in names) + "</tr></thead><tbody>"]
    for d in reversed(dates):
        cells = []
        for etf, _ in all_series:
            v = next((x for x in scores if x["date"] == d and x["code"] == etf["code"]), None)
            cells.append("<td>{}</td>".format("—" if not v else "{} ({})".format(
                v["score"], v.get("action") or v.get("rating") or "—")))
        hist.append("<tr><td>{}</td>{}</tr>".format(d, "".join(cells)))
    hist.append("</tbody></table>")

    logs = sorted(f[:-3] for f in os.listdir(os.path.join(root, "logs"))
                  if f.endswith(".md") and f[:4].isdigit())[-12:][::-1]
    log_items = "".join('<li><a href="https://github.com/{}/blob/main/logs/{}.md">{} 日志</a></li>'.format(
        repo, l, l) for l in logs)
    src_items = "".join("<li>{}</li>".format(s) for s in coverage_stats)

    html = TEMPLATE.replace("{{DATA_DATE}}", data_date).replace("{{RUN_AT}}", run_at) \
        .replace("{{CARDS}}", "".join(cards)).replace("{{COMP}}", "".join(comp)) \
        .replace("{{LEGEND}}", "".join(legend)) \
        .replace("{{HIST}}", "".join(hist)).replace("{{LOGS}}", log_items) \
        .replace("{{SRC}}", src_items).replace("{{REPO}}", repo)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    return path


TEMPLATE = """<!DOCTYPE html>
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
.tbl td.txt{text-align:left}
.tbl .best{color:var(--teal);font-weight:700}
.tbl .total td{font-weight:700;background:#f8fafc}
.tbl.small td,.tbl.small th{padding:6px 10px}
.box{background:#fff;border:1px solid var(--line);border-radius:12px;padding:12px 18px}
.box ul{margin:6px 0;padding-left:18px;font-size:13px}
.box a{color:var(--teal);text-decoration:none}
.note{color:var(--muted);font-size:12px;margin-top:26px;border-top:1px solid var(--line);padding-top:14px}
</style>
</head>
<body>
<div class="wrap">
<h1>红利低波 ETF 监测评分</h1>
<p class="meta">数据日期 {{DATA_DATE}} · 生成时间 {{RUN_AT}}（北京时间） · 每交易日收盘后（北京 16:20）自动更新</p>
<div class="cards">{{CARDS}}</div>
<h3>六维横向对比</h3>
{{COMP}}
<h3>四档操作建议</h3>
{{LEGEND}}
<h3>近 30 个交易日评分</h3>
{{HIST}}
<h3>数据源与覆盖</h3>
<div class="box"><ul>{{SRC}}</ul></div>
<h3>日志存档</h3>
<div class="box"><ul>{{LOGS}}</ul></div>
<p class="note">评分体系：股息吸引力 20 / 分红质量与可持续性 20 / 低波动与风险控制 20 / 估值合理性 15 / 盈利与财务质量 15 / 市场环境与资金趋势 10，子项统一用窗口内百分位排名标准化。综合评分按四档换算成仓位建议（A ≥70 / B 60–69 / C 45–59 / D <45），四档只调仓位、不是买卖信号。数据全部来自公开免费接口（东财行情与净值、中证指数估值、东财成分股财务、国债收益率报表）；成分股财务为前十大持仓加权代理，PB 分位无免费日频源未接入。风险预警独立于综合分。评分为配置吸引力排序工具，不构成投资建议。源码、回测与逐日日志：<a href="https://github.com/{{REPO}}">github.com/{{REPO}}</a></p>
</div>
</body>
</html>
"""
