#!/bin/bash
# launcher 빌드 → launchd plist 설치 → 등록
# 사용법: ./install.sh          (빌드 + 등록)
#         ./install.sh --build  (빌드만, 등록하지 않음)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
BIN="$HOME/.local/bin/meeting-sync-launcher"
LABEL="local.meeting-sync"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/.local/bin" "$HOME/.local/state/meeting-sync" "$HOME/Library/LaunchAgents"

# 다시 빌드하면 바이너리 서명이 바뀌어 전체 디스크 접근 권한이 풀리므로, 바뀌었을 때만 빌드한다
if [[ ! -x "$BIN" || "$HERE/launcher.c" -nt "$BIN" ]]; then
    clang -O2 -DSCRIPT_PATH="\"$HERE/meeting_sync.py\"" -o "$BIN" "$HERE/launcher.c"
    echo "launcher를 빌드했습니다: $BIN"
    echo "→ 시스템 설정 > 개인정보 보호 및 보안 > 전체 디스크 접근 권한에 이 파일을 추가하세요."
fi

[[ "${1:-}" == "--build" ]] && exit 0

sed -e "s#__HOME__#$HOME#g" -e "s#__BIN__#$BIN#g" "$HERE/$LABEL.plist" > "$PLIST"
plutil -lint "$PLIST" >/dev/null
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "launchd 등록 완료: $LABEL (로그: ~/.local/state/meeting-sync/sync.log)"
