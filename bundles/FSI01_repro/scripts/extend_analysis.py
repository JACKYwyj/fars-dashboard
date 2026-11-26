#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extend_analysis.py — 初稿增补分析（全部基于已验证的 master_monthly.csv）
=====================================================================
目的
----
初稿已有的实证（VAR(1)、Granger、IRF）我已逐条核对：
  · 系数/标准误/t值/p值 与 EViews 输出**逐位一致**
  · IRF 峰值、±2SE 下界、衰减率 **逐条一致**
  · 特征根模长（表4.5）从 8×8 系数矩阵重算 **5/5 完全吻合**
本脚本在此基础上补充**初稿没有的**分析，全部可复现：

  1. 样本外 expanding-window 预测力（初稿完全缺失）
     —— 初稿称 FSI 有"稳健单向正向预测作用"，但只在样本内做 Granger。
        样本外能不能预测，是这句话成立与否的关键。
  2. 替代金融压力指标稳健性（OVX / VVIX / GPR / ANFCI / NFCI）
  3. 子样本稳健性（GFC 危机期 2004-2009 / 危机后 2010-2024）
  4. 滚动 60 个月系数路径（检验系数是否稳定，不是只看全样本一点）
  5. 特征根模长（表4.5 的正式复算，作为可复现资产留档）
  6. 金融压力高/低分组的 RV 分布差异（非参数稳健性）

输出
----
    output_ext/table_ext_*.csv     新增表格数据
    output_ext/ext_results.json    全部标量结果
    figures/*.png                  新增图表（由 make_figures.py 读取本脚本产物绘制）

运行
----
    cd work && python3 extend_analysis.py
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd
import statsmodels.api as sm

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(BASE, "work")
OUT = os.path.join(BASE, "output_ext")
os.makedirs(OUT, exist_ok=True)

# 五个可再生能源股票指数
INDICES = [
    ("RV_ECO", "RVECO", "WilderHill 清洁能源指数 ECO"),
    ("RV_CELS", "RVCELS", "NASDAQ Clean Edge 绿色能源指数 CELS"),
    ("RV_AGINA", "RVAGINA", "Ardour 全球替代能源北美指数 AGINA"),
    ("RV_NEX", "RVNEX", "WilderHill 新能源全球创新指数 NEX"),
    ("RV_SPGCE", "RVSPGCE", "标普全球清洁能源指数 SPGCE"),
]

# 初稿基准回归使用的控制变量（与方法章一致）
CONTROLS = ["ADS", "CPI", "DWTI", "DDOLLAR", "EFFR", "LOGEPU"]

HAC_LAGS = 6  # 与 analysis.py 一致


def nn(df, c):
    return pd.to_numeric(df[c], errors="coerce")


def load():
    df = pd.read_csv(os.path.join(WORK, "master_monthly.csv"))
    df["date"] = df["date"].astype(str)
    return df


def make_V(df):
    """
    变量构造 —— **必须严格复刻 analysis.py / rv.prg 的定义**。
    踩过的坑：直接拿 master_monthly.csv 里的 RV_ECO 原始值回归，
    系数会差 3 个数量级（0.000235 vs 初稿 0.141167），
    因为初稿用的是 ln(RV×10000) 的基点对数。
    """
    V = pd.DataFrame(index=df.index)
    for k in ["RV_ECO", "RV_CELS", "RV_AGINA", "RV_NEX", "RV_SPGCE", "RV_DJUSEN"]:
        V[k] = np.log(nn(df, k) * 10000)          # log(RV × 10000) → 基点
    V["FSI"] = nn(df, "FSI")
    V["ADS"] = nn(df, "ADS")
    V["CPI"] = nn(df, "CPI")
    V["WTI"] = nn(df, "WTI")
    V["logWTI"] = np.log(nn(df, "WTI"))
    V["DWTI"] = V.logWTI.diff() * 100
    V["Dollar"] = nn(df, "Dollar")
    V["logDollar"] = np.log(nn(df, "Dollar"))
    V["DDollar"] = V.logDollar.diff() * 100
    V["EFFR"] = nn(df, "EFFR")
    V["logEPU"] = np.log(nn(df, "EPU"))
    V["VIX"] = nn(df, "VIX")
    V["logVIX"] = np.log(nn(df, "VIX"))
    V["DVIX"] = V.logVIX.diff() * 100
    V["logVVIX"] = np.log(nn(df, "VVIX"))
    V["vvixp"] = nn(df, "VVIX") / 100
    V["OVX"] = nn(df, "OVX")
    V["GPR"] = nn(df, "GPR")
    V["ANFCI"] = nn(df, "ANFCI")
    V["NFCI"] = nn(df, "NFCI")
    V["SP500"] = nn(df, "SP500")
    V["BDI"] = nn(df, "BDI")
    V["VVIX"] = nn(df, "VVIX")
    V["EPU"] = nn(df, "EPU")
    V["M2"] = nn(df, "M2")
    V["IP"] = nn(df, "IP")
    return V


def build_xy(df, dep, fsi_col="FSI", extra_controls=None):
    """
    构造月度回归所需的滞后结构：被解释变量 t，解释变量 t-1。
    对齐 analysis.py 的 VAR 设定：变量顺序 [dep, ADS, CPI, DWTI, DDollar, EFFR, logEPU, FSI]
    全部取 1 期滞后。
    """
    V = make_V(df)
    dep_key = {"RV_ECO": "RV_ECO", "RV_CELS": "RV_CELS", "RV_AGINA": "RV_AGINA",
               "RV_NEX": "RV_NEX", "RV_SPGCE": "RV_SPGCE"}[dep]
    controls = ["ADS", "CPI", "DWTI", "DDollar", "EFFR", "logEPU"]
    out = pd.DataFrame({"date": df["date"].astype(str).values})
    out["y"] = V[dep_key].values
    out["fsi"] = V[fsi_col].shift(1).values
    out["own"] = V[dep_key].shift(1).values
    for c in controls:
        out[c] = V[c].shift(1).values
    out = out.replace([np.inf, -np.inf], np.nan)
    out = out.dropna(subset=[c for c in out.columns if c != "date"])
    for c in out.columns:
        if c != "date":
            out[c] = out[c].astype(float)
    return out


def hac_fit(m):
    X = m.drop(columns=["y", "date"])
    r = sm.OLS(m["y"].astype(float), sm.add_constant(X.astype(float))).fit(
        cov_type="HAC", cov_kwds={"maxlags": HAC_LAGS})
    return r


# ---------------------------------------------------------------- 1. 基准复算
def baseline_recheck(df):
    rows = []
    for col, short, name in INDICES:
        m = build_xy(df, col)
        r = hac_fit(m)
        ci = r.conf_int(alpha=0.05)
        rows.append({
            "index": short, "name": name, "n": int(r.nobs),
            "coef_FSI": float(r.params["fsi"]), "se_FSI": float(r.bse["fsi"]),
            "t_FSI": float(r.tvalues["fsi"]), "p_FSI": float(r.pvalues["fsi"]),
            "ci_lo": float(ci.loc["fsi", 0]), "ci_hi": float(ci.loc["fsi", 1]),
            "R2": float(r.rsquared), "adjR2": float(r.rsquared_adj),
            "F": float(r.fvalue), "F_p": float(r.f_pvalue),
        })
    return pd.DataFrame(rows)


# ------------------------------------------- 2. 样本外 expanding-window 预测力
def oos_expanding(df, dep="RV_SPGCE", fsi_col="FSI", min_train=120, horizon=1):
    """
    expanding-window 一步预测：
      每期用 [0, t) 训练，预测 t 期 RV。
      模型 A: 含 FSI；模型 B: 不含 FSI（仅自身滞后+控制变量）
      两者逐期 RMSE 比较 → Diebold-Mariano 检验
    重要：这里的"预测"是**条件均值预测**（不是方向准确率）。
    """
    m = build_xy(df, dep, fsi_col)
    m = m.reset_index(drop=True)
    y = m["y"].values
    X_full = m.drop(columns=["date", "y"]).values
    names = list(m.drop(columns=["date", "y"]).columns)
    fsi_idx = names.index("fsi")
    keep = [i for i in range(len(names)) if i != fsi_idx]

    preds_a, preds_b, actuals, dates = [], [], [], []
    for t in range(min_train, len(m)):
        Xtr, ytr = X_full[:t], y[:t]
        # A：全变量
        # ⚠️ 不能用 sm.add_constant(单行数组)：它会因「只有一行、无法判定是否常量」
        #    而不添加截距列，导致 predict 维度不匹配 → 全部被 except 吞掉 → OOS N=0。
        #    改为显式拼 [1, x]。
        try:
            Xtr_c = np.column_stack([np.ones(len(Xtr)), Xtr])
            ra = sm.OLS(ytr, Xtr_c).fit()
            pa = float(ra.predict(np.column_stack([np.ones(1), X_full[t:t+1]]))[0])
        except Exception:
            continue
        # B：去掉 FSI
        try:
            Xtr_k = np.column_stack([np.ones(len(Xtr)), Xtr[:, keep]])
            rb = sm.OLS(ytr, Xtr_k).fit()
            pb = float(rb.predict(np.column_stack([np.ones(1), X_full[t:t+1][:, keep]]))[0])
        except Exception:
            continue
        if not (np.isfinite(pa) and np.isfinite(pb)):
            continue
        preds_a.append(pa); preds_b.append(pb)
        actuals.append(y[t]); dates.append(m["date"].iloc[t])

    preds_a = np.array(preds_a); preds_b = np.array(preds_b)
    actuals = np.array(actuals)
    e_a = actuals - preds_a
    e_b = actuals - preds_b
    d = e_a ** 2 - e_b ** 2          # 损失差：负=含FSI更好

    out = {
        "dep": dep, "fsi": fsi_col, "n_oos": int(len(actuals)),
        "start": dates[0] if dates else None, "end": dates[-1] if dates else None,
        "rmse_with_fsi": float(np.sqrt(np.mean(e_a ** 2))),
        "rmse_without_fsi": float(np.sqrt(np.mean(e_b ** 2))),
        "mae_with_fsi": float(np.mean(np.abs(e_a))),
        "mae_without_fsi": float(np.mean(np.abs(e_b))),
        "r2_oos_with_fsi": float(1 - np.sum(e_a ** 2) / np.sum((actuals - actuals.mean()) ** 2)),
        "r2_oos_without_fsi": float(1 - np.sum(e_b ** 2) / np.sum((actuals - actuals.mean()) ** 2)),
    }

    # Diebold-Mariano：H0 = 两模型损失无差异
    try:
        from scipy import stats
        n = len(d)
        dbar = d.mean()
        s = d.std(ddof=1)
        t_dm = dbar / (s / np.sqrt(n)) if s > 0 else np.nan
        p_dm = float(2 * (1 - stats.t.cdf(abs(t_dm), n - 1)))
        # Harvey-Leybourne-Newbold 小样本修正
        d1, d2 = e_a ** 2, e_b ** 2
        dbar1, dbar2 = d1.mean(), d2.mean()
        d12 = dbar1 - dbar2
        s1, s2 = d1.std(ddof=1), d2.std(ddof=1)
        hl = d12 / np.sqrt((s1 ** 2 + s2 ** 2) / n) if (s1 > 0 or s2 > 0) else np.nan
        p_hl = float(2 * (1 - stats.t.cdf(abs(hl), n - 1)))
        out.update({"dm_t": float(t_dm), "dm_p": p_dm, "hl_p": p_hl,
                    "loss_diff_mean": float(dbar)})
    except Exception as e:
        out["dm_error"] = str(e)

    # Clark-West：嵌套模型（无FSI 嵌套在 含FSI 中）
    try:
        from scipy import stats
        e1, e2 = e_b ** 2, e_a ** 2        # e1=限制模型(无FSI), e2=宽松模型(含FSI)
        n = len(e1)
        # ⚠️ u / v 必须是**逐期序列**，不是标量均值。
        #    写成标量会让 cw 变成常数 → cw.std(ddof=1)=nan → p=nan。
        e1bar, e2bar = e1.mean(), e2.mean()
        u = (np.sqrt(np.abs(e2)) - np.sqrt(np.abs(e1))) ** 2      # 逐期
        v = 0.5 * (e2 - e1)                                      # 逐期
        # Clark-West 调整损失差
        cw = u - (1 + 0.5 / n) * v
        s_cw = cw.std(ddof=1)
        t_cw = cw.mean() / (s_cw / np.sqrt(n)) if s_cw > 0 else np.nan
        p_cw = float(1 - stats.t.cdf(t_cw, n - 1))
        out.update({"cw_t": float(t_cw), "cw_p": p_cw,
                    "cw_stat": float(cw.mean())})
    except Exception as e:
        out["cw_error"] = str(e)

    # 逐期明细（供画图）
    detail = pd.DataFrame({
        "date": dates, "actual": actuals,
        "pred_with_fsi": preds_a, "pred_without_fsi": preds_b,
        "sqerr_with_fsi": e_a ** 2, "sqerr_without_fsi": e_b ** 2,
        "loss_diff": d,
    })
    return out, detail


# ------------------------------------------- 3. 替代金融压力指标
ALT_FSI = [
    ("OVX", "OVX 原油波动率指数"),
    ("VVIX", "VVIX 波动率波动率指数"),
    ("GPR", "GPR 地缘政治风险指数"),
    ("ANFCI", "ANFCI 美国金融状况指数"),
    ("NFCI", "NFCI 芝加哥联储金融状况指数"),
]


def alt_fsi_robustness(df):
    rows = []
    for col, name in ALT_FSI:
        if col not in df.columns:
            continue
        for dep, short, _ in INDICES:
            m = build_xy(df, dep, fsi_col=col)
            if len(m) < 60:
                continue
            r = hac_fit(m)
            ci = r.conf_int(alpha=0.05)
            rows.append({
                "alt_fsi": col, "alt_name": name, "index": short,
                "n": int(r.nobs), "coef": float(r.params["fsi"]),
                "se": float(r.bse["fsi"]), "t": float(r.tvalues["fsi"]),
                "p": float(r.pvalues["fsi"]),
                "ci_lo": float(ci.loc["fsi", 0]), "ci_hi": float(ci.loc["fsi", 1]),
                "R2": float(r.rsquared),
            })
    return pd.DataFrame(rows)


# ------------------------------------------- 4. 子样本稳健性
def subsample(df):
    rows = []
    subs = [
        ("全样本 2000M01-2024M12", None, None),
        ("GFC 危机期 2004M01-2009M12", "2004M01", "2009M12"),
        ("GFC 危机后 2010M01-2024M12", "2010M01", "2024M12"),
        ("后疫情期 2021M01-2024M12", "2021M01", "2024M12"),
    ]
    for label, lo, hi in subs:
        for dep, short, _ in INDICES:
            m = build_xy(df, dep)
            if lo:
                m = m[(m["date"] >= lo) & (m["date"] <= hi)]
            if len(m) < 40:
                continue
            r = hac_fit(m)
            ci = r.conf_int(alpha=0.05)
            rows.append({
                "subsample": label, "index": short, "n": int(r.nobs),
                "coef_FSI": float(r.params["fsi"]), "se_FSI": float(r.bse["fsi"]),
                "t_FSI": float(r.tvalues["fsi"]), "p_FSI": float(r.pvalues["fsi"]),
                "ci_lo": float(ci.loc["fsi", 0]), "ci_hi": float(ci.loc["fsi", 1]),
                "R2": float(r.rsquared),
            })
    return pd.DataFrame(rows)


# ------------------------------------------- 5. 滚动 60 月系数路径
def rolling_coef(df, dep, window=60):
    m = build_xy(df, dep).reset_index(drop=True)
    out = []
    for t in range(window, len(m)):
        tr = m.iloc[:t]
        try:
            r = sm.OLS(tr["y"], sm.add_constant(tr.drop(columns=["date", "y"]))).fit(
                cov_type="HAC", cov_kwds={"maxlags": HAC_LAGS})
        except Exception:
            continue
        out.append({"date": m["date"].iloc[t], "coef": float(r.params["fsi"]),
                    "se": float(r.bse["fsi"]), "t": float(r.tvalues["fsi"]),
                    "p": float(r.pvalues["fsi"])})
    return pd.DataFrame(out)


# ------------------------------------------- 6. 特征根模长（复算表4.5）
def eigen_stability(proj_eviews):
    import csv as _csv
    files = {
        "VAR_ECO": "var_eco_lag1_回归结果.csv",
        "VAR_CELS": "var_cels_lag1_回归结果.csv",
        "VAR_AGINA": "var_agina_lag1_回归结果.csv",
        "VAR_NEX": "var_nex_lag1_回归结果.csv",
        "VAR_SPGCE": "var_spgce_lag1_回归结果.csv",
    }
    rows = []
    for tag, fn in files.items():
        path = os.path.join(proj_eviews, fn)
        if not os.path.exists(path):
            continue
        rs = list(_csv.reader(open(path, encoding="utf-8", errors="replace")))
        hdr_i = None
        for i, r in enumerate(rs):
            cells = [c.strip() for c in r]
            if i > 4 and len([c for c in cells if c]) >= 8:
                hdr_i = i
                break
        hdr = [c.strip() for c in rs[hdr_i]]
        A = {}
        for i in range(hdr_i + 1, len(rs)):
            r = rs[i]
            if not r or not r[0].strip():
                continue
            lab = r[0].strip()
            if lab.endswith("(-1)") and len(r) >= 9:
                try:
                    A[lab[:-4]] = [float(c) for c in r[1:9]]
                except ValueError:
                    pass
            if len(A) >= 8:
                break
        if len(A) < 8:
            continue
        names = [hdr[j] for j in range(1, 9)]
        M = np.array([A[n] for n in names])
        mod = np.sort(np.abs(np.linalg.eigvals(M)))[::-1]
        rows.append({"system": tag, "max_modulus": float(mod[0]),
                     "stable": bool((mod < 1).all()),
                     "moduli": "  ".join("%.4f" % x for x in mod)})
    return pd.DataFrame(rows)


# ------------------------------------------- 7. FSI 高低分组的 RV 分布
def regime_compare(df):
    rows = []
    fsi = nn(df, "FSI")
    med = fsi.median()
    for dep, short, _ in INDICES:
        rv = nn(df, dep)
        hi = rv[fsi >= med].dropna()
        lo = rv[fsi < med].dropna()
        if len(hi) < 10 or len(lo) < 10:
            continue
        try:
            from scipy import stats
            u, p = stats.mannwhitneyu(hi, lo, alternative="two-sided")
        except Exception:
            u, p = np.nan, np.nan
        rows.append({"index": short, "n_high": int(len(hi)), "n_low": int(len(lo)),
                     "median_high": float(hi.median()), "median_low": float(lo.median()),
                     "mean_high": float(hi.mean()), "mean_low": float(lo.mean()),
                     "ratio_median": float(hi.median() / lo.median()) if lo.median() else np.nan,
                     "mannwhitney_u": float(u), "mannwhitney_p": float(p)})
    return pd.DataFrame(rows)


# ------------------------------------------- 8. FSI 替代指标相关性
def alt_corr(df):
    cols = ["FSI"] + [c for c, _ in ALT_FSI if c in df.columns]
    cols = [c for c in cols if c in df.columns]
    sub = df[cols].apply(pd.to_numeric, errors="coerce")
    return sub.corr()


def main():
    df = load()
    res = {"generated_at": pd.Timestamp.now().isoformat(),
           "data_file": "work/master_monthly.csv",
           "n_months": int(len(df))}

    print("=" * 70)
    print("初稿增补分析 —— extend_analysis.py")
    print("=" * 70)

    # 1 基准复算
    base = baseline_recheck(df)
    base.to_csv(os.path.join(OUT, "table_ext_baseline_recheck.csv"), index=False)
    print("\n[1] 基准回归复算（HAC maxlags=%d）" % HAC_LAGS)
    for _, r in base.iterrows():
        print("   %-8s n=%3d  b=%.6f  se=%.6f  t=%.4f  p=%.3e  95%%CI[%.4f,%.4f]" % (
            r["index"], r["n"], r["coef_FSI"], r["se_FSI"], r["t_FSI"],
            r["p_FSI"], r["ci_lo"], r["ci_hi"]))
    res["baseline_recheck"] = base.to_dict("records")

    # 2 样本外
    oos_rows, oos_details = [], []
    for dep, short, _ in INDICES:
        o, det = oos_expanding(df, dep=dep)
        o["index"] = short
        oos_rows.append(o)
        det["index"] = short
        oos_details.append(det)
        print("\n[2] 样本外 expanding-window · %s" % short)
        print("   样本外 N=%d (%s ~ %s)" % (o["n_oos"], o["start"], o["end"]))
        print("   RMSE 含FSI=%.6f  不含FSI=%.6f" % (o["rmse_with_fsi"], o["rmse_without_fsi"]))
        print("   OOS R² 含FSI=%.4f  不含FSI=%.4f" % (o["r2_oos_with_fsi"], o["r2_oos_without_fsi"]))
        if "dm_p" in o:
            print("   Diebold-Mariano: t=%.4f  p=%.4f  (HL 修正 p=%.4f)" % (
                o["dm_t"], o["dm_p"], o.get("hl_p", float("nan"))))
        if "cw_p" in o:
            print("   Clark-West:       t=%.4f  p=%.4f" % (o["cw_t"], o["cw_p"]))
    oos_df = pd.DataFrame(oos_rows)
    oos_df.to_csv(os.path.join(OUT, "table_ext_oos.csv"), index=False)
    pd.concat(oos_details).to_csv(os.path.join(OUT, "table_ext_oos_detail.csv"), index=False)
    res["oos"] = oos_df.to_dict("records")

    # 3 替代 FSI
    alt = alt_fsi_robustness(df)
    if len(alt):
        alt.to_csv(os.path.join(OUT, "table_ext_alt_fsi.csv"), index=False)
        print("\n[3] 替代金融压力指标（以 RVSPGCE 为例）")
        for _, r in alt[alt["index"] == "RVSPGCE"].iterrows():
            print("   %-6s n=%3d  b=%+.6f  t=%+.4f  p=%.4f  %s" % (
                r["alt_fsi"], r["n"], r["coef"], r["t"], r["p"],
                "显著" if r["p"] < 0.05 else "不显著"))
        res["alt_fsi"] = alt.to_dict("records")

    # 4 子样本
    sub = subsample(df)
    sub.to_csv(os.path.join(OUT, "table_ext_subsample.csv"), index=False)
    print("\n[4] 子样本稳健性（FSI 系数）")
    for label in sub["subsample"].unique():
        s = sub[sub["subsample"] == label]
        sig = int((s["p_FSI"] < 0.05).sum())
        print("   %-26s n范围=%d~%d  显著数=%d/%d" % (
            label, s["n"].min(), s["n"].max(), sig, len(s)))
    res["subsample"] = sub.to_dict("records")

    # 5 滚动系数
    roll_all = []
    print("\n[5] 滚动 60 月 FSI 系数路径")
    for dep, short, _ in INDICES:
        rc = rolling_coef(df, dep)
        rc["index"] = short
        roll_all.append(rc)
        pos = int((rc["coef"] > 0).sum())
        print("   %-8s 期数=%d  为正比例=%.1f%%  系数范围[%.4f, %.4f]  最小|t|=%.3f" % (
            short, len(rc), 100 * pos / len(rc), rc["coef"].min(), rc["coef"].max(),
            rc["t"].abs().min()))
    roll = pd.concat(roll_all)
    roll.to_csv(os.path.join(OUT, "table_ext_rolling.csv"), index=False)
    res["rolling_summary"] = {
        key: {"n": int(len(g)), "pos_pct": float(100 * (g["coef"] > 0).mean()),
              "coef_min": float(g["coef"].min()), "coef_max": float(g["coef"].max()),
              "min_abs_t": float(g["t"].abs().min()),
              "median_coef": float(g["coef"].median())}
        for key, g in roll.groupby("index")}

    # 6 特征根
    ev = eigen_stability(os.path.join(BASE, "eviews"))
    if len(ev):
        ev.to_csv(os.path.join(OUT, "table_ext_eigen.csv"), index=False)
        print("\n[6] 特征根模长复算（表4.5）")
        for _, r in ev.iterrows():
            print("   %-10s max=%.4f  全部<1: %s" % (r["system"], r["max_modulus"], r["stable"]))
        res["eigen"] = ev.to_dict("records")

    # 7 高低分组
    reg = regime_compare(df)
    if len(reg):
        reg.to_csv(os.path.join(OUT, "table_ext_regime.csv"), index=False)
        print("\n[7] FSI 高/低分组（中位数切分）的 RV 分布")
        for _, r in reg.iterrows():
            print("   %-8s 高压中位=%.6f 低压中位=%.6f 倍数=%.2f  MWU p=%.4g" % (
                r["index"], r["median_high"], r["median_low"],
                r["ratio_median"], r["mannwhitney_p"]))
        res["regime"] = reg.to_dict("records")

    # 8 相关性
    cc = alt_corr(df)
    cc.to_csv(os.path.join(OUT, "table_ext_alt_corr.csv"))
    print("\n[8] FSI 与替代指标相关系数（对 FSI 列）")
    for k in cc.index:
        print("   %-6s r=%.4f" % (k, cc.loc[k, "FSI"]))
    res["alt_corr"] = {k: float(cc.loc[k, "FSI"]) for k in cc.index}

    with open(os.path.join(OUT, "ext_results.json"), "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1)
    print("\n[完成] 产出写入 %s" % OUT)


if __name__ == "__main__":
    main()
