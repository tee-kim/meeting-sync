# meeting-sync 설계

Mac 음성 메모 앱의 회의 녹음을 녹취록과 회의록으로 바꿔 private GitHub 저장소에 자동으로 쌓는다.

## 결정 사항

- 대상: 음성 메모 중 제목이 `회의`·`미팅`으로 시작하거나 끝나는 녹음, 또는 `회의록`이 들어간 녹음
- 업로드 대상: `transcript.md`(녹취 전문)와 `minutes.md`(회의록 요약). 음성 파일은 커밋하지 않음
- 녹취: whisper-cpp `large-v3-turbo`, 한국어, Mac 안에서 처리
- 요약: `claude -p`, 도구 없이 텍스트만 입력. 녹취 텍스트는 Anthropic으로 전송됨
- 수집 방식: launchd가 음성 메모 폴더를 직접 감시하고, 제목은 `CloudRecordings.db`에서 읽음

## 구성

```
~/Project/meeting-sync/          도구 저장소
  meeting_sync.py                본체
  launcher.c                     전체 디스크 접근 권한을 받을 작은 실행 파일
  local.meeting-sync.plist       launchd 설정 템플릿
  install.sh                     빌드, 설치, launchd 등록
~/Project/meeting-notes/         결과 저장소 (private GitHub 저장소 권장)
  YYYY/MM/YYYY-MM-DD_<제목>/transcript.md, minutes.md
~/.local/state/meeting-sync/     processed.json(처리 기록), sync.log
```

## 흐름

1. launchd: Recordings 폴더 변경을 감지(WatchPaths)하고, 보조로 10분마다(StartInterval 600) launcher를 실행
2. launcher가 `meeting_sync.py`를 자식 프로세스로 실행. 전체 디스크 접근 권한은 launcher에만 부여되고 자식이 상속받음. `/bin/bash`나 python 전체에 권한을 주지 않기 위한 구조
3. DB 조회: 열 이름을 런타임에 확인. 제목 규칙에 맞고, 고유 ID가 처리 기록에 없는 녹음을 고름
4. 안정성 확인: 파일 크기가 30초 사이에 변하면 녹음 중으로 보고 이번 실행은 건너뜀
5. ffmpeg로 16kHz mono wav 임시 파일 생성 → whisper-cli로 녹취(`-l ko`) → 임시 wav 삭제
6. `claude -p --tools ""`로 녹취록에서 회의록 생성(참석자, 논의 내용, 결정 사항, 액션 아이템)
7. 결과 저장소에 파일을 쓰고(끝에 생성 정보 블록 포함) commit, push. push 전에 `pull --rebase`로 GitHub 웹 수정 사항을 먼저 받음
8. 처리 기록에 남기고 macOS 알림. 실패하면 기록하지 않아 다음 실행에서 재시도하고 실패 알림을 띄움

## 안전장치

- 실행 중복 방지: lock 파일
- `.gitignore`에서 `*.m4a`, `*.wav`, `*.mp3` 차단
- 같은 날짜·제목이 이미 있으면 디렉터리 이름 뒤에 `-2` 등을 붙임
- DB 스키마가 예상과 다르면 명확한 오류를 로그에 남기고 알림
- 전체 디스크 접근 권한이 없으면 안내 알림

## 테스트

- 단위: 제목 필터, 디렉터리 이름 생성, DB 읽기, 처리 기록, 생성 정보, push 충돌 (`python3 -m unittest discover -s tests`)
- 통합: 실제 회의 녹음 파일로 `--file` 모드 전체 실행 (임시 저장소, push 제외)
- 실제: 음성 메모로 짧은 "회의 테스트" 녹음 후 자동 업로드 확인
