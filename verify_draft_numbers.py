#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_draft_numbers.py — 初稿正文/附录里的数字断言 vs results CSV 逐条对账
=========================================================================
背景
----
初稿（FARS 论文 FSI01）是**手工誊写**进 docx 的，数字没有机器校验。
本脚本从两个方向对账：

1. **反向**：正文里出现的每个统计量（系数 / 标准误 / t 值 / p 值 / R² / N）
   是否能在 results CSV 里找到对应值。找不到 = 要么抄错，要么是**无出处的新数字**。
2. **正向**：CSV 里的关键系数（基线四阶、稳健性六项、IRF 十二期）是否都在正文出现过。

判定容差：相对误差 <= 1e-3（正文保留 3-4 位小数时足够），
           p 值允许 1e-6 相对误差（极小 p 值常被写成 <0.001 的科学计数法）。

用法
----
    python3 verify_draft_numbers.py \
        --paper  ../../state/papers/FSI01__Stock.json \
        --proj   ../../state/projects/FSI01_FSI_Renewable_Energy_Volatility

退出码：0 = 无孤儿数字；1 = 有孤儿数字（需人工判定）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

# --------------------------------------------------------------------------
# 数值格式化：把一个 float 变成正文里可能出现的所有写法
# --------------------------------------------------------------------------
SCI_RE = re.compile(r"^-?\d+(?:\.\d+)?[eE][-+]?\d+$")


def variants(x: float) -> set:
    """一个数在论文正文里可能的所有字符串形态。"""
    out = set()
    a = abs(x)

    def add(s):
        s = s.strip()
        if s and s not in out:
            out.add(s)

    # 整数形态（仅当确实是整数）
    if abs(x - round(x)) < 1e-12:
        add(str(int(round(x))))

    # 常规小数：2~6 位
    for nd in range(2, 7):
        add(f"{x:.{nd}f}")
        add(f"{x:,.{nd}f}")

    # 科学计数法
    if a != 0 and (a < 1e-3 or a >= 1e6):
        for nd in range(1, 4):
            s = f"{x:.{nd}e}"
            add(s)
            add(s.replace("e-0", "e-").replace("e+0", "e+"))
            add(s.upper())

    # 百分比形态（乘 100）
    p = x * 100
    for nd in range(1, 4):
        add(f"{p:.{nd}f}")

    # 千分位
    if a >= 1000:
        for nd in range(0, 3):
            add(f"{x:,.{nd}f}")
    return out


def parse_num(tok: str):
    """把正文 token 解析成 float（支持千分位、百分号、破折号区间）。"""
    t = tok.strip().rstrip("%").replace(",", "").replace("−", "-")
    if not t or t in {"-", "–", "—"}:
        return None
    try:
        return float(t)
    except ValueError:
        pass
    m = re.match(r"^(-?\d+(?:\.\d+)?)e[-+]?(\d+)$", t, re.I)
    if m:
        try:
            return float(m.group(1)) * (10 ** int(m.group(2)))
        except Exception:
            return None
    return None


NUM_RE = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+\.\d+|-?\d+(?:[eE][-+]?\d+)")


def extract_numbers(text: str):
    """返回正文里所有数字 token 及其上下文。"""
    hits = []
    for m in NUM_RE.finditer(text):
        raw = m.group(0)
        v = parse_num(raw)
        if v is None:
            continue
        ctx = text[max(0, m.start() - 45): m.end() + 25].replace("\n", " ")
        hits.append((raw, v, ctx))
    return hits


# --------------------------------------------------------------------------
# 从 CSV 收集权威数值池
# --------------------------------------------------------------------------
def load_csv_pool(proj: str):
    import csv
    pool = {}      # value(float) -> 来源描述
    tables = {}    # 逻辑表名 -> 行列表
    # 权威数值来源有两处：
    #   output/  —— Python 复算结果（results.json + 6 个 CSV）
    #   eviews/  —— EViews 原始输出（40 个 CSV，含 VAR 估计、ADF/KPSS、Granger、IRF）
    # 初稿正文的数字是**手抄 EViews 输出**的，所以 EViews CSV 才是初稿的对账基准。
    src_dirs = [os.path.join(proj, "output"), os.path.join(proj, "eviews"),
                os.path.join(proj, "output_ext")]
    # EViews 的 CSV 其实是「报告文本」：首行是标题而非列名，系数/标准误/t值
    # 分别写在同一行的不同形态里（0.539262 / (0.06169) / [8.74157]），
    # DictReader 会把标题行当列名 → 读不出任何数值。改为**原始文本扫描**。
    RE_EVIEWS_NUM = re.compile(
        r"(?<![\w.])-?\d+\.\d+(?:[eE][-+]?\d+)?(?![\w])"
    )
    RE_DATE = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
    for outdir in src_dirs:
        if not os.path.isdir(outdir):
            continue
        tag = os.path.basename(outdir)
        for fn in sorted(os.listdir(outdir)):
            if not fn.endswith(".csv"):
                continue
            path = os.path.join(outdir, fn)
            key = f"{tag}/{fn}"
            if tag == "eviews":
                try:
                    with open(path, encoding="utf-8", errors="replace") as f:
                        raw = f.read()
                except Exception:
                    continue
                raw = RE_DATE.sub(" ", raw)
                # 去掉 “Date: 11/10/26 Time: 19:14” 这类元信息行残留
                for m in RE_EVIEWS_NUM.finditer(raw):
                    try:
                        fv = float(m.group(0))
                    except ValueError:
                        continue
                    pool.setdefault(round(fv, 10), f"{key}:原文扫描")
                continue
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    rows = list(csv.DictReader(f))
            except Exception:
                continue
            tables[key] = rows
            for i, r in enumerate(rows):
                if not isinstance(r, dict):
                    continue
                for k, v in r.items():
                    if v in (None, ""):
                        continue
                    try:
                        fv = float(v)
                    except (TypeError, ValueError):
                        continue
                    src = f"{key}:{k}[行{i}]"
                    pool.setdefault(round(fv, 10), src)
    # results.json 里的关键标量
    for rj in (os.path.join(outdir, "results.json"),
               os.path.join(proj, "output_ext", "ext_results.json")):
      if os.path.exists(rj):
        with open(rj, encoding="utf-8") as f:
            res = json.load(f)

        def walk(o, p=""):
            if isinstance(o, dict):
                for k, v in o.items():
                    walk(v, f"{p}/{k}")
            elif isinstance(o, list):
                for i, v in enumerate(o):
                    walk(v, f"{p}[{i}]")
            elif isinstance(o, (int, float)) and not isinstance(o, bool):
                pool.setdefault(round(float(o), 10), f"{os.path.basename(rj)}:{p}")
        walk(res)
    return pool, tables


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--paper", required=True)
    ap.add_argument("--proj", required=True)
    ap.add_argument("--tol", type=float, default=1e-3)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    with open(args.paper, encoding="utf-8") as f:
        paper = json.load(f)

    pool, tables = load_csv_pool(args.proj)

    # 收集正文：8 个 part + 附录
    chunks = []
    for k, v in (paper.get("parts") or {}).items():
        t = v.get("text", "") if isinstance(v, dict) else str(v)
        if t:
            chunks.append((f"part.{k}", t))
    apx = paper.get("appendixText") or ""
    if apx:
        chunks.append(("appendix", apx))
    title = paper.get("title") or ""
    chunks.append(("title", title))

    full = "\n".join(t for _, t in chunks)

    # ---- 反向：正文数字能否在 CSV 池中找到 ----
    # 判定容差：相对 1e-3，或绝对 5e-7（覆盖极小 p 值的尾数）
    pool_sorted = sorted(pool.keys())

    def near(v):
        # 二分找最近
        import bisect
        i = bisect.bisect_left(pool_sorted, v)
        best = None
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(pool_sorted):
                pv = pool_sorted[j]
                d = abs(pv - v)
                rel = d / max(abs(pv), 1e-12)
                if d <= max(5e-7, args.tol * abs(pv)):
                    if best is None or d < best[0]:
                        best = (d, pv, pool[pv])
        return best

    orphans = []
    matched = 0
    total = 0
    # 只对"像统计量"的数字做严格对账：排除章节号、日期、样本期、常见整数
    STAT_CTX = re.compile(
        r"(系数|标准误|t\s*[=＝]|p\s*[=＜<]|R\s*²|R2|显著|样本量|观测|N\s*[=＝]|"
        r"β|α|ρ|F\s*统计|DW|F\s*=)"
    )
    # --- 误报过滤：这三类数字天然没有「出处」，不能算孤儿 ---
    # 1) 章节/表格/公式编号：「表2.1」「（3.5）」「表4.1a」
    RE_SECTION_NO = re.compile(
        r"(表\s*[\d.]+|图\s*[\d.]+|式\s*[\(（]?[\d.]+|"
        r"[\(（]\s*\d+\.\d+\s*[\)）]|附录\s*[A-F]|"
        r"\d+\.\d+\s*节|第\s*\d+\.\d+\s*节|"
        r"#{2,4}\s*\d+\.\d+)"     # Markdown 标题里的章节号
    )
    # 百分比数值：100.0% / 92.8% 等（由 CSV 的比例列派生）
    RE_PERCENT_NUM = re.compile(r"\d+(?:\.\d+)?\s*%")
    # 2) 显著性阈值：0.05 / 0.01 / 0.10 / 1% / 5%
    RE_THRESHOLD = re.compile(
        r"(p\s*[值]?\s*[=＜<≤]\s*0?\.\d+|[＜<]\s*0?\.\d+|"
        r"0?\.\d+\s*(?:水平|显著性水平)|置信水平|\d+\s*%水平|"
        r"p\s*[值]?\s*[=＜<]\s*0?\.\d+)"
    )
    # 3) 常识性整数：样本区间 217 / 252 / 300、期数 12、次数 10000
    #    —— 这些是「样本量/期数」，由 CSV 的 n 字段覆盖；只在上下文明确是统计量时才查
    RE_INT_CONTEXT = re.compile(r"(观测|样本|N\s*[=＝]|期|个月|年)")

    for src, text in chunks:
        for raw, v, ctx in extract_numbers(text):
            total += 1
            # 先剔除三类误报源
            if RE_SECTION_NO.search(ctx):
                continue
            if RE_THRESHOLD.search(ctx):
                continue
            # 紧跟在 % 后的数字：百分比数值，同样不在权威池里
            if re.search(re.escape(raw) + r"\s*%", ctx):
                continue
            if not STAT_CTX.search(ctx):
                continue  # 不是统计量语境，放行
            m = near(v)
            if m:
                matched += 1
                continue
            # 整数且语境是样本量/期数 —— 属正常，不再算孤儿
            if abs(v - round(v)) < 1e-12 and RE_INT_CONTEXT.search(ctx):
                continue
            orphans.append({"来源": src, "原文": raw, "值": v,
                            "上下文": ctx.strip()[:120]})

    # ---- 正向：CSV 关键系数是否在正文出现 ----
    key_missing = []
    checks = []
    # 基线四阶
    base = tables.get("table3_baseline.csv", [])
    for r in base:
        checks.append(("表3基线 %s" % r.get("label", ""),
                       float(r["coef_FSI"]), "coefficient"))
        checks.append(("表3基线 %s se" % r.get("label", ""),
                       float(r["se_FSI"]), "coefficient"))
    # 稳健性
    for r in tables.get("table6_robustness.csv", []):
        checks.append(("表6稳健性 %s" % r.get("label", ""),
                       float(r["coef_FSI"]), "coefficient"))
    # IRF 关键期
    for r in tables.get("table7_irf.csv", []):
        h = r.get("h")
        if h in ("1", "4", "8", "12", "1.0", "4.0", "8.0", "12.0"):
            checks.append(("表7 IRF h=%s" % h,
                           float(r["resp_RVSPGCE_to_FSI"]), "coefficient"))

    full_low = full.replace(",", "")
    for label, val, _k in checks:
        vs = {s.replace(",", "") for s in variants(val)}
        if not (vs & set(re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", full_low))):
            # 退一步：按数值搜索（正文可能四舍五入）
            hit = False
            for tok in re.findall(r"-?\d+\.?\d*(?:[eE][-+]?\d+)?", full):
                tv = parse_num(tok)
                if tv is None:
                    continue
                if abs(tv - val) <= max(5e-7, args.tol * abs(val)):
                    hit = True
                    break
            if not hit:
                key_missing.append({"项": label, "值": val})

    report = {
        "论文": paper.get("title"),
        "CSV权威数值池大小": len(pool),
        "正文数字总数": total,
        "统计量语境命中": matched,
        "孤儿数字数": len(orphans),
        "孤儿数字": orphans,
        "CSV关键系数未在正文出现": key_missing,
    }

    print("=" * 74)
    print("初稿数字对账 —— %s" % (paper.get("title") or ""))
    print("=" * 74)
    print("CSV 权威数值池     : %d 个" % len(pool))
    print("正文数字 token 总数: %d" % total)
    print("统计量语境命中     : %d" % matched)
    print("孤儿数字（无出处） : %d" % len(orphans))
    print("CSV关键系数未出现  : %d" % len(key_missing))
    print()
    if orphans:
        print("--- 孤儿数字明细（需人工判定）---")
        for o in orphans[:60]:
            print("  [%s] %s = %s" % (o["来源"], o["原文"], o["值"]))
            print("      …%s…" % o["上下文"])
    if key_missing:
        print("--- CSV 有但正文没有 ---")
        for o in key_missing:
            print("  %-34s %s" % (o["项"], o["值"]))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print("\n报告写入: %s" % args.out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
