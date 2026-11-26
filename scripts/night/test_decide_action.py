#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
decide_action.py 验收用例 —— 12 项，全自动跑，不依赖网络/极米在线。

用例覆盖：
  A. 判定路径（task / search）
  B. 提取规则（交付物、动作、截止期）
  C. 排除规则（噪声、愿望、需人工在场）
  D. 提示词渲染（DECISION_BLOCK 注入、路径指令正确）
  E. 极米不可用时降级
  F. 日期过滤（只认当日）

跑法：python3 scripts/night/test_decide_action.py
退出码 0 = 全过。
"""
import json, os, subprocess, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "decide_action.py")

PASS, FAIL = [], []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
        print(f"  ✓ {name}")
    else:
        FAIL.append((name, detail))
        print(f"  ✗ {name}  {detail}")


def run_decider(jm=None, daily=None, base_prompt=None, extra=None):
    """跑一次判定器，返回 (stdout_json, prompt_text, decision_json)。"""
    tmp = tempfile.mkdtemp(prefix="decide_test_")
    jm_path = os.path.join(tmp, "jm.json")
    daily_path = os.path.join(tmp, "daily.md")
    dec_path = os.path.join(tmp, "decision.json")
    prompt_path = os.path.join(tmp, "prompt.md")

    if jm is None:
        jm = {"open_loops": [], "window": [], "transcripts": []}
    with open(jm_path, "w", encoding="utf-8") as f:
        json.dump(jm, f, ensure_ascii=False)
    with open(daily_path, "w", encoding="utf-8") as f:
        f.write(daily or "")

    if base_prompt is None:
        base_prompt = "# 基础正文\n\n原始内容\n"
    base_path = os.path.join(tmp, "base.md")
    with open(base_path, "w", encoding="utf-8") as f:
        f.write(base_prompt)

    # ⚠️ 必须传 --no-latest-good：生产环境的「极米降级副本」会在测试目录里
    #    命中真实工作区的 jm_latest_good.json → 让「极米不可用」的用例永远不成立。
    #    （2026-11-26 实测踩过：36 项里 7 项因此失败）
    cmd = [sys.executable, SCRIPT, "--jm-file", jm_path, "--daily-file", daily_path,
           "--out", dec_path, "--prompt-out", prompt_path, "--base-prompt", base_path,
           "--no-latest-good"]
    if extra:
        cmd += extra
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    out = {}
    try:
        out = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception:
        out = {"_raw": p.stdout, "_err": p.stderr}
    dec = {}
    if os.path.exists(dec_path):
        with open(dec_path, encoding="utf-8") as f:
            dec = json.load(f)
    prompt = ""
    if os.path.exists(prompt_path):
        with open(prompt_path, encoding="utf-8") as f:
            prompt = f.read()
    return out, prompt, dec, p


# ============ A. 判定路径 ============
print("\n[A] 判定路径")

# A1: 极米未闭环里有明确产出任务 → task
jm1 = {"open_loops": [
    {"title": "把论文文献综述初稿写完", "text": "今天要把文献综述初稿完成，明天下午要交给导师"},
    {"title": "整理实习申请材料", "text": "简历需要更新"},
], "window": [], "transcripts": []}
out, prompt, dec, _ = run_decider(jm=jm1, daily="")
check("A1 有实质任务时 mode=task", dec.get("mode") == "task", f"got {dec.get('mode')}")
check("A1 任务数>=1", len(dec.get("tasks", [])) >= 1, f"got {len(dec.get('tasks', []))}")
check("A1 命中的是论文综述任务",
      any("文献综述" in t["title"] for t in dec.get("tasks", [])),
      str([t["title"] for t in dec.get("tasks", [])]))

# A2: 没有任何实质任务 → search
jm2 = {"open_loops": [
    {"title": "想了解一下宏观经济的时间序列方法", "text": "也许找点资料看看"},
], "window": [], "transcripts": []}
out, prompt, dec, _ = run_decider(jm=jm2, daily="# 今日\n\n- 天气不错\n- 想到一个点子但还没定\n")
check("A2 无实质任务时 mode=search", dec.get("mode") == "search", f"got {dec.get('mode')}")
check("A2 理由含'没有'", "没有" in dec.get("reason", ""), dec.get("reason", ""))

# A3: 空输入 → search，且不崩
out, prompt, dec, _ = run_decider(jm={"open_loops": [], "window": [], "transcripts": []}, daily="")
check("A3 空输入 mode=search", dec.get("mode") == "search", f"got {dec.get('mode')}")
check("A3 空输入无异常退出", out.get("ok") is True, str(out))

# ============ B. 提取规则 ============
print("\n[B] 提取规则")

# B1: 交付物类型识别正确
jm_b1 = {"open_loops": [
    {"title": "做一份小组作业的 PPT", "text": "明天要展示，需要把图表整理好"},
], "window": [], "transcripts": []}
out, prompt, dec, _ = run_decider(jm=jm_b1, daily="")
check("B1 PPT 归类为演示稿",
      any(t.get("deliverable") == "演示稿" for t in dec.get("tasks", [])),
      str([(t["title"], t.get("deliverable")) for t in dec.get("tasks", [])]))

# B2: 工作区当日记忆里的任务也能被检出
daily_b2 = "# 今日待办\n\n- 完成实证回归结果表的整理\n- 核对参考文献引用格式\n"
out, prompt, dec, _ = run_decider(jm={"open_loops": [], "window": [], "transcripts": []}, daily=daily_b2)
check("B2 从每日记忆检出任务", dec.get("mode") == "task", f"got {dec.get('mode')} {dec.get('reason')}")
check("B2 来源标记为 daily-md",
      any(t.get("source") == "daily-md" for t in dec.get("tasks", [])),
      str([t.get("source") for t in dec.get("tasks", [])]))

# B3: 截止期信号加权
jm_b3 = {"open_loops": [
    {"title": "写完报告", "text": "这份报告截止今天要交"},
], "window": [], "transcripts": []}
out, prompt, dec, _ = run_decider(jm=jm_b3, daily="")
task = dec.get("tasks", [{}])[0] if dec.get("tasks") else {}
check("B3 识别出时间信号", any("时间信号" in r for r in task.get("reasons", [])), str(task.get("reasons")))

# ============ C. 排除规则 ============
print("\n[C] 排除规则")

# C1: 心跳噪声被排除
out, prompt, dec, _ = run_decider(
    jm={"open_loops": [], "window": [], "transcripts": []},
    daily="- heartbeat 第 12 轮：无新情况\n- 心跳巡检正常\n")
check("C1 心跳模板不产生候选", dec.get("candidate_count", 0) == 0,
      f"got {dec.get('candidate_count')}")

# C2: 需人工在场被排除
jm_c2 = {"open_loops": [
    {"title": "参加申硕面试", "text": "明天上午面试，需要准备材料"},
], "window": [], "transcripts": []}
out, prompt, dec, _ = run_decider(jm=jm_c2, daily="")
check("C2 需人工在场 → mode=search", dec.get("mode") == "search", f"got {dec.get('mode')}")
check("C2 排除理由记录了原因",
      any("人工" in b for t in dec.get("rejected", []) for b in t.get("blockers", [])),
      str([t.get("blockers") for t in dec.get("rejected", [])]))

# C3: 纯愿望被排除
jm_c3 = {"open_loops": [
    {"title": "想读一些关于因子模型的论文", "text": "有空了解一下"},
], "window": [], "transcripts": []}
out, prompt, dec, _ = run_decider(jm=jm_c3, daily="")
check("C3 纯愿望 → mode=search", dec.get("mode") == "search", f"got {dec.get('mode')}")

# C4: 无交付物的主题不入选
jm_c4 = {"open_loops": [
    {"title": "人工智能在金融领域的应用趋势", "text": "这个话题最近很热"},
], "window": [], "transcripts": []}
out, prompt, dec, _ = run_decider(jm=jm_c4, daily="")
check("C4 纯主题不入选", dec.get("mode") == "search", f"got {dec.get('mode')}")

# ============ D. 提示词渲染 ============
print("\n[D] 提示词渲染")

base_prompt = "# 基础任务正文\n\n步骤 1：读取输入\n"
out, prompt, dec, _ = run_decider(jm=jm1, daily="", base_prompt=base_prompt)
check("D1 渲染结果含 DECISION_BLOCK", "本轮路径判定" in prompt, prompt[:200])
check("D2 渲染结果保留原始正文", "步骤 1：读取输入" in prompt, prompt[:200])
check("D3 task 模式指示直接完成任务", "直接去完成这些任务" in prompt, "")
check("D4 task 模式列出具体任务", "文献综述" in prompt, "")
check("D5 含判定理由与时间", "判定理由" in prompt and "判定时间" in prompt, "")

out, prompt, dec, _ = run_decider(jm=jm2, daily="", base_prompt=base_prompt)
check("D6 search 模式指示走联网搜索", "走联网搜索路径" in prompt, "")
check("D7 search 模式不列任务", "当日待完成实质任务" not in prompt, "")

# D8: 基础正文里已有 DECISION_BLOCK 标记 → 替换而非重复
out, prompt, dec, _ = run_decider(jm=jm1, daily="",
                                  base_prompt="# T\n\n<!-- DECISION_BLOCK -->\n\n## 你的任务\n正文\n")
check("D8 已有标记被替换", prompt.count("本轮路径判定") == 1, f"count={prompt.count('本轮路径判定')}")
check("D9 替换后原正文保留", "## 你的任务" in prompt, "")

# ============ E. 极米降级 ============
print("\n[E] 极米降级")

out, prompt, dec, _ = run_decider(jm={"open_loops": [], "window": [], "transcripts": []},
                                  daily="- 完成论文初稿第二章\n")
check("E1 极米空数据 jm_available=false", dec.get("jm_available") is False, str(dec.get("jm_available")))
check("E2 降级说明写进 jm_state", "不可用" in dec.get("jm_state", ""), dec.get("jm_state", ""))
check("E3 极米不可用仍能靠本地记忆判定 task", dec.get("mode") == "task", f"got {dec.get('mode')}")
check("E4 降级状态写进提示词", "不可用" in prompt, "")

# ============ F. 日期过滤 ============
print("\n[F] 日期过滤")

# F1: 非当日的极米 memory 不算当日任务
jm_f1 = {"open_loops": [], "window": [
    {"sourceType": "memory", "title": "三天前的论文初稿任务", "text": "完成论文初稿",
     "date": "2026-11-16"},
], "transcripts": []}
out, prompt, dec, _ = run_decider(jm=jm_f1, daily="")
check("F1 非当日条目被过滤", dec.get("candidate_count", 0) == 0,
      f"got {dec.get('candidate_count')}")

# F2: 当日的极米 memory 算当日任务
today = out.get("date")
jm_f2 = {"open_loops": [], "window": [
    {"sourceType": "memory", "title": "今天的论文初稿任务", "text": "完成论文初稿",
     "date": today},
], "transcripts": []}
out, prompt, dec, _ = run_decider(jm=jm_f2, daily="")
check("F2 当日条目被采纳", dec.get("mode") == "task", f"got {dec.get('mode')}")

# ============ G. 工程自记不得冒充用户待办（2026-11-19 回归） ============
print("\n[G] 工程自记 vs 用户待办")

# G1: agent 复盘文档里的"规则定义行 / 引用示例行"不得被判成任务
daily_g1 = (
    "## 路径判定环节新增（用户要求，02:45 完成）\n\n"
    "**判定规则**：\n"
    "+ 必须有交付物（论文/PPT/申请材料/分析产出/图表/数据/核验/文档/文本加工/研究设计）\n"
    '- "产出已人工核验：" → 命中"核验"\n'
    '- "修复一（自我快照）：…" → 命中"代码/模型"\n'
)
out, prompt, dec, _ = run_decider(jm={"open_loops": [], "window": [], "transcripts": []}, daily=daily_g1)
check("G1 规则定义/示例行 → mode=search", dec.get("mode") == "search",
      f"got {dec.get('mode')} {[t.get('title') for t in dec.get('tasks', [])]}")

# G2: 标题里带"任务"的 agent 记录段（看板等）不得误判
daily_g2 = (
    "## 15:55 新建桌面「任务进度看板」\n\n"
    "- 已更新 4 份现行交付物（正文/README/定量分析报告），需重跑脚本\n"
    "- 已用浏览器打开页面核验：文本快照与截图均确认渲染正确\n"
)
out, prompt, dec, _ = run_decider(jm={"open_loops": [], "window": [], "transcripts": []}, daily=daily_g2)
check("G2 '任务进度看板'段 → mode=search", dec.get("mode") == "search",
      f"got {dec.get('mode')} {[t.get('title') for t in dec.get('tasks', [])]}")

# G3: 用户手写的待办段仍要能检出（不能矫枉过正）
daily_g3 = (
    "# 2026-11-19\n\n"
    "## 今日待办\n\n"
    "- 完成论文初稿第二章\n"
    "- 核对参考文献引用格式并导出 PDF\n"
)
out, prompt, dec, _ = run_decider(jm={"open_loops": [], "window": [], "transcripts": []}, daily=daily_g3)
check("G3 用户待办段仍检出 task", dec.get("mode") == "task",
      f"got {dec.get('mode')} {[t.get('title') for t in dec.get('tasks', [])]}")
check("G3 命中的是'完成论文初稿第二章'",
      any("初稿" in t.get("title", "") for t in dec.get("tasks", [])),
      str([t.get("title") for t in dec.get("tasks", [])]))

# G4: 无标题的纯清单文件仍按待办处理
daily_g4 = "- 完成论文初稿第二章\n- 整理小组作业图表\n"
out, prompt, dec, _ = run_decider(jm={"open_loops": [], "window": [], "transcripts": []}, daily=daily_g4)
check("G4 无标题纯清单 → mode=task", dec.get("mode") == "task",
      f"got {dec.get('mode')} {[t.get('title') for t in dec.get('tasks', [])]}")

# ============ 结果 ============
print("\n" + "=" * 56)
print(f"通过 {len(PASS)} / {len(PASS) + len(FAIL)}")
if FAIL:
    print("\n失败明细：")
    for n, d in FAIL:
        print(f"  ✗ {n}  {d}")
    sys.exit(1)
print("全部通过 ✓")
