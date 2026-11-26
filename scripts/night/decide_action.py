#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
当日任务判定器 —— 判断"今天有没有可以完成的实质任务"，并决定本轮走哪条路。

为什么要有这一步（2026-11-19 用户要求）：
  原流程是「读画像 → 联网搜候选 → 选工具 → 动手」，
  等于默认每晚都在联网找东西。但极米记得里有大量**当日就能做完的实质任务**
  （论文初稿某节、PPT 某页、申请材料、回归脚本、引用核验…）。
  这些比"再找一个新工具"更值钱。

输出两条路：
  mode = "task"   → 当日有实质任务 → 直接去完成它（不联网搜新工具）
  mode = "search" → 当日没有可完成的实质任务 → 才走原来的联网搜索路径

判定是**确定性规则**（关键词 + 可完成性 + 截止期），不是让 agent 自由发挥。
agent 可以推翻，但必须在报告里写明理由。

输入（都可覆盖，便于单测）：
  --jm-file     极米采集结果，默认 memory/nightly/jm_<date>.json
  --daily-file  当日工作区记忆，默认 memory/<date>.md
  --out         判定结果 JSON，默认 memory/nightly/<date>_decision.json
  --prompt-out  渲染后的任务正文，默认 memory/nightly/<date>_prompt.md
  --base-prompt 基础任务正文，默认 scripts/night/TASK_PROMPT.md
"""
import argparse, json, os, re, sys
from datetime import datetime, timedelta, timezone

WS = os.path.expanduser("~/.openclaw/workspace")
MEM = os.path.join(WS, "memory")
# ============================================================
# 基础论文（2026-11-26 起）
# ------------------------------------------------------------
# 主人要求：凌晨自主构建如果做这篇论文，**必须在这篇初稿基础上继续写**，
# 而不是另起炉灶重写。配置在 memory/nightly/_base_paper.json。
# 判定为 task 时把这段注入任务正文，让 agent 知道基线在哪、硬约束是什么。
BASE_PAPER = os.path.join(MEM, "nightly", "_base_paper.json")
NIGHT = os.path.join(WS, "scripts", "night")
TZ = timezone(timedelta(hours=8))

# ---------------------------------------------------------------- 规则表

# 交付物关键词：出现即说明这条是"要产出东西"，而不是"想想就好"
# (正则, 标签)
DELIVERABLE_PAIRS = [
    (r"论文|初稿|草稿|章节|文献综述|综述|摘要|附录|参考文献", "论文文本"),
    (r"PPT|ppt|幻灯片|演示稿|slides", "演示稿"),
    (r"简历|CV|cover ?letter|求职信|申请材料|申请文书|网申|申硕|推荐信", "申请材料"),
    (r"回归|实证|数据表|结果表|描述统计|稳健性|代码|脚本|程序|模型", "分析产出"),
    (r"图表|作图|绘图|可视化|图\s*\d|表\s*\d", "图表"),
    (r"数据集|数据收集|爬取|抓取|整理数据|清洗", "数据"),
    (r"核验|核查|校对|引用|参考文献核验|查重", "核验"),
    (r"报告|作业|讲稿|proposal|提案|计划书|开题", "文档"),
    (r"翻译|润色|改写|排版|格式整理", "文本加工"),
    (r"问卷|调查|访谈提纲|实验设计|测量", "研究设计"),
]
DELIVERABLE = [p for p, _ in DELIVERABLE_PAIRS]

# 可完成性动词：有动作 + 有产出才可能今晚做完
ACTION = [
    r"完成|做完|写完|写好|补全|填好|整理|生成|导出|跑通|复现|重跑|核对|核验|更新|修改|修|做|产出|提交|打印|排版|剪辑|翻译|润色|实现|搭建|部署|测试",
]

# 截止期信号
DUE = [
    r"截止|deadline|due\b|之前|前交|要交|到期|ddl",
    r"今天|今晚|明天|后天|本周|这周|下周|月底|月初",
]

# 不可由 agent 独立完成（需要人在场/线下/外部账户）
NOT_AGENT = [
    r"面试|宣讲|面试课|面谈",
    r"上课|听课|出席|参加|开会|见面|面聊|拜访",
    r"缴费|交钱|转账|缴费|还款|报名费|交材料给",
    r"体检|打印店|寄出|邮寄|快递|签字|盖章",
    r"打电话|接电话|微信问|问老师|去问",
    r"买|购买|下单|订机票|订酒店",
]

# 心跳巡检模板噪声（出现在工作区每日记忆里）
NOISE = [
    re.compile(r"heartbeat", re.I),
    re.compile(r"心跳"),
    re.compile(r"巡检"),
    re.compile(r"^\s*[-*]?\s*(无新情况|no new|一切正常|无异常)"),
    re.compile(r"自检|终态自检|工具级坑"),
]

# ⚠️ 工程自记噪声（2026-11-19 实测踩到）
# 工作区 memory/<date>.md 是**agent 自己写的技术记录**（凌晨系统复盘、踩坑记录），
# 不是用户的待办清单。原先把它当候选源，导致这些行被误判成"用户当日任务"：
#   - "产出已人工核验："          → 命中"核验"
#   - "修复一（自我快照）：..."     → 命中"代码/模型"
#   - "***execv 不做 PATH 查找**"  → 命中"申请材料"（申请 申硕 里的"申"）
# 症状：判定从 search 翻成 task，任务列表全是无关的工程碎屑。
# 处置：技术味重的行直接排除；且工作区记忆只从**明确的待办段落**里取。
TECH_RECORD = [
    re.compile(r"`[^`]+`"),                 # 含行内代码
    re.compile(r"\b(cron|pid|PPID|setsid|fork|execv|PATH|shell|bash|zsh|json|yaml|yml|regex|API|PID)\b", re.I),
    re.compile(r"\.(py|sh|md|json|js|ts|txt|log|csv)\b"),
    re.compile(r"https?://"),
    re.compile(r"(初始化|自记|复盘|踩坑|坑位|实现细节|根因|排查|修复过程|验收结果|执行链|流水线|采集器|判定器)"),
    re.compile(r"^[*_`#>\-| ]{0,4}\*?\*?[^：:]{0,30}[：:]\s*$"),
]

# 工作区记忆里，只有这些标题下的内容才被视为"用户待办"
TODO_SECTION = re.compile(
    r"(待办|待办事项|任务|要做|要做的事|安排|计划|截止|deadline|ddl|今日重点|"
    r"下一步|next\s*steps?|todo|action\s*items?)", re.I)

# ⚠️ 2026-11-19 04:00 实测回归（第二次同类问题）
# 上一版靠 is_tech_record() 对**单行**做技术味过滤，结果仍漏：
#   memory/<date>.md 里的这些行照样被判成"用户当日任务" →
#     "+ 必须有交付物（论文/PPT/申请材料/…）"   ← 规则定义行
#     '- "产出已人工核验：" → 命中"核验"'          ← 引用的示例行
#     '- "修复一（自我快照）：…" → 命中"代码/模型"'  ← 引用的示例行
# 症状：判定从 search 翻成 task，任务列表全是 agent 自己的工程碎屑
#       （2026-11-18 与 2026-11-19 两天都被误翻）。
# 教训：工作区每日记忆的**主体是 agent 的工程复盘，不是用户待办**。
# 单行正则挡不住"用自然语言引用规则/示例"的行 —— 必须**按段落标题分类**：
#   1) 标题命中 TECH_SECTION → 该段整体是 agent 自记，整段跳过；
#   2) 行命中 META_LINE → 规则定义/引用示例，跳过；
#   3) 只有 USER_SECTION 下的正文行才可能是用户待办。
TECH_SECTION = re.compile(
    r"(踩坑|教训|根因|排查|修复|验收|实测|新增组件|新增条目|可执行组件|"
    r"规则|判定|脚本|实现|架构|组件|流水线|执行链|采集器|判定器|"
    r"数据源|状态|结论|收尾|顺带|其它|其他|已落地|复盘|记录|说明|"
    r"路径判定|关键实现坑|坑位|验收结果|验收结论|当前状态)", re.I)

# ⚠️ 第二类误判（2026-11-19 04:20 实测）：标题里带"任务"两个字的**agent 工程记录**
#   "## 15:55 新建桌面「任务进度看板」"  → 整段被判成用户待办段
#   "### 三、Oatly 利用率口径：之前记的 TODO 方向是错的，已纠正" → 同理
# 处置：这些是 agent 自己的**已完工记录**，标题里带"任务/TODO/计划"纯属巧合，
#       因此标题侧再补一批 agent 语汇；行侧再加"已完成"判定（见 DONE_LINE）。
TECH_SECTION_EXTRA = re.compile(
    r"(新建|看板|Heartbeat|第\s*\d+\s*轮|自动化|工具级|口径|纠正|"
 r"止血|bug|假数据|造假|巡检)", re.I)

# 用户侧段落标题（与 TODO_SECTION 并集使用）
USER_SECTION = re.compile(
    r"(待办|待办事项|今日待办|任务|要做|要做的事|今日安排|今日计划|截止|"
    r"deadline|ddl|今日重点|明天要做|下一步|next\s*steps?|todo|action\s*items?)", re.I)

# 元规则行：定义句 / 引用示例句 / 对话口吻
META_LINE = re.compile(
    r"(→|=>|命中|必须有|禁止|允许|应当|不该|例如|比如|示例|"
    r"判定为|修正为|症状：|根因是|修法|修完|实测|复测|"
    r"^[\"“'‘]|[\"”'’][\s】\]）]*$)", re.I)

# 已完成/历史陈述句 —— 不是待办（用户待办通常是未完成动作）
DONE_LINE = re.compile(
    r"(已完成|已修好|已修|已更新|已止血|已纠正|已生成|已落地|已核验|"
    r"已归档|已删除|无新情况|刚刚|刚才|此前误判|之前记的|"
    r"实测踩|踩到的坑|坑位)", re.I)

# 纯愿望/状态类，无交付物
VAGUE = [
    r"想(要)?(学|看|读|了解|研究|试|学一下)",
    r"考虑|打算|计划(?!写)|准备(?!做)|也许|可能|要不要|该不该",
    r"了解一下|调研一下|关注一下",
]

# 候选来源权重：越具体越优先
SOURCE_WEIGHT = {
    "jm-loop": 5.0,        # 极米未闭环事项（待办/愿望）
    "jm-memory-today": 4.5,  # 极米当日记忆
    "jm-card-today": 4.0,   # 极米当日卡片
    "daily-md": 3.5,         # 工作区当日记忆
    "jm-transcript": 1.5,    # 原始转录（噪声大，权重低）
    "jm-meeting": 1.0,
}

DATE_KEYS = ["date", "day", "occurredAt", "occurred_at", "createdAt", "created_at",
             "timestamp", "ts", "startTime", "start_time", "time", "dateStr", "when"]


def read(path, limit=None):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read() if limit is None else f.read(limit)
    except Exception:
        return ""


def is_noise(text):
    t = (text or "").strip()
    if not t:
        return True
    return any(p.search(t) for p in NOISE)


def is_tech_record(text):
    """判断一行是否属于 agent 自己的工程记录（而非用户待办）。"""
    t = (text or "").strip()
    if not t:
        return False
    return any(p.search(t) for p in TECH_RECORD)


def entry_date(rec):
    """尽力从一条极米记录里抠出日期字符串（YYYY-MM-DD）。"""
    for k in DATE_KEYS:
        v = rec.get(k)
        if not v:
            continue
        s = str(v)
        m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
        if m:
            return m.group(1)
        # 毫秒/秒时间戳
        if re.fullmatch(r"\d{10,13}", s):
            n = int(s)
            if n > 1e12:
                n //= 1000
            try:
                return datetime.fromtimestamp(n, TZ).strftime("%Y-%m-%d")
            except Exception:
                pass
    return None


def hit(patterns, text):
    """返回第一个命中的原始文本（无则 None）。patterns 是正则字符串列表。"""
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return m.group(0)
    return None


def hit_label(pairs, text):
    """返回 (命中文本, 标签)。pairs 是 (正则, 标签) 列表。"""
    for p, label in pairs:
        m = re.search(p, text, re.I)
        if m:
            return m.group(0), label
    return None, None


def score_candidate(title, body, source, today):
    """返回 (score, deliverable, reasons, blockers)。score<=0 表示不采纳。"""
    text = f"{title}\n{body}"
    reasons, blockers = [], []

    deliverable, dlabel = hit_label(DELIVERABLE_PAIRS, text)
    if not deliverable:
        return 0, None, [], ["无明确交付物"]
    reasons.append(f"交付物:{dlabel}")

    if not hit(ACTION, text):
        reasons.append("无动作动词")
        blockers.append("只有状态/主题，没有可执行动作")
    else:
        reasons.append("有动作动词")

    due = hit(DUE, text)
    if due:
        reasons.append(f"含时间信号:{due}")

    not_agent = hit(NOT_AGENT, text)
    if not_agent:
        blockers.append(f"需人工在场/线下:{not_agent}")

    if hit(VAGUE, text) and not due:
        blockers.append("属于愿望/意向而非当日交付")

    base = SOURCE_WEIGHT.get(source, 1.0)
    if source in ("jm-memory-today", "jm-card-today", "daily-md"):
        base += 0.5          # 当日来源
    s = base
    if deliverable:
        s += 2.0
    if due:
        s += 1.5
    if not hit(ACTION, text):
        s -= 1.5
    if blockers:
        s -= 2.0

    # 截断正文做打分，避免长转录刷分
    t_short = text[:300]
    if hit(DELIVERABLE, t_short):
        pass
    else:
        s -= 1.0

    return round(s, 2), dlabel, reasons, blockers


def collect_candidates(jm, daily_md, today):
    """从极米 + 当日工作区记忆里抽候选任务。"""
    cands = []

    def add(title, body, source):
        title = (title or "").strip()
        body = (body or "").strip()
        if not title and not body:
            return
        # 只检查非空字段：is_noise("") 为 True，空正文不应把候选丢掉
        if title and is_noise(title):
            return
        if body and is_noise(body[:200]):
            return
        # 工程自记行排除（技术味重 → 不是用户待办）
        probe = f"{title}\n{body}"[:400]
        if source in ("daily-md", "jm-transcript") and is_tech_record(probe):
            return
        s, dv, reasons, blockers = score_candidate(title, body, source, today)
        cands.append({
            "title": title[:160] or body[:160],
            "excerpt": body[:300],
            "source": source,
            "score": s,
                    "deliverable": dv,
            "reasons": reasons,
            "blockers": blockers,
        })

    if jm:
        for r in jm.get("open_loops", []) or []:
            if not isinstance(r, dict):
                r = {"title": str(r)}
            add(r.get("title", ""), r.get("text") or r.get("detail") or r.get("note") or "",
                "jm-loop")

        for r in jm.get("window", []) or []:
            if not isinstance(r, dict):
                continue
            st = r.get("sourceType", "")
            d = entry_date(r)
            if d != today:
                continue
            body = r.get("text") or ""
            if st == "memory":
                add(r.get("title", "") or body[:60], body, "jm-memory-today")
            elif st == "card":
                add(r.get("title", ""), body or r.get("title", ""), "jm-card-today")
            elif st == "meeting":
                add(r.get("title", ""), body, "jm-meeting")

        for r in jm.get("transcripts", []) or []:
            if not isinstance(r, dict):
                continue
            d = entry_date(r)
            if d and d != today:
                continue
            # 转录长且杂，按行取有交付物信号的句子
            for line in (r.get("text") or "").splitlines():
                line = line.strip()
                if 6 <= len(line) <= 160 and hit(DELIVERABLE, line):
                    add(line[:120], "", "jm-transcript")

    # 工作区当日记忆：**按段落标题分类**取候选（2026-11-19 修）。
    # 旧版逻辑（先找待办段落，找不到就全量扫描）有两个洞：
    #   ① 标题里含"判定/规则/踩坑"等词的段落是 agent 自记，却因为段落里
    #      出现了"任务"二字而被当成待办段；
    #   ② 回退全量扫描时，单行过滤挡不住"引用示例"的自然语言行。
    # 新版规则：
    #   - 标题命中 TECH_SECTION → 整段跳过（agent 工程自记）
    #   - 标题命中 USER_SECTION → 该段正文行才可能是用户待办
    #   - 其它标题（无待办词）→ 整段跳过
    #   - 任何情况下都再过一遍 META_LINE（规则定义/引用示例）
    if daily_md:
        section_kind = None          # "user" | "tech" | None
        # 整份文件**一个标题都没有** → 当作纯待办清单（用户手写风格）；
        # 只要有标题，就按标题分类，标题之前的内容一律忽略。
        has_heading = any(l.strip().startswith("#") for l in daily_md.splitlines())
        for raw in daily_md.splitlines():
            line = raw.strip()
            if line.startswith("#"):
                head = re.sub(r"^#+\s*", "", line)
                if TECH_SECTION.search(head) or TECH_SECTION_EXTRA.search(head):
                    section_kind = "tech"
                elif USER_SECTION.search(head):
                    section_kind = "user"
                else:
                    section_kind = None
                continue
            if not has_heading:
                section_kind = "user"
            if section_kind != "user":
                continue
            line = re.sub(r"^[-*+]\s*", "", line).strip()
            if not (6 <= len(line) <= 200):
                continue
            if META_LINE.search(line):
                continue
            if DONE_LINE.search(line):
                continue
            if hit(DELIVERABLE, line):
                add(line[:160], "", "daily-md")

    cands.sort(key=lambda x: -x["score"])
    return cands


def decide(cands, jm_ok, today):
    accepted = [c for c in cands if c["score"] >= 4.0 and not c["blockers"]]
    rejected = [c for c in cands if c not in accepted][:12]
    if accepted:
        mode = "task"
        reason = f"当日检出 {len(accepted)} 条可完成的实质任务，优先完成而不是联网找新工具"
    else:
        mode = "search"
        reason = "当日没有可完成的实质任务" + ("" if jm_ok else "（且极米数据不可用，仅依据本地记忆）")
        if jm_ok:
            reason += "；按设计走联网搜索路径，且候选必须贴合用户画像"
    return mode, reason, accepted[:5], rejected


DECISION_BLOCK = """<!-- DECISION_BLOCK -->

## ⚠️ 本轮路径判定（自动生成，优先于下面的通用流程）

**判定结果：`{mode}`**
**判定理由：** {reason}
**判定时间：** {now}（当日 = {today}）
**极米数据：** {jm_state}

{body}

### 你必须怎么执行

- 若判定为 **`task`**：**直接去完成这些任务**。不要另起炉灶去联网搜新工具。
  对每条任务：读输入 → 动手做 → 产出实际文件 → 记录真实结果。
  外部资料只允许"为完成该任务服务"，不能喧宾夺主。
- 若判定为 **`search`**：按下面原有的「步骤 2 判断与选题 → 步骤 3 动手做」走联网搜索路径。
- **你可以推翻判定**：如果某条任务实际做不了（缺数据、缺环境、超时），
  或你判断有更该做的事，**允许改判**，但必须在 `00_晨间简报.md` 里
  写明「原判定 / 改判理由 / 实际做了什么」。
- 无论走哪条路，都必须**真正动手产出**，不许只给推荐清单。

---

"""


def read_base_paper():
    """读基础论文配置。没有就返回 None（不阻断原有流程）。"""
    try:
        with open(BASE_PAPER, encoding="utf-8") as f:
            d = json.load(f)
        return d if d and d.get("code") else None
    except Exception:
        return None


def base_paper_block(bp):
    """把基础论文信息渲染成任务正文里的一段。"""
    if not bp:
        return ""
    L = ["", "### 📌 本轮基础论文（必须在其基础上继续写）", "",
         "本轮若做的是这篇论文，**必须以它为基线继续写**，不要另起炉灶重写：", "",
         "- **论文**：`%s`（code `%s` / topic `%s`）" %
         (bp.get("title", ""), bp.get("code"), bp.get("topic")),
         ]
    if bp.get("farsPaper"):
        L.append("- **FARS 论文 JSON**（正文/参考文献/数字锚点都在里面）：`%s`" % bp["farsPaper"])
    if bp.get("workDir"):
        L.append("- **可复现工作目录**（数据/脚本/结果 CSV）：`%s`" % bp["workDir"])
    if bp.get("dataDir"):
        L.append("- **源数据目录**：`%s`" % bp["dataDir"])
    L += [
        "- **硬约束**：",
        "  1. **引用必须经 Crossref 权威核验** —— 用 "
        "`tools/fars-dashboard/fars_data.py` 的 `crossref_verify()`；"
        "作者/期刊/年份/文章号与权威记录不一致 → 以权威记录为准，"
        "**不要保留初稿里的错误**。",
        "  2. **数字必须与 results CSV 对账** —— 用 "
        "`tools/night/paper_verify/verify_table_numbers.py` 和 "
        "`verify_prose_claims.py`；正文里的每个系数/t/p 都要能在 CSV 里找到。",
        "  3. **数据来源声明要真实** —— 论文写「来自 Bloomberg」但底层其实是 EViews "
        "工作簿/公开 CSV，这是硬伤；改用 `fars_data.fred_series()` 拉可复现的公开序列"
        "（FRED 免 key），并如实标注来源。",
        "  4. **新结论必须标明是新增的还是复现的** —— 不要把已有结论当成新发现。",
        ""]
    if bp.get("note"):
        L.append("- **备注**：%s" % bp["note"])
        L.append("")
    return "\n".join(L)


def render_prompt(base, decision, out_path):
    body_lines = []
    if decision["mode"] == "task":
        body_lines.append("**当日待完成实质任务（按优先级）：**\n")
        for i, t in enumerate(decision["tasks"], 1):
            body_lines.append(
                f"{i}. **{t['title']}**\n"
                f"   - 交付物类型：{t['deliverable']}\n"
                f"   - 来源：{t['source']}\n"
                f"   - 判定依据：{'; '.join(t['reasons'])}\n"
                f"   - 摘录：{t['excerpt'][:160]}\n")
        body_lines.append("**要求：** 至少真正完成其中 1 条，产出可交付文件；"
                          "做不到的要如实写明卡在哪一步。\n")
    else:
        body_lines.append("**当日无实质任务可完成 → 走联网搜索路径**（见下方步骤 2/3）。\n")
        if decision["rejected"]:
            body_lines.append("已排除的候选（供参考，不要重复挑）：\n")
            for t in decision["rejected"][:6]:
                b = "; ".join(t["blockers"]) or "未达阈值"
                body_lines.append(f"- {t['title'][:90]} —— {b}\n")

    bp = read_base_paper()
    if bp:
        # 基础论文块放在「当日任务」之后、「怎么执行」之前
        body_lines.append(base_paper_block(bp))

    block = DECISION_BLOCK.format(
        mode=decision["mode"],
        reason=decision["reason"],
        now=decision["generated_at"],
        today=decision["date"],
        jm_state=decision["jm_state"],
        body="".join(body_lines),
    )

    if "<!-- DECISION_BLOCK -->" in base:
        out = base.replace("<!-- DECISION_BLOCK -->", block.strip())
    else:
        out = block.strip() + "\n\n---\n\n" + base
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(out)
    return out_path


def main():
    ap = argparse.ArgumentParser(description="当日任务判定器")
    now = datetime.now(TZ)
    today = now.strftime("%Y-%m-%d")
    ap.add_argument("--jm-file", default=os.path.join(MEM, "nightly", f"jm_{today}.json"))
    # ⚠️ 降级副本隔离开关：生产环境要（夜间手机休眠时用白天的成功副本），
    #    但验收测试必须禁用（否则测试期望的「极米不可用→降级」永远不成立）。
    ap.add_argument("--no-latest-good", action="store_true",
                    help="禁用 jm_latest_good.json 降级副本（测试用）")
    ap.add_argument("--daily-file", default=os.path.join(MEM, f"{today}.md"))
    ap.add_argument("--out", default=os.path.join(MEM, "nightly", f"{today}_decision.json"))
    ap.add_argument("--prompt-out", default=os.path.join(MEM, "nightly", f"{today}_prompt.md"))
    ap.add_argument("--base-prompt", default=os.path.join(NIGHT, "TASK_PROMPT.md"))
    a = ap.parse_args()

    jm = None
    raw = read(a.jm_file)
    if raw.strip():
        try:
            jm = json.loads(raw)
        except Exception:
            jm = None

    def _has_payload(d):
        if not d:
            return False
        return bool(d.get("open_loops") or d.get("window")
                    or d.get("transcripts") or d.get("decisions")
                    or d.get("available_days"))

    # 夜间 04:00 手机休眠 → 端口关闭 → 当日文件可能是空/失败标记。
    # 此时回退到「最近一次成功采集」副本（白天采集的真实数据），
    # 而不是把判定降级成「无极米信息」（2026-11-19 修复）。
    jm_source = "当日文件"
    if not _has_payload(jm) and not a.no_latest_good:
        latest_good = os.path.join(MEM, "nightly", "jm_latest_good.json")
        if os.path.exists(latest_good):
            try:
                with open(latest_good, encoding="utf-8") as f:
                    g = json.load(f)
                if _has_payload(g):
                    jm = g
                    jm_source = "最近一次成功副本（jm_latest_good.json）"
            except Exception:
                pass

    jm_ok = _has_payload(jm)
    jm_stale = jm_ok and jm_source != "当日文件"

    daily_md = read(a.daily_file)
    cands = collect_candidates(jm, daily_md, today)
    mode, reason, accepted, rejected = decide(cands, jm_ok, today)

    decision = {
        "date": today,
        "generated_at": now.isoformat(),
        "mode": mode,
        "reason": reason,
        "jm_available": jm_ok,
        "jm_source": jm_source,
        "jm_stale": jm_stale,
        "jm_state": ("可用（来源：%s）" % jm_source) if jm_ok
                    else "不可用（手机休眠/端口关闭），已降级为本地记忆判断",
        "tasks": accepted,
        "rejected": rejected,
        "candidate_count": len(cands),
        "base_paper": read_base_paper(),
    }
    os.makedirs(os.path.dirname(a.out), exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(decision, f, ensure_ascii=False, indent=2)

    base = read(a.base_prompt)
    if base.strip():
        render_prompt(base, decision, a.prompt_out)

    print(json.dumps({
        "ok": True,
        "date": today,
        "mode": mode,
        "tasks": len(accepted),
        "rejected": len(rejected),
        "candidates": len(cands),
        "jm_available": jm_ok,
        "decision": a.out,
        "prompt": a.prompt_out,
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
