#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成《FARS 路演产品说明计划书》docx。

按路演「三、展示内容与材料要求」的四个模块组织：
  1. 痛点与场景（含赛道归属）
  2. 知乎生态融合度
  3. 技术架构与亮点
  4. 未来规划与商业/社区价值
外加：可运行 Demo（核心）、操作指南、演示视频备选方案。

截图：/tmp/fars_shots（生产实例实拍，13 张）
"""
import os, json
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

SHOTS_DIR = "/tmp/fars_shots"
META = "/tmp/fars_shots.json"
# 输出路径：**不要硬编码本机绝对路径**（会泄漏用户名，CI 也会因此失败）
# 优先级：环境变量 FARS_DELIV_DIR > 桌面交付目录 > 当前目录
import os as _os
_deliv = _os.environ.get("FARS_DELIV_DIR") or _os.path.join(
    _os.path.expanduser("~"), "Desktop", "FARS Dashboard 内测版")
if not _os.path.isdir(_deliv):
    _deliv = _os.getcwd()
OUT = _os.path.join(_deliv, "FARS 路演产品说明计划书.docx")

CJK = "PingFang SC"
LATIN = "Helvetica Neue"
MONO = "Menlo"
INK = RGBColor(0x1A, 0x1A, 0x1A)
INK2 = RGBColor(0x4A, 0x4A, 0x4A)
INK4 = RGBColor(0x8A, 0x8A, 0x8A)
HOT = RGBColor(0xE0, 0x4F, 0x1F)
W = 16.6


def set_cjk(run, cjk=CJK, latin=LATIN):
    run.font.name = latin
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.append(rf)
    rf.set(qn("w:ascii"), latin)
    rf.set(qn("w:hAnsi"), latin)
    rf.set(qn("w:eastAsia"), cjk)


def p(doc, text="", size=10.5, color=INK, bold=False, align=None,
      before=0, after=6, indent=0, mono=False, italic=False):
    par = doc.add_paragraph()
    if align:
        par.alignment = align
    par.paragraph_format.space_before = Pt(before)
    par.paragraph_format.space_after = Pt(after)
    if indent:
        par.paragraph_format.left_indent = Cm(indent)
    if text:
        r = par.add_run(text)
        r.font.size = Pt(size)
        r.font.color.rgb = color
        r.bold = bold
        r.italic = italic
        set_cjk(r, MONO if mono else CJK, MONO if mono else LATIN)
    return par


def rich(doc, parts, size=10.5, before=0, after=6, indent=0):
    par = doc.add_paragraph()
    par.paragraph_format.space_before = Pt(before)
    par.paragraph_format.space_after = Pt(after)
    if indent:
        par.paragraph_format.left_indent = Cm(indent)
    for text, o in parts:
        r = par.add_run(text)
        r.font.size = Pt(o.get("size", size))
        r.font.color.rgb = o.get("color", INK)
        r.bold = o.get("bold", False)
        r.italic = o.get("italic", False)
        set_cjk(r, MONO if o.get("mono") else CJK, MONO if o.get("mono") else LATIN)
    return par


def h1(doc, num, text):
    par = doc.add_paragraph()
    par.paragraph_format.space_before = Pt(20)
    par.paragraph_format.space_after = Pt(8)
    r = par.add_run("%s  %s" % (num, text))
    r.font.size = Pt(16)
    r.bold = True
    r.font.color.rgb = INK
    set_cjk(r)
    pbdr = OxmlElement("w:pBdr")
    bot = OxmlElement("w:bottom")
    bot.set(qn("w:val"), "single"); bot.set(qn("w:sz"), "12")
    bot.set(qn("w:space"), "4"); bot.set(qn("w:color"), "E04F1F")
    pbdr.append(bot)
    par._p.get_or_add_pPr().append(pbdr)
    return par


def h2(doc, text):
    par = doc.add_paragraph()
    par.paragraph_format.space_before = Pt(13)
    par.paragraph_format.space_after = Pt(5)
    r = par.add_run(text)
    r.font.size = Pt(12.5); r.bold = True; r.font.color.rgb = INK
    set_cjk(r)
    return par


def h3(doc, text):
    par = doc.add_paragraph()
    par.paragraph_format.space_before = Pt(9)
    par.paragraph_format.space_after = Pt(3)
    r = par.add_run(text)
    r.font.size = Pt(11); r.bold = True; r.font.color.rgb = HOT
    set_cjk(r)
    return par


def bullet(doc, text, level=0, head=None):
    par = doc.add_paragraph()
    par.paragraph_format.left_indent = Cm(0.6 + level * 0.6)
    par.paragraph_format.space_after = Pt(3)
    par.paragraph_format.line_spacing = 1.4
    r = par.add_run("• " if level == 0 else "– ")
    r.font.size = Pt(10.5); r.font.color.rgb = HOT if level == 0 else INK4
    set_cjk(r)
    if head:
        rb = par.add_run(head)
        rb.font.size = Pt(10.5); rb.bold = True; rb.font.color.rgb = INK
        set_cjk(rb)
    r2 = par.add_run(text)
    r2.font.size = Pt(10.5); r2.font.color.rgb = INK2
    set_cjk(r2)
    return par


def numbered(doc, n, title, detail):
    par = doc.add_paragraph()
    par.paragraph_format.left_indent = Cm(0.8)
    par.paragraph_format.space_after = Pt(2)
    r = par.add_run("%s " % n)
    r.font.size = Pt(10.5); r.bold = True; r.font.color.rgb = HOT
    set_cjk(r)
    rt = par.add_run(title)
    rt.font.size = Pt(10.5); rt.bold = True; rt.font.color.rgb = INK
    set_cjk(rt)
    if detail:
        par2 = doc.add_paragraph()
        par2.paragraph_format.left_indent = Cm(1.5)
        par2.paragraph_format.space_after = Pt(6)
        par2.paragraph_format.line_spacing = 1.4
        rd = par2.add_run(detail)
        rd.font.size = Pt(10); rd.font.color.rgb = INK2
        set_cjk(rd)


def shot(doc, fname, caption):
    path = os.path.join(SHOTS_DIR, fname)
    if not os.path.exists(path):
        return False
    par = doc.add_paragraph()
    par.alignment = WD_ALIGN_PARAGRAPH.CENTER
    par.paragraph_format.space_before = Pt(6)
    par.paragraph_format.space_after = Pt(3)
    par.add_run().add_picture(path, width=Cm(W))
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.paragraph_format.space_after = Pt(12)
    r = cap.add_run(caption)
    r.font.size = Pt(8.5); r.font.color.rgb = INK4; r.italic = True
    set_cjk(r)
    return True


def shade(cell, hexcolor):
    tcpr = cell._tc.get_or_add_tcPr()
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear"); sh.set(qn("w:color"), "auto")
    sh.set(qn("w:fill"), hexcolor)
    tcpr.append(sh)


def table(doc, headers, rows, widths=None, fs=9):
    tbl = doc.add_table(rows=1, cols=len(headers))
    tbl.style = "Table Grid"
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, htxt in enumerate(headers):
        cell = tbl.rows[0].cells[i]
        shade(cell, "E04F1F")
        par = cell.paragraphs[0]
        par.paragraph_format.space_after = Pt(2)
        par.paragraph_format.space_before = Pt(2)
        r = par.add_run(htxt)
        r.font.size = Pt(fs + 0.5); r.bold = True
        r.font.color.rgb = RGBColor(0xFF, 0xFF, 0xFF)
        set_cjk(r)
    for ri, row in enumerate(rows):
        cells = tbl.add_row().cells
        for i, val in enumerate(row):
            cell = cells[i]
            if ri % 2 == 1:
                shade(cell, "FAF7F4")
            par = cell.paragraphs[0]
            par.paragraph_format.space_after = Pt(2)
            par.paragraph_format.space_before = Pt(2)
            par.paragraph_format.line_spacing = 1.3
            r = par.add_run(str(val))
            r.font.size = Pt(fs); r.font.color.rgb = INK2
            set_cjk(r)
    if widths:
        for row in tbl.rows:
            for i, wd in enumerate(widths):
                row.cells[i].width = Cm(wd)
    doc.add_paragraph().paragraph_format.space_after = Pt(6)
    return tbl


def callout(doc, title, body):
    tbl = doc.add_table(rows=1, cols=1)
    cell = tbl.rows[0].cells[0]
    shade(cell, "FFF4EC")
    par = cell.paragraphs[0]
    par.paragraph_format.space_after = Pt(2)
    r = par.add_run(title)
    r.font.size = Pt(10); r.bold = True; r.font.color.rgb = HOT
    set_cjk(r)
    par2 = cell.add_paragraph()
    par2.paragraph_format.space_after = Pt(2)
    par2.paragraph_format.line_spacing = 1.4
    r2 = par2.add_run(body)
    r2.font.size = Pt(9.5); r2.font.color.rgb = INK2
    set_cjk(r2)
    doc.add_paragraph().paragraph_format.space_after = Pt(6)
    return tbl


def code_block(doc, lines):
    tbl = doc.add_table(rows=1, cols=1)
    cell = tbl.rows[0].cells[0]
    shade(cell, "F5F3EF")
    cell.paragraphs[0]._p.getparent().remove(cell.paragraphs[0]._p)
    for ln in lines:
        par = cell.add_paragraph()
        par.paragraph_format.space_after = Pt(1)
        par.paragraph_format.space_before = Pt(1)
        par.paragraph_format.line_spacing = 1.3
        r = par.add_run(ln)
        r.font.size = Pt(9); r.font.color.rgb = RGBColor(0x2B, 0x2B, 0x2B)
        set_cjk(r, MONO, MONO)
    doc.add_paragraph().paragraph_format.space_after = Pt(4)
    return tbl


def pagebreak(doc):
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def build():
    shots = {s["file"]: s["note"] for s in json.load(open(META))}
    doc = Document()
    st = doc.styles["Normal"]
    st.font.size = Pt(10.5); st.font.name = LATIN
    st.element.rPr.rFonts.set(qn("w:eastAsia"), CJK)
    st.paragraph_format.space_after = Pt(6)
    st.paragraph_format.line_spacing = 1.45
    for s in doc.sections:
        s.page_width = Cm(21); s.page_height = Cm(29.7)
        s.left_margin = Cm(2.2); s.right_margin = Cm(2.2)
        s.top_margin = Cm(2.0); s.bottom_margin = Cm(2.0)

    # ================= 封面 =================
    for _ in range(3):
        doc.add_paragraph()
    p(doc, "FARS · IDE", 13, HOT, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, after=4)
    p(doc, "路演产品说明计划书", 28, INK, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER, after=6)
    p(doc, "Financial Analysis & Research Studio　·　面向实证金融论文写作的 AI 工作台",
      11.5, INK2, align=WD_ALIGN_PARAGRAPH.CENTER, after=24)
    table(doc, ["项目", "说明"], [
        ["赛道归属", "S3 赛道 —— AI×知识与探索（主）／ AI×陪伴与交互（辅）"],
        ["产品形态", "macOS 原生桌面应用（.app），离线运行，密钥本地存储"],
        ["当前版本", "内测版 v0.9"],
        ["文档日期", "2026 年 11 月 25 日"],
        ["演示形态", "可运行 Demo（真实交互原型）+ 高清录屏备选视频 + 现场操作"],
        ["截图说明", "全部为生产实例实拍（127.0.0.1:8790，真实论文与实时知乎热榜）"],
    ], widths=[3.0, 13.6])
    doc.add_paragraph()
    p(doc, "本计划书中的所有界面截图均由自动化脚本在运行中的生产实例上实拍，"
           "未经修饰或拼接；文中出现的建议文本、回归结果、知乎热榜条目均为该实例的真实运行产物。",
      9, INK4, align=WD_ALIGN_PARAGRAPH.CENTER, italic=True)
    pagebreak(doc)

    # ================= 一、痛点与场景 =================
    h1(doc, "一、", "痛点与场景")

    h2(doc, "1.1 真实问题：AI 写论文，写的是「看起来像论文的东西」")
    p(doc, "学术写作的产出物是可被逐条核验的：每个数字必须能回到某张回归表，"
           "每处引用必须真的存在，每句断言的强度必须匹配证据。"
           "但通用大模型写出来的东西往往只满足「读起来像」——"
           "它可以生成流畅的文献综述，却不会告诉你那句「Fama 与 French 建立了三因子模型」"
           "究竟来自哪篇文献、哪张表、哪个系数。")
    p(doc, "结果是研究者承担了本该由工具承担的对账成本："
           "写完之后逐条核对数字、逐处查引用、逐句判断语气是否过强。"
           "这些工作既枯燥又最容易出错——因为它需要跨文档比对，"
           "而这恰好是纯生成式工具最不擅长的。")

    h2(doc, "1.2 三个具体痛点")
    table(doc, ["痛点", "具体表现", "FARS 的应对"], [
        ["数字不可回源", "正文里的系数、样本区间、显著性，与真实回归结果对不上；"
                        "审稿时被问「这个 0.27 从哪来」无从回答",
         "机械门禁 + 完整性审计：论断逐条回源产物对账，不一致项直接列清单并附定位"],
        ["建议是空话", "「加强逻辑」「提高严谨性」「注意文献衔接」——"
                      "换个主题也照样成立，等于没说",
         "强制要求每条建议引用正文前后文的具体措辞作为依据；"
         "并禁止编造数据、文献或结论"],
        ["选方向靠想象", "「你这节要不要再严谨一点」——用户得自己在脑子里想象严谨长什么样",
         "FAN HUD 扇面：指针移动即时在正文预览该方向的实际写法，"
         "不用读完文字就能感知差异"],
    ], widths=[2.4, 7.0, 7.2])

    h2(doc, "1.3 目标用户与使用场景")
    bullet(doc, "写实证金融/经济学论文的研究生与博士生，尤其是需要自己跑回归、自己写正文的类型。",
           head="目标用户：")
    bullet(doc, "开题阶段——不知道选题是否值得写、怎么把一个现实问题变成研究问题。",
           head="场景一：")
    bullet(doc, "写作阶段——大纲有了但每节的论证接不上；已有段落想改但不知道改哪里、怎么改。",
           head="场景二：")
    bullet(doc, "投稿前——数字口径、引用真实性、语气强度需要逐条自查。",
           head="场景三：")

    h2(doc, "1.4 赛道归属说明")
    p(doc, "FARS 主赛道是 AI×知识与探索：它把「知识生产」这件事做成一条有门禁的流水线，"
           "核心价值在于**让产出可核验**。")
    p(doc, "同时它也落在 AI×陪伴与交互：陪伴体「刘看山」不是一个装饰动画，"
           "而是把工作状态翻译成即时反馈的交互件——"
           "它在生成时切到「电脑」动作、拖动光球时切到「运球」、"
           "选中文字时切到「打招呼」并询问「选中的这段，要怎么改？」。"
           "这类「状态可视 + 主动提示」的交互设计，是陪伴式 AI 的典型形态，"
           "只是这里的陪伴对象是研究工作本身。")

    shot(doc, "02_workbench_paper.png",
         "图 1　FARS 工作台：左栏论文任务、中间八段正文、右栏随上下文变化的控制面板")
    pagebreak(doc)

    # ================= 二、知乎生态融合度 =================
    h1(doc, "二、", "知乎生态融合度")
    p(doc, "FARS 不是「一个顺便支持知乎链接的工具」，而是把知乎的开放能力做成了产品的两个入口，"
           "把官方 IP 素材做成了贯穿全局的交互语言。")

    h2(doc, "2.1 能力一：知乎热榜选题入口（知识与探索）")
    p(doc, "右栏新增「知乎热榜选题」分区，直接抓取知乎实时热榜，"
           "把当下真实发生的社会议题转成可一键开题的研究入口。")
    numbered(doc, "①", "接入方式",
             "服务端代理 /api/zhihu/hot，阶梯降级 hot-list-web → total。"
             "实测知乎主接口需登录态（401），公开端点 hot-list-web 可直接取数（200）。")
    numbered(doc, "②", "数据字段",
             "每条含：排名、问题标题、摘要、热度（并解析为可排序的数值，如「827 万热度」→ 8270000）、"
             "知乎问题链接、封面图。")
    numbered(doc, "③", "开题动作",
             "点任一问题 → 选择研究主题（ESG／机器学习／股票收益可预测性）→ "
             "系统创建论文骨架并自动打开，同时把知乎问题链接、排名、热度、摘要写入论文的 origin 字段。")
    shot(doc, "11_zhihu_hot.png",
         "图 2　知乎热榜选题分区：实时抓取知乎热榜，点击即可进入开题流程")
    shot(doc, "13_zhihu_start.png",
         "图 3　从知乎选题开题：选定问题后选择研究主题，系统建骨架并自动打开")

    h2(doc, "2.2 能力二：刘看山官方 IP 素材（全场景交互语言）")
    p(doc, "产品内嵌知乎官方「刘看山」IP 素材包（6 个透明 RGBA GIF，320×320，20fps），"
           "并将其绑定到 FARS 的真实工作状态机——不是随机播放的装饰。")
    table(doc, ["FARS 工作状态", "刘看山动作", "语义"], [
        ["空闲待命", "待机", "在的，随时可以开始写"],
        ["正在生成 / 流式输出", "电脑", "正在写……有真实产物才往下走"],
        ["拖拽光球选节", "运球", "把光球拖到要写的那一节"],
        ["选中正文文字", "打招呼", "选中的这段，要怎么改？"],
        ["悬停光球 / 交互", "晃悠", "双击并按住我，可以调写作方向"],
        ["90 秒无操作", "瞌睡", "……（睡着了，戳一下）"],
    ], widths=[4.2, 3.0, 9.4])
    shot(doc, "12_kanshan.png",
         "图 4　刘看山陪伴体：状态跟随真实工作状态切换，配文同步反映当前进度")

    h2(doc, "2.3 为什么这样融合是有价值的")
    bullet(doc, "知乎提供的是「真实发生的公共议题」，这正是研究选题最好的来源——"
                "它天然带着热度、争议和未解问题，比凭空想选题更有价值。",
           head="对用户：")
    bullet(doc, "IP 素材让一个学术工具「有温度」，"
                "而这套温度不是贴图——每一个动作都对应真实状态，"
                "用户能从它的动作判断「系统现在在干什么」。",
           head="对体验：")
    bullet(doc, "知乎热榜是公开可取的开放能力，接入成本低、不依赖授权，"
                "却直接补上了「选题」这个论文工作流最前端的一环。",
           head="对生态：")

    pagebreak(doc)

    # ================= 三、技术架构与亮点 =================
    h1(doc, "三、", "技术架构与亮点")

    h2(doc, "3.1 架构总览")
    table(doc, ["层", "选型", "说明"], [
        ["模型层", "可配置多厂商接入",
         "设置面板配置 base / protocol / model；支持 Anthropic Messages 与 OpenAI 兼容协议；"
         "密钥存 macOS 钥匙串，不落文件、不进分发包"],
        ["编排层", "Python 标准库 HTTP 服务（8038 行）",
         "四步工作流编排：备齐真实产物 → 派生大纲 → 逐节写作 → 完整性审计；"
         "论断回源对账（AI Integrity Audit）"],
        ["检索层", "多通道文献检索",
         "OpenAlex → Crossref → arXiv 阶梯降级；另有章节↔文献支撑关系比对"],
        ["审计层", "独立 audit 模块",
         "机械比对正文数字与真实产物，输出 violation / weakness 两级严重度清单"],
        ["前端层", "原生 JS + 手写 SVG（12598 行）",
         "FAN HUD 扇面为手写 SVG 几何计算（角度=方向类别，半径=支配强度）；"
         "无前端框架依赖"],
        ["分发层", "PyInstaller → macOS .app",
         "静态资源随包分发；状态落到 Application Support；不依赖本机任何目录"],
    ], widths=[2.0, 4.0, 10.6])

    h2(doc, "3.2 亮点一：机械门禁 —— 可信度从「模型大概说得对」转移到「流程不允许含糊」")
    p(doc, "这是 FARS 与普通 AI 写作工具最核心的差别。没有真实产物，就不允许往下写；"
           "正文里的每个数字都要能回源到某张表；审计环节不是「模型觉得没问题」，"
           "而是机械比对后给出不一致清单。")
    shot(doc, "04_workflow.png",
         "图 5　可重现工作流四步闭环，每一步都有真实门禁")

    h2(doc, "3.3 亮点二：Agent 工作流 —— 状态机 + 幂等落盘")
    p(doc, "四步工作流不是四个按钮，而是有依赖关系的状态机："
           "上一步不满足条件，下一步按钮置灰并说明缺什么。"
           "论文状态以 JSON 全量落盘，配合论文级写锁，"
           "避免并行写两节时后写者用旧快照覆盖先写者的内容。")

    h2(doc, "3.4 亮点三：上下文真实的建议引擎")
    p(doc, "建议生成时，服务端把「所在章节全文 + 选中文字 + 前文 + 后文 + 标题 / 主题 / 写作方向」"
           "一起交给模型，并附加硬性约束：建议必须落到选中文字的具体措辞上、"
           "依据必须引用前后文的具体问题、不得编造数据文献结论。")
    p(doc, "工程上还有一个关键细节：选到某节的第一句或最后一句时，本节内没有上文或下文。"
           "此时系统会**跨章节借用**上一章的结尾或下一章的开头作为语境；"
           "若确实位于全文首尾，则改从「开篇点题／收束全文」的角度提建议。")
    shot(doc, "09_selection_modify.png",
         "图 6　选区改写：左键选中正文任一段文字，自动给出带上下文依据的修改建议")

    h2(doc, "3.5 亮点四：FAN HUD —— 把「选方向」变成一次空间操作")
    p(doc, "传统做法是让用户从下拉框读一串文字选项，再凭记忆理解每个方向的含义。"
           "FAN HUD 把它改成扇形：候选分布在角度上，指针移动即刻在正文中预览该方向的实际写法，"
           "半径对应强度。空间位置承担了「类别」与「强度」两个维度。")
    shot(doc, "08_fanhud_direction.png",
         "图 7　FAN HUD 写作方向扇面：指针移动实时预览候选，右侧列表同步显示候选与依据")

    h2(doc, "3.6 亮点五：多源检索与引用回源")
    p(doc, "文献检索走 OpenAlex → Crossref → arXiv 阶梯降级，避免单一通道限流即失效；"
           "系统另维护章节↔文献的支撑关系表，"
           "在正文中高亮每条引用究竟由哪一节支撑。")
    shot(doc, "10_sources.png",
         "图 8　文献与数据查看器：文献 PDF、数据集与已下载三个页签，可就地打开原始依据")

    h2(doc, "3.7 工程实现难度与取舍")
    table(doc, ["难点", "做法", "代价与收益"], [
        ["长文上下文超限", "按「节」为最小生成单元，每节独立携带该节全文与邻章片段",
         "牺牲整篇一次性生成的流畅度，换来每节可独立重写与回溯"],
        ["生成中不能改论文", "生成结果走插槽机制，确认前只预览，确认后才写盘",
         "多一层交互，但杜绝了「生成中途手改被覆盖」"],
        ["模型建议不可信", "强制依据 + 禁止编造 + 建议可逐条选择应用",
         "建议数量变少，但每条都能落到具体措辞"],
        ["第三方接口不稳定", "知乎热榜、文献检索均做阶梯降级 + 失败可见",
         "拿不到时明确提示降级通道，而不是静默返回空"],
    ], widths=[3.0, 6.4, 7.2])

    pagebreak(doc)

    # ================= 四、未来规划 =================
    h1(doc, "四、", "未来规划与商业／社区价值")

    h2(doc, "4.1 迭代路线")
    table(doc, ["阶段", "时间", "重点", "交付"], [
        ["v1.0 路演版", "2026 Q4",
         "现有能力打磨 + 知乎热榜与 IP 素材完整接入 + 体验链接与演示视频",
         "可对外分发版本；论文模板库；一键导出投稿包"],
        ["v1.1 协作版", "2027 Q1",
         "多人协作编辑、版本历史、审稿意见回填工作流",
         "课题组内共享工作区；导师批注直接变成改写任务"],
        ["v1.2 生态版", "2027 Q2",
         "论文与数据集的社区化流通；知乎问题与学者的双向映射",
         "公开论文库；可复现数据包；知乎热榜问题追踪看板"],
        ["v2.0 平台版", "2027 H2",
         "多语言检索与写作、跨语种引用核验、领域知识图谱",
         "中英文学术写作；领域实体关系图谱；自动识别文献间矛盾"],
    ], widths=[2.4, 1.8, 6.4, 6.0])

    h2(doc, "4.2 商业价值")
    bullet(doc, "订阅制（按月/季）：覆盖学生与青年教师的高频写作场景；"
                "免费额度覆盖导入、浏览与基础审计。", head="商业模式：")
    bullet(doc, "高校院系与研究中心批量授权，把「论断回源」变成送审前的标准流程。",
           head="机构客户：")
    bullet(doc, "回归工具链与数据服务的增值订阅（FARS 已具备真实回归产物的组织能力）。",
           head="增值方向：")

    h2(doc, "4.3 社区价值")
    bullet(doc, "社区可沉淀「论文 + 数据 + 回归产物」的完整可复现包，"
                "让研究方法的复用不再停留在 PDF 里。", head="可复现性：")
    bullet(doc, "知乎热榜问题与论文的双向映射：读者点开一篇论文能看见它回应的是哪个真实议题；"
                "研究者从热榜出发能看见哪些问题尚无实证研究。", head="知识与探索：")
    bullet(doc, "公开的改写建议数据集（带上下文依据），"
                "既是社区的写作规范，也是后续做学术写作模型的高质量训练语料。",
           head="数据沉淀：")
    bullet(doc, "「有依据的建议」这一交互范式可以直接复用到其他专业写作工具，"
                "IP 陪伴体的状态映射设计也可作为独立组件开放。",
           head="范式外溢：")

    h2(doc, "4.4 对知乎社区的增量")
    table(doc, ["对知乎的价值", "具体体现"], [
        ["把热榜变成研究入口",
         "知乎热榜已接入 FARS 右栏，任何一个社会热议问题都可以一步进入实证研究路径"],
        ["激活优质问题内容",
         "被追问「有没有实证研究」的优质问题会获得真实研究回应，而不只是观点表达"],
        ["IP 场景延展",
         "刘看山素材在学术工具中的状态化使用，为 IP 官方素材库提供了新的使用范式与曝光场景"],
        ["沉淀学术讨论",
         "论文与讨论可回流知乎，让专业内容在站内形成闭环"],
    ], widths=[4.2, 12.4])

    pagebreak(doc)

    # ================= 五、Demo 方案 =================
    h1(doc, "五、", "可运行 Demo 方案（核心）")
    p(doc, "路演要求「必须展示真实可交互的软件原型或可运行体验链接，严禁只讲纯概念 PPT」。"
           "本项目提供三层保障，任一层失效都不会翻车。")

    h2(doc, "5.1 第一层：现场真机演示")
    numbered(doc, "①", "启动",
             "双击「FARS Dashboard.app」，约 12 秒启动后自动打开浏览器。")
    numbered(doc, "②", "演示动线（约 3 分钟）",
             "打开真实论文 → 正文八段结构 → FAN HUD 选方向 → "
             "左键选一段正文看带依据的改写建议 → 应用改写 → "
             "右栏看知乎热榜实时数据 → 从热榜一键开题 → 刘看山陪伴体全程在场。")
    numbered(doc, "③", "兜底",
             "现场断网时，热榜面板会显示降级提示，其余功能全部离线可用"
             "（已生成内容、审计、改写建议的界面逻辑都在本地）。")
    shot(doc, "01_workbench_empty.png",
         "图 9　启动后的首屏工作台（现场演示第一帧）")

    h2(doc, "5.2 第二层：高清录屏备选视频")
    p(doc, "已用自动化脚本在独立演示实例上录制完整演示动线，"
           "视频为 1440×900 / 25fps / H.264（MP4），时长 84 秒，大小 6.4 MB，"
           "覆盖从首屏到「从知乎选题建论文」的全流程，共十步。")
    table(doc, ["项", "实测值"], [
        ["文件名", "FARS 路演Demo演示视频.mp4"],
        ["分辨率 / 帧率", "1440 × 900 / 25 fps"],
        ["编码", "H.264（libx264, CRF 20, yuv420p, faststart）"],
        ["时长 / 大小", "84.32 秒 / 6.4 MB"],
        ["录制实例", "独立演示实例（端口 8807），与生产数据完全隔离"],
        ["覆盖动线", "首屏 → 打开论文 → 正文 → FAN HUD 方向扇面 → "
                     "选区改写建议 → 应用改写落盘 → 知乎热榜 → 刘看山陪伴体 → "
                     "知乎选题建论文 → 结果段"],
        ["校验方式", "抽帧 OCR 逐帧核对，确认界面为真实运行实例，"
                     "知乎热榜为实时抓取的真实数据"],
    ], widths=[4.0, 12.6])
    p(doc, "视频随本计划书一并交付，用于现场网络波动或接口限流时直接播放。")

    h2(doc, "5.3 第三层：可运行体验链接")
    p(doc, "已实测打通对外体验链接：演示实例通过隧道对外暴露，"
           "外部浏览器可直接打开完整交互界面（非截图、非录屏）。")
    table(doc, ["项", "实测值"], [
        ["体验链接", "https://unprocreant-janina-sideways.ngrok-free.dev"],
        ["落地实例", "独立演示实例（127.0.0.1:8807），只含演示用论文"],
        ["对外暴露内容", "FE111234 演示论文、DEMO01 演示数据、"
                         "从知乎热榜新建的选题实例"],
        ["数据隔离", "生产实例的 140 篇未发表论文**未**对外暴露，"
                     "两者状态目录、任务清单完全独立"],
        ["可用交互", "打开论文、FAN HUD 选方向、选区改写并落盘、"
                     "知乎热榜实时抓取、从热榜一键开题"],
        ["兜底顺序", "真机演示 → 体验链接 → 录屏视频（三层互为备份）"],
    ], widths=[4.0, 12.6])

    h2(doc, "5.4 公开演示网页（GitHub Pages）")
    p(doc, "产品已公开发布到 GitHub，并配套上线一个**无需安装、浏览器直达**的操作演示网页。"
           "评委、投资人、评委可自行打开查看真实界面截图与功能说明。")
    table(doc, ["项", "实测值"], [
        ["演示网页", "https://jackywyj.github.io/fars-dashboard/"],
        ["源码仓库", "https://github.com/JACKYwyj/fars-dashboard"],
        ["安装包发布", "https://github.com/JACKYwyj/fars-dashboard/releases/tag/v0.995"],
        ["网页内容", "核心能力 / 操作演示（真实运行截图）/ 八段结构 / "
                     "技术细节 / 下载入口"],
        ["截图来源", "均为**真实运行实例**，深色与日间双模式；"
                     "知乎热榜为实时抓取的真实数据"],
        ["部署方式", "GitHub Pages（随仓库 docs/ 目录自动部署），"
                     "CI 每次推送自动重建"],
        ["CI 状态", "安装验证 + 冒烟测试 + 密钥扫描 + 本机路径扫描，全绿"],
        ["安全边界", "仓库不含任何 API Key，也不含任何论文数据；"
                     "论文与 Key 只在本机"],
    ], widths=[4.0, 12.6])
    p(doc, "注意区分：GitHub Pages 演示网页是**静态截图站**，用于展示与传播；"
           "「可运行体验链接」是**真实可交互**的实例。二者互为补充。")

    h2(doc, "5.4 演示时的推荐顺序")
    table(doc, ["优先级", "动作", "时长", "看点"], [
        ["1", "现场打开真实论文并选中一段正文", "60 秒",
         "即时弹出带「依据」的改写建议——这是最直观的差异点"],
        ["2", "应用一条建议", "40 秒", "正文当场改变，落盘可查"],
        ["3", "展示可重现工作流四步", "50 秒", "「没有真实产物不许往下写」的机制"],
        ["4", "展示知乎热榜 → 一键开题", "40 秒", "知乎生态融合度"],
        ["5", "展示刘看山陪伴体状态切换", "20 秒", "IP 素材与状态机绑定"],
        ["备", "播放录屏视频", "3 分钟", "任何环节失灵时的完整保底"],
    ], widths=[1.4, 6.2, 1.8, 7.2])

    pagebreak(doc)

    # ================= 六、操作指南 =================
    h1(doc, "六、", "现场操作指南")

    h2(doc, "6.1 论文管理")
    numbered(doc, "①", "导入 / 新建",
             "顶栏「导入文件」支持 docx / pdf / md / txt，导入后自动解析为八段结构；"
             "也可直接把文件拖进窗口。")
    numbered(doc, "②", "切换论文", "点击左侧任务列表条目。每条目显示主题、期限、进度与状态。")
    numbered(doc, "③", "论文级操作",
             "右键条目打开上下文菜单：重命名、导出 PDF、导出 Word、删除等。")
    shot(doc, "06_task_context_menu.png",
         "图 10　左侧任务列表的右键菜单")

    h2(doc, "6.2 正文写作")
    numbered(doc, "①", "按节组织",
             "固定八节：摘要 → 引言 → 文献综述 → 数据与变量 → 研究设计 → "
             "实证结果 → 稳健性检验 → 结论。每节独立状态与审计徽章。")
    numbered(doc, "②", "生成某节",
             "点该节的「生成」按钮，或把光球拖到该节位置；「钉住」可把结果固定到右栏。")
    numbered(doc, "③", "编辑全文",
             "顶栏「编辑全文」进入全文编辑态；⌘/Ctrl + S 保存。")
    shot(doc, "03_manuscript.png",
         "图 11　正文编辑区：每节标题栏含状态、审计徽章与四个操作按钮")

    h2(doc, "6.3 FAN HUD 与选区改写")
    numbered(doc, "①", "打开方向扇面",
             "顶栏「写作方向」按钮，或双击光球并按住 0.5 秒。")
    numbered(doc, "②", "浏览与确认",
             "指针在扇面内移动即时预览；Shift + ↑↓ 调方向强度；点击或 Enter 确认；Esc 取消。")
    numbered(doc, "③", "选区改写",
             "在正文里用**左键**选中一段文字 → 松开即自动弹出建议面板 → "
             "移动指针预览 → 点「按此建议改写选中的文字」或按 Enter 应用。")
    code_block(doc, [
        "选区改写：正文区 → 左键选中 → 松开 → 选建议 → Enter 应用",
        "方向扇面：顶栏「写作方向」→ 指针移动预览 → 点击确认",
        "保存全文：⌘/Ctrl + S　　撤销：⌘/Ctrl + Z",
    ])

    h2(doc, "6.4 知乎热榜与陪伴体")
    numbered(doc, "①", "看热榜",
             "右栏「知乎热榜选题」分区，延后加载以不抢首屏；点「刷新」可手动重取。")
    numbered(doc, "②", "开题",
             "点任一问题 → 选研究主题 → 「创建并打开」。"
             "系统建论文骨架、写入 origin 来源并自动切换过去。")
    numbered(doc, "③", "陪伴体",
             "贴在光球旁。悬停它会打招呼；90 秒无操作会睡着，"
             "任何交互都会唤醒并切回对应状态。")
    shot(doc, "05_spec_editor.png",
         "图 12　规格编辑器：方向强度、语气与笔法旋钮，改动即时作用于预览")
    shot(doc, "07_settings.png",
         "图 13　设置面板：模型选择、API 接入、端口与生成参数")

    pagebreak(doc)

    # ================= 七、风险与预案 =================
    h1(doc, "七、", "现场风险与预案")
    table(doc, ["风险", "影响", "预案"], [
        ["现场断网 / 网络抖动", "知乎热榜取不到、模型调用失败",
         "已录制完整视频直接播放；其余功能离线可用；热榜面板会明确提示降级原因"],
        ["模型接口限流", "生成与改写建议变慢或失败",
         "现场只演示已生成好的真实论文；改写建议可提前预热"],
        ["选区改写返回慢", "演示节奏被打断",
         "提前在同一条正文上跑一次，走缓存；或直接演示已应用后的结果"],
        ["误操作改坏论文", "现场演示翻车",
         "演示用独立实例与独立数据目录；一键重置；生产数据不被触碰"],
        ["评委追问技术细节", "回答仓促",
         "备齐：架构分层表、状态机说明、审计检测项清单、知乎接口认证实测结论"],
    ], widths=[3.4, 5.0, 8.2])

    h2(doc, "7.1 可能被追问的技术问题（备答要点）")
    bullet(doc, "知乎热榜是否稳定可用？—— 主端点 hot-list-web 公开可取（200），"
                "主接口 hot-lists/total 需登录态（401），因此做阶梯降级并暴露降级状态。",
           head="接口稳定性：")
    bullet(doc, "AI 建议会不会编造？—— 系统提示词硬性禁止编造数据文献结论；"
                "每条建议必须引用前后文具体措辞；最终由完整性审计机械对账兜底。",
           head="幻觉问题：")
    bullet(doc, "离线能不能用？—— 状态、审计、改写界面逻辑全在本地；"
                "仅模型调用与热榜抓取需要网络。密钥存系统钥匙串，不进分发包。",
           head="隐私与离线：")
    bullet(doc, "为什么用原生技术栈？—— 前端 1.2 万行原生 JS + 手写 SVG，"
                "FAN HUD 的扇面几何是自研算法；后端只用标准库。"
                "好处是打包体积小、启动快、无供应链风险。",
           head="技术选型：")

    doc.add_paragraph()
    p(doc, "文档结束。所有截图与数据均取自运行中的生产实例，操作路径可当场复现。",
      9, INK4, italic=True, before=14)

    doc.save(OUT)
    print("✅ 已生成:", OUT)
    print("   大小: %.1f MB" % (os.path.getsize(OUT) / 1024 / 1024))
    print("   段落:", len(doc.paragraphs), " 表格:", len(doc.tables),
          " 图片:", len(doc.inline_shapes))


if __name__ == "__main__":
    build()
