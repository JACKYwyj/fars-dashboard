#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_section_numbering.py — 修复初稿导入的章节标题丢失 + 增补章节编号冲突
=====================================================================
发现的两个问题（2026-11-26 实测）
--------------------------------
**问题A：初稿的「4.7 结果讨论」标题在导入时丢了**
  初稿 docx 段落 967 是标题「4.7 结果讨论」，其下有 3 段实质内容
  （"综合本章4.1至4.6节的实证发现…" / "首先，在动态传导链条的内部一致性上…" /
  "其次，在与能源金融既有文献的对话上…" / "最后，必须清醒认识实证证据的适用边界…"）。
  导入器 `split_sections()` 的标题判定依赖字号+加粗，
  而该标题的样式未命中判定条件 → 标题被丢弃，但正文内容被保留。
  结果：FARS 里「结果讨论」的内容存在却没有标题，读者会以为它属于 4.6。

**问题B：增补章节撞号**
  初稿目录里已有「4.7 结果讨论」，我把增补分析也编成 4.7 → 编号冲突。

处理
----
A. 在「综合本章4.1至4.6节的实证发现…」之前补回 `## 4.7 结果讨论`
B. 增补分析整体改号为 4.8（标题与小节号一并改）

用法
----
    python3 fix_section_numbering.py --paper ../../../papers/FSI01__Stock.json
"""
from __future__ import annotations

import argparse
import json
import os

# 初稿「4.7 结果讨论」正文的第一句（用它定位，比标题更稳）
DISCUSSION_ANCHOR = "综合本章4.1至4.6节的实证发现"
DRAFT_HEADING = "## 4.7 结果讨论"

# 增补章节的旧编号 → 新编号
REMAP = [
    ("## 4.7 增补分析（本研究在初稿基础上的扩展）",
     "## 4.8 增补分析（本研究在初稿基础上的扩展）"),
    ("### 4.7.1 ", "### 4.8.1 "),
    ("### 4.7.2 ", "### 4.8.2 "),
    ("### 4.7.3 ", "### 4.8.3 "),
    ("### 4.7.4 ", "### 4.8.4 "),
    ("### 4.7.5 ", "### 4.8.5 "),
    ("### 4.7.6 ", "### 4.8.6 "),
    ("### 4.7.7 ", "### 4.8.7 "),
    ("### 4.7.8 ", "### 4.8.8 "),
]


def fix(d):
    changed = {"restoredHeading": False, "renumbered": 0}

    # ---------- A. 补回初稿的 4.7 标题 ----------
    for key, part in (d.get("parts") or {}).items():
        text = part.get("text", "")
        if DISCUSSION_ANCHOR not in text:
            continue
        # 已有标题就不动
        if DRAFT_HEADING in text:
            break
        i = text.find(DISCUSSION_ANCHOR)
        # 往前回退到段落边界（双换行）
        j = text.rfind("\n\n", 0, i)
        j = 0 if j < 0 else j + 2
        text = text[:j] + DRAFT_HEADING + "\n\n" + text[j:]
        part["text"] = text
        changed["restoredHeading"] = True
        changed["restoredIn"] = key
        break

    # ---------- B. 增补章节改号 ----------
    for key, part in (d.get("parts") or {}).items():
        text = part.get("text", "")
        orig = text
        for old, new in REMAP:
            if old in text:
                text = text.replace(old, new)
        if text != orig:
            part["text"] = text
            changed["renumbered"] = sum(1 for old, _ in REMAP if old in orig)

    return changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", required=True)
    args = ap.parse_args()

    with open(args.paper, encoding="utf-8") as f:
        d = json.load(f)

    ch = fix(d)

    with open(args.paper, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)

    print("=" * 70)
    print("章节编号修复完成")
    print("=" * 70)
    print("  A. 补回初稿「4.7 结果讨论」标题 : %s%s" % (
        "✅" if ch["restoredHeading"] else "— 未找到锚点（可能已存在）",
        ("（在 part.%s）" % ch.get("restoredIn")) if ch.get("restoredIn") else ""))
    print("  B. 增补章节改号 4.7 → 4.8      : %s" % (
        "✅" if ch["renumbered"] else "— 未发现旧编号"))

    # 复核
    print()
    print("  复核 —— 各 part 的章节标题：")
    import re
    for k, v in (d.get("parts") or {}).items():
        t = v.get("text", "")
        heads = [m.group(1) for m in re.finditer(r"^#{2,3}\s*(4\.[78].*)$", t, re.M)]
        if heads:
            print("    part.%-10s %s" % (k, " | ".join(heads)))


if __name__ == "__main__":
    main()
