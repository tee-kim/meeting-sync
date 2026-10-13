#!/usr/bin/env python3
"""음성 메모의 회의 녹음을 녹취록·회의록으로 만들어 meeting-notes 저장소에 올린다.

설계: docs/design.md
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

HOME = Path.home()
RECORDINGS_DIR = HOME / "Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings"
DB_NAME = "CloudRecordings.db"
NOTES_REPO = Path(os.environ.get("MEETING_NOTES_REPO", HOME / "Project/meeting-notes"))
STATE_DIR = HOME / ".local/state/meeting-sync"
STATE_FILE = STATE_DIR / "processed.json"
WHISPER_MODEL = HOME / ".local/share/whisper/ggml-large-v3-turbo.bin"
TITLE_PREFIXES = ("회의", "미팅")
TITLE_KEYWORD = "회의록"  # 제목 어디에 있어도 처리 대상
# 제목이 회의·미팅으로 끝나도 처리 대상 (예: "… Weekly 미팅")
CORE_DATA_OFFSET = 978307200  # 2001-01-01 UTC의 Unix 시각
STABLE_WAIT = 30

MINUTES_PROMPT = """다음 stdin은 회의 녹음을 음성 인식으로 받아쓴 녹취록이다. 회의 제목: {title}, 날짜: {date}.
녹취록만 근거로 한국어 회의록을 Markdown으로 작성하라. 아래 형식을 글자 그대로 지킨다.

첫 줄: "참석자: " 뒤에 녹취에서 확인되는 이름을 쉼표로 나열. 하나도 없으면 "참석자: 확인 불가"

## 1. 요약
- 핵심 3개 이하. 각 1~2문장

## 2. 안건별 논의
### 2.1 (안건 이름)
- 현황: (공유된 사실, 수치)
- 논의: (오간 의견, 쟁점)
(안건마다 ### 2.2, ### 2.3 … 으로 이어서 쓴다. 다룬 순서를 따른다)

## 3. 결정 사항
| # | 결정 내용 | 안건 |
|---|---|---|
(안건 칸에는 2.1 같은 번호. 결정이 없으면 표 대신 "- 없음")

## 4. 액션 아이템
| # | 담당 | 할 일 | 기한 |
|---|---|---|---|
(담당은 이름이나 팀. 모르면 "미정". 기한은 회의 날짜 기준으로 YYYY-MM-DD로 환산하고, 환산이 애매하면 "다음 주 중"처럼 말한 표현을 그대로 기한 칸에 쓴다. 기한 표현을 할 일 칸에 넣지 않는다. 언급이 없으면 "미정". 액션이 없으면 표 대신 "- 없음")

## 5. 확인 필요
- 음성 인식 오류로 의미가 불분명한 부분이나 고유명사 (없으면 "- 없음")

규칙:
- 모든 문장은 개조식으로 '~함', '~임', '~예정'처럼 끝낸다.
- 표 칸 안에서는 줄바꿈과 | 문자를 쓰지 않는다.
- 녹취록에 없는 내용은 지어내지 않는다. 불확실한 고유명사 뒤에는 (?)를 붙인다.
- 머리말, 맺음말, 코드 블록 없이 첫 줄부터 위 형식만 출력한다."""

log = logging.getLogger("meeting-sync")

_PREFIX_RE = re.compile(r"^(?:회의|미팅)(?:[\s_\-:]+|$)")
_KEYWORD_RE = re.compile(r"(?:^|[\s_\-:])회의록(?=[\s_\-:]|$)")
_SUFFIX_RE = re.compile(r"[\s_\-:]+(?:회의|미팅)$")


def nfc(s):
    return unicodedata.normalize("NFC", s or "")


def is_meeting_title(title):
    t = nfc(title).strip()
    return t.startswith(TITLE_PREFIXES) or TITLE_KEYWORD in t or bool(_SUFFIX_RE.search(t))


def slugify(title):
    t = _KEYWORD_RE.sub(" ", nfc(title).strip()).strip()
    t = _PREFIX_RE.sub("", t)
    t = _SUFFIX_RE.sub("", t)
    t = re.sub(r'[\\/:*?"<>|#\[\]]', " ", t)
    t = re.sub(r"\s+", "-", t.strip())
    t = re.sub(r"-{2,}", "-", t).strip("-._")
    return t[:60] or "회의"


def target_dir(root, when, title):
    parent = root / f"{when:%Y}" / f"{when:%m}"
    base = f"{when:%Y-%m-%d}_{slugify(title)}"
    d, n = parent / base, 2
    while d.exists():
        d, n = parent / f"{base}-{n}", n + 1
    return d


@dataclass
class Recording:
    uid: str
    title: str
    path: Path
    date: dt.datetime
    duration: float


class SchemaError(Exception):
    pass


def _snapshot_db(db_path, tmp):
    # 음성 메모 앱이 쓰는 중일 수 있으므로 WAL 파일까지 복사본으로 읽는다
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db_path) + suffix)
        if src.exists():
            shutil.copy2(src, Path(tmp) / (db_path.name + suffix))
    return Path(tmp) / db_path.name


def list_recordings(db_path, recordings_dir):
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(_snapshot_db(db_path, tmp))
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(ZCLOUDRECORDING)")}
            if {"ZPATH", "ZDATE"} - cols or not cols & {"ZCUSTOMLABEL", "ZENCRYPTEDTITLE"}:
                raise SchemaError(f"ZCLOUDRECORDING 열 구성이 예상과 다릅니다: {sorted(cols)}")
            want = [c for c in ("ZUNIQUEID", "ZCUSTOMLABEL", "ZENCRYPTEDTITLE", "ZPATH", "ZDATE", "ZDURATION") if c in cols]
            rows = con.execute(f"SELECT {', '.join(want)} FROM ZCLOUDRECORDING").fetchall()
        finally:
            con.close()
    recs = []
    for row in rows:
        r = dict(zip(want, row))
        if not r["ZPATH"]:
            continue
        path = Path(r["ZPATH"])
        recs.append(Recording(
            uid=str(r.get("ZUNIQUEID") or r["ZPATH"]),
            title=nfc(r.get("ZCUSTOMLABEL") or r.get("ZENCRYPTEDTITLE")),
            path=path if path.is_absolute() else recordings_dir / path,
            date=dt.datetime.fromtimestamp((r["ZDATE"] or 0) + CORE_DATA_OFFSET),
            duration=float(r.get("ZDURATION") or 0),
        ))
    return recs


def load_state(path):
    return set(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else set()


def save_state(path, done):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(done), ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def pending(recs, done):
    return [r for r in recs if is_meeting_title(r.title) and r.uid not in done]


def is_stable(path, wait=STABLE_WAIT):
    before = path.stat().st_size
    time.sleep(wait)
    after = path.stat().st_size
    return before == after and after > 0


def run(cmd, input=None, timeout=3600):
    try:
        # whisper-cli는 진행 출력에서 한글을 바이트 중간에서 자르기도 하므로 깨진 바이트는 치환한다
        res = subprocess.run(cmd, input=input, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout, check=True)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"{Path(cmd[0]).name} 실패 (exit {e.returncode}): {(e.stderr or e.stdout or '').strip()[-500:]}") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{Path(cmd[0]).name} 시간 초과 ({timeout}초)") from None
    return res.stdout


def transcribe(audio, model, workdir):
    wav = workdir / "audio.wav"
    run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", str(audio),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
    base = workdir / "transcript"
    run(["whisper-cli", "-m", str(model), "-l", "ko", "-f", str(wav), "-otxt", "-of", str(base), "-np"])
    wav.unlink()
    return base.with_suffix(".txt").read_text(encoding="utf-8", errors="replace").strip()


def summarize(transcript, title, date):
    prompt = MINUTES_PROMPT.format(title=title, date=date)
    out = run(["claude", "-p", prompt, "--tools", "", "--no-session-persistence"], input=transcript, timeout=900)
    if not out.strip():
        raise RuntimeError("claude가 빈 회의록을 반환했습니다")
    return out.strip()


def render_transcript(rec, text):
    return (f"# {rec.title} — 녹취록\n\n"
            f"- 날짜: {rec.date:%Y-%m-%d %H:%M}\n"
            f"- 길이: {int(rec.duration // 60)}분\n"
            f"- 음성 인식: whisper-cpp large-v3-turbo (자동 변환이라 오류가 있을 수 있음)\n\n"
            f"---\n\n{text}\n")


WEEKDAYS = "월화수목금토일"
_ATTENDEES_RE = re.compile(r"^\s*\**참석자\**\s*[:：]\s*(.*)$")


def display_title(title):
    return re.sub(r"\s{2,}", " ", _KEYWORD_RE.sub(" ", nfc(title))).strip() or nfc(title).strip()


def render_minutes(rec, body):
    # claude가 첫 줄에 "참석자: …"를 쓰면 머리 표로 옮긴다
    lines = body.strip().splitlines()
    m = _ATTENDEES_RE.match(lines[0]) if lines else None
    attendees = (m.group(1).strip() if m else "") or "확인 불가"
    if m:
        body = "\n".join(lines[1:]).strip()
    when = f"{rec.date:%Y-%m-%d}({WEEKDAYS[rec.date.weekday()]}) {rec.date:%H:%M} · {int(rec.duration // 60)}분"
    return (f"# {display_title(rec.title)}\n\n"
            f"| 항목 | 내용 |\n|---|---|\n"
            f"| 일시 | {when} |\n"
            f"| 참석자 | {attendees.replace('|', '/')} |\n"
            f"| 원문 | [녹취록](transcript.md) |\n\n"
            f"{body}\n")


def parse_github_repo(url):
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?/?$", url or "")
    return m.group(1) if m else None


def github_repo(repo):
    res = subprocess.run(["git", "-C", str(repo), "remote", "get-url", "origin"], capture_output=True, text=True)
    return parse_github_repo(res.stdout.strip()) or repo.name


def render_footer(rec, uploaded_at, repo_name):
    if rec.uid.startswith("file:"):
        source = f"오디오 파일 「{rec.path.name}」"
    else:
        source = f"Mac 음성 메모 「{rec.title}」"
    return (f"\n---\n\n**생성 정보**\n\n"
            f"- 원본: {source} ({rec.date:%Y-%m-%d %H:%M} 녹음, {int(rec.duration // 60)}분)\n"
            f"- 녹취: whisper-cpp large-v3-turbo로 Mac에서 처리 (음성 파일은 업로드하지 않음)\n"
            f"- 회의록: Claude가 녹취록을 바탕으로 자동 작성\n"
            f"- 업로드: meeting-sync가 {uploaded_at:%Y-%m-%d %H:%M}에 GitHub {repo_name}에 자동 커밋\n")


def process(rec, repo, model):
    with tempfile.TemporaryDirectory(prefix="meeting-sync-") as tmp:
        text = transcribe(rec.path, model, Path(tmp))
    if not text.strip():
        raise RuntimeError("녹취 결과가 비어 있습니다")
    minutes = summarize(text, rec.title, f"{rec.date:%Y-%m-%d}")
    d = target_dir(repo, rec.date, rec.title)
    d.mkdir(parents=True)
    files = [d / "transcript.md", d / "minutes.md"]
    rel = [str(f.relative_to(repo)) for f in files]
    try:
        footer = render_footer(rec, dt.datetime.now(), github_repo(repo))
        files[0].write_text(render_transcript(rec, text) + footer, encoding="utf-8")
        files[1].write_text(render_minutes(rec, minutes) + footer, encoding="utf-8")
        run(["git", "-C", str(repo), "add", "--", *rel])
        run(["git", "-C", str(repo), "commit", "-q", "-m", f"회의록: {rec.date:%Y-%m-%d} {rec.title}", "--", *rel])
    except Exception:
        subprocess.run(["git", "-C", str(repo), "reset", "-q", "--", *rel], capture_output=True)
        shutil.rmtree(d, ignore_errors=True)
        raise
    return d


FOOTER_MARK = "\n---\n\n**생성 정보**"


def read_saved(d):
    """이미 올린 회의 폴더에서 녹음 정보, 녹취 본문, minutes.md의 생성 정보 블록을 읽는다."""
    t = (d / "transcript.md").read_text(encoding="utf-8")
    head, _, rest = t.partition("\n---\n\n")
    text = rest.split(FOOTER_MARK)[0].strip()
    title = re.search(r"^# (.*) — 녹취록$", head, re.M).group(1)
    date = dt.datetime.strptime(re.search(r"^- 날짜: (.+)$", head, re.M).group(1).strip(), "%Y-%m-%d %H:%M")
    minutes = int(re.search(r"^- 길이: (\d+)분", head, re.M).group(1))
    m = (d / "minutes.md").read_text(encoding="utf-8")
    i = m.find(FOOTER_MARK)
    footer = m[i:] if i >= 0 else ""
    return Recording(f"saved:{d.name}", title, d / "transcript.md", date, minutes * 60.0), text, footer


def reformat(d, repo):
    """저장된 녹취록으로 회의록만 현재 포맷으로 다시 만든다 (다시 녹취하지 않음)."""
    rec, text, footer = read_saved(d)
    minutes = summarize(text, rec.title, f"{rec.date:%Y-%m-%d}")
    f = d / "minutes.md"
    f.write_text(render_minutes(rec, minutes) + footer, encoding="utf-8")
    rel = str(f.relative_to(repo))
    run(["git", "-C", str(repo), "add", "--", rel])
    run(["git", "-C", str(repo), "commit", "-q", "-m", f"회의록 포맷 정리: {rec.date:%Y-%m-%d} {rec.title}", "--", rel])


def push_pending(repo):
    ahead = run(["git", "-C", str(repo), "rev-list", "--count", "@{u}..HEAD"]).strip()
    if ahead != "0":
        # GitHub 웹에서 고친 내용이 있으면 먼저 받아 그 위에 올린다
        run(["git", "-C", str(repo), "pull", "--rebase", "--quiet"], timeout=300)
        run(["git", "-C", str(repo), "push", "--quiet"], timeout=300)
        log.info("push 완료 (커밋 %s개)", ahead)


def notify(title, message):
    subprocess.run(["osascript", "-e", "on run argv", "-e",
                    "display notification (item 2 of argv) with title (item 1 of argv)",
                    "-e", "end run", title, message], capture_output=True)


def acquire_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(STATE_DIR / "lock", "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    return fh


def list_all():
    db = RECORDINGS_DIR / DB_NAME
    with tempfile.TemporaryDirectory() as tmp:
        con = sqlite3.connect(_snapshot_db(db, tmp))
        cols = [r[1] for r in con.execute("PRAGMA table_info(ZCLOUDRECORDING)")]
        con.close()
    print("열:", ", ".join(cols))
    recs = sorted(list_recordings(db, RECORDINGS_DIR), key=lambda r: r.date, reverse=True)
    for r in recs[:20]:
        mark = "*" if is_meeting_title(r.title) else " "
        print(f"{mark} {r.date:%Y-%m-%d %H:%M}  {int(r.duration // 60):>3}분  {r.title!r}  exists={r.path.exists()}")
    print(f"전체 {len(recs)}건 (* = 처리 대상 제목)")
    return 0


def audio_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)])
    return float(out.strip() or 0)


def sync(args):
    done = load_state(STATE_FILE)
    recs = pending(list_recordings(RECORDINGS_DIR / DB_NAME, RECORDINGS_DIR), done)
    if args.dry_run:
        for r in recs:
            print(f"{r.date:%Y-%m-%d %H:%M}  {int(r.duration // 60):>3}분  {r.title}  ({r.path.name})")
        print(f"처리 대상 {len(recs)}건")
        return 0
    if recs:
        log.info("처리 대상 %d건: %s", len(recs), ", ".join(r.title for r in recs))
    failures = 0
    for rec in recs:
        if not rec.path.exists():
            log.info("파일 없음, 건너뜀: %s (%s)", rec.title, rec.path)
            continue
        if not is_stable(rec.path):
            log.info("녹음 중으로 보여 다음 실행으로 넘김: %s", rec.title)
            continue
        log.info("처리 시작: %s", rec.title)
        try:
            d = process(rec, NOTES_REPO, WHISPER_MODEL)
        except Exception as e:
            failures += 1
            log.exception("처리 실패: %s", rec.title)
            notify("회의록 생성 실패", f"{rec.title}: {e}")
            continue
        done.add(rec.uid)
        save_state(STATE_FILE, done)
        log.info("커밋 완료: %s", d)
        if not args.no_push:
            try:
                push_pending(NOTES_REPO)
            except Exception as e:
                log.error("push 실패, 다음 실행에서 다시 시도: %s", e)
        notify("회의록 업로드", rec.title)
    if not args.no_push:
        try:
            push_pending(NOTES_REPO)
        except Exception as e:
            failures += 1
            log.error("push 실패: %s", e)
            notify("회의록 push 실패", str(e))
    return 1 if failures else 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="처리 대상만 출력")
    p.add_argument("--list", action="store_true", help="음성 메모 전체 목록과 DB 열 구성 출력 (진단용)")
    p.add_argument("--no-push", action="store_true", help="커밋까지만 하고 push하지 않음")
    p.add_argument("--file", type=Path, help="음성 메모 대신 이 오디오 파일을 처리 (처리 기록에 남기지 않음)")
    p.add_argument("--title", help="--file과 함께 쓰는 회의 제목")
    p.add_argument("--reformat", nargs="+", type=Path, metavar="DIR",
                   help="이미 올린 회의 폴더의 회의록을 저장된 녹취록으로 다시 만듦")
    args = p.parse_args(argv)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(STATE_DIR / "sync.log", encoding="utf-8"),
                                  logging.StreamHandler(sys.stderr)])

    lock = acquire_lock()
    if lock is None:
        log.info("다른 실행이 진행 중이라 종료")
        return 0
    try:
        if args.file:
            if not args.title:
                p.error("--file에는 --title이 필요합니다")
            rec = Recording(f"file:{args.file}", nfc(args.title), args.file,
                            dt.datetime.fromtimestamp(args.file.stat().st_mtime), audio_duration(args.file))
            print(process(rec, NOTES_REPO, WHISPER_MODEL))
            if not args.no_push:
                push_pending(NOTES_REPO)
            return 0
        if args.reformat:
            for d in args.reformat:
                log.info("포맷 정리: %s", d)
                reformat(d.resolve(), NOTES_REPO)
            if not args.no_push:
                push_pending(NOTES_REPO)
            return 0
        if args.list:
            return list_all()
        return sync(args)
    except PermissionError as e:
        log.error("음성 메모 폴더 접근 거부: %s", e)
        notify("meeting-sync 권한 필요", "meeting-sync-launcher에 전체 디스크 접근 권한을 주세요")
        return 3
    except SchemaError as e:
        log.error("%s", e)
        notify("meeting-sync 오류", "음성 메모 DB 구조가 바뀌었습니다. sync.log를 확인하세요")
        return 2
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
