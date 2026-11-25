#!/usr/bin/env python3
"""文件导入 / 导出 —— 左侧「文件列表」的底层能力。

为什么单独一个模块：
  server.py 已经 4200 行，导入解析与导出生成逻辑单独放，改起来不互相踩。

约定：
  - 上传走 base64 JSON（后端是原生 http.server，没有 multipart 解析器）。
  - 导入的文档按章节切成 paper.parts，归位逻辑复用 server.py 的
    _assign_parts_from_paragraphs（同一套章节识别规则，不另造一套）。
  - 导出：docx / md / txt / json。
    导入的文件保留原件 → 导出优先回吐原件（保证往返保真），
    追踪表里的论文则从 paper 实体重新生成 docx。
"""

import io
import os
import re

# ---------------------------------------------------------------- 中文小节编号
# "一、引言" / "第一节 引言" / "（一）引言" → "1. 引言"
# 这样既有的 _is_section_head 就能认出来（它只认阿拉伯数字编号）。
_CN_NUM = {
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
    "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
    "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20,
}


def _cn2int(s):
    s = (s or "").strip()
    if s in _CN_NUM:
        return _CN_NUM[s]
    if len(s) == 2:
        a, b = s[0], s[1]
        if b == "十":
            return _CN_NUM.get(a, 0) * 10
        if a == "十":
            return 10 + _CN_NUM.get(b, 0)
        return _CN_NUM.get(a, 0) * 10 + _CN_NUM.get(b, 0)
    return 0


def normalize_headings(paras):
    """把中文小节编号规整成「N. 标题」，让既有的章节识别能认。"""
    out = []
    for p in paras:
        t = (p or "").strip()
        if not t:
            continue
        t = re.sub(r"^第([一二三四五六七八九十]+)\s*[节、.．]\s*",
                   lambda m: "%d. " % _cn2int(m.group(1)), t)
        t = re.sub(r"^([一二三四五六七八九十]+)\s*[、.．]\s*(?=\S)",
                   lambda m: "%d. " % _cn2int(m.group(1)), t)
        t = re.sub(r"^（([一二三四五六七八九十]+)）\s*(?=\S)",
                   lambda m: "%d. " % _cn2int(m.group(1)), t)
        out.append(t)
    return out


# ---------------------------------------------------------------- 解析
def pdf_paras(data):
    """PDF → 段落文本行。先试 PyMuPDF（版面更准），再退回 pypdf。"""
    paras = []
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(stream=data, filetype="pdf")
        for pg in doc:
            for line in (pg.get_text() or "").splitlines():
                t = line.strip()
                if t:
                    paras.append(t)
        doc.close()
        if paras:
            return paras
    except Exception:
        pass
    try:
        from pypdf import PdfReader
        rd = PdfReader(io.BytesIO(data))
        for pg in rd.pages:
            for line in (pg.extract_text() or "").splitlines():
                t = line.strip()
                if t:
                    paras.append(t)
        if paras:
            return paras
    except Exception as e:
        raise ValueError("PDF 解析失败：%s: %s" % (type(e).__name__, e))
    if not paras:
        raise ValueError("PDF 里没抽到文字（可能是扫描件，需要 OCR）")
    return paras


def parse_upload(filename, data):
    """上传的字节 → 段落列表（章节已归一化）。

    返回 {"paras": [...], "ext": ".docx", "coreTitle": "..."}
    """
    filename = filename or ""
    ext = os.path.splitext(filename)[1].lower()
    paras = []
    core_title = ""

    if ext == ".docx":
        try:
            from docx import Document
        except Exception as e:
            raise ValueError("服务端缺少 python-docx：%s" % e)
        try:
            doc = Document(io.BytesIO(data))
        except Exception as e:
            raise ValueError("docx 解析失败（文件可能损坏）：%s" % e)
        try:
            core_title = (doc.core_properties.title or "").strip()
        except Exception:
            core_title = ""
        for p in doc.paragraphs:
            t = (p.text or "").strip()
            if t:
                paras.append(t)
        # 表格保留成文本行（真实数据不丢）
        for i, tb in enumerate(doc.tables):
            rows = []
            for r in tb.rows:
                cells = [(c.text or "").strip() for c in r.cells]
                rows.append(" | ".join(cells))
            if rows:
                paras.append("【表格 %d】" % (i + 1))
                paras.extend(rows)
    elif ext == ".pdf":
        paras = pdf_paras(data)
    else:
        try:
            txt = data.decode("utf-8")
        except UnicodeDecodeError:
            txt = data.decode("utf-8", "replace")
        paras = [l.strip() for l in txt.splitlines() if l.strip()]

    paras = normalize_headings(paras)
    return {"paras": paras, "ext": ext, "coreTitle": core_title}


def infer_topic(*texts):
    """从文件名/正文推断主题（决定规格与因子池）。"""
    blob = " ".join(t or "" for t in texts).lower()
    if "esg" in blob or "环境" in blob or "可持续" in blob or "碳排放" in blob:
        return "ESG"
    if "machine" in blob or "机器学习" in blob or "机器" in blob or "ml" in blob \
            or "神经网络" in blob or "随机森林" in blob:
        return "Machine"
    if "stock" in blob or "股票" in blob or "股价" in blob or "动量" in blob or "收益" in blob:
        return "Stock"
    return "Stock"


# ---------------------------------------------------------------- 生成
def build_docx(title, part_items, refs=None, tables=None):
    """从 paper 实体生成 docx 字节。

    part_items: [(章节名, 正文), ...]
    """
    from docx import Document
    doc = Document()
    doc.add_heading(title or "未命名", level=0)
    for label, text in part_items:
        if not (text or "").strip():
            continue
        doc.add_heading(label, level=1)
        for line in (text or "").splitlines():
            if line.strip():
                doc.add_paragraph(line.strip())
    if refs:
        doc.add_heading("References", level=1)
        for r in refs:
            doc.add_paragraph(str(r))
    if tables:
        doc.add_heading("Tables", level=1)
        for t in tables:
            doc.add_paragraph("【%s】" % (t.get("title") or ""))
            cols = t.get("columns") or []
            for row in (t.get("rows") or []):
                if isinstance(row, dict):
                    vals = ([str(row.get(c, "")) for c in cols] if cols
                            else [str(v) for v in row.values()])
                else:
                    vals = [str(v) for v in row]
                doc.add_paragraph(" | ".join(vals))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def build_markdown(title, part_items, refs=None, tables=None):
    out = ["# %s" % (title or "未命名"), ""]
    for label, text in part_items:
        if not (text or "").strip():
            continue
        out += ["## %s" % label, "", (text or "").strip(), ""]
    if refs:
        out += ["## References", ""]
        for r in refs:
            out.append("- %s" % r)
        out.append("")
    if tables:
        out += ["## Tables", ""]
        for t in tables:
            out += ["### %s" % (t.get("title") or ""), ""]
            cols = t.get("columns") or []
            for row in (t.get("rows") or []):
                if isinstance(row, dict):
                    vals = ([str(row.get(c, "")) for c in cols] if cols
                            else [str(v) for v in row.values()])
                else:
                    vals = [str(v) for v in row]
                out.append("| " + " | ".join(vals) + " |")
            out.append("")
    return "\n".join(out).encode("utf-8")


def build_txt(title, part_items, refs=None, tables=None):
    out = [title or "未命名", ""]
    for label, text in part_items:
        if not (text or "").strip():
            continue
        out += [label, "-" * len(label), (text or "").strip(), ""]
    if refs:
        out += ["References", "-" * 10]
        for r in refs:
            out.append(str(r))
        out.append("")
    return "\n".join(out).encode("utf-8")
