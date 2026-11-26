#!/usr/bin/env python3
"""金融压力對可再生能源股票指數波動率影響 — 实证分析主程序
题目: 金融壓力對可再生能源股票指數波動率影響的實證研究
      An Empirical Study on the Impact of Financial Stress on Renewable Energy Stock Index Volatility
"""
import os, json, math, warnings
from math import erf, sqrt
import numpy as np, pandas as pd, statsmodels.api as sm
from statsmodels.tsa.stattools import adfuller, kpss, grangercausalitytests
from arch import arch_model
warnings.filterwarnings("ignore")

BASE=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT=os.path.join(BASE,"output"); os.makedirs(OUT,exist_ok=True)

df=pd.read_csv(os.path.join(BASE,"work","master_monthly.csv"), index_col=0)
df.index=[str(x) for x in df.index]
def nn(c): return pd.to_numeric(df[c], errors='coerce')

# ---------- 变量构造 (严格按 rv.prg 的定义) ----------
V=pd.DataFrame(index=df.index)
for k in ["RV_ECO","RV_CELS","RV_AGINA","RV_NEX","RV_SPGCE","RV_DJUSEN"]:
    V[k]=np.log(nn(k)*10000)          # log(RV * 10000)  -> bp
V["FSI"]=nn("FSI"); V["ADS"]=nn("ADS"); V["CPI"]=nn("CPI")
V["WTI"]=nn("WTI"); V["logWTI"]=np.log(nn("WTI")); V["DWTI"]=V.logWTI.diff()*100
V["Dollar"]=nn("Dollar"); V["logDollar"]=np.log(nn("Dollar")); V["DDollar"]=V.logDollar.diff()*100
V["EFFR"]=nn("EFFR"); V["logEPU"]=np.log(nn("EPU"))
V["VIX"]=nn("VIX"); V["logVIX"]=np.log(nn("VIX")); V["DVIX"]=V.logVIX.diff()*100
V["logVVIX"]=np.log(nn("VVIX")); V["vvixp"]=nn("VVIX")/100
V["logSP500"]=np.log(nn("SP500")); V["DSP500"]=V.logSP500.diff()*100
V["logBDI"]=np.log(nn("BDI")); V["DBDI"]=V.logBDI.diff()*100
V["M2"]=nn("M2"); V["IP"]=nn("IP")
V["RVSPGCE"]=V["RV_SPGCE"]

res_out={}

def w(name, obj):
    p=os.path.join(OUT,name); 
    if isinstance(obj,pd.DataFrame): obj.to_csv(p)
    elif isinstance(obj,str): open(p,"w").write(obj)
    return p

# ================= 1. 描述性统计 =================
desc_cols=["RV_ECO","RV_CELS","RV_AGINA","RV_NEX","RV_SPGCE","RV_DJUSEN",
           "FSI","ADS","CPI","WTI","EFFR","logEPU","VIX","Dollar","M2","IP"]
D=V[desc_cols].describe().T
D["obs"]=V[desc_cols].count()
res_out["desc"]=D
print("="*90); print("表1 描述性统计"); print(D.round(4).to_string())

# ================= 2. 单位根检验 =================
unit=[]
for c in ["RV_SPGCE","RV_ECO","RV_CELS","RV_AGINA","RV_NEX","RV_DJUSEN","FSI"]:
    s=V[c].dropna()
    ad=adfuller(s.values, autolag="AIC")
    try: kp=kpss(s.values, regression="c", nlags="auto")
    except Exception: kp=(np.nan,np.nan,"")
    unit.append({"var":c,"n":len(s),"ADF":ad[0],"ADF_p":ad[1],"ADF_stat":ad[2],
                 "KPSS_stat":kp[0],"KPSS_p":kp[1]})
U=pd.DataFrame(unit); res_out["unitroot"]=U
print("\n"+"="*90); print("表2 单位根检验 (ADF / KPSS)"); print(U.round(4).to_string(index=False))

# ================= 3. 基准回归: 对标 rv.prg 的 VAR 形式 =================
# rv.prg: VAR TableFSISPGCE{1..4}.LS  {1..4} RVSPGCE ADS CPI DWTI DDollar EFFR logEPU FSI
#  -> 每一阶是"被解释变量在方程右侧的滞后阶数"设定, 等价于多方程 VAR 中 RV 方程的设定
def build(lag_orders):
    """lag_orders: 变量顺序 -> 最大滞后阶; 构造多方程 VAR 的第 i 个方程(以 RVSPGCE 为焦点)"""
    order=["RVSPGCE","ADS","CPI","DWTI","DDollar","EFFR","logEPU","FSI"]
    X=pd.DataFrame(index=V.index)
    for v in order:
        for L in range(1, lag_orders+1):
            X[f"{v}_L{L}"]=V[v].shift(L)
    m=pd.concat([V["RVSPGCE"].rename("y"), X], axis=1).dropna()
    return m

def fit(m, label, robust=True):
    X=sm.add_constant(m.drop(columns="y"))
    r=sm.OLS(m["y"],X).fit(cov_type="HAC",cov_kwds={"maxlags":6}) if robust else sm.OLS(m["y"],X).fit()
    fsi_cols=[c for c in r.params.index if c.startswith("FSI_L")]
    if fsi_cols:
        cov=r.cov_params()
        b_sum=float(sum(r.params[c] for c in fsi_cols))
        var=sum(cov.loc[c,c] for c in fsi_cols)
        for i,a in enumerate(fsi_cols):
            for bb in fsi_cols[i+1:]:
                var += 2*cov.loc[a,bb]
        se_sum=float(np.sqrt(var)) if var>0 else np.nan
        t_sum=b_sum/se_sum if se_sum and se_sum==se_sum else np.nan
        p_sum=2*(1-0.5*(1+erf(abs(t_sum)/np.sqrt(2)))) if t_sum==t_sum else np.nan
    else:
        b_sum=se_sum=t_sum=p_sum=np.nan
    out={"label":label,"n":int(r.nobs),"R2":r.rsquared,"adjR2":r.rsquared_adj,
         "F":r.fvalue,"F_p":r.f_pvalue,
         "coef_FSI":b_sum,"se_FSI":se_sum,"t_FSI":t_sum,"p_FSI":p_sum}
    if se_sum==se_sum and se_sum>0:
        out["ci_lo"]=float(b_sum-1.96*se_sum); out["ci_hi"]=float(b_sum+1.96*se_sum)
    else:
        out["ci_lo"]=np.nan; out["ci_hi"]=np.nan
    return r,out

print("\n"+"="*90); print("表3 基准回归 (HAC 稳健标准误, 聚焦 RV_SPGCE 方程)")
base_tbl=[]
fits={}
for L in (1,2,3,4):
    m=build(L)
    r,o=fit(m,f"order{L}")
    fits[f"order{L}"]=(r,m)
    base_tbl.append(o)
    print(f"  order{L}: n={o['n']:3d} R2={o['R2']:.4f} | FSI b={o['coef_FSI']:.6f} se={o['se_FSI']:.6f} "
          f"t={o['t_FSI']:.3f} p={o['p_FSI']:.4f} CI=[{o['ci_lo']:.4f},{o['ci_hi']:.4f}]")
BT=pd.DataFrame(base_tbl); res_out["baseline"]=BT
w("table3_baseline.csv",BT)

# 完整系数表 (order 1..4)
coef_rows=[]
for L in (1,2,3,4):
    r,_=fits[f"order{L}"]
    for k in r.params.index:
        coef_rows.append({"order":L,"var":k,"coef":r.params[k],"se":r.bse[k],"t":r.tvalues[k],
                          "p":r.pvalues[k],"n":int(r.nobs)})
CT=pd.DataFrame(coef_rows); res_out["baseline_coefs"]=CT
w("table3_coefs_full.csv",CT)

# ================= 4. GARCH (替代波动率度量) =================
print("\n"+"="*90); print("表4 GARCH(1,1)-t 条件波动率 (作为 RV 的替代度量)")
sp=np.log(nn("RV_SPGCE")*10000).dropna()
sp=sp[sp.index>="2003M12"]
sp.index=[str(i) for i in sp.index]
am=arch_model(sp.values, vol="GARCH", p=1, q=1, dist="t", rescale=False)
gfit=am.fit(disp="off", show_warning=False)
print(f"  omega={gfit.params['omega']:.6f} alpha={gfit.params['alpha[1]']:.4f} "
      f"beta={gfit.params['beta[1]']:.4f} nu={gfit.params.get('nu',np.nan):.2f} "
      f"logL={gfit.loglikelihood:.2f}")

# GARCH 条件标准差 (log-RV 单位) -> 取对数作为**替代波动率度量**
cond_vol=pd.Series(gfit.conditional_volatility, index=sp.index)
gv_log=np.log(cond_vol)

def _gv_reg(dep, label):
    """同一控制变量设定下回归; 被解释变量分别为 log(RV) 与 log(GARCH条件波动率)"""
    tmp=pd.DataFrame({"dep":dep}, index=dep.index)
    for c in ["FSI","ADS","CPI","DWTI","DDollar","EFFR","logEPU"]:
        tmp[c]=pd.to_numeric(V[c],errors="coerce").reindex(tmp.index)
    tmp["dep_l1"]=tmp["dep"].shift(1)
    mm=tmp.dropna()
    if len(mm)<30:
        print(f"  [GARCH] {label}: 子样本不足 (n={len(mm)})")
        return None
    r=sm.OLS(mm["dep"], sm.add_constant(mm[["dep_l1","ADS","CPI","DWTI","DDollar","EFFR","logEPU","FSI"]])).fit(
        cov_type="HAC",cov_kwds={"maxlags":6})
    out={"label":label,"n":int(r.nobs),"R2":r.rsquared,"adjR2":r.rsquared_adj,
         "coef_FSI":float(r.params["FSI"]),"se_FSI":float(r.bse["FSI"]),
         "t_FSI":float(r.tvalues["FSI"]),"p_FSI":float(r.pvalues["FSI"])}
    print(f"  {label}: n={out['n']} R2={out['R2']:.4f} | FSI b={out['coef_FSI']:.6f} "
          f"se={out['se_FSI']:.6f} t={out['t_FSI']:.3f} p={out['p_FSI']:.6f}")
    return out

r_rv = _gv_reg(sp, "被解释变量 = log(RV_SPGCE)        [同期控制变量]")
r_gv = _gv_reg(gv_log, "被解释变量 = log(GARCH条件波动率) [替代度量]")
res_out["garch"]={"params":{k:float(v) for k,v in gfit.params.items()},
                  "loglik":float(gfit.loglikelihood),
                  "aic":float(gfit.aic),"bic":float(gfit.bic),
                  "n":int(len(sp)),
                  "persistence":float(gfit.params["alpha[1]"]+gfit.params["beta[1]"]),
                  "reg_logRV":r_rv, "reg_garchvol":r_gv}

# ================= 5. Granger 因果 =================
print("\n"+"="*90); print("表5 Granger 因果检验")
gr=[]
pairs=[("FSI","RVSPGCE"),("RVSPGCE","FSI"),
       ("FSI","RV_ECO"),("RV_ECO","FSI"),("FSI","RV_CELS"),("RV_CELS","FSI")]
for cause,eff in pairs:
    sub=V[[cause,eff]].dropna()
    if len(sub)<50: continue
    try:
        t=grangercausalitytests(sub.values, maxlag=4, verbose=False)
        for lag in (1,2,3,4):
            pv=t[lag][0]["ssr_ftest"][1]
            gr.append({"cause":cause,"effect":eff,"lag":lag,"f":float(t[lag][0]["ssr_ftest"][0]),"p":float(pv)})
    except Exception as e:
        gr.append({"cause":cause,"effect":eff,"lag":0,"f":np.nan,"p":np.nan})
GT=pd.DataFrame(gr); res_out["granger"]=GT
w("table5_granger.csv",GT)
print(GT.pivot_table(index=["cause","effect"],columns="lag",values="p").round(4).to_string())

# ================= 6. 稳健性: 子样本 + 替代 FSI 指标 + 控制变量集合 =================
print("\n"+"="*90); print("表6 稳健性检验")
rob=[]
def run(ycol, controls, label, data=None, lags=1):
    """ls_spec 语义: 所有变量取 1..lags 阶滞后; FSI 系数报告各阶之和"""
    src = data if data is not None else V
    order=[ycol]+controls+["FSI"]
    X=pd.DataFrame(index=src.index)
    for v in order:
        for L in range(1, lags+1):
            X[f"{v}_L{L}"]=src[v].shift(L)
    m=pd.concat([src[ycol].rename("y"),X],axis=1).dropna()
    if len(m)<30: return None
    r=sm.OLS(m["y"], sm.add_constant(m.drop(columns="y"))).fit(cov_type="HAC",cov_kwds={"maxlags":6})
    fsi_cols=[c for c in r.params.index if c.startswith("FSI_L")]
    b=float(sum(r.params[c] for c in fsi_cols))
    cov=r.cov_params()
    se=float(np.sqrt(sum(cov.loc[c,c] for c in fsi_cols)
        +2*sum(cov.loc[a,bb] for i,a in enumerate(fsi_cols) for bb in fsi_cols[i+1:])))
    t=b/se if se>0 else np.nan
    p=2*(1-0.5*(1+erf(abs(t)/np.sqrt(2)))) if t==t else np.nan
    return {"label":label,"n":int(r.nobs),"R2":r.rsquared,"adjR2":r.rsquared_adj,
            "coef_FSI":b,"se_FSI":se,"t_FSI":float(t),"p_FSI":float(p)}

BASE_CTRL=["ADS","CPI","DWTI","DDollar","EFFR","logEPU"]
# (a) 不同 y (6 个指数)
for y in ["RVSPGCE","RV_ECO","RV_CELS","RV_AGINA","RV_NEX","RV_DJUSEN"]:
    o=run(y, BASE_CTRL, f"dep={y}")
    if o: rob.append(o); print(f"  {o['label']:34s} n={o['n']:3d} R2={o['R2']:.3f} FSI b={o['coef_FSI']:+.4f} t={o['t_FSI']:+6.2f} p={o['p_FSI']:.4f}")
# (b) 子样本
for lo,hi,lbl in [("2004M01","2014M12","GFC前+危机 (2004-2014)"),("2015M01","2024M12","后危机 (2015-2024)"),
                   ("2004M01","2009M12","危机期 (2004-2009)"),("2010M01","2019M12","欧债后 (2010-2019)"),
                   ("2020M01","2024M12","疫情后 (2020-2024)")]:
    sub=V.loc[lo:hi]
    o=run("RVSPGCE", BASE_CTRL, f"sub={lbl}", data=sub)
    if o: rob.append(o); print(f"  {o['label']:34s} n={o['n']:3d} R2={o['R2']:.3f} FSI b={o['coef_FSI']:+.4f} t={o['t_FSI']:+6.2f} p={o['p_FSI']:.4f}")
# (c) 替代金融压力指标
alt={"ANFCI":nn("ANFCI"),"NFCI":nn("NFCI"),"NFCIRISK":nn("NFCIRISK"),
     "NFCICREDIT":nn("NFCICREDIT"),"NFCILEVERAGE":nn("NFCILEVERAGE"),
     "OVX":np.log(nn("OVX")),"VVIX":V["logVVIX"],"GPR":np.log(nn("GPR"))}
for nm,s in alt.items():
    V2=V.copy(); V2["FSI"]=s
    o=run("RVSPGCE", BASE_CTRL, f"FSI_alt={nm}", data=V2)
    if o: rob.append(o); print(f"  {o['label']:34s} n={o['n']:3d} R2={o['R2']:.3f} FSI b={o['coef_FSI']:+.4f} t={o['t_FSI']:+6.2f} p={o['p_FSI']:.4f}")
# (d) 控制变量集合变化
for ctrl,lbl in [(BASE_CTRL,"full controls"),(["ADS","CPI","DWTI","EFFR"],"minus dollar & EPU"),
                 (["ADS","DWTI","DDollar","EFFR","logEPU"],"minus CPI"),
                 (["ADS","CPI","DWTI","DDollar","EFFR"],"minus EPU"),
                 ([ "DVIX","DWTI","DDollar","EFFR","logEPU"],"use DVIX instead of ADS")]:
    if "DVIX" in ctrl: V2=V.copy(); V2["ADS"]=V["DVIX"]
    else: V2=V
    o=run("RVSPGCE", ctrl, f"ctrl={lbl}", data=V2)
    if o: rob.append(o); print(f"  {o['label']:34s} n={o['n']:3d} R2={o['R2']:.3f} FSI b={o['coef_FSI']:+.4f} t={o['t_FSI']:+6.2f} p={o['p_FSI']:.4f}")

ROB=pd.DataFrame(rob); res_out["robustness"]=ROB
w("table6_robustness.csv",ROB)

# ================= 7. 脉冲响应 (VAR) =================
print("\n"+"="*90); print("表7 VAR 脉冲响应 (FSI -> RV_SPGCE)")
try:
    from statsmodels.tsa.api import VAR
    vars_=["RVSPGCE","FSI","ADS","CPI","DWTI","DDollar","EFFR","logEPU"]
    vd=V[vars_].dropna()
    res=VAR(vd).fit(maxlags=2, ic=None)
    irf=res.irf(12)
    mats = getattr(irf,"irfs",None)
    if mats is None: mats=irf.orthogonal_irfs
    irf_out=[]
    for h in range(1,13):
        r=mats[h]
        # 索引: [var, shock] -> 对 FSI 的冲击使 RVSPGCE 的响应
        irf_out.append({"h":h,"resp_RVSPGCE_to_FSI":float(r[0,1]),
                        "resp_RVSPGCE_to_own":float(r[0,0]),
                        "resp_FSI_to_RVSPGCE":float(r[1,0])})
    IRF=pd.DataFrame(irf_out); res_out["irf"]=IRF
    w("table7_irf.csv",IRF)
    print(IRF.round(5).to_string(index=False))
    res_out["var"]={"lags":2,"n":int(len(vd)),"aic":float(res.aic),"bic":float(res.bic)}
except Exception as e:
    print("  VAR 失败:", e)

# ================= 8. 季度频率稳健性 =================
print("\n"+"="*90); print("表8 季度频率稳健性")
qs=pd.DataFrame(index=V.index)
for c in ["RVSPGCE","RV_ECO","RV_CELS","RV_AGINA","RV_NEX","RV_DJUSEN","FSI","ADS","CPI","DWTI","DDollar","EFFR","logEPU"]:
    qs[c]=V[c]
import re as _re
def _q(s):
    m=_re.match(r"^(\d{4})M(\d{2})$", str(s))
    if not m: return str(s)
    y=int(m.group(1)); mm=int(m.group(2)); return f"{y}Q{(mm-1)//3+1}"
q=qs.groupby([_q for _ in qs.index]).mean()
def _back(s):
    mm=_re.match(r"^(\d{4})Q(\d)$", str(s))
    if not mm: return str(s)
    return f"{mm.group(1)}M{(int(mm.group(2))-1)*3+1:02d}"
q.index=[_back(x) for x in q.index]
o=run("RVSPGCE", BASE_CTRL, "quarterly", data=q)
if o: print(f"  {o['label']:34s} n={o['n']:3d} R2={o['R2']:.3f} FSI b={o['coef_FSI']:+.4f} t={o['t_FSI']:+6.2f} p={o['p_FSI']:.4f}")
res_out["quarterly"]=o

# 保存变量表供 EViews 使用
E=pd.DataFrame(index=V.index)
E["date"]=V.index
for c in V.columns: E[c]=V[c].values
E.to_csv(os.path.join(BASE,"work","eviews_vars.csv"), index=False)

def _ser(o):
    if isinstance(o,(pd.DataFrame,pd.Series)): return o.to_dict()
    if isinstance(o,(np.integer,)): return int(o)
    if isinstance(o,(np.floating,)): return float(o)
    if isinstance(o,(np.bool_,)): return bool(o)
    return str(o)
json.dump(res_out, open(os.path.join(OUT,"results.json"),"w"), default=_ser, ensure_ascii=False, indent=1)
print("\n[完成] 结果写入", OUT)
