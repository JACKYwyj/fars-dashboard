#!/usr/bin/env python3
"""构建金融压力 x 可再生能源股票指数波动率 实证研究的主数据集。
输入: data/RV.xls (EViews 工作簿), data/GSCID.xlsx (日度), data/GSCIM.xlsx (月度)
输出: work/master_monthly.csv
"""
import os, math, statistics, csv, datetime
from collections import defaultdict
import openpyxl, xlrd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(BASE, "data")
WORK = os.path.join(BASE, "work")

CODES = ("GSIAEXR,GSBREXR,GSCCEXR,GSKCEXR,GSICEXR,GSCNEXR,GSCTEXR,GSCLEXR,GSGOEXR,"
         "GSGCEXR,GSHOEXR,GSLHEXR,GSLCEXR,GSLVEXR,GSNGEXR,GSIKEXR,GSSIEXR,GSSOEXR,"
         "GSSBEXR,GSIZEXR,GSWHEXR,GSFCEXR,GSKWEXR").split(",")

# 标普 GSCI 官方板块归属（S&P GSCI commodity index sector classification）
SECTORS = {
 "ENERGY":           ["GSBREXR","GSCLEXR","GSGOEXR","GSHOEXR","GSNGEXR"],
 "AGRICULTURE":      ["GSCCEXR","GSKCEXR","GSCNEXR","GSCTEXR","GSSOEXR","GSSBEXR","GSWHEXR","GSKWEXR"],
 "LIVESTOCK":        ["GSLHEXR","GSLCEXR","GSLVEXR","GSFCEXR"],
 "INDUSTRIAL_METALS":["GSIAEXR","GSICEXR","GSIKEXR","GSIZEXR"],
 "PRECIOUS_METALS":  ["GSGCEXR","GSSIEXR"],
}

def excel_serial_to_ymd(s, datemode):
    d = xlrd.xldate_as_tuple(s, datemode)
    return f"{d[0]:04d}M{d[1]:02d}"

def load_gsci_daily():
    wb = openpyxl.load_workbook(os.path.join(DATA, "GSCID.xlsx"), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True)); wb.close()
    hdr = [str(x) for x in rows[0][1:]]
    out = []
    for r in rows[1:]:
        if hasattr(r[0], "year"):
            out.append((r[0].date(), list(r[1:])))
    return hdr, out

def load_gsci_monthly():
    """GSCIM.xlsx 结构: R1-3 为元信息(Start/End/Frequency), R4 为表头,
    R5 起为月度块(每月首行, 日=7); 文件尾部误追加了一段日度块, 需按月度边界截断。"""
    wb = openpyxl.load_workbook(os.path.join(DATA, "GSCIM.xlsx"), read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = list(ws.iter_rows(values_only=True)); wb.close()
    hdr = [str(x) for x in rows[3][1:]]
    out = []
    for r in rows[4:]:
        d = r[0]
        if not hasattr(d, "year"):
            continue
        out.append((d.date(), list(r[1:])))
    # 截断到月度块: 日度追加块从某行开始(非 day==7 或日期回退)
    end = len(out)
    for i in range(1, len(out)):
        if out[i][0].day != 7:
            end = i
            break
    return hdr, out[:end]

def monthly_rv(daily, ci):
    """月度已实现波动率: 月内日对数收益的标准差 (Parkinson 风格, 样本标准差)"""
    bym = defaultdict(list)
    for d, row in daily:
        v = row[ci]
        if isinstance(v, (int, float)) and v > 0:
            bym[(d.year, d.month)].append(v)
    out = {}
    for k, vals in bym.items():
        if len(vals) < 3:
            continue
        lr = [math.log(vals[i]/vals[i-1]) for i in range(1, len(vals))]
        out[f"{k[0]:04d}M{k[1]:02d}"] = statistics.stdev(lr)
    return out

def monthly_ret(daily_monthly, ci):
    """月度超额收益 (对数)"""
    out = {}
    prev = None
    for d, row in daily_monthly:
        v = row[ci]
        if not isinstance(v, (int, float)) or v <= 0:
            continue
        key = f"{d.year:04d}M{d.month:02d}"
        if prev is not None:
            py, pm = prev[0]
            ok = (d.year, d.month) == ((py + 1, 1) if pm == 12 else (py, pm + 1))
            if ok:
                out[key] = math.log(v/prev[1])
        prev = ((d.year, d.month), v)
    return out

def aggregate(series_map, keys, method):
    """把成分序列聚合成板块序列, 返回 {ym: value}"""
    d = defaultdict(list)
    for c in keys:
        for k, v in series_map[c].items():
            d[k].append(v)
    out = {}
    for k, vs in d.items():
        if len(vs) != len(keys):
            continue
        if method == "rss":
            out[k] = math.sqrt(sum(x*x for x in vs))
        elif method == "mean":
            out[k] = sum(vs)/len(vs)
        elif method == "sum":
            out[k] = sum(vs)
        elif method == "meanret":
            out[k] = sum(vs)/len(vs)
    return out

def main():
    hdr_d, daily = load_gsci_daily()
    hdr_m, monthly = load_gsci_monthly()
    print(f"[GSCI daily ] {len(daily)} obs  {daily[0][0]} .. {daily[-1][0]}  series={len(hdr_d)}")
    print(f"[GSCI month ] {len(monthly)} obs  {monthly[0][0]} .. {monthly[-1][0]}")

    rv = {c: monthly_rv(daily, CODES.index(c)) for c in CODES}
    rt = {c: monthly_ret(monthly, CODES.index(c)) for c in CODES}

    # ---- RV.xls 主数据 ----
    b = xlrd.open_workbook(os.path.join(DATA, "RV.xls"))
    s = b.sheet_by_name("Data")
    th = s.row_values(0)
    base = {}
    for r in range(1, s.nrows):
        ym = excel_serial_to_ymd(s.cell_value(r, 0), b.datemode)
        base[ym] = {th[c]: s.cell_value(r, c) for c in range(1, s.ncols)}

    # ---- 与作者 'Data Ambiguity' 表比对, 选定聚合方法 ----
    sa = b.sheet_by_name("Data Ambiguity")
    ah = sa.row_values(0)
    auth = {}
    for r in range(1, sa.nrows):
        auth[sa.cell_value(r, 0)] = {ah[c]: sa.cell_value(r, c) for c in range(1, sa.ncols)}

    def corr(x, y):
        n = len(x); mx = sum(x)/n; my = sum(y)/n
        cov = sum((a-mx)*(c-my) for a, c in zip(x, y))
        sx = math.sqrt(sum((a-mx)**2 for a in x)); sy = math.sqrt(sum((c-my)**2 for c in y))
        return cov/(sx*sy) if sx*sy > 0 else 0.0

    print("\n[聚合方法识别] 作者 'Data Ambiguity' 表 vs GSCI 日度重构")
    best = None
    for method in ("rss", "mean", "sum"):
        row = []
        for g, ms in SECTORS.items():
            gr = aggregate(rv, ms, method)
            com = sorted(set(gr) & set(auth))
            if len(com) < 10:
                row.append((g, 0, 0.0)); continue
            x = [gr[k] for k in com]; y = [auth[k][g] for k in com]
            c = corr(x, y)
            mx = sum(x)/len(x); my = sum(y)/len(y)
            row.append((g, len(com), c))
            if best is None or c > best[0]:
                best = (c, method, g)
        print(f"  method={method:5s} " + "  ".join(f"{g[:6]}:c={c:.3f}" for g, n, c in row))
    print(f"  -> 最佳: method={best[1]} 组={best[2]} corr={best[0]:.4f}")

    METHOD = best[1]
    # 用作者表的窗口(2006M03-2022M12)做最终校准
    win = [k for k in auth if k in set(auth)]
    print(f"  校准窗口: {win[0]} .. {win[-1]}  ({len(win)} 个月)")

    # ---- 构建主表 ----
    all_sector_rv = {}
    for g, ms in SECTORS.items():
        all_sector_rv[g] = aggregate(rv, ms, METHOD)
    all_sector_rt = {}
    for g, ms in SECTORS.items():
        all_sector_rt[g] = aggregate(rt, ms, "meanret")
    # 成分等权月对数收益
    eq_ret = {}
    for g, ms in SECTORS.items():
        d = defaultdict(list)
        for c in ms:
            for k, v in rt[c].items(): d[k].append(v)
        eq_ret[g] = {k: sum(vs)/len(vs) for k, vs in d.items() if len(vs) == len(ms)}

    hdr_out = ["date"]
    hdr_out += [k for k in th[1:]]
    hdr_out += [f"GSCI_{g}_RV" for g in SECTORS] + ["GSCI_COMMODITY_RV", "GSCI_ALL23_RV"]
    hdr_out += [f"GSCI_{g}_RET" for g in SECTORS] + ["GSCI_COMMODITY_RET"]
    hdr_out += [f"RV_{c}" for c in CODES] + [f"RET_{c}" for c in CODES]

    # 组合商品 RV: 6 个板块 RV 的 RSS
    d = defaultdict(list)
    for g in SECTORS:
        for k, v in all_sector_rv[g].items(): d[k].append(v)
    comm_rv = {k: math.sqrt(sum(x*x for x in vs)) for k, vs in d.items() if len(vs) == len(SECTORS)}
    d2 = defaultdict(list)
    for c in CODES:
        for k, v in rv[c].items(): d2[k].append(v)
    all23_rv = {k: math.sqrt(sum(x*x for x in vs)) for k, vs in d2.items() if len(vs) == len(CODES)}

    out = os.path.join(WORK, "master_monthly.csv")
    n = 0
    with open(out, "w", newline="") as f:
        w = csv.writer(f); w.writerow(hdr_out)
        for ym in sorted(base):
            row = [ym]
            for k in th[1:]:
                v = base[ym][k]
                row.append("" if v in ("", None) else v)
            for g in SECTORS:
                row.append(all_sector_rv[g].get(ym, ""))
            row.append(comm_rv.get(ym, ""))
            row.append(all23_rv.get(ym, ""))
            for g in SECTORS:
                row.append(eq_ret[g].get(ym, ""))
            # 商品组合对数收益 (6 板块等权)
            srows = [all_sector_rt[g].get(ym) for g in SECTORS]
            row.append((sum(srows)/len(srows)) if all(x is not None for x in srows) else "")
            for c in CODES:
                row.append(rv[c].get(ym, ""))
            for c in CODES:
                row.append(rt[c].get(ym, ""))
            w.writerow(row); n += 1
    print(f"\n[输出] {out}  rows={n} cols={len(hdr_out)}")

    # 摘要统计
    print("\n[覆盖检查]")
    for k in ("RV_SPGCE","FSI","RV_ECO","RV_CELS","RV_AGINA","RV_NEX","RV_DJUSEN","ADS","VIX","WTI","EFFR","EPU","CPI","M2","IP","Dollar"):
        if k in th:
            vals = [base[ym][k] for ym in base if base[ym][k] not in ("", None)]
            print(f"   {k:12s} n={len(vals):4d}  {min(vals):>12.4f} .. {max(vals):>12.4f}")
    for g in SECTORS:
        vals = [v for v in all_sector_rv[g].values()]
        print(f"   GSCI_{g:18s} n={len(vals):4d}  {min(vals):>10.5f} .. {max(vals):>10.5f}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
