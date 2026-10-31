#!/bin/bash
# 把 CodeFuel.app 打成带「拖入应用程序」布局的 macOS 安装镜像。
# 用法: build/make_dmg.sh [app路径] [输出dmg路径] [--skip-layout]
#   --skip-layout  跳过访达图标排版（CI 等无图形会话环境用）
set -euo pipefail
cd "$(dirname "$0")/.."

SKIP_LAYOUT=0
POSITIONAL=()
for arg in "$@"; do
  case "$arg" in
    --skip-layout) SKIP_LAYOUT=1 ;;
    *) POSITIONAL+=("$arg") ;;
  esac
done

APP="${POSITIONAL[0]:-dist/CodeFuel.app}"
OUT="${POSITIONAL[1]:-dist/CodeFuel-macOS-$(uname -m).dmg}"
VOLUME="CodeFuel"   # 固定卷名：AppleScript 用 disk "CodeFuel" 定位，避免空格/中文失败

[[ -d "$APP" ]] || { echo "找不到应用：$APP" >&2; exit 1; }
[[ -f "$OUT" ]] && { echo "输出已存在，请先改名：$OUT" >&2; exit 1; }

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/codefuel-dmg.XXXXXX")"
RW="$(mktemp -u "${TMPDIR:-/tmp}/codefuel-rw.XXXXXX").dmg"
MNT="/Volumes/$VOLUME"

cleanup() {
  hdiutil detach "$MNT" -quiet >/dev/null 2>&1 || true
  rm -rf "$STAGE" "$RW"
}
trap cleanup EXIT

ditto "$APP" "$STAGE/CodeFuel.app"
ln -s /Applications "$STAGE/Applications"

# 额外留 32 MiB 给 HFS+ 和 Finder 元数据。
SIZE_KB=$(( $(du -sk "$STAGE" | cut -f1) + 32768 ))
hdiutil create -quiet -srcfolder "$STAGE" -volname "$VOLUME" -fs HFS+ \
  -format UDRW -size "${SIZE_KB}k" "$RW"
hdiutil attach "$RW" -mountpoint "$MNT" -noautoopen -quiet

# 只是视觉排版；显式跳过或没有图形会话时不做，DMG 仍完全可用。
if [[ "$SKIP_LAYOUT" == "1" ]]; then
  echo "已跳过图标排版（--skip-layout）。"
elif osascript <<'APPLESCRIPT' >/dev/null 2>&1
tell application "Finder"
  tell disk "CodeFuel"
    open
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set bounds of container window to {200, 160, 860, 580}
    set opts to icon view options of container window
    set arrangement of opts to not arranged
    set icon size of opts to 128
    set position of item "CodeFuel.app" of container window to {160, 190}
    set position of item "Applications" of container window to {500, 190}
    close
    open
    update without registering applications
    delay 1
    close
  end tell
end tell
APPLESCRIPT
then
  echo "已应用拖入式安装布局。"
else
  echo "未能自动排列图标（无图形会话或未授权访达自动化），DMG 仍可直接使用。"
fi

sync
hdiutil detach "$MNT" -quiet
hdiutil convert "$RW" -format UDZO -imagekey zlib-level=9 -o "$OUT" -quiet

echo
echo "已完成：$OUT"
hdiutil verify "$OUT" | tail -1
du -sh "$OUT"
