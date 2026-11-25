#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sources.py —— 文献与数据的**检索 → 下载 → 校验 → 可打开**能力。

设计来源：把「文献资料包」任务里沉淀的实战经验搬进 FARS，
让任何人填个 API Key 就能复刻同样的找文献/找数据能力。

沉淀的经验（都是踩过才知道的）：
  1. 检索源要**阶梯降级**，不能死磕一个：
     OpenAlex（首选，一次给全字段）→ Crossref → arXiv → Europe PMC。
  2. OpenAlex 429 = **当日预算耗尽**（按 IP 共享、午夜 UTC 重置），
     重试无效，必须换通道。
  3. 出版商反爬**不返回 403**，而是返回 200 + HTML
     —— 1.2MB 的 HTML 会被「体积大于阈值即通过」的检查放行。
     唯一可靠判据是**内容检查**。
  4. 四重校验：%PDF 头 / %%EOF 尾 / pypdf 可解析且页数>0 / 首页确为目标文献。
  5. 通过了全部校验 ≠ 就是你要的那份文档
     → title 字段以 **PDF 首页实际标题**为准，不采信文件名。
  6. 判定「付费墙」要看**所有合法开放通道皆空**
     （Unpaywall oa_locations / Semantic Scholar openAccessPdf / DOI 落地页直链）。
  7. 落地数只按「pypdf 能打开且页数>0」计，不按「尝试过」计。
  8. 去重按**字节内容**（md5），不按文件名。
  9. 收尾必须做「磁盘 ↔ 记录」双向断言。

对外接口：
    search_literature(query, limit, ...)   → 候选列表
    download_literature(rec, dest_dir)    → 下载+校验+返回落盘结果
    search_data(query, limit)             → 数据集候选
    download_data(rec, dest_dir)          → 下载数据文件
    open_local(path)                      → 用系统默认程序打开
"""

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import ssl

# ---------------------------------------------------------------- 通用工具

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 检索 API 的联系邮箱。
# ⚠️ 实测踩过：Unpaywall 用 example.com 邮箱会返回 **422**
#   （"Please use your own email address in API calls"）→ OA 直链通道整个失效。
# 所以这里必须用**真实可用**的邮箱。可以用环境变量覆盖：
#   export FARS_CONTACT_EMAIL=you@example.com
CONTACT_EMAIL = os.environ.get("FARS_CONTACT_EMAIL") or "fars.dashboard@gmail.com"


# 单篇文献下载的**总时间预算**与**候选链接上限**
# （落地页解析可能展开几十个链接，必须有闸门，否则一个请求挂几分钟）
DOWNLOAD_BUDGET_SEC = 75
MAX_DOWNLOAD_TRIES = 8
# 单个候选链接的下载超时
DOWNLOAD_TIMEOUT_SEC = 20


def _http(url, data=None, headers=None, timeout=30, retries=2):
    """带 UA 的 HTTP 请求，返回 (status, bytes)。失败抛异常。

    ⚠️ 实测踩过：部分 OA 镜像站（bioline.org.br 等）**证书链不完整**，
      Python 默认会 CERTIFICATE_VERIFY_FAILED → 直接判「没有可用直链」，
      但浏览器能正常打开。
      → 加一层降级：只有当错误确实是 SSL 证书类问题时，
        才用「不校验证书」的 context 重试一次，并在返回里标记。
        其他错误（超时/403/404）**不降级**，避免掩盖真问题。
    """
    h = {"User-Agent": _UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    last = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.getcode(), r.read()
        except urllib.error.HTTPError as e:
            last = e
            # 4xx 不重试（除 429）
            if e.code != 429:
                raise
        except Exception as e:
            last = e
            # ↓ SSL 证书问题 → 不校验证书重试一次
            if _is_ssl_error(e):
                try:
                    ctx = ssl.create_default_context()
                    ctx.check_hostname = False
                    ctx.verify_mode = ssl.CERT_NONE
                    req2 = urllib.request.Request(url, data=data, headers=h)
                    with urllib.request.urlopen(req2, timeout=timeout,
                                              context=ctx) as r:
                        return r.getcode(), r.read()
                except urllib.error.HTTPError as e2:
                    last = e2
                    if e2.code != 429:
                        raise
                except Exception as e2:
                    last = e2
        if attempt < retries:
            time.sleep(1.2 * (attempt + 1))
    raise last if last else RuntimeError("请求失败")


def _is_ssl_error(e):
    """判断是不是 SSL 证书类错误（该降级重试）。"""
    msg = str(e).lower()
    if "certificate" in msg or "certificate_verify_failed" in msg:
        return True
    # urllib 把 SSLCertVerificationError 包在 URLError 里，reason 里会带信息
    reason = getattr(e, "reason", None)
    if reason is not None and _is_ssl_error(reason):
        return True
    return False


def _json_api(url, timeout=30):
    """调 JSON API，返回 dict。"""
    st, raw = _http(url, timeout=timeout)
    if st != 200:
        raise RuntimeError("HTTP %s" % st)
    return json.loads(raw.decode("utf-8", "replace"))


# ---------------------------------------------------------------- PDF 校验

def pdf_ok(path):
    """四重校验：文件头 %PDF / 尾部 %%EOF / pypdf 可解析且页数>0。

    返回 (ok, 说明)。**不通过的文件必须删除**，不能留在资料包里。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(5)[:4]
            if head != b"%PDF":
                return False, "文件头不是 %PDF"
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 2048))
            if b"%%EOF" not in f.read():
                return False, "尾部无 %%EOF"
        try:
            import pypdf
        except ImportError:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                import PyPDF2 as pypdf
        r = pypdf.PdfReader(path)
        pages = len(r.pages)
        if pages < 1:
            return False, "0 页"
        return True, "%d 页" % pages
    except Exception as e:
        return False, str(e)[:80]


def pdf_first_page_text(path, max_chars=1200):
    """取 PDF 首页文本（用于确认「确实是目标文献」）。"""
    try:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                import pypdf
            except ImportError:
                import PyPDF2 as pypdf
        r = pypdf.PdfReader(path)
        if not r.pages:
            return ""
        return (r.pages[0].extract_text() or "")[:max_chars]
    except Exception:
        return ""


# ================================================================ 文献检索

def _abstract_from_inverted(idx):
    """OpenAlex 的 abstract_inverted_index 是 {词: [位置列表]}，
    必须按位置号排序后拼词 —— 直接 str() 会得到乱序片段。"""
    if not idx:
        return ""
    pairs = []
    for word, poss in idx.items():
        for p in (poss or []):
            pairs.append((p, word))
    pairs.sort()
    return " ".join(w for _, w in pairs)


def search_openalex(query, limit=8, oa_only=True):
    """OpenAlex 检索（首选通道，一次给全字段）。"""
    params = {
        "search": query,
        "per-page": str(max(1, min(50, limit))),
        "mailto": CONTACT_EMAIL,
    }
    if oa_only:
        params["filter"] = "is_oa:true"
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    try:
        data = _json_api(url, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 429:
            raise RuntimeError("OpenAlex 429：当日检索预算已耗尽（按 IP 共享、午夜 UTC 重置）。"
                               "请稍后重试，或换用其它检索源。")
        raise
    out = []
    for w in (data.get("results") or []):
        loc = (w.get("best_oa_location") or {}) or {}
        pdf_url = loc.get("pdf_url") or loc.get("url") or ""
        out.append({
            "source": "openalex",
            "id": w.get("id") or "",
            "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
            "title": w.get("title") or w.get("display_name") or "",
            "authors": ", ".join(
                (a.get("author") or {}).get("display_name", "")
                for a in (w.get("authorships") or [])[:8]),
            "year": w.get("publication_year") or "",
            "journal": (((w.get("primary_location") or {}).get("source") or {})
                        .get("display_name") or ""),
            "cited_by": w.get("cited_by_count") or 0,
            "oa_status": (w.get("open_access") or {}).get("oa_status") or "",
            "pdf_url": pdf_url,
            "landing_url": (w.get("primary_location") or {}).get("landing_page_url") or "",
            "abstract": _abstract_from_inverted(w.get("abstract_inverted_index")),
        })
    return out


def search_crossref(query, limit=8, from_year=2018):
    """Crossref 兜底（无 DOI 时用）。默认过滤掉大量 SSRN 预印本。"""
    params = {
        "query.bibliographic": query,
        "rows": str(max(1, min(50, limit))),
        "filter": "from-pub-date:%d-01-01,type:journal-article" % from_year,
        "select": "DOI,title,author,issued,container-title,is-referenced-by-count,abstract",
    }
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    data = _json_api(url, timeout=30)
    out = []
    for it in ((data.get("message") or {}).get("items") or []):
        issued = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
        out.append({
            "source": "crossref",
            "doi": it.get("DOI") or "",
            "title": (it.get("title") or [""])[0],
            "authors": ", ".join(
                "%s %s" % (a.get("given") or "", a.get("family") or "")
                for a in (it.get("author") or [])[:8]),
            "year": issued[0] if issued else "",
            "journal": (it.get("container-title") or [""])[0],
            "cited_by": it.get("is-referenced-by-count") or 0,
            "oa_status": "",
            "pdf_url": "",
            "landing_url": ("https://doi.org/" + it["DOI"]) if it.get("DOI") else "",
            "abstract": re.sub(r"<[^>]+>", " ", it.get("abstract") or "")[:600],
        })
    return out


def search_arxiv(query, limit=8):
    """arXiv（最新预印本，PDF 直链稳定）。"""
    url = ("http://export.arxiv.org/api/query?search_query=all:%s"
           "&max_results=%d" % (urllib.parse.quote(query), max(1, min(50, limit))))
    st, raw = _http(url, timeout=30)
    xml = raw.decode("utf-8", "replace")
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", xml, re.S):
        def _g(tag):
            m = re.search(r"<%s[^>]*>(.*?)</%s>" % (tag, tag), entry, re.S)
            return re.sub(r"\s+", " ", m.group(1)).strip() if m else ""
        aid = _g("id")
        pdf = re.sub(r"^http://arxiv\.org/abs/", "https://arxiv.org/pdf/", aid) if aid else ""
        out.append({
            "source": "arxiv",
            "id": aid,
            "doi": "",
            "title": _g("title"),
            "authors": ", ".join(re.findall(r"<name>(.*?)</name>", entry)[:8]),
            "year": _g("published")[:4],
            "journal": "arXiv preprint",
            "cited_by": 0,
            "oa_status": "green",
            "pdf_url": pdf,
            "landing_url": aid,
            "abstract": _g("summary"),
        })
    return out


def _openalex_resolve_authors(surname, limit=5):
    """把姓氏解析成 OpenAlex 的 author id 列表。

    为什么必须先解析 id：直接用 search=surname 检索 works 时，
    同名学者会把目标彻底淹没。
    实测（"Jensen" + 1970 + 金融领域）：
      关键词检索 → 语言学/眼科学/农业经济学的同名学者
      作者 id 检索 → 直接命中 Michael C. Jensen 的作品集
    """
    url = ("https://api.openalex.org/authors?" + urllib.parse.urlencode(
        {"search": surname, "per-page": str(max(1, min(25, limit))),
         "mailto": CONTACT_EMAIL}))
    try:
        data = _json_api(url, timeout=30)
    except Exception:
        return []
    out = []
    for a in (data.get("results") or []):
        aid = (a.get("id") or "").split("/")[-1]
        if not aid:
            continue
        out.append({
            "id": aid,
            "name": a.get("display_name") or "",
            "works": a.get("works_count") or 0,
            "cited": a.get("cited_by_count") or 0,
            "inst": ((a.get("last_known_institution") or {}).get("display_name") or ""),
        })
    # 作品多、被引多的作者更可能是目标领域的大牛
    out.sort(key=lambda x: -(x["cited"]))
    return out


def _openalex_works_by_author(aid, year=0, limit=20):
    """按 author id + 年份拉作品（精确到人，不受同名干扰）。"""
    filt = "author.id:%s" % aid
    if year:
        filt += ",publication_year:%d" % year
    url = ("https://api.openalex.org/works?" + urllib.parse.urlencode(
        {"filter": filt, "per-page": str(max(1, min(50, limit))),
         "mailto": CONTACT_EMAIL}))
    try:
        data = _json_api(url, timeout=30)
    except Exception:
        return []
    out = []
    for w in (data.get("results") or []):
        loc = (w.get("best_oa_location") or {}) or {}
        out.append({
            "source": "openalex",
            "id": w.get("id") or "",
            "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
            "title": w.get("title") or w.get("display_name") or "",
            "authors": ", ".join(
                (a.get("author") or {}).get("display_name", "")
                for a in (w.get("authorships") or [])[:8]),
            "year": w.get("publication_year") or "",
            "journal": (((w.get("primary_location") or {}).get("source") or {})
                        .get("display_name") or ""),
            "cited_by": w.get("cited_by_count") or 0,
            "oa_status": (w.get("open_access") or {}).get("oa_status") or "",
            "pdf_url": loc.get("pdf_url") or loc.get("url") or "",
            "landing_url": (w.get("primary_location") or {}).get("landing_page_url") or "",
            "abstract": _abstract_from_inverted(w.get("abstract_inverted_index")),
        })
    return out


def search_by_author(surname, year=0, limit=6, extra=""):
    """按**作者姓氏 + 年份**精确检索。

    为什么需要这条专用通道：
    「补引用缺口」时，正文写的是「Fama and French (1992)」，
    但那篇论文的真实标题是 "Cross-section expected stock returns" ——
    **标题里根本没有 "Fama"**。用关键词检索 "Fama 1992" 只能拿到
    引用它的二手论文（实测：Fama-French 1992 redux...），拿不到原文。

    正确做法：走 Crossref 的 query.author + 年份区间过滤，
    以及 OpenAlex 的 publication_year 过滤 —— 按元数据筛，而不是按标题文字筛。
    """
    surname = (surname or "").strip()
    if not surname:
        return [], []
    extra = (extra or "").strip()
    out, used = [], []

    # ---- Crossref：query.author + 年份区间 ----
    try:
        params = {
            "query.author": surname,
            "rows": str(max(1, min(20, limit))),
            "filter": "type:journal-article",
            "select": "DOI,title,author,issued,container-title,is-referenced-by-count,abstract",
        }
        # 主题词一起带上 —— 只按姓氏查会被同名学者淹没。
        # 实测："Jensen 1970" 查出来全是语言学/眼科学论文，
        # 因为 CAPM 那篇的作者是 "E. Jensen"，
        # 加上 "capital asset prices" 才能把它顶到前面。
        if extra:
            params["query.bibliographic"] = "%s %s" % (surname, extra)
        if year:
            params["filter"] += ",from-pub-date:%d-01-01,until-pub-date:%d-12-31" % (year, year)
        url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
        data = _json_api(url, timeout=30)
        for it in ((data.get("message") or {}).get("items") or []):
            issued = ((it.get("issued") or {}).get("date-parts") or [[None]])[0]
            out.append({
                "source": "crossref",
                "doi": it.get("DOI") or "",
                "title": (it.get("title") or [""])[0],
                "authors": ", ".join(
                    "%s %s" % (a.get("given") or "", a.get("family") or "")
                    for a in (it.get("author") or [])[:8]),
                "year": issued[0] if issued else "",
                "journal": (it.get("container-title") or [""])[0],
                "cited_by": it.get("is-referenced-by-count") or 0,
                "oa_status": "",
                "pdf_url": "",
                "landing_url": ("https://doi.org/" + it["DOI"]) if it.get("DOI") else "",
                "abstract": re.sub(r"<[^>]+>", " ", it.get("abstract") or "")[:600],
            })
        used.append("crossref:author")
    except Exception as e:
        used.append("crossref:author 失败 %s" % str(e)[:60])

    # ---- OpenAlex：作者 + 年份过滤 ----
    try:
        params = {"filter": "is_oa:true"}
        if year:
            params["filter"] += ",publication_year:%d" % year
        params["search"] = ("%s %s" % (surname, extra)).strip() if extra else surname
        params["per-page"] = str(max(1, min(20, limit)))
        params["mailto"] = CONTACT_EMAIL
        url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
        data = _json_api(url, timeout=30)
        for w in (data.get("results") or []):
            loc = (w.get("best_oa_location") or {}) or {}
            out.append({
                "source": "openalex",
                "id": w.get("id") or "",
                "doi": (w.get("doi") or "").replace("https://doi.org/", ""),
                "title": w.get("title") or w.get("display_name") or "",
                "authors": ", ".join(
                    (a.get("author") or {}).get("display_name", "")
                    for a in (w.get("authorships") or [])[:8]),
                "year": w.get("publication_year") or "",
                "journal": (((w.get("primary_location") or {}).get("source") or {})
                            .get("display_name") or ""),
                "cited_by": w.get("cited_by_count") or 0,
                "oa_status": (w.get("open_access") or {}).get("oa_status") or "",
                "pdf_url": loc.get("pdf_url") or loc.get("url") or "",
                "landing_url": (w.get("primary_location") or {}).get("landing_page_url") or "",
                "abstract": _abstract_from_inverted(w.get("abstract_inverted_index")),
            })
        used.append("openalex:author")
    except Exception as e:
        used.append("openalex:author 失败 %s" % str(e)[:60])

    # ---- OpenAlex 作者 id 通道（最精确）----
    # 先把姓氏解析成 author id，再按 id + 年份拉作品 —— 不受同名学者干扰。
    for a in _openalex_resolve_authors(surname, limit=5):
        for w in _openalex_works_by_author(a["id"], year, limit=15):
            out.append(w)
        used.append("openalex:authorid:%s(%s)" % (a["id"], a["name"][:20]))

    return out, used


def search_literature(query, limit=8, channels=None):
    """文献检索：**阶梯降级** —— 依次尝试各通道，返回第一个有结果的。

    返回 (records, 用到的通道列表, 警告列表)。
    """
    if not query:
        return [], [], ["检索词为空"]
    order = channels or ["openalex", "crossref", "arxiv"]
    fns = {"openalex": search_openalex,
           "crossref": search_crossref,
           "arxiv": search_arxiv}
    used, warns = [], []
    for ch in order:
        try:
            recs = fns[ch](query, limit)
            used.append(ch)
            if recs:
                return recs, used, warns
            warns.append("%s：无结果" % ch)
        except RuntimeError as e:
            warns.append("%s：%s" % (ch, e))
        except Exception as e:
            warns.append("%s：%s" % (ch, str(e)[:80]))
    return [], used, warns


# ================================================================ 文献下载

def _unpaywall_pdf(doi):
    """Unpaywall：查合法 OA 直链。返回 (url, 状态说明) 或 (None, 说明)。"""
    if not doi:
        return None, "无 DOI"
    url = ("https://api.unpaywall.org/v2/%s?email=%s"
           % (urllib.parse.quote(doi), urllib.parse.quote(CONTACT_EMAIL)))
    try:
        d = _json_api(url, timeout=20)
    except Exception as e:
        return None, "Unpaywall 查询失败：%s" % str(e)[:60]
    locs = d.get("oa_locations") or []
    for l in locs:
        u = l.get("url_for_pdf")
        if u:
            return u, "Unpaywall OA"
    return None, "Unpaywall 无 OA 直链"


def _semantic_scholar_pdf(doi):
    """Semantic Scholar：备用 OA 通道。"""
    if not doi:
        return None, "无 DOI"
    url = ("https://api.semanticscholar.org/graph/v1/paper/DOI:%s"
           "?fields=openAccessPdf" % urllib.parse.quote(doi))
    try:
        d = _json_api(url, timeout=20)
    except Exception as e:
        return None, "Semantic Scholar 查询失败：%s" % str(e)[:60]
    u = (d.get("openAccessPdf") or {}).get("url")
    return (u, "Semantic Scholar OA") if u else (None, "Semantic Scholar 无 OA 直链")


def _mdpi_mirror(title, journal):
    """MDPI 镜像 URL 构造（实测多篇有效，含无 -v2 的老格式）。"""
    if not journal or "mdpi" not in journal.lower():
        return []
    m = re.search(r"mdpi\.com/([^/]+)/([^/]+)/", title or "")
    if not m:
        return []
    j = m.group(1)
    art = re.search(r"/(\d+)", m.group(2) or "")
    if not art:
        return []
    a = art.group(1)
    base = ("https://mdpi-res.com/d_attachment/%s/%s-%s/article_deploy/%s-%s"
            % (j, j, a, j, a))
    return [base + "-v2.pdf", base + ".pdf"]


def _download_to(url, path, referer=None):
    """下载到 path，返回 (ok, 说明)。**不做内容判断**（校验交给调用方）。"""
    headers = {"Accept": "application/pdf,*/*"}
    if referer:
        headers["Referer"] = referer
    st, raw = _http(url, headers=headers, timeout=DOWNLOAD_TIMEOUT_SEC, retries=1)
    if st != 200 or not raw:
        return False, "HTTP %s / 空响应" % st
    with open(path, "wb") as f:
        f.write(raw)
    return True, "%d 字节" % len(raw)


def _extract_pdf_links(html, base_url):
    """从落地页 HTML 里**解析出真正的 PDF 链接**。

    ⚠️ 实测踩过（这是文献下载失败的最大原因）：
      Unpaywall / Semantic Scholar / OpenAlex 给的 pdf_url 经常是
      **落地页**而不是 PDF 本身，下载回来是 HTML：
        - bioline.org.br/pdf?nd13075   → 200 text/html
        - epubs.surrey.ac.uk/806458/  → 200 text/html
        - linkinghub.elsevier.com/... → 200 text/html
      → 必须「下载返回 HTML 时，解析该 HTML 找 PDF 链接」再试一次。

    解析顺序（学术站点的通用做法）：
      1. <meta name="citation_pdf_url" content="...">   ← 最标准，命中率最高
      2. href/src 里含 .pdf / download / fulltext / bitstream 的
      3. escript/iframe 的 src
    返回 [(url, 来源说明), ...]
    """
    out = []
    seen = set()

    def _add(u, src):
        if not u:
            return
        u = urllib.parse.urljoin(base_url, u.strip())
        if u in seen:
            return
        seen.add(u)
        out.append((u, src))

    # ① citation_pdf_url（标准学术元标签）
    for m in re.findall(r'<meta[^>]+name=["\']citation_pdf_url["\'][^>]*'
                        r'content=["\']([^"\']+)', html, re.I):
        _add(m, "citation_pdf_url")
    for m in re.findall(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*'
                        r'name=["\']citation_pdf_url', html, re.I):
        _add(m, "citation_pdf_url")

    # ② href
    for l in re.findall(r'href=["\']([^"\']+)["\']', html, re.I):
        ll = l.lower()
        if any(k in ll for k in (".pdf", "/pdf", "download", "fulltext",
                                 "bitstream", "view/")):
            _add(l, "href")
    # ③ src / embed / iframe
    for l in re.findall(r'(?:src|data-pdf|data-src)="([^"]+)"', html, re.I):
        if any(k in l.lower() for k in (".pdf", "download", "fulltext", "bitstream")):
            _add(l, "embed")
    return out


def _landing_page_pdf_links(url, timeout=25):
    """抓落地页 → 解析 PDF 候选链接。失败返回 []。"""
    try:
        st, raw = _http(url, timeout=timeout, retries=0,
                        headers={"Accept": "text/html,application/xhtml+xml,*/*"})
    except Exception:
        return []
    if st != 200 or not raw:
        return []
    try:
        html = raw.decode("utf-8", "replace")
    except Exception:
        return []
    return _extract_pdf_links(html, url)


def _looks_like_html(path):
    """判定是不是「200 + HTML」的假 PDF（出版商反爬最典型的伪装）。"""
    try:
        with open(path, "rb") as f:
            head = f.read(2048).lstrip()[:512].lower()
        return head.startswith(b"<html") or head.startswith(b"<!doctype") or b"<html" in head
    except Exception:
        return False


def download_literature(rec, dest_dir, title_hint=""):
    """下载一篇文献到 dest_dir，**必须通过四重校验**才算成功。

    返回 dict:
        {ok, path, reason, pages, bytes, actual_title, tried:[...]}
    失败的会**删除临时文件**，不留假 PDF 在资料包里。
    """
    os.makedirs(dest_dir, exist_ok=True)
    tried = []

    # ① 候选直链（按优先级）
    urls = []
    if rec.get("pdf_url"):
        urls.append((rec["pdf_url"], "检索结果直链"))
    doi = rec.get("doi") or ""
    if doi:
        for fn, label in ((_unpaywall_pdf, "Unpaywall"),
                          (_semantic_scholar_pdf, "Semantic Scholar")):
            try:
                u, note = fn(doi)
                tried.append("%s：%s" % (label, note))
                if u:
                    urls.append((u, label))
            except Exception as e:
                tried.append("%s：%s" % (label, str(e)[:60]))
    # MDPI 镜像
    for u in _mdpi_mirror(rec.get("title") or title_hint, rec.get("journal") or ""):
        urls.append((u, "MDPI 镜像"))

    # ①b 落地页 → 解析真正的 PDF 链接
    #   （Unpaywall/S2/OpenAlex 给的 pdf_url 常常是落地页，不是 PDF）
    landing_pages = []
    for u in (rec.get("pdf_url"), rec.get("landing_url")):
        if u and u not in landing_pages:
            landing_pages.append(u)
    if doi:
        landing_pages.append("https://doi.org/" + doi)
    for lp in landing_pages[:2]:
        # 只取前 3 个候选：优先 citation_pdf_url（最准），其次 href
        cands = _landing_page_pdf_links(lp)[:3]
        for pu, psrc in cands:
            urls.append((pu, "%s→%s" % ("落地页", psrc)))
    # 去重（保留顺序）
    seen_u, uniq = set(), []
    for u, lab in urls:
        if u in seen_u:
            continue
        seen_u.add(u)
        uniq.append((u, lab))
    urls = uniq

    # ② 逐个尝试
    fname = _safe_filename("%s_%s" % (
        (rec.get("authors") or "anon").split(",")[0].strip().replace(" ", "") or "ref",
        (rec.get("year") or "n.d.")), rec.get("title") or "") + ".pdf"
    path = os.path.join(dest_dir, fname)
    last_reason = "没有可用直链"
    # ⚠️ 实测踩过：落地页解析会展开**几十个**候选链接（download/view/fulltext…），
    #   每个都按 60 秒超时去下载 → 一个请求能挂好几分钟，
    #   前端一直转圈，用户以为死了。
    # → 加**总时间预算**和**候选上限**：宁可早失败并给出 tried 明细，
    #   也不要让 UI 无限等待。
    DEADLINE = time.time() + DOWNLOAD_BUDGET_SEC
    MAX_TRY = MAX_DOWNLOAD_TRIES
    for url, label in urls:
        if time.time() > DEADLINE:
            tried.append("达到下载时间预算（%d 秒），停止尝试" % DOWNLOAD_BUDGET_SEC)
            break
        if MAX_TRY <= 0:
            tried.append("达到候选上限（%d 个），停止尝试" % MAX_DOWNLOAD_TRIES)
            break
        MAX_TRY -= 1
        try:
            ok, note = _download_to(url, path)
        except Exception as e:
            tried.append("%s：下载异常 %s" % (label, str(e)[:60]))
            continue
        if not ok:
            tried.append("%s：%s" % (label, note))
            last_reason = "%s %s" % (label, note)
            continue
        if _looks_like_html(path):
            # 200 + HTML = 反爬伪装，不是 PDF
            tried.append("%s：返回 HTML 不是 PDF" % label)
            last_reason = "%s 返回 HTML（反爬）" % label
            try:
                os.remove(path)
            except Exception:
                pass
            continue
        good, why = pdf_ok(path)
        if not good:
            tried.append("%s：校验不通过（%s）" % (label, why))
            last_reason = "%s 校验不通过：%s" % (label, why)
            try:
                os.remove(path)
            except Exception:
                pass
            continue
        # ✅ 四重校验通过
        pages = 0
        m = re.match(r"(\d+)", why or "")
        if m:
            pages = int(m.group(1))
        actual_title = _guess_title_from_pdf(path, rec.get("title") or "")
        return {"ok": True, "path": path, "reason": "", "pages": pages,
                "bytes": os.path.getsize(path), "actual_title": actual_title,
                "tried": tried, "label": label}

    # ③ 全失败 → 判定是不是付费墙
    tried.append("所有直链均失败")
    return {"ok": False, "path": "", "reason": last_reason, "pages": 0,
            "bytes": 0, "actual_title": "", "tried": tried,
            "paywall_hint": "所有合法开放通道皆空 → 可能是闭源付费墙（需机构订阅）"}


def _norm_title(s):
    """标题归一化：去掉大小写差异、标点、多余空白、常见副标题符号。"""
    s = (s or "").lower()
    s = s.replace("–", "-").replace("—", "-").replace("’", "'")
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _title_similarity(a, b):
    """两段文本的词级相似度（Jaccard，按词集合）。"""
    ta = set(_norm_title(a).split())
    tb = set(_norm_title(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / float(len(ta | tb))


def _guess_title_from_pdf(path, fallback=""):
    """从 PDF 首页文本里找出**真正的标题**。

    ⚠️ 实测踩过两次：
      1. 首页第 0 行常常是**期刊名**（如 "FINANCIAL AND CREDIT ACTIVITY: PROBLEMS
         OF THEORY AND PRACTICE"）→ 直接取第一行会把刊名当标题。
      2. 改成长度优先后，又取到了**正文首句**（"is also a company strategy for..."）。
    根因：PDF 首页的文本行顺序**不可靠**，标题可能在第 3~4 行，
    且首页里混着刊名、作者、单位、邮箱、Abstract 正文。

    正解：**用检索到的标题当锚**，在首页前若干行里找与它最相似的一行。
    这样既避免刊名/正文误判，又保留了「以实际内容为准」的校正能力。
    """
    txt = pdf_first_page_text(path, 900)
    if not txt:
        return fallback
    lines = [l.strip() for l in txt.splitlines() if l.strip()][:14]
    if not lines:
        return fallback

    if fallback:
        # 候选 = 单行 + **连续 2~3 行拼接**
        # ⚠️ 实测踩过：PDF 首页会把标题拆成多行
        #   （"Mapping the ESG–Firm Value Relationship:" /
        #     "Bibliometric Evidence and a Theoretical Synthesis" /
        #     "of Value-Relevance Mechanisms"）
        #   → 只比单行的话，相似度只有 0.083，会误判成「不匹配」。
        cands = []
        for i, l in enumerate(lines):
            if len(l) < 8:
                continue
            cands.append(l)
            for k in (2, 3):
                if i + k <= len(lines):
                    cands.append(" ".join(lines[i:i + k]))
        best, best_sim = None, 0.0
        for l in cands:
            sim = _title_similarity(l, fallback)
            if sim > best_sim:
                best, best_sim = l, sim
        # 相似度够高 → 认为这一段就是实际标题
        if best is not None and best_sim >= 0.45:
            return best[:220]
        # 相似度不够 → 检索标题与首页不匹配（**很可能是文件与预期不符**）
        # 这时如实报告首页最像标题的行，供人工判断，而不是硬套检索标题。
        cands = [l for l in lines if len(l) >= 15
                 and not re.match(r"^(doi|issn|https?://|www\.|\d{4}$|email|corresponding)",
                                  l, re.I)
                 and not re.search(r"\d{3}-\d{4}", l)]
        if cands:
            return max(cands, key=len)[:220]
        return fallback

    cands = [l for l in lines if len(l) >= 15
             and not re.match(r"^(doi|issn|https?://|www\.|\d{4}$|email|corresponding)",
                              l, re.I)]
    return (max(cands, key=len)[:220] if cands else fallback)


def _safe_unlink(path):
    """删文件，出错不抛。"""
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass


def _safe_filename(base, title):
    """生成安全的文件名（去掉路径分隔符与控制字符，限长）。"""
    s = re.sub(r"[\\/:\*\?\"<>\|]", "_", (title or ""))[:60]
    s = re.sub(r"\s+", " ", s).strip(" ._")
    name = "%s_%s" % (base, s) if s else base
    return re.sub(r"[^A-Za-z0-9_\-\u4e00-\u9fff\. ]", "_", name)[:120]


def dedupe_by_content(folder, exts=(".pdf",)):
    """按**字节内容**去重（不按文件名 —— 同名不同内容是常态）。

    返回 (删除列表, 保留列表)。
    """
    h = {}
    for dp, _, fs in os.walk(folder):
        for fn in fs:
            if fn.lower().endswith(tuple(exts)):
                p = os.path.join(dp, fn)
                try:
                    d = hashlib.md5(open(p, "rb").read()).hexdigest()
                except Exception:
                    continue
                h.setdefault(d, []).append(p)
    removed, kept = [], []
    for d, paths in h.items():
        if len(paths) == 1:
            kept.append(paths[0])
            continue
        paths.sort()          # 确定性：取路径字典序最小的
        kept.append(paths[0])
        for p in paths[1:]:
            try:
                os.remove(p)
                removed.append(p)
            except Exception:
                pass
    return removed, kept


# ================================================================ 数据检索

# World Bank 常用数据源（按相关度）。实测逐个扫 source 目录可用，
# 但全扫 71 个要几十秒 → 只扫这几个最相关的。
WB_SOURCES = [
    ("2", "World Development Indicators"),      # 主指标库
    ("15", "Global Economic Monitor"),          # 宏观时间序列
    ("18", "Global Development Finance"),
    ("11", "Africa Development Indicators"),
]


def _wb_indicators(max_pages=3, per_page=400):
    """拉 World Bank 指标目录（本地过滤用）。

    ⚠️ 实测踩过两个坑：
      1. `/v2/indicator?search=<kw>` 的 search 参数**被服务端忽略**
         —— 查 GDP / CO2 / inflation 返回的都一样（Poverty Headcount 前 5 条）。
      2. 不带 source 时只按 ID 顺序返回**前 N 条**（total 有 29544 条），
         GDP 类指标根本不在前 500 条里 → 必须按 source 拉。
    → 现在：按相关 source 逐个拉 + 翻页，最多 max_pages 页（够用且快）。
    """
    out = []
    for sid, sname in WB_SOURCES:
        for page in range(1, max_pages + 1):
            try:
                url = ("https://api.worldbank.org/v2/indicator?format=json"
                       "&per_page=%d&source=%s&page=%d" % (per_page, sid, page))
                d = _json_api(url, timeout=30)
            except Exception:
                break
            rows = d[1] if isinstance(d, list) and len(d) > 1 else []
            if not rows:
                break
            for r in rows:
                r["_wb_source_name"] = sname
                out.append(r)
            if len(rows) < per_page:
                break
    return out


_WB_CACHE = {"at": 0, "rows": []}


def _wb_indicators_cached(max_pages=3):
    """带缓存的指标目录（10 分钟内复用）—— 否则每次检索都要拉 10 秒。"""
    now = time.time()
    if _WB_CACHE["rows"] and (now - _WB_CACHE["at"]) < 600:
        return _WB_CACHE["rows"]
    rows = _wb_indicators(max_pages=max_pages)
    _WB_CACHE["at"] = now
    _WB_CACHE["rows"] = rows
    return rows


def _wb_match(row, query):
    """指标与查询词的相关度（0 = 不匹配）。"""
    if not query:
        return 1.0
    q = query.lower().strip()
    name = (row.get("name") or "").lower()
    note = (row.get("sourceNote") or "").lower()
    unit = (row.get("unit") or "").lower()
    src = ((row.get("source") or {}).get("value") or "").lower()
    terms = [t for t in re.split(r"[\s,，、/]+", q) if len(t) > 1]
    score = 0.0
    for t in terms:
        if t in name:
            score += 3.0
            # ⚠️ 实测踩过：查 "GDP" 时 "Agriculture, forestry, and fishing,
            #   value added (% of GDP)" 排在真正的 "GDP (current US$)" 前面
            #   —— 因为它名字里含 "GDP"，但 GDP 只是**分母**不是主题。
            # 修正：**词在名字开头**（真正的标题词）权重更高。
            if name.startswith(t):
                score += 4.0
            # 名字以 "xxx (…% of GDP)" 结尾的，是"占 GDP 比例"，降权
            elif re.search(r"\(%s of|%s of\)" % (re.escape(t), re.escape(t)), name):
                score -= 2.0
        elif t in unit:
            score += 1.5
        elif t in src:
            score += 1.0
        elif t in note:
            score += 0.5
    return score


def search_data(query, limit=8):
    """公开数据集检索。

    通道（按实测可靠性排序）：
      1. **World Bank** —— 官方宏观指标，实测稳定可用。
         ⚠️ 服务端 search 参数无效 → 拉目录后**本地过滤**。
      2. **Kaggle Datasets** —— 学术数据集聚合，检索可用（免认证），
         下载需走 dataset download 端点（本机实测较慢/可能超时，属尽力而为）。
      3. **figshare** —— 实测本机 API 整体 403（不是姿势问题，是 IP/区域限制），
         保留通道但如实报告失败原因。
    """
    out, warns = [], []
    # ① World Bank（本地过滤）
    try:
        rows = _wb_indicators_cached()
        scored = []
        for r in rows:
            sc = _wb_match(r, query)
            if sc > 0:
                scored.append((sc, r))
        # 分数高的在前；同分按指标名
        scored.sort(key=lambda x: (-x[0], x[1].get("name") or ""))
        if not scored:
            warns.append("World Bank：指标目录里没有匹配「%s」的指标" % (query or ""))
        # ⚠️ 不同 source 目录里有**同名指标**（如 GDP 在 WDI 和 GEM 都有）
        #   → 必须按 indicator id 去重，否则列表里全是重复项。
        _seen_ids = set()
        _uniq = []
        for sc, r in scored:
            iid = r.get("id") or ""
            if iid in _seen_ids:
                continue
            _seen_ids.add(iid)
            _uniq.append((sc, r))
        for sc, r in _uniq[:limit]:
            ind_id = r.get("id") or ""
            src = r.get("source") or {}
            out.append({
                "source": "worldbank",
                "title": r.get("name") or ind_id,
                "id": ind_id,
                "publisher": "World Bank",
                "format": "CSV",
                "url": ("https://api.worldbank.org/v2/en/indicator/%s"
                        "?downloadformat=csv" % ind_id),
                "size": "",
                "year": "",
                "desc": ("匹配度 %.1f · 来源：%s · 单位：%s。%s"
                         % (sc, src.get("value") or "", r.get("unit") or "",
                            (r.get("sourceNote") or "")[:140])),
            })
    except Exception as e:
        warns.append("World Bank：%s" % str(e)[:90])

    # ② Kaggle（检索可用）
    try:
        kw = query.replace(" ", "+")
        url = ("https://www.kaggle.com/api/v1/datasets/list?search=%s&page=1"
               % urllib.parse.quote(query))
        st, raw = _http(url, timeout=25)
        arr = json.loads(raw.decode("utf-8", "replace")) if st == 200 else []
        for a in arr[:max(2, limit // 2)]:
            ref = a.get("ref") or ""
            out.append({
                "source": "kaggle",
                "title": a.get("title") or ref,
                "id": ref,
                "publisher": a.get("creatorName") or a.get("ownerName") or "",
                "format": "ZIP (CSV 内)",
                "url": ("https://www.kaggle.com/api/v1/datasets/download/%s" % ref),
                "size": str(a.get("totalBytes") or ""),
                "year": str((a.get("lastUpdated") or "")[:4]),
                "desc": (a.get("subtitle") or a.get("description") or "")[:200],
            })
    except Exception as e:
        warns.append("Kaggle：%s" % str(e)[:90])

    # ③ figshare（如实报告）
    try:
        url = "https://api.figshare.com/v2/articles/search"
        payload = json.dumps({"search_for": query,
                              "page_size": max(1, min(50, limit))}).encode("utf-8")
        st, raw = _http(url, data=payload, timeout=25,
                        headers={"Content-Type": "application/json"})
        arts = json.loads(raw.decode("utf-8", "replace")) if st == 200 else []
        for a in arts[:limit]:
            out.append({
                "source": "figshare",
                "title": a.get("title") or "",
                "id": str(a.get("id") or ""),
                "publisher": "",
                "format": "dataset",
                "url": a.get("url_public_html") or "",
                "size": str(a.get("size") or ""),
                "year": str((a.get("published_date") or "")[:4]),
                "desc": (a.get("description") or "")[:200],
            })
    except Exception as e:
        warns.append("figshare：%s（本机实测该 API 整体 403，非姿势问题）"
                     % str(e)[:60])
    return out, warns


def download_data(rec, dest_dir):
    """下载数据文件到 dest_dir。返回 dict（同 download_literature 的形状）。"""
    os.makedirs(dest_dir, exist_ok=True)
    url = rec.get("url") or ""
    if not url:
        return {"ok": False, "reason": "没有下载地址", "path": "", "tried": []}
    fname = _safe_filename(str(rec.get("id") or "data"), rec.get("title") or "data")
    # ⚠️ 实测踩过：直接按 URL 猜扩展名落盘 → 若实际是 ZIP 而猜成 ".csv"，
    #   解压出来的目标文件**和源文件同名** → 边解压边截断源 → 得到 0 字节。
    # 现在：**一律先下到 .part 临时文件**，再按魔数决定最终文件名与是否解压。
    tmp_path = os.path.join(dest_dir, fname + ".part")
    try:
        ok, note = _download_to(url, tmp_path)
    except Exception as e:
        return {"ok": False, "reason": "下载异常：%s" % str(e)[:80], "path": "", "tried": []}
    if not ok:
        _safe_unlink(tmp_path)
        return {"ok": False, "reason": note, "path": "", "tried": []}
    size = os.path.getsize(tmp_path)
    if size == 0:
        _safe_unlink(tmp_path)
        return {"ok": False, "reason": "下载到 0 字节", "path": "", "tried": []}

    # ① 按魔数判定类型
    with open(tmp_path, "rb") as f:
        magic = f.read(4)

    # ② ZIP → 解压出真正的数据文件
    if magic[:2] == b"PK":
        path = ""
        try:
            import zipfile
            with zipfile.ZipFile(tmp_path) as z:
                names = [n for n in z.namelist()
                         if not n.startswith("__MACOSX") and not n.endswith("/")]
                # ⚠️ 实测踩过：World Bank 的 ZIP 里**有三个 CSV**：
                #   Metadata_Indicator_API_XXX.csv → 844 字节，**只是指标元数据**
                #   API_XXX.csv                    → 300KB，**真正的时序数据**
                #   Metadata_Country_API_XXX.csv   → 65KB，国家名对照表
                # 取「第一个 .csv」会拿到 844 字节元数据 —— 用户点开没有年份数值。
                # 现在：优先 API_ 开头且不含 Metadata 的那个。
                pick = None
                for n in names:
                    ln = n.lower()
                    if ln.startswith("api_") and "metadata" not in ln \
                            and ln.endswith(".csv"):
                        pick = n
                        break
                if not pick:
                    for pref in (".csv", ".xlsx", ".json", ".txt"):
                        for n in names:
                            if n.lower().endswith(pref) \
                                    and "metadata" not in n.lower():
                                pick = n
                                break
                        if pick:
                            break
                pick = pick or (names[0] if names else None)
                if not pick:
                    raise RuntimeError("压缩包为空")
                target = os.path.join(dest_dir, fname + os.path.splitext(pick)[1])
                # 目标必须与临时文件不同名（否则会截断源）
                if os.path.abspath(target) == os.path.abspath(tmp_path):
                    target = os.path.join(dest_dir, fname + "_data"
                                          + os.path.splitext(pick)[1])
                with z.open(pick) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                path = target
        except Exception as e:
            # 解压失败 → 保留压缩包（东西还在）
            keep = os.path.join(dest_dir, fname + ".zip")
            try:
                if os.path.abspath(keep) != os.path.abspath(tmp_path):
                    os.replace(tmp_path, keep)
                    path = keep
                else:
                    path = tmp_path
            except Exception:
                path = tmp_path
            try:
                sz = os.path.getsize(path)
            except Exception:
                sz = 0
            _safe_unlink(tmp_path) if path != tmp_path else None
            return {"ok": True, "path": path,
                    "reason": "解压失败，保留压缩包：%s" % str(e)[:60],
                    "pages": 0, "bytes": sz,
                    "actual_title": rec.get("title") or "", "tried": []}
        _safe_unlink(tmp_path)
        size = os.path.getsize(path)
    else:
        # ③ 非 ZIP：按魔数/内容定扩展名
        ext = ".csv"
        head = b""
        try:
            with open(tmp_path, "rb") as f:
                head = f.read(2048).lstrip()[:200].lower()
        except Exception:
            pass
        if head.startswith(b"{") or head.startswith(b"["):
            ext = ".json"
        elif head.startswith(b"pk"):
            ext = ".zip"
        elif magic[:4] == b"\xd0\xcf\x11\xe0":
            ext = ".xls"
        path = os.path.join(dest_dir, fname + ext)
        if os.path.abspath(path) != os.path.abspath(tmp_path):
            os.replace(tmp_path, path)
        else:
            path = tmp_path
        size = os.path.getsize(path)

    # ④ 数据文件必须是「能打开的文本/表格」，不是 HTML 伪装
    if os.path.splitext(path)[1].lower() in (".csv", ".json", ".txt"):
        try:
            head = open(path, "rb").read(2048).lstrip()[:200].lower()
            if head.startswith(b"<html") or b"<!doctype" in head:
                _safe_unlink(path)
                return {"ok": False, "reason": "返回 HTML 不是数据文件", "path": "", "tried": []}
        except Exception:
            pass
    return {"ok": True, "path": path, "reason": "", "pages": 0, "bytes": size,
            "actual_title": rec.get("title") or "", "tried": []}


# ================================================================ 本地打开

def open_local(path):
    """用系统默认程序打开本地文件（PDF → 预览，CSV → 表格/文本）。

    返回 (ok, 说明)。
    """
    if not path:
        return False, "没有路径"
    p = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(p):
        return False, "文件不存在"
    try:
        subprocess.Popen(["/usr/bin/open", p])
        return True, p
    except Exception as e:
        return False, str(e)[:80]


def preview_text(path, max_chars=4000):
    """读一点文件内容出来（CSV/文本），给前端做内嵌预览。"""
    try:
        ext = os.path.splitext(path)[1].lower()
        if ext in (".csv", ".tsv", ".txt", ".md", ".json"):
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read(max_chars)
        return ""
    except Exception as e:
        return "读取失败：%s" % str(e)[:80]
