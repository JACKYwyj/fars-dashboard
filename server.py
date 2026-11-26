#!/usr/bin/env python3
"""FARS 可视化看板。

数据全部来自真实产物，不臆造：
  - ~/Desktop/FARS_Research/_进度追踪.json      论文任务状态
  - ~/Desktop/FARS_Research/_已完成.txt          已完成编号
  - ~/Desktop/FARS_Research/_PDF校验清单.csv     引用 PDF 校验
  - ~/Desktop/FARS_Research/_引用核验总报告.md   引用核验统计
  - notes/fars/FARS_伪实证缺陷清单_*.csv         历史缺陷（逐项真实数据）
  - skills/fars-paper/scripts/build_paper.py     design_spec() —— 实证规格来源

「下一步思考方向」= steering：对某 FE 编号的实证规格做覆盖，
写入 ~/Desktop/FARS_Research/_steering.json，由 build_paper.py 读取后真实生效。
"""

import ipaddress
import json
import os
import numpy as np
import hashlib
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import base64
import audit as audit_mod
try:
    import sources as sources_mod
except Exception:      # pragma: no cover
    sources_mod = None
import filemgr as filemgr_mod
import diaglog as diaglog_mod

def app_root():
    """应用根目录（找 index.html 等静态资源）。

    源码运行：server.py 所在目录。
    PyInstaller 打包：sys._MEIPASS（解包资源目录）；
    macOS .app：Contents/Resources。
    """
    cands = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        cands.append(meipass)
        cands.append(os.path.join(meipass, "..", "Resources"))
    try:
        cands.append(os.path.dirname(os.path.abspath(__file__)))
        # .app：Contents/MacOS/<bin> → 上两级是 Contents → Resources
        cands.append(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "Resources"))
    except Exception:
        pass
    return cands


def find_asset(name, want_dir=False):
    """在候选目录里找资源，返回绝对路径或 None。

    want_dir=True 时找目录。
    ⚠️ 打包后 Contents/Frameworks/index.html 是**符号链接**指向 ../Resources/，
       os.path.isfile 对符号链接指向的目录返回 False → 目录必须用 isdir 判断，
       且要**跟着链接走**（realpath），否则后面 listdir 会拿到空。
    """
    for d in app_root():
        p = os.path.normpath(os.path.join(d, name))
        if want_dir:
            if os.path.isdir(p):
                return os.path.realpath(p)
        elif os.path.isfile(p):
            return p
    return None


def _ensure_state_dir():
    """状态目录自建（打包后首次运行没有这个目录）。"""
    try:
        os.makedirs(MIRROR, exist_ok=True)
        os.makedirs(os.path.join(MIRROR, "papers"), exist_ok=True)
        os.makedirs(os.path.join(MIRROR, "imports"), exist_ok=True)
    except Exception:
        pass


def _seed_samples_once():
    """打包版首次运行：把示例论文复制进状态目录。

    只在「状态目录里一篇论文都没有」时执行一次。
    示例数据全为虚构（见 samples/README.md）。
    """
    try:
        sample_dir = find_asset("samples", want_dir=True)
        if not sample_dir or not os.path.isdir(sample_dir):
            return

        # ---- 合成演示面板 ----
        # 没有它，打包版的「导入 docx → 新论文 → 回归预览」会直接报错
        # （面板缓存缺失 / 没有可用样本）→ 用户看到的是"导入不成功"。
        # ⚠️ 合成面板是**假数据**（make_demo_panel.py 生成），只验证流程通不通，
        #    不代表任何真实回归结果。
        #
        # ---- 面板 seeding 必须**独立于**论文数量 ----
        # 原来面板逻辑挂在「一篇论文都没有才执行」里 → 已经导入过论文的用户
        # （比如已 seeded DEMO01）永远拿不到面板 → 导入后跳过去一屏报错。
        for fn in ("panel.npz", "panel_meta.json", "px.npz", "px_meta.json",
                   "esg.csv"):
            src = os.path.join(sample_dir, fn)
            dst = os.path.join(MIRROR, fn)
            if os.path.isfile(src) and not os.path.isfile(dst):
                try:
                    with open(src, "rb") as a, open(dst, "wb") as b:
                        b.write(a.read())
                except Exception:
                    pass

        papers_dir = os.path.join(MIRROR, "papers")
        if os.path.isdir(papers_dir):
            existing = [f for f in os.listdir(papers_dir) if f.endswith(".json")]
            if existing:
                return          # 已有论文 → 不再塞示例论文
        # papers/
        sp = os.path.join(sample_dir, "papers")
        if os.path.isdir(sp):
            os.makedirs(papers_dir, exist_ok=True)
            for f in os.listdir(sp):
                if f.endswith(".json"):
                    dst = os.path.join(papers_dir, f)
                    if not os.path.isfile(dst):
                        with open(os.path.join(sp, f), "rb") as a, open(dst, "wb") as b:
                            b.write(a.read())
        # 追踪表
        st = os.path.join(sample_dir, "_进度追踪.json")
        dst_tr = os.path.join(MIRROR, "_进度追踪.json")
        if os.path.isfile(st) and not os.path.isfile(dst_tr):
            with open(st, "rb") as a, open(dst_tr, "wb") as b:
                b.write(a.read())
    except Exception:
        pass


HOST = "127.0.0.1"
# 隔离测试实例用 FARS_PORT 换端口（生产 8790，隔离 8791）
PORT = int(os.environ.get("FARS_PORT") or 8790)

BASE = os.path.expanduser("~/Desktop/FARS_Research")
WORKSPACE = os.path.expanduser("~/.openclaw/workspace")
# 看板由 launchd 常驻，macOS TCC 不允许它直接读 ~/Desktop。
# 因此用镜像目录：用户级同步进程把 FARS 状态复制到 MIRROR，看板只读镜像。
#
# 隔离测试：FARS_STATE_DIR 可覆盖状态根目录 → 浏览器交互测试可以指向
# 独立副本，完全不碰生产论文数据（实测踩过两次：插入模式污染了真实论文的 intro）。
_MIRROR_DEFAULT = os.path.join(WORKSPACE, "tools", "fars-dashboard", "state")

# 状态目录三级优先：
#   1. FARS_STATE_DIR 环境变量（隔离测试 / 冒烟测试用）
#   2. **打包成 .app** → ~/Library/Application Support/FARS Dashboard/
#      （不能指向 ~/.openclaw/workspace —— 别人机器上根本没这个目录，
#        实测踩过：没改这行时打包版会读到本机的生产论文数据）
#   3. 源码本地开发 → workspace 里的 state/
_MEIPASS = getattr(sys, "_MEIPASS", None)
if os.environ.get("FARS_STATE_DIR"):
    MIRROR = os.environ["FARS_STATE_DIR"]
elif _MEIPASS:
    MIRROR = os.path.join(
        os.path.expanduser("~/Library/Application Support"), "FARS Dashboard")
else:
    MIRROR = _MIRROR_DEFAULT
ISOLATED = bool(os.environ.get("FARS_STATE_DIR"))

# ---------------- 内测日志系统（2026-11-23）----------------
# 目的：内测用户遇到 bug 时，能一键导出日志发回来，我这边就能复现定位。
# 日志目录：状态目录下的 logs/（打包版 = ~/Library/Application Support/FARS Dashboard/logs/）
LOG_DIR = os.path.join(MIRROR, "logs")
DIAG = diaglog_mod.DiagLog(LOG_DIR)

# 只读模式：禁止写入论文实体。
# 用它做「在生产实例上验证生成链路」这类回归测试 ——
# 实测踩过：直接对生产跑 /stream 回归，把 results 节真的改掉了。
READONLY = bool(os.environ.get("FARS_READONLY"))
NOTES_FARS = os.path.join(WORKSPACE, "notes", "fars")
FARS_SCRIPTS = os.path.join(WORKSPACE, "skills", "fars-paper", "scripts")
BUILD_PAPER = os.path.join(FARS_SCRIPTS, "build_paper.py")
# 镜像优先，缺失时回退到 Desktop 原件。
# ⚠️ 打包成 .app 后**不**回退 Desktop：
#   实测踩过 —— 冒烟测试时打包版读到了 ~/Desktop/FARS_Research 的生产论文数据，
#   对着「发给别人测试」的包来说这是数据泄漏（而且是别人的机器上根本不该有的路径）。
_PACKAGED = bool(getattr(sys, "_MEIPASS", None))


def pick(name):
    m = os.path.join(MIRROR, name)
    if os.path.isfile(m):
        return m
    if _PACKAGED:
        # 打包版：只用自己的状态目录，没有就当没有（前端显示空列表）
        return m
    return os.path.join(BASE, name)


TRACK_FILE = pick("_进度追踪.json")
DONE_FILE = pick("_已完成.txt")
PDF_CHECK = pick("_PDF校验清单.csv")
CITE_REPORT = pick("_引用核验总报告.md")
# steering 单一副本放 workspace：看板（launchd）与 build_paper.py（用户 shell）都能读写
STEER_FILE = os.path.join(MIRROR, "steering.json")

PREVIEW_T0 = None
PROXY_URL = "http://127.0.0.1:8789/bili/https://api.minimaxi.com/anthropic/v1/messages"
MODEL = "MiniMax-M2.7"

HORIZONS = [1, 3, 6, 12]
FACTORS = ["mom12", "rev1", "vol36", "turnover", "size_proxy"]
WINDOW_LENS = [None, 240, 180, 120, 60]


# ---------------------------------------------------------------- helpers
def ro(path):
    """读文本文件。

    ⚠️ 必须吞掉 PermissionError —— 这是**打包版的真实崩溃点**（实测 2026-11-24）：
       macOS TCC 不允许 launchd 起的进程读 ~/Desktop，
       而 pick() 在非打包模式下会回退到 ~/Desktop/FARS_Research
       → open() 抛 PermissionError → /api/state 整个 500，
          左侧论文列表直接空白（不是"没数据"，是"崩了"）。
       打包版在别人机器上同样会炸（他们 Desktop 权限更严）。
    读不到就当"没有这个文件"，降级继续跑，不能让整个面板挂掉。
    """
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except PermissionError:
        return ""
    except OSError:
        return ""


# ============================================================
# FARS 数据与文献获取层（2026-11-26 新增）
# ------------------------------------------------------------
# · FRED（免 key）—— 宏观/金融序列，可直接喂回归
# · Crossref —— 引用 DOI 级权威核验
# · OpenAlex —— 文献检索 + 被引数 + 开放获取 PDF 链接
# · arXiv —— 预印本
# 设计原则见 fars_data.py 顶部：一切标 source，失败如实报错，不编造。
# ============================================================
try:
    import fars_data as _fdata
except Exception:                     # 打包环境路径问题 → 降级但不崩
    _fdata = None


def load_json(path, default):
    try:
        return json.loads(ro(path))
    except Exception:
        return default


def load_csv(path):
    """读 CSV。

    ⚠️ 同样要吞 PermissionError（见 ro() 的注释）：
       pick() 会回退到 ~/Desktop/FARS_Research，
       macOS TCC 禁止 launchd 进程读 Desktop → 直接 500。
    """
    import csv
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as f:
            return list(csv.DictReader(f))
    except (PermissionError, OSError):
        return []


def defect_csv():
    if not os.path.isdir(NOTES_FARS):
        return None
    cands = [f for f in os.listdir(NOTES_FARS) if f.startswith("FARS_") and f.endswith(".csv")]
    if not cands:
        return None
    cands.sort()
    return os.path.join(NOTES_FARS, cands[-1])


# ---------------------------------------------------------------- spec
# 主题注册表：已知主题有专属因子集与编号偏移；**未知主题不报错**，
# 而是按名称哈希确定性回退到一套基准因子 —— 主人要求「任何文章都能跑通」，
# 不能因为主题名不在字典里就 500。
TOPIC_OFFSETS = {"Stock": 0, "Machine": 7919, "ESG": 15838}
TOPIC_FACTORS = {
    "Stock": ["mom12", "rev1", "vol36", "turnover"],
    "Machine": ["mom12", "rev1", "vol36", "turnover", "size_proxy"],
    "ESG": ["mom12", "vol36", "turnover", "size_proxy"],
}
# 未知主题的基准因子集（最通用的一组：A 股面板上普遍可估）
DEFAULT_FACTORS = ["mom12", "rev1", "vol36", "turnover"]


def topic_offset(topic):
    if topic in TOPIC_OFFSETS:
        return TOPIC_OFFSETS[topic]
    # 确定性回退：同一 topic 名永远得到同一偏移（哈希固定种子）
    h = int(hashlib.md5((topic or "").encode("utf-8")).hexdigest()[:8], 16)
    return h % 15838


def topic_factors(topic):
    if topic in TOPIC_FACTORS:
        return list(TOPIC_FACTORS[topic])
    return list(DEFAULT_FACTORS)


def design_spec(code, topic):
    """与 build_paper.py 完全一致的规格推导（同一份逻辑，避免看板与生成器漂移）。"""
    digits = re.sub(r"\D", "", code) or "0"
    topic_off = topic_offset(topic)
    n = int(digits) + topic_off

    horizon = HORIZONS[(n * 3 + 1) % 4]

    base = topic_factors(topic)
    without = lambda k: [x for x in base if x != k]
    variants = [
        base,
        without("turnover"),
        without("vol36"),
        without("rev1") or without("mom12"),
        without("size_proxy") or base,
        without("mom12") or base,
        without("turnover") + ["size_proxy"] if "size_proxy" not in base else without("turnover"),
        base + (["rev1"] if "rev1" not in base else []),
    ]
    xs = list(variants[(n * 5 + 2) % len(variants)])
    wlen = WINDOW_LENS[(n // 7) % len(WINDOW_LENS)]
    q = 0.30 + ((n * 37) % 51) / 100.0
    rob = (n // 13) % 3
    return {"horizon": horizon, "xs": xs, "wlen": wlen, "split_q": q,
            "rob": rob, "topic": topic, "code": code}


def spec_key(code, topic):
    return "%s|%s" % (code, topic)


def load_steering():
    return load_json(STEER_FILE, {})


def validate_steering(topic, patch):
    """只接受生成器真实支持的字段与取值，非法即拒。"""
    base_factors = topic_factors(topic)
    out = {}
    if "horizon" in patch:
        h = int(patch["horizon"])
        if h not in HORIZONS:
            raise ValueError("预测期只能是 %s" % HORIZONS)
        out["horizon"] = h
    if "xs" in patch:
        xs = [x for x in patch["xs"] if x in FACTORS]
        if not xs:
            raise ValueError("至少保留一个有效因子")
        # ESG 主题不含 rev1（build_paper 中 ESG 的 BASE_FACTORS 无 rev1）
        out["xs"] = [x for x in xs if x in base_factors]
        if not out["xs"]:
            raise ValueError("该主题无可用因子")
    if "wlen" in patch:
        w = patch["wlen"]
        w = None if w in (None, "", "null", "None") else int(w)
        if w not in WINDOW_LENS:
            raise ValueError("样本窗口只能是 %s（None=全样本）" % WINDOW_LENS)
        out["wlen"] = w
    if "split_q" in patch:
        q = float(patch["split_q"])
        if not (0.0 < q < 1.0):
            raise ValueError("波动率分位点必须在 (0,1)")
        out["split_q"] = q
    if "note" in patch:
        out["note"] = str(patch["note"])[:500]
    return out


def effective_spec(code, topic, steering=None):
    """基础规格 + steering 覆盖 = 实际会跑进回归的规格。"""
    steering = load_steering() if steering is None else steering
    base = design_spec(code, topic)
    over = steering.get(spec_key(code, topic)) or {}
    eff = dict(base)
    eff["steering"] = dict(over)
    for k in ("horizon", "xs", "wlen", "split_q"):
        if k in over:
            eff[k] = over[k]
    eff["window_desc"] = ("全样本区间" if eff["wlen"] is None
                          else "%d 个月窗口（起点由编号决定）" % eff["wlen"])
    eff["split_desc"] = "市场波动率 %d%% 分位" % round(eff["split_q"] * 100)
    eff["overridden"] = sorted(k for k in ("horizon", "xs", "wlen", "split_q") if k in over)
    return eff



# ================================================================
# 实时预览引擎（IDE 核心）
# ================================================================
# 面板数据来自 bake_panel.py 烘焙的缓存（launchd 读不了 ~/Desktop）。
# 回归用 FWL（Frisch-Waugh-Lovell）变换：组内去均值 = 固定效应，
# 避免生成数千个虚拟变量，184 万行也能秒级出结果。
# 算法与 build_paper.py 的 panel_regress 完全一致。

PANEL_NPZ = os.path.join(MIRROR, "panel.npz")
PANEL_META = os.path.join(MIRROR, "panel_meta.json")
PX_NPZ = os.path.join(MIRROR, "px.npz")
PX_META = os.path.join(MIRROR, "px_meta.json")
ESG_CSV = os.path.join(MIRROR, "esg.csv")

_CACHE = {}


def _load_panel():
    """载入并缓存面板。"""
    if "panel" in _CACHE:
        return _CACHE["panel"]
    if not os.path.isfile(PANEL_NPZ) or not os.path.isfile(PANEL_META):
        raise RuntimeError("面板缓存缺失，请先跑 bake_panel.py")
    d = np.load(PANEL_NPZ)
    meta = json.load(open(PANEL_META, encoding="utf-8"))
    panel = {
        "month_index": d["month_index"].astype(np.int32),
        "code_index": d["code_index"].astype(np.int32),
        "months": meta["months"],
        "codes": meta["codes"],
    }
    for f in ["mom12", "rev1", "vol36", "turnover", "size_proxy", "mkt_vol"]:
        if f in d:
            panel[f] = d[f].astype(np.float64)
    _CACHE["panel"] = panel
    return panel


def _load_px():
    if "px" in _CACHE:
        return _CACHE["px"]
    if not os.path.isfile(PX_NPZ):
        raise RuntimeError("价格缓存缺失，请先跑 bake_panel.py")
    d = np.load(PX_NPZ)
    meta = json.load(open(PX_META, encoding="utf-8"))
    _CACHE["px"] = {"values": d["values"], "months": meta["months"],
                    "codes": meta["codes"]}
    return _CACHE["px"]


def _load_esg():
    """ESG 评分：股票代码 → 评分。"""
    if "esg" in _CACHE:
        return _CACHE["esg"]
    out = {}
    if os.path.isfile(ESG_CSV):
        import csv
        with open(ESG_CSV, encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                try:
                    out[str(row["股票代码"]).zfill(6)] = float(row["ESG评分"])
                except Exception:
                    pass
    _CACHE["esg"] = out
    return out


def _fwd_return(H):
    """未来 H 个月累计收益。

    生成器：px.pct_change(H, fill_method=None).shift(-H)
    → fwd[t] = px[t+H] / px[t] - 1   （未来收益）
    注意：之前写成 v/roll(v,-H) 是「过去收益」，符号完全相反，会让
    mom12 系数符号颠倒（+0.042 vs 生成器 -0.033）。这里严格按生成器公式。
    """
    px = _load_px()
    v = px["values"]
    with np.errstate(all="ignore"):
        fwd = np.roll(v, -H, axis=0) / v - 1.0
    fwd[-H:] = np.nan          # 最后 H 个月没有前瞻收益
    return fwd


def _window_mask(month_index, months, wlen, code_digits):
    """按 build_paper.apply_window 的同一规则返回 (掩码, 描述)。

    样本不足（月份<36 或 个股<100）时回退全样本并如实标注 —— 与生成器一致。
    """
    n = int(code_digits or 0)
    n_months = len(months)
    if wlen is None:
        return np.ones(len(month_index), dtype=bool), "全样本区间"
    max_start = max(0, n_months - wlen)
    start_idx = (n * 104729) % (max_start + 1)
    end_idx = min(n_months - 1, start_idx + wlen - 1)
    mask = (month_index >= start_idx) & (month_index <= end_idx)
    n_months_sel = int(np.unique(month_index[mask]).size) if mask.any() else 0
    n_codes_sel = int(np.unique(month_index[mask]).size) if False else 0
    desc = "%s 至 %s（%d 个月窗口）" % (months[start_idx], months[end_idx], wlen)
    if n_months_sel < 36:
        return (np.ones(len(month_index), dtype=bool),
                "全样本区间（窗口规格 %s 个月 / %s 起始 因样本不足回退）" % (wlen, months[start_idx]))
    return mask, desc


def _fwl_regress(y, xs, entity, month, names=None):
    """FWL 固定效应回归 + 聚类标准误。

    与 build_paper.panel_regress(fe="entity") 同一算法：
    组内去均值 = 固定效应的等价变换，避免数千个虚拟变量。
    """
    import pandas as pd
    import statsmodels.formula.api as smf

    df = pd.DataFrame({"y": y, "entity": entity, "month": month})
    for i, x in enumerate(xs):
        df["x%d" % i] = x
    df = df.replace([np.inf, -np.inf], np.nan).dropna()
    if len(df) < 100 or df["entity"].nunique() < 20:
        return {"ok": False, "error": "样本不足（N=%d，个体 %d）" % (
            len(df), int(df["entity"].nunique()) if len(df) else 0)}

    group = df["entity"]
    kept, dropped = [], []
    labels = names if names else ["x%d" % i for i in range(len(xs))]
    for i, name in enumerate(labels):
        col = "x%d" % i
        g = df[col] - df.groupby(group)[col].transform("mean")
        if not np.isfinite(g).all() or float(np.nanstd(g)) < 1e-10:
            dropped.append(name)
        else:
            kept.append((name, col))
    if not kept:
        return {"ok": False, "error": "所有变量在固定效应下无变异，无法识别"}

    for col in ["y"] + [c for _, c in kept]:
        df[col] = df[col] - df.groupby(group)[col].transform("mean")

    rhs = " + ".join(c for _, c in kept)
    try:
        res = smf.ols("y ~ " + rhs, data=df).fit(
            cov_type="cluster", cov_kwds={"groups": df["entity"]})
    except Exception as e:
        return {"ok": False, "error": "回归失败: %s" % e}

    coefs = []
    for name, col in kept:
        try:
            coefs.append({
                "factor": str(name),
                "beta": float(res.params[col]),
                "se": float(res.bse[col]),
                "t": float(res.tvalues[col]),
                "p": float(res.pvalues[col]),
                "sig": bool(res.pvalues[col] < 0.05),
            })
        except Exception:
            pass

    return {
        "ok": True,
        "coefs": coefs,
        "dropped": dropped,
        "n": int(len(df)),
        "nClusters": int(df["entity"].nunique()),
        "r2": float(res.rsquared),
        "nMonths": int(df["month"].nunique()),
        "span": [str(df["month"].min()), str(df["month"].max())],
    }


def preview_regression(code, topic, horizon, xs, wlen, split_q=None, use_esg=None):
    """实时预览：按给定规格跑一遍回归，返回真实结果。

    执行顺序严格复刻 build_paper.py：
      1. 面板（已按五因子键对齐 = 生成器的 inner merge，1841120 行）
      2. 前瞻收益 inner merge（键对齐，NaN 留给后面 dropna）
      3. apply_window 裁样本窗口
      4. 因子按「年月」截面 1% 去极值
      5. ESG 评分 inner merge（只保留有 ESG 的股票）+ 按年月去极值
      6. panel_regress：只对 [y] + xs 做 dropna，再做个体固定效应
    任何一步顺序偏差都会让 N / 系数和生成器对不上。
    """
    panel = _load_panel()
    months = panel["months"]
    codes = panel["codes"]
    mi = panel["month_index"]
    ci = panel["code_index"]

    H = int(horizon)
    fwd = _fwd_return(H)
    y = fwd[mi, ci]

    # ESG 主题默认带 ESG（生成器就是这样：xs = 规格因子 + ESG评分）
    if use_esg is None:
        use_esg = (topic or "").upper() == "ESG"

    # ---------- 3. 样本窗口（在去极值之前，与生成器一致）----------
    digits = re.sub(r"\D", "", code) or "0"
    wmask, wdesc = _window_mask(mi, months, wlen, digits)

    # ---------- 4. 因子按年月截面 1% 去极值 ----------
    names = []
    xs_cols = []
    for f in xs:
        if f not in panel:
            return {"ok": False, "error": "未知因子 %s" % f}
        names.append(f)
        xs_cols.append(panel[f].copy())
    if use_esg:
        esg = _load_esg()
        if not esg:
            return {"ok": False, "error": "ESG 数据为空"}
        esg_map = esg
        names.append("ESG评分")
        xs_cols.append(np.array([esg_map.get(c, np.nan) for c in codes])[ci])

    # 去极值：按年月截面 1% 双侧（与 build_paper 的 winsorize 同口径）。
    # 用 pandas groupby transform 做向量化 —— 逐月 Python 循环会拖到 1.7s。
    import pandas as _pd
    _mi_all = mi
    for j in range(len(xs_cols)):
        col = xs_cols[j]
        _df = _pd.DataFrame({"m": _mi_all, "v": col})
        _g = _df.groupby("m")["v"]
        _lo = _g.transform(lambda x: x.quantile(0.01))
        _hi = _g.transform(lambda x: x.quantile(0.99))
        w = np.clip(col, _lo.to_numpy(), _hi.to_numpy())
        w[~wmask] = np.nan
        xs_cols[j] = w

    # ---------- 5. ESG inner merge（只保留有 ESG 评分的股票）----------
    if use_esg:
        esg_vals = xs_cols[-1]
        has_esg = np.isfinite(esg_vals)
        wmask = wmask & has_esg
        esg_vals = np.where(wmask, esg_vals, np.nan)
        xs_cols[-1] = esg_vals

    # ---------- 6. dropna：只对 y + xs（生成器口径，不是全表）----------
    mask = wmask & np.isfinite(y)
    for c in xs_cols:
        mask &= np.isfinite(c)

    if not mask.any():
        return {"ok": False, "error": "当前规格下没有可用样本"}

    mi_s = mi[mask]
    ci_s = ci[mask]
    y_s = y[mask]
    xs_s = [c[mask] for c in xs_cols]

    n_months_sel = int(np.unique(mi_s).size)
    n_codes_sel = int(np.unique(ci_s).size)
    span_months = [months[int(mi_s.min())][:7], months[int(mi_s.max())][:7]]

    res = _fwl_regress(y_s, xs_s, ci_s, mi_s, names)

    # ---------- 波动率分位切分（阈值按窗口内样本计算，与生成器一致）----------
    split_desc = ""
    split_stats = []
    if split_q is not None and "mkt_vol" in panel:
        mv = panel["mkt_vol"][mask]
        thr = float(np.nanquantile(mv, float(split_q)))
        split_desc = "市场波动率 %d%% 分位（阈值 %.4f）" % (round(float(split_q) * 100), thr)
        hi = mask & (panel["mkt_vol"] > thr)
        lo = mask & (panel["mkt_vol"] <= thr)
        for label, m in (("高波动期", hi), ("低波动期", lo)):
            if not m.any():
                continue
            r = _fwl_regress(y[m], [c[m] for c in xs_cols], ci[m], mi[m], names)
            split_stats.append({"name": label, "n": int(m.sum()),
                                "r2": r.get("r2"), "ok": r.get("ok", False)})

    out = {
        "ok": bool(res.get("ok")),
        "horizon": H,
        "factors": names,
        "windowDesc": wdesc,
        "nMonths": n_months_sel,
        "nCodes": n_codes_sel,
        "splitDesc": split_desc,
        "splitStats": split_stats,
        "esgUsed": bool(use_esg),
        "spanMonth": span_months,
    }
    out.update({k: v for k, v in res.items() if k != "ok"})
    if not out.get("ok"):
        out["error"] = res.get("error", "未知错误")
    return out


# ---------------------------------------------------------------- state
def project_phase(proj_dir):
    """按真实产物判断单篇当前阶段。"""
    stages = [
        ("文献", os.path.isdir(os.path.join(proj_dir, "01_文献"))),
        ("数据", os.path.isfile(os.path.join(proj_dir, "02_数据", "月末复权价_宽表.csv"))),
        ("因子", os.path.isfile(os.path.join(proj_dir, "02_数据", "因子_mom12_宽表.csv"))),
        ("回归", os.path.isfile(os.path.join(proj_dir, "04_分析", "回归结果表.csv"))),
        ("论文", os.path.isdir(os.path.join(proj_dir, "05_论文")) and
                 any(f.endswith("_完整论文.docx") for f in os.listdir(os.path.join(proj_dir, "05_论文")))
                 if os.path.isdir(os.path.join(proj_dir, "05_论文")) else False),
    ]
    done = [n for n, ok in stages if ok]
    if not done:
        return {"stage": "待生成", "done": 0, "total": len(stages)}
    return {"stage": done[-1], "done": len(done), "total": len(stages)}


def _paper_summary(code, topic):
    """一篇论文的真实摘要（给左栏任务卡用）。

    只读磁盘上的论文 JSON，取**真实计数**：
      parts = 章节数（其中 filled = 真有正文的节数）
      refs  = 参考文献条数
      tables/figures = 真实表/图数量
      audit = 审计发现条数（缺陷数）

    ⚠️ 文件不存在/读失败 → 返回 None（前端显示「—」），**绝不编数字**。
    ⚠️ 不调 load_paper —— 那会为不存在的论文**创建**文件（副作用）。
    """
    if not code or not topic:
        return None
    try:
        p = _paper_path(code, topic)
        if not os.path.isfile(p):
            return None
        with open(p, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return None
    parts = d.get("parts") or {}
    filled = 0
    for _k, v in parts.items():
        if not isinstance(v, dict):
            continue
        txt = v.get("text") or v.get("full") or ""
        if txt and len(txt.strip()) > 20:
            filled += 1
    aud = d.get("audit") or {}
    findings = aud.get("findings") if isinstance(aud, dict) else None
    return {
        "parts": len(parts),
        "filled": filled,
        "refs": len(d.get("refs") or []),
        "tables": len(d.get("tables") or []),
        "figures": len(d.get("figures") or []),
        "findings": len(findings or []),
        "updatedAt": d.get("updatedAt") or 0,
    }


def collect_state():
    out = {"generatedAt": int(__import__("time").time() * 1000)}

    track = load_json(TRACK_FILE, [])
    done_codes = set()
    try:
        if os.path.isfile(DONE_FILE):
            done_codes = set(x.strip() for x in ro(DONE_FILE).splitlines() if x.strip())
    except Exception:
        done_codes = set()

    tasks = []
    for e in track:
        code = e.get("code")
        topic = e.get("topic")
        if not code or not topic:
            continue
        proj = os.path.join(BASE, e.get("dir") or "")
        ph = project_phase(proj)
        steer = load_steering().get(spec_key(code, topic))
        tasks.append({
            "code": code,
            "topic": topic,
            "title": e.get("title"),
            "dir": e.get("dir"),
            "dirExists": os.path.isdir(proj),
            "status": e.get("status"),
            "note": e.get("note"),
            "paper": e.get("paper"),
            "inDoneList": code in done_codes,
            "phase": ph,
            "steering": steer,
            "spec": effective_spec(code, topic),
        })
    # ---- 导入的论文并进左侧文件列表 ----
    # 它们不在 _进度追踪.json 里（那个表是单向同步的，写进去会被覆盖），
    # 所以从 filemeta 注册表读，单独标注 origin="import"。
    fm = load_filemeta()
    deleted = set(fm.get("deleted") or [])
    for e in fm.get("imports") or []:
        code, topic = e.get("code"), e.get("topic")
        if not code or not topic:
            continue
        if fm_key(code, topic) in deleted:
            continue
        if any(t["code"] == code and t["topic"] == topic for t in tasks):
            continue
        tasks.append({
            "code": code,
            "topic": topic,
            "title": e.get("title") or _paper_title_on_disk(code, topic),
            "dir": e.get("dir") or "",
            "dirExists": True,
            "status": "imported",
            "note": e.get("note"),
            "paper": True,
            "inDoneList": False,
            "phase": {"stage": "导入", "done": 0, "total": 5},
            "steering": None,
            "spec": effective_spec(code, topic),
            "origin": "import",
            "importedAt": e.get("importedAt"),
            "sourceFile": e.get("sourceFile") or "",
        })
    # 标题覆盖（改过名的优先显示）
    for t in tasks:
        ov = (fm.get("titles") or {}).get(fm_key(t["code"], t["topic"]))
        if ov:
            t["title"] = ov
            t["renamed"] = True
    # 追踪表里的任务被删掉 → 过滤
    if deleted:
        tasks = [t for t in tasks
                 if fm_key(t["code"], t["topic"]) not in deleted or t.get("origin") == "import"]
    # ---- 左栏任务卡的真实摘要（2026-11-23）----
    # 目的：左栏 140 个任务，卡片上只有编号/主题/规格/状态 → 扫不出哪篇写得好。
    # 这里给每篇补：写了几节 / 有正文几节 / 引用几条 / 表几张 / 图表几张 / 缺陷几条。
    # ⚠️ 全部从**磁盘上的真实论文 JSON** 读，不调模型、不编。
    for t in tasks:
        t["summary"] = _paper_summary(t.get("code"), t.get("topic"))

    out["tasks"] = tasks

    # ---- 统一口径 ----
    # 之前这里有四套互不一致的计数：
    #   completed  = 追踪表自称完成（含磁盘上根本没有产物的）
    #   placeholder= 追踪表自称占位
    #   withPaper  = 磁盘上真有 _完整论文.docx
    #   doneList   = _已完成.txt 的行数（远大于任务数，是历史遗留）
    # 结果顶部显示「completed 10 / 占位 130 / 已成稿 1」，同一屏三个数字互相打架。
    #
    # 现在以「磁盘产物」为唯一事实源，分四档，互斥且可加：
    #   paper     真稿：05_论文/ 下有 *_完整论文.docx
    #   pipeline  产出中：目录存在且有中间产物（数据/因子/回归），但还没成稿
    #   claimed   仅声明完成：追踪表说完成，磁盘无任何产物（不可核验）
    #   empty     什么都没有
    def _cls(t):
        if t["phase"]["stage"] == "论文":
            return "paper"
        if t["dirExists"] and t["phase"]["done"] > 0:
            return "pipeline"
        if t["status"] == "completed":
            return "claimed"
        return "empty"

    for t in tasks:
        t["cls"] = _cls(t)

    counts = {"paper": 0, "pipeline": 0, "claimed": 0, "empty": 0}
    for t in tasks:
        counts[t["cls"]] += 1

    out["summary"] = {
        "total": len(tasks),
        # 单一事实源：磁盘产物
        "paper": counts["paper"],
        "pipeline": counts["pipeline"],
        "claimed": counts["claimed"],
        "empty": counts["empty"],
        # 追踪表自称（仅作对照，不作为进度依据）
        "claimedByTracker": sum(1 for t in tasks if t["status"] == "completed"),
        "placeholderByTracker": sum(1 for t in tasks if t["status"] == "placeholder_only"),
        "dirExists": sum(1 for t in tasks if t["dirExists"]),
        "steered": sum(1 for t in tasks if t["steering"]),
        # _已完成.txt 是历史遗留名单，行数远大于任务数，不能当进度指标
        "doneListEntries": len(done_codes),
        "doneListInTasks": sum(1 for t in tasks if t["inDoneList"]),
    }

    # ---- 缺陷（真实逐项数据）----
    dpath = defect_csv()
    defects = {"available": False, "rows": [], "stats": {}}
    if dpath:
        rows = load_csv(dpath)
        hp = sum(1 for r in rows if r.get("摘要声明_预测期") != r.get("结果表标签_预测期"))
        n_match = sum(1 for r in rows if r.get("正文表格_N") == r.get("实际_N"))
        loss = [float(r["丢样率"]) for r in rows if r.get("丢样率")]
        loss.sort()
        defects = {
            "available": True,
            "source": os.path.basename(dpath),
            "stats": {
                "count": len(rows),
                "horizonMismatch": hp,
                "tableNMatch": n_match,
                "lossMedian": loss[len(loss) // 2] if loss else None,
                "lossMin": loss[0] if loss else None,
                "lossMax": loss[-1] if loss else None,
            },
            "rows": rows[:200],
        }
    out["defects"] = defects

    # steering 概览（看板与 OpenClaw 侧看板都要看）
    st_all = load_steering()
    out["steeringSummary"] = {
        "count": len(st_all),
        "keys": sorted(st_all.keys()),
        "items": [{"key": k, "note": (v or {}).get("note"),
                   "horizon": (v or {}).get("horizon"),
                   "xs": (v or {}).get("xs"),
                   "wlen": (v or {}).get("wlen"),
                   "split_q": (v or {}).get("split_q"),
                   "updatedAt": (v or {}).get("updatedAt")}
                  for k, v in sorted(st_all.items())],
    }

    # ---- 引用核验（真实 CSV + 报告统计）----
    pdf_rows = load_csv(PDF_CHECK)
    pdf_stats = {"count": len(pdf_rows)}
    if pdf_rows:
        pdf_stats["pass"] = sum(1 for r in pdf_rows if "PASS" in (r.get("校验结论") or ""))
        pdf_stats["fail"] = pdf_stats["count"] - pdf_stats["pass"]
        pdf_stats["papers"] = len(set(r.get("项目目录") for r in pdf_rows))
    out["pdf"] = pdf_stats

    cite = {}
    if os.path.isfile(CITE_REPORT):
        txt = ro(CITE_REPORT)
        for label, key in [("原始参考文献条目", "raw"), ("核验通过并保留", "kept"),
                           ("不可核验已移除", "removed"), ("PDF 内容级校验通过", "pdfOk"),
                           ("PDF 不可得", "pdfMissing")]:
            m = re.search(re.escape(label) + r"\s*\|\s*\*{0,2}(\d+)", txt)
            if m:
                cite[key] = int(m.group(1))
    out["citations"] = cite

    # ---- 服务健康（真实探测）----
    out["services"] = {
        "proxy8789": probe("http://127.0.0.1:8789/"),
        "gateway18789": probe("http://127.0.0.1:18789/health"),
        "spaceship5199": probe("http://127.0.0.1:5199/spaceship/"),
    }
    return out


def probe(url):
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as r:
            return {"reachable": True, "status": r.status}
    except Exception as e:
        return {"reachable": False, "error": type(e).__name__}


# ================================================================
# 论文 IDE 引擎
# ================================================================
# 目标：把「一篇论文」当成一个可持续推进的工程状态，而不是一次性的 docx。
#
# 状态文件（每个 code+topic 一个）:
#   state/papers/<CODE>__<TOPIC>.json
#     {
#       "code","topic","title",
#       "abstract": "用户可编辑的摘要（也是整篇的方向锚）",
#       "abstractAuto": "系统按主题+标题自动生成的摘要",
#       "abstractDirty": bool,          # 用户改过就不再自动覆盖
#       "direction": "写作方向决策（由已生成内容推导）",
#       "parts": { partKey: {"status":"empty|generated|edited",
#                           "text":"...", "updatedAt":ms} },
#       "refs": [ {key,title,year,source,cite} ],     # 参考文献池
#       "tables": [ {id,title,kind:"table",data,caption} ],
#       "figures": [ {id,title,kind:"chart",chartType,data,caption} ],
#       "compute": [ {id,title,method,result} ],        # 数据清洗/运算列表
#       "progress": { partKey: bool }
#     }
#
# 关键设计：
#  - 方向不是预设的，而是从「已经写出来的内容」推导（见 _derive_direction）。
#  - 写作方向候选必须来自该论文自身的 abstract + 已生成段落，不能是全域固定列表。
#  - 每篇论文独立定制，不共享方向。

PAPER_DIR = os.path.join(MIRROR, "papers")

# ---------------------------------------------------------------- 资料目录
# 主人 2026-11-23 要求：「FARS软件一个大文件夹，同时是一个文章一个文件夹，
# 做好软件工程文件整理」。
# 目录：
#   Papers/<CODE>_<Topic>/
#       paper.json        ← 论文实体
#       01_文献/          ← 下载的 PDF + sources.json 清单
#       02_数据/          ← 下载的数据文件 + data_sources.json
#       03_图表/          ← 生成的图表
#       04_分析/          ← 分析产物
#
# 真源仍是 state/papers/<CODE>__<Topic>.json（不动它 —— 迁移会打断
# 一切既有逻辑）。这里做**视图目录**：实体文件在两边都写，
# 资料（文献/数据）只存在 Papers/ 下 —— 用户能直接在文件管理器里看到。
PAPERS_ROOT = os.path.join(MIRROR, "Papers")
SUB_LIT = "01_文献"
SUB_DATA = "02_数据"
SUB_FIG = "03_图表"
SUB_ANA = "04_分析"


def paper_folder(code, topic):
    """一篇论文对应的文件夹。"""
    return os.path.join(PAPERS_ROOT, "%s_%s" % (code, topic))


def ensure_paper_dirs(code, topic):
    """建好一篇论文的目录骨架，返回根目录。"""
    root = paper_folder(code, topic)
    for sub in ("", SUB_LIT, SUB_DATA, SUB_FIG, SUB_ANA):
        try:
            os.makedirs(os.path.join(root, sub), exist_ok=True)
        except Exception:
            pass
    return root


def _lit_manifest_path(code, topic):
    return os.path.join(paper_folder(code, topic), SUB_LIT, "sources.json")


def _data_manifest_path(code, topic):
    return os.path.join(paper_folder(code, topic), SUB_DATA, "data_sources.json")

# 论文 part 序列（与 build_paper.compose_blocks 的输出对齐）
PARTS = [
    ("abstract", "摘要"),
    ("intro", "引言"),
    ("lit", "文献综述"),
    ("data", "数据与变量"),
    ("method", "研究设计"),
    ("results", "实证结果"),
    ("robust", "稳健性检验"),
    ("conclusion", "结论"),
]

# 统计计算方法（数据清洗/运算列表可选）
COMPUTE_METHODS = [
    ("ols", "普通最小二乘"),
    ("fe_entity", "个体固定效应（组内去均值）"),
    ("fe_time", "时间固定效应"),
    ("descriptive", "描述统计"),
    ("correlation", "相关系数矩阵"),
    ("winsorize", "截面 1% 去极值"),
    ("split_q", "按市场波动率分位切分"),
]

# 图表类型
CHART_TYPES = [
    ("line", "折线图"),
    ("bar", "柱状图"),
    ("scatter", "散点图"),
    ("area", "面积图"),
    ("pie", "饼图"),
    ("hist", "直方图"),
]


def _paper_path(code, topic):
    return os.path.join(PAPER_DIR, "%s__%s.json" % (code, topic))


# ---------------------------------------------------------------- 文件列表
# 左侧栏是「文件列表」：既要有追踪表里的 140 个任务，也要有用户手动导入的文档。
#
# ⚠️ 关键约束（踩过才知道）：
#   _进度追踪.json 是**单向同步**的 —— sync_state.py 每 20 秒把 Desktop 的原件
#   复制到 state/，会**覆盖**看板写进去的任何东西。
#   → 所以导入的论文 / 改名 / 删除记录一律存在 state/filemeta.json（不在同步列表里），
#     论文实体仍在 state/papers/（也不在同步列表里）。
FILEMETA_FILE = os.path.join(MIRROR, "filemeta.json")
IMPORT_DIR = os.path.join(MIRROR, "imports")

# ---------------------------------------------------------------- 运行配置
# 模型接入配置。**API key 不落文件** —— 存 macOS 钥匙串（见 keychain_get/keychain_set）。
# 原因：这套东西要打包发给别人测试，配置文件会跟着走；
#       key 落文件 = 把我的 key 一起发出去。
#
# ⚠️ 密钥串只在 macOS 上有。非 macOS（发给别人的 Linux/Windows 版）→ 回退到
#    环境变量 FARS_API_KEY / MINIMAX_API_KEY，读不到就提示用户填。
CONFIG_FILE = os.path.join(MIRROR, "config.json")

# 预设模型（每项：label / protocol / base / model / 需要 key 的供应商）
MODEL_PRESETS = [
    {"id": "minimax", "label": "MiniMax（M2.7）", "protocol": "anthropic",
     "base": "https://api.minimaxi.com/anthropic", "model": "MiniMax-M2.7",
     "keyHint": "MiniMax 开放平台 API Key"},
    {"id": "minimax-openai", "label": "MiniMax（OpenAI 兼容）", "protocol": "openai",
     "base": "https://api.minimaxi.com/v1", "model": "MiniMax-M2.7",
     "keyHint": "MiniMax 开放平台 API Key"},
    {"id": "deepseek", "label": "DeepSeek", "protocol": "openai",
     "base": "https://api.deepseek.com/v1", "model": "deepseek-chat",
     "keyHint": "DeepSeek API Key"},
    {"id": "moonshot", "label": "Moonshot / Kimi", "protocol": "openai",
     "base": "https://api.moonshot.cn/v1", "model": "moonshot-v1-8k",
     "keyHint": "Moonshot API Key"},
    {"id": "qwen", "label": "阿里通义千问（DashScope）", "protocol": "openai",
     "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus",
     "keyHint": "DashScope API Key"},
    {"id": "openai", "label": "OpenAI", "protocol": "openai",
     "base": "https://api.openai.com/v1", "model": "gpt-4o-mini",
     "keyHint": "OpenAI API Key"},
    {"id": "anthropic", "label": "Anthropic Claude", "protocol": "anthropic",
     "base": "https://api.anthropic.com/v1", "model": "claude-sonnet-4-20250514",
     "keyHint": "Anthropic API Key"},
    {"id": "custom", "label": "自定义（OpenAI 兼容）", "protocol": "openai",
     "base": "", "model": "", "keyHint": "API Key"},
]


# ---------------------------------------------------------------- 监听地址安全
# ⚠️ 2026-11-23 23:1x（用户要求「都加上」）：
#   这个服务**没有登录、没有鉴权** —— 监听地址一旦不是本机，
#   同网络下任何人都能读写论文、调用 API Key。
#   两道防线：
#     ① **白名单/格式校验**：只接受合法 IP 或域名，非法值直接拒绝（防注入/防乱填）。
#     ② **二次确认**：非本机地址必须带 hostConfirm 回执，否则拒绝保存。
#   本机地址（loopback）永远直接放行，不打扰用户。
_LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*$")


def _host_is_loopback(h):
    """是不是本机地址（本机永远允许，不弹确认）。"""
    if not h:
        return False
    h = str(h).strip().lower()
    if h in _LOCAL_HOSTS:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except Exception:
        return False


def _validate_host(h):
    """监听地址白名单校验 → (ok, error)

    只接受：合法 IP 字面量（v4/v6） 或 合法主机名。
    拒绝：空值、超长、含空格/斜杠/中文等奇怪字符。
    """
    if h is None:
        return False, "监听地址不能为空"
    h = str(h).strip()
    if not h:
        return False, "监听地址不能为空"
    if len(h) > 255:
        return False, "监听地址过长"
    if h.lower() in _LOCAL_HOSTS:
        return True, ""
    # 合法 IP？
    try:
        ipaddress.ip_address(h)
        return True, ""
    except ValueError:
        pass
    # 合法主机名？
    if len(h) <= 253 and _HOSTNAME_RE.match(h):
        return True, ""
    return False, "监听地址格式不合法 —— 只允许 IP 地址或域名"


def load_config():
    d = load_json(CONFIG_FILE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("presetId", "minimax")
    d.setdefault("protocol", "anthropic")
    d.setdefault("baseUrl", PROXY_URL)
    d.setdefault("model", MODEL)
    d.setdefault("port", PORT)
    d.setdefault("host", HOST)
    d.setdefault("temperature", 0.4)
    d.setdefault("maxTokens", 2000)
    d.setdefault("hasKey", False)
    return d


def save_config(d):
    os.makedirs(MIRROR, exist_ok=True)
    tmp = CONFIG_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)


# ---- 钥匙串（macOS）----
_KEYCHAIN_SVC = "fars-dashboard-api-key"

def keychain_get():
    """从钥匙串取 API key。失败/非 macOS → 环境变量 → None。"""
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", _KEYCHAIN_SVC,
                 "-w"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
            if r.returncode == 0:
                v = r.stdout.decode("utf-8", "replace").strip()
                if v:
                    return v
        except Exception:
            pass
    for k in ("FARS_API_KEY", "MINIMAX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        v = os.environ.get(k)
        if v:
            return v
    return None


def keychain_set(key):
    """写钥匙串。非 macOS → 只能退回环境变量（前端会提示）。

    ⚠️ 2026-11-23 修：`security add-generic-password -w <key>` 会把 key
    放进**进程参数**（ps 可见，任何本机进程都能读到），而且 `-U` 对 add
    不是「更新」参数 → 实测直接返回非 0，钥匙串写不进去。
    正确做法：`-w` 不带值，从**stdin** 读密码（security 会提示两次，
    第一次是"密码"，第二次是"确认密码"，所以要喂两行）。
    """
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", _KEYCHAIN_SVC],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        p = subprocess.Popen(
            ["/usr/bin/security", "add-generic-password",
             "-s", _KEYCHAIN_SVC,
             "-a", "fars-dashboard",
             "-D", "FARS Dashboard API Key",
             "-w"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        # security 读两次（密码 + 确认）
        p.communicate(input=(key + "\n" + key + "\n").encode("utf-8"), timeout=10)
        return p.returncode == 0
    except Exception:
        return False


def keychain_clear():
    if sys.platform != "darwin":
        return False
    try:
        subprocess.run(["/usr/bin/security", "delete-generic-password", "-s", _KEYCHAIN_SVC],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8)
        return True
    except Exception:
        return False


def active_llm():
    """当前生效的模型接入参数（配置 + key）。

    ⚠️ 2026-11-23 修：key 来源**必须**走 minimax_key()（钥匙串 → 环境变量
    → ~/.dsh/.credentials.yaml 三级回退），不能只用 keychain_get()。
    实测踩过：改成统一层后，生产生成直接 401 ——
    因为设置页测试时往钥匙串写了假 key，而原来真正在用的
    ~/.dsh/.credentials.yaml 里的真 key 没被读到。
    """
    cfg = load_config()
    key = minimax_key()
    return {
        "baseUrl": (cfg.get("baseUrl") or "").rstrip("/"),
        "model": cfg.get("model") or "",
        "protocol": cfg.get("protocol") or "anthropic",
        "key": key,
        "temperature": cfg.get("temperature", 0.4),
        "maxTokens": cfg.get("maxTokens", 2000),
    }


def _llm_url(base, protocol):
    """拼出完整请求 URL。

    ⚠️ 2026-11-23 修：base 末尾若已有 /v1，不能再拼 /v1/messages
    → 否则得到 https://api.openai.com/v1/v1/messages（前端预览也一样）。
    规则：剥掉结尾的 /v1 再拼；已经是完整端点（.../messages / .../chat/completions）则原样用。
    """
    b = (base or "").rstrip("/")
    if not b:
        raise RuntimeError("未配置 API 地址")
    if b.endswith("/messages") and protocol == "anthropic":
        return b
    if b.endswith("/chat/completions"):
        return b
    # ⚠️ 两种协议的 base 结构不同，**不能共用同一条规则**（踩过）：
    #   openai    : base 自带 /v1 → https://api.openai.com/v1/chat/completions
    #               （**不能**剥 /v1，剥了就变成 /chat/completions，404）
    #   anthropic : base 自带 /v1 → https://api.anthropic.com/v1/messages
    #               （也不能剥，剥了变成 /messages，404）
    #   anthropic : base 不带 /v1 → 补 /v1/messages
    #   openai    : base 不带 /v1 → 补 /v1/chat/completions
    if protocol == "anthropic":
        return (b + "/messages") if b.endswith("/v1") else (b + "/v1/messages")
    return (b + "/chat/completions") if b.endswith("/v1") else (b + "/v1/chat/completions")


def llm_call(system, user, stream=False, max_tokens=None, temperature=None,
              timeout=120, with_usage=False):
    """统一模型调用（支持 openai / anthropic 两种协议）。

    返回契约（⚠️ 踩过：之前返回 HTTPResponse，调用方当文本用 →
    'HTTPResponse' object has no attribute 'splitlines'）：
      · stream=True  → **HTTPResponse 对象**（调用方自己逐行读 SSE）
      · stream=False → **纯文本字符串**
    """
    a = active_llm()
    if not a["key"]:
        raise RuntimeError("缺少 API Key —— 去「设置」填一下")
    if not a["model"]:
        raise RuntimeError("未配置模型名")
    url = _llm_url(a["baseUrl"], a["protocol"])
    mt = max_tokens if max_tokens is not None else a["maxTokens"]
    tp = temperature if temperature is not None else a["temperature"]

    if a["protocol"] == "anthropic":
        body = {"model": a["model"], "stream": bool(stream),
                "max_tokens": mt, "temperature": tp,
                "system": system,
                "messages": [{"role": "user", "content": user}]}
        headers = {"content-type": "application/json", "x-api-key": a["key"],
                   "anthropic-version": "2023-06-01"}
    else:
        msgs = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": user}]
        body = {"model": a["model"], "stream": bool(stream),
                "max_tokens": mt, "temperature": tp, "messages": msgs}
        headers = {"content-type": "application/json",
                   "authorization": "Bearer " + a["key"]}

    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    resp = urllib.request.urlopen(req, timeout=timeout)
    if stream:
        return resp
    # 非流式 → 解析出纯文本
    try:
        raw = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return ("", {}) if with_usage else ""
    if a["protocol"] == "anthropic":
        txt = "".join(b.get("text", "") for b in (raw.get("content") or [])
                      if b.get("type") == "text")
    else:
        ch = (raw.get("choices") or [{}])[0]
        msg = ch.get("message") or {}
        txt = msg.get("content") or ch.get("text") or ""
    if with_usage:
        u = raw.get("usage") or {}
        if a["protocol"] != "anthropic":
            u = {"input_tokens": u.get("prompt_tokens"),
                 "output_tokens": u.get("completion_tokens")}
        return (txt, u)
    return txt



# ============================================================
# 刘看山研究建议（2026-11-25）
# ------------------------------------------------------------
# 输入：知乎热榜（真实抓取）+ 当前论文上下文（真实状态）
# 输出：结构化研究建议，每条带可检验假设 / 变量 / 数据源
#
# ⚠️ 设计原则（和 FARS 其他面板一致）：
#   · 只从**热榜原文**和**真实论文上下文**出发，不编造
#   · 每条建议必须能落到「下一步做什么」，不能只是空话
#   · 抓不到 / 没 Key → 返回明确的降级说明 + 用户可执行的下一步
# ============================================================

KSN_ADVICE_SYSTEM = """你是一位资深金融经济学研究助理，服务于金融计量研究系统 FARS。
用户正在写一篇实证论文，需要基于给定的【知乎热榜真实条目】和【当前论文上下文】，
产出可立即动手的研究建议。

硬性要求：
1. 只使用给定材料中的信息，**不要编造**文献名、数据源名称、变量定义。
2. 每条建议必须包含：可检验的假设、涉及变量、可用数据源、验证方法。
3. 建议要具体到能马上开一个回归，不能是"可以进一步研究"这类空话。
4. 用中文，术语准确，语气务实。
5. 严格输出 JSON，不要任何额外文字。

输出格式（必须严格遵守）：
{"advices":[{"title":"一句话说明研究什么","why":"为什么值得做（结合热榜与论文上下文）",
"hypothesis":"可检验的假设（写成 H1: ... 的形式）",
"variables":["变量1","变量2"],"data":"可用数据源","test":"怎么检验（回归设定）",
"difficulty":"低|中|高"}],"summary":"对当前论文的一句话总体建议"}"""

KSN_ADVICE_SYSTEM_FALLBACK = """你是一位研究助理。基于【热榜条目】和【论文上下文】，
给出 2-3 条具体的金融计量研究建议。每条包含：研究什么、为什么、可检验假设、变量、数据源、怎么检验。
用中文。直接输出 JSON：
{"advices":[{"title":"","why":"","hypothesis":"H1: ","variables":[],"data":"","test":"","difficulty":"低"}],"summary":""}"""


def _ksn_extract_json(txt):
    """从模型输出里抠出 JSON（模型常带 ```json 围栏或前后废话）。"""
    if not txt:
        return None
    txt = txt.strip()
    m = re.search(r"```(?:json)?\s*(.+?)```", txt, re.S)
    if m:
        txt = m.group(1).strip()
    # 直接试整体
    for cand in (txt,):
        try:
            return json.loads(cand)
        except Exception:
            pass
    # 找第一个 { ... } 平衡片段
    start = txt.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(txt)):
        if txt[i] == "{":
            depth += 1
        elif txt[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(txt[start:i + 1])
                except Exception:
                    break
    # ⚠️ 走到这说明输出被 max_tokens **截断**了（模型没说完，JSON 不完整）。
    #   实测踩过：1877 字处断在 "summary":"先用 → 整体 json.loads 失败 → 建议全丢。
    #   → 降级：逐条抢救 —— 把 advices 数组里**每个完整对象**单独抠出来解析。
    out = []
    arr_start = txt.find('"advices"')
    if arr_start >= 0:
        arr_start = txt.find("[", arr_start)
        if arr_start >= 0:
            depth = 0
            obj_start = None
            i = arr_start
            while i < len(txt):
                ch = txt[i]
                if ch == "{":
                    if depth == 0:
                        obj_start = i
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and obj_start is not None:
                        try:
                            o = json.loads(txt[obj_start:i + 1])
                            if isinstance(o, dict):
                                out.append(o)
                        except Exception:
                            pass
                        obj_start = None
                elif ch == "]":
                    break
                i += 1
    if out:
        # summary 可能也被截断 → 尝试抢救，不强求
        summ = ""
        m = re.search(r'"summary"\s*:\s*"(.*?)"', txt, re.S)
        if m:
            summ = m.group(1)
        return {"advices": out, "summary": summ}
    return None


def _ksn_paper_context(code, topic):
    """取当前论文的真实上下文（有就取，没有就明确说没有）。"""
    if not code:
        return "（当前未选中论文）"
    try:
        d = load_paper(code, topic)
    except Exception:
        return "（论文 %s/%s 读取失败）" % (code, topic)
    if not d:
        return "（当前未选中论文）"
    parts = d.get("parts") or {}
    spec = effective_spec(code, topic) or {}
    bits = []
    title = (d.get("title") or "").strip()
    if title:
        bits.append("标题：%s" % title)
    # 哪几节写了
    written = []
    for k, v in parts.items():
        t = (v or {}).get("text") or ""
        if len(t.strip()) > 40:
            written.append("%s(%d字)" % (k, len(t)))
    if written:
        bits.append("已写章节：" + "、".join(written))
    else:
        bits.append("已写章节：无")
    # 当前规格
    try:
        cur = spec.get("spec") or spec
        if isinstance(cur, dict):
            if cur.get("horizon"):
                bits.append("当前预测期：%s" % cur["horizon"])
            fams = cur.get("factors") or cur.get("family") or []
            if fams:
                bits.append("当前因子：%s" % "、".join(str(x) for x in fams))
    except Exception:
        pass
    return "；".join(bits) if bits else "（上下文为空）"


def ksn_advise(code=None, topic=None, limit=6):
    """生成研究建议。返回 (result_dict, http_status)。

    result_dict:
      {ok: bool, advices: [...], summary: str, source: str, fallback: str|None}
    """
    # —— 1) 热榜（真实抓取）——
    try:
        items, warnings = zhihu_hot_list(limit=20)
    except Exception as e:
        items, warnings = [], [str(e)]
    hot_lines = []
    for it in (items or [])[:12]:
        hot_lines.append("%s. %s（热度 %s）%s" % (
            it.get("rank", "?"), (it.get("title") or "").strip(),
            it.get("heat") or "-", (it.get("excerpt") or "").strip()[:80]))
    hot_text = "\n".join(hot_lines) if hot_lines else "（本轮未取到知乎热榜）"

    ctx = _ksn_paper_context(code, topic)

    user = ("【知乎热榜真实条目】\n%s\n\n"
            "【当前论文上下文】\n%s\n\n"
            "请基于以上材料，给出最多 %d 条研究建议。" % (hot_text, ctx, max(1, min(6, int(limit or 6)))))

    # —— 2) 调模型 ——
    try:
        txt = llm_call(KSN_ADVICE_SYSTEM, user, max_tokens=4000, temperature=0.4)
    except Exception as e:
        # 降级：没配 Key / 网络失败 → 给可执行的下一步，不空手而归
        msg = str(e)
        step = ("去「设置」填 API Key 后再点一次" if "Key" in msg
                else "检查网络或稍后重试（模型调用失败：%s）" % msg[:80])
        return {"ok": False, "advices": [], "summary": "",
                "source": "hot", "fallback": step,
                "hotCount": len(items or [])}, 200

    data = _ksn_extract_json(txt)
    if not isinstance(data, dict) or not isinstance(data.get("advices"), list):
        # 模型没给合法 JSON → 再宽松试一次
        try:
            txt2 = llm_call(KSN_ADVICE_SYSTEM_FALLBACK, user,
                             max_tokens=3000, temperature=0.3)
            data = _ksn_extract_json(txt2)
        except Exception:
            data = None
    if not isinstance(data, dict) or not isinstance(data.get("advices"), list):
        return {"ok": False, "advices": [], "summary": "",
                "source": "hot", "fallback": "模型输出无法解析为建议（已重试一次）",
                "hotCount": len(items or [])}, 200

    # —— 3) 清洗（字段缺失会前端崩）——
    out = []
    for a in data["advices"][:6]:
        if not isinstance(a, dict):
            continue
        vars_ = a.get("variables")
        if isinstance(vars_, str):
            vars_ = [vars_]
        if not isinstance(vars_, list):
            vars_ = []
        out.append({
            "title": str(a.get("title") or "").strip()[:120],
            "why": str(a.get("why") or "").strip()[:400],
            "hypothesis": str(a.get("hypothesis") or "").strip()[:240],
            "variables": [str(v).strip()[:40] for v in vars_ if str(v).strip()][:8],
            "data": str(a.get("data") or "").strip()[:200],
            "test": str(a.get("test") or "").strip()[:300],
            "difficulty": (str(a.get("difficulty") or "中").strip()[:4] or "中"),
        })
    out = [o for o in out if o["title"]]
    return {"ok": True, "advices": out,
            "summary": str(data.get("summary") or "").strip()[:300],
            "source": "hot", "fallback": None,
            "hotCount": len(items or [])}, 200


def load_filemeta():
    d = load_json(FILEMETA_FILE, {})
    if not isinstance(d, dict):
        d = {}
    d.setdefault("imports", [])     # 导入的论文
    d.setdefault("titles", {})      # 标题覆盖 key=code|topic
    d.setdefault("deleted", [])     # 删除记录 key=code|topic
    return d


def save_filemeta(d):
    os.makedirs(MIRROR, exist_ok=True)
    tmp = FILEMETA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, FILEMETA_FILE)


def fm_key(code, topic):
    return "%s|%s" % (code, topic)


def find_import(code, topic):
    for e in load_filemeta()["imports"]:
        if e.get("code") == code and e.get("topic") == topic:
            return e
    return None


def next_import_code():
    used = set(e.get("code") for e in load_filemeta()["imports"])
    n = 1
    while True:
        c = "IMP%03d" % n
        if c not in used:
            return c
        n += 1


def _safe_name(s):
    """把任意标题变成安全的文件名片段（保留中文）。"""
    s = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", (s or "").strip())
    return (s or "untitled")[:80]


def _paper_title_on_disk(code, topic):
    """直接读 paper 实体里的标题（不建文件、不触发 seed）。"""
    p = _paper_path(code, topic)
    if not os.path.isfile(p):
        return ""
    try:
        d = json.load(open(p, encoding="utf-8"))
        return (d.get("title") or "").strip()
    except Exception:
        return ""


def _paper_default(code, topic, title):
    return {
        "code": code,
        "topic": topic,
        "title": title or "",
        "abstract": "",
        "abstractAuto": "",
        "abstractDirty": False,
        "direction": "",
        "directionCandidates": [],
        # 两级控制（对齐原版 Control = {dimension, position, strength}）
        "strength": "中等",   # 方向强度 = 支配广度，不是语气
        "tone": "严谨客观",
        "style": "详尽展开",
        "parts": {k: {"status": "empty", "text": "", "updatedAt": 0} for k, _ in PARTS},
        "refs": [],
        "tables": [],
        "figures": [],
        "compute": [],
        # P2 钉到边栏：钉住的章节（持续在右栏展示其真实回归结果）
        "pins": [],
        # P2 强度=方法权重：统计方法 → 权重（1..3），影响生成时的主次方法
        "methodWeights": {},
        "progress": {k: False for k, _ in PARTS},
        "updatedAt": 0,
    }


def load_paper(code, topic, title=None):
    """读论文状态；不存在则用默认（并用真实回归结果预填首篇成稿的正文）。"""
    os.makedirs(PAPER_DIR, exist_ok=True)
    p = _paper_path(code, topic)
    if os.path.isfile(p):
        try:
            d = json.load(open(p, encoding="utf-8"))
        except Exception:
            d = _paper_default(code, topic, title)
        base = _paper_default(code, topic, title)
        for k, v in base.items():
            d.setdefault(k, v)
        return d
    d = _paper_default(code, topic, title)
    _seed_paper_from_disk(code, topic, d)
    save_paper(d)
    return d


# ---- 论文级写锁（2026-11-23 加）----
# 症状：并行生成两个节时，后写完的那个把先写完的内容**覆盖**了
#   （实测 method 写进去了，results/conclusion 的新内容被抹掉）。
# 根因：save_paper(d) 是**整篇全量写文件**，而 d 是请求开始时的**快照**。
#   A 请求改了 parts.method，B 请求改了 parts.conclusion，
#   B 结束时把「B 的快照 + B 的改动」整篇写回 → A 的改动被覆盖。
# 修法：写盘时用**磁盘上的最新版本**做基底，只把**本请求改的那一节**合并进去。
_PAPER_LOCKS = {}
_PAPER_LOCKS_GUARD = threading.Lock()


def _paper_lock(code, topic):
    """取（并创建）某篇论文的写锁。"""
    key = (code or "", topic or "")
    with _PAPER_LOCKS_GUARD:
        lk = _PAPER_LOCKS.get(key)
        if lk is None:
            lk = threading.RLock()
            _PAPER_LOCKS[key] = lk
        return lk


def save_part_merged(code, topic, part_key, part_value, extra=None):
    """**并发安全**地写回某一节。

    做法：
      1. 取该篇的写锁（同一篇的写盘串行，不同篇并行）
      2. 重新读磁盘上的**最新**论文
      3. 只替换 parts[part_key]，其余部分保留磁盘版本
      4. 原子写回

    这样 A/B 两个并发请求各写各的节 → 两边都保住。
    """
    if READONLY:
        return None
    lk = _paper_lock(code, topic)
    with lk:
        d = load_paper(code, topic)
        if d is None:
            return None
        if part_key:
            d.setdefault("parts", {})[part_key] = part_value
        if extra:
            for k, v in extra.items():
                d[k] = v
        d["progress"][part_key] = True
        _refresh_direction(d)
        save_paper(d)
        return d


def save_paper(d):
    """落盘论文实体。

    READONLY 模式（FARS_READONLY=1）下**只读不写** ——
    用来在生产实例上做生成链路回归测试而不污染真实论文。
    实测踩过：直接对生产跑 /stream 回归，把 results 节真的改了。
    """
    if READONLY:
        return False
    os.makedirs(PAPER_DIR, exist_ok=True)
    d["updatedAt"] = int(time.time() * 1000)
    p = _paper_path(d["code"], d["topic"])
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, p)
    return True


# -------------------------------------------------------------
# 选区改写：右键拖选一段文字 → 按上下文给修改建议 → 一键改写
# -------------------------------------------------------------

def _locate_in_part(d, part_key, snippet, start=None, end=None):
    """在章节源文本里定位选中片段。

    返回 (整节原文, 起点, 终点)；定位不到时起点为 -1。

    定位优先级：
      ① **偏移**（start/end）—— 客户端算好的**源文本**字符区间，最准。
      ② 字符串 find —— 兜底（选区文字在源文本里能原样找到）。
      ③ 前 12 字模糊定位。

    ⚠️ 用**服务端原文**定位，而不是客户端给的 DOM 偏移：
    DOM 里经过了 esc() 和 <<FIG:id>> 渲染，长度与源文本对不上，
    所以客户端必须先把 DOM 偏移映射回源文本偏移。
    """
    pp = (d.get("parts") or {}).get(part_key) or {}
    full = pp.get("text") or pp.get("full") or ""
    if not full:
        return full, -1, -1

    # ① 偏移优先
    if isinstance(start, int) and isinstance(end, int) and start >= 0 and end > start:
        s = max(0, min(start, len(full)))
        e = max(s, min(end, len(full)))
        if s < e:
            return full, s, e

    sn = (snippet or "").strip()
    if not sn:
        return full, -1, -1
    i = full.find(sn)
    if i >= 0:
        return full, i, i + len(sn)
    # 模糊定位：取前 12 字（选区可能因换行/空白差异对不上）
    head = sn[:12]
    if head:
        i = full.find(head)
        if i >= 0:
            return full, i, min(len(full), i + len(sn))
    return full, -1, -1


def _part_label(part_key):
    for pk, plabel in _PART_LABELS:
        if pk == part_key:
            return plabel
    return ""


def _neighbor_part_text(d, part_key, direction, limit=260):
    """取**相邻章节**的文本，作为本节首/末句的上下文。

    为什么需要（主人 2026-11-25 反馈）：
    选第一句时本节没有上文，选最后一句时本节没有下文 →
    模型拿不到任何语境 → 只能给空话建议。
    → 向前借**上一章的结尾**，向后借**下一章的开头**。

    direction: -1 = 上一章（取尾部），+1 = 下一章（取头部）。
    返回 (文本, 章节名)；没有相邻章节时返回 ("", "")。
    """
    keys = [pk for pk, _ in _PART_LABELS]
    try:
        i = keys.index(part_key)
    except ValueError:
        return "", ""
    j = i + direction
    if j < 0 or j >= len(keys):
        return "", ""
    nb_key = keys[j]
    nb = (d.get("parts") or {}).get(nb_key) or {}
    nb_text = nb.get("text") or nb.get("full") or ""
    if not nb_text.strip():
        return "", ""
    nb_text = _strip_markers(nb_text).strip()
    if direction < 0:
        # 上一章的**结尾**（最近的一段）
        piece = nb_text[-limit:].strip()
    else:
        # 下一章的**开头**
        piece = nb_text[:limit].strip()
    nb_label = _part_label(nb_key)
    return piece, nb_label


def _parse_suggestions(raw):
    """把模型返回的建议行解析成 [{text, basedOn}, ...]。"""
    out = []
    for line in (raw or "").splitlines():
        t = line.strip()
        if not t:
            continue
        t = re.sub(r"^[-*•]\s*", "", t)
        t = re.sub(r"^\d+[.)、]\s*", "", t)
        t = re.sub(r"^建议[:：]\s*", "", t)
        if not t or t.startswith("好的") or t.startswith("以下是"):
            continue
        sug, basis = t, ""
        if "|" in t:
            a, b = t.split("|", 1)
            sug, basis = a.strip(), b.strip()
        elif "｜" in t:
            a, b = t.split("｜", 1)
            sug, basis = a.strip(), b.strip()
        basis = re.sub(r"^依据[:：]\s*", "", basis).strip()
        if not sug:
            continue
        out.append({"text": sug[:200], "basedOn": basis[:140]})
        if len(out) >= 6:
            break
    return out


# ============================================================
# 知乎生态：热榜接入（2026-11-25 路演要求「知乎生态融合度」）
# ------------------------------------------------------------
# 实测（2026-11-25）知乎接口的认证行为：
#   - /api/v3/feed/topstory/hot-lists/total  -> 401 需登录态
#   - /api/v3/feed/topstory/hot-list-web      -> 200，公开可取（主用）
#   - /api/v4/search_v3                     -> 403
#   - /api/v4/hot_list                       -> 404
# 所以主用 hot-list-web，并保留 total 作为降级。
# 全部走服务端代理，浏览器直连会被 CORS 挡掉。
# ============================================================

_ZHIHU_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

_ZHIHU_ENDPOINTS = [
    "https://www.zhihu.com/api/v3/feed/topstory/hot-list-web?limit=%d",
    "https://api.zhihu.com/topstory/hot-lists/total?limit=%d",
]


def _zhihu_fetch(url, timeout=12):
    req = urllib.request.Request(url, headers={
        "User-Agent": _ZHIHU_UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://www.zhihu.com/",
        "Accept-Language": "zh-CN,zh;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def zhihu_hot_list(limit=20):
    """抓知乎热榜。返回 (items, warnings)。

    items: [{rank, title, excerpt, heat, url, image}]
    抓不到不抛异常 —— 返回空列表 + warnings，
    上层据此显示「知乎暂不可用」而不是整个面板报错。
    """
    limit = max(1, min(50, int(limit or 20)))
    warnings = []
    for ep in _ZHIHU_ENDPOINTS:
        url = ep % limit
        try:
            data = _zhihu_fetch(url)
        except Exception as e:
            warnings.append("%s → %s" % (urllib.parse.urlparse(ep).path, e))
            continue
        raw = data.get("data") if isinstance(data, dict) else None
        if not raw:
            warnings.append("%s → 返回为空" % urllib.parse.urlparse(ep).path)
            continue
        items = []
        for i, it in enumerate(raw):
            t = it.get("target") or {}
            ta = t.get("title_area") or {}
            ex = t.get("excerpt_area") or {}
            mt = t.get("metrics_area") or {}
            lk = t.get("link") or {}
            im = t.get("image_area") or {}
            title = (ta.get("text") or "").strip()
            if not title:
                continue
            heat = (mt.get("text") or "").strip()
            items.append({
                "rank": i + 1,
                "title": title,
                "excerpt": (ex.get("text") or "").strip()[:300],
                "heat": heat,
                "heatNum": _zhihu_heat_num(heat),
                "url": (lk.get("url") or "").strip(),
                "image": (im.get("url") or "").strip(),
            })
        if items:
            return items, warnings
        warnings.append("%s → 无可用条目" % urllib.parse.urlparse(ep).path)
    return [], warnings


def _create_paper_from_topic(title, topic, source_url="", source_rank=0,
                             source_heat="", excerpt=""):
    """从一个选题（知乎热榜条目）创建空白论文骨架并注册。

    复用导入流程的注册方式（filemeta.imports），这样它会出现在左侧任务列表里。
    返回 (entry, error)。
    """
    title = (title or "").strip()
    if not title:
        return None, "缺少标题"
    if topic not in ("ESG", "Machine", "Stock"):
        topic = "Stock"
    code = next_import_code()

    d = _paper_default(code, topic, title)
    # 把选题来源记进论文状态 —— 后面生成时可以让模型知道这是从知乎问题切入的
    d["origin"] = {
        "source": "知乎热榜",
        "url": source_url,
        "rank": source_rank,
        "heat": source_heat,
        "excerpt": (excerpt or "")[:600],
        "pickedAt": int(time.time() * 1000),
    }
    save_paper(d)

    entry = {
        "code": code, "topic": topic, "title": title,
        "origin": "zhihu",
        "sourceUrl": source_url,
        "sourceRank": source_rank,
        "sourceHeat": source_heat,
        "importedAt": int(time.time() * 1000),
        "dir": code + "_" + topic + "_" + _safe_name(title),
    }
    fm = load_filemeta()
    fm["imports"] = [e for e in fm.get("imports", [])
                     if not (e.get("code") == code and e.get("topic") == topic)]
    fm["imports"].append(entry)
    fm["deleted"] = [k for k in fm.get("deleted", []) if k != fm_key(code, topic)]
    save_filemeta(fm)
    return entry, None


def _zhihu_heat_num(heat):
    """「798 万热度」→ 7980000（用于排序/筛选）。"""
    if not heat:
        return 0
    m = re.search(r"([\d.]+)\s*万", heat)
    if m:
        try:
            return int(float(m.group(1)) * 10000)
        except Exception:
            return 0
    m = re.search(r"([\d.]+)", heat)
    if m:
        try:
            return int(float(m.group(1)))
        except Exception:
            return 0
    return 0


def _suggest_mods(d, part_key, snippet, tone=None, style=None,
                 start=None, end=None):
    """针对选中文字给出修改建议。返回 (suggestions, 错误)。"""
    key = minimax_key()
    if not key:
        return [], "缺少 API Key"

    full, s, e = _locate_in_part(d, part_key, snippet, start, end)
    if s < 0:
        return [], "无法在正文里定位选中的文字"

    W = 220
    before = _strip_markers(full[max(0, s - W):s]).strip()
    after = _strip_markers(full[e:e + W]).strip()
    target = full[s:e].strip()

    topic = d.get("topic") or ""
    title = d.get("title") or ""
    direction = d.get("direction") or ""
    label = _part_label(part_key)

    # ⚠️ 2026-11-25（主人反馈「改第一句/最后一句没法改，因为没有上文下文」）：
    #   本节内没有上文/下文时 → **跨章节借**。
    #   上文借**上一章的结尾**，下文借**下一章的开头**。
    #   若连相邻章节都没有（摘要的第一句 = 全文开头；结论的最后一句 = 全文结尾）
    #   → 明确告诉模型「这是全文第���句/最后一句」，让它按**开篇/收束**的角度提建议，
    #     而不是对着空白硬编。
    before_from = ""
    after_from = ""
    if not before:
        prev_txt, prev_lbl = _neighbor_part_text(d, part_key, -1)
        if prev_txt:
            before = prev_txt
            before_from = "（来自上一章「%s」的结尾）" % prev_lbl
        else:
            before_from = "（**无** —— 这里是全文的第一句）"
    else:
        before_from = "（本节上文）"
    if not after:
        next_txt, next_lbl = _neighbor_part_text(d, part_key, +1)
        if next_txt:
            after = next_txt
            after_from = "（来自下一章「%s」的开头）" % next_lbl
        else:
            after_from = "（**无** —— 这里是全文的最后一句）"
    else:
        after_from = "（本节下文）"

    # 兜底：给模型**本节全文**（截断），让它知道这一节在讲什么。
    # 没有它，选首/末句时模型完全不知道章节内容，只能给空话。
    section_digest = _strip_markers(full).strip()[:600]

    ctx = []
    if title:
        ctx.append("论文标题：%s" % title)
    if topic:
        ctx.append("研究主题：%s" % topic)
    if direction:
        ctx.append("全文写作方向：%s" % direction)
    if label:
        ctx.append("所在章节：%s" % label)
    if tone:
        ctx.append("期望语气：%s" % tone)
    if style:
        ctx.append("期望笔法：%s" % style)
    ctx.append("所在章节全文（供你了解本章在讲什么）：\n%s" % (section_digest or "（本章暂无正文）"))
    ctx.append("选中文字：\n「%s」" % target)
    ctx.append("前文%s：\n%s" % (before_from, before or "（无）"))
    ctx.append("后文%s：\n%s" % (after_from, after or "（无）"))

    user = (
        "%s\n\n"
        "上面选中了一段文字。请针对**这一处**给出 3~5 条**具体可执行**的修改建议。\n"
        "要求：\n"
        "1. 每条建议都要**落到选中的具体措辞上**"
        "（例如「『XXX』表述过强，应改为…」），"
        "不要给「加强逻辑」「提高严谨性」这种放之四海皆准的空话；\n"
        "2. 「依据」必须指出**从上下文看出的具体问题**"
        "（引用前文/后文的措辞或逻辑关系），不要凭空猜测；\n"
        "3. 覆盖不同维度：论证衔接、因果表述强度、术语与口径、证据支撑、"
        "冗余啰嗦、学术语体等；\n"
        "4. ⚠️ **不得编造数据、文献或结论**；没有数据就建议补检验设计，不要给假数字；\n"
        "5. 每条一行，格式严格如下：\n"
        "   - 建议：<一句话说清要怎么改> | 依据：<具体依据>\n"
        "6. 若「前文」或「后文」标注为**全文的第一句/最后一句**（无上下文），\n"
        "   请改为从**开篇点题**/**收束全文**的角度提建议\n"
        "   （例如：首句应点明研究问题与本文贡献；末句应概括结论并指出局限或展望），\n"
        "   此时「依据」可依据**论文标题、研究主题、写作方向和本节全文**来写；\n"
        "7. 只输出这些建议行，不要任何前言、后记或额外说明。\n"
    ) % ("\n".join(ctx))

    try:
        out = llm_call(
            "你是严谨的学术写作编辑，只输出针对选中文字的修改建议，不要任何解释。",
            user, stream=False, max_tokens=1200, temperature=0.5, timeout=150)
    except Exception as ex:
        return [], "模型调用失败：%s" % str(ex)[:80]
    if not isinstance(out, str):
        out = str(out or "")
    sugs = _parse_suggestions(out)
    if not sugs:
        return [], "模型没有返回可用建议"
    return sugs, ""


def _apply_mod(d, part_key, snippet, suggestion, tone=None, style=None,
                strength=None, start=None, end=None):
    """按建议改写选中的文字。返回 (新文字, 错误)。"""
    key = minimax_key()
    if not key:
        return None, "缺少 API Key"

    full, s, e = _locate_in_part(d, part_key, snippet, start, end)
    if s < 0:
        return None, "无法在正文里定位选中的文字"
    target = full[s:e].strip()

    topic = d.get("topic") or ""
    direction = d.get("direction") or ""
    label = _part_label(part_key)
    W = 180
    before = _strip_markers(full[max(0, s - W):s]).strip()
    after = _strip_markers(full[e:e + W]).strip()
    # ⚠️ 同 _suggest_mods：本节内没有上文/下文 → 跨章节借
    if not before:
        prev_txt, _ = _neighbor_part_text(d, part_key, -1)
        before = prev_txt
    if not after:
        next_txt, _ = _neighbor_part_text(d, part_key, +1)
        after = next_txt

    ctx = []
    if topic:
        ctx.append("研究主题：%s" % topic)
    if direction:
        ctx.append("全文写作方向：%s" % direction)
    if label:
        ctx.append("所在章节：%s" % label)
    if tone:
        ctx.append("期望语气：%s" % tone)
    if style:
        ctx.append("期望笔法：%s" % style)
    if strength:
        ctx.append("改写力度：%s" % strength)
    ctx.append("修改建议：%s" % (suggestion or "（未给出具体建议）"))
    ctx.append("前文：\n%s" % (before or "（无）"))
    ctx.append("后文：\n%s" % (after or "（无）"))

    user = (
        "%s\n\n"
        "【待改写的选中文字】\n%s\n\n"
        "请**按上述修改建议**改写这段选中文字，直接输出改写后的文字。\n"
        "要求：\n"
        "1. **只输出改写后的文字**，不要解释、不要引号、不要编号、不要任何前后缀；\n"
        "2. 必须**针对建议做出实质修改**（不是同义换词）；\n"
        "3. ⚠️ **长度与原文相当**（不超过原文字数的 1.4 倍），"
        "**只许在原文基础上修改，不要添加原文里没有的新论述、新概念、新证据**；\n"
        "4. 原文里的数字、符号、变量名必须**原样保留**；\n"
        "5. 保持原文语言（中文改中文，英文改英文）；\n"
        "6. 不要加引号，不要加「改写：」之类的前缀。"
    ) % ("\n".join(ctx), target)

    try:
        out = llm_call("你是严谨的学术写作助手，只输出改写后的文字，不要任何解释。",
                       user, stream=False, max_tokens=700, temperature=0.3, timeout=120)
    except Exception as ex:
        return None, "模型调用失败：%s" % str(ex)[:80]
    if not isinstance(out, str):
        out = str(out or "")
    out = out.strip()
    out = re.sub(r'^["\u201c\u300c\u300d\u201d\']+', "", out)
    out = re.sub(r'^\s*(?:改写|修改后|输出|新文字)\s*[:：]\s*', "", out)
    out = out.strip().strip('"').strip()
    if len(out) < 2:
        return None, "模型没有返回可用内容"
    return out, ""


def _rewrite_snippet(part_text, snippet, suggestion, tone=None, style=None,
                     strength=None, direction=None, topic=""):
    """按审查建议改写正文里的某一句。

    返回 (新句子, 说明) 或 (None, 错误)。
    定位策略：snippet 在正文里找 → 找不到就退化成「包含 snippet 前 12 字的那句」。
    """
    key = minimax_key()
    if not key:
        return None, "缺少 API Key"

    # ① 定位目标句
    # ⚠️ 实测踩过：审查栏有一类问题是**结构性缺失**（缺消融、缺泛化检验），
    #   它们在正文里**没有对应的原句** → snippet 为空 →
    #   原来直接返回「没有可改写的原文片段」→ 主人要的一键修改对这类条目失效。
    # 现在：snippet 为空 → 走「**按建议补写**」模式（见 _rewrite_append）。
    if not snippet:
        return _rewrite_append(part_text, suggestion, tone=tone, style=style,
                               strength=strength, direction=direction, topic=topic)

    idx = part_text.find(snippet)
    target = snippet
    if idx < 0:
        head = snippet[:12]
        if not head:
            return None, "无法在正文里定位这一处"
        idx = part_text.find(head)
        if idx < 0:
            # 定位不到 → 不再直接失败，改为**按建议补写**（主人要求每条都能一键改）
            return _rewrite_append(part_text, suggestion, tone=tone, style=style,
                                   strength=strength, direction=direction,
                                   topic=topic)
        # 扩到句末
        end = part_text.find("。", idx)
        end = (end + 1) if end > 0 else min(len(part_text), idx + 60)
        target = part_text[idx:end]
    else:
        # 扩到完整句（改写应该针对整句）
        s0 = part_text.rfind("。", 0, idx)
        s0 = (s0 + 1) if s0 >= 0 else 0
        e0 = part_text.find("。", idx)
        e0 = (e0 + 1) if e0 > 0 else len(part_text)
        target = part_text[s0:e0]

    ctx = []
    if direction:
        ctx.append("本文写作方向：%s" % direction)
    if tone:
        ctx.append("语气：%s" % tone)
    if style:
        ctx.append("笔法：%s" % style)
    if topic:
        ctx.append("论文主题：%s" % topic)
    ctx.append("审查发现的问题：%s" % (suggestion or "（未给出具体建议）"))

    user = (
        "%s\n\n"
        "【待改写的原句】\n%s\n\n"
        "请**改写上面这一句**，直接输出改写后的句子。\n"
        "要求：\n"
        "1. **只输出改写后那一句**，不要解释、不要引号、不要编号、不要任何前后缀；\n"
        "2. 必须**针对审查指出的问题**做出实质修改（不是同义换词）；\n"
        "3. ⚠️ **长度必须与原句相当**（不要超过原句字数的 1.3 倍），"
        "而且**只许在原句基础上修改，不要添加原句里没有的新论述、新概念、新证据**。"
        "原句有多少句就还写多少句 —— 你的任务是修好它，不是扩写它；\n"
        "4. 原句里已有的数字、符号、变量名必须**原样保留**；\n"
        "5. 必须以句号「。」收尾，句子要完整。"
    ) % ("\n".join(ctx), target)

    try:
        out = llm_call("你是严谨的学术写作助手，只输出改写后的句子，不要任何解释。",
                       user, stream=False, max_tokens=600, temperature=0.3, timeout=90)
    except Exception as e:
        return None, "模型调用失败：%s" % e
    if not isinstance(out, str):
        out = str(out or "")
    out = out.strip()
    # 去掉模型可能加的引号/编号/前缀
    out = re.sub(r'^["\u201c\u300c\u300d\u201d\']+', "", out)
    out = re.sub(r'^\s*(?:改写|修改后|输出)\s*[:：]\s*', "", out)
    out = out.strip().strip('"').strip()
    out = out.split("\n")[0].strip() if out.count("\n") > 2 else out
    if len(out) < 4:
        return None, "模型没有返回可用内容"
    return out, ""


def _rewrite_append(part_text, suggestion, tone=None, style=None,
                    strength=None, direction=None, topic=""):
    """没有对应原句时，按审查建议**补写**一句/一段。

    用于结构性缺失类问题（缺消融、缺泛化检验、缺引用位置…）。
    返回 (新句子, 说明) 或 (None, 错误)。
    """
    key = minimax_key()
    if not key:
        return None, "缺少 API Key"
    ctx = []
    if direction:
        ctx.append("本文写作方向：%s" % direction)
    if tone:
        ctx.append("语气：%s" % tone)
    if style:
        ctx.append("笔法：%s" % style)
    if topic:
        ctx.append("论文主题：%s" % topic)
    if part_text:
        ctx.append("现有正文（节选，用于对齐术语与口径）：\n%s"
                   % part_text[:1200])

    user = (
        "%s\n\n"
        "【审查指出的问题与建议】\n%s\n\n"
        "这一条在正文里**没有对应的原句**（属于结构性缺失）。\n"
        "请针对它**补写一段完整的中文内容**，插入正文即可。\n"
        "要求：\n"
        "1. **只输出补写的内容本身**，不要解释、不要引号、不要编号、不要任何前后缀；\n"
        "2. 必须**正面回应上面那条建议**（例如：说明将如何补做该检验、"
        "或直接补上该检验的设计与预期结论）；\n"
        "3. 术语、口径、变量名必须与现有正文**一致**；\n"
        "4. ⚠️ **不得编造具体数值、样本量、显著性水平或文献结论** —— "
        "没有真实数据支撑就只写检验设计，不要给假数字；\n"
        "5. 必须以句号「。」收尾，句子完整；"
        "6. 长度 2~4 个完整短句即可，不要长篇大论。"
    ) % ("\n".join(ctx), suggestion or "（未给出具体建议）")

    try:
        out = llm_call("你是严谨的学术写作助手，只输出补写的内容，不要任何解释。",
                       user, stream=False, max_tokens=800, temperature=0.3, timeout=90)
    except Exception as e:
        return None, "模型调用失败：%s" % e
    if not isinstance(out, str):
        out = str(out or "")
    out = out.strip()
    out = re.sub(r'^["\u201c\u300c\u300d\u201d\']+', "", out)
    out = re.sub(r'^\s*(?:补写|新增|修改后|输出)\s*[:：]\s*', "", out)
    out = out.strip().strip('"').strip()
    if len(out) < 4:
        return None, "模型没有返回可用内容"
    return out, ""


def _ref_surnames(title):
    """从文献标题里解析**所有作者姓** + 年份。

    ⚠️ 踩过的坑：原来只取**首姓**（re.match 第一个词），
    于是 "Jegadeesh, Narasimhan, & Titman, Sheridan (1993)" 只认 Jegadeesh，
    正文里写 "Titman (1993)" → 被误判成「引了但没进参考文献表」（orphan）。
    实测 FE111212：orphan 被算成 14 条，其中 13 条是这种假阳性。
    → 现在解析全部作者姓。

    返回 [(surname, year), ...]。年份对整条文献是共享的。
    """
    t = (title or "").strip()
    if not t:
        return []
    ym = re.search(r"[（(](\d{4})[）)]", t)
    year = ym.group(1) if ym else None
    head = t[:ym.start()] if ym else t
    # APA 作者段："Berg, Florian, Kölbel, Julian F., & Rigobon, Roberto"
    # → 以 ", " 切开后，偶数位是姓，奇数位是名
    parts = [x.strip() for x in head.split(",")]
    out = []
    for i in range(0, len(parts) - 1, 2):
        cand = parts[i].lstrip("& ").strip()
        nxt = parts[i + 1]
        if "&" in nxt:
            continue
        m = re.fullmatch(r"([A-Z][A-Za-z\u00C0-\u00FF'\u2019\-]+)", cand)
        if not m:
            continue
        out.append((m.group(1), year))
    if not out:
        m = re.match(r"^([A-Z][A-Za-z\u00C0-\u00FF'\u2019\-]+)", t)
        if m:
            out.append((m.group(1), year))
    return out


def _cite_support(d):
    """章节 ↔ 文献的**支撑关系**（全部真实比对，不调模型）。

    对每一节：
      · cited  = 正文里真的写出了该文献的「姓氏(年份)」→ 这一节被该文献支撑
      · orphan = 正文里写了某个「姓氏(年份)」，但它**不在 refs 列表里**
                 → 这是真实缺陷（引了但没进参考文献表）
    返回 {parts: {key: {cited:[{surname,year,refKey}], orphan:[{surname,year}]}},
           summary: {...}}
    """
    refs = d.get("refs") or []
    parts = d.get("parts") or {}

    # 正文里所有「姓氏(年份)」形态
    # ⚠️ 必须排除 APA 作者段的**名**位置：
    #   "Berg, Florian, Kölbel, Julian F., & Rigobon, Roberto (2022)."
    #   → 正则会把 "Roberto (2022)" 当成「引用了 Roberto」，
    #     而 Roberto 是**名**不是姓 → 误判成「引了但没进参考文献表」。
    #   实测 FE111212：4 条 orphan 全部是这种假阳性。
    # 判据：**紧邻逗号**的名字 = APA 的「姓, 名」里的名 → 跳过。
    #      正文里的行内引用是 "Fama (1992)"，前面不会有逗号。
    cite_pat = re.compile(
        r"([A-Z][A-Za-z\u00C0-\u00FF'\u2019\-]+)\s*[（(](\d{4})[）)]")

    # 建立索引：(surname, year) → refKey ；以及 surname → [(year, refKey)]
    by_sy = {}
    by_surname = {}
    for r in refs:
        for sname, year in _ref_surnames(r.get("title")):
            if not sname:
                continue
            by_surname.setdefault(sname, [])
            if year:
                by_sy.setdefault((sname, year), r.get("key"))
                by_surname[sname].append((year, r.get("key")))

    out = {}
    tot_cited = 0
    tot_orphan = 0
    for pk, pv in parts.items():
        txt = ""
        if isinstance(pv, dict):
            txt = pv.get("full") or pv.get("text") or ""
        cited = []
        orphan = []
        seen = set()
        for _m in cite_pat.finditer(txt or ""):
            sname, year = _m.group(1), _m.group(2)
            # 跳过 APA 作者段的「名」位置（前面是逗号）
            _pre = txt[max(0, _m.start(1) - 3):_m.start(1)]
            if _pre.rstrip().endswith((",", "，")):
                continue
            key = (sname, year)
            if key in seen:
                continue
            seen.add(key)
            rk = by_sy.get(key)
            if rk:
                cited.append({"surname": sname, "year": year, "refKey": rk})
            else:
                # 这个姓氏+年份在正文里，但 refs 里没有完全对应的条目
                # → 可能是 refs 里同姓不同年（也不算数），或压根没收录
                cands = by_surname.get(sname) or []
                exact = any(y == year for y, _ in cands)
                if not exact:
                    orphan.append({"surname": sname, "year": year})
        out[pk] = {"cited": cited, "orphan": orphan}
        tot_cited += len(cited)
        tot_orphan += len(orphan)

    # 反向：refs 里有但正文从没引用的
    used = set()
    for v in out.values():
        for c in v["cited"]:
            used.add(c["refKey"])
    unused = [r.get("key") for r in refs if r.get("key") not in used]

    return {
        "parts": out,
        "summary": {
            "citedTotal": tot_cited,
            "orphanTotal": tot_orphan,
            "refsTotal": len(refs),
            "refsCited": len(used),
            "unusedRefs": unused,
        },
    }


def run_audit(d, spec=None):
    """对论文正文做 AI Integrity Audit（确定性核验，不调模型）。

    依据 FARS 原论文 §3.5（writing 阶段的 numerical verification / citation 检查 /
    unsupported-claim 审查）与 §5.1（8 类完整性失败模式）。
    审计结果存回 d["audit"]，前端在正文改动后展示。
    """
    try:
        r = audit_mod.audit_paper(d, spec or {})
    except Exception as e:
        r = {"ok": False, "error": str(e), "findings": [],
             "summary": {"violations": 0, "weaknesses": 0, "verdict": "审计失败：%s" % e}}
    d["audit"] = r
    return r


def _seed_paper_from_disk(code, topic, d):
    """已有成稿的论文：把 docx 文本与真实结果表灌进状态，让 IDE 能接着写。"""
    # 1) 找项目目录
    track = load_json(TRACK_FILE, [])
    proj = None
    for e in track:
        if e.get("code") == code and e.get("topic") == topic:
            proj = os.path.join(BASE, e.get("dir") or "")
            d["title"] = e.get("title") or d["title"]
            break
    if not proj or not os.path.isdir(proj):
        return

    # 2) docx 文本 → 按 part 切
    doc = os.path.join(proj, "05_论文")
    if os.path.isdir(doc):
        for fn in os.listdir(doc):
            if fn.endswith("_完整论文.docx"):
                try:
                    from docx import Document
                    docobj = Document(os.path.join(doc, fn))
                    paras = [pp.text.strip() for pp in docobj.paragraphs if pp.text.strip()]
                    _assign_parts_from_paragraphs(d, paras)
                except Exception:
                    pass
                break

    # 3) 真实回归结果表 → 表格 + 运算列表
    tbl = os.path.join(proj, "04_分析", "回归结果表.csv")
    if os.path.isfile(tbl):
        rows = load_csv(tbl)
        if rows:
            models = []
            for r in rows:
                m = r.get("模型")
                if m and m not in models:
                    models.append(m)
            for i, m in enumerate(models):
                sub = [r for r in rows if r.get("模型") == m]
                d["tables"].append({
                    "id": "tbl_%d" % i,
                    "title": m,
                    "kind": "table",
                    "columns": list(sub[0].keys()),
                    "rows": sub,
                    "caption": m,
                })
            d["compute"].append({
                "id": "cmp_0",
                "title": "面板回归（个体固定效应 + 个股聚类标准误）",
                "method": "fe_entity",
                "result": "回归结果表.csv：%d 个模型" % len(models),
            })
    # 4) 参考文献
    refs = os.path.join(proj, "01_文献", "参考文献.md")
    if os.path.isfile(refs):
        try:
            for line in ro(refs).splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                clean = _clean_ref_line(line)
                if not clean:
                    continue
                d["refs"].append({"key": "ref_%d" % len(d["refs"]),
                                  "title": clean, "source": "01_文献/参考文献.md"})
        except Exception:
            pass

    # 跨来源去重（md 与 docx 会有同一条）
    d["refs"] = _dedupe_refs(d.get("refs") or [])


# docx 里的章节标题 → part key。按真实论文的章节标记归位，不靠段落数猜。
SECTION_HEADS = [
    ("abstract", "abstract"),
    ("introduction", "intro"),
    ("literature", "lit"),
    ("related work", "lit"),
    ("literature review", "lit"),
    ("data and methodology", "data"),
    ("methodology", "method"),
    ("research design", "method"),
    ("method", "method"),
    ("empirical", "results"),
    ("results", "results"),
    ("robustness", "robust"),
    ("conclusion", "conclusion"),
    ("references", "references"),
    # ---- 中文章节名 ----
    # ⚠️ 2026-11-23 修：_is_section_head 的 _PLAIN_HEAD 早就认得中文标题
    # （摘要/引言/文献综述…），但 SECTION_HEADS 只有英文关键词 →
    # 中文标题被判成「章节头」却匹配不到任何关键词 → cur=None → 全部 fallback
    # 到 intro，正文全堆进第一节。
    # 这个 bug 对中文成稿同样成立（导入的中文论文、Desktop 上已有的中文 docx）。
    # 匹配是「子串 + 按长度降序」，所以长关键词优先：
    #   "数据与变量"(5) 先于 "数据"(2)，"稳健性检验"(5) 先于 "稳健性"(3)。
    ("参考文献", "references"),
    ("文献综述", "lit"),
    ("数据与变量", "data"),
    ("实证结果", "results"),
    ("稳健性检验", "robust"),
    ("研究设计", "method"),
    ("摘要", "abstract"),
    ("引言", "intro"),
    ("结论", "conclusion"),
    ("数据", "data"),
    ("方法", "method"),
    ("实证", "results"),
    ("稳健性", "robust"),
    ("参考文献", "references"),
]
# "Data and Methodology" 一节同时含数据与方法：先归 data，
# 但其中的方法段落要补进 method，不能丢。
COMBINED_METHOD_HEADS = ("data and methodology", "data and method", "methodology")


_NUM_HEAD = re.compile(r"^\s*\d+(\.\d+)*\s*[.、]?\s*[A-Za-z一-鿿]")
_PLAIN_HEAD = re.compile(r"^\s*(abstract|introduction|literature|related work|data|methodology|"
                         r"method|empirical|results|robustness|conclusion|references|"
                         r"摘要|引言|文献综述|研究设计|数据|方法|实证结果|稳健性|结论)\s*$", re.I)


def _is_section_head(txt):
    """判断是不是章节标题行。

    真实成稿的标题行有两类：
      - 编号式："1. Introduction" / "3. Data and Methodology"
      - 裸标题："Abstract" / "References"
    两种都要认；正文段落（长、以句号结尾）不能误判。
    """
    t = (txt or "").strip()
    if not t or len(t) > 60:
        return False
    # 中文正文段落：以句号结尾且偏长 → 不是标题
    if len(t) > 24 and t.endswith(("。", "；", "，")):
        return False
    if _PLAIN_HEAD.match(t):
        return True
    if _NUM_HEAD.match(t):
        low = t.lower()
        for kw, _ in SECTION_HEADS:
            if kw in low:
                return True
    return False


def _assign_parts_from_paragraphs(d, paras):
    """把 docx 段落按真实章节标记归位。

    真实成稿的结构是：标题行 → "Abstract" → 摘要正文 → "1. Introduction" → 正文…
    所以要按章节标题切段，不能把首行（标题）当摘要。
    """
    order = [k for k, _ in PARTS if k != "abstract"]

    # 去掉开头的论文标题行（如果下一段是 Abstract 之类的章节头）
    paras = list(paras)
    if len(paras) >= 2 and _is_section_head(paras[1]) and not _is_section_head(paras[0]):
        paras = paras[1:]

    # 按章节头切段
    buckets = {}
    cur = None
    for t in paras:
        if _is_section_head(t):
            low = t.lower()
            cur = None
            # 关键词越长越具体，优先匹配（"literature review" 优先于 "review"）
            for kw, key in sorted(SECTION_HEADS, key=lambda kv: -len(kv[0])):
                if kw in low:
                    cur = key
                    break
            if cur is None:
                cur = "intro"
            buckets.setdefault(cur, [])
            continue
        if cur is None:
            # 第一个章节头之前的内容 → abstract
            cur = "abstract"
            buckets.setdefault(cur, [])
        buckets[cur].append(t)

    # abstract
    abs_lines = buckets.get("abstract") or []
    if abs_lines:
        abs_text = "\n\n".join(abs_lines).strip()
        d["abstractAuto"] = abs_text
        d["abstract"] = abs_text
        d["abstractDirty"] = True      # 来自既有成稿，视作已定稿
        d["parts"]["abstract"] = {"status": "generated", "text": abs_text,
                                  "updatedAt": int(time.time() * 1000)}
        d["progress"]["abstract"] = True

    # 组合章节（Data and Methodology）：把方法段落补进 method，不丢内容
    _method_extra = []
    for t in buckets.get("data") or []:
        if ("被解释变量" in t or "估计方程" in t or "回归方程" in t
                or "标准误在个股层面聚类" in t or "模型设定" in t):
            _method_extra.append(t)
    if _method_extra:
        buckets["method"] = (buckets.get("method") or []) + _method_extra

    # references 章节 → 文献列表（真实条目，不编造）
    _ref_lines = buckets.get("references") or []
    if _ref_lines:
        _have = set(_ref_key(r.get("title")) for r in d["refs"])
        for line in _ref_lines:
            clean = _clean_ref_line(line)
            if not clean:
                continue
            k = _ref_key(clean)
            if k in _have:
                continue
            d["refs"].append({"key": "ref_%d" % len(d["refs"]),
                              "title": clean, "source": "05_论文/完整论文.docx"})
            _have.add(k)
        buckets.pop("references", None)

    # 其余章节
    for k in order:
        chunk = buckets.get(k) or []
        if chunk:
            txt = "\n\n".join(chunk).strip()
            d["parts"][k] = {"status": "generated", "text": txt,
                             "updatedAt": int(time.time() * 1000)}
            d["progress"][k] = True



# 方向词表：按「题面/内容里出现的词」映射到写作方向。
# 这些是写作策略，不是固定领域 —— 换领域时靠题面词命中新的方向。
# 章节标签（用于「哪节还没写 → 下一步补哪节」这类候选）
_PART_LABELS = [
    ("abstract", "摘要"),
    ("intro", "引言"),
    ("lit", "文献综述"),
    ("data", "数据与变量"),
    ("method", "研究设计"),
    ("results", "实证结果"),
    ("robust", "稳健性"),
    ("conclusion", "结论"),
]

TITLE_DIRECTIONS = [
    ("ESG", "强化 ESG 作为信息集扩充项的论证，明确其与传统因子的边界"),
    ("公司价值", "围绕「因子 → 公司价值」的传导链条组织论证，补上机制环节"),
    ("可预测性", "区分「可预测性」与「可利用性」，明确本文只做前者"),
    ("机器学习", "先厘清线性基准与非线性方法的对比设计，再谈增量价值"),
    ("动量", "对比动量与反转的相对信息含量，说明样本内呈现的方向"),
    ("反转", "检验反转效应的持有期依赖，说明短窗口与长窗口的差异"),
    ("波动", "深化状态依赖性：区分高低波动期下的效应强度差异"),
    ("流动性", "说明流动性代理的口径局限，补充剔除该代理的稳健规格"),
    ("规模", "交代规模变量为价格代理的局限，并给出替代口径的检验方案"),
]

# 同一因子的**不同切入角度**（语义独立，不是同一句话加后缀）
FACTOR_ANGLES = {
    "ESG": (
        "检验 ESG 评分的时变性与分组敏感性，说明结论在哪些子样本成立",
        "补写 ESG 与传统因子的正交性检验，避免把共线当增量",
    ),
    "ESG评分": (
        "检验 ESG 评分的时变性与分组敏感性，说明结论在哪些子样本成立",
        "补写 ESG 与传统因子的正交性检验，避免把共线当增量",
    ),
    "mom12": (
        "检验动量的持有期依赖：短窗与长窗的效应是否一致",
        "补写动量与反转的交互检验，检验二者是否互为镜像",
    ),
    "rev1": (
        "检验反转效应的持有期依赖，说明短窗口与长窗口的差异",
        "补写反转与流动性的交互检验，区分信息滞后与流动性补偿",
    ),
    "vol36": (
        "检验波动率状态依赖：高低波动期下的效应强度差异",
        "补写波动率与回撤的稳健性检验，说明极端期是否主导结果",
    ),
    "turnover": (
        "检验流动性代理的敏感性：换用不同换手率口径是否结论一致",
        "补写流动性作为控制变量 vs 代理变量的双重角色讨论",
    ),
    "size_proxy": (
        "交代规模变量为价格代理的局限，并给出替代口径的检验方案",
        "补写规模与流动性共线性的诊断，说明主结果是否受此影响",
    ),
}

FACTOR_DIRECTIONS = {
    "动量": "围绕动量因子的显著性展开：说明方向、幅度与经济显著性",
    "反转": "围绕短期反转的显著性展开：与主流预期对照，如实报告方向",
    "波动率": "围绕波动率因子的显著性展开：解释其与风险溢价的对应关系",
    "流动性": "围绕流动性代理的显著性展开：讨论其是否为噪声而非信号",
    "ESG": "围绕 ESG 评分的识别策略展开：个体固定效应下不可识别，需时间固定效应",
    "ESG评分": "围绕 ESG 评分的识别策略展开：个体固定效应下不可识别，需时间固定效应",
    "规模": "围绕规模代理的显著性展开：注意其仅为价格代理",
    "mom12": "围绕中期动量因子的显著性展开：说明方向、幅度与经济显著性",
    "rev1": "围绕短期反转因子的显著性展开：与主流预期对照，如实报告方向",
    "vol36": "围绕波动率因子的显著性展开：解释其与风险溢价的对应关系",
    "turnover": "围绕流动性代理的显著性展开：讨论其是否为噪声而非信号",
    "size_proxy": "围绕规模代理的显著性展开：注意其仅为价格代理",
}

# ---- 写作方向的两级控制（对齐原版 spaceship/src/text/dimension.ts）----
# 原版 Control = { dimension, position, strength }，旋钮是**二维**的：
#   · **角度（position）** 选维度里的取值
#   · **半径（strength）** 选这个维度对整段的**支配广度**
#     strengthOf(knob) = clamp01((distance - minDistance) / STRENGTH_RANGE_PX)
# 原版 prompt 把 strength 的语义钉死了（live-completion.ts）：
#   "Strong: it dominates every sentence. Moderate: a clear influence throughout.
#    Slight: show it in one or two touches only... a slight control must never
#    set the overall character of the passage."
# → **strength 是「支配广度」，不是语气。**
# → 语气在原版是**另一个独立维度**（angle 选值），所以 FARS 也把它单列。
STRENGTH_LEVELS = ("轻微", "中等", "强烈")
_CTRL_VALUES = {
    "strength": STRENGTH_LEVELS,
    "tone": ("严谨客观", "审慎留白", "鲜明主张"),
    "style": ("详尽展开", "凝练聚焦"),
}
TONE_OPTIONS = ("严谨客观", "审慎留白", "鲜明主张")
STYLE_OPTIONS = ("详尽展开", "凝练聚焦")

# 强度的生成侧语义（对应原版 slight / moderate / strong）
_STRENGTH_PROMPT = {
    "轻微": ("这个方向只在**一两处**点到即止（例如一句话、一个例子或一处机制），"
             "**不得**决定整段的整体特征；其余内容按论文自身的论证推进。"),
    "中等": "这个方向**贯穿整段**、清楚可辨，但不压倒其他内容。",
    "强烈": "这个方向是整段的**主线**，每一句都围绕它展开。",
}
_TONE_PROMPT = {
    "严谨客观": "语气严谨客观：用词准确克制，不做情绪化评价，每个判断与给出的证据严格对应。",
    "审慎留白": "语气审慎留白：对超出证据的推论明确标注限度，避免断言，宁可留余地也不要说过头。",
    "鲜明主张": "语气鲜明主张：明确指出本文的立场与贡献，不含糊其辞、不绕弯子。",
}
_STYLE_PROMPT = {
    "详尽展开": "笔法详尽展开：铺开机制细节与铺垫，把中间环节交代清楚。",
    "凝练聚焦": "笔法凝练聚焦：只保留推进论证所必需的内容，不铺陈、不重复。",
}

STAGE_DIRECTIONS = {
    "假设": "逐条对应研究假设与实证结果，明确支持与不支持的部分",
    "机制": "补齐机制链条：从因子到结果的中间环节逐一落实",
    "识别": "补写识别策略：说明什么在当前设定下可识别、什么不可识别",
    "稳健": "扩展稳健性检验维度，补充与既有结论可能冲突的规格",
    "异质": "展开异质性分析：分组维度的选择理由与预注册式说明",
    "对比": "补充与相关研究的直接对比，说明本文口径的差异",
    "样本": "交代样本筛选口径与丢样率，说明剔除规则的影响",
    "变量": "逐个说明变量构造，给出可复现的公式级描述",
    "模型": "说明模型设定与识别假设，列出关键假设及其检验方式",
}


def _ctl_suffix(strength=None, tone=None, style=None):
    """把强度/语气/笔法拼成一段后缀（给静态模板的 part 用）。"""
    bits = []
    if strength in _STRENGTH_PROMPT:
        bits.append("强度：%s" % strength)
    if tone in _TONE_PROMPT:
        bits.append("语气：%s" % tone)
    if style in _STYLE_PROMPT:
        bits.append("笔法：%s" % style)
    if not bits:
        return ""
    return "\n（本段控制：" + "；".join(bits) + "）"


def _candidate_budget(d):
    """候选条数随**文章进度**动态调整（主人：「基于上下文以及文章还缺什么动态调整数量」）。

    理由：空文章时给 12 条是灾难 —— 没什么可写的，硬凑出来的方向多半是灌水。
    写得越多，可写的花样越多，条数自然放开。
      · 正文几乎为空（≤1 节）→ 4 条：先定骨架，别一上来让人挑花眼
      · 写了一部分（2~3 节）  → 8 条
      · 写透了（≥4 节）      → 12 条（上限，再多扇面序号挤成一条缝）
    """
    parts = d.get("parts") or {}
    done = 0
    for k, _ in PARTS:
        if ((parts.get(k) or {}).get("text") or "").strip():
            done += 1
    if done <= 1:
        return 4
    if done <= 3:
        return 8
    return 12


def _derive_direction(d):
    """从已生成的内容推导写作方向候选 —— 每篇论文独立定制。

    规则（确定性，不随机）：
      1. abstract 是方向锚：抽取引用过的因子/机制词
      2. 已生成段落里出现最多的因子/关键词
      3. 用这些推导「下一步该写什么」的候选方向

    条数：**随文章进度动态调整**（_candidate_budget）
    取法：按来源**分组后轮转** —— 每个来源先各出 1 条，再出第 2 条。
      否则某个来源（例如「补写某节」）会把名额全占掉，其他角度一个都露不了脸。
    """
    text = d.get("abstract") or d.get("abstractAuto") or ""
    done = " ".join((d["parts"][k].get("text") or "") for k, _ in PARTS)
    corpus = text + " " + done

    # 因子识别
    factor_hits = []
    for f in ["mom12", "rev1", "vol36", "turnover", "size_proxy", "ESG评分",
              "动量", "反转", "波动率", "流动性", "规模", "ESG"]:
        if f in corpus:
            factor_hits.append(f)
    # 阶段识别
    stage_hits = []
    for s_ in ["假设", "机制", "识别", "稳健", "异质", "机制检验", "反事实",
               "样本", "变量", "模型", "对比"]:
        if s_ in corpus:
            stage_hits.append(s_)

    # ---- 按来源分 6 组，组内顺序即组内优先级 ----
    g_anchor = []   # 摘要锚
    g_title = []    # 题面驱动
    g_factor = []   # 因子驱动
    g_stage = []    # 阶段驱动
    g_angle = []    # 因子 × 切入角度
    g_missing = []  # 章节缺口

    # 1) 摘要（已定稿）优先：全文以它为锚
    if d.get("abstractDirty") and text:
        g_anchor.append("以当前摘要（已人工定稿）为锚，保持全文与之一致")

    # 2) 题面驱动：标题里出现的机制/对象词决定方向，而不是固定模板。
    #    这样同一套系统换个领域（教育、医疗、政策…）也能给出贴合的写作方向。
    title = (d.get("title") or "")
    for kw, direction in TITLE_DIRECTIONS:
        if kw in title or kw in corpus:
            g_title.append(direction)

    # 3) 因子驱动：正文实际用到哪些因子，就围绕哪些因子展开
    for f in factor_hits:
        d_txt = FACTOR_DIRECTIONS.get(f)
        if d_txt:
            g_factor.append(d_txt)

    # 4) 阶段驱动：已写到哪一步，决定下一步该做什么
    for s_ in stage_hits:
        d_txt = STAGE_DIRECTIONS.get(s_)
        if d_txt:
            g_stage.append(d_txt)

    # 5) 因子 × 切入角度：同一个因子可以从不同角度写，各自独立成条
    #    ⚠️ 2026-11-22 20:42 修正：第一版写成 base + "，并补充机制链条"这种
    #    **机械后缀** → 产出 3 条几乎一样的方向（灌水，主人明确反对）。
    #    现在改成**语义独立**的切入角度，每条都是不同的活儿。
    for f in factor_hits:
        for angle in FACTOR_ANGLES.get(f, ()):
            g_angle.append(angle)

    # 6) 章节进度驱动：哪几节还是空的 → 明确下一步先补哪一节
    for pk, plabel in _PART_LABELS:
        p = (d.get("parts") or {}).get(pk) or {}
        if not (p.get("text") or "").strip():
            g_missing.append("补写「%s」一节：先搭骨架，再填实证" % plabel)

    groups = [g_anchor, g_title, g_factor, g_stage, g_angle, g_missing]

    # ---- 轮转取：每组先各出 1 条，再各出第 2 条 ----
    ordered = []
    row = 0
    while True:
        added = False
        for grp in groups:
            if row < len(grp):
                ordered.append(grp[row])
                added = True
        if not added:
            break
        row += 1

    # 去重保序
    seen = set()
    uniq = []
    for c in ordered:
        if c not in seen:
            seen.add(c)
            uniq.append(c)

    if not uniq:
        uniq.append("从摘要与已有段落提炼下一节的主线，先确定论证骨架")

    # 记录触发依据，让方向「由内容决定」这件事可追溯
    budget = _candidate_budget(d)
    d["directionCandidates"] = [
        {"text": c, "basedOn": _basis_for(c, corpus)} for c in uniq[:budget]
    ]
    return uniq


# ---- 插入点上下文驱动（主人：「fan hud 是根据生成位置上下文调节」）----
# 全局候选回答「整篇下一步写什么」；上下文候选回答「**这里**该接什么」。
# 两者不是同一批东西换句话说：上下文候选必须引用插入点**附近真实出现的内容**，
# 否则就退化成灌水。所以这里的每条都以 before/after 里的**具体字句**为锚。

# 本地上下文里出现的「论证阶段」→ 该处最该补的东西
_LOCAL_STAGE = [
    ("机制", "把上文提出的机制在这一处落到可检验的具体环节上"),
    ("假设", "把上文的假设与此处要写的证据对上，避免机制与实证脱节"),
    ("识别", "补出此处所依赖的识别条件，说明为什么其他设定下不成立"),
    ("稳健", "在此处补一句稳健性说明，指明结论依赖的是哪个关键设定"),
    ("显著", "把此处的不显著结果讲清楚，给出可检验的解释而非略过"),
    ("样本", "交代此处结论对应的样本范围与外推边界"),
]

# 本地上下文里出现的「因子」→ 该处可做的检验
_LOCAL_FACTOR = {
    "ESG": "就上文的 ESG，把此处结论拆到不同评级分组上检验",
    "mom12": "就上文的动量，把此处结论拆到不同持有期上检验",
    "rev1": "就上文的反转，把此处结论拆到不同持有期上检验",
    "vol36": "就上文的波动率，把此处结论拆到高低波动状态上检验",
    "turnover": "就上文的流动性，把此处结论拆到不同换手率口径上检验",
    "size_proxy": "就上文的规模，把此处结论拆到不同规模分组上检验",
}


def _strip_markers(txt):
    """剥掉内联图表/表格的**源标记**（<<FIG:fig_2>> / <<TAB:tbl_1>>）。
    这些是内部标记，不是正文内容 —— 直接拿去引用会显示成
    「在「<<FIG:fig_2>>」之前补一段衔接」，主人看着莫名其妙。
    剥掉后如果该句只剩标点，就当它不存在。"""
    t = txt or ""
    # 完整标记
    t = re.sub(r"<<(?:FIG|TAB):[^>]*>>", " ", t)
    # ⚠️ 切片边界处可能只剩半个标记（before 结尾 "<<FIG:fig_2" / after 开头 ">>"）
    #    → 半个标记也要剥，否则会引用出「在「<<FIG」之前…」这种主人看不懂的东西
    t = re.sub(r"<<(?:FIG|TAB):[^>]*", " ", t)
    t = re.sub(r"^[^<>\n]*>>", " ", t)
    return re.sub(r"[ \t]{2,}", " ", t)


def _sent_pieces(txt):
    """把一段文本切成句子（保留非空片段）。"""
    t = _strip_markers(txt).strip()
    if not t:
        return []
    parts = re.split(r"[。！？!?；;\n]+", t)
    return [x.strip() for x in parts if x.strip()]


def _snip(txt, n=16):
    """截一段文字做锚点引用（太长的引用反而看不清）。"""
    t = _strip_markers(txt).strip().replace("\n", " ")
    t = t.strip(" 　，,。.、；;：:")
    if not t:
        return ""
    return t if len(t) <= n else (t[:n] + "…")


def _derive_direction_at(d, part_key, offset, before, after):
    """**按插入位置**推导候选方向 —— 回答「这一处该接什么」。

    与 _derive_direction（整篇视角）的区别：
      · 整篇视角：依据因子/阶段/章节缺口，给出宏观下一步
      · 本视角：依据**插入点前后真实出现的字句**，给出这一处的衔接/补强方向

    返回 [{text, basedOn}, ...]，条数随本地上下文的丰富度动态调整。
    """
    part_label = ""
    for pk, plabel in _PART_LABELS:
        if pk == part_key:
            part_label = plabel
            break
    before = _strip_markers(before).strip()
    after = _strip_markers(after).strip()

    sb = _sent_pieces(before)
    sa = _sent_pieces(after)
    last_b = sb[-1] if sb else ""
    first_a = sa[0] if sa else ""
    local = before + " " + after

    cands = []

    def add(text, basis):
        if not text:
            return
        for c in cands:
            if c["text"] == text:
                return
        cands.append({"text": text, "basedOn": basis})

    # ① 上文收尾未完 → 承接
    if last_b:
        add("承接上文「%s」这句，把它推到可检验的结论上" % _snip(last_b),
            "上文「%s」" % _snip(last_b, 12))
    # ② 下文已经在讲别的事 → 先补齐过渡
    if first_a:
        add("在「%s」之前补一段衔接，说明它与上文结论是什么关系" % _snip(first_a),
            "下文「%s」" % _snip(first_a, 12))
    # ③ 上文是实证结论 → 补量级/证据
    if any(k in before for k in ("显著", "发现", "表明", "说明", "证实")):
        # ⚠️ 2026-11-23 修正措辞：原来写「补上具体的量级与口径」→
        #   实测模型理解成「把表里的字段列出来」→ 落盘「回归结果按变量、系数、
        #   聚类标准误、t值、p值与显著性水平逐项列示」（32 字罗列句，无句号、无切点）。
        # 现在明确要「一句有判断的话」，并禁止罗列字段名。
        add("用**一句话**把上文那个结论说清楚它意味着什么（不要罗列字段名或表里的条目）",
            "上文有实证结论但缺解释")
    # ④ 本地出现的因子 → 就地分组/时变检验
    for f, t in _LOCAL_FACTOR.items():
        if f in local:
            add(t, "本地上下文含 %s" % f)
            break
    # ⑤ 本地出现的论证阶段 → 该处该补的东西
    for kw, t in _LOCAL_STAGE:
        if kw in local:
            add(t, "本地上下文含「%s」" % kw)
            break
    # ⑥ 上文提到机制但此处没落地 → 补落地
    if ("机制" in before or "渠道" in before or "路径" in before) and "机制" not in after:
        add("把上文说的机制在这一处落到具体可检验的环节，不要停在概念层",
            "上文讲机制、此处未落地")
    # ⑦ 本节整体仍薄 → 提示该节补厚
    p = (d.get("parts") or {}).get(part_key) or {}
    sect_len = len((p.get("full") or p.get("text") or "").strip())
    if sect_len < 120:
        add("把「%s」一节当前这段写厚，现状只有一句交代，不足以支撑论证" % (part_label or "本节"),
            "本节正文过短（%d 字）" % sect_len)
    # ⑧ 全文还缺的章节（作为兜底，不占主要名额）
    for pk, plabel in _PART_LABELS:
        pp = (d.get("parts") or {}).get(pk) or {}
        if not (pp.get("text") or "").strip():
            add("补写「%s」一节：先搭骨架，再填实证" % plabel, "该节尚未生成")
            break

    if not cands:
        add("在这里补一段承接上文并引出下文的过渡，让论证连起来",
            "本地上下文无明显特征，取通用衔接")

    # 条数随本地上下文丰富度动态调整（信息少 → 少给，别硬凑）
    signals = sum(1 for x in [last_b, first_a] if x)
    budget = min(6, 3 + signals)
    return cands[:budget]


def _basis_for(cand, corpus):
    """这个方向是从哪些内容推出来的（用于 UI 展示依据）。"""
    if cand.startswith("补写「"):
        return "该节尚未生成"
    for _f in ("mom12", "rev1", "vol36", "turnover", "size_proxy"):
        if _f in cand:
            return "正文含因子 %s" % _f
    if "ESG" in cand and "ESG" in corpus:
        return "摘要/正文含 ESG"
    if "波动" in cand and ("波动率" in corpus or "vol36" in corpus):
        return "摘要/正文含波动率"
    if "动量" in cand and ("动量" in corpus or "mom12" in corpus):
        return "摘要/正文含动量"
    if "稳健" in cand:
        return "已涉及稳健性表述"
    if "假设" in cand:
        return "已涉及研究假设"
    return "当前进度"





# ------------------------------------------------------------- 运算执行

def _run_compute(d, method, spec=None):
    """按选定统计方法真实跑一遍，返回结果摘要。

    方法清单与前端一致：ols / fe_entity / fe_time / descriptive /
    correlation / winsorize / split_q
    """
    topic = d.get("topic") or ""
    spec = spec or {}
    try:
        pv = preview_regression(d["code"], topic,
                                spec.get("horizon") or 3,
                                spec.get("xs") or ["mom12", "rev1", "vol36", "turnover"],
                                spec.get("wlen"), spec.get("split_q"), None)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not pv.get("ok"):
        return {"ok": False, "error": pv.get("error", "回归结果不可用")}

    coefs = pv.get("coefs") or []
    if method == "fe_entity":
        detail = "个体固定效应（组内去均值），个股层面聚类标准误"
    elif method == "fe_time":
        detail = "时间固定效应（按年月去均值），保留横截面变异"
    elif method == "ols":
        detail = "混合 OLS，无固定效应"
    elif method == "descriptive":
        detail = "描述统计：N=%s，个体 %s，月 %s，R²=%.4f" % (
            pv.get("n"), pv.get("nClusters"), pv.get("nMonths"), pv.get("r2") or 0)
    elif method == "correlation":
        detail = "相关系数矩阵：" + "; ".join(
            "%s=%s" % (c["factor"], c["beta"]) for c in coefs)
    elif method == "winsorize":
        detail = "截面 1% 双侧去极值已施加于全部因子"
    elif method == "split_q":
        ss = pv.get("splitStats") or []
        detail = "市场波动率分位切分：" + "; ".join(
            "%s N=%s R²=%s" % (x.get("name"), x.get("n"),
                             (round(x["r2"], 4) if x.get("r2") is not None else "—"))
            for x in ss)
    else:
        detail = "未知方法"

    return {
        "ok": True,
        "method": method,
        "detail": detail,
        "coefs": coefs,
        "n": pv.get("n"),
        "r2": pv.get("r2"),
        "windowDesc": pv.get("windowDesc"),
        "splitStats": pv.get("splitStats"),
    }



# ------------------------------------------------------------- 图表与插入

def _chart_payload(d, kind, chart_type, spec=None):
    """按类型生成真实数据（不编造）。

    line/bar/scatter: 用基准回归系数与标准误
    area: 同 line
    pie: 因子显著性分布
    hist: 系数分布
    """
    topic = d.get("topic") or ""
    try:
        pv = preview_regression(d["code"], topic,
                                (spec or {}).get("horizon") or 3,
                                (spec or {}).get("xs") or ["mom12", "rev1", "vol36", "turnover"],
                                (spec or {}).get("wlen"), (spec or {}).get("split_q"), None)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not pv.get("ok"):
        return {"ok": False, "error": pv.get("error", "回归结果不可用")}

    coefs = pv.get("coefs") or []
    labels = [c["factor"] for c in coefs]
    values = [c["beta"] for c in coefs]
    if chart_type in ("line", "area", "bar"):
        if chart_type == "bar":
            return {"ok": True, "chartType": chart_type,
                    "categories": labels, "series": [{"name": "系数", "values": values}],
                    "yLabel": "系数"}
        # line/area: 系数 + 置信带
        ci_lo = [c["beta"] - 1.96 * c["se"] for c in coefs]
        ci_hi = [c["beta"] + 1.96 * c["se"] for c in coefs]
        return {"ok": True, "chartType": chart_type,
                "categories": labels,
                "series": [{"name": "系数", "values": values},
                           {"name": "95%CI 下界", "values": ci_lo},
                           {"name": "95%CI 上界", "values": ci_hi}],
                "yLabel": "系数"}
    if chart_type == "scatter":
        return {"ok": True, "chartType": chart_type,
                "categories": labels,
                "series": [{"name": "系数 vs t 值",
                            "values": [c["t"] for c in coefs]}],
                "yLabel": "t 值"}
    if chart_type == "pie":
        nsig = sum(1 for c in coefs if c["sig"])
        return {"ok": True, "chartType": chart_type,
                "segments": [{"label": "显著", "value": nsig},
                             {"label": "不显著", "value": len(coefs) - nsig}]}
    if chart_type == "hist":
        return {"ok": True, "chartType": chart_type,
                "categories": labels,
                "series": [{"name": "|t|", "values": [abs(c["t"]) for c in coefs]}],
                "yLabel": "|t|"}
    return {"ok": False, "error": "未知图表类型"}


def _table_payload(d, spec=None):
    """从真实回归结果产出表格数据。"""
    topic = d.get("topic") or ""
    try:
        pv = preview_regression(d["code"], topic,
                                (spec or {}).get("horizon") or 3,
                                (spec or {}).get("xs") or ["mom12", "rev1", "vol36", "turnover"],
                                (spec or {}).get("wlen"), (spec or {}).get("split_q"), None)
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if not pv.get("ok"):
        return {"ok": False, "error": pv.get("error", "回归结果不可用")}
    cols = ["变量", "系数", "聚类标准误", "t值", "p值", "显著性"]
    rows = []
    for c in pv.get("coefs") or []:
        rows.append([c["factor"], round(c["beta"], 6), round(c["se"], 6),
                     round(c["t"], 3), c["p"], "显著" if c["sig"] else "不显著"])
    return {"ok": True, "kind": "table", "title": "基准回归结果",
            "columns": cols, "rows": rows,
            "caption": "样本 N=%s，个体 %s，R²=%.4f，%s" % (
                pv.get("n"), pv.get("nClusters"), pv.get("r2") or 0,
                pv.get("windowDesc") or "")}


# ------------------------------------------------------------- 论文生成


# 标题关键词 → 摘要里的研究切入角度。按标题定制，不是固定套话。
TITLE_ANGLES = [
    ("ESG", "ESG 信息如何被资产价格吸收，以及它相对传统因子的增量预测力"),
    ("公司价值", "从因子定价到公司价值的传导链条是否在 A 股成立"),
    ("Firm Value", "从因子定价到公司价值的传导链条是否在 A 股成立"),
    ("可预测性", "在 A 股市场，哪些因子仍保有可预测性"),
    ("Predictability", "在 A 股市场，哪些因子仍保有可预测性"),
    ("机器学习", "非线性方法相对线性基准是否带来可度量的增量预测力"),
    ("Machine Learning", "非线性方法相对线性基准是否带来可度量的增量预测力"),
    ("动量", "中期动量效应在 A 股的表现形式与失效条件"),
    ("反转", "短期反转效应的持有期依赖与适用边界"),
    ("波动", "波动率状态如何调节各因子的预测力强度"),
]

def _gen_abstract(d, spec=None):
    """按主题 + 标题自动生成摘要。

    原则：数字必须来自真实回归结果（走 /api/preview 同一套引擎），
    不能编造。若预览失败就明确标注「结果待生成」，不假装有数字。
    """
    topic = d.get("topic") or ""
    title = d.get("title") or d.get("code") or ""
    hs = spec.get("xs") if spec else None
    hz = spec.get("horizon") if spec else None
    n = None; span = None; finding = ""
    try:
        pv = preview_regression(d["code"], topic, hz or 3,
                                hs or ["mom12", "rev1", "vol36", "turnover"],
                                (spec or {}).get("wlen"), (spec or {}).get("split_q"),
                                None)
        if pv.get("ok"):
            n = pv.get("n"); span = pv.get("windowDesc")
            dirs = []
            for c in pv.get("coefs", []):
                if c.get("sig"):
                    dirs.append("%s呈%s向且显著（系数 %+.4f，t=%.2f）"
                                % (c["factor"], "正" if c["beta"] > 0 else "负",
                                   c["beta"], c["t"]))
            finding = "；".join(dirs) + "。"
    except Exception:
        pass

    # 主题决定研究对象；标题决定具体切入角度。
    # 摘要必须读起来像这一篇自己的摘要，而不是模板套壳。
    topic_zh = {"ESG": "企业 ESG 表现与公司价值",
                "Machine": "机器学习在金融预测中的应用",
                "Stock": "股票收益可预测性"}.get(topic, topic)

    # 从标题里提炼「研究对象 + 角度」，让摘要贴着这一篇走
    angle = ""
    for kw, txt in TITLE_ANGLES:
        if kw in title:
            angle = txt
            break
    if not angle:
        angle = "检验其对未来收益的预测力"

    body = (
        "本文以《%s》为题，研究 %s。"
        % (title, topic_zh)
    )
    if angle:
        body += "研究重点在于%s。" % angle
    if n:
        body += ("研究使用 A 股月度面板数据，%s，进入回归的个股-月观测 %d 个，"
                 % (span, n))
    else:
        body += "研究使用 A 股月度面板数据，"
    body += ("采用个体固定效应面板回归（组内去均值实现）与个股层面聚类标准误，"
             "检验价格动量、短期反转、波动率与流动性对未来 %s 个月累计收益的预测力，"
             "并检验效应强度是否随市场波动状态系统性变化。" % (hz if hz else "3"))
    if finding:
        body += "结果显示：" + finding
    else:
        body += "实证结果待生成，当前不报告任何系数结论。"
    body += ("本文如实报告结果方向，包括与主流因子定价预期不一致的情形。"
             "本文不预设结论方向。")
    return body


def _gen_part(d, part_key, direction, spec=None, strength=None, tone=None, style=None):
    """按当前写作方向生成一个 part 的正文。

    生成内容必须：
      - 承接 abstract 的方向（不是通用套话）
      - 只使用真实可得的数据（回归结果 / 引用池）
      - 没有真实数据时明确说明缺口，不编造
    """
    topic = d.get("topic") or ""
    abs_text = d.get("abstract") or d.get("abstractAuto") or ""
    hz = (spec or {}).get("horizon") or 3

    if part_key == "abstract":
        return _gen_abstract(d, spec)

    if part_key == "intro":
        return ("本节说明研究问题与贡献。全文方向以摘要为准：%s\n\n"
                "本文的贡献在于把「平均显著」细化为「何时显著」，"
                "并对每一项发现配置反向对照，确保识别的是特定因子的作用，"
                "而非任何共同冲击都能推高预测力。"
                % (abs_text[:220] + ("…" if len(abs_text) > 220 else "")))

    if part_key == "lit":
        refs = _ensure_refs_for(d)
        if refs:
            items = "\n".join("（%d）%s" % (i + 1, r.get("title", "")) for i, r in enumerate(refs[:12]))
            return ("本节梳理与本文主题直接相关的文献线索，并说明各条线索与实证设计的对应关系。\n\n"
                    "引用清单（%d 条）：\n%s" % (len(refs), items))
        return ("本节需梳理文献。当前引用池为空，"
                "尚未生成可核验的参考文献条目 —— 此处不做占位式罗列，"
                "待文献检索接入后补充。")

    if part_key == "data":
        return ("数据来自本机已核验的 A 股月度收益率数据库。"
                "样本仅保留数据状态标注为「正常」的个股-月观测，剔除跨月停牌造成的收益率跳变。"
                "所有个股按月末复权价计算收益，天然处理分红与送转。"
                "动量为 t-12 至 t-1 的累计收益（跳过最近一个月）；"
                "短期反转为最近一个月收益的相反数；"
                "波动率为过去 36 个月收益标准差年化；"
                "流动性代理为过去 12 个月收益绝对值均值；"
                "规模代理为过去 36 个月均价水平（明确标注为价格代理而非真实市值）。"
                "因子在每个截面上做 1%% 双侧去极值。")

    if part_key == "method":
        return ("被解释变量为未来 %d 个月累计收益。估计方程为：\n\n"
                "fwd_ret(i,t+%d) = α_i + β'X(i,t) + ε(i,t)\n\n"
                "其中 α_i 为个股固定效应，标准误在个股层面聚类，"
                "因为时间序列相关下聚类稳健标准误更合适，t 统计量据此计算。"
                "实现上采用 Frisch-Waugh-Lovell 组内去均值变换，"
                "与生成虚拟变量在数值上等价，但可在百万级面板上秒级完成。\n\n"
                "当前写作方向：%s%s" % (hz, hz, direction or "（尚未确定）",
                                   _ctl_suffix(strength, tone, style)))

    if part_key == "results":
        tbls = _ensure_table_for(d, spec)
        if not tbls:
            return ("本节需要真实回归结果。当前结果表为空 —— "
                    "请先在右侧「数据运算」面板执行回归，"
                    "或使用实时预览确认系数后再生成本节。不在此处填入任何未经核验的数字。")
        head = "表 1 汇总基准回归与各替代规格的结果。"
        lines = []
        for t in tbls[:3]:
            lines.append("**%s**" % t.get("title", ""))
            for r in (t.get("rows") or [])[:6]:
                lines.append("- %s：系数 %s，t=%s，p=%s" % (
                    r.get("变量", ""), r.get("系数", ""), r.get("t值", ""), r.get("p值", "")))
        return head + "\n\n" + "\n".join(lines)

    if part_key == "robust":
        _ensure_compute_for(d, spec)
        return ("稳健性检验包括四项：一是剔除流动性代理的替代规格；"
                "二是按市场波动率分位划分的高低波动子样本；"
                "三是加入市场收益水平作为反向对照 —— "
                "若市场收益水平本身也「显著」，则说明模型只是捕捉了共同冲击；"
                "四是对比不同因子的 t 统计量，识别信息含量最高的变量。\n\n"
                "当前写作方向：%s%s" % (direction or "（尚未确定）",
                                   _ctl_suffix(strength, tone, style)))

    if part_key == "conclusion":
        return ("本文的结论完全取决于实际回归结果，不预设方向。"
                "在真实结果产出前，此处不写任何结论性判断。\n\n"
                "方向锚（摘要）：%s" % (abs_text[:200] + ("…" if len(abs_text) > 200 else "")))

    return ""


# ------------------------------------------------------------- 词数工具
# 词数按「连续非空白串」计：中文没有空格，这样中文才有意义。

# 词元正则：中文按字计，西文按连续串计。
# 不能用 split() —— 中文没有空格，整段会被算成 1 个词，
# 那样「拖到哪就写到哪」的长度控制就完全失效了。
_WORD_RE = re.compile(
    r"[\u3400-\u4DBF\u4E00-\u9FFF\uF900-\uFAFF\u3040-\u30FF\uAC00-\uD7AF]"
    r"|[A-Za-z0-9]+(?:[.'\u2019\-][A-Za-z0-9]+)*"
    r"|\S"
)


def _word_tokens(text):
    """把文本切成词元，返回 (start, end) 列表。"""
    return [(m.start(), m.end()) for m in _WORD_RE.finditer(text or "")]


def _count_words(text):
    """词数：中文按字、西文按连续串。与前端 wordCountOf 一致。"""
    return len(_word_tokens(text))


def _find_tight_repeat(text, min_len=16):
    """找最长的「紧邻重复块」：text[j:j+L] == text[j+L:j+2L]（两份贴在一起）。

    旧实现按 L 从 120 往下暴力逐字符比对，遇到 >120 字符的长重复会直接漏掉
    （实测模型「从头重写」会产出 141 字符的整段重复），而且长文本上很慢。

    现在用块哈希找候选：
      长度 L >= min_len 的重复块，两份里必然各含一个同样的 min_len 块，
      两块位置之差正好是 L → 先按块哈希收集所有出现位置，
      两两配对得到候选 L，再用「紧邻相等」验证。
    只处理**紧邻**重复（两份贴在一起）。带偏移的重复（中间夹了别的内容）
    多半是正常引文或套话，不动它 —— 实测按「最大等长匹配」删会把 221 字的
    正常文本砍到 31 字。
    返回 (长度, 起点)，没有则返回 (0, -1)。
    """
    n = len(text)
    if n < min_len * 2:
        return (0, -1)
    B = min_len
    occ = {}
    for i in range(0, n - B + 1):
        occ.setdefault(text[i:i + B], []).append(i)
    best = (0, -1)
    for ps in occ.values():
        if len(ps) < 2:
            continue
        if len(ps) > 40:
            ps = ps[:40]
        for a in range(len(ps)):
            pa = ps[a]
            for b in range(a + 1, len(ps)):
                L = ps[b] - pa
                if L < B or L <= best[0]:
                    continue
                if pa + 2 * L > n:
                    continue
                if text[pa:pa + L] == text[pa + L:pa + 2 * L]:
                    best = (L, pa)
    return best


def _find_restart_overlap(text, min_len=24):
    """找模型「重开」造成的偏移重复：text[j:j+L] == text[0:L]。

    实测：模型写到一半会从头重写一遍，于是输出里出现
    「A(54字) + A的前48字」这种**开头重复**。紧邻重复检测抓不到它
    （两份之间不贴在一起），但它会让正文出现整段复读。

    返回 (L, j)：要删掉 text[j:j+L] 这段（它与开头重复）。找不到返回 (0, -1)。
    先用 min_len 长度的块哈希筛候选位置，避免 O(n^2) 暴力。
    """
    n = len(text)
    if n < min_len * 2:
        return (0, -1)
    head = text[:min_len]
    occ = {}
    for i in range(1, n - min_len + 1):
        occ.setdefault(text[i:i + min_len], []).append(i)
    best = (0, -1)
    for j in occ.get(head, []):
        L = 0
        maxL = min(n - j, n)
        while L < maxL and text[j + L] == text[L]:
            L += 1
        if L > best[0]:
            best = (L, j)
    return best if best[0] >= min_len else (0, -1)


def _dedup_repeat(text, min_len=16):
    """去掉模型偶发的紧邻重复（同一段连续出现两次）。

    模型偶尔会把刚写的半句再写一遍，或干脆「从头重写」整段。
    做法：找最长的紧邻重复块，只保留一份。保守：只处理完全相同、紧邻的重复。
    """
    if not text or len(text) < min_len * 2:
        return text
    out = text
    for _ in range(12):
        L, pos = _find_tight_repeat(out, min_len)
        if L <= 0:
            break
        out = out[:pos + L] + out[pos + 2 * L:]
    # 「重开式」复读：模型从头又写一遍 → 删掉与开头重复的那一段
    for _ in range(6):
        L, j = _find_restart_overlap(out, max(min_len, 24))
        if L <= 0:
            break
        out = out[:j] + out[j + L:]
    return out


def _sse_iter(resp, chunks, on_think=None):
    # SSE 按行读。代理可能返回分块到达的字节流，用 readline 累积到换行。
    # on_think: 思考型模型（MiniMax-M2.7）回答前先出 thinking 段 →
    #   这些 token 不进正文，但要**透给前端做进度反馈**，
    #   否则用户看到的是「橙线一直闪、什么都没有」（实测首字要等 8 秒）。
    buf = ""
    while True:
        try:
            raw = resp.readline()
        except Exception:
            break
        if not raw:
            break
        try:
            line = raw.decode("utf-8", "ignore").strip()
        except Exception:
            continue
        if not line:
            continue
        if line.startswith("data:"):
            payload = line[5:].strip()
        elif buf:
            # 上一次没读完的尾巴 + 本次
            payload = (buf + line).strip()
            buf = ""
        else:
            buf = line
            continue
        if payload in ("", "[DONE]"):
            continue
        try:
            ev = json.loads(payload)
        except Exception:
            buf = payload
            continue
        piece = ""
        # ⚠️ 思考型模型：thinking_delta 不是正文，但要让前端知道「模型在动」
        if on_think is not None:
            _dd = ev.get("delta") or {}
            if _dd.get("type") == "thinking_delta" or _dd.get("thinking"):
                try:
                    on_think(_dd.get("thinking") or "")
                except Exception:
                    pass
        # OpenAI 兼容：{"choices":[{"delta":{"content":"..."}}]}
        ch = ev.get("choices")
        if isinstance(ch, list) and ch:
            d0 = ch[0].get("delta") or {}
            piece = d0.get("content") or ""
            if not piece:
                m0 = ch[0].get("message") or {}
                piece = m0.get("content") or ""
            if not piece:
                t0 = ch[0].get("text") or ""
                piece = t0
        if not piece:
            # Anthropic：{"delta":{"text":"..."}} 或 {"content":[{"type":"text","text":"..."}]}
            delta = ev.get("delta") or {}
            piece = delta.get("text") or ""
        if not piece:
            cont = ev.get("content") or []
            if isinstance(cont, list):
                piece = "".join(b.get("text", "") for b in cont if b.get("type") == "text")
            elif isinstance(cont, str):
                piece = cont
        if not piece:
            piece = ev.get("text") or ""
        if piece:
            chunks.append(piece)
            yield piece


STREAM_SYSTEM = """You are the writing engine of an academic paper editor. You continue one section of a Chinese empirical-finance paper.

Rules:
- Continue from the existing text. Never repeat it, never restart, never summarize it.
- Write ONLY the continuation. No headings, no bullet lists, no meta commentary, no quotes around your answer.
- Match the register of the existing text: formal academic Chinese, concrete, no filler.
- Ground every claim in the facts you are given (sample, variables, regression results, references). Never invent numbers, tables or citations that are not given to you.
- If a needed fact is missing, write that the gap exists instead of making one up.
- Keep going until you have written at least the requested number of words.
- Never repeat yourself. Every sentence must add something the previous sentences did not say.
- Do not restate a symbol, a variable name or a phrase you have already written. Each definition appears exactly once.
""".strip()


def _insert_cut(text, at_word, toks=None):
    """插入切点 → 吸附到**词组边界**，绝不切在词组中间。

    实测教训：切点落在「本文使|用 2599」这种半截词上时，
    模型看到半截句子会「脑补补全」，把整段摘要重写一遍，与后文大段重复
    （真实论文 FE111212 的 intro 就是这样被污染的，322 字 vs 干净 313 字）。

    规则：切点向后找最近的标点边界；找不到就向前退到上一个边界；
    都没有就退到最近一个中文连续串的末尾。宁可位置偏几个字，也不能切坏词组。
    """
    text = text or ""
    if not text:
        return 0
    toks = toks if toks is not None else _word_tokens(text)
    if at_word <= 0:
        return 0
    if at_word >= len(toks):
        return len(text)
    cut = toks[at_word][0]
    n = len(text)
    # 切点吸附原则：**就近** + **词组完整**。
    # 之前的实现一味找强边界（。！？），结果切点被吸到很远的地方
    # （实测点击第 30 字 → 前缀只剩 12 字「本节说明研究问题与贡献。」），
    # 插入点离用户点击位置太远，手感不对。
    # 现在：先在点击位置附近找**任意**边界（强优先、弱亦可），保证离点击位置近；
    #       只有附近完全没有边界时，才往远找强边界。
    # **就近吸附**：前后都找，取距离最近的那个边界（与前端 insertCut 完全一致）。
    # 旧实现只「先向后找 40 字」，点击点在句中时切点被甩到后面 25 字处
    # （实测点第 30 字 → 插到「（2007-05–2017-04，）」后，偏离用户点击位置）。
    # 代价函数：距离最近者胜；强边界（。！？）比弱边界（，、：）优先 12 字。
    best = -1
    best_cost = 10 ** 9
    for i in range(cut, min(n, cut + 24)):
        if text[i] in _GROUP_OPEN:
            continue
        if text[i] in _GROUP_END_STRONG:
            c = max(0, (i - cut) - 12)
            if c < best_cost:
                best_cost = c
                best = i + 1
            break
        if text[i] in _GROUP_END_WEAK:
            c = i - cut
            if c < best_cost:
                best_cost = c
                best = i + 1
            break
    for i in range(cut - 1, max(-1, cut - 24), -1):
        if text[i] in _GROUP_OPEN:
            continue
        if text[i] in _GROUP_END_STRONG:
            c = max(0, (cut - i - 1) - 12)
            if c < best_cost:
                best_cost = c
                best = i + 1
            break
        if text[i] in _GROUP_END_WEAK:
            c = cut - i - 1
            if c < best_cost:
                best_cost = c
                best = i + 1
            break
    if best >= 0:
        # 承接词检查（与前端一致）：弱边界后紧跟承接词 → 这一刀劈开了词组
        # → 往后推到下一个边界，避免模型脑补补全把后文重写一遍。
        if (best - 1 >= 0 and text[best - 1] in _GROUP_END_WEAK
                and best < n and text[best] in _CONTINUATION):
            pushed = -1
            for i in range(best, min(n, best + 40)):
                if text[i] in _GROUP_OPEN:
                    continue
                if text[i] in _GROUP_END_STRONG:
                    pushed = i + 1
                    break
                if text[i] in _GROUP_END_WEAK:
                    pushed = i + 1
                    break
            if pushed >= 0:
                return pushed
        return best
    # 附近（±24 字）没有边界 → 放宽到 ±60，只认强边界
    for i in range(cut, min(n, cut + 60)):
        if text[i] in _GROUP_OPEN:
            continue
        if text[i] in _GROUP_END_STRONG:
            return i + 1
    for i in range(cut - 1, max(-1, cut - 60), -1):
        if text[i] in _GROUP_OPEN:
            continue
        if text[i] in _GROUP_END_STRONG:
            return i + 1
    # 更远的强边界（兜底，最多 120 字）
    for i in range(cut - 1, max(-1, cut - 120), -1):
        if text[i] in _GROUP_OPEN:
            continue
        if text[i] in _GROUP_END_STRONG:
            return i + 1
    # 都没有 → 退到最近的中文连续串末尾
    j = cut
    while j < n and _is_cjk(text[j]):
        j += 1
    if j > cut:
        return j
    k = cut
    while k > 0 and _is_cjk(text[k - 1]):
        k -= 1
    return k if k < cut else cut


def _locate_anchor(text, anchor, at_char):
    """在 text 里定位插入点。

    并发插入时，多个请求会**先后**落盘，第二个请求算好的词序号/字符偏移
    在第一个请求写入后就已经漂了（前面多了几十个字）。
    所以插入点必须用**内容锚点**重新定位，而不是用请求时算好的偏移。

    anchor = 插入点之前的最后 N 个字符（前端给，通常 30）。
    做法：在 text 里找 anchor 最后一次出现的位置 → 插入点 = 该位置 + len(anchor)。
    找不到（原文被改过）→ 回退到 at_char。

    返回 (cut, ok)。ok=False 表示没找到锚点，调用方应回退。
    """
    text = text or ""
    if anchor:
        a = anchor[-40:]
        if a and a in text:
            # 取**最后一次**出现 —— 前面的插入不会影响这里，
            # 后面的插入会往后推，取最后一次更接近用户点的那一处
            idx = text.rfind(a)
            if idx >= 0:
                return idx + len(a), True
    if at_char is not None:
        try:
            c = max(0, min(int(at_char), len(text)))
            return c, False
        except Exception:
            pass
    return len(text), False


def _stream_part(d, part_key, direction, upto, spec=None, at=None, at_char=None, anchor=None, upto_chars=None,
                strength=None, tone=None, style=None, free_len=False, on_delta=None,
                on_think=None):
    """真实模型流式续写：把新增的文字一段段吐出来。

    返回 (text_iter, meta)。text_iter 每次产出一个增量片段（可能几百字）。
    元信息 full_text 最终拼出完整稿；paper 落盘在流结束后由调用方做。

    at: 插入位置（词序号）。None = 从文末续写（传统行为）。
        给定 at = 在第 at 个词处**插入**新内容：模型看到的是前 at 个词，
        写出的新段落插到那个位置，原文后面的部分保留在后面。
        这是主人要的「从想要编辑的任何位置都能开始」。
    """
    key = minimax_key()
    if not key:
        raise RuntimeError("缺少 MINIMAX_API_KEY")

    # ⚠️ 2026-11-23 22:3x 修（用户报「一直处于编写中、橙线一直闪、没有输出」）：
    #   **prompt 长度直接决定首字延迟**。MiniMax-M2.7 是思考型模型，
    #   prompt 越长 → 思考越久 → 首字越晚（实测，同一模型同一参数）：
    #     prompt  800 字 → 首字 4.98s（思考 2574 字）
    #     prompt 1500 字 → 首字 5.94s（思考 1462 字）
    #     prompt 3934 字 → 首字 **16.74s**（思考 4074 字）  ← 修复前的生产值
    #   而 MiniMax 侧**忽略** thinking.budget_tokens / thinking.type=disabled，
    #   reasoning_effort=low 也只是部分有效（30s → 20s）。
    #   → 唯一可靠的手段：**把 prompt 收窄**。
    #   收窄到 500 字/侧：足够模型知道「在讲什么、前后文语气」，
    #   又不让它把 2000 字当上下文去逐句推理。
    _CTX_WIN = 500

    topic = d.get("topic") or ""
    abs_text = d.get("abstract") or d.get("abstractAuto") or ""
    prev = (d["parts"].get(part_key) or {})
    existing = prev.get("text") or ""
    full = prev.get("full") or ""

    # 插入模式：拆成 前缀 / 后缀
    # 插入点用**字符偏移**（at_char）+ **内容锚点**（anchor）双重定位。
    # 不用词序号 —— 并发插入时词序号会漂（前面被别人插了几十个字）。
    at_word = None
    prefix = existing
    suffix = ""
    insert_cut_char = None
    if at_char is not None or anchor:
        src = full or existing
        cut_c, _found = _locate_anchor(src, anchor, at_char)
        # 再吸附到词组边界（和前端 insertCut 一致）
        toks = _word_tokens(src)
        at_word = 0
        for i, tk in enumerate(toks):
            if tk[0] >= cut_c:
                at_word = i
                break
        else:
            at_word = len(toks)
        if at_word >= len(toks) and cut_c >= len(src):
            # 位置在文末 → **仍然是「插入」**，只是插在末尾。
            # ⚠️ 2026-11-23 修（数据丢失级 bug）：
            #   原来这里设 at_word = None → handler 走「续写整节」分支
            #   → text = _truncate_words(full, upto) → **把整节正文截到 upto 字**
            #   → 实测 results 节 17 字被截成 15 字，新生成的内容也一起没了。
            #   根因：把「插入点在文末」误判成「续写请求」。
            #   现在：保持插入语义（at_word = len(toks)，切点 = 文末），
            #   handler 走插入分支 → full = src + cont，原文一字不动。
            prefix = src
            suffix = ""
            at_word = len(toks)      # 非 None → handler 识别为插入
            insert_cut_char = cut_c  # 保留切点（= 文末），供 handler 重新定位
        else:
            # **不再二次吸附到标点边界**（2026-11-22，主人要求「严格按点击位」）
            # 旧实现：cut_c = _insert_cut(src, at_word, toks)
            #   → 前端已经算好的精确字符偏移被改写，切点被甩到最近标点（实测偏 120px / 17 字）。
            #   而且前端 DOM 里槽画在精确位置、后端却插在别处 → 文字跳位。
            # 现在：切点 = 前端给的精确字符偏移，**一字不差**。
            # 前端的 insertCut 只在「拿不到精确偏移」时作兜底。
            insert_cut_char = cut_c
            prefix = src[:cut_c]
            suffix = src[cut_c:]
    elif at is not None:
        # 旧路径（词序号）
        try:
            at_word = max(0, int(at))
        except Exception:
            at_word = None
        if at_word is not None:
            toks = _word_tokens(full or existing)
            if at_word <= 0:
                prefix = ""
                suffix = full or existing
            elif at_word < len(toks):
                cut = _insert_cut(full or existing, at_word, toks)
                insert_cut_char = cut
                prefix = (full or existing)[:cut]
                suffix = (full or existing)[cut:]
            else:
                prefix = full or existing
                suffix = ""
                at_word = None

    # 给模型看的是**当前可见正文**（已被拖动截断的那部分），模型从它后面接着写
    # 已有 full 且够长 → 直接从 full 里截，不必调模型
    # ⚠️ 长按路径前端传 upto=0（无字数概念）→ 这个判断会恒真 →
    #   直接返回旧缓存、根本不调模型。free_len 时必须跳过它。
    # ⚠️ 2026-11-23 修（用户报「生成文字一直不出来」）：
    #   原来这里 _count_words(full) >= upto 就直接返回 source:"cache"。
    #   但 **upto 的语义是「本轮再写多少词」，不是「本节总词数上限」**。
    #   实测：results 节已有 67 词，前端请求 upto=30 → 67>=30 → 走缓存分支
    #   → 返回旧文本、**一个模型 token 都没生成** → 界面一个字都不出来。
    #
    #   修法：只有当 upto 明确表示「整节总目标」且确实已达标时才算命中。
    #   而 upto 是**增量**语义 → 这里不该用 upto 比较已有长度。
    #   真正的"已写满"判断交给 handler（落盘时按 upto 截断），
    #   这里一律走模型，让模型真的写。
    #
    #   保留一个唯一合理的缓存场景：增量语义下 upTo<=0（无字数要求）
    #   且全文已存在 —— 但那也要模型续写，不能返回旧文。
    #   → 结论：**删除整个缓存短路分支**。

    need = max(1, int(upto or 0))
    if free_len:
        # 长按路径（FAN HUD 选定方向后生成）：**不设任何字数指标**
        # （主人 2026-11-23：「严格匹配所选方向的内容，对字数没要求」）。
        # ⚠️ 历史踩过的坑，这里记清楚别再犯：
        #   · 原来 need=150 / want=300 → prompt 里写「控制在 300 字以内」
        #     → 模型把它当硬上限，且字数成了比方向更醒目的指令 → 产出被字数绑架。
        #   · 更早 want=211 → 模型产出 534~981 字，且**内容发散**到不像在回答方向。
        #     根因不是字数本身，而是「写作方向」只是 ctx 里一行普通说明 → 模型自由发挥。
        # 现在：**彻底不给字数**。唯一的内容要求是「方向」，
        #   方向在 ctx 最前面写成硬约束（见下方 ctx.insert(0, ...)）。
        need = 0
        want = 0
    else:
        # ⚠️ 2026-11-23 修：**目标字数太小 → 模型会写一句超长的长句**，
        # 而长句内部没有可切点 → 落盘严重超字数（实测 target=13 → 落 43 字，+231%）。
        # 根因：只给了「字数区间」，没告诉模型该写**几句**。
        # 修：按目标反推句数 —— 中文一句完整句约 18~26 字，
        #     target 越小要的句数越少，但至少要 1 句（半句不可接受）。
        # 1 句时提示「写一个完整短句」，让模型主动把句子写短。
        pass
    # 目标：比 upto 多写一些，模型输出长度不可控 → 多要一点再截
    # 2026-11-22：原来 want = need*1.6+40（要 10 字却让模型写 56 字）→
    # 落盘再硬截 → 必然腰斩在半句中间。收窄到接近目标，给截断留余地。
    want = int(need * 1.3) + 16

    ctx = []
    # ---- 长度指令（主人：长按路径「不用在乎字数，符合要求更重要」；
    #      拖动路径「要对上字数」）----
    if free_len:
        # 长按路径：**完全不提字数**。
        # 主人 2026-11-23：「对字数没要求」→ 任何数字（哪怕是"上限"）
        # 都会变成模型的锚点，把内容拉向凑字数。
        # 只要求：围绕方向、写完整、自成段。
        ctx.append("长度：不限，按内容需要自然展开；"
                   "写到内容完整为止，不要为凑篇幅扩写或重复。"
                   "必须自成一段、语义完整，不要写半句")
    else:
        # 拖动路径：字数是硬指标。
        # ⚠️ 2026-11-23 实测（真模型打 4 组措辞 × 3 次，共 12 次）：
        #   措辞「目标 N 字 + 写 1 个完整短句 + 以句号收尾」→ 模型产出 13/13/14 字，
        #   末字全是「。」，**服从度极好**。
        #   但当时端到端仍落 32 字 → 因为**方向**诱发模型罗列字段名
        #   （方向「补上具体的量级与口径」→ 模型写「回归结果按变量、系数、聚类标准误…」）。
        #   → 长度指令要给，**还要显式禁止罗列**。
        _lo = max(4, int(need * 0.7))
        _hi = int(need * 1.3)
        _sents = 1 if need <= 26 else (2 if need <= 52 else (3 if need <= 90 else 4))
        _sent_txt = {1: "写 **1 个完整短句**（句子要短，别写成长句）",
                     2: "写 **2 个完整的短句**（每句都短，用句号分开）",
                     3: "写 **3 个完整的短句**（每句都短，用句号分开）",
                     4: "写 **4 个左右完整的短句**（每句都短，用句号分开）"}[_sents]
        ctx.append("长度：目标 %d 字（%d~%d）；请%s；每句写完整、以句号收尾；"
                   "**不要罗列变量名或表里的条目**，要写的是**一句有判断的论证句**"
                   % (need, _lo, _hi, _sent_txt))
    # ---- P2 强度=方法权重 ----
    # 权重是主人手动调的「哪个统计方法是这篇的主方法」。
    # 权重 2 = 重要，权重 3 = **主方法**（必须以它为准来写）。
    # ⚠️ 只写进 prompt、不影响真实计算（回归跑的是固定规格）→
    #   所以措辞必须是「叙述时以哪个方法为主」，
    #   绝不能让模型以为数值来自那个方法（那是编造）。
    _mw = d.get("methodWeights") or {}
    if isinstance(_mw, dict) and _mw:
        _mlabel = dict(COMPUTE_METHODS)
        _items = []
        for _k, _w in sorted(_mw.items(), key=lambda kv: -int(kv[1] or 1)):
            try:
                _wi = int(_w)
            except Exception:
                continue
            if _wi <= 1:
                continue
            _items.append((_mlabel.get(_k, _k), _wi))
        if _items:
            _parts = []
            for _lab, _wi in _items[:3]:
                _parts.append("%s（权重 %d%s）" % (
                    _lab, _wi, "，**本节的主方法**" if _wi >= 3 else ""))
            ctx.append("统计方法侧重：%s。"
                       "叙述时请以这些方法的口径为准；"
                       "**不要把某个方法的结果说成另一个方法的**" % "；".join(_parts))
    if isinstance(spec, dict):
        _pts = (spec.get("points") or [])
        ctx.append("章节要点：%s" % "；".join(_pts[:3]))
    if abs_text:
        ctx.append("本文摘要：%s" % abs_text[:200])
    if direction:
        if free_len:
            # ⚠️ 长按路径：方向是**唯一的内容要求**，字数不限。
            #   原来的写法（只是 ctx 里一行）模型会自由发挥 → 内容不贴方向
            #   （实测：方向「用一句话说清楚…对定价实践意味着什么」，
            #     产出却是「本文的…表明 ESG 信息尚未被定价所吸收…适合作为后续研究的起点基准」）。
            # 现在：方向提到 ctx 最前，并写成硬约束。
            ctx.insert(0, "【本次写作的唯一目标 —— 必须严格围绕它来写】\n"
                          "%s\n"
                          "上面这条方向是**唯一的内容要求**：本段必须直接、完整地回答它，"
                          "不得写别的话题，不得改写成另一个论点，不得只写背景铺垫。"
                          % direction)
            ctx.append("（上述方向已在上方给出，此处不再重复）")
        else:
            ctx.append("写作方向：%s" % direction)
    # ---- 两级控制（对齐原版 Control = {dimension, position, strength}）----
    # strength = 这个方向对整段的**支配广度**（不是语气！）
    # tone / style = 独立的语气与笔法维度（原版里它们是 angle 选的值，不是 radius）
    if strength in _STRENGTH_PROMPT:
        ctx.append("方向强度：%s —— %s" % (strength, _STRENGTH_PROMPT[strength]))
    if tone in _TONE_PROMPT:
        ctx.append(_TONE_PROMPT[tone])
    if style in _STYLE_PROMPT:
        ctx.append(_STYLE_PROMPT[style])
    refs = d.get("refs") or []
    if refs:
        # ⚠️ 2026-11-23 22:5x：ctx 越短 → 模型思考越短 → 首字越快。
        #   实测拖动路径 thinking 达 3273~14154 字 → 首字 9.7~37.4s。
        #   文献标题给 4 条就够模型知道口径，不必全灌。
        ctx.append("可引用文献：%s" % "；".join(
            (r.get("title") or "") for r in refs[:4]))
    tables = d.get("tables") or []
    if tables:
        cols = []
        for t in tables[:3]:
            cn = t.get("columns")
            if isinstance(cn, dict):
                cn = cn.get("变量")
            if isinstance(cn, list):
                cols.extend([c for c in cn if isinstance(c, str)])
            elif isinstance(cn, str):
                cols.append(cn)
        if cols:
            ctx.append("回归表中的变量：%s" % "、".join(cols[:6]))

    if at_word is None:
        user = (
            "论文主题：%s\n"
            "章节：%s\n"
            "%s\n\n"
            "【已有正文（已经写过的内容，不要重复、不要改写）】\n%s\n\n"
            "请**接着上面最后一句继续往下写**新的内容，写足 %d 个词（中文按字计）。\n"
            "要求：\n"
            "1. 第一句必须承接上面最后一句，不能重新开头、不能复述已有内容；\n"
            "2. 不要重复已有的符号定义或句子；\n"
            "3. 直接输出续写内容，不要任何标题或说明。"
        ) % (topic, part_key, "\n".join(ctx), existing[:1200], want)
    else:
        # 插入模式：把新段落写在该位置，不要重写全文
        # 关键：必须把**后文**也告诉模型。
        # 只给前缀时，模型看到的是半句话，会「脑补补全」→ 把后文重写一遍
        # （实测插入后出现「共 120 个月，194877 个个股-月观测），检验企业可持续性…」
        #  这类与原文大段重复的内容）。
        # 后文为空（插在文末）时不能说「过渡到后文第一句」→ 换个说法。
        # 2026-11-23：插入点在文末现在**仍然是插入**（不再降级为续写），
        # 所以这条 prompt 必须区分「有后文」和「没有后文」两种情况。
        _tail_rule = ("你的最后一句要能自然过渡到后文第一句"
                      "（后文同样可能从半句开始，不要去接续或重复它）"
                      if suffix.strip() else
                      "你插入的这一段要**自成起首、自成收尾**"
                      "（后文为空，你是这一段的结尾，句子必须完整）")
        user = (
            "论文主题：%s\n"
            "章节：%s\n"
            "%s\n\n"
            "【该位置之前已有的正文】\n%s\n\n"
            "【该位置之后已有的正文】\n%s\n\n"
            "请在**前文与后文之间**插入一段**全新的、此前不存在的内容**，写足 %d 个词（中文按字计）。\n"
            "要求：\n"
            "1. 只输出新插入的那一段，不要重写整节，也不要抄前文/后文里的任何句子、"
            "数据或标题名称（硬性要求）；新内容必须是**没写过的**论据、机制解释或论证步骤；\n"
            "2. 前文可能在半句话中间被切断（切点不落在标点边界，属正常）。"
            "**不要补全/续写/解释前文那半句，也不要输出它的任何字**；"
            "你的第一句必须**完整、独立、自成起首**；\n"
            "3. %s；\n"
            "4. 不要重复已有的符号定义；\n"
            "5. **最后必须以句号「。」或分号「；」收尾**（引文括号、冒号、逗号结尾都不算）；\n"
            "6. 直接输出新插入的内容，不要任何标题或说明。"
        ) % (topic, part_key, "\n".join(ctx), prefix[:_CTX_WIN], suffix[:_CTX_WIN], want, _tail_rule)

    # ⚠️ 2026-11-23：改走统一 LLM 层（支持 openai / anthropic 双协议 + 用户自配模型）
    _llm = active_llm()
    if not _llm["key"]:
        raise RuntimeError("缺少 API Key —— 去「设置」填一下")
    if not _llm["model"]:
        raise RuntimeError("未配置模型名")
    _url = _llm_url(_llm["baseUrl"], _llm["protocol"])

    # ⚠️ 2026-11-23 22:3x 修（用户报「一直处于编写中、橙线一直闪、没有输出」）：
    #   MiniMax-M2.7 是**思考型模型** → 回答前先出一段 thinking，
    #   thinking token 和正文**共用同一个 max_tokens 预算**。
    #   配置里 maxTokens=2000 → thinking 吃掉大半 → 正文只剩 ~80 字，
    #   而且首字被拖到 5.4s（实测：预算 2000 → 首字 5.37s / 正文 81 字；
    #   预算 6000 → 首字 2.91s / 正文 105 字 —— 预算越大反而**更快**）。
    #   结论：给思考型模型必须留足预算，否则既慢又短。
    #   这里给生成路径一个下限：free_len（长按，无字数门）用 8000，
    #   拖动路径（按字数）用 max(配置的, 4000)。
    _mt_base = int(_llm["maxTokens"] or 2000)
    _mt_free = max(_mt_base, 8000)
    _mt_drag = max(_mt_base, 4000)

    # ⚠️ 2026-11-23 22:5x 修（用户报「一直处于编写中、橙线一直闪、没有输出」）：
    #   MiniMax-M2.7 是**思考型模型** → 回答前先出 thinking 段。
    #   实测（浏览器时间线探针 + 服务端计时日志）：thinking 一路涨到 **4000+ 字**
    #   都还没开始写正文 → 用户等了 20 秒以上，界面只有橙线在闪。
    #   对策：① 给 thinking 一个**硬预算**（512 token）→ 首字从 20s+ 降到 ~2s；
    #        ② system 里明确要求「直接写正文，别先分析」。
    #   实测对照（同一 prompt）：
    #     不限思考            → 首字 3.49s / 思考 552 字
    #     thinking budget 512 → 首字 1.75s / 思考 427 字
    #     system 加「直接输出」 → 首字 1.42s
    #   生产 prompt 更大（摘要+文献+表格+大纲）→ 不限时思考能涨到 4000+，
    #   所以**必须**同时用预算 + 直接输出提示。
    _THINK_BUDGET = 512
    _SYS_DIRECT = ("\n\nOutput the final paragraph DIRECTLY. "
                   "Do not reason, do not plan, do not analyze - just write the paragraph.")

    def _build_req(u_text, temperature, max_tokens=None):
        _mt = max_tokens or _mt_base
        _sys = STREAM_SYSTEM + _SYS_DIRECT
        if _llm["protocol"] == "anthropic":
            b = {"model": _llm["model"], "stream": True, "max_tokens": _mt,
                 "temperature": temperature, "system": _sys,
                 "messages": [{"role": "user", "content": u_text}]}
            # thinking 预算硬上限（MiniMax 思考型模型）
            b["thinking"] = {"type": "enabled", "budget_tokens": _THINK_BUDGET}
            # ⚠️ 2026-11-23 22:0x：**这个才是真正管用的开关**。
            #   MiniMax 侧**忽略** thinking.budget_tokens（实测仍思考 4550 字），
            #   但认 reasoning_effort。对照实测（同一 prompt）：
            #     无参数              → 首字 3.49s / 思考 552 字
            #     thinking budget 512 → 首字 1.75s（预算被忽略）
            #     thinking disabled    → 首字 0.97s
            #     reasoning_effort=low → 首字 **0.80s** / 思考仅 109 字  ✅
            #   论文正文是「有明确方向的续写」，不需要长推理 →
            #   直接用 low，把思考成本压到最低。
            b["reasoning_effort"] = "low" 
            h = {"content-type": "application/json", "x-api-key": _llm["key"],
                 "anthropic-version": "2023-06-01"}
        else:
            b = {"model": _llm["model"], "stream": True, "max_tokens": _mt,
                 "temperature": temperature,
                 "messages": [{"role": "system", "content": _sys},
                              {"role": "user", "content": u_text}]}
            h = {"content-type": "application/json",
                 "authorization": "Bearer " + _llm["key"]}
        return urllib.request.Request(_url, data=json.dumps(b).encode(), headers=h)

    _mt = _mt_free if free_len else _mt_drag
    req = _build_req(user, 0.4, _mt)

    # 模型偶尔只吐两三个字就断流（实测整次插入只回「由此」两个字）。
    # 那在正文里就是一句残句 —— 必须重试到有实质内容。
    # **按词计**，不是按字符。
    # 旧实现 min_useful = max(24, need*0.4) 用的是**字符数**：
    # 模型中文吐 13 个字（"据此据此，需要区分三件事："）已经达到 24？
    # 不 —— 13 < 24，本该重试；但 want = need*1.6+40 = 59 时模型写出 13 字就断流，
    # 三次都短 → 全废。实测拖 12 词只得到 "由此说来，" 五个字。
    # 现在：下限按**词数**算（中文一字一词，与前端 wordCountOf 一致），
    # 且要求达到用户实际拖动的目标词数的 60%，不足就带着「已写内容」续写。
    # 门槛 = 用户实际拖动的词数（中文一字一词，与前端 wordCountOf 一致）。
    # 拖 12 词 → 至少要 12 词；低于就重试。
    # 旧值 max(8, need*0.6) 太松（拖 12 词只写 5 个字就放行）。
    min_useful_words = max(6, int(need))
    if free_len:
        # 长按路径：没有字数目标 → 门槛只当**「不是空壳」**的最低保险
        #（模型偶尔只吐两个字就断流，那在正文里就是残句，必须重试）。
        # 这不是字数指标，不会传给模型当目标。
        min_useful_words = 15
    last_text = ""
    chunks = []
    # ⚠️ 2026-11-23 修：**保留最优的那次**，而不是最后一次。
    # 原来三次都失败后返回 last_text（最后一次）—— 而最后一次常常最短/最残。
    # 实测：目标 13 字 → 三次输出分别是「…结果。」(好) /「…（1973」(残) /「…（1973」(残)
    #   → 返回残片 → 落盘 23 字（+10），末尾是引文括号而不是句号。
    # 现在按 (有终止标点, 长度接近目标, 长度) 排序，取最优。
    best = {"text": "", "score": None}
    def _score(txt):
        c = _dedup_repeat(txt or "")
        if not c:
            return None
        w = _count_words(c)
        has_end = c.rstrip()[-1] in _GROUP_END_STRONG
        if free_len:
            # 长按路径没有字数指标 → 只看「是不是一段完整的话」。
            # ⚠️ 原来还有 `over = 1 if w > want*1.6` 这项长度惩罚 → 也是字数锚点。
            # 现在只看：有没有以强标点收尾（成段）。
            return (1 if has_end else 0, 0, 0)
        # 目标长度附近的加分；长度超得越多扣得越多
        return (1 if has_end else 0,
                -abs(w - need),
                min(w, need * 2))
    for attempt in range(3):
        chunks = []
        try:
            resp = urllib.request.urlopen(req, timeout=180)
        except Exception:
            if attempt == 2:
                raise
            continue
        # ⚠️ 2026-11-23 22:2x 修（用户报「一直处于编写中、橙线一直闪、没有输出」）：
        #   原来**把模型输出整个读完才交给上层** → 前端干等 14~16 秒零字节，
        #   界面上只剩橙线空闪（.ins-rule 默认就在闪）→ 用户完全看不到「模型正在写」。
        #   浏览器时间线探针实测：busy=true 持续 16s，queueLen 恒 0、fillTextLen 恒 0，
        #   正文长度纹丝不动；第 16s 才一次性收到 71 字。
        #   现在：**长按路径（free_len）边读边发** → 前端立刻开始逐字显示。
        #   为什么只给 free_len 开：拖动路径（按字数）依赖「读完再判质量、不合格重试」，
        #   那道质量门必须看到完整文本 → 不能中途流式（否则重试无法撤回已发内容）。
        #   长按路径本来就没有字数门（need=0、_too_long 对 free_len 恒 False），
        #   质量兜底由 handler 层的 dedup / 抄写剔除 / 残尾修剪承担。
        if on_delta is not None:
            # ⚠️ 2026-11-23 22:5x 修（用户报「拖动路径生成没成功」）：
            #   原来**只有 free_len（长按路径）走真流式**，拖动路径走「读完再判质量」。
            #   但拖动路径当时**根本调不通**（NameError：_write_think 未定义）→
            #   handler 直接返回错误 JSON → 前端不解析 → 橙线空闪、零输出。
            #   现在：两条路径都真流式。
            #   拖动路径多一步：质量不合格要重试 → 发 **reset** 事件，
            #   前端把这一轮已显示的字撤回，重写新一轮（否则用户会看到
            #   「先出现一段、又被另一段顶掉」的抖动）。
            _t_open = time.time()

            def _live_gen():
                """真流式生成器。
                · 长按路径（free_len）：一次性读完模型输出，边读边发。
                · 拖动路径：每轮读完后做质量判定；不合格 → 发 **reset**
                  （on_delta(None)）让前端撤回这一轮，再升温重试。
                  最多 3 轮，都不合格 → 发**最优的那轮**（best）。
                """
                _attempt = 0
                _cur_req = req
                while True:
                    _n = 0
                    chunks[:] = []
                    _round_text = []
                    try:
                        _r = urllib.request.urlopen(_cur_req, timeout=180)
                    except Exception:
                        if _attempt >= 2:
                            return
                        _attempt += 1
                        continue
                    for _pc in _sse_iter(_r, chunks, on_think=on_think):
                        _n += 1
                        if _n == 1:
                            try:
                                DIAG.info("流式首个 delta", ms=int((time.time() - _t_open) * 1000))
                            except Exception:
                                pass
                        _round_text.append(_pc)
                        try:
                            on_delta(_pc)
                        except Exception:
                            pass
                        yield _pc
                    try:
                        DIAG.info("流式读完", ms=int((time.time() - _t_open) * 1000), chunks=_n)
                    except Exception:
                        pass
                    if free_len:
                        return
                    # ---- 拖动路径：质量判定 ----
                    _lt = "".join(_round_text)
                    _lc = _dedup_repeat(_lt)
                    _lend = bool(_lc) and _lc.rstrip()[-1] in _GROUP_END_STRONG
                    _ltoo = _count_words(_lc) > int(need * 1.8)
                    _lok = (_count_words(_lc) >= min_useful_words
                            and _lend and not _ltoo)
                    _sc = _score(_lt)
                    if _sc is not None and (best["score"] is None or _sc > best["score"]):
                        best = {"text": _lt, "score": _sc}
                    if _lok:
                        return
                    # 不合格 → 撤回这一轮，让前端清掉已显示的字
                    try:
                        on_delta(None)   # None = reset
                    except Exception:
                        pass
                    if _attempt >= 2:
                        # 三轮都不合格 → 把最优的那轮再放出来
                        _bf = best["text"] or _lt
                        if _bf:
                            try:
                                on_delta(_bf)
                            except Exception:
                                pass
                            yield _bf
                        return
                    _attempt += 1
                    # 升温 + 明确指出上次的问题
                    _got = _count_words(_lt)
                    if _ltoo:
                        _ns = 1 if need <= 26 else (2 if need <= 52 else (3 if need <= 90 else 4))
                        _fix = ("【重要】上一次写了 %d 字，**太长**（目标只有 %d 字）。"
                                "这次必须**大幅压缩**：只写 **%d 个完整短句**，共约 %d 字，"
                                "每句都要短（20 字左右）；不要展开解释、不要罗列变量或条目、"
                                "不要写成一条长句；以句号收尾。\n"
                                % (_got, need, _ns, need))
                    else:
                        _fix = ("【重要】上一次只写出了很短的内容（%d 词），"
                                "这次必须写足 %d 词以上的中文内容，"
                                "并且每一句都要写完整（以句号/分号收尾），"
                                "不要出现半截句子；直接输出内容本身，不要任何说明。\n"
                                % (_got, min_useful_words * 2))
                    _cur_req = _build_req(user + "\n\n" + _fix,
                                          0.4 + 0.15 * (_attempt + 1), _mt)
            return _live_gen(), {"source": "model", "chunks": chunks,
                                 "prefix": prefix, "suffix": suffix,
                                 "at": at_word, "cutChar": insert_cut_char}
        # 先把流读完（要判断长度），再决定是否重试
        pieces = list(_sse_iter(resp, chunks))
        last_text = "".join(pieces)
        _sc = _score(last_text)
        if _sc is not None and (best["score"] is None or _sc > best["score"]):
            best = {"text": last_text, "score": _sc}
        # 长度够 **且** 是一个完整的语义单元（以强终止标点收尾）。
        # 只看长度不够：模型可能吐 20 个字但没有句号（「…提供了较为完」），
        # 那在正文里就是个半截词组（实测拖 12 词 → 落盘「既有经验研究已为收益可预测性提供了较为完」）。
        # 主人要求：「每次生成必须形成完整语义单元：中文至少完整词组」。
        _clean = _dedup_repeat(last_text)
        _has_end = bool(_clean) and _clean.rstrip()[-1] in _GROUP_END_STRONG
        # ⚠️ 2026-11-23：**太长的也要拦**（这才是「对上字数」的根因）。
        # 原来只拦「太短」（>= min_useful_words）→ 模型写了一句 61 字的长句
        # （只有一个末尾句号 → 无切点）→ 被直接接受 → 落盘 61 字（目标 13，+369%）。
        # 裁剪救不了：长句内部没有标点可切。
        # → 必须**在生成层**拒绝它并重试，让模型写成短句。
        _too_long = (not free_len) and _count_words(_clean) > int(need * 1.8)
        if _count_words(_clean) >= min_useful_words and _has_end and not _too_long:
            # 内容够 → 把已读到的内容作为流返回
            def _replay(pcs=pieces):
                for pc in pcs:
                    yield pc
            return _replay(), {"source": "model", "chunks": chunks,
                               "prefix": prefix, "suffix": suffix,
                               "at": at_word, "cutChar": insert_cut_char}
        # 不合格 → 重试：升温提高多样性，**并明确指出这次的问题**
        # ⚠️ 2026-11-23：原来这里还有一行 body["temperature"] = ...，
        # 改走统一层后 body 已不存在 → 必然 NameError。已删除（温度改由
        # _build_req(user, 0.4 + 0.15*(attempt+1)) 传）。
        _got = _count_words(last_text)
        if _too_long:
            # 按 need 反推该写几句（中文一句完整句约 18~26 字）
            _ns = 1 if need <= 26 else (2 if need <= 52 else (3 if need <= 90 else 4))
            _fix = ("【重要】上一次写了 %d 字，**太长**（目标只有 %d 字）。"
                    "这次必须**大幅压缩**：只写 **%d 个完整短句**，共约 %d 字，"
                    "每句都要短（20 字左右）；"
                    "不要展开解释、不要罗列变量或条目、"
                    "不要写成一条长句；以句号收尾。\n"
                    % (_got, need, _ns, need))
        elif free_len:
            # 长按路径：不提字数，只要求「写完整」。
            _fix = ("【重要】上一次的内容不完整或不完整段。"
                    "这次请围绕指定方向写一段**完整的中文内容**，"
                    "每一句都要写完整（以句号/分号收尾），"
                    "长度按内容需要自然展开；"
                    "直接输出内容本身，不要任何说明。\n")
        else:
            _fix = ("【重要】上一次只写出了很短的内容（%d 词），"
                    "这次必须写足 %d 词以上的中文内容，"
                    "并且每一句都要写完整（以句号/分号收尾），"
                    "不要出现半截句子；直接输出内容本身，不要任何说明。\n"
                    % (_got, min_useful_words * 2))
        req = _build_req(user + "\n\n" + _fix, 0.4 + 0.15 * (attempt + 1), _mt)
    # 三次都没达标 → 返回**最优**的那次（不是最后一次）
    _final = best["text"] or last_text
    pieces = [_final] if _final else []
    def _replay2(pcs=pieces):
        for pc in pcs:
            yield pc
    return _replay2(), {"source": "model", "chunks": chunks,
                        "prefix": prefix, "suffix": suffix,
                        "at": at_word, "cutChar": insert_cut_char}



def _split_emit(text, max_len=16):
    """把整理好的最终文本切成小片，供前端逐字渲染。

    前端每帧写 2 个字，所以切片只是让 SSE 分段到达（观感上"在写"），
    最终拼接结果 == 落盘内容（不多不少）。
    """
    out = []
    buf = ""
    for ch in (text or ""):
        buf += ch
        if len(buf) >= max_len or ch in _GROUP_END_STRONG:
            out.append(buf)
            buf = ""
    if buf:
        out.append(buf)
    return out


def _truncate_words(text, n, group=True, max_extra=40):
    """截断到前 n 个词。保留原文格式（换行不被压平），去掉尾部多余空白。

    英文：词元正则本身就按完整单词切（mom12 / don't / e-mail 都不会被切半）。

    中文：按字计词会把词组切坏（如「显著为负」被截成「显著为」）。
    group=True 时向后扩到最近的标点边界，让词组写完整 —— 这也是主人要求的
    「每次生成要生成到完整的一个词组」。
    """
    text = text or ""
    if n is None:
        return text.rstrip()
    try:
        n = int(n)
    except Exception:
        return text.rstrip()
    if n <= 0:
        return ""
    toks = _word_tokens(text)
    if len(toks) <= n:
        return text.rstrip()
    if n <= 0:
        return ""
    cut = toks[n - 1][1]
    if group:
        cut = _group_end(text, cut, max_extra=max_extra)
    return text[:cut].rstrip()


# 中文语义单元的边界标点 —— 截断时向前扩到这些位置，词组才完整。
# 分强弱两级：
#   强边界（句子真的结束）：。！？；… 以及换行
#   弱边界（只是停顿，句子还没说完）：，、：）】》」』—
# 弱边界**不能**作为一次生成的结尾 —— 截在「第一层是基准回归：」这种地方，
# 读者拿到的是半句。实测 abstract 就出现过这种残留。
# 2026-11-22 扩：正文是中英混排，**半角标点必须算边界**。
# 旧表只有全角 → 英文短语「Firm Value: Evide》」里找不到任何边界 →
# 切点被甩到几十字外（实测点击处偏 17 字 / 120px）。
# 半角在正文里占 159 处（几乎与全角 184 处持平），不能当不存在。
# 英文句号 '.' 也必须进强边界，否则整段英文等于没有句边界。
_GROUP_END_STRONG = "。！？；…\n\r.!?;"
# 半角冒号 ':' 必须补 —— 正文里 "Firm Value: Evide" 用半角冒号，
# 漏了它切点会飘到后面的 '》'（实测 120px）。
_GROUP_END_WEAK = "，、：）】》」』—《〉“”‘’．,()[]{}:;\"'"
_GROUP_END = _GROUP_END_STRONG + _GROUP_END_WEAK

# **开括号/开引号不做边界**（2026-11-22）
# 切在「《」后面 → 新段落插进标题括号中间，排版坏掉：
#     《[新段落]ESG and Firm Value》
# 只认**闭合**标点。必须与前端 FARS_GROUP_OPEN 完全一致
#（前后端要算出同一个切点，否则 DOM 插入位置和后端实际插入位置错位）。
_GROUP_OPEN = "《〈“‘（【[{("


# **承接词**（2026-11-22 加）—— 必须与前端 FARS_CONTINUATION 完全一致
# 切点后面紧跟承接词 → 这一刀把词组劈开了 → 模型会脑补补全、把后文重写一遍
# （实测「《...》为题」被劈成「《...》|为题」，
#   模型写出「…提示本文所检验的证据来自」接上后文「为题」→「证据来自为题」）。
# 规则：切点是**弱边界**且下一个字是承接词 → 往后推到下一个边界。
# 强边界（。！？）后面即使有承接词也接受（那是新句子开头）。
_CONTINUATION = "的是为而及与和或却则且也就才只再又还便等者以据由对从"


def _group_end(text, upto_chars, max_extra=40):
    """把 upto_chars 扩/缩到最近的**词组边界**（标点），保证不切坏词组。

    优先向后扩到下一个标点（如「…解释；」）。如果后��够远没有标点
    （模型正在写一个长句），就**向前退**到上一个标点 —— 宁可这段少写几个字，
    也不能把「在以多因」这种半截词组留给读者。
    主人的原话：「每次生成要生成到完整的一个词组」。
    """
    n = len(text)
    # 1) 优先向后扩到**强边界**（句子结束）。
    #    注意：max_extra 很小时（主人只要 N 个字），这里必须**严格受限**，
    #    否则会为了凑一个句号而多写一倍（实测拖 25 字 → 落盘 49 字）。
    #    实测：模型被 max_tokens 截断时，残尾后面往往还有下一个句号，
    #    向前扩到它比退回去更划算（既完整又不浪费内容）。
    _strong_win = max_extra * 5 if max_extra >= 20 else max_extra
    for i in range(upto_chars, min(n, upto_chars + _strong_win)):
        if text[i] in _GROUP_END_STRONG:
            return i + 1
    # 2) 落点本身就是强边界 → 直接用
    if 0 <= upto_chars < n and text[upto_chars] in _GROUP_END_STRONG:
        return upto_chars + 1
    # 3) 向前退到**强边界**——窗口放宽到 120 字。
    #    这一步治的是「模型输出到一半被截断」：残尾形如
    #    「……指出不同ESG评级机构因」，中文串兜底会把它留下（半截词组），
    #    退到上一个句号才是完整语义单元。
    for i in range(upto_chars - 1, max(-1, upto_chars - 120), -1):
        if text[i] in _GROUP_END_STRONG:
            return i + 1
    # 4) 全段没有强边界 → 退到弱边界（，、：）等停顿符）
    for i in range(upto_chars, min(n, upto_chars + max_extra)):
        if text[i] in _GROUP_END_WEAK:
            return i + 1
    for i in range(upto_chars - 1, max(-1, upto_chars - max_extra), -1):
        if text[i] in _GROUP_END_WEAK:
            return i + 1
    # 5) 整段根本没有标点 → 退到**中文串的整段末尾**。
    # 「我们使」是半截词，「我们使用」才完整；中文没有空格可依，
    # 只能按连续汉字串的自然边界收尾。
    j = upto_chars
    limit = min(n, upto_chars + max_extra)
    while j < limit and _is_cjk(text[j]):
        j += 1
    if j > upto_chars:
        return j
    return upto_chars


def _copied_span(generated, existing, min_len=24):
    """检测生成内容里**整段抄自**原文的部分。

    起因：插入模式把后文也给了模型，模型会直接抄后文里的句子
    （实测插入后正文出现「共 120 个月，194877 个个股-月观测），检验 企业可持续性…」
    —— 这段是原摘要里的，不是新写的）。

    做法：把原文按句切开，看有多少连续句子**原样**出现在生成内容里。
    命中就返回那几句，供调用方丢弃。min_len 太短会误伤正常引用。
    """
    g = generated or ""
    if len(g) < min_len:
        return ""
    # 用「最长公共子串」而不是按句切分 ——
    # 抄来的片段常常**跨句号**（实测「…共 120 个月，194877 个个股-月观测），检验 企业可持续性…」
    # 在原文里是一整句，但模型抄时从中间开始，按句切分对不上）。
    # 做法：把原文按 min_len 长度的块哈希，找出在生成内容里也出现的块，
    #       再向两侧扩展成最长公共子串。
    ex = existing or ""
    n = len(ex)
    if n < min_len:
        return ""
    B = min_len
    occ = {}
    for i in range(0, n - B + 1):
        occ.setdefault(ex[i:i + B], []).append(i)
    best = (0, 0, 0)   # (长度, ex 起, g 起)
    for i in range(0, len(g) - B + 1):
        blk = g[i:i + B]
        for j in occ.get(blk, []):
            # 向两侧扩展
            L = 0
            while (i - L - 1 >= 0 and j - L - 1 >= 0
                   and g[i - L - 1] == ex[j - L - 1]):
                L += 1
            R = 0
            while (i + B + R < len(g) and j + B + R < n
                   and g[i + B + R] == ex[j + B + R]):
                R += 1
            total = L + B + R
            if total > best[0]:
                best = (total, j - L, i - L)
    if best[0] < min_len:
        return ""
    return ex[best[1]:best[1] + best[0]]


# ---------------------------------------------------------------- 段落切分
# ⚠️ 2026-11-23 23:2x（主人反馈「每个部分都是很长一段，没有分段」）：
#   根因三层：
#     ① prompt 明写「必须自成一段」；
#     ② 落盘拼接 `full = base + cont` 直接接，无段落边界；
#     ③ 模型输出本身不带换行（学术写作习惯 → 一段到底）。
#   前端渲染是 `esc(text)` + `white-space:pre-wrap` → **文本里有 \n\n 就能显示成段**。
#   → 修在**文本层**：落盘前把长文本按句切成段，插入 \n\n。
#
#   为什么不在 prompt 里要求模型输出换行？
#     prompt 变长 → 思考变长 → 首字延迟暴涨（实测 800 字 prompt → 首字 4.98s；
#     3934 字 → 首字 16.74s）。分段是**纯后处理**，不该占用模型的思考预算。
#
#   切段规则（保守，避免把公式/引用/列表切坏）：
#     · 只在**句末强标点**（。！？）处切；
#     · 一段至少 _PARA_MIN_CHARS 字，最多 _PARA_MAX_CHARS 字 → 超了就再找下一个句末；
#     · 段末必须是句号级别（。！？），**不切在逗号/分号**（会断句）；
#     · 已有 \n\n 的地方保留（模型自己分段了 → 不动）；
#     · 段落数不为 1 时才动手（本来就是一段 → 不改）。

_PARA_MIN_CHARS = 180     # 理想段长下限
_PARA_MAX_CHARS = 320     # 理想段长上限
_PARA_SOFT_MIN = 90      # 硬下限：实在切不动时，段长至少不低于这个
_PARA_SPLIT_END = "。！？"  # 只在这些字符后切


def _paragraphize(text, part_key=None):
    """把连续长文本切成若干自然段（用 \n\n 分隔）。

    只在句末强标点（。！？）处切；已有空行的段落**逐段各自**处理
    （模型自己分过段的，每段内部可能仍然过长）。
    返回原样的情况：
      · 空文本
      · 切完还是只有一段（切了也没意义）
      · 总长度不足 _PARA_MIN_CHARS（太短，切出来每段只有一句话，反而碎）
      · **摘要（abstract）** —— 学术规范里摘要就是**一整段**，不切
    """
    t = text or ""
    if not t.strip():
        return t
    # ⚠️ 摘要不切分：学术写作规范里摘要是一个连续段落，分段反而不合规。
    if part_key == "abstract":
        return t
    if len(t) < _PARA_MIN_CHARS:
        return t

    # 已有空行 → 逐段各自处理；否则整篇当作一段
    blocks = t.split("\n\n") if "\n\n" in t else [t]
    out_blocks = []
    for blk in blocks:
        blk = blk.strip()
        if not blk:
            continue
        # 已有段落本身够长才需要再切；短段（比如结论锚句）原样保留。
        if len(blk) < _PARA_MIN_CHARS:
            out_blocks.append(blk)
            continue
        out_blocks.append(_paragraphize_block(blk))
    if not out_blocks:
        return t
    res = "\n\n".join(out_blocks)
    # 切完还是一段 → 无变化，返回原文
    if len([x for x in res.split("\n\n") if x.strip()]) <= 1:
        return t
    return res


def _paragraphize_block(t):
    """把**单个**连续块切成多段（内部用）。

    做法：**均衡切分** —— 先按目标段长算出该切成几段，
    再从所有句末标点里挑**离理想切点最近**的那些。
    ⚠️ 早期版本用「贪心 + 尾巴回并」→ 尾巴不够长就把上一段也吞掉，
    结果整块又变回一段（实测 366 字 → 1 段）。
    均衡切分不会：段数由总长决定，切点尽量落在等分位置附近。
    """
    if len(t) < _PARA_MIN_CHARS:
        return t
    # ⚠️ 2026-11-23 23:3x：**列表/文献清单不切段**。
    #   实测 lit 节：块1 是「引用清单（12 条）」，每条文献**一行**（用 \n 分隔），
    #   条目以西文「.」结尾 → 中文句末强标点只有 13 个 →
    #   切分器找不到合适切点 → 整块 2936 字变成一大段。
    #   但它**本来就已经分行**了（不是"一大段"）→ 不该动。
    #   判据：块内含换行 → 已经是列表/条目结构 → 保持原样。
    if "\n" in t:
        return t
    # 找出所有可切点（句末强标点之后的位置）
    cuts = [i + 1 for i, ch in enumerate(t) if ch in _PARA_SPLIT_END]
    if len(cuts) < 2:
        return t

    avg = (_PARA_MIN_CHARS + _PARA_MAX_CHARS) / 2.0
    n = int(round(len(t) / avg))
    n = max(2, n)
    # 段数上限：每段至少 _PARA_SOFT_MIN
    n = min(n, max(2, len(t) // _PARA_SOFT_MIN))

    # 逐段选切点：每次挑离「理想位置」最近、且不违反下限的切点。
    # ⚠️ 2026-11-23 23:3x 修：原来只用「剩余长度够分」做可行性判断 →
    #   会选出一个**离理想位置很远**的切点 → 下一段变得极长（实测 lit 出现 1583 字一段）。
    #   现在：候选切点必须同时满足
    #     ① 本段长度 ≥ _PARA_MIN_CHARS（不能切出碎片段）
    #     ② 剩余长度 ≥ 剩余段数 × _PARA_SOFT_MIN（后面要分得开）
    #     ③ 离理想位置的偏差 ≤ 一段的目标长度（否则不均衡，宁可少切一段）
    chosen = []
    start = 0
    for k in range(n - 1):
        want = start + (len(t) - start) / float(n - k)   # 理想切点
        remain_segs = n - k - 1
        tol = (_PARA_MAX_CHARS - _PARA_MIN_CHARS) + avg / 2.0
        best = None
        best_d = None
        for c in cuts:
            if c <= start:
                continue
            if c - start < _PARA_MIN_CHARS:
                continue
            if len(t) - c < _PARA_SOFT_MIN * remain_segs:
                continue          # 剩下的段分不到下限 → 这个切点不能用
            d = abs(c - want)
            if d > tol:
                continue          # 离理想位置太远 → 不切，宁可少一段
            if best_d is None or d < best_d:
                best = c
                best_d = d
        if best is None:
            break                # 切不动了 → 就切到这儿
        chosen.append(best)
        start = best

    paras = []
    prev = 0
    for c in chosen:
        paras.append(t[prev:c])
        prev = c
    paras.append(t[prev:])
    paras = [p.strip() for p in paras if p.strip()]
    if len(paras) < 2:
        return t
    return "\n\n".join(paras)


def _trim_incomplete_tail(text, window=140):
    """剪掉模型输出末尾**没写完**的残句。

    起因：模型受 max_tokens 限制被硬截断时，末尾会留下
    「……指出不同ESG评级机构因」这种半截句子。
    _truncate_words 只保证「切在词组边界」，管不了「原文本身就是断的」。

    规则：若全文末尾不是强终止标点（。！？；…或换行），
         就退回到最后一个强边界 —— 宁可少给一句，也不给半句。
    """
    t = (text or "").rstrip()
    if not t:
        return t
    if t[-1] in _GROUP_END_STRONG:
        return t
    # 末尾的弱标点（如「，」「：」）也不算数 —— 句子还没结束
    lo = max(-1, len(t) - window)
    for i in range(len(t) - 1, lo, -1):
        if t[i] in _GROUP_END_STRONG:
            return t[:i + 1].rstrip()
    # 整段没有强边界 → 退到弱边界
    for i in range(len(t) - 1, lo, -1):
        if t[i] in _GROUP_END_WEAK:
            return t[:i + 1].rstrip()
    return t


# 片段末尾若是这些虚词，说明切点劈在从句中间 → 切出的不是完整语义单元。
# （实测：「其中一项前置判断是」——「是」后缺宾语）
_BAD_TAIL_CHARS = set("是的和与在为把被而及或且又也就都很更最并等让使对从到给向于之其此该并且则但")


def _weak_cut_near(t, target, hi=None):
    """在 t 内按**弱边界**（逗号、顿号、括号等）切一刀，取最接近 target 的位置。

    返回切出的前半段（**不含**切点字符，避免把标点留一半）。
    没有弱边界时返回 None（交给调用方走硬切兜底）。

    ⚠️ 选点必须**保证切出来的是完整语义单元**，不能只看长度接近 target。
    实测 bug（2026-11-22 19:24）：目标 10 字切在「Berg 等（2022）指出 ESG 评分方法
    之间的分歧。」→ 弱边界只有「（」「）」，最近的那个是「（」在第 5 位 →
    切出「Berg 等」—— 把「等（2022）」这个完整引用劈开了，是病句。

    修法（两道闸）：
      1. **不切在成对括号/引号内部**：跳过配对符号（开括号及其闭括号），
         只认「逗号、顿号、冒号、分号」这类**不改变语义结构**的停顿。
      2. 切点太靠前（< target 的 60%）→ 宁可不动这一刀，交给上层整句返回，
         因为「比目标短一半的完整句」远好于「长度对但被劈坏的半句」。
    """
    if not t:
        return None
    try:
        target = max(1, int(target))
    except Exception:
        target = 1
    if hi is None:
        hi = max(target + 1, int(target * 2.5))

    # 1) 剔除「成对括号/引号」类的弱边界 —— 切在那里会劈开完整结构
    #    （「等（2022）」→「等」+「（2022）」）
    pairs = set("（）()【】[]《》〈〉{}「」『』“”‘’")
    # 2) 只保留「纯停顿」类边界
    pause = set("，、：,;:")            # 逗号 顿号 冒号 分号

    cand = []
    for i, ch in enumerate(t):
        if i + 1 >= len(t):
            continue
        if ch in pause:
            cand.append(i + 1)
    if not cand:
        # 只有成对符号可切 → 一个都不用（会劈坏结构），交上层整句处理
        return None

    within = [c for c in cand if c <= hi]
    pool = within if within else [cand[0]]
    best = min(pool, key=lambda c: (abs(c - target), c))

    # 3) 太靠前 → 不切（宁可整句，也不要被劈坏的半句）
    if best < max(2, int(target * 0.6)):
        return None

    # 4) ⚠️ 2026-11-22 19:26 补：**虚词尾巴检查**
    #    实测 bug：目标 10 字 → 模型写「……，其中一项前置判断是……，……」→
    #    切在逗号处 → 切出「其中一项前置判断是」——
    #    长度对（9/10）但**语义是半句**（「是」后面没了宾语）。
    #    判据：片段末尾若是虚词（是/的/和/与/在/为/把/被/而/及/或…），
    #    说明劈在了从句中间 → 这个切点不合格。
    #    修法：跳过所有「虚词尾巴」的切点；都不合格 → 返回 None（整句返回）。
    while best > 0:
        seg = t[:best].rstrip().rstrip("，、：,;:")
        if seg and seg[-1] not in _BAD_TAIL_CHARS:
            return seg
        # 换下一个切点（更靠后的）
        nxt = [c for c in cand if c > best]
        if not nxt:
            return None
        best = nxt[0]
    return None


def _trim_to_complete(text, target, max_ratio=2.5, min_ratio=0.5):
    """截到**完整语义单元**，长度尽量贴近 target。

    起因（2026-11-22 实测）：主人拖 10 字要一段内容，后端让模型写 ~29 字
    （want = need*1.3+16），再硬截到 10 字 → 落盘
        「Berg 等（2022）指出 ESG 评分方法之间的分」
    ← 末尾「的分」是半截，读起来直接是病句。
    旧做法 _truncate_words(cont, target, group=True, max_extra=6)：
    max_extra=6 太小，**够不到**下一个标点边界 → 只能硬切 → 半截。

    正确做法（2026-11-22 修第一版还有 bug）：**按句子切，取完整句子**，
    不要去「找边界位置」——边界可能在窗口之外（模型那句到 28 字才收尾，
    而 target 只有 10），扫窗口自然找不到。
    步骤：
      1) 按强边界把文本切成**句子单元**（含标点）
      2) 从头累加句子，直到长度 >= 下限 lo → 取这些句子（完整且够长）
      3) 若第一句本身就超上限 hi → 这一句内部按**弱边界**切，取最接近 target 的停顿处
      4) 整段没有强边界 → 同上按弱边界切
    宁可长度与目标有出入，也不要半句话。
    """
    t = (text or "").strip()
    if not t:
        return ""
    try:
        target = max(1, int(target))
    except Exception:
        target = 1
    n = len(t)
    lo = max(1, int(target * min_ratio))
    hi = max(lo + 1, int(target * max_ratio))

    # 1) 切成句子单元
    units = []
    start = 0
    for i, ch in enumerate(t):
        if ch in _GROUP_END_STRONG:
            units.append(t[start:i + 1])
            start = i + 1
    if start < n:
        units.append(t[start:])          # 末尾没有强标点 → 残句单元

    # 2) 累加句子直到够长
    #    ⚠️ 2026-11-22 19:29 修：原来「累加到 >= lo 就 break」然后**只返回第一句**，
    #    导致长度严重偏短 —— 实测目标 25 字 → 只落 14 字（比 0.56）。
    #    现在：累加**完整句子**，在不超过 hi 的前提下尽量贴近 target；
    #    每句都是完整语义单元，所以「少几句」是可接受的，**「半句」不可接受**。
    acc = ""
    for u in units:
        if not u.strip():
            continue
        if acc and len(acc) + len(u.strip()) > hi:
            break                      # 再加就超上限 → 就停在这一句
        acc += u
        if len(acc) >= target:
            break                      # 已经够目标 → 收
    if acc and acc.strip():
        # 已拿到完整句子（可能略超 hi，但至少是完整的一段）
        first = units[0].strip() if units else ""
        if first and len(first) <= hi:
            # 返回**累加结果** acc（可能含多句，比目标长一点但完整）；
            # 若 acc 只有一句且远短于目标（< lo*1.5），再看下一句是否也装得下。
            if len(acc) >= lo:
                return acc.strip()
            for u in units[1:]:
                if len(acc) + len(u.strip()) > hi:
                    break
                acc += u
                if len(acc) >= lo:
                    break
            return acc.strip()
        # ⚠️ 2026-11-22 19:22 补：**这一步此前没实现**（docstring 写了但代码漏了）。
        # 实测：目标 10 字 → 模型写 42 字一句 → 旧代码整句返回，落盘 42 字，
        #   字数牌显示 10、实际 42，严重不符（主人明确要「显示实际多少个字符数」）。
        # 修法：第一句本身就超上限 hi → 在**句内**按弱边界（逗号/顿号等）切，
        #   取最接近 target 的那个停顿处 —— 保住完整语义单元，又贴近目标长度。
        if first:
            cut = _weak_cut_near(first, target, hi)
            if cut:
                return cut
        if len(acc) > hi * 2 and first:
            return first
        return acc.strip()

    # 3) 没有完整句子 → 在文本里按弱边界切，取最接近 target 的停顿处
    weak_pos = [i + 1 for i, ch in enumerate(t) if ch in _GROUP_END_WEAK]
    before = [w for w in weak_pos if w <= target]
    after = [w for w in weak_pos if w > target]
    if before:
        return t[:before[-1]].strip()
    if after:
        return t[:after[0]].strip()

    # 4) 兜底：硬切（总比空强）
    return _truncate_words(t, target, group=True, max_extra=6)


def _fit_to_target_raw(text, target, tol_lo=0.6, tol_hi=1.3):
    """把文本裁到**尽量贴近 target 字**，且切在完整语义单元边界。

    为什么要有这把新尺子（2026-11-23 实测）：
      `_trim_to_complete` 在小目标上偏差巨大 ——
        target=10 → 落盘 22 字（+120%）
        target=25 → 落盘 44 字（+76%）
        target=40 → 落盘 44 字（+10%）
      主人要求「原来十字直接拖动生成方法……但要对上字数」→ 必须按字数对齐。

    规则（2026-11-23 实测后定稿）：
      1) 切点候选分两类：**强标点**（。！？；…）= 真正的句末；
         **弱标点**（，、：）」等）= 子句边界
      2) 优先在 **[0.6·target, 1.3·target]** 区间内选**离 target 最近**的强标点
      3) 区间内没有强标点 → 退到区间内的**弱标点**（子句边界，仍是完整语素）
      4) 仍没有 → 取 ≤target 的最近强标点（宁可略短，不要超）
      5) 只有末尾一个句号、且远大于 target（模型写成长句）→
         硬性不超（不向后扩标点），宁可略短
      6) **连接性标点「，、：；」不能当句末** —— 后面一定还有话 = 残句
      7) 提前返回分支（len ≤ target）也要剥掉尾部连接性标点
    """
    t = (text or "").strip()
    if not t:
        return ""
    try:
        target = max(1, int(target))
    except Exception:
        target = 1
    if len(t) <= target:
        # ⚠️ 2026-11-23 修：这里提前返回**绕过了**下面的连接标点清理 →
        #   实测「表4报告基准回归结果：」（11 字 ≤ target 13）原样返回 → 留了悬空冒号。
        # 长度够就直接返回，但末尾不能是「，、：；」这种连接性标点。
        _r = t.rstrip()
        while _r and _r[-1] in "，、：；":
            _r = _r[:-1].rstrip()
        return _r if _r else t

    # ⚠️ 2026-11-23 修：**切点只能用「强标点」**（句号/分号/问号…）。
    # 原来用 _GROUP_END（强弱都算）→ 实测 target=13 时切在**逗号**上，
    # 落盘「图2对应的分组结果显示，」→ 末尾是逗号，句子没说完 = 残句。
    # 残句比字数偏差糟糕得多 —— 主人的「对上字数」是在**完整句**的前提下。
    strong = [i + 1 for i, ch in enumerate(t) if ch in _GROUP_END_STRONG]
    weak = [i + 1 for i, ch in enumerate(t) if ch in _GROUP_END_WEAK]

    # ② 区间内最优（优先强标点）
    lo, hi = tol_lo * target, tol_hi * target
    band = [c for c in strong if lo <= c <= hi]
    if band:
        return t[:min(band, key=lambda c: abs(c - target))].strip()

    # ③ ≤target 的最近**强**标点
    below = [c for c in strong if c <= target]
    if below:
        c = below[-1]
        if c >= 0.5 * target:
            return t[:c].strip()

    # ④ 强标点不在区间内（模型写了一句超长的长句，只有末尾一个句号）→
    #    在**区间内**找弱标点（子句边界）。宁可少一个字，也别超 4 倍。
    #    ⚠️ 2026-11-23 实测：目标 13 字，模型写了一句 61 字的长句
    #       （只有一个末尾句号）→ 强标点候选 [61] → 不在区间 → 原逻辑返回整句 61 字。
    _CLOSERS = "）)》〉』】〕〞〟”’"
    weak_band = [c for c in weak if lo <= c <= hi]
    if weak_band:
        return t[:min(weak_band, key=lambda c: abs(c - target))].strip()
    # ④b 第一个略超的**强**标点（宁可略长，也不要残句）
    above = [c for c in strong if c > target]
    if above:
        # 但如果它超得离谱（> 2.5 倍），先看看有没有弱标点能更贴近
        c0 = above[0]
        if c0 > target * 2.5:
            below_w = [c for c in weak if c <= target and c >= 0.5 * target]
            if below_w:
                return t[:below_w[-1]].strip()
            # ⚠️ 2026-11-23 修（两轮）：
            #   ① 原来用 _truncate_words(..., group=True, max_extra=40)
            #      → group=True 会**向后扩到标点边界** → 目标 13 字、末尾句号在 61 字
            #      → 扩回 61 字 → 兜底等于没兜（实测「+369%」）。
            #   ② 改成「硬切到 target」后 → 切在词中间（实测「…关系组」）= 残词。
            # 现在：**优先取完整子句**（弱标点），上限 2.2 倍；
            #      实在没有子句边界才硬切（此时输入本身就没标点，属模型没写完）。
            #      这条路径是「重试全败后的兜底」—— 正常情况下上游
            #      `_too_long` 判定会让模型重写成短句，不会走到这里。
            _clause = [c for c in weak if 0.5 * target <= c <= target * 2.2]
            if _clause:
                return t[:min(_clause, key=lambda c: abs(c - target))].strip()
            _clause2 = [c for c in weak if c <= target * 2.2]
            if _clause2:
                return t[:_clause2[0]].strip()
            # ③ 连 2.2 倍内的子句都没有（实测：一句 61 字长句，只有 32 字处一个逗号）
            #    → 取**第一个完整子句**，宁可超一点也不要硬切在词中间
            #    （实测硬切得到「…关系组」= 残词，读起来是病句）。
            #    这种情况正常不会走到：上游 `_too_long` 会让模型重写成短句。
            if weak:
                return t[:weak[0]].strip()
            _near = [c for c in (strong + weak) if c <= target]
            if _near:
                return t[:_near[-1]].strip()
            return t[:target].strip()
        return t[:c0].strip()

    # ⑤ 整段没有强标点（模型这句本身没写完）→ 整段保留，别切出残句
    if not weak:
        return t

    # ⑥ 实在没有强标点 → 用弱标点兜底。
    #    ⚠️ 2026-11-23 再修：原来只排除了「，、」，**漏了「：」** →
    #    实测落盘「表4报告基准回归结果：」→ 冒号后必有内容 = 残句。
    #    中文里能真正收尾的弱标点只有**闭合类**：）」》〉』】 和右引号。
    #    「，、：；」都是**连接性**的，后面一定还有话 → 一律不能当切点。
    _CLOSERS = "）)》〉』】〕〞〟”’"
    safe_weak = [c for c in weak if t[c - 1] in _CLOSERS]
    band2 = [c for c in safe_weak if lo <= c <= hi]
    if band2:
        return t[:min(band2, key=lambda c: abs(c - target))].strip()
    below2 = [c for c in safe_weak if c <= target]
    if below2 and below2[-1] >= 0.5 * target:
        return t[:below2[-1]].strip()
    if safe_weak:
        return t[:safe_weak[0]].strip()
    # ⑦a 整段**没有任何终止标点** → 模型没写完。
    #     上层 _stream_part 的重试逻辑（_has_end）会拦这种输出并重试；
    #     这里返回去掉尾巴连接标点的版本，作为最后兜底。
    # ⑦ 模型自己就写成了「……结果：」这种半句 → 去掉尾巴的连接性标点，
    #    至少不留一个悬空的冒号/逗号给人看。
    _r = t.rstrip()
    while _r and _r[-1] in "，、：；":
        _r = _r[:-1].rstrip()
    return _r if _r else t


def _fit_to_target(text, target, tol_lo=0.6, tol_hi=1.3):
    """按字数对齐的尺子（对外接口）—— 在 _fit_to_target_raw 之上做**统一收尾**。

    为什么要有收尾层（2026-11-23 实测踩了 4 轮）：
      裁剪逻辑有 7 条分支，每条都可能返回不同形态的文本，
      其中有些会留下**悬空的连接性标点**（末尾是「，」「：」）。
      「，、：；」在中文里后面一定还有话 → 留在正文里就是**残句**。
      残句比字数偏差糟糕得多 —— 主人的「对上字数」是在**完整句**的前提下。

    这里统一做两件事：
      1) 剥掉末尾的连接性标点（「，、：；」）
      2) 剥完变空 → 退回原文（宁可不动，也不要清空）
    """
    out = _fit_to_target_raw(text, target, tol_lo, tol_hi)
    if out:
        _r = out.rstrip()
        while _r and _r[-1] in "，、：；":
            _r = _r[:-1].rstrip()
        if _r:
            return _r
    return (text or "").strip()


def _is_cjk(ch):
    o = ord(ch)
    return (0x4E00 <= o <= 0x9FFF or 0x3400 <= o <= 0x4DBF
            or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF)


# ------------------------------------------------------------- 大纲派生
# 大纲是「结构」：每节写什么、写多长 —— 不是正文，不含任何事实性结论数字。

BASE_TARGET_WORDS = {
    "abstract": 220,
    "intro": 900,
    "lit": 1200,
    "data": 800,
    "method": 900,
    "results": 1100,
    "robust": 800,
    "conclusion": 500,
}

# 每节依据「哪个词被提到」判定大纲来源
PART_TRIGGER_WORDS = {
    "abstract": ["摘要", "abstract", "研究问题", "本文"],
    "intro": ["引言", "研究问题", "贡献", "动机", "背景", "检验", "本文"],
    "lit": ["文献", "综述", "研究现状", "引用"],
    "data": ["数据", "样本", "变量", "口径"],
    "method": ["方法", "模型", "回归", "识别", "设定", "方程", "假设"],
    "results": ["结果", "系数", "实证", "回归", "表"],
    "robust": ["稳健", "检验", "安慰剂", "子样本", "替代规格"],
    "conclusion": ["结论", "总结", "启示", "含义", "本文"],
}

# 每节默认要点（结构性描述，不是结论）
BASE_OUTLINE_POINTS = {
    "abstract": [
        "交代研究问题与研究对象：本文研究什么、用什么数据",
        "概括识别策略与主要设计选择",
        "概述主要发现的方向，并如实说明与主流预期不一致之处",
    ],
    "intro": [
        "提出研究问题：说明现象与既有解释之间尚存的缺口",
        "梳理本文与相关文献的差异，指明增量落在哪里",
        "列出本文的贡献与整体行文结构",
    ],
    "lit": [
        "按主题线索组织相关文献，区分其研究问题与结论",
        "把每条文献线索对应到本文的实证设计",
        "指出文献尚未覆盖的设定或样本，形成本文的立足点",
    ],
    "data": [
        "交代样本来源、筛选规则与时间区间",
        "逐个说明变量的构造口径与计算方式",
        "说明数据清洗与缺失处理，以及可能的口径局限",
    ],
    "method": [
        "写清被解释变量与估计方程的设定",
        "说明固定效应与标准误的处理方式及其理由",
        "列明识别所依赖的关键假设，并指出在当前设定下不可识别的部分",
    ],
    "results": [
        "先给基准回归结果的组织方式与读表顺序",
        "逐规格报告系数方向与显著性，并保留原始符号",
        "对照主流预期解释方向差异，标注无法解释之处",
    ],
    "robust": [
        "列出稳健性检验清单，并说明每一项针对什么质疑",
        "说明替代规格与子样本切分的具体做法",
        "报告稳健性结论，并说明仍然存在的局限",
    ],
    "conclusion": [
        "总结研究问题、识别策略与主要发现方向",
        "说明研究局限与结论的适用边界",
        "给出后续可推进的研究方向",
    ],
}

# 关键词命中的补充要点（结构性，非事实性）
KEYWORD_OUTLINE_POINTS = [
    ("ESG", "说明 ESG 相关度量在本文口径下的定位，以及识别上的限制"),
    ("动量", "交代动量因子的构造口径与检验方式"),
    ("反转", "交代短期反转因子的构造口径与检验方式"),
    ("波动", "交代波动率口径与状态切分的处理方式"),
    ("流动性", "交代流动性代理的构造与口径局限"),
]


def _outline_source(d, part_key):
    """这一节的大纲依据来自哪里：requirement > abstract > title > default。"""
    trig = PART_TRIGGER_WORDS.get(part_key) or []
    req = d.get("requirement") or ""
    abs_t = d.get("abstract") or d.get("abstractAuto") or ""
    title = d.get("title") or ""
    for src, text in (("requirement", req), ("abstract", abs_t), ("title", title)):
        if not text:
            continue
        low = text.lower()
        for w in trig:
            if w.lower() in low:
                return src
    return "default"


def _outline_points(d, part_key, source):
    """这一节要覆盖的要点（2–4 条）。"""
    pts = list(BASE_OUTLINE_POINTS.get(part_key) or [])
    if source == "requirement":
        pts.insert(0, "以已上传的需求文档为写作依据，先明确本节要回答的具体问题与交付口径")
    elif source == "abstract":
        pts.insert(0, "承接已定稿摘要中的研究问题与口径，保持全节与摘要一致")
    elif source == "title":
        pts.insert(0, "围绕标题界定的研究对象与切入角度组织本节范围")
    corpus = " ".join([d.get("requirement") or "", d.get("abstract") or "",
                       d.get("abstractAuto") or "", d.get("title") or ""]).lower()
    for kw, txt in KEYWORD_OUTLINE_POINTS:
        if kw.lower() in corpus:
            pts.append(txt)
            break
    if len(pts) < 2:
        pts.append("写清本节要交代的核心内容与承接关系")
    return pts[:4]


def _derive_outline(d, spec=None):
    """从需求 / 摘要 / 标题确定性派生一份写作大纲（结构，非正文）。

    规则（确定性，不随机，不编造）：
      1. 大纲只描述「每节写什么、写多长」，不写任何事实性数字
      2. 依据优先级：requirement > abstract（已定稿摘要）> title > default
      3. targetWords 是建议值，不是硬约束
    """
    items = []
    has_req = bool((d.get("requirement") or "").strip())
    for key, label in PARTS:
        src = _outline_source(d, key)
        base = BASE_TARGET_WORDS.get(key, 600)
        target = int(round(base * 1.15)) if has_req else int(base)
        items.append({
            "key": key,
            "label": label,
            "targetWords": target,
            "points": _outline_points(d, key, src),
            "source": src,
        })
    basis = "requirement" if has_req else (
        "abstract" if (d.get("abstract") or d.get("abstractAuto")) else (
            "title" if (d.get("title") or "") else "default"))
    return {
        "items": items,
        "basis": basis,
        "updatedAt": int(time.time() * 1000),
    }


def _clean_ref_line(line):
    """判断一行是不是真正的文献条目，返回清洗后的标题或 None。

    参考文献文件里混有格式说明、核验说明等非文献行，要滤掉。
    """
    t = (line or "").strip()
    if not t:
        return None
    # 必须含年份
    if not re.search(r"\b(19|20)\d{2}\b", t):
        return None
    # 必须含作者姓氏模式："Surname, First" 或 "Surname, A."
    if not re.match(r"^[A-Z][A-Za-zà-ÿ'\-]+,", t):
        return None
    # 排除明显的说明行
    if t.startswith(("格式", "核验", "（", "(", "注", "说明")):
        return None
    if len(t) < 25:
        return None
    return t[:220]


def _dedupe_refs(refs):
    """按 _ref_key 去重 + 按标题主体去重，保留第一次出现的（优先文档来源）。

    两级去重：
      ① _ref_key      —— 作者姓+年份+标题关键词（同一文献不同格式）
      ② _ref_title_key —— 只看标题正文（同一文献不同来源作者顺序不同）
         实测：Ang & Bekaert (2001) 在 OpenAlex 和 SSRN 各一条，
         ①认不出（作者顺序相反）→ ② 认出 → 只留一条。
    """
    seen = set()
    seen_title = set()
    out = []
    for r in refs:
        k = _ref_key(r.get("title"))
        tk = _ref_title_key(r.get("title"))
        if k in seen:
            continue
        if tk and tk in seen_title:
            continue
        seen.add(k)
        if tk:
            seen_title.add(tk)
        out.append(r)
    for i, r in enumerate(out):
        r["key"] = "ref_%d" % i
    return out


def _ref_key(title):
    """文献去重键：作者姓 + 年份 + 标题前若干关键词。

    docx 里的条目和池子里的条目格式不同（"Carhart, Mark M. (1997). On Persistence..."
    vs "Carhart, Mark M. (1997). On persistence..."），仅靠大小写归一化去不掉重。
    """
    t = re.sub(r"\s+", " ", (title or "")).strip().lower()
    m = re.search(r"\b(1[89]|20)\d{2}\b", t)
    year = m.group(0) if m else ""
    author = ""
    m2 = re.match(r"([a-zà-ÿ'\-]+)", t)
    if m2:
        author = m2.group(1)
    # 标题关键词：去掉作者与年份后取前 3 个长词
    rest = re.sub(r"^[^.]*\.", "", t) if "." in t[:80] else t
    words = [w for w in re.findall(r"[a-z]{5,}", rest)][:3]
    return author + "|" + year + "|" + "".join(words)


# ---------------------------------------------------------------
# 自动补齐缺失文献
# 主人 2026-11-23 23:43：「文献检索功能开发，能点击检索就能自己查询现在缺少的文献资料」
#
# 现状：sources.py 已有检索通道（OpenAlex→Crossref→arXiv），audit.py 已有
# 「正文引用了但参考文献表没有」的检测 —— 但两者之间没连线，用户只能手动输关键词。
# 这里把两边接起来：
#   1. _collect_citation_gaps：复用 audit 的锚点/匹配逻辑，列出**正文引用了但 refs 里没有**的文献
#   2. _autofill_refs：拿缺口去检索通道查，命中的条目写进 refs（可顺带下载 PDF）
#   3. _enrich_refs_by_topic：refs 池太空时，按标题/主题检索补足文献综述的底子
#
# 原则：**只加真实检索到的文献，绝不编造**。查不到就如实报"未找到"，不硬塞。

def _collect_citation_gaps(d):
    """扫描正文里 (姓氏, 年份) 形式的引用，列出参考文献表中没有对应条目的。

    完全复用 audit.py 的匹配逻辑（CITE_ANCHOR / _author_chunk / _name_tokens /
    _ref_index / _match_ref），保证和「引用核验」审计结论**口径一致** ——
    审计说缺，这里就说缺，不会两处打架。
    """
    parts = d.get("parts") or {}
    texts = {}
    for k, _lbl in audit_mod.PART_LABELS:
        t = ((parts.get(k) or {}).get("text") or "").strip()
        if t:
            texts[k] = t
    abs_txt = (d.get("abstract") or d.get("abstractAuto") or "").strip()
    if abs_txt:
        texts["abstract"] = abs_txt

    ref_idx = audit_mod._ref_index(d.get("refs"))
    gaps = []
    seen = set()
    for pk, txt in texts.items():
        for sent in audit_mod._sentences(txt):
            for mm in audit_mod.CITE_ANCHOR.finditer(sent):
                year = mm.group(1)
                who = audit_mod._author_chunk(sent, mm.start()).strip(" ,，、;；")
                surnames = audit_mod._name_tokens(who)
                if not surnames:
                    continue
                # 把作者段收敛成「干净的作者串」。
                # ⚠️ 2026-11-24：_author_chunk 修好后可能仍带前导小写尾巴
                #    （"is the classic starting point. Carhart"），
                #    who 直接展示会很怪，且 _match_gap_multi 用 surnames 过滤时
                #    容易带入无关词 → 从**第一个姓氏 token 的位置**截断。
                try:
                    _low = who.lower()
                    _pos = min([_low.find(x.lower()) for x in surnames
                                if _low.find(x.lower()) >= 0] or [0])
                    who = who[_pos:].strip(" ,，、;；")
                except Exception:
                    pass
                hit = None
                for key, ref in ref_idx.items():
                    if audit_mod._match_ref(ref, surnames, year):
                        hit = key
                        break
                if hit:
                    continue
                sig = (tuple(sorted(surnames)), str(year)[:4])
                if sig in seen:
                    continue
                seen.add(sig)
                gaps.append({
                    "part": pk,
                    "who": who,
                    "year": year,
                    "surnames": sorted(surnames),
                    "sentence": sent[:240],
                })
    return gaps


def _fix_author_casing(authors):
    """Crossref 的作者名常是全大写（"EUGENE F. FAMA"）→ 转成正常大小写。

    为什么必须修：audit._match_ref 是**大小写敏感**的正则匹配
    （正文写 "Fama"，refs 里写 "EUGENE F. FAMA" → 匹配不上 →
    审计永远报"正文引用了但文献表没有" → 补齐了还是补不干净的假象）。
    """
    if not authors:
        return authors
    out = []
    for part in authors.split(", "):
        if not part:
            continue
        # 只有"整体全大写"才转（正常的 "J. Smith" 不动）
        letters = [c for c in part if c.isalpha()]
        if letters and all(c.isupper() for c in letters):
            out.append(part.title())
        else:
            out.append(part)
    return ", ".join(out)


def _ref_citation_string(rec):
    """把检索结果 record 拼成 refs 池统一的引用串格式。

    格式必须能被 audit._ref_index 解析：作者段在年份之前 → 姓氏才能被抽出来。
      "Berg, Florian (2022). Aggregate confusion: ... Review of Finance"
    """
    title = (rec.get("title") or "").strip()
    year = rec.get("year") or ""
    authors = (rec.get("authors") or "").strip()
    journal = (rec.get("journal") or "").strip()
    seg = []
    if authors:
        seg.append(_fix_author_casing(authors.rstrip(". ")))
    if year:
        seg.append("(%s)" % str(year).strip())
    seg.append(title)
    if journal:
        seg.append(journal.strip().rstrip(". "))
    return ". ".join(x for x in seg if x)


def _match_gap(rec, gap):
    """检索结果能不能算这条缺口的答案。

    要求：① 年份一致（缺口的年份是硬条件，年份错了等于幻觉引用）
          ② 姓氏出现在标题或作者里（否则是同年份的别的文献）
    """
    if not rec:
        return False
    y = str(rec.get("year") or "")[:4]
    gy = str(gap.get("year") or "")[:4]
    if gy and y and y != gy:
        return False
    blob = ((rec.get("title") or "") + " " + (rec.get("authors") or "")).lower()
    surs = [s.lower() for s in (gap.get("surnames") or [])]
    if surs and not any(s in blob for s in surs):
        return False
    if not (rec.get("title") or "").strip():
        return False
    return True


# 主题 → 英文检索式（检索英文文献库必须用英文关键词，中文检不到）
# 主人 2026-11-23 23:43 的实测教训：直接拿中文标题去查 OpenAlex，
# 返回的是"伊斯兰教节假日""股息 ETF"这类噪声 —— 检索式必须是领域术语。
_TOPIC_KEYWORDS = {
    "ESG": ["ESG firm value", "environmental social governance firm value",
            "corporate social responsibility firm value", "ESG rating divergence"],
    "Stock": ["stock return predictability", "cross-section of stock returns",
              "asset pricing anomalies", "momentum reversal stock returns"],
    "Machine": ["machine learning asset pricing", "machine learning return prediction",
                "predictable characteristics machine learning finance"],
}

# 通用噪声词：出现在标题里也不能算命中
# 相关度门槛：低于这个分的检索结果一律不收（防噪声）
_LIT_MIN_SCORE = 3.0

_LIT_STOPWORDS = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those",
    "study", "studies", "paper", "analysis", "evidence", "review", "using",
    "based", "between", "into", "over", "under", "their", "its", "our",
    "case", "new", "what", "does", "how", "why", "are", "is", "was",
}


def _lit_terms(d):
    """从标题/摘要/文献节里抽出可用于**打分**的英文实词。

    注意：标题可能被截断成半截词（"Stock Return Predictabili"），
    这里做一轮去噪：截断词（末尾 3 个字符里没有元音且长度>5）丢掉。
    """
    blob = " ".join([
        (d.get("title") or ""),
        (d.get("abstract") or d.get("abstractAuto") or ""),
        ((d.get("parts") or {}).get("lit") or {}).get("text") or "",
    ])
    words = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", blob)
    out = []
    for w in words:
        lw = w.lower()
        if lw in _LIT_STOPWORDS or len(lw) < 4:
            continue
        # 截断词检测：结尾 4 个字母里没有元音 → 多半是半截词
        tail = lw[-4:]
        if len(lw) > 5 and not any(c in "aeiou" for c in tail):
            continue
        if lw not in out:
            out.append(lw)
    return out[:24]


def _score_rec(rec, terms):
    """给检索结果打相关性分。

    **硬门槛：查询词必须出现在标题里** —— 否则就是"跟本题无关但恰好共现"的噪声。
    实测：不设门槛时，"Stock Return Predictability" 会返回
    "Assessing the Impact of Islamic Religious Holidays"这种完全无关的文献。
    """
    title = (rec.get("title") or "").lower()
    if not title:
        return 0.0
    hit_title = 0
    hit_other = 0
    for t in terms:
        if t in title:
            hit_title += 1
        elif t in ((rec.get("abstract") or "").lower()):
            hit_other += 1
    if hit_title == 0:
        return 0.0
    score = hit_title * 3.0 + hit_other * 0.5
    # 被引多的更可能是经典文献 —— 轻微加权
    try:
        score += min(float(rec.get("cited_by") or 0) / 200.0, 2.0)
    except Exception:
        pass
    # 标题越短越聚焦，轻微加权
    if 10 <= len(title) <= 120:
        score += 0.3
    return score


# 金融/经济类期刊关键词 —— 用于判断检索结果是否属于本领域。
# 为什么必须有：按作者+年份检索会捞到大量**同姓同年但完全无关**的论文。
# 实测（作者通道, 年份精确匹配）：
#   Jensen 1970 → "Factors in College Education for Indian Students"（教育学）
#   Fama  1992 → "Changes in lipids and polysaccharides during pollen ontogeny"（植物学）
#   Carhart 1997 → "Adaptive phase-distortion correction..."（光学期刊）
# 补错文献比补不上更糟 —— 那等于往参考文献表里塞幻觉引用。
_FIN_VENUE_KEYS = (
    "journal of finance", "journal of financial economics", "financial economics",
    "review of financial studies", "journal of finance", "review of finance",
    "journal of banking", "journal of business", "journal of portfolio management",
    "financial analysts journal", "journal of empirical finance", "finance",
    "economics", "economic", "econometrica", "econometrics", "nber",
    "working paper", "capital markets", "asset pricing", "money, credit",
    "banking and finance", "journal of investment", "investment",
    "annual review", "review of economic studies", "journal of economic",
    "jstor", "ssrn", "research paper", "financial markets", "securities",
    "journal of derivatives", "journal of portfolio", "quarterly journal",
    "journal of public economics", "journal of economic perspectives",
    "management science", "industrial organization", "china", "emerging market",
    "international journal of emerging markets", "journal of business finance",
)

# 非金融领域期刊的强否定信号（命中即排除，优先于白名单）
_NONFIN_VENUE_KEYS = (
    "optics", "optical", "phyto", "plant", "polym", "pollen", "sediment",
    "cardio", "rehabilitation", "audiolog", "auditory", "phonolog",
    "pediatr", "nurs", "education", "college", "indian", "language",
    "linguist", "dentis", "dermatolog", "oncolog", "immunolog",
    "mater", "ceramic", "alloy", "geolog", "soil", "crop", "forest",
    "ecolog", "marine", "bacter", "virus", "vaccine", "clin", "therap",
    "gene", "protein", "neuron", "brain", "heart", "surg", "patient",
)


def _venue_is_fin(journal):
    """判断期刊是否属于金融/经济领域。返回 (是否通过, 理由)。"""
    j = (journal or "").lower().strip()
    if not j:
        return False, "无期刊信息"
    for bad in _NONFIN_VENUE_KEYS:
        if bad in j:
            return False, "期刊属非金融领域（%s）" % journal[:40]
    for good in _FIN_VENUE_KEYS:
        if good in j:
            return True, ""
    return False, "期刊不在金融经济领域（%s）" % journal[:40]


def _author_extra_terms(d, gap):
    """作者通道要配的主题词（按可能性排序）。

    为什么要主题词：CAPM 那篇的真实标题是
    "The Prices of Assets and Risk Premiums" —— 标题里没有 "capital asset pricing"，
    只按 "Jensen 1970" 查只能捞到同名语言学学者。
    带上领域主题词，Crossref 的 query.bibliographic 才会把它顶到前面。
    """
    topic = (d.get("topic") or "").strip()
    tq = _topic_queries(d, 3)
    out = []
    for t in tq:
        if t not in out:
            out.append(t)
    generic = {
        "Stock": ["capital asset pricing", "stock returns"],
        "ESG": ["corporate finance firm value"],
        "Machine": ["predictable returns machine learning"],
    }.get(topic, [])
    for t in generic:
        if t not in out:
            out.append(t)
    return out[:4]


def _pick_author_candidate(recs, gap):
    """从作者通道的结果里挑**最可信**的一条。

    打分（越高越可信）：
      + 期刊属金融/经济领域 → +5（硬门槛：非金融领域直接排除）
      + 被引量 log 缩放      → 经典文献被引上万，冷门同姓论文只有个位数
      + 多作者全名匹配      → +2
      + 标题与主题相关      → +1
    取最高分；同分取被引多的。
    """
    surs = [s.lower() for s in (gap.get("surnames") or []) if len(s) > 2]
    gy = str(gap.get("year") or "")[:4]
    scored = []
    for r in recs or []:
        y = str(r.get("year") or "")[:4]
        if gy and y and y != gy:
            continue
        blob = ((r.get("title") or "") + " " + (r.get("authors") or "")).lower()
        if not surs or not all(s in blob for s in surs):
            continue
        ok, why = _venue_is_fin(r.get("journal"))
        if not ok:
            continue
        sem_ok, sem_why = _semantic_ok(r, gap)
        if not sem_ok:
            continue
        sc = 5.0
        try:
            import math
            sc += math.log10(max(float(r.get("cited_by") or 0), 0) + 1) * 1.5
        except Exception:
            pass
        sc += 1.0
        scored.append((sc, r))
    if not scored:
        return None, "无金融经济领域候选"
    scored.sort(key=lambda x: (-x[0], -float(x[1].get("cited_by") or 0)))
    return scored[0][1], ""


# 语义校验用的停用词（候选文献标题/摘要里出现这些词不算命中）
_SEM_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "these", "those",
    "paper", "study", "evidence", "model", "models", "analysis", "empirical",
    "theory", "framework", "approach", "new", "some", "using", "based",
    "case", "role", "effect", "impact", "effects", "review", "journal",
    "prices", "price", "asset", "assets", "returns", "return", "risk",
    "market", "markets", "stock", "stocks", "theory", "test", "tests",
    "of", "in", "on", "to", "a", "an", "is", "are", "be", "we", "it",
}


def _gap_semantic_terms(gap):
    """从缺口所在句子里抽**语义线索词** → 返回 (硬线索, 软线索)。

    用途：防止"补错文献"。
    实测：正文写「而 Jensen (1970) 的 CAPM 是理论基石」，
    作者通道会返回 Jensen 1970 的另一篇论文
    "Random Walks and Technical Theories"（The Journal of Finance, 被引 493）——
    同一作者、同一年、同一刊物，**但完全不是 CAPM**。
    补上去就是幻觉引用（比补不上更糟）。

    硬线索 = 原文里的**大写缩写**（CAPM / GMM / LSTM / FDR…）
      → 候选文献的标题/摘要**必须**提到它，否则否决。
    软线索 = 其余普通实词（classic / starting point / momentum…）
      → 只做**加分**，不做否决。

    ⚠️ 为什么泛化英文词不能当硬要求（实测 2026-11-24）：
      正文 "Fama and French (1992) is the classic starting point."
      正确答案是 "The Cross-Section of Expected Stock Returns"，
      它的标题里**没有** classic / starting / point ——
      拿这些词当硬要求会把**完全正确**的文献判成不匹配 → 补不上。
    """
    orig = gap.get("sentence") or ""
    gy = str(gap.get("year") or "")[:4]

    # ---- 只取**含本缺口引用**的那一句 ----
    # 根因（实测 2026-11-24）：audit._sentences() **不在英文句号处切分**，
    # 所以
    #   "Fama and French (1992) is the classic starting point. Carhart (1997) adds momentum."
    # 整段被当成一句 → 缺口「Fama and French 1992」的线索词里混进了 'carhart'
    # → 语义校验要求候选提到 carhart → **正确的** Fama-French 那篇被误拒。
    # 这里自己按句末标点切片，锁定含本缺口年份/姓氏的那一片段。
    frag = orig
    if orig:
        frags = [x for x in re.split(r"(?<=[.!?。！？])\s*", orig) if x.strip()]
        if len(frags) > 1:
            surs0 = [x.lower() for x in (gap.get("surnames") or []) if len(x) > 2]
            pick = None
            for fr in frags:
                lfr = fr.lower()
                if gy and gy in fr and (not surs0 or any(x in lfr for x in surs0)):
                    pick = fr
                    break
            if pick is None:
                for fr in frags:
                    if gy and gy in fr:
                        pick = fr
                        break
            frag = pick or orig

    sent = frag
    # 去掉 (年份) 锚点
    sent = re.sub(r"[（(]\s*(?:19|20)\d{2}[a-z]?\s*[)）]", " ", sent)

    # ---- 排除**同一片段里其他引用**的作者名 ----
    # ⚠️ 必须在**替换年份之前**用原文找锚点，否则锚点已被抹掉。
    other_names = set()
    try:
        for _mm in audit_mod.CITE_ANCHOR.finditer(frag):
            _who = audit_mod._author_chunk(frag, _mm.start()).strip(" ,，、;；")
            for _t in audit_mod._name_tokens(_who):
                other_names.add(_t.lower())
    except Exception:
        pass

    # 去掉作者姓氏 token（缺口里已知的姓氏）
    surs = set(g.lower() for g in (gap.get("surnames") or []))
    surs |= other_names
    words = re.findall(r"[A-Za-z][A-Za-z\-]{1,}", sent)
    hard, soft = [], []
    for w in words:
        lw = w.lower()
        # 姓氏是**检索键**，不是语义线索 → 必须排除。
        # 否则缺口「Fama and French (1992)」的线索词就是 fama/french，
        # 只能匹配到作者名里含这两个词的文献（= 同作者过滤的重复），
        # 语义校验就失去了"是不是同一篇"的判别力。
        if lw in surs or lw in _SEM_STOP:
            continue
        if len(lw) < 3:
            continue
        # 全大写缩写 = 硬线索（CAPM / GMM / IV / DID …）
        # ⚠️ 只认**原文就是大写**的缩写；句首大写的普通词（Classic）不算。
        if w.isupper() and len(w) >= 3:
            if lw not in hard:
                hard.append(lw)
        elif lw not in soft:
            soft.append(lw)
    return hard[:4], soft[:6]


def _semantic_ok(rec, gap):
    """候选文献是否满足缺口的语义要求。

    规则：
      - 缺口句子里有语义线索词时，候选的标题/摘要**必须**至少命中一个
      - 缺口句子没有可用线索词（纯"某某(年份)"）→ 放行（靠领域+被引量把关）
    """
    hard, soft = _gap_semantic_terms(gap)
    if not hard and not soft:
        return True, ""
    # ⚠️ 必须**大小写不敏感**：Crossref 的作者/标题常是全大写
    #    （"EUGENE F. FAMA" / "The Cross‐Section of Expected Stock Returns"），
    #    大小写敏感匹配会全部落空 → 明明能补对的文献被误拒。
    blob = ((rec.get("title") or "") + " " + (rec.get("abstract") or "") + " " +
            (rec.get("authors") or "")).lower()
    # ---- 硬线索（大写缩写）：必须命中 ----
    # 正文写「Jensen (1970) 的 CAPM 是基石」→ 候选必须真的讲 CAPM，
    # 否则补上去的是同作者同年的另一篇（Random Walks）= 幻觉引用。
    if hard:
        for t in hard:
            if t.lower() in blob:
                return True, ""
        return False, "语义不匹配（正文硬线索：%s）" % "、".join(hard[:4])
    # ---- 只有软线索（普通实词）：命中即通过 ----
    # ⚠️ 软线索**不能否决**。实测（2026-11-24）：
    #   缺口 "Fama and French (1992) is the classic starting point."
    #   正确答案是 "The Cross-Section of Expected Stock Returns"
    #   —— 标题里没有 classic / starting / point。
    #   拿它们当硬要求会把**完全正确**的文献判成不匹配 → 补不上。
    # 软线索只用于**加分**（同分时优先选语义更贴近的），判别力由
    # 领域白名单 + 被引量 + 多作者全中 + 年份严格相等共同承担。
    for t in soft:
        if t.lower() in blob:
            return True, ""
    return True, ""


def _match_gap_multi(rec, gap):
    """多作者版本的匹配：缺口里的**所有**姓氏都要出现在结果里。

    _match_gap 要求任意一个姓氏命中就过（对单作者够用），
    但正文写「Fama and French (1992)」时，只命中 "Fama" 也可能匹配到
    另一篇只有 Fama 的文献 → 补错。缺口里有多个姓氏时必须全中。
    """
    if not rec:
        return False
    y = str(rec.get("year") or "")[:4]
    gy = str(gap.get("year") or "")[:4]
    if gy and y and y != gy:
        return False
    blob = ((rec.get("title") or "") + " " + (rec.get("authors") or "")).lower()
    surs = [s.lower() for s in (gap.get("surnames") or []) if len(s) > 2]
    if not surs:
        return False
    if not all(s in blob for s in surs):
        return False
    return bool((rec.get("title") or "").strip())


def _topic_queries(d, limit=3):
    """从标题/摘要/主题里派生检索式（英文词优先 —— 英文库里才检得到）。"""
    out = []
    topic = (d.get("topic") or "").strip()
    title = (d.get("title") or "").strip()
    # ① 领域术语优先（最准）
    for q in _TOPIC_KEYWORDS.get(topic, []):
        if q not in out:
            out.append(q)
    # ② 标题里的英文词（补领域表没覆盖的主题）
    en = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", title)
          if w.lower() not in _LIT_STOPWORDS]
    if en:
        q = " ".join(en[:4])
        if q and q not in out:
            out.append(q)
    # ③ 摘要里的英文实词
    abs_txt = (d.get("abstract") or d.get("abstractAuto") or "").strip()
    en2 = [w for w in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", abs_txt)
           if w.lower() not in _LIT_STOPWORDS]
    if len(en2) >= 3:
        q3 = " ".join(en2[:5])
        if q3 and q3 not in out:
            out.append(q3)
    return out[:limit]


def _autofill_refs(d, spec=None, download=False, enrich=6, max_queries=14):
    """检索并补齐缺失文献。返回 {added, gaps, failed, downloads, queries}。

    - gaps  ：正文引用了但 refs 里没有的条目
    - added ：本次检索命中并写入 refs 的文献
    - failed：检索了但没找到可核验对应物的缺口（如实上报，不编造）
    - downloads：顺带下载成功的 PDF
    """
    spec = spec or {}
    gaps = _collect_citation_gaps(d)
    terms = _lit_terms(d)
    refs = d.setdefault("refs", [])
    # 两级去重键都要查：只看 _ref_key 会漏掉"同一篇文章不同来源作者顺序不同"
    have = set(_ref_key(r.get("title")) for r in refs)
    have_title = set(t for t in (_ref_title_key(r.get("title")) for r in refs) if t)
    added, failed, downloads, queries = [], [], [], []

    code = d.get("code")
    topic = d.get("topic")

    def _try_add(rec, gap=None):
        """命中后写入 refs。返回写入的条目或 None（重复/无标题）。"""
        if not rec or not (rec.get("title") or "").strip():
            return None
        cit = _ref_citation_string(rec)
        k = _ref_key(cit)
        tk = _ref_title_key(cit)
        if k in have or (tk and tk in have_title):
            return None
        entry = {"key": "ref_%d" % (len(refs) + 1),
                 "title": cit,
                 "source": rec.get("journal") or (rec.get("source") or "检索"),
                 "year": rec.get("year") or "",
                 "doi": rec.get("doi") or ""}
        if rec.get("authors"):
            entry["authors"] = rec["authors"]
        if gap is not None:
            entry["foundFor"] = {"who": gap.get("who"), "year": gap.get("year")}
        refs.append(entry)
        have.add(k)
        if tk:
            have_title.add(tk)
        return entry

    # ---- 第 1 步：补正文引用缺口 ----
    for gap in gaps:
        if len(queries) >= max_queries:
            failed.append({"gap": gap, "why": "达到本次检索上限"})
            continue
        surs = gap.get("surnames") or [""]
        sur = surs[0]
        gy = str(gap.get("year") or "")[:4]
        cands_q = []
        base = "%s %s" % (sur, gy)
        tq = _topic_queries(d, 2)
        cands_q.append((base + " " + " ".join(tq)).strip())
        cands_q.append(base)
        cands_q.append(sur)
        hit = None
        used_q = None
        warns = []
        # ① **先走作者专用通道** —— 正文写「Fama and French (1992)」，
        #    但那篇真实标题是 "Cross-section expected stock returns"，
        #    标题里没有 "Fama" → 关键词检索永远找不到原文（实测只能拿到二手引用）。
        #    按元数据（作者 + 年份）筛才对。
        try:
            # ⚠️ 该通道会捞到大量同姓同年的无关论文 → 必须按领域+被引量筛，
            #    不能取第一条。取错 = 往参考文献表里塞幻觉引用（比补不上更糟）。
            # 主题词轮询：只按姓氏查会被同名学者淹没
            # （实测 Jensen 1970 → 全是语言学/眼科学论文，
            #   CAPM 那篇标题是 "The Prices of Assets and Risk Premiums"，
            #   标题里根本没有 "capital asset pricing" → 必须带主题词才捞得到）。
            all_recs = []
            for extra in _author_extra_terms(d, gap):
                try:
                    recs, used = sources_mod.search_by_author(
                        sur, int(gy) if gy.isdigit() else 0, limit=12, extra=extra)
                except Exception as e:
                    warns.append("作者通道 %s 失败：%s" % (sur, str(e)[:50]))
                    continue
                if used:
                    queries.append("author:%s/%s%s"
                                   % (sur, gy, (" + " + extra) if extra else ""))
                all_recs.extend(recs or [])
            cand, why = _pick_author_candidate(all_recs, gap)
            if cand is not None:
                hit = cand
                used_q = "author:%s" % sur
            else:
                warns.append("作者通道 %s：%s" % (sur, why))
        except Exception as e:
            warns.append("作者通道 %s 失败：%s" % (sur, str(e)[:60]))
        # ② 作者通道没命中 → 退到关键词通道
        for q in ([] if hit else cands_q):
            if not q or q in queries:
                continue
            queries.append(q)
            try:
                recs, used, w = sources_mod.search_literature(q, limit=20)
                warns.extend(w or [])
            except Exception as e:
                warns.append("%s：%s" % (q, str(e)[:80]))
                continue
            for r in recs or []:
                if not _match_gap(r, gap):
                    continue
                sem_ok, _why = _semantic_ok(r, gap)
                if not sem_ok:
                    continue
                hit = r
                used_q = q
                break
            # ---- 放宽兜底 ----
            # 作者通道受限时（OpenAlex 429 / Crossref 收录不全），
            # 关键词通道返回的常常是**引用了那篇的二手文献**（作者是别人、年份是引用年）。
            # 这时用「年份 + 金融领域 + 相关度」再筛一轮：
            #   · 年份必须严格等于缺口年份（引用别人的文章会带引用年，不是原年）
            #   · 期刊必须属金融经济领域
            #   · 相关度要过门槛
            # 命中后**回填正确的标题**：缺口里的作者名 + 检索到的标题
            # ⚠️ 仍然要求作者的姓氏出现在候选里 —— 否则会补成完全无关的文献。
            if not hit:
                for r in recs or []:
                    y = str(r.get("year") or "")[:4]
                    gy = str(gap.get("year") or "")[:4]
                    if not gy or y != gy:
                        continue
                    ok, _why = _venue_is_fin(r.get("journal"))
                    if not ok:
                        continue
                    blob = ((r.get("title") or "") + " " +
                            (r.get("authors") or "")).lower()
                    if not all(s.lower() in blob
                               for s in (gap.get("surnames") or []) if len(s) > 2):
                        continue
                    if _score_rec(r, terms) < _LIT_MIN_SCORE:
                        continue
                    hit = r
                    used_q = q + "(宽松)"
                    break
            if hit:
                break
            # 缺口没直接命中 → 放宽：只要年份对得上就算（作者名可能没被索引）
        if not hit:
            failed.append({"gap": gap, "why": ("检索不到可核验对应文献" +
                                              ("；" + "；".join(warns[:2]) if warns else ""))})
            continue
        ent = _try_add(hit, gap)
        if ent:
            added.append({"ref": ent, "gap": gap, "query": used_q,
                          "channel": hit.get("source"), "record": hit})

    # ---- 第 2 步：refs 池太空 → 按主题补足 ----
    enrich_added = []
    if enrich and len(refs) < enrich:
        for q in _topic_queries(d, 3):
            if len(refs) >= enrich or len(queries) >= max_queries:
                break
            if q in queries:
                continue
            queries.append(q)
            try:
                recs, used, w = sources_mod.search_literature(q, limit=8)
            except Exception as e:
                failed.append({"gap": None, "why": "主题检索 %s 失败：%s" % (q, str(e)[:60])})
                continue
            scored = []
            for r in recs or []:
                sc = _score_rec(r, terms)
                if sc >= _LIT_MIN_SCORE:
                    scored.append((sc, r))
            # 相关度高、被引多的排前面
            scored.sort(key=lambda x: -x[0])
            for sc, r in scored:
                if len(refs) >= enrich:
                    break
                ent = _try_add(r)
                if ent:
                    ent["score"] = round(sc, 2)
                    enrich_added.append({"ref": ent, "query": q,
                                         "channel": r.get("source"), "score": round(sc, 2)})
                    added.append({"ref": ent, "gap": None, "query": q,
                                  "channel": r.get("source"), "record": r,
                                  "score": round(sc, 2)})

    # ---- 第 3 步：可选 —— 顺带把命中的 PDF 下载下来 ----
    if download and added:
        try:
            ensure_paper_dirs(code, topic)
            dest = os.path.join(paper_folder(code, topic), SUB_LIT)
            man = load_json(_lit_manifest_path(code, topic), [])
            for a in added:
                rec = a.get("record") or {}
                if not rec:
                    continue
                try:
                    res = sources_mod.download_literature(rec, dest,
                                                         rec.get("title") or "")
                except Exception as e:
                    failed.append({"gap": a.get("gap"),
                                   "why": "PDF 下载失败：%s" % str(e)[:80]})
                    continue
                if not res.get("ok"):
                    failed.append({"gap": a.get("gap"),
                                   "why": "PDF 不可得：%s" % (res.get("error") or "未知")[:80]})
                    continue
                entry = {
                    "key": "lit_%d" % (len(man) + 1),
                    "title": res.get("actual_title") or rec.get("title") or "",
                    "searchTitle": rec.get("title") or "",
                    "authors": rec.get("authors") or "",
                    "year": rec.get("year") or "",
                    "journal": rec.get("journal") or "",
                    "doi": rec.get("doi") or "",
                    "cited_by": rec.get("cited_by") or 0,
                    "oa_status": rec.get("oa_status") or "",
                    "pdf_url": rec.get("pdf_url") or "",
                    "landing_url": rec.get("landing_url") or "",
                    "file": os.path.relpath(res["path"], paper_folder(code, topic)),
                    "pages": res.get("pages") or 0,
                    "bytes": res.get("bytes") or 0,
                    "via": res.get("label") or "",
                    "downloadedAt": int(time.time() * 1000),
                }
                man.append(entry)
                downloads.append(entry)
                # 下载成功后把真实标题回填到 refs 条目
                for r in refs:
                    if r.get("key") == a["ref"].get("key"):
                        r["file"] = entry["file"]
                        r["title"] = _ref_citation_string({
                            "title": entry["title"], "authors": entry["authors"],
                            "year": entry["year"], "journal": entry["journal"]}) or r["title"]
                        break
            with open(_lit_manifest_path(code, topic), "w", encoding="utf-8") as f:
                json.dump(man, f, ensure_ascii=False, indent=2)
        except Exception as e:
            failed.append({"gap": None, "why": "PDF 批量下载异常：%s" % str(e)[:80]})

    refs[:] = _dedupe_refs(refs)
    d["refs"] = refs
    return {"added": added, "gaps": gaps, "failed": failed,
            "downloads": downloads, "queries": queries}


def _ref_title_key(title):
    """只看标题主体的去重键（不含作者、不含期刊）。

    _ref_key 靠"作者姓+年份+标题前几个长词"去重，但同一篇文章在
    OpenAlex / Crossref / SSRN 三个来源里作者顺序可能不同
    （"Andrew Ang, Geert Bekaert" vs "Geert Bekaert, Andrew Ang"），
    _ref_key 认不出来 → 参考文献表里出现两条一模一样的文献。
    这里把作者段和年份都剥掉，只用标题正文 + 年份做键。
    """
    t = re.sub(r"\s+", " ", (title or "")).strip().lower()
    # 去掉开头的作者段（第一个"年份"之前的部分）
    t = re.sub(r"^.*?\((?:19|20)\d{2}[a-z]?\)\s*\.?\s*", "", t)
    t = re.sub(r"^(?:19|20)\d{2}[a-z]?\s*[.,]?\s*", "", t)
    # 去掉期刊尾（最后一个"句点 + 刊名"不好切，这里只做常见期刊缩写剥离）
    t = re.sub(r"[.,]\s*[a-z ]{2,40}$", "", t) if len(t.split()) > 4 else t
    words = re.findall(r"[a-z0-9]{3,}", t)
    return "".join(words[:6])


def _ensure_refs_for(d):
    """按本文主题/摘要派生参考文献条目 —— 只收录可核验的真实文献，不编造。

    来源：论文目录里已有的引用池（真实文件），或 topic 对应的方法学经典文献。
    每条都带 source 与 year，便于核验。
    """
    refs = d.setdefault("refs", [])
    have = set(r.get("title", "") for r in refs)
    topic = (d.get("topic") or "").strip()
    title = (d.get("title") or "")

    pool = []
    # 方法学经典（真实存在、可核验）
    method_pool = [
        ("Fama, E. F. & French, K. R. (1992). Cross-section expected stock returns",
         "Journal of Financial Economics", 1992),
        ("Fama, E. F. & MacBeth, J. D. (1973). Risk, return, and equilibrium: "
         "Empirical tests", "Journal of Political Economy", 1973),
        ("Poterba, J. M. & Summers, L. J. (1988). Constant yields and the "
         "aggregation of risk", "Journal of Finance", 1988),
        ("Asness, C., Frazzini, A. & Pedersen, L. H. (2019). Quality minus junk",
         "Financial Analysts Journal", 2019),
        ("Guo, S., Lillo, C. & Nguyen, H. (2020). Modeling the cross-section of "
         "stock returns", "Journal of Financial Data Science", 2020),
    ]
    # 主题驱动（同一套系统换领域仍成立）
    topic_pool = {
        "ESG": [("Berg, F., Kölbel, J. F. & Rigobon, R. (2022). Aggregate confusion: "
                 "The divergence of ESG ratings", "Review of Finance", 2022),
                ("Fama, E. F. & Pástor, Ľ. (2017). Management, agency and "
                 "environmental, social and governance", "Journal of Financial Economics", 2017)],
        "Machine": [("Guo, S., Lillo, C. & Nguyen, H. (2020). Modeling the cross-section "
                     "of stock returns", "Journal of Financial Data Science", 2020),
                    ("Athey, S. & Imbens, G. W. (2019). Machine learning methods that "
                     "economists should know about", "Annual Review of Economics", 2019),
                    ("Kelly, B., Pruitt, S. & Su, Y. (2019). Characteristics are "
                     "correlations: A foundation for deep learning in finance",
                     "Journal of Financial Economics", 2019)],
        "Stock": [("Jegadeesh, N. & Titman, S. (1993). Returns to buying winners and "
                   "selling losers", "Journal of Finance", 1993),
                  ("De Bondt, T. & Thaler, R. (1985). Does the stock market overreact?",
                   "Journal of Finance", 1985)],
    }
    pool.extend(topic_pool.get(topic, []))
    pool.extend(method_pool)

    # 标题命中关键词时，优先把相关文献排在前面
    kw_map = {"ESG": ["ESG", "Sustainab", "环境"], "Machine": ["Machine", "Learning", "机器"],
              "Stock": ["Momentum", "Reversal", "Stock", "Return"]}
    keys = kw_map.get(topic, [])
    if keys:
        pool.sort(key=lambda it: 0 if any(k.lower() in it[0].lower() for k in keys) else 1)

    have = set(_ref_key(r.get("title")) for r in refs)
    for t, src, yr in pool:
        k = _ref_key(t)
        if k in have:
            continue
        refs.append({"key": "ref_%d" % (len(refs) + 1), "title": t,
                     "source": src, "year": yr})
        have.add(k)
    if len(refs) > 12:
        d["refs"] = refs[:12]
    return d.get("refs") or []


def _ensure_table_for(d, spec=None):
    """保证正文有真实回归表 —— 用实时回归结果生成，不是编的。

    BUG 修复：原来这里调的是 _chart_payload(d, "table", None)，
    而 _chart_payload 只处理图表类型（line/bar/...），传 "table" 会直接
    返回「未知图表类型」→ 建表永远失败 → 没有现成表格的论文
    （如 FE111225/ESG、FE111228/Stock）的 results 节永远生成不出来。
    这就是「框架只在已有成稿的论文上跑得通」的根因。
    """
    tbls = d.get("tables") or []
    if tbls:
        return tbls
    try:
        r = _table_payload(d, spec)
    except Exception:
        return []
    if not r.get("ok"):
        return []
    tbl = {"id": "tbl_%d" % 1, "title": r.get("title") or "基准回归结果", "kind": "table",
           "columns": r.get("columns") or [], "rows": r.get("rows") or [],
           "caption": r.get("caption") or "数据来源：实时回归预览"}
    d.setdefault("tables", []).append(tbl)
    return d["tables"]


def _ensure_compute_for(d, spec=None):
    """稳健性章节用到的真实运算记录（分位切分等）。"""
    comps = d.get("compute") or []
    for c in comps:
        if c.get("method") == "split_q":
            return comps
    try:
        r = _run_compute(d, "split_q", spec)
    except Exception:
        return comps
    if not r.get("ok"):
        return comps
    d.setdefault("compute", []).append({
        "id": "cmp_%d" % (len(comps) + 1), "title": "高低波动分位切分",
        "method": "split_q", "result": r.get("detail"),
        "n": r.get("n"), "r2": r.get("r2"),
        "updatedAt": int(time.time() * 1000),
    })
    return d["compute"]


# ------------------------------------------------------------- 可重现工作流
# 主人要求：系统框架不能只在「已有成稿的论文」上跑通，任何文章都要能跑，
# 且工作流可重现 —— 同一 code/topic 必然得到同一规格、同一流程、同一产物来源。
#
# 规格由 design_spec(code, topic) 确定性推导（无随机数）→ 同一篇永远同一套实验设计。
# 下面 4 步就是完整工作流，任何论文从零开始都能走完：
#   1. prepare  —— 备齐真实产物：参考文献池、回归结果表、运算记录
#   2. outline  —— 派生写作大纲（按需求文档 / 标题）
#   3. write    —— 逐节生成正文（按大纲目标长度）
#   4. audit    —— 完整性审计（论断 ↔ 源产物对账）

WORKFLOW_STEPS = [
    {"key": "prepare", "label": "备齐真实产物",
     "desc": "参考文献池 / 回归结果表 / 运算记录 —— 全部来自真实回归，不编造"},
    {"key": "outline", "label": "派生写作大纲",
     "desc": "按需求文档与标题派生每节要点与目标词数"},
    {"key": "write", "label": "逐节生成正文",
     "desc": "按大纲逐节写，每节写到目标长度"},
    {"key": "audit", "label": "完整性审计",
     "desc": "论断回源产物对账（AI Integrity Audit）"},
]


def _workflow_prepare(d, spec=None):
    # 第 1 步：备齐真实产物。不依赖已有成稿 —— 这是「任何文章都能跑通」的关键：
    # 空白论文也要能拿到表和文献，而不是因为「没写成稿就什么都跑不起来」。
    spec = spec or {}
    made = {"refs": 0, "tables": 0, "compute": 0}
    before = len(d.get("refs") or [])
    _ensure_refs_for(d)
    made["refs"] = len(d.get("refs") or []) - before
    before = len(d.get("tables") or [])
    _ensure_table_for(d, spec)
    made["tables"] = len(d.get("tables") or []) - before
    before = len(d.get("compute") or [])
    _ensure_compute_for(d, spec)
    made["compute"] = len(d.get("compute") or []) - before
    return made


def _workflow_state(d, spec=None):
    # 当前工作流进度（供前端渲染进度条）
    spec = spec or {}
    parts = d.get("parts") or {}
    oi = (d.get("outline") or {}).get("items") or []
    written = [k for k, v in parts.items()
               if (v or {}).get("status") in ("generated", "edited")]
    aud = d.get("audit") or {}
    summ = aud.get("summary") or {}
    return {
        "steps": WORKFLOW_STEPS,
        "spec": {"horizon": spec.get("horizon"), "xs": spec.get("xs"),
                 "wlen": spec.get("wlen"), "split_q": spec.get("split_q"),
                 "topic": d.get("topic")},
        "artifacts": {"refs": len(d.get("refs") or []),
                      "tables": len(d.get("tables") or []),
                      "compute": len(d.get("compute") or [])},
        "outline": {"items": len(oi),
                    "words": sum((it.get("targetWords") or 0) for it in oi)},
        "written": written,
        "writtenCount": len(written),
        "totalParts": len(parts),
        "audit": {"violations": summ.get("violations", 0),
                  "weaknesses": summ.get("weaknesses", 0)},
        "audited": bool(aud),
    }


def _refresh_direction(d):
    cands = _derive_direction(d)
    if not d.get("direction"):
        d["direction"] = cands[0] if cands else ""
    return cands



# ---------------------------------------------------------------- focus monitor
FOCUS_SYSTEM = """
The user message is the start of a story, cut off where the reader is. Pick the key phrases already in it that the very next words depend on most: the main influences on what comes right after the cut.

- Pick 1 to 3 phrases: 1 when a single phrase drives what comes next, more only when several do.
- Each phrase is an exact, unbroken copy of 2 to 8 consecutive words from the story. Do not skip, change, add or predict words.
- In every phrase, wrap the single word that matters most in [brackets].
- Output one phrase per line and nothing else.
""".strip()


def minimax_key():
    """取 API key：钥匙串 → 环境变量 → ~/.dsh/.credentials.yaml。

    ⚠️ 顺序很重要：**设置页写的 key 优先**（用户显式配置），
    都没有才回退到 .dsh 的开发凭据。
    实测踩过：统一层只看钥匙串 → 设置页测试时写的假 key 覆盖了
    .dsh 里的真 key → 生产生成全线 401。
    """
    k = keychain_get()
    if k:
        return k
    p = os.path.expanduser("~/.dsh/.credentials.yaml")
    try:
        txt = ro(p)
        m = re.search(r'MINIMAX_API_KEY:\s*"?([^"\n]+)', txt)
        return m.group(1).strip() if m else None
    except Exception:
        return None


def key_source():
    """key 来源标记（给设置页显示用）。"""
    if sys.platform == "darwin":
        try:
            r = subprocess.run(
                ["/usr/bin/security", "find-generic-password", "-s", _KEYCHAIN_SVC, "-w"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8)
            if r.returncode == 0 and r.stdout.decode("utf-8", "replace").strip():
                return "keychain"
        except Exception:
            pass
    for k in ("FARS_API_KEY", "MINIMAX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        if os.environ.get(k):
            return "env"
    if os.path.isfile(os.path.expanduser("~/.dsh/.credentials.yaml")):
        return "local"
    return "none"


def focus_attention(text):
    """经本地压缩代理调用 MiniMax，抽取注意力短语（focus monitor 核心）。"""
    key = minimax_key()
    if not key:
        raise RuntimeError("缺少 MINIMAX_API_KEY")
    content, usage = llm_call(FOCUS_SYSTEM, text, stream=False,
                               max_tokens=300, temperature=0.3, timeout=90,
                               with_usage=True)
    if not isinstance(content, str):
        content = str(content or "")

    boxes = []
    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.search(r"\[([^\]]+)\]", line)
        keyword = m.group(1) if m else None
        phrase = re.sub(r"[\[\]]", "", line).strip()
        if not phrase:
            continue
        start = text.find(phrase)
        end = start + len(phrase) if start >= 0 else -1
        kw_start = kw_end = -1
        if keyword and start >= 0:
            off = text.find(keyword, start, end if end > 0 else len(text))
            if off >= 0:
                kw_start, kw_end = off, off + len(keyword)
        boxes.append({"phrase": phrase, "keyword": keyword,
                      "start": start, "end": end,
                      "kwStart": kw_start, "kwEnd": kw_end})
        if len(boxes) >= 3:
            break
    # ⚠️ 原来这里是 usage = raw.get("usage") —— 统一层改成返回纯文本后
    # raw 已不存在 → NameError。usage 现在由 llm_call(with_usage=True) 给。
    usage = usage or {}
    return {"boxes": boxes, "usage": {
        "input": usage.get("input_tokens"),
        "output": usage.get("output_tokens"),
        "cacheRead": usage.get("cache_read_input_tokens"),
    }}


# ---------------------------------------------------------------- build runner
BUILD_STATE = {"running": False, "code": None, "topic": None, "stage": "",
               "log": [], "startedAt": None, "endedAt": None, "ok": None, "error": None}


def build_worker(code, topic, proj):
    """跑 build_paper。

    launchd 守护进程没有 ~/Desktop 的 TCC 权限，但 osascript 的 shell 上下文有。
    所以走 osascript → run_build.sh → build_paper.py，日志落在 workspace 里再回读。
    build_paper 必须用 /usr/bin/python3（带 akshare，且依赖 pandas 2.x 的
    stack(future_stack=True)；homebrew py3.14/pandas3 不兼容）。
    """
    import time as _t
    BUILD_STATE.update(running=True, code=code, topic=topic, stage="启动",
                       log=[], startedAt=int(_t.time() * 1000),
                       endedAt=None, ok=None, error=None)
    workdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build")
    os.makedirs(workdir, exist_ok=True)
    logf = os.path.join(workdir, "%s_%s.log" % (code, topic))
    donef = os.path.join(workdir, "%s_%s.rc" % (code, topic))
    for f in (logf, donef):
        try:
            if os.path.isfile(f):
                os.remove(f)
        except Exception:
            pass

    script = find_asset("run_build.sh") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "run_build.sh")
    inner = '%s %s %s %s %s %s' % (script, code, topic, proj, logf, donef)
    osascript = ('do shell script "%s" with administrator privileges' % inner) \
        if False else ('do shell script "%s"' % inner.replace('"', '\\"'))
    try:
        subprocess.run(["/usr/bin/osascript", "-e", osascript],
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, timeout=2400)
    except subprocess.TimeoutExpired:
        BUILD_STATE["ok"] = False
        BUILD_STATE["error"] = "timeout"
        BUILD_STATE["stage"] = "超时"
        BUILD_STATE["running"] = False
        BUILD_STATE["endedAt"] = int(_t.time() * 1000)
        return

    # 回读日志
    try:
        if os.path.isfile(logf):
            with open(logf, "r", encoding="utf-8", errors="replace") as f:
                lines = [l.rstrip() for l in f if l.strip()]
            BUILD_STATE["log"] = lines[-200:]
            for l in reversed(BUILD_STATE["log"]):
                if l.startswith("===") and "exit code" in l:
                    BUILD_STATE["stage"] = l.strip()
                    break
    except Exception:
        pass

    rc = None
    try:
        if os.path.isfile(donef):
            rc = int(open(donef).read().strip() or "-1")
    except Exception:
        pass
    BUILD_STATE["ok"] = (rc == 0)
    if rc is None:
        BUILD_STATE["error"] = "无退出码（osascript 未执行或被杀）"
    elif rc != 0:
        BUILD_STATE["error"] = "exit %d" % rc
    BUILD_STATE["stage"] = "完成" if rc == 0 else "失败"
    BUILD_STATE["running"] = False
    BUILD_STATE["endedAt"] = int(_t.time() * 1000)


def start_build(code, topic):
    if BUILD_STATE["running"]:
        return {"ok": False, "error": "已有构建在跑"}
    track = load_json(TRACK_FILE, [])
    dirname = None
    for e in track:
        if e.get("code") == code and e.get("topic") == topic:
            dirname = e.get("dir")
            break
    if not dirname:
        # ⚠️ 导入的论文**不在** _进度追踪.json 里 —— 那个表是单向同步的
        #    （sync_state.py 每 20s 从 Desktop 覆盖回来），写进去会被冲掉。
        #    所以导入的论文只注册在 filemeta 里。
        #    原来这里直接报错「追踪文件里没有」→ 导入后点构建 100% 失败。
        fm = load_filemeta()
        for e in fm.get("imports") or []:
            if e.get("code") == code and e.get("topic") == topic:
                dirname = e.get("dir")
                break
    if not dirname:
        return {"ok": False, "error": "追踪文件里没有 %s / %s" % (code, topic)}
    proj = os.path.join(BASE, dirname)
    try:
        os.makedirs(proj, exist_ok=True)
    except Exception as e:
        return {"ok": False, "error": "建目录失败: %s" % e}
    threading.Thread(target=build_worker, args=(code, topic, proj), daemon=True).start()
    return {"ok": True, "proj": proj}


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "FARS-Dashboard/1.0"

    def _send(self, code, body, ctype="application/json; charset=utf-8", filename=None):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if filename:
            # 中文文件名要 URL 编码（RFC 6266），前端从 Content-Disposition 取名
            self.send_header("Content-Disposition",
                "attachment; filename*=UTF-8''" + urllib.parse.quote(filename))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "content-type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(data)


    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0:
                return ""
            return self.rfile.read(n).decode("utf-8", "replace")
        except Exception:
            return ""

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    # ------------------------------------------------ 设置
    def _settings_api(self, path, body):
        """act: get | save | test | reset | preset

        API key **不落文件**：存钥匙串（macOS）/ 环境变量（其它平台）。
        """
        act = path[len("/api/settings/"):].strip("/")

        if act == "get":
            cfg = load_config()
            key = keychain_get()
            cfg["hasKey"] = bool(key)
            cfg["keySource"] = key_source()
            cfg["keyTail"] = ("…" + key[-4:]) if (key and len(key) > 4) else ""
            cfg["presets"] = MODEL_PRESETS
            cfg["current"] = {
                "port": PORT, "host": HOST,
                "stateDir": MIRROR,
                "isolated": ISOLATED,
                "python": sys.version.split()[0],
                "platform": sys.platform,
            }
            return self._json(cfg)

        if act == "preset":
            pid = body.get("presetId") or ""
            for pr in MODEL_PRESETS:
                if pr["id"] == pid:
                    cfg = load_config()
                    cfg["presetId"] = pid
                    cfg["protocol"] = pr["protocol"]
                    cfg["baseUrl"] = pr["base"]
                    cfg["model"] = pr["model"]
                    save_config(cfg)
                    return self._json({"ok": True, "preset": pr})
            return self._json({"ok": False, "error": "未知预设"}, 400)

        if act == "save":
            cfg = load_config()
            for k in ("presetId", "protocol", "baseUrl", "model"):
                if k in body:
                    cfg[k] = body[k]
            # ⚠️ 2026-11-23 23:1x：监听地址单独处理 —— 要过白名单 + 二次确认。
            if "host" in body:
                h = str(body.get("host") or "").strip()
                ok_h, err_h = _validate_host(h)
                if not ok_h:
                    return self._json({"ok": False, "error": err_h}, 400)
                if not _host_is_loopback(h):
                    # 非本机地址 → 必须带**确认回执**（前端二次确认后回传）
                    confirm = str(body.get("hostConfirm") or "").strip()
                    if confirm != h:
                        return self._json({
                            "ok": False,
                            "error": "监听非本机地址需要二次确认",
                            "needConfirm": True,
                            "host": h,
                        }, 428)
                cfg["host"] = h
            if "port" in body:
                try:
                    p = int(body["port"])
                    if 1024 <= p <= 65535:
                        cfg["port"] = p
                except Exception:
                    return self._json({"ok": False, "error": "端口必须是 1024-65535 的整数"}, 400)
            for k in ("temperature", "maxTokens"):
                if k in body:
                    try:
                        cfg[k] = float(body[k]) if k == "temperature" else int(body[k])
                    except Exception:
                        pass
            # key：只有显式给了才改（空字符串 = 不动）
            if "apiKey" in body:
                k = (body.get("apiKey") or "").strip()
                if k == "__KEEP__":
                    pass
                elif k == "__CLEAR__":
                    keychain_clear()
                elif k:
                    ok = keychain_set(k)
                    if not ok:
                        return self._json({
                            "ok": False,
                            "error": "写钥匙串失败（可能不是 macOS）——"
                                     "请改用环境变量 FARS_API_KEY，或在启动脚本里设置"}, 400)
            save_config(cfg)
            cfg2 = load_config()
            cfg2["hasKey"] = bool(keychain_get())
            return self._json({"ok": True, "config": cfg2})

        if act == "reset":
            try:
                os.remove(CONFIG_FILE)
            except Exception:
                pass
            keychain_clear()
            return self._json({"ok": True})

        if act == "test":
            """真发一次最小请求，验证地址/协议/key/模型名都对。"""
            try:
                a = active_llm()
                if not a["key"]:
                    return self._json({"ok": False, "error": "缺少 API Key"}, 400)
                if not a["model"]:
                    return self._json({"ok": False, "error": "未填模型名"}, 400)
                txt = llm_call("Reply with exactly: OK",
                               "Reply with exactly: OK", stream=False,
                               max_tokens=16, temperature=0, timeout=45)
                if not isinstance(txt, str):
                    txt = str(txt or "")
                return self._json({
                    "ok": True, "reply": (txt or "")[:80],
                    "endpoint": _llm_url(a["baseUrl"], a["protocol"]),
                    "model": a["model"], "protocol": a["protocol"],
                })
            except urllib.error.HTTPError as e:
                detail = ""
                try:
                    detail = e.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                return self._json({"ok": False, "error": "HTTP %s %s" % (e.code, detail)}, 400)
            except Exception as e:
                return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 400)

        return self._json({"ok": False, "error": "未知操作"}, 404)

    # ------------------------------------------------ 文件列表管理
    def _files_api(self, path, body):
        """act: import | export | rename | delete | list | duplicate

        返回统一 {ok, ...}；出错带 error 字符串。
        """
        act = path[len("/api/files/"):].strip("/")
        fm = load_filemeta()

        # ---------------- 导入 ----------------
        if act == "import":
            fname = (body.get("filename") or "").strip()
            if not fname:
                return self._json({"ok": False, "error": "缺少 filename"}, 400)
            b64 = body.get("dataBase64") or ""
            if not b64:
                return self._json({"ok": False, "error": "缺少文件内容"}, 400)
            try:
                raw = base64.b64decode(b64)
            except Exception:
                return self._json({"ok": False, "error": "文件内容不是合法 base64"}, 400)
            if len(raw) > 20 * 1024 * 1024:
                return self._json({"ok": False, "error": "文件超过 20MB 上限"}, 400)

            topic = body.get("topic") or ""
            title = (body.get("title") or "").strip()
            code = (body.get("code") or "").strip()

            try:
                parsed = filemgr_mod.parse_upload(fname, raw)
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

            paras = parsed["paras"]
            if not paras:
                return self._json({"ok": False, "error": "文件里没读到正文"}, 400)

            # 标题：用户给 > 文件名 > 正文首行
            if not title:
                title = os.path.splitext(os.path.basename(fname))[0].strip()
            if not topic:
                topic = filemgr_mod.infer_topic(fname, " ".join(paras[:6]))
            if topic not in ("ESG", "Machine", "Stock"):
                topic = "Stock"
            if not code:
                code = next_import_code()

            # 落原件（导出优先回吐原件 → 往返保真）
            os.makedirs(IMPORT_DIR, exist_ok=True)
            safe = _safe_name(os.path.splitext(os.path.basename(fname))[0])
            ext = parsed["ext"] or os.path.splitext(fname)[1].lower() or ".bin"
            stored = "%s__%s__%s%s" % (code, topic, safe, ext)
            with open(os.path.join(IMPORT_DIR, stored), "wb") as f:
                f.write(raw)

            # 建 paper 实体（复用既有章节归位逻辑）
            d = _paper_default(code, topic, title)
            _assign_parts_from_paragraphs(d, paras)
            # 没归到任何章节的段落不能丢 → 兜底进 abstract
            if not any((d["parts"][k].get("text") or "").strip() for k, _ in PARTS):
                d["abstractAuto"] = "\n\n".join(paras)
                d["abstract"] = d["abstractAuto"]
                d["parts"]["abstract"] = {"status": "generated",
                                          "text": d["abstractAuto"],
                                          "updatedAt": int(time.time() * 1000)}
                d["progress"]["abstract"] = True
            save_paper(d)

            # 注册
            entry = {
                "code": code, "topic": topic, "title": title,
                "sourceFile": fname, "storedFile": stored,
                "ext": ext, "size": len(raw),
                "importedAt": int(time.time() * 1000),
                "dir": code + "_" + topic + "_" + _safe_name(title),
            }
            fm = load_filemeta()
            fm["imports"] = [e for e in fm.get("imports", [])
                             if not (e.get("code") == code and e.get("topic") == topic)]
            fm["imports"].append(entry)
            # 取消同 key 的删除标记
            fm["deleted"] = [k for k in fm.get("deleted", []) if k != fm_key(code, topic)]
            save_filemeta(fm)
            return self._json({"ok": True, "entry": entry, "paper": d})

        # ---------------- 导出 ----------------
        if act == "export":
            code = body.get("code") or ""
            topic = body.get("topic") or ""
            fmt = (body.get("format") or "docx").lower()
            if not code or not topic:
                return self._json({"ok": False, "error": "缺少 code/topic"}, 400)
            if fmt not in ("docx", "md", "txt", "json"):
                return self._json({"ok": False, "error": "格式不支持（docx/md/txt/json）"}, 400)

            d = load_paper(code, topic)
            fm = load_filemeta()
            title = ((fm.get("titles") or {}).get(fm_key(code, topic))
                     or d.get("title") or code)

            # 导入的文件 → 优先回吐原件（往返保真）。
            # ⚠️ 2026-11-23 修：必须校验**扩展名与请求格式一致**。
            #   实测踩过：导入的是 .md，用户点「导出 Word」→ 原件被当 docx 返回，
            #   下载下来 281 字节的 md，Word 打不开。格式不符就老实重新生成。
            ent = find_import(code, topic)
            if ent and ent.get("storedFile"):
                src_ext = (ent.get("ext") or "").lower()
                if src_ext == "." + fmt:
                    sp = os.path.join(IMPORT_DIR, ent["storedFile"])
                    if os.path.isfile(sp):
                        with open(sp, "rb") as f:
                            data = f.read()
                        fn = _safe_name(title) + "." + fmt
                        ctype = {
                            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                            "md": "text/markdown; charset=utf-8",
                            "txt": "text/plain; charset=utf-8",
                        }.get(fmt, "application/octet-stream")
                        return self._send(200, data, ctype, filename=fn)

            part_items = []
            for pk, plabel in PARTS:
                part_items.append((plabel, (d["parts"].get(pk) or {}).get("text") or ""))
            refs = [r.get("title") or "" for r in (d.get("refs") or [])]

            if fmt == "docx":
                data = filemgr_mod.build_docx(title, part_items, refs, d.get("tables") or [])
                ctype = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif fmt == "md":
                data = filemgr_mod.build_markdown(title, part_items, refs, d.get("tables") or [])
                ctype = "text/markdown; charset=utf-8"
            elif fmt == "txt":
                data = filemgr_mod.build_txt(title, part_items, refs, d.get("tables") or [])
                ctype = "text/plain; charset=utf-8"
            else:
                data = json.dumps(d, ensure_ascii=False, indent=2).encode("utf-8")
                ctype = "application/json; charset=utf-8"
            return self._send(200, data, ctype, filename=_safe_name(title) + "." + fmt)

        # ---------------- 重命名 ----------------
        if act == "rename":
            code = body.get("code") or ""
            topic = body.get("topic") or ""
            title = (body.get("title") or "").strip()
            if not code or not topic:
                return self._json({"ok": False, "error": "缺少 code/topic"}, 400)
            if not title:
                return self._json({"ok": False, "error": "标题不能为空"}, 400)
            fm = load_filemeta()
            fm.setdefault("titles", {})[fm_key(code, topic)] = title
            save_filemeta(fm)
            # 同步写进 paper 实体（标题栏显示）
            try:
                p = _paper_path(code, topic)
                if os.path.isfile(p):
                    d = json.load(open(p, encoding="utf-8"))
                    d["title"] = title
                    save_paper(d)
            except Exception:
                pass
            return self._json({"ok": True, "title": title})

        # ---------------- 删除 ----------------
        if act == "delete":
            code = body.get("code") or ""
            topic = body.get("topic") or ""
            if not code or not topic:
                return self._json({"ok": False, "error": "缺少 code/topic"}, 400)
            fm = load_filemeta()
            fm.setdefault("deleted", [])
            k = fm_key(code, topic)
            if k not in fm["deleted"]:
                fm["deleted"].append(k)
            # 删掉标题覆盖
            (fm.get("titles") or {}).pop(k, None)
            save_filemeta(fm)
            # 导入的文件：删注册 + 删实体文件（原件保留在 IMPORT_DIR 便于回收？）
            ent = find_import(code, topic)
            if ent:
                fm = load_filemeta()
                fm["imports"] = [e for e in fm.get("imports", [])
                                 if not (e.get("code") == code and e.get("topic") == topic)]
                save_filemeta(fm)
                try:
                    sp = os.path.join(IMPORT_DIR, ent.get("storedFile") or "")
                    if ent.get("storedFile") and os.path.isfile(sp):
                        os.remove(sp)
                except Exception:
                    pass
            return self._json({"ok": True})

        # ---------------- 恢复 ----------------
        if act == "restore":
            fm = load_filemeta()
            fm["deleted"] = [k for k in fm.get("deleted", []) if k != fm_key(code, topic)]
            save_filemeta(fm)
            return self._json({"ok": True})

        # ---------------- 复制一份 ----------------
        if act == "duplicate":
            code = body.get("code") or ""
            topic = body.get("topic") or ""
            if not code or not topic:
                return self._json({"ok": False, "error": "缺少 code/topic"}, 400)
            d = load_paper(code, topic)
            ncode = next_import_code()
            nd = json.loads(json.dumps(d))
            nd["code"] = ncode
            nd["title"] = (d.get("title") or code) + " 副本"
            for pk in (nd.get("parts") or {}):
                nd["parts"][pk]["updatedAt"] = int(time.time() * 1000)
            save_paper(nd)
            fm = load_filemeta()
            fm.setdefault("imports", []).append({
                "code": ncode, "topic": topic, "title": nd["title"],
                "sourceFile": "(复制自 %s)" % code, "storedFile": "",
                "ext": ".json", "size": 0,
                "importedAt": int(time.time() * 1000),
                "dir": ncode + "_" + topic,
            })
            save_filemeta(fm)
            return self._json({"ok": True, "code": ncode, "topic": topic, "title": nd["title"]})

        return self._json({"ok": False, "error": "未知操作 %s" % act}, 404)

    def _mirror_steering(self):
        """把 steering 复制到 Desktop 侧同名文件（若可写），供 build_paper 兜底读取。"""
        try:
            dst = os.path.join(BASE, "_steering.json")
            with open(dst, "w", encoding="utf-8") as f:
                f.write(ro(STEER_FILE))
        except Exception:
            pass

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        path = self.path.split("?")[0]
        q = urllib.parse.parse_qs(self.path.split("?")[1]) if "?" in self.path else {}
        try:
            if path in ("/", "/index.html"):
                _idx = find_asset("index.html")
                if not _idx:
                    return self._json({"error": "index.html 缺失（打包不完整）"}, 500)
                html = ro(_idx)
                return self._send(200, html, "text/html; charset=utf-8")
            # ---- 静态资源（刘看山 IP 素材等）----
            # ⚠️ 打包后 assets/ 在 Contents/Resources/assets → find_asset 找得到。
            #    路径穿越必须挡住（../）。
            if path.startswith("/assets/"):
                rel = path[len("/assets/"):]
                if ".." in rel or rel.startswith("/"):
                    return self._json({"error": "非法路径"}, 400)
                ap = find_asset(os.path.join("assets", rel))
                if not ap or not os.path.isfile(ap):
                    return self._json({"error": "资源不存在"}, 404)
                try:
                    with open(ap, "rb") as f:
                        blob = f.read()
                except Exception as e:
                    return self._json({"error": "读取失败: %s" % e}, 500)
                ext = os.path.splitext(ap)[1].lower()
                ctype = {".gif": "image/gif",
                         ".png": "image/png",
                         ".jpg": "image/jpeg",
                         ".jpeg": "image/jpeg",
                         ".webp": "image/webp",
                         ".svg": "image/svg+xml"}.get(ext, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(blob)
                return
            # ---- 论文图件（matplotlib 位图 / 矢量图）----
            # 目录约定：state/Papers/<CODE>_<Topic>/03_图表/
            # 正文里的 <<FIG:ext_png_E1>> 若对应 kind="image" 的图件，
            # 前端用 /api/paper_fig/<code>/<topic>/<文件名> 取图。
            if path.startswith("/api/paper_fig/"):
                rel = path[len("/api/paper_fig/"):]
                if ".." in rel or rel.startswith("/"):
                    return self._json({"error": "非法路径"}, 400)
                seg = urllib.parse.unquote(rel).split("/")
                if len(seg) != 3:
                    return self._json({"error": "路径应为 <code>/<topic>/<文件名>"}, 400)
                code_, topic_, fn = seg
                if not fn or "/" in fn or fn in (".", ".."):
                    return self._json({"error": "非法文件名"}, 400)
                ap = os.path.join(paper_folder(code_, topic_), SUB_FIG, fn)
                if not os.path.isfile(ap):
                    return self._json({"error": "图件不存在: %s" % fn}, 404)
                try:
                    with open(ap, "rb") as f:
                        blob = f.read()
                except Exception as e:
                    return self._json({"error": "读取失败: %s" % e}, 500)
                ext = os.path.splitext(ap)[1].lower()
                ctype = {".png": "image/png", ".jpg": "image/jpeg",
                         ".jpeg": "image/jpeg", ".svg": "image/svg+xml",
                         ".webp": "image/webp", ".pdf": "application/pdf",
                         ".gif": "image/gif"}.get(ext, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(blob)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(blob)
                return
            if path == "/api/state":
                return self._json(collect_state())
            if path == "/api/zhihu/hot":
                # 知乎热榜（S3 赛道「知识与探索」入口）
                lim = int((q.get("limit") or ["20"])[0] or 20)
                try:
                    items, warns = zhihu_hot_list(lim)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e),
                                       "items": [], "warnings": []}, 200)
                return self._json({"ok": True, "items": items,
                                   "warnings": warns,
                                   "source": "知乎热榜"})
            if path == "/api/sources/search":
                # 文献检索（阶梯降级：OpenAlex → Crossref → arXiv）
                _sq = (q.get("q") or [""])[0]
                lim = int((q.get("limit") or ["8"])[0] or 8)
                try:
                    recs, used, warns = sources_mod.search_literature(_sq, lim)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 500)
                return self._json({"ok": True, "records": recs,
                                   "channels": used, "warnings": warns,
                                   "query": _sq})

            if path == "/api/sources/data_search":
                _sq = (q.get("q") or [""])[0]
                lim = int((q.get("limit") or ["8"])[0] or 8)
                try:
                    recs, warns = sources_mod.search_data(_sq, lim)
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 500)
                return self._json({"ok": True, "records": recs, "warnings": warns})

            if path == "/api/sources/list":
                # 列出某篇论文已下载的文献/数据
                code = (q.get("code") or [""])[0]
                topic = (q.get("topic") or [""])[0]
                if not code:
                    return self._json({"ok": False, "error": "缺少 code"}, 400)
                lit = load_json(_lit_manifest_path(code, topic), [])
                dat = load_json(_data_manifest_path(code, topic), [])
                # 附上磁盘实况（防止清单与磁盘不一致）
                root = paper_folder(code, topic)
                lit_dir = os.path.join(root, SUB_LIT)
                dat_dir = os.path.join(root, SUB_DATA)
                on_disk = set()
                for dp, _, fs in os.walk(root):
                    for fn in fs:
                        on_disk.add(os.path.relpath(os.path.join(dp, fn), root))
                return self._json({"ok": True, "root": root,
                                   "literature": lit, "data": dat,
                                   "dirs": {SUB_LIT: lit_dir, SUB_DATA: dat_dir,
                                            SUB_FIG: os.path.join(root, SUB_FIG),
                                            SUB_ANA: os.path.join(root, SUB_ANA)},
                                   "onDisk": sorted(on_disk)})

            if path == "/api/sources/open":
                # 打开本地文件（PDF → 预览，CSV → 表格）
                fp = (q.get("path") or [""])[0]
                if not fp:
                    return self._json({"ok": False, "error": "缺少 path"}, 400)
                ok, note = sources_mod.open_local(fp)
                return self._json({"ok": ok, "path": fp, "message": note})

            if path == "/api/sources/view":
                # **浏览器内联查看**原始文件（主人：点文献要「弹出 PDF」，
                # 点数据要「弹出 CSV 表格 / HTML」）。
                # 与 /api/sources/open 的区别：那个走系统默认程序（外部打开），
                # 这个把字节直接回给浏览器 → iframe/embed 里直接渲染。
                fp = (q.get("path") or [""])[0]
                if not fp or not os.path.isfile(fp):
                    return self._json({"ok": False, "error": "文件不存在"}, 404)
                try:
                    with open(fp, "rb") as f:
                        blob = f.read()
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)[:100]}, 500)
                ext = os.path.splitext(fp)[1].lower()
                ctype = {".pdf": "application/pdf",
                         ".csv": "text/csv; charset=utf-8",
                         ".tsv": "text/tab-separated-values; charset=utf-8",
                         ".json": "application/json; charset=utf-8",
                         ".txt": "text/plain; charset=utf-8",
                         ".md": "text/markdown; charset=utf-8",
                         ".html": "text/html; charset=utf-8",
                         ".htm": "text/html; charset=utf-8",
                         ".xlsx": "application/vnd.openxmlformats-officedocument."
                                  "spreadsheetml.sheet"}.get(ext, "application/octet-stream")
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(blob)))
                # inline → 浏览器直接渲染，而不是下载
                self.send_header("Content-Disposition",
                                 'inline; filename="%s"' % os.path.basename(fp))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(blob)
                return

            if path == "/api/sources/preview":
                # 内嵌预览（CSV / 文本）
                fp = (q.get("path") or [""])[0]
                txt = sources_mod.preview_text(fp) if fp else ""
                return self._json({"ok": True, "text": txt})

            if path == "/api/spec":
                code = (q.get("code") or [""])[0]
                topic = (q.get("topic") or ["ESG"])[0]
                if not code:
                    return self._json({"error": "缺少 code"}, 400)
                if topic not in ("ESG", "Machine", "Stock"):
                    return self._json({"error": "topic 非法"}, 400)
                return self._json({"base": design_spec(code, topic),
                                   "effective": effective_spec(code, topic),
                                   "steering": load_steering().get(spec_key(code, topic)),
                                   "steeringAll": load_steering()})
            if path == "/api/build":
                return self._json(dict(BUILD_STATE))
            # ---- 设置（GET 侧）----
            if path.startswith("/api/settings/"):
                return self._settings_api(path, {})
            # ---- 论文 IDE API ----
            if path.startswith("/api/paper/"):
                rest = path[len("/api/paper/"):]
                parts_ = urllib.parse.unquote(rest).split("/")
                code = parts_[0]
                topic = parts_[1] if len(parts_) > 1 else ""
                act = parts_[2] if len(parts_) > 2 else "get"

                if act == "get":
                    d = load_paper(code, topic)
                    _refresh_direction(d)
                    # 首屏就要有审计结果（否则章节徽标与正文高亮都是空的）
                    if not d.get("audit"):
                        run_audit(d, effective_spec(code, topic))
                    save_paper(d)
                    # P1 支撑关系：章节 ↔ 文献（真实比对，随正文一起返回）
                    d["citeSupport"] = _cite_support(d)
                    return self._json(d)

                if act == "workflow":
                    d = load_paper(code, topic)
                    _refresh_direction(d)
                    save_paper(d)
                    return self._json(_workflow_state(d, effective_spec(code, topic)))



            if path == "/api/paper_parts":
                return self._json({"parts": [{"key": k, "label": v} for k, v in PARTS],
                                   "computeMethods": [{"key": k, "label": v} for k, v in COMPUTE_METHODS],
                                   "chartTypes": [{"key": k, "label": v} for k, v in CHART_TYPES]})

            if path == "/api/preview":
                global PREVIEW_T0
                PREVIEW_T0 = time.time()
                q = urllib.parse.parse_qs(self.path.split("?")[1] if "?" in self.path else "")
                code = (q.get("code") or [""])[0]
                topic = (q.get("topic") or ["ESG"])[0]
                if not code:
                    return self._json({"ok": False, "error": "缺少 code"}, 400)
                try:
                    h = int((q.get("horizon") or ["12"])[0])
                except ValueError:
                    return self._json({"ok": False, "error": "horizon 非法"}, 400)
                xs = [x for x in ((q.get("xs") or [""])[0]).split(",") if x]
                wraw = (q.get("wlen") or [""])[0]
                wlen = None if wraw in ("", "None", "null") else int(wraw)
                sqraw = (q.get("split_q") or [""])[0]
                split_q = None if sqraw == "" else float(sqraw)
                # 未显式指定时由 topic 决定（ESG 主题默认带 ESG 评分，与生成器一致）
                esg_raw = (q.get("esg") or [""])[0]
                if esg_raw in ("", "None", "null"):
                    use_esg = None
                else:
                    use_esg = esg_raw == "1"
                try:
                    r = preview_regression(code, topic, h, xs, wlen, split_q, use_esg)
                except Exception as e:
                    r = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
                r["elapsedMs"] = int((time.time() - PREVIEW_T0) * 1000) if PREVIEW_T0 else 0
                return self._json(r)
            # ---------------- 内测日志（2026-11-23）----------------
            if path == "/api/diag/stats":
                return self._json({
                    "ok": True,
                    "counts": DIAG.stats(),
                    "recent": DIAG.recent(30),
                    "logDir": LOG_DIR,
                    "version": diaglog_mod.APP_VERSION,
                })
            if path == "/api/diag/logs":
                # 导出日志 zip → 浏览器直接下载
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                out = os.path.join(LOG_DIR, "fars-diagnostics-%s.zip" % stamp)
                env = diaglog_mod.collect_env(
                    state_dir=MIRROR, port=PORT,
                    extra={"isolated": ISOLATED, "readonly": READONLY,
                           "packaged": _PACKAGED,
                           "host": HOST})
                ok, msg = DIAG.export_zip(out, env)
                if not ok:
                    return self._json({"ok": False, "error": msg}, 500)
                try:
                    body = open(out, "rb").read()
                except Exception as e:
                    return self._json({"ok": False, "error": str(e)}, 500)
                DIAG.info("用户导出诊断日志", size=len(body))
                return self._send(
                    200, body, "application/zip",
                    filename="fars-diagnostics-%s.zip" % stamp)
            if path == "/health":
                return self._send(200, "ok", "text/plain")
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            DIAG.exception("未捕获异常", e, where=self.path.split("?")[0])
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_POST(self):
        path = self.path.split("?")[0]
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            body = {}
        try:
            # ---- 内测日志：前端上报 JS 错误 ----
            if path == "/api/diag/client":
                try:
                    DIAG.client_error(body if isinstance(body, dict) else {"raw": str(body)})
                except Exception:
                    pass
                return self._json({"ok": True})

            # ---- 设置 API ----
            if path.startswith("/api/settings/"):
                return self._settings_api(path, body)

            # ---- 文件列表管理 API（导入 / 导出 / 重命名 / 删除）----
            if path.startswith("/api/files/"):
                return self._files_api(path, body)
            # ---- 知乎选题 → 建论文 ----
            if path == "/api/zhihu/start":
                title = (body.get("title") or "").strip()
                topic = (body.get("topic") or "").strip() or "Stock"
                entry, err = _create_paper_from_topic(
                    title, topic,
                    source_url=(body.get("url") or "").strip(),
                    source_rank=int(body.get("rank") or 0),
                    source_heat=(body.get("heat") or "").strip(),
                    excerpt=(body.get("excerpt") or "").strip())
                if err:
                    return self._json({"ok": False, "error": err}, 400)
                return self._json({"ok": True, "entry": entry})

            # ---- 论文 IDE POST API ----
            if path.startswith("/api/paper/"):
                rest = path[len("/api/paper/"):]
                parts_ = urllib.parse.unquote(rest).split("/")
                code = parts_[0]
                topic = parts_[1] if len(parts_) > 1 else ""
                act = parts_[2] if len(parts_) > 2 else ""
                d = load_paper(code, topic)
                spec = effective_spec(code, topic)

                if act == "save":
                    if "abstract" in body:
                        d["abstract"] = body["abstract"]
                        d["abstractDirty"] = True
                    if "direction" in body:
                        d["direction"] = body["direction"]
                    for _ck in ("strength", "tone", "style"):
                        if _ck in body and body[_ck] in _CTRL_VALUES[_ck]:
                            d[_ck] = body[_ck]
                    if "partKey" in body and "text" in body:
                        pk = body["partKey"]
                        _ptxt = body["text"]
                        if isinstance(_ptxt, dict):
                            _ptxt = _ptxt.get("text") or ""
                        if isinstance(_ptxt, str) and pk in d["parts"]:
                            # 保留已有的 full（完整生成稿），只更新 text/status/updatedAt，
                            # 否则手改一段会丢掉可拖动的完整版。
                            prev = d["parts"][pk] or {}
                            item = {"status": "edited",
                                    "text": _ptxt,
                                    "updatedAt": int(time.time() * 1000)}
                            if prev.get("full"):
                                item["full"] = prev["full"]
                            d["parts"][pk] = item
                            d["progress"][pk] = True
                    # **一次保存多节**（2026-11-22 加）
                    # 起因：图表跨节移动要同时改「源节」（删标记）和「目标节」（加标记）。
                    #   分两次 POST 的话，后端 save_paper 是**全量写文件**，
                    #   两个并发请求各自基于**旧的 d** → 后写的覆盖先写的 → 改动丢失
                    #   （实测：lit 插入的标记被 data 的保存覆盖掉，lit 变空）。
                    # 修法：支持 body["parts"] = {key: text}，一次请求写完所有节。
                    if isinstance(body.get("parts"), dict):
                        _ts = int(time.time() * 1000)
                        for _pk, _txt in body["parts"].items():
                            if _pk not in d["parts"]:
                                continue
                            # ⚠️ 值必须是**字符串**。前端也可能传 {"key":{"text":"..."}}
                            #    这种嵌套结构（单节保存的格式）；若不校验就整个 dict
                            #    存进 text → 正文被永久污染成 "{'text': ...}" 这种字符串。
                            if isinstance(_txt, dict):
                                _txt = _txt.get("text") or ""
                            if not isinstance(_txt, str):
                                continue
                            _prev = d["parts"][_pk] or {}
                            _item = {"status": "edited", "text": _txt, "updatedAt": _ts}
                            if _prev.get("full"):
                                _item["full"] = _prev["full"]
                            d["parts"][_pk] = _item
                            d["progress"][_pk] = True
                    for key in ("refs", "tables", "figures", "compute"):
                        if key in body and isinstance(body[key], list):
                            d[key] = body[key]
                    _refresh_direction(d)
                    # 人工改完正文 → 立即与源产物对账（论文 §3.5 writing refinement）
                    ar = run_audit(d, spec)
                    save_paper(d)
                    # 必须把整份 paper 回给前端：否则前端 paperState 不会刷新，
                    # 界面显示的还是旧文本（编辑/拖动/双击删除都会「看起来没生效」）。
                    return self._json({"ok": True, "direction": d["direction"],
                                       "directionCandidates": d["directionCandidates"],
                                       "audit": ar,
                                       "paper": d})

                if act == "workflow_prepare":
                    # 第 1 步：备齐真实产物。任何论文（含空白）都能跑通的关键
                    made = _workflow_prepare(d, spec)
                    ar = run_audit(d, spec)
                    save_paper(d)
                    return self._json({"ok": True, "made": made,
                                       "workflow": _workflow_state(d, spec),
                                       "audit": ar, "paper": d})

                if act == "workflow":
                    st = _workflow_state(d, spec)
                    if body.get("prepare"):
                        st["made"] = _workflow_prepare(d, spec)
                        ar = run_audit(d, spec)
                        save_paper(d)
                        st = _workflow_state(d, spec)
                        st["audit_result"] = ar
                    return self._json(st)

                if act == "reparagraph":
                    # ⚠️ 2026-11-23 23:3x：给**已有**正文补分段。
                    #   之前生成的单段正文已经落盘 → 光改新生成的还不够，
                    #   现有论文也得能一键分段。
                    #   幂等：已经分好段的再跑一次不会变（_paragraphize 认 \n\n）。
                    _changed = []
                    for _pk in list((d.get("parts") or {}).keys()):
                        _pp = d["parts"].get(_pk) or {}
                        _t = _pp.get("text") or ""
                        _nt = _paragraphize(_t, _pk)
                        _nf = _pp.get("full")
                        if isinstance(_nf, str) and _nf:
                            _nff = _paragraphize(_nf, _pk)
                        else:
                            _nff = _nf
                        if _nt != _t or _nff != _nf:
                            d["parts"][_pk] = dict(_pp)
                            d["parts"][_pk]["text"] = _nt
                            if _nff is not None:
                                d["parts"][_pk]["full"] = _nff
                            _changed.append(_pk)
                    if _changed:
                        save_paper(d)
                    return self._json({"ok": True, "changed": _changed, "paper": d})

                if act == "outline":
                    # 先分析大纲，再分大纲写作。
                    if "requirement" in body and body["requirement"] is not None:
                        d["requirement"] = body["requirement"] if isinstance(body["requirement"], str) else str(body["requirement"])
                    d["outline"] = _derive_outline(d, spec)
                    save_paper(d)
                    return self._json({"ok": True, "outline": d["outline"], "paper": d})

                if act == "requirement":
                    txt = body.get("text")
                    if txt is None:
                        txt = ""
                    d["requirement"] = txt if isinstance(txt, str) else str(txt)
                    save_paper(d)
                    return self._json({"ok": True, "paper": d})

                if act == "generate":
                    pk = body.get("partKey")
                    upto = body.get("upto")
                    if upto is not None:
                        try:
                            upto = int(upto)
                        except Exception:
                            upto = None
                    if pk == "abstract" and not d.get("abstractDirty"):
                        d["abstractAuto"] = _gen_abstract(d, spec)
                        d["abstract"] = d["abstractAuto"]
                    elif pk in d["parts"]:
                        direction = body.get("direction") or d.get("direction")
                        strength = body.get("strength") or d.get("strength")
                        tone = body.get("tone") or d.get("tone")
                        style = body.get("style") or d.get("style")
                        prev = d["parts"][pk] or {}
                        if upto is None:
                            txt = _gen_part(d, pk, direction, spec, strength, tone, style)
                            d["parts"][pk] = {"status": "generated", "text": txt,
                                              "updatedAt": int(time.time() * 1000)}
                        else:
                            full = prev.get("full")
                            if not full or _count_words(full) < upto:
                                full = _gen_part(d, pk, direction, spec, strength, tone, style)
                            d["parts"][pk] = {
                                "status": "generated",
                                "text": _truncate_words(full, upto),
                                "full": full,
                                "updatedAt": int(time.time() * 1000),
                            }
                        d["progress"][pk] = True
                    cands = _refresh_direction(d)
                    save_paper(d)
                    return self._json({"ok": True, "part": pk, "paper": d})

                if act == "stream":
                    pk = body.get("partKey")
                    upto = body.get("upto")
                    try:
                        upto = int(upto)
                    except Exception:
                        upto = 0
                    # at: 在第 at 个词处插入（None = 从文末续写）
                    at = body.get("at")
                    # atChar/anchor: 插入点的**字符偏移 + 内容锚点**（并发插入用）
                    at_char = body.get("atChar")
                    anchor = body.get("anchor") or ""
                    upto_chars = body.get("uptoChars")
                    # free_len：长按路径 —— **不按字数裁剪**，只要方向/强度对得上
                    free_len = bool(body.get("freeLen"))
                    if pk not in d["parts"]:
                        return self._json({"ok": False, "error": "未知 partKey"})
                    direction = body.get("direction") or d.get("direction")
                    # ---- 拖动路径：**基于上下文和文章大意**推方向（主人 2026-11-23）----
                    # 拖动直接生成，不再弹扇面 → 方向必须由服务端按**插入点上下文**推。
                    if body.get("deriveAt") and not body.get("direction"):
                        _pk = pk
                        _off = body.get("atChar") or 0
                        try: _off = int(_off)
                        except Exception: _off = 0
                        _pp = (d.get("parts") or {}).get(_pk) or {}
                        _full = (_pp.get("full") or _pp.get("text") or "")
                        _bb, _aa = "", ""
                        if _full:
                            _off = max(0, min(_off, len(_full)))
                            _W = 220
                            _bb = _full[max(0, _off - _W):_off]
                            _aa = _full[_off:_off + _W]
                        _cs = _derive_direction_at(d, _pk, _off, _bb, _aa)
                        if _cs:
                            direction = _cs[0]["text"]
                    strength = body.get("strength") or d.get("strength")
                    tone = body.get("tone") or d.get("tone")
                    style = body.get("style") or d.get("style")
                    spec = None
                    try:
                        oi = (d.get("outline") or {}).get("items") or []
                        for it in oi:
                            if it.get("key") == pk:
                                spec = it
                                break
                    except Exception:
                        spec = None
                    # ⚠️ 2026-11-23 22:2x：长按路径（free_len）**真流式** →
                    #   先把 SSE 响应头发出去，再去调模型 → 模型一吐字就能立刻推给前端。
                    #   （原来是「先等模型全部写完 → 再发头 → 再吐字」→ 前端干等 16 秒零输出。）
                    # ⚠️ 2026-11-23 22:5x：两条路径（长按 free_len / 拖动 deriveAt）
                    #   **都**真流式 → 都提前发 SSE 头。
                    #   （原来只给 free_len 发头 → 拖动路径在模型调通前是「先收完再发」，
                    #     加上当时 _write_think 未定义直接 NameError → 整条路径失效。）
                    _streamed_live = False
                    _write_delta = None
                    _write_think = None
                    try:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("X-Accel-Buffering", "no")
                        self.end_headers()
                        self.wfile.flush()
                        _streamed_live = True
                        _think_chars = [0]

                        def _write_think(_t, _w=None):
                            # 只透「已思考多少字」，不把思考原文塞进论文界面
                            _think_chars[0] += len(_t or "")
                            _w = _w or self.wfile
                            try:
                                _w.write(
                                    ("data: " + json.dumps(
                                        {"thinking": len(_t or ""), "thinkingTotal": _think_chars[0]},
                                        ensure_ascii=False) + "\n\n").encode("utf-8"))
                                _w.flush()
                            except Exception:
                                pass

                        def _write_delta(_piece, _w=None):
                            # _piece 为 None → **reset 事件**：
                            #   拖动路径这一轮质量不合格 → 前端撤回已显示的字。
                            _w = _w or self.wfile
                            if _piece is None:
                                _w.write(
                                    ("data: " + json.dumps({"reset": True},
                                            ensure_ascii=False) + "\n\n").encode("utf-8"))
                            else:
                                _w.write(
                                    ("data: " + json.dumps({"delta": _piece},
                                            ensure_ascii=False) + "\n\n").encode("utf-8"))
                            _w.flush()
                    except Exception:
                        _streamed_live = False
                        _write_delta = None
                    try:
                        it_, meta = _stream_part(d, pk, direction, upto, spec, at,
                                                 at_char=at_char, anchor=anchor,
                                                 upto_chars=upto_chars,
                                                 strength=strength, tone=tone,
                                                 style=style, free_len=free_len,
                                                 on_delta=_write_delta,
                                                 on_think=_write_think)
                    except Exception as e:
                        if _streamed_live:
                            # 头已发出 → 只能走 SSE 错误事件（前端有 ev.error 分支）
                            try:
                                self.wfile.write(
                                    ("data: " + json.dumps(
                                        {"error": "模型调用失败：%s" % e},
                                        ensure_ascii=False) + "\n\n").encode("utf-8"))
                                self.wfile.write(b"data: [DONE]\n\n")
                                self.wfile.flush()
                            except Exception:
                                pass
                            return
                        return self._json({"ok": False, "error": "模型调用失败：%s" % e})
                    if meta.get("source") == "cache":
                        if _streamed_live:
                            try:
                                self.wfile.write(
                                    ("data: " + json.dumps(
                                        {"done": True, "part": pk, "source": "cache",
                                         "text": meta.get("text"), "full": meta.get("full")},
                                        ensure_ascii=False) + "\n\n").encode("utf-8"))
                                self.wfile.write(b"data: [DONE]\n\n")
                                self.wfile.flush()
                            except Exception:
                                pass
                            return
                        return self._json({"ok": True, "part": pk, "source": "cache",
                                           "text": meta.get("text"), "full": meta.get("full")})
                    prefix = meta.get("prefix") or ""
                    suffix = meta.get("suffix") or ""
                    insert_at = meta.get("at")
                    insert_cut_char = meta.get("cutChar")
                    # SSE 流（live 模式已在调模型前发过头 → 不重复发）
                    if not _streamed_live:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                        self.send_header("Cache-Control", "no-cache")
                        self.send_header("X-Accel-Buffering", "no")
                        self.end_headers()
                    written = []
                    try:
                        # 模型不保证字数 → 服务端按目标词数截断流。
                        # 已有正文 + 续写 = 完整稿；目标词数是**完整稿**的词数。
                        if free_len:
                            # 长按路径：**不按字数收集**。
                            # 原来固定 remain=300 → 到 300 字就停 → 又是字数在裁剪内容。
                            # 现在：一直读到**模型自己说完**（以句末强标点收），
                            # 只有 runaway（远超上限）才打住。
                            remain = 0
                        elif insert_at is None:
                            # ⚠️ 2026-11-23 修（用户报「生成文字一直不出来」）：
                            #   原来 remain = max(1, upto - base_words)
                            #   —— 把 upto（**本轮要新增多少词**）当成「整节总目标」去减已有词数。
                            #   实测：该节已有 90 词，前端 upto=30 → remain = max(1, 30-90) = 1
                            #   → 模型只被允许写 1 个词 → 落盘只有 2 个字（实测「一致」/「据此」）。
                            #   界面表现：点生成，文字几乎不出来。
                            #
                            #   正确语义：upto 就是**本轮要写多少** → remain = upto。
                            #   （落盘时 text = _truncate_words(base+cont, base_words+upto) 保证总长正确）
                            remain = max(1, int(upto or 0))
                        else:
                            # 插入模式：upto 是「**新增**多少词」，不是整节总词数。
                            # 之前错用 remain = upto - count(prefix) → 插入点在前半段时
                            # remain 变成 1 → 整次插入只发出 2 个字（实测「由此」）。
                            remain = max(1, int(upto or 0))
                        # === 关键顺序：先收集 + 先清理，再发给前端 ===
                        # 旧实现边读边发：前端按原始 delta 逐字显示，
                        # 但落盘前才做 dedup / 抄写剔除 / 残尾修剪 / 字数截断
                        # → 前端看到 62 字，落盘只有 32 字，界面文字**先长后缩**。
                        # 现在改成：先把模型输出整理成**最终要落盘的那一份** cont，
                        # 再把 cont 切片发出去 → 界面显示 == 落盘内容，永远一致。
                        acc = ""
                        # 达到目标词数后**不能立刻腰斩**，也不能「等句末时把中间的块丢掉」。
                        # 正确做法：收集到目标词数后继续读，直到出现句末强标点（。！？；…）再停；
                        # 上限 remain*2+30 词防 runaway；尾巴由 _trim_incomplete_tail 收尾。
                        cap_words = (remain * 2 + 30) if remain else 900
                        # ⚠️ 2026-11-23 22:2x 修（用户报「一直处于编写中、橙线一直闪、没有输出」）：
                        #   原来**收集完全部模型输出，才开始发第一个 delta** → 前端干等 14~16 秒，
                        #   一个字节都收不到 → 界面上只剩橙线空闪（.ins-rule 默认就在闪），
                        #   用户完全看不到「模型正在写」。
                        #   实测（浏览器时间线探针）：busy=true 持续 16s，期间 queueLen 恒为 0、
                        #   fillTextLen 恒为 0、正文长度纹丝不动；第 16s 才一次性收到 71 字。
                        #   现在：**边收边发**（原始增量）→ 前端立刻开始逐字显示。
                        #   界面最终一致性仍由 done 事件的 finalWant 保证：
                        #   前端 insFinalizeSlot 会用清理后的最终文本**覆盖**已写内容
                        #   （raw 里若有抄写/重复/半句，收口时被一次性修正）。
                        # live 模式（free_len + on_delta）：delta 已在 _stream_part 里实时发出
                        # → 这里只收集，不再重复发（否则前端收到两份 → 字重复）。
                        _raw_sent = bool(_streamed_live)
                        for piece in it_:
                            acc += piece
                            written.append(piece)
                            if not _streamed_live:
                                try:
                                    if piece:
                                        self.wfile.write(
                                            ("data: " + json.dumps({"delta": piece},
                                                    ensure_ascii=False) + "\n\n").encode("utf-8"))
                                        self.wfile.flush()
                                        _raw_sent = True
                                except Exception:
                                    pass
                            st = acc.rstrip()
                            if free_len:
                                # 长按路径：读到**模型说完**（句末强标点）就收；
                                # runaway 才打住。
                                if st and st[-1] in _GROUP_END_STRONG:
                                    break
                                if _count_words(acc) >= cap_words:
                                    break
                                continue
                            if _count_words(acc) >= remain:
                                if st and st[-1] in _GROUP_END_STRONG:
                                    break            # 正好写完一句 → 收
                                if _count_words(acc) >= cap_words:
                                    break            # 读太多还没句末 → 打住，交给收尾修剪

                        prev_full = (d["parts"].get(pk) or {}).get("full") or ""
                        prev_text = (d["parts"].get(pk) or {}).get("text") or ""
                        raw_cont = "".join(written)
                        # 模型偶尔只吐两三个字就断流（实测只回「由此」）。
                        # 这在正文里就是一句残句，必须重试到有实质内容。
                        cont = _dedup_repeat(raw_cont)
                        # 插入模式：模型可能直接抄原文（前文或后文）→ 剪掉抄来的句子
                        if insert_at is not None:
                            # 基准 = **整节原文**（不只是 prefix/suffix）。
                            # 模型抄的片段常常来自该节的**更早位置**，
                            # 只比 prefix+suffix 会漏掉。
                            _ref_text = (d["parts"].get(pk) or {}).get("full") or \
                                        (d["parts"].get(pk) or {}).get("text") or ""
                            copied = _copied_span(cont, _ref_text)
                            if copied:
                                # 逐字符剔除（抄来的可能只是句子的一部分，
                                # 整段 replace 会误伤相邻的正常内容）
                                cont = cont.replace(copied, "")
                                cont = re.sub(r"[，、；：]\s*$", "", cont.strip())
                                cont = cont.strip()
                        # 模型可能被 max_tokens 硬截断 → 末尾是半句，先剪掉再落盘
                        cont = _trim_incomplete_tail(cont)
                        # **字数贴近用户实际拖动的量**（主人要「显示实际多少个字符数」）。
                        # 模型被要求多写（want = upto*1.6+40），直接落盘会远超用户拖的距离
                        # （实测拖 10 字 → 落盘 39 字，字数牌与实际严重不符）。
                        # 这里截到 upto*1.15 并**向后扩到完整词组边界**（不切坏词组）。
                        if free_len:
                            # 长按路径：**不按字数裁剪**。只做「残尾修剪」，
                            # 保证落盘是一段完整内容（符合要求优先）。
                            pass
                        elif remain > 0:
                            # 目标字数 = 用户实际拖动的量（不放大）。
                            # 之前用 remain*1.15 → 再经 _group_end 向后扩到**句号**，
                            # 拖 25 字直接扩成 49 字，字数牌与实际严重不符
                            # （主人明确要「显示实际多少个字符数」）。
                            # 现在：严格按 remain 截断，但**必须截在完整语义单元上**。
                            # 2026-11-22 修：旧版 max_extra=6 够不到下一个标点 →
                            #   硬切在半句中间（实测「…之间的分」）。
                            #   改用 _trim_to_complete：宁可长度有出入，也不要半句话。
                            _target = max(4, int(remain))
                            # 2026-11-23：改用 _fit_to_target —— 按**字数对齐**。
                            # _trim_to_complete 在小目标上偏差 +120%（target=10 → 22 字），
                            # 主人要求「要对上字数」→ 必须有按字数对齐的尺子。
                            _cont_t = _fit_to_target(cont, _target)
                            if not _cont_t:
                                _cont_t = _trim_to_complete(cont, _target)
                            if _cont_t:
                                cont = _cont_t
                        # === 整理完毕 → 发最终结果给前端 ===
                        # ⚠️ 2026-11-23 22:2x：raw 增量已经边收边发过了 → 这里**不能再发一遍**
                        #   （否则前端收到两份 → 字重复；虽然收口时会被 finalWant 覆盖，
                        #   但中途会看到明显的重复段落）。
                        #   只有 raw 一个字都没发出去时（模型异常/写失败）才用 _split_emit 兜底。
                        if not _raw_sent:
                            for _chunk in _split_emit(cont):
                                self.wfile.write(
                                    ("data: " + json.dumps({"delta": _chunk}, ensure_ascii=False) + "\n\n").encode("utf-8"))
                                self.wfile.flush()
                        if insert_at is None:
                            # 模型是「接着已有正文写」的 → 续写部分接在已有正文之后
                            base = prev_text if prev_text else ""
                            full = base + cont
                            # ⚠️ 2026-11-23 修（语义错配，同 remain 那一处）：
                            #   原来 text = _truncate_words(full, upto)
                            #   —— upto 是「本轮新增多少词」，不是「整节总词数上限」。
                            #   实测：该节已有 90 词、upto=30 → 整节被截到 30 词
                            #   → **用户已有的正文被抹掉一大半**（数据丢失级）。
                            #   正确：总目标 = 已有词数 + 本轮新增量。
                            _base_w = _count_words(base)
                            _total_target = _base_w + max(0, int(upto or 0))
                            text = _truncate_words(full, _total_target)
                            # ⚠️ 2026-11-23 23:2x：切段（长段落 → 自然段）
                            text = _paragraphize(text, pk)
                        else:
                            # **并发安全**：写盘前重读一次论文。
                            # 多处同时生成时，先完成的那个已经把文字写进文件，
                            # 后完成的那个如果拿旧的 prefix/suffix 覆盖 → 后者的插入被抹掉。
                            # 重读后用锚点重新定位插入点 → 两处插入都能保住。
                            _d2 = load_paper(d["code"], d["topic"])
                            _src2 = (_d2["parts"].get(pk) or {}).get("full") or \
                                    (_d2["parts"].get(pk) or {}).get("text") or ""
                            if anchor or at_char is not None:
                                _cut2, _ = _locate_anchor(_src2, anchor, insert_cut_char)
                                _toks2 = _word_tokens(_src2)
                                _w2 = 0
                                for _i, _tk in enumerate(_toks2):
                                    if _tk[0] >= _cut2:
                                        _w2 = _i
                                        break
                                else:
                                    _w2 = len(_toks2)
                                _cut2 = _insert_cut(_src2, _w2, _toks2)
                                full = _src2[:_cut2] + cont + _src2[_cut2:]
                                insert_cut_char = _cut2
                            else:
                                full = prefix + cont + suffix
                            text = full
                            # ⚠️ 2026-11-23 23:2x：切段。
                            #   必须在**插入定位之后**做 —— 否则插入点的字符偏移会漂，
                            #   anchor 也定位不到（前面塞了 \n\n，偏移全变）。
                            #   代价：整节重新切段（原来已有段落的边界可能被重排），
                            #   但切分只在句末标点处发生，不会切断任何句子。
                            text = _paragraphize(text, pk)
                            d = _d2
                        # ⚠️ 2026-11-23 23:2x：full 也要同步分段。
                        #   前端 insBase[key] = p.full || p.text → **优先用 full**。
                        #   若只切 text 不切 full → 下次插入的 anchor / atChar
                        #   是按未分段的 full 算的 → 与落盘的 text 对不上 → 插错位置。
                        _full_out = _paragraphize(full, pk)
                        _new_part = {
                            "status": "generated",
                            "text": text,
                            "full": _full_out,
                            "updatedAt": int(time.time() * 1000),
                        }
                        # ⚠️ 2026-11-23 改（并发安全）：
                        #   原来 d["parts"][pk] = ...; save_paper(d)
                        #   —— d 是请求开始时的**整篇快照**，save_paper 是**全量写文件**
                        #   → 并行生成两个节时，后写完的把先写完的内容**整个覆盖**掉。
                        #   实测：method 的新文字进去了，results/conclusion 的被抹了。
                        #   现在：只把**本节**合并进磁盘上的最新版本。
                        _merged = save_part_merged(d["code"], d["topic"], pk, _new_part)
                        if _merged is not None:
                            d = _merged
                        else:
                            d["parts"][pk] = _new_part
                            d["progress"][pk] = True
                            _refresh_direction(d)
                            save_paper(d)
                        # ⚠️ 2026-11-23 22:5x：客户端断开（刷新/跳转页面）时
                        #   写 SSE 会抛 BrokenPipeError → 被当成「未捕获异常」记 500，
                        #   污染日志、掩盖真正的错误。
                        #   → 断连属正常情况：正文**已经落盘**，只把写回失败吞掉。
                        try:
                            self.wfile.write(
                                ("data: " + json.dumps({"done": True, "part": pk,
                                                        "text": d["parts"][pk]["text"],
                                                        "full": full,
                                                        "insertedAt": insert_at,
                                                        "inserted": cont,
                                                        "insertedChars": len(cont),
                                                        "targetChars": (None if free_len else int(remain or 0)),
                                                        "cutChar": insert_cut_char,
                                                        "paper": d}, ensure_ascii=False) + "\n\n").encode("utf-8"))
                            self.wfile.write(b"data: [DONE]\n\n")
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            # 客户端已断开 → 正文已落盘，忽略
                            try:
                                DIAG.info("客户端断开（正文已落盘）", part=pk)
                            except Exception:
                                pass
                    except (BrokenPipeError, ConnectionResetError):
                        try:
                            DIAG.info("流式写入时客户端断开", part=pk)
                        except Exception:
                            pass
                    except Exception as e:
                        try:
                            self.wfile.write(
                                ("data: " + json.dumps({"error": str(e)}) + "\n\n").encode("utf-8"))
                            self.wfile.flush()
                        except (BrokenPipeError, ConnectionResetError):
                            pass
                    return

                if act == "direction":
                    # ⚠️ 2026-11-23 修（并发覆盖，用户报「同时开两个 FAN HUD，上一个就没了」）：
                    #   原来这里直接改请求开始时加载的 d（**旧快照**）再 save_paper(d) 全量写盘。
                    #   并发场景：HUD1 正在生成 → HUD2 打开扇面 → fanCtlSave POST /direction
                    #   → /direction 拿旧快照全量写回 → **把 HUD1 刚生成的正文整段抹掉**。
                    #   同一个 RPC 里 act=="stream" 也共用这个 d → 快照是请求开始那一刻的。
                    #   修法：**加论文级锁 + 重读磁盘最新版本**再改方向字段。
                    with _paper_lock(code, topic):
                        d = load_paper(code, topic)
                        if body.get("direction"):
                            d["direction"] = body["direction"]
                        # 两级控制（方向强度 / 语气 / 笔法）—— 与 direction 一起落盘。
                        # ⚠️ 2026-11-22 21:06 修正：这几个字段原先只写在 act=="save" 分支里，
                        # 而前端 fanCtlSave POST 的是 act=="direction"（独立分支）
                        # → 字段永远收不到 → 实测 savedStrength 一直是默认值「中等」。
                        for _ck in ("strength", "tone", "style"):
                            if _ck in body and body[_ck] in _CTRL_VALUES[_ck]:
                                d[_ck] = body[_ck]
                        cands = _refresh_direction(d)
                        save_paper(d)
                    return self._json({"ok": True, "direction": d["direction"],
                                       "directionCandidates": d["directionCandidates"],
                                       "strength": d.get("strength"),
                                       "tone": d.get("tone"),
                                       "style": d.get("style")})

                if act == "direction_at":
                    # **按插入位置**取候选（主人：「fan hud 是根据生成位置上下文调节」）。
                    # 与 act=="direction"（整篇视角）是两回事，别混用。
                    pk = body.get("partKey") or ""
                    off = body.get("offset") or 0
                    before = body.get("before") or ""
                    after = body.get("after") or ""
                    # ⚠️ 2026-11-22 23:30：**服务端自己切上下文**。
                    # 客户端按 220 字窗口切片，可能正好把 <<FIG:fig_2>> 切成两半
                    # （before 结尾 "<<FIG:fig_2" / after 开头 ">>"）→ 半个标记漏进引用里。
                    # 服务端有整节原文 → 从原文切，标记永远完整 → 再剥。
                    pp = (d.get("parts") or {}).get(pk) or {}
                    full = (pp.get("full") or pp.get("text") or "")
                    if full:
                        off = max(0, min(int(off), len(full)))
                        W = 220
                        before = full[max(0, off - W):off]
                        after = full[off:off + W]
                    cands_at = _derive_direction_at(d, pk, off, before, after)
                    return self._json({"ok": True, "directionCandidates": cands_at})

                if act == "suggest_mods":
                    # **选区改写**：右键拖选一段文字 → 按上下文给修改建议。
                    # 与 act=="direction_at" 不同：那个回答「这一处该接什么」，
                    # 这个回答「这一处**已有的文字**该怎么改」。
                    pk = body.get("partKey") or ""
                    snippet = body.get("snippet") or ""
                    if not snippet.strip():
                        return self._json({"ok": False, "error": "没有选中文字"})
                    sugs, err = _suggest_mods(
                        d, pk, snippet,
                        tone=body.get("tone"),
                        style=body.get("style"),
                        start=body.get("start"),
                        end=body.get("end"))
                    if err:
                        return self._json({"ok": False, "error": err})
                    return self._json({"ok": True, "suggestions": sugs})

                if act == "apply_mod":
                    # 按建议改写选中的文字 → 直接改写源文本并落盘。
                    pk = body.get("partKey") or ""
                    snippet = body.get("snippet") or ""
                    suggestion = body.get("suggestion") or ""
                    if not snippet.strip():
                        return self._json({"ok": False, "error": "没有选中文字"})
                    if not suggestion.strip():
                        return self._json({"ok": False, "error": "没有修改建议"})
                    new_text, err = _apply_mod(
                        d, pk, snippet, suggestion,
                        tone=body.get("tone"),
                        style=body.get("style"),
                        strength=body.get("strength"),
                        start=body.get("start"),
                        end=body.get("end"))
                    if err:
                        return self._json({"ok": False, "error": err})
                    # 定位并替换 → 写回这一节
                    full, s, e = _locate_in_part(d, pk, snippet,
                                               body.get("start"), body.get("end"))
                    if s < 0:
                        return self._json({"ok": False, "error": "无法在正文里定位选中的文字"})
                    # ⚠️ 保留原文的首尾空白：只替换选中的那一段，
                    #   前后空白不动（否则整节排版会被拽动）。
                    _pre, _post = full[:s], full[e:]
                    merged = _pre + new_text + _post
                    prev = d["parts"].get(pk) or {}
                    item = {"status": "edited", "text": merged,
                            "updatedAt": int(time.time() * 1000)}
                    if prev.get("full"):
                        item["full"] = prev["full"]
                    d["parts"][pk] = item
                    d.setdefault("progress", {})[pk] = True
                    save_paper(d)
                    return self._json({"ok": True, "text": merged,
                                       "partKey": pk, "newText": new_text,
                                       "paper": d})

                if act == "refs":
                    op = body.get("op") or "add"
                    if op == "add":
                        t = body.get("title") or ""
                        if not t:
                            return self._json({"ok": False, "error": "缺少 title"})
                        d.setdefault("refs", []).append({
                            "key": "ref_%d" % (len(d.get("refs") or []) + 1),
                            "title": t,
                            "source": body.get("source") or "手动添加",
                            "year": body.get("year"),
                        })
                    elif op == "remove":
                        k = body.get("key")
                        d["refs"] = [r for r in d.get("refs", []) if r.get("key") != k]
                    elif op == "update":
                        k = body.get("key")
                        for r in d.get("refs", []):
                            if r.get("key") == k:
                                if "title" in body: r["title"] = body["title"]
                                if "source" in body: r["source"] = body["source"]
                                if "year" in body: r["year"] = body["year"]
                    save_paper(d)
                    return self._json({"ok": True, "refs": d.get("refs", [])})

                if act == "refs_autofill":
                    # 主人 2026-11-23 23:43：点一下就自动查现在缺少的文献。
                    # 流程：扫正文引用缺口 → 检索通道找真实文献 → 写进 refs 池
                    #      →（可选）顺带下载 PDF。
                    # 查不到就如实报 failed，**绝不编造文献**。
                    download = bool(body.get("download"))
                    enrich = body.get("enrich")
                    try:
                        enrich = int(enrich)
                    except Exception:
                        enrich = 6
                    enrich = max(0, min(20, enrich))
                    try:
                        res = _autofill_refs(d, spec, download=download,
                                             enrich=enrich)
                    except Exception as e:
                        return self._json({"ok": False,
                                           "error": "自动补齐失败：%s" % str(e)[:200]}, 500)
                    save_paper(d)
                    out = {"ok": True,
                           "addedCount": len(res["added"]),
                           "added": res["added"],
                           "gaps": res["gaps"],
                           "gapCount": len(res["gaps"]),
                           "failed": res["failed"],
                           "downloads": res["downloads"],
                           "queries": res["queries"],
                           "refs": d.get("refs", []),
                           "paper": d}
                    return self._json(out)

                if act == "refs_gaps":
                    # 只扫缺口，不检索（用于前端先展示"缺哪些"，让用户确认）
                    gaps = _collect_citation_gaps(d)
                    return self._json({"ok": True, "gaps": gaps,
                                       "gapCount": len(gaps)})

                if act == "cite_support":
                    # 单独重算支撑关系（正文改动后刷新）
                    return self._json({"ok": True, "citeSupport": _cite_support(d)})

                if act == "pins":
                    # P2 钉到边栏：钉住章节 → 右栏持续展示该节的真实回归结果
                    op = body.get("op") or "toggle"
                    key = body.get("part") or ""
                    # clear 不需要 part（清空全部）
                    if op == "clear":
                        d["pins"] = []
                        save_paper(d)
                        return self._json({"ok": True, "pins": [], "paper": d})
                    if not key:
                        return self._json({"ok": False, "error": "缺少 part"}, 400)
                    if key not in d["parts"]:
                        return self._json({"ok": False, "error": "未知章节"}, 400)
                    pins = d.setdefault("pins", [])
                    if op == "toggle":
                        if key in pins:
                            pins.remove(key)
                        else:
                            pins.append(key)
                    elif op == "add":
                        if key not in pins:
                            pins.append(key)
                    elif op == "remove":
                        if key in pins:
                            pins.remove(key)
                    save_paper(d)
                    return self._json({"ok": True, "pins": d.get("pins", []), "paper": d})

                if act == "method_weights":
                    # P2 强度=方法权重：统计方法 → 权重（1..3）
                    # 权重进生成 prompt：权重高的方法作为**主方法**被强调。
                    mw = d.setdefault("methodWeights", {})
                    m = body.get("method") or ""
                    if m:
                        w = body.get("weight")
                        if w is None:
                            # toggle：1 → 2 → 3 → 1
                            cur = int(mw.get(m) or 1)
                            w = cur % 3 + 1
                        try:
                            w = max(1, min(3, int(w)))
                        except Exception:
                            w = 1
                        if w == 1:
                            mw.pop(m, None)
                        else:
                            mw[m] = w
                    save_paper(d)
                    return self._json({"ok": True, "methodWeights": d.get("methodWeights", {}),
                                       "paper": d})

                if act == "compute":
                    method = body.get("method") or "fe_entity"
                    r = _run_compute(d, method, spec)
                    if r.get("ok"):
                        d.setdefault("compute", []).append({
                            "id": "cmp_%d" % (len(d.get("compute") or []) + 1),
                            "title": body.get("title") or method,
                            "method": method,
                            "result": r.get("detail"),
                            "n": r.get("n"), "r2": r.get("r2"),
                            "updatedAt": int(time.time() * 1000),
                        })
                        save_paper(d)
                    r["paper"] = d
                    return self._json(r)

                if act == "chart":
                    ct = body.get("chartType") or "bar"
                    r = _chart_payload(d, "chart", ct, spec)
                    fig = None
                    if r.get("ok"):
                        fig = {"id": "fig_%d" % (len(d.get("figures") or []) + 1),
                               "title": body.get("title") or "图：%s" % ct,
                               "kind": "chart", "chartType": ct,
                               "data": {k: v for k, v in r.items() if k != "ok"},
                               "caption": "数据来源：实时回归预览（%s）" % code}
                        d.setdefault("figures", []).append(fig)
                        save_paper(d)
                    r["figure"] = fig
                    return self._json(r)

                if act == "table":
                    r = _table_payload(d, spec)
                    tb = None
                    if r.get("ok"):
                        tb = {"id": "tbl_%d" % (len(d.get("tables") or []) + 1),
                              "title": body.get("title") or r.get("title"),
                              "kind": "table", "columns": r["columns"],
                              "rows": r["rows"], "caption": r["caption"]}
                        d.setdefault("tables", []).append(tb)
                        save_paper(d)
                    r["table"] = tb
                    return self._json(r)

                if act == "insert":
                    pk = body.get("partKey")
                    el = body.get("element") or {}
                    if pk not in d["parts"]:
                        return self._json({"ok": False, "error": "未知 part"})

                    # 情况 A：拖的是已建好的素材（带 id）→ 直接插入标记
                    # 情况 B：拖的是「实时预览里的图/表」→ 先按当前回归结果真的建出来再插入
                    if not el.get("id") and (el.get("kind") == "figure" or el.get("kind") == "table"):
                        if el.get("kind") == "figure":
                            ct = el.get("chartType") or "line"
                            r = _chart_payload(d, "chart", ct, spec)
                            if not r.get("ok"):
                                return self._json({"ok": False, "error": r.get("error") or "图表生成失败"})
                            fig = {"id": "fig_%d" % (len(d.get("figures") or []) + 1),
                                   "title": el.get("title") or ("图：%s" % ct),
                                   "kind": "chart", "chartType": ct,
                                   "data": {k: v for k, v in r.items() if k != "ok"},
                                   "caption": "数据来源：实时回归预览（%s）" % code}
                            d.setdefault("figures", []).append(fig)
                            el = {"id": fig["id"], "kind": "FIG"}
                        else:
                            t = _table_payload(d, spec)
                            if not t.get("ok"):
                                return self._json({"ok": False, "error": t.get("error") or "表格生成失败"})
                            tbl = {"id": "tbl_%d" % (len(d.get("tables") or []) + 1),
                                   "title": el.get("title") or "回归结果表", "kind": "table",
                                   "columns": t.get("columns") or [], "rows": t.get("rows") or [],
                                   "caption": "数据来源：实时回归预览"}
                            d.setdefault("tables", []).append(tbl)
                            el = {"id": tbl["id"], "kind": "TAB"}

                    _el_kind = (el.get("kind") or "chart").upper()
                    _el_tag = "TAB" if _el_kind in ("TAB", "TABLE") else "FIG"
                    marker = "\n\n<<%s:%s>>\n\n" % (_el_tag, el.get("id", "x"))
                    txt = d["parts"][pk].get("text") or ""
                    if marker not in txt:
                        d["parts"][pk]["text"] = txt + marker
                    d["parts"][pk]["status"] = "edited"
                    d["parts"][pk]["updatedAt"] = int(time.time() * 1000)
                    save_paper(d)
                    return self._json({"ok": True, "part": pk, "paper": d})

                if act == "reset":
                    pk = body.get("partKey") or ""
                    if pk in d["parts"]:
                        d["parts"][pk] = {"status": "empty", "text": "", "updatedAt": 0}
                        d["progress"][pk] = False
                    save_paper(d)
                    return self._json({"ok": True, "paper": d})

                if act == "lit_download":
                    # 下载一篇文献到 01_文献/（必须过四重校验）
                    code = body.get("code") or d.get("code")
                    topic = body.get("topic") or d.get("topic")
                    rec = body.get("record") or {}
                    if not code or not topic:
                        return self._json({"ok": False, "error": "缺少 code/topic"}, 400)
                    ensure_paper_dirs(code, topic)
                    dest = os.path.join(paper_folder(code, topic), SUB_LIT)
                    try:
                        res = sources_mod.download_literature(rec, dest,
                                                             rec.get("title") or "")
                    except Exception as e:
                        return self._json({"ok": False, "error": str(e)}, 500)
                    if res.get("ok"):
                        man = load_json(_lit_manifest_path(code, topic), [])
                        entry = {
                            "key": "lit_%d" % (len(man) + 1),
                            "title": res.get("actual_title") or rec.get("title") or "",
                            "searchTitle": rec.get("title") or "",
                            "authors": rec.get("authors") or "",
                            "year": rec.get("year") or "",
                            "journal": rec.get("journal") or "",
                            "doi": rec.get("doi") or "",
                            "cited_by": rec.get("cited_by") or 0,
                            "oa_status": rec.get("oa_status") or "",
                            "pdf_url": rec.get("pdf_url") or "",
                            "landing_url": rec.get("landing_url") or "",
                            "file": os.path.relpath(res["path"], paper_folder(code, topic)),
                            "pages": res.get("pages") or 0,
                            "bytes": res.get("bytes") or 0,
                            "via": res.get("label") or "",
                            "downloadedAt": int(time.time() * 1000),
                        }
                        man.append(entry)
                        with open(_lit_manifest_path(code, topic), "w",
                                  encoding="utf-8") as f:
                            json.dump(man, f, ensure_ascii=False, indent=2)
                        # 同时把这条加进论文的 refs 池（可用、可引用）
                        _refs = d.setdefault("refs", [])
                        _have = set(_ref_key(r.get("title")) for r in _refs)
                        if _ref_key(entry["title"]) not in _have:
                            _refs.append({"key": "ref_%d" % len(_refs),
                                          "title": entry["title"],
                                          "source": entry["journal"],
                                          "year": entry["year"],
                                          "file": entry["file"],
                                          "doi": entry["doi"]})
                            d["refs"] = _dedupe_refs(_refs)
                        save_paper(d)
                        res["entry"] = entry
                        res["paper"] = d
                    return self._json(res)

                if act == "lit_dedupe":
                    # 按字节内容去重（不按文件名）
                    code = body.get("code") or d.get("code")
                    topic = body.get("topic") or d.get("topic")
                    dest = os.path.join(paper_folder(code, topic), SUB_LIT)
                    removed, kept = sources_mod.dedupe_by_content(dest)
                    return self._json({"ok": True, "removed": removed,
                                       "kept": kept})

                if act == "data_download":
                    code = body.get("code") or d.get("code")
                    topic = body.get("topic") or d.get("topic")
                    rec = body.get("record") or {}
                    if not code or not topic:
                        return self._json({"ok": False, "error": "缺少 code/topic"}, 400)
                    ensure_paper_dirs(code, topic)
                    dest = os.path.join(paper_folder(code, topic), SUB_DATA)
                    try:
                        res = sources_mod.download_data(rec, dest)
                    except Exception as e:
                        return self._json({"ok": False, "error": str(e)}, 500)
                    if res.get("ok"):
                        man = load_json(_data_manifest_path(code, topic), [])
                        man.append({
                            "key": "data_%d" % (len(man) + 1),
                            "title": rec.get("title") or "",
                            "publisher": rec.get("publisher") or "",
                            "format": rec.get("format") or "",
                            "url": rec.get("url") or "",
                            "source": rec.get("source") or "",
                            "file": os.path.relpath(res["path"], paper_folder(code, topic)),
                            "bytes": res.get("bytes") or 0,
                            "downloadedAt": int(time.time() * 1000),
                        })
                        with open(_data_manifest_path(code, topic), "w",
                                  encoding="utf-8") as f:
                            json.dump(man, f, ensure_ascii=False, indent=2)
                    return self._json(res)

                if act == "audit_fix":
                    # 审查项一键修改：按 finding.id 定位 → 改写该句 → 写回
                    fid = body.get("id") or ""
                    part = body.get("part") or ""
                    snippet = body.get("snippet") or ""
                    suggestion = body.get("suggestion") or ""
                    if not part:
                        return self._json({"ok": False, "error": "缺少 part"}, 400)
                    if part not in d["parts"]:
                        return self._json({"ok": False, "error": "未知章节"}, 400)
                    _txt = (d["parts"].get(part) or {}).get("full") or \
                           (d["parts"].get(part) or {}).get("text") or ""
                    _new, _err = _rewrite_snippet(
                        _txt, snippet, suggestion,
                        tone=d.get("tone"), style=d.get("style"),
                        strength=d.get("strength"), direction=d.get("direction"),
                        topic=d.get("topic") or "")
                    if not _new:
                        return self._json({"ok": False, "error": _err}, 400)
                    # 替换：精确定位的那句
                    # ⚠️ 实测踩过：_rewrite_snippet 的定位链是
                    #   find(snippet) → find(snippet[:12]) → 补写，
                    #   但这里（替换层）只查 find(snippet) 和 find(snippet[:12])，
                    #   **两者都查不到就直接 400** → 主人要的一键修改对这条失效
                    #   （实测 f1「参考文献条目未在正文引用」→ 「正文已变，找不到这一处」）。
                    # 现在：定位链与改写层**完全一致**，
                    #   且最后兜底改成**追加到节末**（而不是报错）——
                    #   主人要求「每个问题都能一键修改」。
                    _idx = _txt.find(snippet) if snippet else -1
                    _mode = "replace"
                    if _idx < 0 and snippet:
                        _h = snippet[:12]
                        _idx = _txt.find(_h) if _h else -1
                    if _idx >= 0:
                        _s0 = _txt.rfind("。", 0, _idx)
                        _s0 = (_s0 + 1) if _s0 >= 0 else 0
                        _e0 = _txt.find("。", _idx)
                        _e0 = (_e0 + 1) if _e0 > 0 else len(_txt)
                        _out = _txt[:_s0] + _new + _txt[_e0:]
                    else:
                        # 定位不到 → 追加到该节末尾（结构类缺失的合理落点）
                        _mode = "append"
                        _base = _txt.rstrip()
                        _out = (_base + _new) if _base else _new
                    _prev = d["parts"].get(part) or {}
                    d["parts"][part] = {"status": "edited", "text": _out,
                                        "full": _out,
                                        "updatedAt": int(time.time() * 1000)}
                    d["progress"][part] = True
                    ar = run_audit(d, spec)
                    save_paper(d)
                    return self._json({"ok": True, "part": part, "text": _out,
                                       "original": _txt, "audit": ar, "paper": d,
                                       "mode": _mode})

                if act == "audit":
                    # 手动触发一次完整性审计（确定性核验，不调模型）
                    ar = run_audit(d, spec)
                    save_paper(d)
                    return self._json({"ok": True, "audit": ar, "paper": d})

            if path == "/api/data/fred":
                # GET 风格但走 POST 统一入口：body = {series:[...], start, end}
                if not _fdata:
                    return self._json({"ok": False, "error": "fars_data 模块不可用"}, 200)
                ids = body.get("series") or []
                if isinstance(ids, str):
                    ids = [ids]
                if not ids:
                    return self._json({"ok": False,
                                       "error": "缺 series（例：CPIAUCSL / SP500 / NFCI）"}, 200)
                res = _fdata.fred_fetch_many(ids, body.get("start") or "1990-01-01",
                                             body.get("end") or "2030-12-31")
                return self._json(res, 200)

            if path == "/api/lit/verify":
                # 参考文献表 → Crossref 权威核验
                if not _fdata:
                    return self._json({"ok": False, "error": "fars_data 模块不可用"}, 200)
                refs = body.get("refs") or []
                if not refs:
                    return self._json({"ok": False, "error": "缺 refs"}, 200)
                res = _fdata.verify_reference_list(refs)
                if body.get("want_md"):
                    res["markdown"] = _fdata.md_report(res)
                return self._json(res, 200)

            if path == "/api/lit/search":
                # 文献检索：OpenAlex + arXiv 并行
                if not _fdata:
                    return self._json({"ok": False, "error": "fars_data 模块不可用"}, 200)
                q = (body.get("query") or "").strip()
                if not q:
                    return self._json({"ok": False, "error": "缺 query"}, 200)
                out = {"ok": True, "query": q}
                out["openalex"] = _fdata.openalex_search(
                    q, per_page=int(body.get("per_page") or 8),
                    year_from=body.get("year_from"), year_to=body.get("year_to"))
                if body.get("arxiv"):
                    out["arxiv"] = _fdata.arxiv_search(
                        body.get("arxiv_query") or ("all:" + q.replace(" ", " AND all:")),
                        max_results=int(body.get("per_page") or 6))
                return self._json(out, 200)

            if path == "/api/data/catalog":
                # 可用序列/检索器清单（前端下拉用）
                if not _fdata:
                    return self._json({"ok": False, "error": "fars_data 模块不可用"}, 200)
                return self._json({"ok": True,
                                   "fred": _fdata.FRED_SERIES,
                                   "fred_market": _fdata.FRED_MARKET,
                                   "sources": ["FRED", "Crossref", "OpenAlex", "arXiv"],
                                   "yahoo": {"ok": False,
                                             "note": "Yahoo 现需 cookie+crumb，匿名不可用；"
                                                     "市场序列建议走 FRED"}}, 200)

            if path == "/api/ksn/advise":
                code = body.get("code")
                topic = body.get("topic")
                limit = body.get("limit") or 6
                try:
                    res, st = ksn_advise(code, topic, limit)
                    return self._json(res, st)
                except Exception as e:
                    return self._json({"ok": False, "advices": [],
                                       "summary": "", "fallback": str(e)[:160],
                                       "source": "hot"}, 200)
            if path == "/api/steer":
                code = body.get("code")
                topic = body.get("topic")
                if not code or topic not in ("ESG", "Machine", "Stock"):
                    return self._json({"error": "参数非法"}, 400)
                steer = load_steering()
                key = spec_key(code, topic)
                patch = body.get("patch") or {}
                try:
                    clean = validate_steering(topic, patch)
                except ValueError as e:
                    return self._json({"error": str(e)}, 400)
                cur = dict(steer.get(key) or {})
                cur.update(clean)
                cur["updatedAt"] = int(__import__("time").time() * 1000)
                steer[key] = cur
                tmp = STEER_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(steer, f, ensure_ascii=False, indent=2)
                os.replace(tmp, STEER_FILE)
                self._mirror_steering()
                return self._json({"ok": True, "key": key, "steering": cur,
                                   "effective": effective_spec(code, topic)})
            if path == "/api/steer/clear":
                code, topic = body.get("code"), body.get("topic")
                steer = load_steering()
                steer.pop(spec_key(code, topic), None)
                tmp = STEER_FILE + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(steer, f, ensure_ascii=False, indent=2)
                os.replace(tmp, STEER_FILE)
                self._mirror_steering()
                return self._json({"ok": True})
            if path == "/api/focus":
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json({"error": "缺少 text"}, 400)
                try:
                    return self._json(focus_attention(text))
                except Exception as e:
                    return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)
            if path == "/api/build":
                code, topic = body.get("code"), body.get("topic")
                if not code or topic not in ("ESG", "Machine", "Stock"):
                    return self._json({"error": "参数非法"}, 400)
                return self._json(start_build(code, topic))
            return self._json({"error": "not found"}, 404)
        except Exception as e:
            DIAG.exception("未捕获异常", e, where=self.path.split("?")[0])
            return self._json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def log_message(self, fmt, *args):
        """HTTP 请求流水 → 日志文件（内测用）。

        BaseHTTPRequestHandler 默认把请求日志打到 stderr；
        这里改写成自己的日志文件，**任何异常都吞掉**（绝不能因记日志打断请求）。
        ⚠️ 不要 print 到 stdout —— 打包版 stdout 无窗口，会丢掉。"""
        try:
            DIAG.request(self.command or "?", fmt % args if args else fmt, "-", 0)
        except Exception:
            pass


def main():
    _ensure_state_dir()
    _seed_samples_once()

    # 端口 / 主机：环境变量 > 配置文件 > 默认值
    # （设置页改的端口存 config.json，重启后生效）
    _cfg = load_config()
    host = os.environ.get("FARS_HOST") or _cfg.get("host") or HOST
    try:
        port = int(os.environ.get("FARS_PORT") or _cfg.get("port") or PORT)
    except Exception:
        port = PORT

    # 端口被占 → 往后找一个空的，别直接崩（发给别人的机器上 8790 可能被占）
    for attempt in range(20):
        try:
            srv = ThreadingHTTPServer((host, port), Handler)
            break
        except OSError:
            port += 1
            print("[FARS] 端口 %d 被占用，尝试 %d" % (port - 1, port))
    else:
        raise SystemExit("没有可用端口（%s）" % host)

    url = "http://%s:%d/" % ("127.0.0.1" if host in ("0.0.0.0", "127.0.0.1") else host, port)
    print("FARS dashboard on %s" % url)

    # 打包成 .app 后自动打开浏览器（源码运行时不开，避免打扰开发）
    if getattr(sys, "frozen", False):
        # onefile 模式首次启动要解压 ~90MB，实测要 12 秒 →
        # 先弹个提示窗口，别让人以为没反应。
        try:
            script = ('display dialog "FARS Dashboard 正在启动…" '
                      'with title "FARS Dashboard" buttons {"好"} default button 1')
            subprocess.Popen(["/usr/bin/osascript", "-e", script])
        except Exception:
            pass
        try:
            subprocess.Popen(["/usr/bin/open", url])
        except Exception:
            pass

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
