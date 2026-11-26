# FSI01 增补分析 —— 可复现说明

## 一句话复现

```bash
python3 scripts/build_dataset.py      # 从原始数据重建 master_monthly.csv
python3 scripts/analysis.py            # 复算基准 VAR(1) / Granger / IRF
python3 scripts/extend_analysis.py     # 复算全部增补分析（表 E1–E7）
python3 scripts/make_figures.py        # 生成扩展图 E1–E7（PNG 300dpi + PDF 矢量）
```

## 目录对照

| 目录 | 内容 | 生成者 |
|---|---|---|
| `scripts/` | 全部复现脚本 | — |
| `data/master_monthly.csv` | 主月度数据集（488KB） | `build_dataset.py` |
| `outputs/table_ext_*.csv` | 表 E1–E7 的权威数值 | `extend_analysis.py` |
| `outputs/ext_results.json` | 全部复算结果汇总 | `extend_analysis.py` |
| `figures/扩展图E*.png` | 出版级位图（300dpi） | `make_figures.py` |
| `figures/扩展图E*.pdf` | 出版级矢量图 | `make_figures.py` |
| `figures/图注清单.csv` | 图注 + 数据来源 | `make_figures.py` |
| `eviews/` | EViews 原始输出（48 个 CSV） | EViews 导出 |
| `prg/` | EViews 程序（`rv.prg` 等） | EViews |
| `docs/` | 文档 | — |

## 复现性保证（实测）

- `extend_analysis.py` 连续两次运行 → **9 个 CSV 字节级完全一致**
- `make_figures.py` 连续两次运行 → **7 张 PNG 字节级完全一致**
- 唯一变动项：`ext_results.json` 的 `generated_at` 时间戳（唯一非确定性字段）
- 脚本内**无随机数**（无 seed / random / bootstrap / sample）→ 结果确定

## 字体依赖

图件中文字体：`Heiti TC`（macOS 自带）。
若在 Linux 上复现，请先安装任一 CJK 字体并改 `make_figures.py` 的
`font.sans-serif` 列表，否则会出现缺字方块（tofu）。

## 数值溯源

| 论文位置 | 数值来源 |
|---|---|
| 表 E1 基准复算 | `outputs/table_ext_baseline_recheck.csv` |
| 表 E2 样本外 | `outputs/table_ext_oos.csv` + `table_ext_oos_detail.csv` |
| 表 E3 替代指标 | `outputs/table_ext_alt_fsi.csv` |
| 表 E4 子样本 | `outputs/table_ext_subsample.csv` |
| 表 E5 特征根 | `outputs/table_ext_eigen.csv` |
| 表 E6 高低分组 | `outputs/table_ext_regime.csv` |
| 表 E7 相关矩阵 | `outputs/table_ext_alt_corr.csv` |
| 滚动系数 | `outputs/table_ext_rolling.csv` |

对账工具：FARS 的 `verify_draft_numbers.py`
（正文统计量 vs EViews/复算 CSV 逐条核验；当前孤儿数字 3 个，均为合法常量）。

## 关键口径（复算时必须一致）

- RV 采用**基点化对数** `ln(RV × 10000)`
- DWTI / DDOLLAR 采用**对数差分乘 100**
- LOGEPU 取自然对数
- 全部解释变量取**一期滞后**
- 标准误采用 **HAC 稳健估计**，Newey-West 滞后阶数 **6**

> ⚠️ 漏掉基点化或派生构造，回归系数会差 3 个数量级（0.000235 vs 0.141167）。
