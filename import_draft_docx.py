#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
import_draft_docx.py — 把 .docx 初稿导入 FARS（成为可继续编辑的论文）
====================================================================
为什么存在
----------
2026-11-26 主人给了《金融压力对可再生能源股票指数波动率影响的实证研究》初稿
（澳科大本科毕业设计，docx）。三个诉求：
  1. **作为后续工作的基线** → 必须落进 FARS，夜间构建才能在它之上继续写
  2. **引用要精准**       → 参考文献表要做 Crossref 权威核验并标注问题
  3. **数据要精准**       → 表格里的数字要能对上 results CSV

本脚本做第 1、2 件事：
  · 解析 docx → 按中文标题层级切成 8 个 FARS part（abstract/intro/lit/data/
    method/results/robust/conclusion）
  · 参考文献表 → 结构化 + Crossref 核验 → 写进 refs，每条带 verify 字段
  · 表格/图 → 登记进 tables/figures（保留原文档顺序与编号）
  · 数字锚点 → 抽出关键实证数字，供夜间构建做「数字对账」用

⚠️ 设计约束（和 FARS 其它工具一致）
  · **不改写正文**：导入的是初稿原话，后续修改由 FARS 编辑器/夜间构建负责
  · **不丢信息**：每条 ref 带 raw 原文，任何时候能回溯
  · **核验失败不隐藏**：NOT_FOUND 也写进 refs，带 verify.status

用法
----
    python3 import_draft_docx.py --docx 初稿.docx --code FE111234 --topic Stock \
        --title "..." [--out state/papers] [--state-dir state]
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
import zipfile
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_draft_refs import docx_paragraphs, split_refs, parse_ref   # noqa: E402
from fars_data import crossref_verify_batch                               # noqa: E402


# ============================================================
# 1) docx → 带字号的段落（用于识别标题层级）
# ============================================================
def docx_styled_paragraphs(docx_path: str) -> List[Tuple[int, str, str]]:
    """返回 [(max_sz_halfpoint, bold, text)]，只含非空段落。"""
    with zipfile.ZipFile(docx_path) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    out = []
    for m in re.finditer(r"<w:p\b[^>]*>(.*?)</w:p>", xml, re.S):
        p = m.group(1)
        szs = [int(v) for v in re.findall(r'<w:sz w:val="(\d+)"', p)]
        bold = ("<w:b/>" in p) or ("<w:b " in p)
        txt = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", p, re.S))
        for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                     ("&quot;", '"'), ("&apos;", "'")):
            txt = txt.replace(a, b)
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            out.append((max(szs) if szs else 0, bold, txt))
    return out


# ============================================================
# 2) 章节切分 → FARS parts
# ============================================================
# 中文标题 → FARS part 键
CHAPTER_MAP = [
    (re.compile(r"^摘要"), "abstract"),
    (re.compile(r"^第一章|绪论|Chapter\s*1"), "intro"),
    (re.compile(r"^第二章|文献综述|Chapter\s*2"), "lit"),
    (re.compile(r"^第三章|研究方法|Chapter\s*3"), "method"),
    (re.compile(r"^第四章|实证结果|Chapter\s*4"), "results"),
    (re.compile(r"^第五章|结论|Chapter\s*5"), "conclusion"),
    (re.compile(r"^附录"), "__appendix__"),
    (re.compile(r"^参考文献"), "__refs__"),
    (re.compile(r"^致谢"), "__ack__"),
]
# 章内小节 → part 的细分（robust 单独抓）
ROBUST_KEYS = ("稳健", "鲁棒", "异质", "敏感性", "稳健性分析")


def split_sections(styled: List[Tuple[int, str, str]]
                   ) -> Tuple[Dict[str, List[str]], List[Tuple[int, str]]]:
    """按标题切成 {part: [正文行]} 与 [表格标题]。

    标题判定：字号 ≥ 32 半磅（=16pt）且加粗，或字号 ≥ 28 且加粗且短。
    """
    parts: Dict[str, List[str]] = {}
    tables: List[Tuple[int, str]] = []
    cur = None
    cur_sub = None
    for sz, bold, txt in styled:
        is_title = bold and ((sz >= 32) or (sz >= 28 and len(txt) <= 60))
        is_sub = bold and 26 <= sz < 32 and len(txt) <= 80
        # 表格/图标题
        if re.match(r"^(表|图)\s*[0-9A-Z]", txt) and len(txt) < 120:
            tables.append((sz, txt))
            continue
        # 二级小节（1.1 / 3.2 …）保留为 markdown 标题，方便后续定位改写
        if bold and 24 <= sz < 32 and re.match(r"^\d+\.\d+(\.\d+)?\s", txt) and len(txt) <= 80:
            parts.setdefault(cur or "__pre__", []).append("## " + txt)
            continue
        if is_title:
            key = None
            for pat, k in CHAPTER_MAP:
                if pat.search(txt):
                    key = k
                    break
            if key is None and cur is not None:
                # 章内二级标题（如「1.1 研究背景」）→ 留在当前 part
                if re.match(r"^\d+\.\d+", txt):
                    cur_sub = txt
                    continue
            if key:
                cur = key
                cur_sub = None
                parts.setdefault(cur, [])
                continue
        if cur is None:
            continue
        # 稳健性相关小节单独收集
        if cur_sub and any(k in cur_sub for k in ROBUST_KEYS):
            parts.setdefault("robust", []).append("## " + cur_sub)
            parts["robust"].append(txt)
            continue
        if cur == "results" and is_sub and any(k in txt for k in ROBUST_KEYS):
            cur_sub = txt
            parts.setdefault("robust", []).append("## " + txt)
            continue
        parts[cur].append(txt)
    return parts, tables


# ============================================================
# 3) 关键数字锚点（供夜间构建做数字对账）
# ============================================================
def extract_number_anchors(text: str) -> List[str]:
    """抽出正文里的关键实证数字（系数/t/卡方/p/峰值等）。"""
    pats = [
        r"\b\d\.\d{4,6}\b",                 # 系数 0.141167
        r"\bt\s*[=＝]\s*-?\d+\.\d+\b",      # t = 3.12
        r"\bp\s*[=＝]\s*0\.\d+\b",          # p = 0.0002
        r"\bχ²\s*[（(]\s*\d+\s*[)）]\s*[=＝]\s*\d+\.\d+",  # 卡方
        r"\bN\s*[=＝]\s*\d{2,4}\b",
    ]
    out = []
    for p in pats:
        out += re.findall(p, text)
    return sorted(set(out))[:400]


# ============================================================
# 4) 主流程
# ============================================================
def build_paper(docx: str, code: str, topic: str, title: str,
                 verify: bool = True) -> Dict:
    styled = docx_styled_paragraphs(docx)
    paras = docx_paragraphs(docx)
    parts, tables = split_sections(styled)
    raw_refs = split_refs(paras)

    # ---- 参考文献：结构化 + 核验 ----
    refs = []
    parsed = []
    for r in raw_refs:
        p = parse_ref(r)
        if p:
            parsed.append(p)
    ver_map = {}
    if verify and parsed:
        vres = crossref_verify_batch(parsed, sleep=0.35)
        for v in vres["results"]:
            ver_map[(v.get("input") or {}).get("raw", "")] = v
        ref_summary = vres["summary"]
    else:
        ref_summary = {}

    for i, p in enumerate(parsed, 1):
        v = ver_map.get(p["raw"], {})
        au = v.get("authoritative") or {}
        refs.append({
            "key": "ref_%d" % i,
            "title": p["raw"][:400],
            "parsed": {"title": p["title"], "year": p["year"],
                       "journal": p["journal"], "volume": p["volume"],
                       "pages": p["pages"], "authors": p["authors"],
                       "is_web": p["is_web"], "is_thesis": p["is_thesis"],
                       "is_working": p["is_working"]},
            "doi": au.get("doi") or v.get("suggested_doi") or "",
            "year": au.get("year") or p["year"],
            "source": au.get("journal") or p["journal"],
            "verify": {"status": v.get("status"), "detail": v.get("detail"),
                       "authoritative_authors": au.get("authors"),
                       "authoritative_doi": au.get("doi"),
                       "article_number": au.get("article_number")},
        })

    # ---- 组装正文 ----
    def join(key):
        return "\n\n".join(parts.get(key, [])).strip()

    body_map = {
        "abstract": join("abstract"),
        "intro": join("intro"),
        "lit": join("lit"),
        "data": join("data"),
        "method": join("method"),
        "results": join("results"),
        "robust": join("robust"),
        "conclusion": join("conclusion"),
    }
    # 附录单独存（不进 parts，但要保留）
    appendix = "\n\n".join(parts.get("__appendix__", [])).strip()
    # ⚠️ 初稿没有独立的「数据」章 —— 数据来源在第三章（研究方法）里。
    #    不能把 method 全文复制成 data（那样两个 part 完全重复，白占篇幅）。
    #    做法：从 method 里**只抽数据相关小节**，其余留在 method。
    if not body_map["data"] and body_map["method"]:
        data_heads = ("3.2 数据来源", "3.3 变量定义", "3.3.1", "3.3.2", "3.3.3")
        keep, out = [], []
        grab = False
        for line in body_map["method"].split("\n\n"):
            if line.startswith("## "):
                grab = any(h in line for h in data_heads)
            (keep if grab else out).append(line)
        if keep:
            body_map["data"] = "\n\n".join(keep).strip()
            body_map["method"] = "\n\n".join(out).strip()

    all_text = "\n".join(v for v in body_map.values() if v)
    anchors = extract_number_anchors(all_text)

    d = {
        "code": code, "topic": topic, "title": title,
        "abstract": body_map["abstract"][:4000],
        "abstractAuto": False, "abstractDirty": False,
        "direction": "在初稿基础上推进：补 OOS 样本外检验、扩展数据源、"
                     "按 Crossref 核验结果修正参考文献",
        "directionCandidates": [
            {"text": "补样本外预测检验（Diebold-Mariano / Clark-West），"
                     "回应「统计显著≠有预测力」的质疑",
             "basedOn": "凌晨稿的 OOS 负结果（DM p=0.388）"},
            {"text": "按 Crossref 核验修正 5 处作者张冠李戴 + 替换网页类引用",
             "basedOn": "refs_verify.json"},
            {"text": "扩展数据源：补 STLFSI4 / NFCI / BAMLH0A0HYM2 做金融压力替代指标",
             "basedOn": "fars_data.FRED_MARKET"},
            {"text": "把五个清洁能源指数的日线改为 FRED/公开源可复现口径",
             "basedOn": "初稿声称 Bloomberg 但底层是 EViews 工作簿"},
        ],
        "strength": "较强", "tone": "严谨客观", "style": "详尽展开",
        "parts": {k: {"status": "done" if v else "empty", "text": v,
                      "full": v, "updatedAt": int(time.time() * 1000)}
                  for k, v in body_map.items()},
        "refs": refs,
        "tables": [{"key": "t%d" % i, "caption": t[1][:200], "source": "初稿导入"}
                   for i, t in enumerate(tables, 1)],
        "figures": [],
        "compute": [],
        "pins": [],
        "methodWeights": {},
        "progress": {k: (bool(v)) for k, v in body_map.items()},
        "updatedAt": int(time.time() * 1000),
        "importedFrom": {
            "docx": os.path.basename(docx),
            "docxPath": docx,
            "importedAt": int(time.time() * 1000),
            "paragraphs": len(styled),
            "tableCaptions": len(tables),
            "appendixChars": len(appendix),
            "refCount": len(refs),
            "refVerifySummary": ref_summary,
            "numberAnchors": anchors,
        },
        "appendixText": appendix[:60000],
        "outline": {"items": [], "basis": "docx 导入", "updatedAt": int(time.time() * 1000)},
        "audit": {"ok": True, "findings": [], "summary": "docx 导入，未跑审计",
                  "auditedAt": int(time.time() * 1000)},
    }
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--docx", required=True)
    ap.add_argument("--code", required=True)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--state-dir", default="")
    ap.add_argument("--out", default="")
    ap.add_argument("--no-verify", action="store_true")
    args = ap.parse_args()

    here = os.path.dirname(os.path.abspath(__file__))
    state_dir = args.state_dir or os.path.join(here, "state")
    out_dir = args.out or os.path.join(state_dir, "papers")
    os.makedirs(out_dir, exist_ok=True)

    print("[1/3] 解析 docx + Crossref 核验参考文献…")
    d = build_paper(args.docx, args.code, args.topic, args.title,
                     verify=not args.no_verify)
    imp = d["importedFrom"]
    print("      段落 %d / 表标题 %d / 参考文献 %d" %
          (imp["paragraphs"], imp["tableCaptions"], imp["refCount"]))
    print("      引用核验: %s" % json.dumps(imp["refVerifySummary"], ensure_ascii=False))
    for k, v in d["parts"].items():
        print("      %-10s %6d 字" % (k, len(v["text"])))
    print("      数字锚点 %d 个 / 附录 %d 字" %
          (len(imp["numberAnchors"]), imp["appendixChars"]))

    fp = os.path.join(out_dir, "%s__%s.json" % (args.code, args.topic))
    io.open(fp, "w", encoding="utf-8").write(
        json.dumps(d, ensure_ascii=False, indent=1))
    print("[2/3] 已写入 %s" % fp)
    print("[3/3] 完成。FARS 面板刷新后即可看到该论文。")


if __name__ == "__main__":
    main()
