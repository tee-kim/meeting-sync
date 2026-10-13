# meeting-sync

Mac 음성 메모 앱으로 녹음한 회의를 녹취록과 회의록으로 바꿔 GitHub 저장소에 자동으로 쌓는 도구예요.

녹음 제목에 `회의록`을 넣거나, 제목을 `회의`·`미팅`으로 시작하거나 끝내면 1시간 회의 기준 약 15분 뒤 아래 두 파일이 커밋돼요.

```
2026/09/2026-09-09_주간-회고/
  transcript.md   녹취 전문 (whisper-cpp, Mac에서 처리)
  minutes.md      회의록: 일시·참석자 표, 요약, 안건별 논의, 결정 사항·액션 아이템 표
```

음성 파일은 업로드하지 않아요. 녹취 텍스트는 회의록 작성을 위해 Claude(Anthropic)로 전송돼요.

## 동작 방식

1. launchd가 음성 메모 폴더를 감시해요 (변경 시 즉시 + 10분마다).
2. 음성 메모 DB(`CloudRecordings.db`)에서 제목을 읽어 처리 대상을 골라요.
3. ffmpeg로 변환한 뒤 whisper-cpp `large-v3-turbo`로 한국어 녹취록을 만들어요.
4. `claude -p`(도구 없이)로 녹취록에서 회의록을 만들어요.
5. 결과 저장소에 커밋하고 push한 뒤 macOS 알림을 띄워요. 실패하면 다음 실행에서 다시 시도해요.

자세한 설계는 [docs/design.md](docs/design.md)에 있어요.

## 요구 사항

- macOS (Apple Silicon 권장), Xcode Command Line Tools (`clang`, `python3`)
- [Homebrew](https://brew.sh): `brew install ffmpeg whisper-cpp gh`
- [Claude Code](https://claude.com/claude-code) CLI 로그인 상태 (`claude -p`가 동작해야 함)
- GitHub CLI 로그인과 git 자격 증명: `gh auth login && gh auth setup-git`

## 설치

```bash
# 1. whisper 모델 (약 1.6GB)
mkdir -p ~/.local/share/whisper
curl -L -C - -o ~/.local/share/whisper/ggml-large-v3-turbo.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin

# 2. 결과 저장소 (private 권장)
mkdir -p ~/Project/meeting-notes && cd ~/Project/meeting-notes
git init -b main && printf '*.m4a\n*.wav\n*.mp3\n*.mp4\n' > .gitignore
git add -A && git commit -m init
gh repo create meeting-notes --private --source . --push

# 3. 이 저장소에서 launcher 빌드
./install.sh --build
```

4. **시스템 설정 → 개인정보 보호 및 보안 → 전체 디스크 접근 권한**에 `~/.local/bin/meeting-sync-launcher`를 추가해요. 음성 메모 폴더가 macOS 보호 영역이라 필요해요. 권한은 이 실행 파일에만 주고, python이나 터미널에는 주지 않아요.
5. `./install.sh`로 launchd에 등록해요.

결과 저장소 위치를 바꾸려면 `meeting_sync.py`의 `NOTES_REPO` 기본값을 고치거나 환경 변수 `MEETING_NOTES_REPO`를 지정하세요.

## 사용 및 점검

| 명령 | 용도 |
|---|---|
| `python3 meeting_sync.py --file a.m4a --title "회의 테스트" --no-push` | 음성 메모 없이 파일 하나로 시험 |
| `python3 meeting_sync.py --reformat ~/Project/meeting-notes/2026/10/*` | 이미 올린 회의록을 저장된 녹취록으로 현재 포맷에 맞춰 다시 만들기 |
| `python3 -m unittest discover -s tests` | 단위 테스트 |
| `tail -f ~/.local/state/meeting-sync/sync.log` | 실행 로그 |

`--list`, `--dry-run`은 음성 메모 DB를 읽기 때문에 전체 디스크 접근 권한이 필요해요. 터미널에서 실행하면 macOS가 터미널 앱의 권한으로 판단하므로, 권한 확인은 launchd를 통해 실행해야 정확해요.

## 주의할 점

- `launcher.c`를 고쳐서 다시 빌드하면 실행 파일 서명이 바뀌어 전체 디스크 접근 권한을 다시 줘야 해요.
- 화자 구분을 하지 않아서 참석자 이름이 비어 있을 수 있어요.
- 음성 메모 앱 내부 DB 구조에 의존해요. 구조가 바뀌면 알림과 함께 오류를 로그에 남겨요.
