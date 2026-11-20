# -*- coding: utf-8 -*-
"""免费数据源连通性自检：在 GitHub Actions 上跑一遍，结果写进 logs/source_probe.md。"""

import json
import os
import sys
import time

from . import feed

CODES = ["600019", "601398", "000651"]
TXT = []


def _sym(code):
    return ("sh" if code[0] == "6" else "sz") + code


def _rec(name, ok, detail):
    TXT.append("| {} | {} | {} |".format(name, "可用" if ok else "不可用", detail))
    print("[PROBE] {} {} {}".format(name, ok, detail), flush=True)


def probe_tencent():
    ok = 0
    t0 = time.time()
    for c in CODES:
        try:
            raw = feed.http_get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                                {"param": "{},day,2024-01-01,2026-12-31,600,"
                                 .format(_sym(c))}, timeout=15)
            node = ((json.loads(raw).get("data") or {}).get(_sym(c)) or {})
            n = len(node.get("day") or node.get("qfqday") or [])
            ok += 1 if n > 0 else 0
            _rec("腾讯 fqkline {}".format(c), n > 0, "{} 根".format(n))
        except Exception as exc:
            _rec("腾讯 fqkline {}".format(c), False, str(exc)[:80])
    _rec("腾讯 fqkline 汇总", ok > 0, "{} / {} 成功，{:.1f}s".format(ok, len(CODES), time.time() - t0))


def probe_sina_k():
    for c in CODES[:2]:
        try:
            raw = feed.http_get("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
                                "CN_MarketData.getKLineData",
                                {"symbol": _sym(c), "scale": "240", "ma": "no", "datalen": "100"},
                                timeout=15,
                                headers={"Referer": "https://finance.sina.com.cn"})
            n = len(json.loads(raw) or [])
            _rec("新浪 K 线 {}".format(c), n > 0, "{} 根".format(n))
        except Exception as exc:
            _rec("新浪 K 线 {}".format(c), False, str(exc)[:80])


def probe_em_single():
    try:
        raw = feed.http_get("https://push2his.eastmoney.com/api/qt/stock/kline/get",
                            {"secid": "1.600019", "fields1": "f1,f2,f3,f4,f5,f6",
                             "fields2": "f51,f52,f53,f54,f55,f56,f57",
                             "klt": "101", "fqt": "0", "beg": "20240101", "end": "20500101",
                             "lmt": "600"}, timeout=15)
        n = len(((json.loads(raw).get("data") or {}).get("klines")) or [])
        _rec("东财 K 线 600019", n > 0, "{} 根".format(n))
    except Exception as exc:
        _rec("东财 K 线 600019", False, str(exc)[:80])


def probe_tx_batch():
    try:
        raw = feed.http_get("https://qt.gtimg.cn/q=" + ",".join(_sym(c) for c in CODES),
                            timeout=15)
        body = raw.replace("\n", "")
        _rec("腾讯批量快照", "~" in body, "{} 字节".format(len(body)))
    except Exception as exc:
        _rec("腾讯批量快照", False, str(exc)[:80])


def probe_sina_batch():
    try:
        raw = feed.http_get("https://hq.sinajs.cn/list=" + ",".join(_sym(c) for c in CODES),
                            timeout=15,
                            headers={"Referer": "https://finance.sina.com.cn"})
        _rec("新浪批量快照", len(raw) > 20, "{} 字节".format(len(raw)))
    except Exception as exc:
        _rec("新浪批量快照", False, str(exc)[:80])


def probe_em_batch():
    try:
        raw = feed.http_get("https://push2.eastmoney.com/api/qt/ulist.np/get",
                            {"fltt": "2", "secids": "1.600019,1.601398,0.000651",
                             "fields": "f12,f14,f2,f9"}, timeout=15)
        n = len(((json.loads(raw).get("data") or {}).get("diff")) or [])
        _rec("东财批量快照", n > 0, "{} 只".format(n))
    except Exception as exc:
        _rec("东财批量快照", False, str(exc)[:80])


def main(root="."):
    TXT.append("| 数据源 | 状态 | 明细 |")
    TXT.append("| --- | --- | --- |")
    for fn in (probe_tencent, probe_sina_k, probe_em_single, probe_tx_batch,
               probe_sina_batch, probe_em_batch):
        try:
            fn()
        except Exception as exc:
            TXT.append("| {} | 异常 | {} |".format(fn.__name__, str(exc)[:80]))
    path = os.path.join(root, "logs", "source_probe.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(["# 免费数据源连通性自检", ""] + TXT + [""]))


if __name__ == "__main__":
    main(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    sys.exit(0)
