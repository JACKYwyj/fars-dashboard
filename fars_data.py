#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fars_data.py — FARS 数据与文献获取层
=====================================
为什么存在
----------
FARS 原来的引用核验靠的是**人工整理的 CSV**（_引用核验总报告.md 等），
数据获取靠 build_paper 里写死的抓取逻辑。两个问题：

1. **引用精度靠人** → 凌晨实测发现初稿里把 Dias 的论文写成 "Trabelsi, N., et al."，
   把 Resources Policy 78 的文章号写成 102876（真值 102860）。人工核对必然漏。
2. **数据源单一** → 论文里常写"数据来自 Bloomberg / Refinitiv 终端"，
   但底层其实是 EViews 工作簿 + 公开 CSV。声明与实际不符 = 硬伤。

本模块把两件事都变成**确定性、可复跑、可审计**的调用：

  数据侧  · FRED（无需 key）· World Bank · IMF · Yahoo Finance 日线
  文献侧  · Crossref（DOI 权威核验）· OpenAlex（检索 + 元数据）· arXiv（预印本）

设计原则（沿用 FARS 既有铁律）
--------------------------------
  · **不编造**：所有返回都带 `source` 字段标明出处；核验失败就如实标 NOT_FOUND
  · **可复跑**：同一入参 → 同一出参（结果落盘缓存 + 来源标注）
  · **不依赖 key**：默认路径全部免鉴权，key 只作为可选加速
  · **离线降级**：网络不通时返回明确错误，不是静默返回空
"""
from __future__ import annotations

import io
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

# ⚠️ 实测：FRED 对 UA 极敏感 —— "Mozilla/5.0" 和自定义 UA 会**读超时 30s**，
#    "curl/8.4.0" 与无 UA 都 0.8s 返回。→ 用 curl UA，别自作聪明加品牌串。
UA = "curl/8.4.0"
TIMEOUT = 25
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


# ============================================================
# 通用 HTTP
# ============================================================
def _get(url: str, headers: Optional[Dict[str, str]] = None, timeout: int = TIMEOUT,
         raw: bool = False) -> Any:
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        body = r.read()
    if raw:
        return body
    txt = body.decode("utf-8", "replace")
    try:
        return json.loads(txt)
    except Exception:
        return txt


def _cache_path(name: str) -> str:
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_datacache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, name)


# ============================================================
# 1) FRED —— 宏观/金融序列（免 key，fredgraph.csv）
# ============================================================
# 常用系列速查（扩充只加一行）
FRED_SERIES = {
    "CPIAUCSL": "CPI 消费者价格指数",
    "EFFR": "有效联邦基金利率",
    "DFEDTARU": "联邦基金利率目标上限",
    "UNRATE": "失业率",
    "GDPC1": "实际 GDP",
    "INDPRO": "工业生产指数",
    "T10Y2Y": "10年-2年国债利差",
    "VIXCLS": "VIX 波动率指数",
    "DGS10": "10年期国债收益率",
    "T10YIE": "10年期通胀预期",
    "BAMLH0A0HYM2": "美国高收益债利差",
    "NFCI": "芝加哥联储全国金融状况指数",
    "STLFSI4": "圣路易斯联储金融压力指数",
    "WALCL": "央行总资产",
    "M2SL": "M2 货币供应量",
    "PCEPILFE": "核心PCE通胀",
    "WTI": "WTI 原油价格",
    "DTWEXBGS": "美元指数(广义)",
    "SP500": "标普500",
}


def fred_series(series_id: str, start: str = "1990-01-01",
                end: str = "2030-12-31") -> Dict[str, Any]:
    """拉 FRED 月度序列。返回 {ok, series_id, name, observations:[{date,value}]}。

    免 key 走 fredgraph.csv。空值（. 表示缺失）会被丢弃并记录 missing 数。
    """
    sid = (series_id or "").strip().upper()
    if not sid:
        return {"ok": False, "error": "series_id 为空"}
    # ⚠️ 实测：带 cosd/coed 参数只回 1 行（区间过滤在 FRED 侧有 bug）→ 拉全量再本地过滤
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv?"
           + urllib.parse.urlencode({"id": sid}))
    try:
        raw = _get(url, raw=True).decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": "FRED 请求失败: %s" % e, "url": url}
    lines = [l for l in raw.strip().splitlines() if l.strip()]
    if len(lines) < 2:
        return {"ok": False, "error": "FRED 无数据（序列不存在或区间为空）",
                "series_id": sid, "url": url}
    header = lines[0].split(",")
    dcol = header[0]
    vcol = header[1] if len(header) > 1 else header[0]
    obs, missing = [], 0
    for l in lines[1:]:
        parts = l.split(",")
        if len(parts) < 2:
            continue
        d0 = parts[0].strip()
        v = parts[1].strip()
        if v in ("", "."):
            missing += 1
            continue
        # 本地按区间过滤（服务端区间过滤不可靠）
        if start and d0 < start:
            continue
        if end and d0 > end:
            continue
        try:
            obs.append({"date": d0, "value": float(v)})
        except ValueError:
            missing += 1
    return {
        "ok": True, "series_id": sid, "name": FRED_SERIES.get(sid, sid),
        "observations": obs, "count": len(obs), "missing": missing,
        "source": "FRED (fred.stlouisfed.org)", "url": url,
        "range": [obs[0]["date"], obs[-1]["date"]] if obs else None,
    }


# 论文里常用的市场序列（都在 FRED，免 key）
FRED_MARKET = {
    "SP500": "标普500 日收盘",
    "VIXCLS": "VIX",
    "WTI": "WTI 原油日收盘",
    "DTWEXBGS": "美元指数(广义) 日值",
    "DGS10": "10年期美债收益率",
    "DGS2": "2年期美债收益率",
    "NFCI": "芝加哥联储 NFCI",
    "STLFSI4": "圣路易斯联储 FSI",
}


def fred_fetch_many(series_ids: List[str], start: str = "1990-01-01",
                    end: str = "2030-12-31") -> Dict[str, Any]:
    """批量拉 FRED，保存成可直接喂给回归的 CSV 文本。"""
    out = {}
    for sid in series_ids or []:
        r = fred_series(sid, start, end)
        out[sid] = r
        time.sleep(0.25)          # 别把 FRED 打出 429
    return {"ok": True, "series": out,
            "count": sum(1 for v in out.values() if v.get("ok"))}


def fred_to_csv(result: Dict[str, Any], series_id: str = "") -> str:
    """把 fred_series 的结果转成两列 CSV 文本（date,value）。"""
    if not result.get("ok"):
        return ""
    lines = ["date,%s" % (series_id or result.get("series_id", "value"))]
    for o in result["observations"]:
        lines.append("%s,%s" % (o["date"], o["value"]))
    return "\n".join(lines)


# ============================================================
# 2) Yahoo Finance —— 指数/ETF 日线（免 key）
# ============================================================
# 论文里五个清洁能源指数的跟踪 ETF
YAHOO_TICKERS = {
    "ICLN": "iShares Global Clean Energy ETF (SPGCE)",
    "PBW": "Invesco WilderHill Clean Energy ETF (ECO)",
    "QCLN": "First Trust NASDAQ Clean Edge ETF (CELS)",
    "PBD": "Invesco Global Clean Energy ETF (NEX)",
    "WCLN": "WilderHill Clean Energy ETF",
    "TAN": "iShares Solar Energy ETF",
    "^VIX": "CBOE 波动率指数",
    "^GSPC": "标普500",
    "CL=F": "WTI 原油期货",
    "DX-Y.NYB": "美元指数",
}


def yahoo_daily(ticker: str, start: str = "2000-01-01",
                end: str = "2030-12-31") -> Dict[str, Any]:
    """拉 Yahoo Finance 日线。返回 {ok, ticker, observations:[{date,open,high,low,close,volume}]}"""
    sym = (ticker or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "ticker 为空"}
    p1 = int(time.mktime(time.strptime(start, "%Y-%m-%d")))
    p2 = int(time.mktime(time.strptime(end, "%Y-%m-%d")))
    # ⚠️ 实测：query1 常返回 429（Edge: Too Many Requests）→ 多 host 重试 + stooq 兜底
    # ⚠️ 实测（2026-11-26）：Yahoo 现在需要 cookie + crumb，
    #    而 getcrumb 端点对匿名请求直接 429 → 匿名不可用。
    #    → 诚实降级：明确告诉调用方需要凭据，不假装能拉。
    #    市场序列的免凭据替代方案：FRED 的 SP500 / VIXCLS / WTI / DGS10 等。
    hosts = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    j = None
    last_err = None
    for host in hosts:
        url = ("https://%s/v8/finance/chart/%s?" % (host, urllib.parse.quote(sym))
               + urllib.parse.urlencode({"period1": p1, "period2": p2, "interval": "1d"}))
        try:
            j = _get(url)
            if isinstance(j, dict) and (j.get("chart") or {}).get("error"):
                last_err = str((j["chart"]["error"]))
                j = None
                time.sleep(1.2)
                continue
            break
        except Exception as e:
            last_err = str(e)
            j = None
            time.sleep(1.2)
    if j is None:
        # ⚠️ stooq 实测返回 JS 验证页（不是 CSV）→ 不可作兜底
        return {"ok": False, "error": "Yahoo 请求失败: %s" % (last_err or "限流/403"),
                "ticker": sym, "url": url,
                "hint": "Yahoo 需 cookie+crumb（见 yahoo_with_crumb）；或改用 FRED 序列"}
    try:
        res = (j.get("chart") or {}).get("result") or []
        if not res:
            err = (j.get("chart") or {}).get("error")
            return {"ok": False, "error": "Yahoo 无数据: %s" % (err or "空结果"),
                    "ticker": sym, "url": url}
        r = res[0]
        ts = r.get("timestamp") or []
        q = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        cl = q.get("close") or []
        op = q.get("open") or []
        hi = q.get("high") or []
        lo = q.get("low") or []
        vo = q.get("volume") or []
        obs = []
        for i, t in enumerate(ts):
            c = cl[i] if i < len(cl) else None
            if c is None:
                continue
            obs.append({
                "date": time.strftime("%Y-%m-%d", time.gmtime(t)),
                "open": op[i] if i < len(op) else None,
                "high": hi[i] if i < len(hi) else None,
                "low": lo[i] if i < len(lo) else None,
                "close": c, "volume": vo[i] if i < len(vo) else None,
            })
        return {"ok": True, "ticker": sym,
                "name": YAHOO_TICKERS.get(sym, sym),
                "observations": obs, "count": len(obs),
                "source": "Yahoo Finance chart API",
                "range": [obs[0]["date"], obs[-1]["date"]] if obs else None,
                "url": url}
    except Exception as e:
        return {"ok": False, "error": "Yahoo 解析失败: %s" % e, "ticker": sym}



def stooq_daily(ticker: str, start: str = "2000-01-01",
                end: str = "2030-12-31") -> Dict[str, Any]:
    """stooq 兜底日线（Yahoo 429 时的备胎）。CSV 格式：Date,Open,High,Low,Close,Volume"""
    sym = (ticker or "").strip().upper()
    if not sym:
        return {"ok": False, "error": "ticker 为空"}
    url = ("https://stooq.com/q/d/l/?" + urllib.parse.urlencode(
        {"s": sym.lower(), "d1": start.replace("-", ""), "d2": end.replace("-", ""),
         "i": "d"}))
    try:
        raw = _get(url, raw=True).decode("utf-8", "replace")
    except Exception as e:
        return {"ok": False, "error": "stooq 请求失败: %s" % e, "url": url}
    lines = [l for l in raw.strip().splitlines() if l.strip()]
    if len(lines) < 2 or lines[0].lower().startswith("no data"):
        return {"ok": False, "error": "stooq 无数据", "ticker": sym, "url": url}
    obs = []
    for l in lines[1:]:
        p = l.split(",")
        if len(p) < 5:
            continue
        try:
            obs.append({"date": p[0], "open": float(p[1]), "high": float(p[2]),
                        "low": float(p[3]), "close": float(p[4]),
                        "volume": float(p[5]) if len(p) > 5 and p[5] not in ("", ".") else None})
        except ValueError:
            continue
    return {"ok": True, "ticker": sym, "name": YAHOO_TICKERS.get(sym, sym),
            "observations": obs, "count": len(obs),
            "source": "stooq.com", "range": [obs[0]["date"], obs[-1]["date"]] if obs else None,
            "url": url}


def yahoo_to_monthly_rv(result: Dict[str, Any]) -> Dict[str, Any]:
    """日线 → 月度已实现波动率 ln(RV×10000)（与论文口径一致）。"""
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error")}
    from collections import defaultdict
    bym = defaultdict(list)
    for o in result["observations"]:
        if o.get("close") is None:
            continue
        bym[o["date"][:7]].append(o["close"])
    out = []
    import math
    for ym in sorted(bym):
        px = bym[ym]
        if len(px) < 5:
            continue
        rets = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))
                if px[i - 1] and px[i] > 0]
        if len(rets) < 4:
            continue
        rv = math.sqrt(sum(r * r for r in rets))     # 已实现波动率（日频聚合）
        out.append({"month": ym, "rv": rv, "lnrv": math.log(rv * 10000)
                    if rv > 0 else None, "n": len(rets)})
    return {"ok": True, "ticker": result.get("ticker"), "monthly": out,
            "count": len(out), "source": "derived from %s" % result.get("source"),
            "formula": "ln(RV*10000), RV = sqrt(sum(r_t^2))"}


# ============================================================
# 3) Crossref —— 引用权威核验（DOI 级）
# ============================================================
def _norm_title(t: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (t or "").lower()).strip()


def crossref_verify(ref: Dict[str, Any]) -> Dict[str, Any]:
    """核验单条参考文献。

    ref: {"title": "...", "author": "...", "year": 2011, "journal": "...", "doi": "..."}

    判定：
      CONFIRMED  —— 条目 DOI 存在，且 Crossref 权威记录标题/期刊/年份一致
      MISMATCH   —— 条目 DOI 存在，但权威记录的标题/期刊/年份对不上
      NO_DOI     —— 条目没写 DOI → 用标题去检索权威记录（能命中就给建议 DOI）
      NOT_FOUND  —— 标题检索也找不到（可能是学位论文/网页/错误条目）
    """
    title = (ref.get("title") or "").strip()
    doi = (ref.get("doi") or "").strip()
    year = ref.get("year")
    journal = (ref.get("journal") or "").strip()
    out = {"input": ref, "status": None, "detail": "", "authoritative": None,
           "suggested_doi": None, "source": "Crossref API"}

    def _norm_match(a: str, b: str) -> float:
        A, B = _norm_title(a), _norm_title(b)
        if not A or not B:
            return 0.0
        sa, sb = set(A.split()), set(B.split())
        return len(sa & sb) / max(1, len(sa | sb))

    # —— 有 DOI：直接取权威记录 ——
    if doi:
        try:
            j = _get("https://api.crossref.org/works/%s"
                     % urllib.parse.quote(doi.replace("https://doi.org/", "")))
            m = j.get("message") or {}
        except Exception as e:
            return {"ok": False, "error": "Crossref 查询失败: %s" % e,
                    "input": ref, "source": "Crossref API"}
        if m.get("title"):
            at = (m.get("title") or [""])[0]
            aj = (m.get("container-title") or [""])[0]
            ay = (m.get("issued", {}).get("date-parts") or [[None]])[0][0]
            sim = _norm_match(title, at)
            issues = []
            if sim < 0.55:
                issues.append("标题相似度 %.2f" % sim)
            if journal and aj and _norm_match(journal, aj) < 0.4:
                issues.append("期刊不一致")
            if year and ay and int(year) != int(ay):
                issues.append("年份 %s≠%s" % (year, ay))
            out["authoritative"] = {
                "title": at, "journal": aj, "year": ay, "doi": m.get("DOI"),
                "authors": ["%s %s" % (a.get("family", ""), a.get("given", ""))
                            for a in (m.get("author") or [])][:6],
                "volume": m.get("volume"), "issue": m.get("issue"),
                "page": m.get("page"), "article_number": m.get("article-number"),
                "type": m.get("type"),
            }
            if issues:
                out["status"] = "MISMATCH"
                out["detail"] = "；".join(issues)
            else:
                out["status"] = "CONFIRMED"
                out["detail"] = "DOI 有效，标题/期刊/年份一致"
            return out

    # —— 无 DOI：用标题检索 ——
    if not title:
        out["status"] = "NOT_FOUND"
        out["detail"] = "既无 DOI 也无标题"
        return out
    try:
        j = _get("https://api.crossref.org/works?"
                 + urllib.parse.urlencode({"query.bibliographic": title, "rows": 3}))
    except Exception as e:
        return {"ok": False, "error": "Crossref 检索失败: %s" % e,
                "input": ref, "source": "Crossref API"}
    items = (j.get("message") or {}).get("items") or []
    if not items:
        out["status"] = "NOT_FOUND"
        out["detail"] = "Crossref 无匹配（可能是学位论文/网页/条目有误）"
        return out
    best, best_sim = None, 0.0
    for it in items:
        t = (it.get("title") or [""])[0]
        s = _norm_match(title, t)
        if s > best_sim:
            best, best_sim = it, s
    if best is None or best_sim < 0.45:
        out["status"] = "NOT_FOUND"
        out["detail"] = "Crossref 无高相似匹配（最高 %.2f）" % best_sim
        return out
    ay = (best.get("issued", {}).get("date-parts") or [[None]])[0][0]
    aj = (best.get("container-title") or [""])[0]
    out["status"] = "NO_DOI"
    out["suggested_doi"] = best.get("DOI")
    out["authoritative"] = {
        "title": (best.get("title") or [""])[0], "journal": aj, "year": ay,
        "doi": best.get("DOI"),
        "authors": ["%s %s" % (a.get("family", ""), a.get("given", ""))
                    for a in (best.get("author") or [])][:6],
        "volume": best.get("volume"), "issue": best.get("issue"),
        "page": best.get("page"), "article_number": best.get("article-number"),
        "type": best.get("type"),
    }
    notes = []
    if year and ay and int(year) != int(ay):
        notes.append("年份应为 %s（条目写 %s）" % (ay, year))
    if journal and aj and _norm_match(journal, aj) < 0.4:
        notes.append("期刊应为 %s（条目写 %s）" % (aj or "?", journal))
    out["detail"] = "标题命中（相似度 %.2f）" % best_sim + \
                    ("；" + "；".join(notes) if notes else "")
    return out


def crossref_verify_batch(refs: List[Dict[str, Any]],
                          sleep: float = 0.35) -> Dict[str, Any]:
    """批量核验。返回 {ok, results:[...], summary:{CONFIRMED,MISMATCH,NO_DOI,NOT_FOUND}}"""
    results = []
    for r in refs or []:
        results.append(crossref_verify(r))
        time.sleep(sleep)
    summary = {}
    for r in results:
        k = r.get("status") or "ERROR"
        summary[k] = summary.get(k, 0) + 1
    return {"ok": True, "results": results, "summary": summary,
            "count": len(results), "source": "Crossref API"}


# ============================================================
# 4) OpenAlex —— 文献检索 + 元数据（含被引数、开放获取链接）
# ============================================================
def openalex_search(query: str, per_page: int = 8,
                     year_from: Optional[int] = None,
                     year_to: Optional[int] = None) -> Dict[str, Any]:
    """检索文献。返回按被引数降序的结果列表。"""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query 为空"}
    params = {"search": q, "per_page": max(1, min(25, per_page)),
              "sort": "cited_by_count:desc"}
    if year_from:
        params["filter"] = "from_publication_date:%d-01-01" % year_from
    if year_to:
        f = params.get("filter", "")
        params["filter"] = (f + "," if f else "") + \
                           "to_publication_date:%d-12-31" % year_to
    try:
        j = _get("https://api.openalex.org/works?"
                 + urllib.parse.urlencode(params))
    except Exception as e:
        return {"ok": False, "error": "OpenAlex 请求失败: %s" % e}
    works = []
    for w in (j.get("results") or []):
        loc = (w.get("primary_location") or {}).get("source") or {}
        works.append({
            "title": w.get("title"),
            "year": w.get("publication_year"),
            "journal": loc.get("display_name"),
            "doi": w.get("doi"),
            "cited_by": w.get("cited_by_count"),
            "authors": [a.get("author", {}).get("display_name")
                        for a in (w.get("authorships") or [])][:6],
            "type": w.get("type"),
            "open_access": (w.get("open_access") or {}).get("is_oa"),
            "pdf_url": (w.get("best_oa_location") or {}).get("pdf_url"),
            "landing": (w.get("primary_location") or {}).get("landing_page_url"),
        })
    return {"ok": True, "query": q, "works": works, "count": len(works),
            "source": "OpenAlex API"}


def openalex_by_doi(doi: str) -> Dict[str, Any]:
    """按 DOI 取权威元数据（Crossref 没有时的兜底）。"""
    d = (doi or "").strip().replace("https://doi.org/", "")
    if not d:
        return {"ok": False, "error": "doi 为空"}
    try:
        j = _get("https://api.openalex.org/works/https://doi.org/%s"
                 % urllib.parse.quote(d))
    except Exception as e:
        return {"ok": False, "error": "OpenAlex 请求失败: %s" % e}
    loc = (j.get("primary_location") or {}).get("source") or {}
    return {"ok": True, "doi": d, "title": j.get("title"),
            "year": j.get("publication_year"), "journal": loc.get("display_name"),
            "cited_by": j.get("cited_by_count"),
            "authors": [a.get("author", {}).get("display_name")
                        for a in (j.get("authorships") or [])][:6],
            "type": j.get("type"),
            "open_access": (j.get("open_access") or {}).get("is_oa"),
            "pdf_url": (j.get("best_oa_location") or {}).get("pdf_url"),
            "landing": (j.get("primary_location") or {}).get("landing_page_url"),
            "source": "OpenAlex API"}


# ============================================================
# 5) arXiv —— 预印本（金融/计量方向）
# ============================================================
def arxiv_search(query: str, max_results: int = 6,
                  sort_by: str = "relevance") -> Dict[str, Any]:
    """检索 arXiv。query 用 arXiv 语法，如 'all:financial stress AND all:renewable'"""
    q = (query or "").strip()
    if not q:
        return {"ok": False, "error": "query 为空"}
    url = ("https://export.arxiv.org/api/query?"
           + urllib.parse.urlencode({"search_query": q, "max_results": max_results,
                                     "sortBy": sort_by, "sortOrder": "descending"}))
    try:
        raw = _get(url)
    except Exception as e:
        return {"ok": False, "error": "arXiv 请求失败: %s" % e}
    if not isinstance(raw, str):
        return {"ok": False, "error": "arXiv 返回非文本"}
    papers = []
    for m in re.finditer(r"<entry>(.*?)</entry>", raw, re.S):
        e = m.group(1)
        t = re.search(r"<title>(.*?)</title>", e, re.S)
        i = re.search(r"<id>(.*?)</id>", e, re.S)
        p = re.search(r"<published>(.*?)</published>", e, re.S)
        au = re.findall(r"<name>(.*?)</name>", e)
        pdf = re.search(r'href="([^"]+\.pdf)"', e)
        summ = re.search(r"<summary>(.*?)</summary>", e, re.S)
        papers.append({
            "title": re.sub(r"\s+", " ", (t.group(1) if t else "")).strip(),
            "id": (i.group(1) if i else "").strip(),
            "published": (p.group(1) if p else "")[:10],
            "authors": [a.strip() for a in au][:6],
            "pdf_url": pdf.group(1) if pdf else None,
            "abstract": re.sub(r"\s+", " ", (summ.group(1) if summ else "")).strip()[:400],
        })
    return {"ok": True, "query": q, "papers": papers, "count": len(papers),
            "source": "arXiv API"}


# ============================================================
# 6) 一站式：论文参考文献 → 核验报告
# ============================================================
def verify_reference_list(refs: List[Dict[str, Any]],
                          sleep: float = 0.35) -> Dict[str, Any]:
    """论文参考文献表全量核验，产出可直接贴进论文附录的报告。"""
    if not refs:
        return {"ok": False, "error": "refs 为空"}
    res = crossref_verify_batch(refs, sleep=sleep)
    rows = []
    for i, r in enumerate(res["results"], 1):
        inp = r.get("input") or {}
        au = r.get("authoritative") or {}
        status = r.get("status")
        rows.append({
            "n": i,
            "status": status,
            "input_title": (inp.get("title") or "")[:120],
            "input_year": inp.get("year"),
            "input_journal": inp.get("journal"),
            "input_doi": inp.get("doi"),
            "auth_title": (au.get("title") or "")[:120],
            "auth_journal": au.get("journal"),
            "auth_year": au.get("year"),
            "auth_authors": au.get("authors"),
            "auth_doi": au.get("doi") or r.get("suggested_doi"),
            "detail": r.get("detail"),
        })
    problems = [r for r in rows if r["status"] in ("MISMATCH", "NOT_FOUND")]
    return {
        "ok": True,
        "count": res["count"],
        "summary": res["summary"],
        "rows": rows,
        "problems": problems,
        "problem_count": len(problems),
        "verdict": ("全部可核验" if not problems
                    else "%d 条需人工修正" % len(problems)),
        "source": "Crossref API",
    }


def md_report(verify_result: Dict[str, Any]) -> str:
    """把核验结果渲染成 Markdown 表格（可直接贴进论文附录 / 报告）。"""
    if not verify_result.get("ok"):
        return "> 核验失败：%s\n" % verify_result.get("error")
    L = ["# 参考文献权威核验（Crossref）", "",
         "| # | 判定 | 条目年份 | 权威年份 | 条目标题 | 权威标题 | 权威期刊 | DOI | 说明 |",
         "|---|---|---|---|---|---|---|---|---|"]
    badge = {"CONFIRMED": "✅ CONFIRMED", "MISMATCH": "⚠️ MISMATCH",
             "NO_DOI": "ℹ️ NO_DOI", "NOT_FOUND": "❌ NOT_FOUND"}
    for r in verify_result.get("rows", []):
        L.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
            r["n"], badge.get(r["status"], r["status"] or "-"),
            r.get("input_year") or "-", r.get("auth_year") or "-",
            (r.get("input_title") or "")[:60],
            (r.get("auth_title") or "")[:60],
            (r.get("auth_journal") or "-")[:38],
            (r.get("auth_doi") or "-")[:34],
            (r.get("detail") or "")[:50]))
    L += ["", "**汇总**：%s" % json.dumps(verify_result.get("summary", {}),
                                    ensure_ascii=False),
          "；%s" % verify_result.get("verdict", "")]
    return "\n".join(L)


# ============================================================
# 7) 自检
# ============================================================
if __name__ == "__main__":
    import sys
    if "--self-test" in sys.argv:
        print("FRED  :", fred_series("CPIAUCSL", "2020-01-01", "2020-06-01"))
        print("Yahoo :", yahoo_daily("ICLN", "2024-01-01", "2024-02-01").get("count"))
        r = crossref_verify({"title": "Financial stress and economic contractions",
                             "journal": "Journal of Financial Stability", "year": 2011,
                             "doi": "10.1016/j.jfs.2010.01.005"})
        print("Crossref:", r.get("status"), "|", r.get("detail"))
        print("OpenAlex:", openalex_search("financial stress renewable energy",
                                           per_page=3).get("count"))
        print("arXiv  :", arxiv_search("all:financial stress AND all:renewable",
                                       max_results=2).get("count"))
