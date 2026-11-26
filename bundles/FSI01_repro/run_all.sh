#!/bin/bash
# FSI01 增补分析 —— 一键复现
set -euo pipefail
cd "$(dirname "$0")/.."
echo "[1/4] 重建数据集"; python3 work/build_dataset.py
echo "[2/4] 复算基准结果"; python3 work/analysis.py
echo "[3/4] 复算增补分析"; python3 work/extend_analysis.py
echo "[4/4] 生成图表"; python3 work/make_figures.py
echo "✅ 完成。产物：output_ext/*.csv, figures/扩展图E*.{png,pdf}"
