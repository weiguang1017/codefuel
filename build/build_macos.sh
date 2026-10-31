#!/bin/bash
# 在 macOS 上把应用打包成 dist/CodeFuel.app
set -euo pipefail
cd "$(dirname "$0")/.."

export UV_DEFAULT_INDEX="${UV_DEFAULT_INDEX:-https://mirrors.aliyun.com/pypi/simple/}"
export PYINSTALLER_CONFIG_DIR="$PWD/build/.pyinstaller"
uv sync --locked --dev
uv run --locked python build/make_icon.py
iconutil -c icns -o build/icon.icns build/icon.iconset
uv run --locked pyinstaller --noconfirm --distpath dist --workpath build/pyinstaller-codefuel build/CodeFuel.spec
ARCH="$(lipo -archs dist/CodeFuel.app/Contents/MacOS/CodeFuel | xargs | tr ' ' '-')"
bash build/make_dmg.sh dist/CodeFuel.app "dist/CodeFuel-macOS-${ARCH}.dmg"

echo
echo "已完成：$(pwd)/dist/CodeFuel.app"
echo "安装镜像：$(pwd)/dist/CodeFuel-macOS-${ARCH}.dmg"
du -sh dist/CodeFuel.app "dist/CodeFuel-macOS-${ARCH}.dmg"
echo "仅分发 CodeFuel.app；旧 DailyQuota 产物不会自动删除。"
echo "首次打开如被拦截：右键 → 打开，或在「系统设置 → 隐私与安全性」中允许。"
