#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_draft_refs.py — 初稿参考文献表 → Crossref 权威核验 → 可执行修正清单
=============================================================================
为什么存在
----------
2026-11-26 上午拿到《金融压力对可再生能源股票指数波动率影响的实证研究》初稿
（澳科大本科毕业设计，22 条参考文献）。人工浏览已发现若干可疑条目：

  · Trabelsi, N., et al. (2023) — 标题实际是 Dias, R., Chambino, M., … 的论文
  · Zavadska, M., et al. (2023) — 标题实际也是 Dias 团队的另一篇
  · Subramaniam & Masron (2022). Resources Policy 78, 102876 — 文章号疑为 102860
  · ETFdb (2026) / KoalaGains (2026) / iAltEnergy (2024) — 网页/blog 当学术引用
  · 正文引了 Sims (1980) 但参考文献表里没有

这些靠肉眼看必漏。本脚本把「初稿 22 条参考文献」全部送进 Crossref 做 DOI 级核验，
输出**逐条判定 + 具体改法**，可直接贴进修改说明。

用法
----
    python3 verify_draft_refs.py --docx <初稿.docx> [--out reports/]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import zipfile
from typing import Dict, List, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fars_data import crossref_verify_batch, md_report   # noqa: E402


# ============================================================
# 1) 从 .docx 抽参考文献段
# ============================================================
def docx_paragraphs(docx_path: str) -> List[str]:
    """返回 .docx 里所有非空段落的纯文本（按顺序）。"""
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    out = []
    for m in re.finditer(r"<w:p\b[^>]*>(.*?)</w:p>", xml, re.S):
        p = m.group(1)
        # 只取 w:t（真文本），排除 w:tab / w:instrText
        txt = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", p, re.S))
        for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                     ("&quot;", '"'), ("&apos;", "'")):
            txt = txt.replace(a, b)
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            out.append(txt)
    return out


def split_refs(paras: List[str]) -> List[str]:
    """定位参考文献表（最后一个「参考文献」标题之后、「附录」之前）。"""
    starts = [i for i, t in enumerate(paras) if t.strip().startswith("参考文献")]
    if not starts:
        return []
    seg = paras[starts[-1] + 1:]
    ends = [i for i, t in enumerate(seg) if t.strip().startswith("附录")]
    if ends:
        seg = seg[:ends[0]]
    return [s for s in seg if len(s) > 40]


# ============================================================
# 2) APA 风格的参考文献 → 结构化
# ============================================================
# "Surname, A., & Surname, B. (2011). Title. Journal, 78, 102876. Extra"
_YEAR = re.compile(r"\((?:19|20)\d{2}[a-z]?\)")


def parse_ref(raw: str) -> Optional[Dict]:
    m = _YEAR.search(raw)
    if not m:
        return None
    year = int(m.group(0)[1:5])
    before = raw[:m.start()].strip().rstrip(".").strip()
    after = raw[m.end():].strip()

    # 作者段 → 姓氏列表
    authors: List[str] = []
    for chunk in re.split(r",\s*&\s*|\s+&\s+|,\s*et al\.?$", before):
        chunk = chunk.strip()
        if not chunk:
            continue
        fam = chunk.split(",")[0].strip()
        if fam and re.match(r"^[A-Z][A-Za-z'’\-]+", fam):
            authors.append(fam)
    if re.search(r"et al", before):
        authors.append("__etal__")

    # 标题 = 第一个句点后的部分，到下一个句点（但别切在缩写里）
    body = after.lstrip(". ").strip()
    tm = re.match(r"^([^.]{8,}?)\.\s+(.*)$", body)
    if tm:
        title = tm.group(1).strip()
        rest = tm.group(2).strip()
    else:
        title, rest = body.rstrip("."), ""

    # 期刊/卷期/页 → 从 rest 里抽第一段（到句点或逗号分隔的"额外说明"前）
    tail = re.split(r"\.\s+[A-Z]", rest, maxsplit=1)
    pub = tail[0].strip().rstrip(".") if tail else rest
    extra = tail[1].strip() if len(tail) > 1 else ""

    vol = ""
    pages = ""
    vm = re.search(r",\s*(\d+)\s*(?:\(([^)]+)\))?\s*,?\s*([0-9A-Za-z\-–—]+)?", pub)
    if vm:
        vol = vm.group(1)
        pages = vm.group(3) or ""
    journal = re.split(r",\s*\d", pub)[0].strip().rstrip(".").strip()

    # 网页/学位论文/工作论文标记
    is_web = bool(re.search(r"ETFdb|KoalaGains|iAltEnergy|\.com|Hub|Site|Blog", raw))
    is_thesis = bool(re.search(r"Doctoral Dissertation|Dissertation|Thesis", raw))
    is_working = bool(re.search(r"Working Paper|preprint|Preprint", raw, re.I))

    return {
        "raw": raw, "title": title, "year": year, "journal": journal,
        "volume": vol, "pages": pages, "authors": authors,
        "doi": "", "extra": extra, "is_web": is_web,
        "is_thesis": is_thesis, "is_working": is_working,
    }


# ============================================================
# 3) 核验 + 生成修正建议
# ============================================================
def build_actions(refs: List[Dict], verified: Dict) -> List[Dict]:
    """把核验结果翻译成**可执行的修改动作**。"""
    out = []
    vmap = {}
    for v in verified.get("results", []):
        vmap[(v.get("input") or {}).get("raw", "")] = v
    for r in refs:
        v = vmap.get(r["raw"], {})
        st = v.get("status")
        au = v.get("authoritative") or {}
        act = {"n": len(out) + 1, "status": st, "raw": r["raw"],
               "input": {"title": r["title"], "year": r["year"],
                         "journal": r["journal"], "authors": r["authors"]},
               "authoritative": au, "detail": v.get("detail"), "actions": []}
        if st == "CONFIRMED":
            act["actions"].append("✅ DOI 有效且标题/期刊/年份一致 → 补写 DOI 即可")
        elif st == "NO_DOI":
            d = au.get("doi") or v.get("suggested_doi")
            if d:
                act["actions"].append("补 DOI: %s" % d)
            if au.get("authors"):
                real = ", ".join(a.split()[0] for a in au["authors"][:3])
                given = ", ".join(r["authors"][:3])
                if real and given and real.split(",")[0] != given.split(",")[0]:
                    act["actions"].append(
                        "⚠️ 作者可能张冠李戴：初稿写 %s → 权威记录是 %s 等"
                        % (given, real))
            if au.get("journal") and r["journal"] and \
               au["journal"].lower() != r["journal"].lower():
                act["actions"].append("期刊核对：初稿「%s」→ 权威「%s」"
                                      % (r["journal"], au["journal"]))
            if au.get("year") and au["year"] != r["year"]:
                act["actions"].append("年份核对：初稿 %s → 权威 %s"
                                      % (r["year"], au["year"]))
            if au.get("article_number") and r["pages"] and \
               au["article_number"] != r["pages"]:
                act["actions"].append("文章号核对：初稿 %s → 权威 %s"
                                      % (r["pages"], au["article_number"]))
        elif st == "MISMATCH":
            act["actions"].append("❌ 条目与权威记录冲突：%s" % v.get("detail"))
            if au.get("title"):
                act["actions"].append("权威标题：%s" % au["title"])
        elif st == "NOT_FOUND":
            act["actions"].append("❌ Crossref 检索不到（标题相似度不足）")
            if r["is_thesis"]:
                act["actions"].append("→ 学位论文不在 Crossref 收录范围，"
                                      "改用学校机构库链接，不要伪造 DOI")
            elif r["is_web"]:
                act["actions"].append("→ 网页/blog 源：不应作为学术引用；"
                                      "改引指数编制方一手文件（指数方法论文档）")
            else:
                act["actions"].append("→ 手动核对原刊；若为预印本，"
                                      "优先替换成已正式发表的版本")
        # 附加的类型提醒
        if r["is_web"]:
            act["actions"].append("⚠️ 非学术来源（网页/blog）→ 建议替换为"
                                  "指数编制方一手方法论文档")
        if r["is_working"]:
            act["actions"].append("ℹ️ 标注为预印本/工作论文 → 若已正式发表，"
                                  "应引正式版")
        out.append(act)
    return out


def render_actions(actions: List[Dict]) -> str:
    L = ["# 初稿参考文献修正清单（Crossref 权威核验）", "",
         "> 判定口径：CONFIRMED=DOI 有效且标题/期刊/年份一致；MISMATCH=条目与权威记录冲突；",
         "> NO_DOI=条目缺 DOI，已按标题检索到权威记录；NOT_FOUND=Crossref 检索不到。", ""]
    need = [a for a in actions if a["status"] != "CONFIRMED"]
    L += ["**共 %d 条，需处理 %d 条**" % (len(actions), len(need)), "",
          "---", ""]
    for a in actions:
        flag = {"CONFIRMED": "✅", "NO_DOI": "ℹ️", "MISMATCH": "⚠️",
                "NOT_FOUND": "❌"}.get(a["status"], "?")
        L.append("## %s %d. %s" % (flag, a["n"], a["input"]["title"][:90]))
        L.append("")
        L.append("- **初稿条目**：`%s`" % a["raw"][:220])
        au = a.get("authoritative") or {}
        if au:
            L.append("- **权威记录**：%s（%s, %s）" %
                     (au.get("title", "")[:110], au.get("journal") or "-",
                      au.get("year") or "-"))
            if au.get("doi"):
                L.append("- **DOI**：`%s`" % au["doi"])
        for act in a["actions"]:
            L.append("- %s" % act)
        L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docx", required=True)
    ap.add_argument("--out", default="")
    ap.add_argument("--json", dest="json_out", default="")
    args = ap.parse_args()

    paras = docx_paragraphs(args.docx)
    raw_refs = split_refs(paras)
    print("[1/3] 抽出参考文献 %d 条" % len(raw_refs))
    refs = []
    for r in raw_refs:
        p = parse_ref(r)
        if p:
            refs.append(p)
        else:
            print("      ⚠️ 无法解析: %s" % r[:70])
    print("[2/3] 结构化 %d 条 → 送 Crossref 核验" % len(refs))
    verified = crossref_verify_batch(refs, sleep=0.4)
    print("[3/3] 核验完成：%s" % json.dumps(verified["summary"], ensure_ascii=False))

    actions = build_actions(refs, verified)
    md = render_actions(actions)
    print()
    print(md)

    if args.out:
        os.makedirs(args.out, exist_ok=True)
        fp = os.path.join(args.out, "初稿参考文献修正清单.md")
        io.open(fp, "w", encoding="utf-8").write(md)
        print("\n✅ 修正清单: %s" % fp)
    if args.json_out:
        io.open(args.json_out, "w", encoding="utf-8").write(
            json.dumps({"summary": verified["summary"], "actions": actions},
                       ensure_ascii=False, indent=2))
        print("✅ JSON: %s" % args.json_out)


if __name__ == "__main__":
    main()
