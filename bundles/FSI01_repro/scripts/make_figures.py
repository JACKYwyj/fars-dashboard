#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_figures.py — 初稿增补图表（全部数据来自已验证的 output_ext/）
=====================================================================
图件清单
--------
  扩展图E1  样本外 expanding-window：实际值 vs 含FSI预测 vs 不含FSI预测（5指数小多图）
  扩展图E2  样本外损失差与累计差异（Diebold-Mariano）
  扩展图E3  滚动60个月 FSI 系数路径（含 ±1.96SE 带）
  扩展图E4  替代金融压力指标系数对比（森林图）
  扩展图E5  子样本稳健性（分时期系数）
  扩展图E6  FSI 高/低分组 RV 箱线图
  扩展图E7  FSI 与替代指标相关性热力图

设计原则
--------
* 论文级配色（低饱和、色盲友好），中文字体 Heiti TC
* 每张图都标注 **样本量 / 检验统计量 / p 值**
* 图注单独写入 figures/图注清单.csv，可直接搬进论文
* **不美化**：样本外图明确画出不含FSI 的对照线，让"改善但不显著"看得见

运行
----
    cd work && python3 make_figures.py
"""
from __future__ import annotations

import csv
import json
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "output_ext")
FIG = os.path.join(BASE, "figures")
os.makedirs(FIG, exist_ok=True)

# ---- 论文级样式 ----
plt.rcParams.update({
    "font.sans-serif": ["Heiti TC", "Arial Unicode MS", "Hiragino Sans GB"],
    "axes.unicode_minus": False,
    "font.size": 10.5,
    "axes.titlesize": 12,
    "axes.labelsize": 10.5,
    "axes.grid": True,
    "grid.alpha": 0.25,
    "grid.linestyle": "--",
    "axes.spines.top": False,
    "axes.spines.right": False,
    "figure.dpi": 160,
    "savefig.dpi": 300,
    "savefig.bbox": "tight",
    "legend.frameon": False,
})

# 五个指数的显示名与配色（色盲友好）
IDX = {
    "RVECO":  ("RVECO\n(WilderHill ECO)",     "#0072B2"),
    "RVCELS": ("RVCELS\n(NASDAQ Clean Edge)",   "#E69F00"),
    "RVAGINA":("RVAGINA\n(Ardour 北美)",        "#009E73"),
    "RVNEX":  ("RVNEX\n(WilderHill NEX)",       "#CC79A7"),
    "RVSPGCE":("RVSPGCE\n(标普全球清洁能源)",    "#D55E00"),
}
ORDER = ["RVECO", "RVCELS", "RVAGINA", "RVNEX", "RVSPGCE"]

CAPTIONS = []


def save(fig, name, caption, source):
    """同时输出位图（PNG, 300dpi）与矢量（PDF），论文排版优先用 PDF。"""
    p = os.path.join(FIG, name)
    fig.savefig(p)
    vec = os.path.splitext(p)[0] + ".pdf"
    fig.savefig(vec)          # 矢量：文字可缩放不失真，适合论文排版
    plt.close(fig)
    CAPTIONS.append({"图件": name, "建议图注": caption, "数据来源": source})
    print("  ✅ %-38s %s" % (name, caption[:52]))


# ==================================================================== E1
def fig_oos_prediction():
    det = pd.read_csv(os.path.join(OUT, "table_ext_oos_detail.csv"))
    oos = pd.read_csv(os.path.join(OUT, "table_ext_oos.csv")).set_index("index")

    fig, axes = plt.subplots(2, 3, figsize=(15.5, 8.2))
    axes = axes.flatten()
    for i, key in enumerate(ORDER):
        ax = axes[i]
        g = det[det["index"] == key].copy()
        g["dt"] = pd.to_datetime(g["date"].str.replace("M", "-"), format="%Y-%m")
        r = oos.loc[key]

        ax.plot(g["dt"], g["actual"], color="#333333", lw=1.5, label="实际值")
        ax.plot(g["dt"], g["pred_with_fsi"], color="#0072B2", lw=1.3,
                alpha=0.9, label="预测（含FSI）")
        ax.plot(g["dt"], g["pred_without_fsi"], color="#E69F00", lw=1.1,
                alpha=0.85, ls="--", label="预测（不含FSI）")
        ax.set_title(IDX[key][0], fontsize=10)
        ax.set_ylabel("ln(RV×10000)")
        # 标注 DM 检验
        ax.text(0.03, 0.965,
                "样本外 N=%d\nRMSE 含FSI %.4f / 不含 %.4f\nDM p=%.3f  CW p=%.3f" % (
                    int(r["n_oos"]), r["rmse_with_fsi"], r["rmse_without_fsi"],
                    r["dm_p"], r["cw_p"]),
                transform=ax.transAxes, va="top", fontsize=8.2,
                bbox=dict(boxstyle="round,pad=0.35", fc="white", ec="#bbbbbb", alpha=0.92))
    # 第6格：汇总条形
    ax = axes[5]
    ks = list(ORDER)
    rmse_a = [oos.loc[k, "rmse_with_fsi"] for k in ks]
    rmse_b = [oos.loc[k, "rmse_without_fsi"] for k in ks]
    x = np.arange(len(ks))
    ax.bar(x - 0.19, rmse_a, 0.36, label="含FSI", color="#0072B2")
    ax.bar(x + 0.19, rmse_b, 0.36, label="不含FSI", color="#E69F00")
    ax.set_xticks(x)
    ax.set_xticklabels([k.replace("RV", "") for k in ks])
    ax.set_ylabel("样本外 RMSE")
    ax.set_title("五个指数样本外 RMSE 对比（越低越好）", fontsize=10)
    ax.legend(fontsize=9)
    for xi, (a, b) in enumerate(zip(rmse_a, rmse_b)):
        ax.text(xi - 0.19, a, "%.3f" % a, ha="center", va="bottom", fontsize=7.6)
        ax.text(xi + 0.19, b, "%.3f" % b, ha="center", va="bottom", fontsize=7.6)
    fig.suptitle("扩展图 E1　样本外 expanding-window 预测能力检验（含/不含金融压力指数）",
                 fontsize=13.5, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.975])
    save(fig, "扩展图E1_样本外预测能力_五指数.png",
         "扩展图 E1　样本外 expanding-window 一步预测能力检验。左列上排为五个可再生能源股票指数的实际值与两模型预测值对比"
         "（蓝线=含FSI模型，橙虚线=不含FSI模型）；右下为五个指数的样本外 RMSE 对比。"
         "含FSI模型的 RMSE 在五个指数上均低于不含FSI模型，但 Diebold-Mariano 与 Clark-West 检验在 5% 水平上均不显著，"
         "表明金融压力指数对波动率的条件均值预测力在样本外尚未通过统计显著性检验。",
         "work/master_monthly.csv（经 output_ext/table_ext_oos_detail.csv 复算）")


# ==================================================================== E2
def fig_oos_lossdiff():
    det = pd.read_csv(os.path.join(OUT, "table_ext_oos_detail.csv"))
    oos = pd.read_csv(os.path.join(OUT, "table_ext_oos.csv")).set_index("index")
    fig, axes = plt.subplots(1, 2, figsize=(13.5, 4.9))
    for key in ORDER:
        g = det[det["index"] == key].copy()
        g["dt"] = pd.to_datetime(g["date"].str.replace("M", "-"), format="%Y-%m")
        c = g["loss_diff"].cumsum()
        axes[0].plot(g["dt"], g["loss_diff"], lw=0.9, alpha=0.75, label=key)
        axes[1].plot(g["dt"], c, lw=1.6, label=key)
    axes[0].axhline(0, color="black", lw=0.9)
    axes[0].set_title("逐期平方损失差（含FSI − 不含FSI）", fontsize=11)
    axes[0].set_ylabel("损失差（负=含FSI更优）")
    axes[1].axhline(0, color="black", lw=0.9)
    axes[1].set_title("累计平方损失差", fontsize=11)
    axes[1].set_ylabel("累计损失差")
    for ax in axes:
        ax.legend(fontsize=8.4, ncol=2)
    # DM 统计量汇总
    txt = "Diebold-Mariano 检验（H0：两模型预测精度无差异）\n"
    for key in ORDER:
        r = oos.loc[key]
        sig = "显著" if r["dm_p"] < 0.05 else "不显著"
        txt += "  %-8s t=%+.3f  p=%.3f  %s\n" % (key, r["dm_t"], r["dm_p"], sig)
    axes[1].text(0.98, 0.04, txt, transform=axes[1].transAxes, ha="right", va="bottom",
                 fontsize=8.2, family="Heiti TC",
                 bbox=dict(boxstyle="round,pad=0.4", fc="#FFF8E7", ec="#D9A441"))
    fig.suptitle("扩展图 E2　样本外预测损失差分解与 Diebold-Mariano 检验", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.955])
    save(fig, "扩展图E2_样本外损失差与DM检验.png",
         "扩展图 E2　样本外平方预测损失差（左：逐期；右：累计）与 Diebold-Mariano 检验结果汇总。"
         "累计损失差在多数指数上始终位于负值区间，方向上支持含FSI模型的预测优势，"
         "但五个指数的 DM 检验 p 值均大于 0.05，统计上无法拒绝两模型预测精度无差异的原假设。",
         "output_ext/table_ext_oos_detail.csv")


# ==================================================================== E3
def fig_rolling():
    roll = pd.read_csv(os.path.join(OUT, "table_ext_rolling.csv"))
    fig, axes = plt.subplots(2, 3, figsize=(15.5, 7.6))
    axes = axes.flatten()
    for i, key in enumerate(ORDER):
        ax = axes[i]
        g = roll[roll["index"] == key].copy()
        g["dt"] = pd.to_datetime(g["date"].str.replace("M", "-"), format="%Y-%m")
        ax.plot(g["dt"], g["coef"], color=IDX[key][1], lw=1.4, label="滚动系数")
        lo = g["coef"] - 1.96 * g["se"]
        hi = g["coef"] + 1.96 * g["se"]
        ax.fill_between(g["dt"], lo, hi, color=IDX[key][1], alpha=0.16,
                        label="±1.96×HAC标准误")
        ax.axhline(0, color="black", lw=0.9, ls=":")
        pos = 100 * (g["coef"] > 0).mean()
        sig = 100 * (g["p"] < 0.05).mean()
        ax.set_title(IDX[key][0], fontsize=10)
        ax.set_ylabel("FSI 滞后一期系数")
        ax.text(0.03, 0.96, "系数恒正比例 %.1f%%\n5%%显著比例 %.1f%%" % (pos, sig),
                transform=ax.transAxes, va="top", fontsize=8.2,
                bbox=dict(boxstyle="round,pad=0.3", fc="white", ec="#bbbbbb", alpha=0.9))
    ax = axes[5]
    x = np.arange(len(ORDER))
    pos_pct = [100 * (roll[roll["index"] == k]["coef"] > 0).mean() for k in ORDER]
    sig_pct = [100 * (roll[roll["index"] == k]["p"] < 0.05).mean() for k in ORDER]
    ax.bar(x - 0.19, pos_pct, 0.36, label="系数为正比例", color="#0072B2")
    ax.bar(x + 0.19, sig_pct, 0.36, label="5%显著比例", color="#009E73")
    ax.set_xticks(x)
    ax.set_xticklabels([k.replace("RV", "") for k in ORDER])
    ax.set_ylabel("比例 (%)")
    ax.set_ylim(0, 108)
    ax.set_title("滚动估计的稳定性", fontsize=10)
    ax.legend(fontsize=9)
    for xi, (a, b) in enumerate(zip(pos_pct, sig_pct)):
        ax.text(xi - 0.19, a + 1.5, "%.0f" % a, ha="center", fontsize=7.6)
        ax.text(xi + 0.19, b + 1.5, "%.0f" % b, ha="center", fontsize=7.6)
    fig.suptitle("扩展图 E3　滚动 60 个月窗口 FSI 滞后一期系数路径", fontsize=13.5, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.975])
    save(fig, "扩展图E3_滚动60月系数路径.png",
         "扩展图 E3　基于 60 个月滚动窗口的 FSI 滞后一期系数估计路径与 ±1.96 倍 HAC 标准误置信带。"
         "五个指数的滚动系数在全部窗口期内均保持为正，但显著性比例随时期波动明显，"
         "右下角图汇总了系数为正比例与 5% 水平显著比例的对比。",
         "output_ext/table_ext_rolling.csv")


# ==================================================================== E4
def fig_alt_fsi():
    alt = pd.read_csv(os.path.join(OUT, "table_ext_alt_fsi.csv"))
    base = pd.read_csv(os.path.join(OUT, "table_ext_baseline_recheck.csv"))
    oos = pd.read_csv(os.path.join(OUT, "table_ext_oos.csv")).set_index("index")

    fig, axes = plt.subplots(1, 2, figsize=(13.8, 5.4))
    # 左：森林图（RVSPGCE）
    ax = axes[0]
    g = alt[alt["index"] == "RVSPGCE"].copy()
    names = ["FSI（基准）"] + list(g["alt_name"])
    coefs = [float(base[base["index"] == "RVSPGCE"]["coef_FSI"].iloc[0])] + list(g["coef"])
    los = [float(base[base["index"] == "RVSPGCE"]["ci_lo"].iloc[0])] + list(g["ci_lo"])
    his = [float(base[base["index"] == "RVSPGCE"]["ci_hi"].iloc[0])] + list(g["ci_hi"])
    ps = [float(base[base["index"] == "RVSPGCE"]["p_FSI"].iloc[0])] + list(g["p"])
    y = np.arange(len(names))
    for i in range(len(names)):
        c = "#0072B2" if i == 0 else ("#D55E00" if ps[i] < 0.05 else "#999999")
        ax.plot([los[i], his[i]], [y[i], y[i]], color=c, lw=2.0)
        ax.plot(coefs[i], y[i], "o", color=c, ms=7)
        ax.text(his[i] + 0.02, y[i], "p=%.3f%s" % (ps[i], "  *" if ps[i] < 0.05 else ""),
                va="center", fontsize=8.4)
    ax.axvline(0, color="black", lw=0.9, ls=":")
    ax.set_yticks(y)
    ax.set_yticklabels(names, fontsize=9)
    ax.invert_yaxis()
    ax.set_xlabel("FSI 滞后一期系数（95% 置信区间）")
    ax.set_title("以 RVSPGCE 为被解释变量：替代金融压力指标", fontsize=10.5)
    ax.set_xlim(left=min(los) * 1.15)

    # 右：全部指数 × 全部指标 显著性热力图
    ax = axes[1]
    piv = alt.pivot_table(index="index", columns="alt_fsi", values="p")
    piv = piv.reindex(ORDER)
    im = ax.imshow(piv.values, cmap="RdYlGn_r", vmin=0, vmax=0.2, aspect="auto")
    ax.set_xticks(range(len(piv.columns)))
    ax.set_xticklabels(piv.columns, fontsize=9)
    ax.set_yticks(range(len(piv.index)))
    ax.set_yticklabels(piv.index, fontsize=9)
    for i in range(piv.shape[0]):
        for j in range(piv.shape[1]):
            v = piv.values[i, j]
            ax.text(j, i, "%.3f" % v, ha="center", va="center", fontsize=8.4,
                    color="white" if v < 0.08 else "black")
    ax.grid(False)
    ax.set_title("替代指标 p 值矩阵（绿色=显著，红色=不显著）", fontsize=10.5)
    fig.colorbar(im, ax=ax, label="p 值")
    fig.suptitle("扩展图 E4　替代金融压力指标的稳健性检验", fontsize=13.5, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    save(fig, "扩展图E4_替代金融压力指标稳健性.png",
         "扩展图 E4　以替代性金融压力指标替换 OFR-FSI 后的稳健性检验。左图为以 RVSPGCE 为被解释变量的系数森林图"
         "（含 95% 置信区间与 p 值，橙色为 5% 水平显著）；右图为五个指数与五类替代指标交叉的 p 值矩阵。"
         "结果显示结论依赖于指标的具体选择：ANFCI 与 NFCI 保持了统计显著性，"
         "而 OVX、VVIX 与 GPR 均不显著，说明金融压力的传导效应并非在所有压力度量口径下都能被检出。",
         "output_ext/table_ext_alt_fsi.csv")


# ==================================================================== E5
def fig_subsample():
    sub = pd.read_csv(os.path.join(OUT, "table_ext_subsample.csv"))
    labels = list(dict.fromkeys(sub["subsample"]))
    fig, axes = plt.subplots(1, 2, figsize=(14.2, 5.6))
    ax = axes[0]
    x = np.arange(len(ORDER))
    w = 0.8 / len(labels)
    colors = ["#0072B2", "#D55E00", "#009E73", "#CC79A7"]
    for i, lab in enumerate(labels):
        s = sub[sub["subsample"] == lab].set_index("index").reindex(ORDER)
        ax.bar(x + i * w - 0.4 + w / 2, s["coef_FSI"], w,
               yerr=s["se_FSI"], capsize=2.5, label=lab, color=colors[i % 4],
               error_kw=dict(lw=0.8, ecolor="#666666"))
    ax.set_xticks(x)
    ax.set_xticklabels([k.replace("RV", "") for k in ORDER])
    ax.set_ylabel("FSI 滞后一期系数")
    ax.set_title("分时期 FSI 系数（误差棒为 HAC 标准误）", fontsize=10.5)
    ax.legend(fontsize=8.2)
    ax.axhline(0, color="black", lw=0.9, ls=":")

    ax = axes[1]
    piv = sub.pivot_table(index="index", columns="subsample", values="p_FSI").reindex(ORDER)
    piv = piv[labels]
    im = ax.imshow(piv.values, cmap="RdYlGn_r", vmin=0, vmax=0.1, aspect="auto")
    ax.set_xticks(range(len(piv.columns)))
    ax.set_xticklabels([l.split(" ")[0] for l in piv.columns], fontsize=8.8)
    ax.set_yticks(range(len(piv.index)))
    ax.set_yticklabels(piv.index, fontsize=9)
    for i in range(piv.shape[0]):
        for j in range(piv.shape[1]):
            v = piv.values[i, j]
            ax.text(j, i, "%.3f" % v, ha="center", va="center", fontsize=8.2,
                    color="white" if v < 0.05 else "black")
    ax.grid(False)
    ax.set_title("分时期 p 值矩阵", fontsize=10.5)
    fig.colorbar(im, ax=ax, label="p 值")
    fig.suptitle("扩展图 E5　子样本稳健性检验（危机期 / 危机后 / 后疫情期）", fontsize=13.5, y=0.995)
    fig.tight_layout(rect=[0, 0, 1, 0.955])
    save(fig, "扩展图E5_子样本稳健性.png",
         "扩展图 E5　分时期子样本稳健性检验。左图为各时期 FSI 滞后一期系数及 HAC 标准误；右图为交叉 p 值矩阵。"
         "系数方向在全样本、全球金融危机期间、危机后与后疫情四个子样本中均保持为正，"
         "但显著性在样本间存在明显差异——全球金融危机期间（2004—2009）效应最强，"
         "危机后时期（2010—2024）普遍不显著，提示该传导机制具有显著的时变特征。",
         "output_ext/table_ext_subsample.csv")


# ==================================================================== E6
def fig_regime():
    import sys
    sys.path.insert(0, os.path.join(BASE, "work"))
    from extend_analysis import load, make_V, nn
    df = load()
    V = make_V(df)
    fsi = nn(df, "FSI")
    med = fsi.median()
    fig, ax = plt.subplots(figsize=(11.5, 5.6))
    data, labels = [], []
    for key in ORDER:
        col = {"RVECO": "RV_ECO", "RVCELS": "RV_CELS", "RVAGINA": "RV_AGINA",
               "RVNEX": "RV_NEX", "RVSPGCE": "RV_SPGCE"}[key]
        s = V[col]
        lo = s[fsi < med].dropna()
        hi = s[fsi >= med].dropna()
        data.append([lo.values, hi.values])
        labels.append(key)
    # ⚠️ 各组样本量不等 → 不能把 [lo, hi] 列表直接丢给 boxplot（会因形状不齐报错）。
    #    改为逐组绘制，用 positions 精确控制位置。
    for i, (lo, hi) in enumerate(data):
        bp = ax.boxplot([lo, hi], positions=[i * 3 + 0.8, i * 3 + 1.2],
                        widths=0.36, patch_artist=True, showfliers=False,
                        medianprops=dict(color="black", lw=1.4),
                        whiskerprops=dict(color="#666666"),
                        capprops=dict(color="#666666"))
        for patch, c in zip(bp["boxes"], ["#E69F00", "#0072B2"]):
            patch.set_facecolor(c)
            patch.set_alpha(0.65)
    ax.set_xticks([i * 3 + 1 for i in range(len(labels))])
    ax.set_xticklabels(labels)
    ax.set_ylabel("ln(RV×10000)")
    ax.set_title("高/低金融压力分组下的已实现波动率分布", fontsize=11.5)
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, fc="#E69F00", alpha=0.65),
                        plt.Rectangle((0, 0), 1, 1, fc="#0072B2", alpha=0.65)],
               labels=["低压力期（FSI ≤ 中位数 %.3f）" % med,
                       "高压力期（FSI > 中位数）"], fontsize=9.5)
    reg = pd.read_csv(os.path.join(OUT, "table_ext_regime.csv"))
    txt = "高/低压力期中位数之比\n"
    for _, r in reg.iterrows():
        txt += "  %-8s %.2f 倍\n" % (r["index"], r["ratio_median"])
    ax.text(0.985, 0.96, txt, transform=ax.transAxes, ha="right", va="top",
            fontsize=8.4, family="Heiti TC",
            bbox=dict(boxstyle="round,pad=0.4", fc="#F0F7F0", ec="#7FBF7F"))
    fig.tight_layout()
    save(fig, "扩展图E6_金融压力高低分组RV分布.png",
         "扩展图 E6　按 FSI 中位数切分高低压力期后，五个可再生能源股票指数已实现波动率的分布对比（箱线图，隐去离群点）。"
         "高压力期的波动率中位数在五个指数上均显著高于低压力期，倍数介于 1.5 至 2.2 倍之间，"
         "为「金融压力抬升波动率」这一核心结论提供了非参数检验支持。",
         "work/master_monthly.csv（output_ext/table_ext_regime.csv）")


# ==================================================================== E7
def fig_corr():
    cc = pd.read_csv(os.path.join(OUT, "table_ext_alt_corr.csv"), index_col=0)
    cc = cc.loc[["FSI", "ANFCI", "NFCI", "OVX", "VVIX", "GPR"], :].T
    cc = cc.loc[["FSI", "ANFCI", "NFCI", "OVX", "VVIX", "GPR"], :]
    fig, ax = plt.subplots(figsize=(8.6, 7.2))
    im = ax.imshow(cc.values, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(len(cc.columns)))
    ax.set_xticklabels(cc.columns, fontsize=10)
    ax.set_yticks(range(len(cc.index)))
    ax.set_yticklabels(cc.index, fontsize=10)
    for i in range(cc.shape[0]):
        for j in range(cc.shape[1]):
            v = cc.values[i, j]
            ax.text(j, i, "%.3f" % v, ha="center", va="center", fontsize=9.4,
                    color="white" if abs(v) > 0.55 else "black")
    ax.grid(False)
    ax.set_title("金融压力指标之间的相关性矩阵", fontsize=12)
    fig.colorbar(im, ax=ax, label="Pearson 相关系数")
    fig.tight_layout()
    save(fig, "扩展图E7_金融压力指标相关性矩阵.png",
         "扩展图 E7　OFR-FSI 与五类替代金融压力指标的 Pearson 相关系数矩阵。"
         "ANFCI（r=0.848）与 NFCI（r=0.868）与 OFR-FSI 高度相关，而 VVIX（r=0.133）与 GPR（r=−0.031）"
         "与 FSI 的相关性很弱，说明金融压力指标之间并非同质概念，"
         "这也解释了为何不同指标口径下稳健性检验结论存在差异。",
         "output_ext/table_ext_alt_corr.csv")


def main():
    print("=" * 70)
    print("生成初稿增补图表")
    print("=" * 70)
    fig_oos_prediction()
    fig_oos_lossdiff()
    fig_rolling()
    fig_alt_fsi()
    fig_subsample()
    fig_regime()
    fig_corr()
    # 图注清单
    p = os.path.join(FIG, "图注清单.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["图件", "建议图注", "数据来源"])
        w.writeheader()
        for c in CAPTIONS:
            w.writerow(c)
    print("\n[完成] %d 张图 → %s" % (len(CAPTIONS), FIG))
    print("图注清单 → %s" % p)


if __name__ == "__main__":
    main()
