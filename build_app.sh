#!/usr/bin/env bash
# 把看板打包成可发给别人的 macOS .app
#
# 产物：dist/FARS Dashboard.app（可直接 zip 发给别人）
#
# 设计要点（都是踩过才知道的）：
#  1. **绝对不带 API Key** —— key 存 macOS 钥匙串，不在代码/配置里。
#  2. **不依赖 ~/.openclaw/workspace** —— 别人机器上没这个目录。
#     状态落到 ~/Library/Application Support/FARS Dashboard/。
#  3. **不依赖 ~/Desktop/FARS_Research** —— 打包版 pick() 不回退 Desktop
#     （实测踩过：会读到本机生产论文数据 = 泄漏）。
#  4. index.html 必须打进包 → --add-data（server.py 用 find_asset 找）。
#  5. docx/numpy/pypdf 运行时依赖 → --collect-all。
#  6. **不要手工拼 .app** —— PyInstaller 自己生成的 .app 才有完整的
#     Contents/Frameworks/Python3；手工拼的会报
#     "Failed to load Python shared library"（实测踩过）。
#  7. **不要 codesign --remove-signature** —— 会破坏 bundle 完整性，
#     同样报 "Failed to load Python shared library"（实测踩过）。
#     保留/重签 ad-hoc 即可。
#  8. 打包后启动要 ~12 秒（onefile 语义，每次解压 90MB）→ 弹提示窗。
#
# 用法： ./build_app.sh
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

DIST="$HERE/dist"
APP_NAME="FARS Dashboard"
BUILD="$HERE/.build"
VENV="$BUILD/venv"

if [ ! -x "$VENV/bin/python" ]; then
  echo "=== 建打包环境 ==="
  mkdir -p "$BUILD"
  /usr/bin/python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --disable-pip-version-check --upgrade pip
  "$VENV/bin/pip" install -q --disable-pip-version-check \
    pyinstaller numpy pandas statsmodels scipy python-docx pypdf PyPDF2 pymupdf
fi

echo "=== 清产物 ==="
rm -rf "$BUILD/work" "$BUILD/dist" "$DIST"
mkdir -p "$DIST"

echo "=== PyInstaller 打包 ==="
"$VENV/bin/python" -m PyInstaller \
  --noconfirm --clean \
  --name "fars-dashboard" \
  --distpath "$BUILD/dist" \
  --workpath "$BUILD/work" \
  --specpath "$BUILD" \
  --add-data "$HERE/index.html:." \
  --add-data "$HERE/samples:samples" \
  --add-data "$HERE/assets:assets" \
  --paths "$HERE" \
  --collect-all docx \
  --collect-all numpy \
  --hidden-import pandas \
  --hidden-import pandas._libs.tslibs.base \
  --hidden-import statsmodels \
  --hidden-import statsmodels.formula.api \
  --hidden-import statsmodels.regression.linear_model \
  --hidden-import scipy \
  --hidden-import scipy.stats \
  --hidden-import filemgr \
  --hidden-import audit \
  --hidden-import diaglog \
  --hidden-import pypdf \
  --windowed \
  --osx-bundle-identifier com.farsecon.fars.dashboard \
  "$HERE/server.py"

SRC_APP="$BUILD/dist/fars-dashboard.app"
[ -d "$SRC_APP" ] || { echo "❌ 没生成 .app"; exit 1; }

echo "=== 改名 + 显示名 ==="
APP="$DIST/$APP_NAME.app"
rm -rf "$APP"
cp -R "$SRC_APP" "$APP"
mv "$APP/Contents/MacOS/fars-dashboard" "$APP/Contents/MacOS/$APP_NAME" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable $APP_NAME" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleName $APP_NAME" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName $APP_NAME" "$APP/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$APP/Contents/Info.plist" 2>/dev/null || true
codesign --force --deep --sign - "$APP" 2>/dev/null || true

echo "=== 冒烟测试（等足时间：onefile 启动要 ~12s）==="
TEST_STATE="$BUILD/teststate"
rm -rf "$TEST_STATE"; mkdir -p "$TEST_STATE"
FARS_STATE_DIR="$TEST_STATE" FARS_PORT=8899 \
  "$APP/Contents/MacOS/$APP_NAME" >"$BUILD/smoke.log" 2>&1 &
SMOKE_PID=$!
OK=0
for i in $(seq 1 25); do
  sleep 2
  if curl -s -m 4 "http://127.0.0.1:8899/health" 2>/dev/null | grep -q ok; then
    OK=1; echo "  ✅ 起来用了 $((i*2)) 秒"
    break
  fi
done
if [ "$OK" = "1" ]; then
  curl -s -m 10 "http://127.0.0.1:8899/" > "$BUILD/page.html"
  if grep -q "taskList" "$BUILD/page.html" && grep -q "settingsWrap" "$BUILD/page.html"; then
    echo "  ✅ 首页正常（$(wc -c < "$BUILD/page.html") 字节，含设置页）"
  else
    echo "  ❌ 首页异常"
  fi
  if curl -s -m 8 "http://127.0.0.1:8899/api/settings/get" | grep -q "presets"; then
    echo "  ✅ 设置接口正常"
  else
    echo "  ❌ 设置接口异常"
  fi
  NT=$(curl -s -m 8 "http://127.0.0.1:8899/api/state" | grep -o '"total":[0-9]*' | head -1)
  echo "  ✅ 论文数 $NT"
  if curl -s -m 8 "http://127.0.0.1:8899/api/paper/DEMO01/ESG" | grep -q "DEMO"; then
    echo "  ✅ 示例论文已就位"
  else
    echo "  ⚠️ 示例论文未就位（samples seeding 有问题）"
  fi
else
  echo "  ⚠️ 健康检查未通过（看 $BUILD/smoke.log）"
  tail -20 "$BUILD/smoke.log" || true
fi
kill $SMOKE_PID 2>/dev/null || true
sleep 1
pkill -f "$APP_NAME" 2>/dev/null || true

echo
echo "=== 产物 ==="
du -sh "$APP"
echo
echo "=== 检查包里有没有泄密的 key ==="
if grep -rl "sk-[A-Za-z0-9]\{20,\}" "$APP/Contents" 2>/dev/null | head -3; then
  echo "  ⚠️ 发现疑似 key！请检查"
else
  echo "  ✅ 未发现 API Key"
fi
echo
echo "=== 净化：去掉构建期泄漏的本机绝对路径 ==="
# ⚠️ 2026-11-23 23:0x 修（用户问「内测文件里有没有我的敏感信息」）：
#   PyInstaller 打进来的第三方包 RECORD 文件里嵌着**构建机的绝对路径**：
#     ../../../../../../../../Library/Caches/com.apple.python/Users/wangyunjie/
#       .openclaw/workspace/tools/fars-dashboard/.build/venv/...
#   → 拿到包的人能看到你的**用户名 + 本机目录结构**。
#   → 打包后统一把文本文件里的本机绝对路径替换掉。
/usr/bin/python3 - "$APP" "$HOME" <<'PYEOF'
import sys, os
app, home = sys.argv[1], sys.argv[2]
hits, fixed = 0, 0
targets = [home, os.path.expanduser("~").replace("/Users/", "/Users/")]
for root, dirs, files in os.walk(app):
    for fn in files:
        p = os.path.join(root, fn)
        try:
            if os.path.getsize(p) > 4 * 1024 * 1024:
                continue
            with open(p, "rb") as f:
                raw = f.read()
            if b"wangyunjie" not in raw and home.encode() not in raw:
                continue
            hits += 1
            try:
                txt = raw.decode("utf-8")
            except UnicodeDecodeError:
                continue
            new = txt.replace(home, "<HOME>")
            new = new.replace("/Users/wangyunjie", "/Users/<USER>")
            new = new.replace("wangyunjie", "<USER>")
            if new != txt:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(new)
                fixed += 1
        except Exception:
            pass
print("  扫到含本机路径的文件: %d，净化: %d" % (hits, fixed))
PYEOF

echo
echo "=== 生成 zip（方便发送）==="
cd "$DIST"
rm -f "$APP_NAME.zip"
# ⚠️ 2026-11-23 23:0x 修：原来用 `ditto -c -k` 打包 → 中文文件名
#   （samples/_进度追踪.json）在 zip 里是**乱码**（MacRoman 编码，UTF-8 标志位没置）。
#   → 改用 Python zipfile 打包：非 ASCII 文件名自动置 UTF-8 标志位，跨平台解压都正常。
/usr/bin/python3 - "$APP_NAME.app" "$APP_NAME.zip" "$HERE/docs/分发说明.md" <<'PYEOF'
import sys, zipfile, os, shutil
app, out, readme = sys.argv[1], sys.argv[2], sys.argv[3]
tmp = out + ".tmp"
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(app):
        for fn in files:
            full = os.path.join(root, fn)
            rel = os.path.join(os.path.basename(app), os.path.relpath(full, app))
            z.write(full, rel)
    if os.path.isfile(readme):
        z.write(readme, "README-分发.md")
shutil.move(tmp, out)
PYEOF
ls -lh "$APP_NAME.zip"
echo
echo "=== 复制到桌面（方便取用）==="
# ⚠️ 只复制 .app 和 .zip，**不碰** ~/Desktop/FARS_Research（论文数据目录）。
#    实测踩过：Desktop 上有生产论文数据，清理/覆盖时误伤过。
DESK="${HOME}/Desktop"
DELIV="$DESK/FARS Dashboard 内测版"
if [ -d "$DESK" ]; then
  # ⚠️ 2026-11-25 修：原来 rm -rf "$DELIV" 会**连带删掉**目录里其他物料
  #    （实测踩过：路演演示视频.mp4 和产品说明计划书.docx 被清掉了）。
  #    → 改成只删本次要覆盖的两个（.app / .zip），其余文件一律保留。
  rm -rf "$DELIV/$APP_NAME.app" "$DELIV/$APP_NAME.zip"
  mkdir -p "$DELIV"
  cp -R "$APP" "$DELIV/"
  cp "$DIST/$APP_NAME.zip" "$DELIV/"
  # 分发说明也放一份，用户不用解包就能看
  cp "$HERE/docs/分发说明.md" "$DELIV/README-分发.md" 2>/dev/null || true
  echo "  ✅ 已复制到：$DELIV"
  ls -lh "$DELIV"
else
  echo "  ⚠️ 桌面目录不存在，跳过"
fi
echo
echo "打包完成。分发说明已放进 zip 根目录（README-分发.md）"
