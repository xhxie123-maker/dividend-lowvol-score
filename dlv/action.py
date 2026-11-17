# -*- coding: utf-8 -*-
"""五档操作建议：把 0–100 的综合评分映射成可执行的仓位纪律。

口径（重要）：
- 档位边界固定：A ≥70 / B 60–69 / C 45–59 / D 35–44 / E <35，不按回测最优点调参，
  避免在 6 年、2 只 ETF 的短样本上过拟合。
- A–D 四档用于「仓位纪律」：给出仓位区间与动作要点。
- E 档是强卖出信号：评分跌入 <35 的极弱区间时触发，先降仓至 0–10%，不做短期回补。
  该档来自用户设定的操作语义，不是回测推导出的最优阈值；历史样本里 E 档样本很少，
  结论只能当方向参考（详见 backtest/report.md 第二节）。
- 与五档评级并存：评级（非常有吸引力…吸引力较低）用于描述，五档操作建议用于执行。
"""

DEFAULT_TIERS = [
    {"min": 70, "key": "A", "label": "A 档 · 积极配置", "position": "70–100%",
     "action": "评分处于强区间，可按计划仓位上限执行，分 2–3 批建仓"},
    {"min": 60, "key": "B", "label": "B 档 · 分批建仓", "position": "50–70%",
     "action": "有配置价值但未到强区间，分 3 批建仓，单批不超过计划仓位 1/3"},
    {"min": 45, "key": "C", "label": "C 档 · 持有观望", "position": "30–50%",
     "action": "维持现有仓位，不新增不减仓，评分回到 60 以上再考虑加仓"},
    {"min": 35, "key": "D", "label": "D 档 · 低配等待", "position": "10–30%",
     "action": "只保留底仓，不新建仓；若因估值、基本面仍要买入，单月不超过计划仓位 1/5"},
    {"min": 0, "key": "E", "label": "E 档 · 强卖出信号", "position": "0–10%",
     "action": "评分跌入极弱区间，触发强卖出：清仓或只留 ≤10% 观察仓，不因短期反弹回补；"
               "评分回到 45 以上再重新评估是否买回"},
]

# 五档的展示颜色（复用看板已有的 s5/s4/s3/s2/s1 样式，E 档为红色）
CLS = {"A": "s5", "B": "s4", "C": "s3", "D": "s2", "E": "s1"}


def tiers_of(cfg_tiers=None):
    if not cfg_tiers:
        return list(DEFAULT_TIERS)
    out = []
    for t in cfg_tiers:
        out.append({"min": t["min"], "key": t.get("key", ""),
                    "label": t["label"], "position": t.get("position", "—"),
                    "action": t.get("action", "")})
    out.sort(key=lambda x: -x["min"])
    return out


def action_of(score, tiers):
    """综合评分 → 五档之一（dict）。score 为 None 时返回 None。"""
    if score is None:
        return None
    for t in tiers:
        if score >= t["min"]:
            return t
    return tiers[-1]


def label_of(score, tiers):
    t = action_of(score, tiers)
    return "—" if t is None else t["label"]


def range_text(tiers, i):
    """第 i 档的分数区间文本，如 70–100 / 60–69 / 45–59 / 35–44 / 0–34。"""
    hi = 100 if i == 0 else int(tiers[i - 1]["min"]) - 1
    return "{:.0f}–{}".format(int(tiers[i]["min"]), hi)


def band_text(tiers):
    return " / ".join("{} {}".format(t["key"], range_text(tiers, i))
                      for i, t in enumerate(tiers))


def legend_rows(tiers):
    return [{"label": t["label"], "range": range_text(tiers, i),
             "position": t["position"], "action": t["action"]}
            for i, t in enumerate(tiers)]
