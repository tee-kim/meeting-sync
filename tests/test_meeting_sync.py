import datetime as dt
import sqlite3
import subprocess
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import meeting_sync as ms  # noqa: E402


class TitleTests(unittest.TestCase):
    def test_meeting_prefixes(self):
        self.assertTrue(ms.is_meeting_title("회의 주간 회고"))
        self.assertTrue(ms.is_meeting_title("  미팅_주간"))
        self.assertTrue(ms.is_meeting_title(unicodedata.normalize("NFD", "회의 테스트")))
        self.assertTrue(ms.is_meeting_title("미션 서비스화 sync-up 회의록"))
        self.assertTrue(ms.is_meeting_title("8/19 PM PD 회의록 Weekly"))
        self.assertTrue(ms.is_meeting_title("주간 공유 미팅"))
        self.assertTrue(ms.is_meeting_title("디자인 리뷰 : 시안 정리 미팅"))
        self.assertTrue(ms.is_meeting_title("제품 주간 회의"))
        self.assertFalse(ms.is_meeting_title("팀미팅"))
        self.assertFalse(ms.is_meeting_title("1on1 메모"))
        self.assertFalse(ms.is_meeting_title("새로운 녹음"))
        self.assertFalse(ms.is_meeting_title("다음 회의 준비"))
        self.assertFalse(ms.is_meeting_title(None))

    def test_slugify(self):
        self.assertEqual(ms.slugify("회의 주간 회고"), "주간-회고")
        self.assertEqual(ms.slugify("미팅: a/b  c"), "a-b-c")
        self.assertEqual(ms.slugify("회의실 예약"), "회의실-예약")
        self.assertEqual(ms.slugify("회의"), "회의")
        self.assertEqual(ms.slugify("미션 서비스화 sync-up 회의록"), "미션-서비스화-sync-up")
        self.assertEqual(ms.slugify("8/16 Live Cell Weekly 회의록"), "8-16-Live-Cell-Weekly")
        self.assertEqual(ms.slugify("회의록"), "회의")
        self.assertEqual(ms.slugify("주간 공유 미팅"), "주간-공유")
        self.assertEqual(ms.slugify("제품 주간 회의"), "제품-주간")

    def test_target_dir_dedupes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            when = dt.datetime(2026, 10, 2, 13, 50)
            first = ms.target_dir(root, when, "회의 주간")
            self.assertEqual(first, root / "2026/10/2026-10-02_주간")
            first.mkdir(parents=True)
            self.assertEqual(ms.target_dir(root, when, "회의 주간"), root / "2026/10/2026-10-02_주간-2")


def make_db(path, rows, cols=("ZUNIQUEID", "ZCUSTOMLABEL", "ZENCRYPTEDTITLE", "ZPATH", "ZDATE", "ZDURATION")):
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE ZCLOUDRECORDING ({', '.join(cols)})")
    con.executemany(f"INSERT INTO ZCLOUDRECORDING VALUES ({', '.join('?' * len(cols))})", rows)
    con.commit()
    con.close()


class DbTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_list_recordings(self):
        db = self.tmp / "CloudRecordings.db"
        make_db(db, [
            ("u1", "회의 주간", None, "a.m4a", 0, 65.0),
            ("u2", None, "미팅 주간", "/abs/b.m4a", 86400, 10.0),
            ("u3", "빈 경로", None, None, 0, 1.0),
        ])
        recs = ms.list_recordings(db, self.tmp)
        self.assertEqual([r.uid for r in recs], ["u1", "u2"])
        self.assertEqual(recs[0].title, "회의 주간")
        self.assertEqual(recs[0].path, self.tmp / "a.m4a")
        self.assertEqual(recs[0].date, dt.datetime.fromtimestamp(978307200))
        self.assertEqual(recs[0].duration, 65.0)
        self.assertEqual(recs[1].title, "미팅 주간")
        self.assertEqual(recs[1].path, Path("/abs/b.m4a"))

    def test_schema_error(self):
        db = self.tmp / "CloudRecordings.db"
        make_db(db, [("u1", "회의", 0)], cols=("ZUNIQUEID", "ZCUSTOMLABEL", "ZDATE"))
        with self.assertRaises(ms.SchemaError):
            ms.list_recordings(db, self.tmp)

    def test_state_and_pending(self):
        state = self.tmp / "s" / "processed.json"
        self.assertEqual(ms.load_state(state), set())
        ms.save_state(state, {"u1"})
        self.assertEqual(ms.load_state(state), {"u1"})
        now = dt.datetime(2026, 10, 6)
        recs = [ms.Recording("u1", "회의 a", Path("a"), now, 0),
                ms.Recording("u2", "회의 b", Path("b"), now, 0),
                ms.Recording("u3", "새로운 녹음", Path("c"), now, 0)]
        self.assertEqual([r.uid for r in ms.pending(recs, {"u1"})], ["u2"])


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


class GithubRepoTests(unittest.TestCase):
    def test_parse_remote(self):
        self.assertEqual(ms.parse_github_repo("https://github.com/owner/meeting-notes.git"), "owner/meeting-notes")
        self.assertEqual(ms.parse_github_repo("git@github.com:owner/meeting-notes.git"), "owner/meeting-notes")
        self.assertEqual(ms.parse_github_repo("https://github.com/owner/meeting-notes"), "owner/meeting-notes")
        self.assertIsNone(ms.parse_github_repo(""))

    def test_github_repo_falls_back_to_dir_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "notes"
            repo.mkdir()
            git(repo, "init", "-q")
            self.assertEqual(ms.github_repo(repo), "notes")
            git(repo, "remote", "add", "origin", "https://github.com/owner/notes.git")
            self.assertEqual(ms.github_repo(repo), "owner/notes")


class RunTests(unittest.TestCase):
    def test_run_tolerates_broken_utf8(self):
        # whisper-cli가 한글을 바이트 중간에서 잘라 출력하는 경우
        out = ms.run(["printf", "\\352\\260ok"])
        self.assertTrue(out.endswith("ok"))


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        git(self.repo, "init", "-q")
        git(self.repo, "config", "user.email", "t@example.com")
        git(self.repo, "config", "user.name", "t")
        self.rec = ms.Recording("u1", "회의 주간 회고", Path("a.m4a"), dt.datetime(2026, 10, 2, 13, 50), 125.0)

    def tearDown(self):
        self._tmp.cleanup()

    def test_render(self):
        t = ms.render_transcript(self.rec, "안녕하세요")
        self.assertTrue(t.startswith("# 회의 주간 회고 — 녹취록\n"))
        self.assertIn("- 날짜: 2026-10-02 13:50", t)
        self.assertIn("- 길이: 2분", t)
        self.assertTrue(t.rstrip().endswith("안녕하세요"))
        m = ms.render_minutes(self.rec, "참석자: 민수, 지영\n\n## 1. 요약\n- x")
        self.assertTrue(m.startswith("# 회의 주간 회고\n\n| 항목 | 내용 |\n|---|---|\n"))
        self.assertIn("| 일시 | 2026-10-02(금) 13:50 · 2분 |\n", m)
        self.assertIn("| 참석자 | 민수, 지영 |\n", m)
        self.assertIn("| 원문 | [녹취록](transcript.md) |\n", m)
        self.assertIn("\n\n## 1. 요약\n- x", m)
        self.assertNotIn("참석자: 민수", m)

    def test_render_minutes_title_and_missing_attendees(self):
        rec = ms.Recording("u", "주간 공유 회의록", Path("a"), dt.datetime(2026, 10, 5, 9, 0), 3600.0)
        m = ms.render_minutes(rec, "## 1. 요약\n- x")
        self.assertTrue(m.startswith("# 주간 공유\n"))
        self.assertIn("| 일시 | 2026-10-05(월) 09:00 · 60분 |", m)
        self.assertIn("| 참석자 | 확인 불가 |", m)

    def test_read_saved_transcript(self):
        d = self.repo / "2026/10/x"
        d.mkdir(parents=True)
        footer = ms.render_footer(self.rec, dt.datetime(2026, 10, 6), "owner/meeting-notes")
        (d / "transcript.md").write_text(ms.render_transcript(self.rec, "첫 줄\n둘째 줄") + footer, encoding="utf-8")
        (d / "minutes.md").write_text("# 옛 회의록\n" + footer, encoding="utf-8")
        rec, text, old_footer = ms.read_saved(d)
        self.assertEqual((rec.title, rec.date, int(rec.duration // 60)), ("회의 주간 회고", dt.datetime(2026, 10, 2, 13, 50), 2))
        self.assertEqual(text, "첫 줄\n둘째 줄")
        self.assertEqual(old_footer, footer)

    def test_reformat_rewrites_minutes_only(self):
        d = self.repo / "2026/10/x"
        d.mkdir(parents=True)
        footer = ms.render_footer(self.rec, dt.datetime(2026, 10, 6), "owner/meeting-notes")
        (d / "transcript.md").write_text(ms.render_transcript(self.rec, "녹취") + footer, encoding="utf-8")
        (d / "minutes.md").write_text("# 옛 회의록\n" + footer, encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-qm", "init")
        with mock.patch.object(ms, "summarize", return_value="참석자: 확인 불가\n\n## 1. 요약\n- 새"):
            ms.reformat(d, self.repo)
        m = (d / "minutes.md").read_text(encoding="utf-8")
        self.assertIn("## 1. 요약\n- 새", m)
        self.assertTrue(m.endswith(footer))
        log = subprocess.run(["git", "-C", str(self.repo), "log", "-1", "--format=%s"], capture_output=True, text=True).stdout
        self.assertEqual(log.strip(), "회의록 포맷 정리: 2026-10-02 회의 주간 회고")

    def test_render_footer(self):
        f = ms.render_footer(self.rec, dt.datetime(2026, 10, 6, 22, 3), "owner/meeting-notes")
        self.assertTrue(f.startswith("\n---\n\n**생성 정보**\n"))
        self.assertIn("- 원본: Mac 음성 메모 「회의 주간 회고」 (2026-10-02 13:50 녹음, 2분)", f)
        self.assertIn("음성 파일은 업로드하지 않음", f)
        self.assertIn("- 회의록: Claude가 녹취록을 바탕으로 자동 작성", f)
        self.assertIn("- 업로드: meeting-sync가 2026-10-06 22:03에 GitHub owner/meeting-notes에 자동 커밋", f)

    def test_render_footer_file_source(self):
        rec = ms.Recording("file:/x/a.m4a", "회의 a", Path("/x/a.m4a"), dt.datetime(2026, 10, 2, 13, 50), 60.0)
        self.assertIn("- 원본: 오디오 파일 「a.m4a」 (2026-10-02 13:50 녹음, 1분)", ms.render_footer(rec, dt.datetime(2026, 10, 6), "owner/meeting-notes"))

    def test_process_commits(self):
        with mock.patch.object(ms, "transcribe", return_value="녹취 본문"), \
             mock.patch.object(ms, "summarize", return_value="## 요약\n- 결정"):
            d = ms.process(self.rec, self.repo, Path("model.bin"))
        self.assertEqual(d, self.repo / "2026/10/2026-10-02_주간-회고")
        self.assertIn("녹취 본문", (d / "transcript.md").read_text())
        self.assertIn("- 결정", (d / "minutes.md").read_text())
        for name in ("transcript.md", "minutes.md"):
            self.assertIn("**생성 정보**", (d / name).read_text())
        self.assertIn("회의록: 2026-10-02 회의 주간 회고", git(self.repo, "log", "--format=%s"))
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_process_failure_leaves_nothing(self):
        with mock.patch.object(ms, "transcribe", return_value="녹취 본문"), \
             mock.patch.object(ms, "summarize", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                ms.process(self.rec, self.repo, Path("model.bin"))
        self.assertFalse((self.repo / "2026").exists())

    def test_empty_transcript_fails(self):
        with mock.patch.object(ms, "transcribe", return_value="  "):
            with self.assertRaises(RuntimeError):
                ms.process(self.rec, self.repo, Path("model.bin"))



class PushTests(unittest.TestCase):
    def test_push_after_remote_edit(self):
        # GitHub 웹에서 파일을 고친 뒤에도 자동 push가 실패하지 않아야 한다
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            remote, a, b = tmp / "remote.git", tmp / "a", tmp / "b"
            subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
            for clone in (a, b):
                subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True, capture_output=True)
                git(clone, "config", "user.email", "t@example.com")
                git(clone, "config", "user.name", "t")
            (a / "x.md").write_text("1\n")
            git(a, "add", "x.md"); git(a, "commit", "-qm", "init"); git(a, "push", "-q", "-u", "origin", "main")
            git(b, "pull", "-q", "origin", "main"); git(b, "branch", "-q", "-u", "origin/main")
            (b / "x.md").write_text("edited on web\n")
            git(b, "commit", "-qam", "web edit"); git(b, "push", "-q")
            (a / "y.md").write_text("new minutes\n")
            git(a, "add", "y.md"); git(a, "commit", "-qm", "auto")
            ms.push_pending(a)
            self.assertEqual(git(a, "rev-list", "--count", "@{u}..HEAD").strip(), "0")
            self.assertEqual((a / "x.md").read_text(), "edited on web\n")


if __name__ == "__main__":
    unittest.main()
