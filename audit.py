#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FARS 论文正文完整性审计（AI Integrity Audit）

依据 FARS 原论文（arXiv:2606.31651v2）：

  §3.5 Writing Stage
      - Analysis Agent 先做 paper blueprint：把每个 central claim 绑到
        supporting experiments / source artifacts / candidate figures / literature；
      - Writing Agent 起草后做 refinement：citation 检查、图表检查、
        **numerical verification against source artifacts**、
        以及 paper-level review for unsupported or overstated claims。
      → 本模块就是这一层在「人工修改」侧的落地：正文改完，逐条数字与
        论断回源产物对账。

  §5.1 Review Protocol
      AI Integrity Audit 的 8 类失败模式：
        hallucinated citations / fabricated or unverifiable results /
        spurious novelty / hallucinated methods or baselines /
        mathematical and logical errors / internal inconsistencies /
        experimental-design pathologies / language pathologies。

  §5.3 Table 4（升级率）
        hallucinated methods/baselines 20/22、hallucinated citations 13/16、
        fabricated results 16/21 几乎总是被判为正式违规；
        design pathologies 22/64、language pathologies 4/31 多记为弱点。
      → 严重度分级照此：可被产物直接证伪的 → violation，其余 → weakness。

原则：全部为**确定性核验**（数字比对、文献比对、方法比对、跨节比对），
不调用模型；每条结论都必须指回真实产物的具体位置（表格 id / 章节 / 文献 key）。
无法回溯的数字一律记为「未追溯」，不做「大概是对的」的放行。
"""

import re
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

# --------------------------------------------------------------------------
# 论文 §5.1 的 8 类完整性失败模式
# --------------------------------------------------------------------------
MODES = [
    ("citation",    "幻觉引用",      "hallucinated citations"),
    ("result",      "编造/不可核验结果", "fabricated or unverifiable results"),
    ("novelty",     "虚假新颖性",    "spurious novelty / contamination"),
    ("method",      "幻觉方法/基线", "hallucinated methods or baselines"),
    ("math",        "数学与逻辑错误", "mathematical and logical errors"),
    ("consistency", "内部不一致",    "internal inconsistencies"),
    ("design",      "实验设计缺陷",  "experimental-design pathologies"),
    ("language",    "语言病理",      "language pathologies"),
]
MODE_LABEL = {k: v for k, v, _ in MODES}
MODE_EN = {k: e for k, _, e in MODES}

# 论文 Table 4：这几类一旦被产物证实，几乎必然升级为正式违规
SEVERE_MODES = {"citation", "result", "method"}

PART_LABELS = [
    ("abstract", "摘要"), ("intro", "引言"), ("lit", "文献综述"),
    ("data", "数据"), ("method", "方法"), ("results", "实证结果"),
    ("robust", "稳健性"), ("conclusion", "结论"),
]
PART_LABEL = dict(PART_LABELS)


# --------------------------------------------------------------------------
# 数字工具
# --------------------------------------------------------------------------
NUM = r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?"


def _dec(s):
    if s is None:
        return None
    s = str(s).strip().replace(",", "")
    if not s or s.lower() in ("na", "nan", "none", "-"):
        return None
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _dec_places(s):
    """字符串里小数位数（不含科学计数法尾数）。"""
    s = str(s).strip().lstrip("+-")
    if "e" in s.lower():
        s = s.lower().split("e")[0]
    if "." not in s:
        return 0
    return len(s.split(".")[1])


def _match_num(prose, prose_raw, art):
    """正文数字是否与产物数字一致（允许正文精度更低）。

    语义：正文写 -0.0327，产物是 -0.0327499 —— 这是**四舍五入到 4 位小数**的
    一致表述，不是编造。只有当差值超过半个末位时才判为不一致。
    """
    if prose is None or art is None:
        return False
    if prose == art:
        return True
    dp = _dec_places(prose_raw)
    tol = Decimal(1).scaleb(-dp) / 2 if dp else Decimal("0.5")
    try:
        return abs(prose - art) <= tol
    except (InvalidOperation, ValueError):
        return False


# --------------------------------------------------------------------------
# 产物索引：把 tables / compute 摊平成「变量 → 数值 → 出处」
# --------------------------------------------------------------------------
KIND_COLS = [
    ("coef", "系数"), ("se", "聚类标准误"), ("t", "t值"), ("p", "p值"),
    ("ci", "95%CI下限"), ("ci", "95%CI上限"), ("n", "观测数"),
    ("r2", "R2"), ("r2", "调整R2"), ("ncl", "聚类个体数"),
]


def build_index(d):
    """返回 (by_var, by_kind, methods, model_titles)。

    每个条目形如 (Decimal值, 出处说明, 所属模型标题)。
    模型标题很关键：同一变量在「基准」「替代规格」「高波动子样本」下
    系数本就不同 —— 那是不同模型，不是内部不一致。
    """
    by_var = {}     # var -> {kind: [(value, prov, model)]}
    by_kind = {}    # kind -> [(value, prov, model)]
    model_titles = []
    for t in (d.get("tables") or []):
        tid = t.get("id") or ""
        title = t.get("title") or ""
        model_titles.append(title)
        for r in (t.get("rows") or []):
            if not isinstance(r, dict):
                continue
            var = (r.get("变量") or "").strip()
            for kind, col in KIND_COLS:
                if col not in r:
                    continue
                raw = r.get(col)
                if raw in (None, ""):
                    continue
                v = _dec(raw)
                if v is None:
                    continue
                prov = "%s · %s" % (tid, title)
                if var:
                    by_var.setdefault(var, {}).setdefault(kind, []).append((v, prov, title))
                by_kind.setdefault(kind, []).append((v, prov, title))
    methods = set()
    for c in (d.get("compute") or []):
        m = (c.get("method") or "").strip()
        if m:
            methods.add(m)
        res = c.get("result") or ""
        for mm in re.finditer(r"N\s*=\s*(\d+)", res):
            v = _dec(mm.group(1))
            if v is not None:
                by_kind.setdefault("n", []).append(
                    (v, "%s · %s" % (c.get("id"), c.get("title")), c.get("title") or ""))
        for mm in re.finditer(r"R\s*[²2]\s*=\s*(" + NUM + ")", res):
            v = _dec(mm.group(1))
            if v is not None:
                by_kind.setdefault("r2", []).append(
                    (v, "%s · %s" % (c.get("id"), c.get("title")), c.get("title") or ""))
    return by_var, by_kind, methods, model_titles


# --------------------------------------------------------------------------
# 正文声明抽取
# --------------------------------------------------------------------------
LABEL_PATTERNS = [
    ("coef", r"(?:系数|beta|β)\s*(?:为|是|=|＝|:|：)?\s*(" + NUM + ")"),
    ("se",   r"(?:聚类标准误|标准误|se)\s*(?:为|是|=|＝|:|：)?\s*(" + NUM + ")"),
    # t / p 必须带显式分隔符，否则 "t-12"、"t+3"、"p.E-5" 会被误当统计量
    ("t",    r"(?:^|[\s,，、（(\[])(?:t\s*(?:值|统计量)?\s*(?:=|＝|:|：)\s*|t\s*=\s*)(" + NUM + r")(?![0-9eE])"),
    ("p",    r"(?:^|[\s,，、（(\[])(?:p\s*(?:值)?\s*(?:=|＝|:|：)\s*|p\s*=\s*)(" + NUM + r")(?![0-9eE])"),
    ("r2",   r"R\s*[²2]\s*(?:=|＝|:|：|为|是)?\s*(" + NUM + ")"),
    ("n",    r"(?:观测数|样本量|有效观测)\s*(?:=|＝|:|：|为|是)?\s*(" + NUM + ")"),
    ("n",    r"(" + NUM + r")\s*个(?:观测|个股-月观测|有效观测)"),
    ("ncl",  r"(?:聚类个体数|个股数)\s*(?:=|＝|:|：|为|是)?\s*(" + NUM + ")"),
]

SIGN_RE = re.compile(r"(负向|正向|负相关|正相关|显著为负|显著为正|为负|为正)")
SIG_RE = re.compile(r"(不显著|显著)")
NEG_CUE = re.compile(r"(未|没有|并未|尚未|不采用|未使用|无法|难以|不能|局限|不涉及|无需)")


def _sentences(text):
    parts = re.split(r"(?<=[。；;！!？?])|\n+", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def _vars_in(sentence, var_universe):
    hits = []
    for v in var_universe:
        if not v:
            continue
        if v in sentence:
            hits.append(v)
    # 长变量名优先（避免 "ESG评分" 被 "ESG" 抢走）
    hits.sort(key=len, reverse=True)
    return hits


def _claims_in(sentence, var_universe):
    """抽取 (kind, raw, value, matched_var) 列表。"""
    out = []
    for kind, pat in LABEL_PATTERNS:
        for m in re.finditer(pat, sentence):
            raw = m.group(1)
            v = _dec(raw)
            if v is None:
                continue
            # 变量上下文：取该匹配点之前最近的变量名
            pre = sentence[:m.start()]
            vs = _vars_in(pre, var_universe)
            var = vs[0] if vs else None
            if var is None:
                vs2 = _vars_in(sentence[m.end():], var_universe)
                var = vs2[0] if vs2 else None
            out.append((kind, raw, v, var))
    return out


# --------------------------------------------------------------------------
# 引用核验
# --------------------------------------------------------------------------
# 引用锚点：只匹配「(年份)」/「（年份）」本身，**不**在正则里嵌套作者串。
# 之前的写法把作者串的多个分支包在嵌套量词里 → 灾难性回溯（实测直接挂住）。
# 现在改为：先线性地找年份锚点，再**反向**取锚点前的一段文本作为作者候选。
CITE_ANCHOR = re.compile(r"[\(（]\s*((?:19|20)\d{2}[a-z]?)\s*[\)）]")

# 姓氏 token：必须支持变音符（Kölbel / Müller / Flammer / Jégou …），
# 否则欧洲作者名会被拆坏，导致「参考文献里明明有」被判成幻觉引用。
_NAME_TOK = re.compile(r"[^\W\d_][\w'’\-]*", re.UNICODE)

# 作者段边界：句号、分号、冒号、逗号后接大写开头等
_AUTHOR_CUT = re.compile(r"[。；;：:\n]|(?<=[a-z])[,，]\s+(?=[A-Z])")


def _author_chunk(sent, start):
    """取句子中 [start, 年份锚点) 之前的作者候选段。

    从锚点往左找最近的边界字符（句读/换行），最多回溯 120 字符。

    ⚠️ 2026-11-24 修：**不能跨过前一个引用锚点，也不能跨过英文句号**。
    实测正文：
        "Fama and French (1992) is the classic starting point. Carhart (1997) adds momentum."
    原来 _AUTHOR_CUT 只切中文句读/逗号，**不切英文句号** →
    Carhart 锚点往左一路回溯到句首，作者段变成
        "Fama and French (1992) is the classic starting point. Carhart"
    → surnames = [Fama, French, Classic, Starting, Point, Carhart]
    → 补齐时要求候选文献同时含 Fama+French+Carhart → **永远匹配不上**；
       audit 侧也会把 Carhart 的引用错误归属给 Fama/French。

    修法两步：
      ① 先把上一个引用锚点 "(YYYY)" 之前的部分**整段丢掉**（作者段不会跨引用）
      ② 英文句号也作为边界 —— 但要求句号前有 ≥3 个小写字母，
         这样 "et al." / "J." 这类缩写不会被误切。
    """
    left = sent[:start]
    if len(left) > 120:
        left = left[-120:]

    # ① 丢掉前一个引用锚点之前的部分
    prev = list(re.finditer(r"[（(]\s*(?:19|20)\d{2}[a-z]?\s*[)）]", left))
    if prev:
        left = left[prev[-1].end():]

    # ② 切边界（英文句末 + 原有句读/逗号）
    parts = _AUTHOR_CUT.split(left)
    chunk = parts[-1] if parts else left
    # ③ 兜底：若仍夹着句号后的小写串（如 "starting point."），切到最后一个句号
    if "." in chunk:
        tail = chunk.rsplit(".", 1)
        # 只有当句号后那段**不含大写词**时才切（避免误伤 "J. F. Fama"）
        if len(tail) == 2 and not re.search(r"[A-Z]", tail[1]):
            chunk = tail[0]
    return chunk


# 中文连接词：正文里写「Fama与French(1992)」，但文献表写「Fama, E. F. & French」。
# _NAME_TOK 的 \w 在 Unicode 下**包含汉字** → 「Fama与French」会被当成一个 token，
# 匹配不上文献表里的 Fama / French → 明明有的文献被判成「幻觉引用」（实测误报 2 项）。
# 修法：抽 token 前先按中文连接词切开。
_CJK_CONNECT = re.compile(r"[与和及或、，,；;：:]")


def _name_tokens(chunk):
    """作者串里的全部候选姓氏 token。"""
    out = []
    for seg in _CJK_CONNECT.split(chunk or ""):
        for m in _NAME_TOK.finditer(seg):
            t = m.group(0)
            if len(t) >= 2 and t[:1].isupper():
                out.append(t)
    return out


def _ref_index(refs):
    """ref key -> {surnames: set, years: set, title: str}。

    姓氏集直接从 title 全文里抽（含变音符），不靠解析「姓, 名, & 姓」结构 ——
    那种解析在缩写体（Berg, F., Kölbel, J. F. & Rigobon, R.）下极易出错。
    """
    idx = {}
    for r in refs or []:
        title = (r.get("title") or "").strip()
        if not title:
            continue
        surnames = set()
        # 作者段 = 标题里第一个括号/年份之前的部分
        head = re.split(r"[（(]\s*(?:19|20)\d{2}", title)[0][:160]
        for m in _NAME_TOK.finditer(head):
            t = m.group(0)
            if len(t) >= 2 and t[:1].isupper():
                surnames.add(t)
        years = set(re.findall(r"\b(?:19|20)\d{2}\b", title))
        idx[r.get("key") or title[:40]] = {
            "surnames": surnames, "years": years, "title": title, "head": head,
        }
    return idx


def _match_ref(ref, surnames, year):
    """正文里的（姓氏, 年份）能否落到某条参考文献上。

    判定：title 全文包含该姓氏 token，且（若两侧都有年份）年份一致。
    """
    title = ref["title"]
    hit_surname = None
    for s in surnames:
        if re.search(r"(?<![^\W\d_])" + re.escape(s) + r"(?![^\W\d_])", title, re.UNICODE):
            hit_surname = s
            break
    if hit_surname is None:
        return None
    if year and ref["years"]:
        if year[:4] not in {y[:4] for y in ref["years"]}:
            return None
    return hit_surname


# --------------------------------------------------------------------------
# 方法核验
# --------------------------------------------------------------------------
CONCRETE_METHODS = [
    "随机森林", "Random Forest", "random forest", "XGBoost", "LightGBM",
    "梯度提升", "GBDT", "决策树", "神经网络", "卷积神经网络", "CNN",
    "LSTM", "GRU", "Transformer", "深度学习模型", "支持向量机", "SVM",
    "K 近邻", "KNN", "贝叶斯优化", "神经网络模型",
]
BASELINE_WORDS = ("基线", "baseline", "对照模型")


# --------------------------------------------------------------------------
# 主审计
# --------------------------------------------------------------------------
def audit_paper(d, spec=None):
    spec = spec or {}
    findings = []
    fid = [0]

    def add(mode, severity, part, snippet, detail, suggestion, evidence=""):
        fid[0] += 1
        findings.append({
            "id": "f%d" % fid[0],
            "mode": mode,
            "modeLabel": MODE_LABEL.get(mode, mode),
            "modeEn": MODE_EN.get(mode, mode),
            "severity": severity,
            "part": part or "",
            "partLabel": PART_LABEL.get(part or "", "全文"),
            "source": texts_src.get(part or "", "body"),
            "snippet": (snippet or "")[:220],
            "detail": detail,
            "suggestion": suggestion,
            "evidence": evidence,
        })

    by_var, by_kind, methods, model_titles = build_index(d)
    var_universe = set()
    for v in by_var:
        if v:
            var_universe.add(v)
    var_universe.add("ESG评分")

    # 收集各节文本
    parts = d.get("parts") or {}
    texts = {}
    texts_src = {}      # part -> 'body'（中栏正文） | 'abstract'（右栏摘要框）
    for k, _ in PART_LABELS:
        p = parts.get(k) or {}
        t = (p.get("text") or "").strip()
        if t:
            texts[k] = t
            texts_src[k] = "body"
    abstract = (d.get("abstract") or d.get("abstractAuto") or "").strip()
    if abstract:
        # 右栏摘要框才是用户看到/编辑的那份 —— 标记为 abstract，
        # 前端才知道该往右栏跳而不是中栏。
        texts["abstract"] = abstract
        texts_src["abstract"] = "abstract"
    full_text = "\n".join(texts.values())

    # ---------- 1) 数字 → 源产物核验（论文 §3.5 numerical verification） ----------
    traced = 0
    untraced = 0
    # 变量 → 跨节出现的数值（内部一致性）
    var_kind_vals = {}
    for pk, txt in texts.items():
        for sent in _sentences(txt):
            for kind, raw, v, var in _claims_in(sent, var_universe):
                # 先查变量专属
                matched = None
                if var and var in by_var and kind in by_var[var]:
                    for av, prov, model in by_var[var][kind]:
                        if _match_num(v, raw, av):
                            matched = (prov, model)
                            break
                if matched is None and kind in by_kind:
                    for av, prov, model in by_kind[kind]:
                        if _match_num(v, raw, av):
                            matched = (prov, model)
                            break
                if matched is not None:
                    traced += 1
                    if var:
                        var_kind_vals.setdefault((var, kind, matched[1]), []).append((v, pk))
                    continue
                untraced += 1
                # 未追溯：说明它声称的数字在产物里根本不存在
                near = ""
                if var and var in by_var:
                    cands = by_var[var].get(kind) or []
                    if cands:
                        near = "；产物中 %s 的同类值为 %s（%s）" % (
                            var, ", ".join(str(x[0]) for x in cands[:4]), cands[0][1])
                add("result", "violation", pk, sent,
                    "正文声明 %s=%s，但源产物（回归结果表 / 运算记录）中找不到可对应的数值%s。" % (
                        {"coef": "系数", "t": "t 值", "p": "p 值", "se": "标准误",
                         "r2": "R²", "n": "观测数", "ncl": "聚类个体数"}.get(kind, kind),
                        raw, near),
                    "按论文 §3.5，把该数字回填到源产物（回归结果表.csv）后再写；"
                    "若产物已变，重新生成该节而不是手改数字。",
                    near)
                if var:
                    var_kind_vals.setdefault((var, kind, None), []).append((v, pk))

    # ---------- 2) 跨节数值不一致（论文 internal inconsistencies） ----------
    # 只在**同一模型**下比对：同一变量在基准 / 替代规格 / 子样本下系数本就不同。
    for (var, kind, model), items in sorted(var_kind_vals.items(), key=lambda x: (x[0][0], x[0][1])):
        if model is None:
            continue          # 未追溯的数字已在第 1 步报过
        vals = items
        if len(vals) < 2:
            continue
        uniq = []
        for v, pk in vals:
            if not any(_match_num(v, str(v), u[0]) for u in uniq):
                uniq.append((v, pk))
        if len(uniq) > 1:
            add("consistency", "violation", uniq[1][1],
                "%s 在不同章节写了不同的%s" % (var, {"coef": "系数", "t": "t 值",
                                                    "p": "p 值", "r2": "R²",
                                                    "n": "观测数"}.get(kind, kind)),
                "同一模型「%s」下，%s 的%s在不同章节取值不一致：%s。"
                "论文 §5.3 指出，摘要/正文/图表之间的数字冲突是最常见的内部一致性失败模式。" % (
                    model, var, kind,
                    "；".join("%s 节 = %s" % (PART_LABEL.get(pk, pk), v) for v, pk in uniq)),
                "以源产物为准统一各节数值；摘要与正文必须同源。",
                model)

    # ---------- 3) 符号 / 显著性 与产物矛盾 ----------
    for pk, txt in texts.items():
        for sent in _sentences(txt):
            vs = _vars_in(sent, var_universe)
            if not vs:
                continue
            var = vs[0]
            if var not in by_var:
                continue
            coef_list = by_var[var].get("coef") or []
            p_list = by_var[var].get("p") or []
            if not coef_list:
                continue
            cval = coef_list[0][0]
            sm = SIGN_RE.search(sent)
            if sm and re.search(r"(系数|beta|β)", sent) is None:
                # 只有在同句出现数值/系数语境时才判断方向，避免误伤「负向对照」这类措辞
                if not re.search(r"(系数|beta|β|t\s*[=＝]|显著)", sent):
                    continue
            if sm:
                word = sm.group(1)
                expect_neg = word in ("负向", "负相关", "显著为负", "为负")
                actual_neg = cval < 0
                if expect_neg != actual_neg:
                    add("math", "violation", pk, sent,
                        "正文称 %s 的效应为「%s」，但源产物中的系数为 %s（%s），方向相反。" % (
                            var, word, cval, coef_list[0][1]),
                        "按源产物改写方向性表述；方向错误属于可被产物直接证伪的错误。",
                        coef_list[0][1])
            sg = SIG_RE.search(sent)
            if sg and p_list:
                pval = p_list[0][0]
                try:
                    pv = float(pval)
                except (TypeError, ValueError):
                    pv = None
                if pv is not None:
                    claim_sig = sg.group(1) == "显著"
                    actual_sig = pv < 0.05
                    if claim_sig != actual_sig:
                        add("math", "violation", pk, sent,
                            "正文称 %s「%s」，但源产物中 p=%s（%s）。" % (
                                var, sg.group(1), pval, p_list[0][1]),
                            "显著性结论必须与回归结果表的 p 值一致；"
                            "若要主张显著，请先在「数据运算」重跑并确认。",
                            p_list[0][1])

    # ---------- 4) 持有期 / 规格 与产物不一致 ----------
    # 真值取**结果表标题**里的持有期（那是真正跑出来的口径），
    # 而不是 spec —— spec 可能已被 steering 改过但产物还是旧的。
    prod_horizons = set()
    for t in model_titles:
        for mm in re.finditer(r"未来\s*(\d+)\s*个月", t):
            try:
                prod_horizons.add(int(mm.group(1)))
            except (TypeError, ValueError):
                pass
    for pk, txt in texts.items():
        for sent in _sentences(txt):
            mm = re.search(r"未来\s*(\d+)\s*个月", sent)
            if not mm:
                continue
            claim_h = int(mm.group(1))
            if prod_horizons and claim_h not in prod_horizons:
                add("consistency", "violation", pk, sent,
                    "正文按「未来 %d 个月」表述，但源产物的结果表是「未来 %s 个月」口径。" % (
                        claim_h, "、".join(str(x) for x in sorted(prod_horizons))),
                    "统一持有期口径：正文、结果表标题、图表必须同源。",
                    "结果表标题：%s" % ("；".join(model_titles[:3]) or "—"))
            elif not prod_horizons and spec.get("horizon") is not None:
                if claim_h != int(spec["horizon"]):
                    add("consistency", "violation", pk, sent,
                        "正文按「未来 %d 个月」表述，而本篇规格是「未来 %d 个月」。" % (
                            claim_h, int(spec["horizon"])),
                        "统一持有期口径。")

    # ---------- 5) 引用核验（论文 hallucinated citations） ----------
    ref_idx = _ref_index(d.get("refs"))
    cited_keys = set()
    for pk, txt in texts.items():
        for sent in _sentences(txt):
            for mm in CITE_ANCHOR.finditer(sent):
                year = mm.group(1)
                who = _author_chunk(sent, mm.start()).strip(" ,，、;；")
                surnames = _name_tokens(who)
                if not surnames:
                    continue
                hit_key = None
                hit_surname = None
                for key, ref in ref_idx.items():
                    s = _match_ref(ref, surnames, year)
                    if s:
                        hit_key, hit_surname = key, s
                        break
                if hit_key is None:
                    add("citation", "violation", pk, sent,
                        "正文提到「%s（%s）」，但参考文献表中没有该文献。" % (who, year),
                        "论文 §5.3：幻觉引用是几乎必被升级为正式违规的模式（13/16）。"
                        "只引用「引用核验」已通过的条目；未核验的文献不得写进正文。")
                else:
                    cited_keys.add(hit_key)
                    ref = ref_idx[hit_key]
                    if year and ref["years"] and year[:4] not in {y[:4] for y in ref["years"]}:
                        add("citation", "violation", pk, sent,
                            "正文写 %s（%s），参考文献表中该条目的年份是 %s。" % (
                                hit_surname, year, "、".join(sorted(ref["years"]))),
                            "核对原文年份后统一；年份不符会被引用检查直接判为违规。",
                            ref["title"][:120])
    # 反向：文献从未被正文提及
    for key, ref in ref_idx.items():
        if key in cited_keys:
            continue
        surn = ref["surnames"]
        if surn and any(re.search(r"(?<![^\W\d_])" + re.escape(s) + r"(?![^\W\d_])",
                                  full_text, re.UNICODE) for s in surn):
            continue
        add("citation", "weakness", "lit",
            ref["title"][:120],
            "参考文献表中的该条目在正文里没有任何引用位置（%s）。" % ref["title"][:120],
            "论文 §3.5 refinement 阶段含 citation 检查："
            "要么在正文补上引用位置，要么从参考文献表移除。")

    # ---------- 6) 方法 / 基线 幻觉 ----------
    for pk, txt in texts.items():
        for sent in _sentences(txt):
            for m in CONCRETE_METHODS:
                if m not in sent:
                    continue
                pre = sent[:max(0, sent.find(m) - 30)]
                if NEG_CUE.search(pre):
                    continue
                if any(m in (t or "") for t in model_titles) or m in " ".join(methods):
                    continue
                add("method", "violation", pk, sent,
                    "正文提到方法「%s」，但本篇的真实产物里只有：%s。" % (
                        m, "；".join(model_titles[:3]) or "无结果表"),
                    "论文 §5.3：幻觉方法/基线 20/22 被升级为正式违规。"
                    "只写实际跑过的模型；未跑的模型应明确写进局限。")
            for w in BASELINE_WORDS:
                if w in sent and len(model_titles) < 2:
                    add("design", "weakness", pk, sent,
                        "正文提到「%s」，但结果表中只有 %d 个规格。" % (w, len(model_titles)),
                        "论文 §5.3：弱基线是实验类缺陷的高频项（30.1%）。"
                        "补一个对照规格，或删去基线表述。")

    # ---------- 7) 新颖性断言（虚假新颖性） ----------
    NOVEL_RE = re.compile(r"(首次|首个|首创|第一个|填补.{0,4}空白|尚无研究|从未有|前所未有|novel for the first time)")
    for pk, txt in texts.items():
        for sent in _sentences(txt):
            if NOVEL_RE.search(sent):
                add("novelty", "weakness", pk, sent,
                    "出现了新颖性断言，但正文没有给出「此前无人做过」的对照证据。",
                    "论文 §5.3：新颖性是低分论文最常被批评的维度（低分组 42.4%），"
                    "而高分组仅 18%。要么补文献支撑，要么改成审慎表述。")

    # ---------- 8) 实验设计缺陷 ----------
    titles_joined = " ".join(model_titles)
    body_all = full_text
    if "消融" not in titles_joined and "消融" not in body_all:
        add("design", "weakness", "results", "",
            "全文与结果表中都没有消融实验（ablation）。",
            "论文 §5.3：缺失消融是实验类缺陷第二高频项（31.9%）。"
            "至少对主结果做一次「逐项剔除」的消融。")
    if not re.search(r"(反向对照|安慰剂|placebo|对照实验|阳性对照)", titles_joined + body_all, re.I):
        add("design", "weakness", "results", "",
            "没有找到正向/反向对照实验。",
            "显著性结论若无对照组，无法排除「共同冲击」解释；"
            "论文要求对每一项发现配置反向对照。")
    if not re.search(r"稳健|替代规格|robust", titles_joined + body_all, re.I):
        add("design", "weakness", "robust", "",
            "没有找到稳健性规格（替代样本 / 替代设定）。",
            "论文 §5.3：仅「实验范围窄」一项就占 46.8% 的批评。")
    add("design", "weakness", "results", "",
        "评估范围为单一市场 / 单一数据集（本系统当前只接入 A 股月度面板）。",
        "论文 §5.3：评估范围窄与跨设定泛化差并列第一（各 46.8%）。"
        "至少补一个时间子样本或替代设定的泛化检验。")

    # ---------- 9) 语言病理 ----------
    for pk, txt in texts.items():
        for sent in _sentences(txt):
            if re.search(r"(待补|待填|TODO|FIXME|占位|此处填入|尚未生成|请先在|\?\?\?|XXX)", sent):
                add("language", "weakness", pk, sent,
                    "正文残留占位/待办措辞。",
                    "论文 §5.3：语言病理多被记为弱点而非违规，但会直接影响可读性评分。")
            if "**" in sent:
                # snippet 必须是**正文原文**，前端靠它定位到具体文字。
                add("language", "weakness", pk, sent,
                    "正文里残留了 Markdown 粗体标记（**…**）",
                    "正文中出现 ** 标记，导出/渲染时会显示成星号。",
                    "去掉标记，或改用真正的加粗样式。")
    # 重复段落
    seen = {}
    for pk, txt in texts.items():
        for sent in _sentences(txt):
            if len(sent) < 30:
                continue
            seen.setdefault(sent, []).append(pk)
    for sent, pks in seen.items():
        if len(pks) >= 2:
            add("language", "weakness", pks[-1], sent,
                "同一句话在 %d 处重复出现（%s）。" % (
                    len(pks), "、".join(sorted(set(PART_LABEL.get(p, p) for p in pks)))),
                "论文 §5.3：重复内容属于语言病理；也说明生成时未做去重。")

    # ---------- 汇总 ----------
    summary = {
        "modes": [{"mode": k, "label": v, "en": e,
                   "count": sum(1 for f in findings if f["mode"] == k)}
                  for k, v, e in MODES],
        "violations": sum(1 for f in findings if f["severity"] == "violation"),
        "weaknesses": sum(1 for f in findings if f["severity"] == "weakness"),
        "byPart": {},
        "numeric": {"traced": traced, "untraced": untraced},
        "refs": len(d.get("refs") or []),
        "models": len(model_titles),
    }
    for f in findings:
        summary["byPart"][f["part"]] = summary["byPart"].get(f["part"], 0) + 1
    findings.sort(key=lambda f: (0 if f["severity"] == "violation" else 1, f["mode"]))
    summary["verdict"] = (
        "存在 %d 项可被源产物证伪的违规，%d 项弱点。" % (summary["violations"], summary["weaknesses"])
        if findings else "未发现完整性问题。")
    return {"ok": True, "findings": findings, "summary": summary,
            "auditedAt": int(__import__("time").time() * 1000)}
