"""Running on the Android tablet: schedule, jobs, status, backups, memory import and the self-updater."""
from __future__ import annotations

import gzip
import io
import os
import shutil
import sqlite3
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

import pytest

from leadgen import tablet
from leadgen.tablet import JOBS, RESTART, TZ, Job, Tablet, due_slot, next_slot, pick_due

REPO = Path(__file__).resolve().parent.parent


def ist(y, m, d, hh, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=TZ)


ON = {"LEADGEN_ENABLED": "true", "OUTREACH_ENABLED": "true", "AUTO_UPDATE": "true"}


# ------------------------------------------------------------------ schedule
def test_due_slot_runs_each_slot_once_and_catches_up_once():
    job = JOBS["followup"]                       # 11:30 16:30 21:30
    assert due_slot(job, ist(2026, 10, 10, 11, 29), None) is None
    assert due_slot(job, ist(2026, 10, 10, 11, 30), None) == ist(2026, 10, 10, 11, 30)
    ran = ist(2026, 10, 10, 11, 30, 5).timestamp()
    assert due_slot(job, ist(2026, 10, 10, 16, 0), ran) is None
    # tablet off from 16:00 to 22:00: only the latest missed slot runs, once
    assert due_slot(job, ist(2026, 10, 10, 22, 0), ran) == ist(2026, 10, 10, 21, 30)
    assert due_slot(job, ist(2026, 10, 10, 22, 1), ist(2026, 10, 10, 22, 0).timestamp()) is None


def test_outreach_only_monday_to_saturday_every_hour():
    job = JOBS["outreach"]
    sunday = ist(2026, 10, 11, 12, 0)
    assert sunday.weekday() == 6
    assert job.slots(sunday) == []
    sat = ist(2026, 10, 10, 10, 11)
    assert [s.strftime("%H:%M") for s in job.slots(sat)] == [f"{h}:11" for h in range(10, 19)]
    assert next_slot(job, ist(2026, 10, 10, 18, 30)) == ist(2026, 10, 12, 10, 11)     # Monday


def test_pick_due_order_switches_and_daily_covers_followup():
    # Tablet switched on at 12:05 on a Saturday with nothing run today.
    now = ist(2026, 10, 10, 12, 5)
    status = {"jobs": {"update": {"last_started": ist(2026, 10, 10, 5, 15).timestamp()},
                       "backup": {"last_started": ist(2026, 10, 9, 23, 30).timestamp()}}}
    assert pick_due(now, status, ON).name == "outreach"          # time-bound work first
    status["jobs"]["outreach"] = {"last_started": now.timestamp()}
    assert pick_due(now, status, ON).name == "daily"
    assert pick_due(now, status, {**ON, "LEADGEN_ENABLED": "false"}) is None
    # running "daily" also counts as the 11:30 follow-up
    status["jobs"]["daily"] = {"last_started": now.timestamp()}
    status["jobs"]["followup"] = {"last_started": now.timestamp()}
    assert pick_due(now, status, ON) is None
    assert pick_due(ist(2026, 10, 10, 16, 31), status, ON).name == "outreach"      # its 16:11 slot
    status["jobs"]["outreach"] = {"last_started": ist(2026, 10, 10, 16, 31).timestamp()}
    assert pick_due(ist(2026, 10, 10, 16, 32), status, ON).name == "followup"


def test_every_job_has_valid_times_and_steps():
    from leadgen.cli import build_parser

    parser = build_parser()
    for job in JOBS.values():
        assert job.slots(ist(2026, 10, 12, 0, 0)) or job.weekdays is not None
        for step in job.steps:
            parser.parse_args(step)          # each step is a valid leadgen command line
    assert tablet.PRIORITY[0] == "update" and set(tablet.PRIORITY) == set(JOBS)


# ------------------------------------------------------------------ settings
def test_settings_round_trip_private_and_env(tmp_path):
    p = tablet.paths(tmp_path)
    tablet.write_settings(p["settings"], {"PP_SHEET_ID": "abc", "OUTREACH_GMAIL_APP_PASSWORD": "abcdabcdabcdabcd",
                                          "GOOGLE_SERVICE_ACCOUNT_FILE": "service-account.json", "OUTREACH_LIVE": "yes",
                                          "CUSTOM_X": "1"})
    assert oct(p["settings"].stat().st_mode & 0o777) == "0o600"
    s = tablet.read_settings(p["settings"])
    assert s["PP_SHEET_ID"] == "abc" and s["CUSTOM_X"] == "1"
    env = tablet.job_env(p, s, base_env={"PATH": "/usr/bin", "SECRET_FROM_SHELL": "x"})
    assert env["GOOGLE_SERVICE_ACCOUNT_FILE"] == str(tmp_path / "service-account.json")
    assert env["OUTREACH_LIVE"] == "true" and env["PP_DB"] == str(p["db"])
    assert "SECRET_FROM_SHELL" not in env
    assert tablet.job_env(p, {"OUTREACH_LIVE": "false"}, base_env={})["OUTREACH_LIVE"] == ""


def test_sheet_id_and_app_password_checks():
    url = "https://docs.google.com/spreadsheets/d/1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789-xyz/edit#gid=0"
    assert tablet.sheet_id_from(url) == "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789-xyz"
    assert tablet.sheet_id_from("not a sheet") == ""
    assert tablet.sheet_id_from("\u200b1h9Wlk0Bo7ZXD2npZXsS8ZoJLq3xfdfa0G7vdeOwzzyo\u200b ") == \
        "1h9Wlk0Bo7ZXD2npZXsS8ZoJLq3xfdfa0G7vdeOwzzyo"
    assert tablet.app_password_ok("abcd efgh ijkl mnop")
    assert not tablet.app_password_ok("MyGmailPassword123")


# ------------------------------------------------------------------ jobs
class FakeRunner:
    def __init__(self, codes=None):
        self.calls = []
        self.codes = codes or {}

    def __call__(self, args, *, cwd, env, log_path, timeout_s, heartbeat=None, stop_flag=None):
        self.calls.append((args[3:], env))
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"ran {' '.join(args[3:])}\n")
        return self.codes.get(args[3], 0)


def test_run_job_runs_steps_records_status_and_alerts_on_repeated_failure(tmp_path, monkeypatch):
    p = tablet.paths(tmp_path)
    tablet.write_settings(p["settings"], {"PP_SHEET_ID": "sid", "OUTREACH_GMAIL_ADDRESS": "pp@example.com",
                                          "OUTREACH_GMAIL_APP_PASSWORD": "abcdabcdabcdabcd"})
    alerts = []
    monkeypatch.setattr(tablet, "send_alert", lambda s, subj, body: alerts.append(subj) or True)
    clock = [ist(2026, 10, 10, 6, 0, 2).timestamp()]
    runner = FakeRunner({"email-hunt": 2})
    t = Tablet(p, python="py", runner=runner, now_fn=lambda: clock[0])
    assert t.run_job(JOBS["daily"]) == 2
    assert [c[0][0] for c in runner.calls] == ["run", "email-hunt", "usp-refresh"]
    assert runner.calls[0][1]["PP_SHEET_ID"] == "sid"
    st = t.status()
    assert st["jobs"]["daily"]["fails_in_row"] == 1 and st["jobs"]["daily"]["running"] is False
    assert st["jobs"]["followup"]["last_started"] == clock[0]          # daily covers the follow-up
    assert alerts == []                                                # one failure: no alert yet
    t.run_job(JOBS["daily"])
    assert alerts == ["Lead generation needs a look"]
    t.run_job(JOBS["daily"])
    assert len(alerts) == 1                                            # at most one alert per job per day
    log = p["logs"] / "2026-10-10.log"
    assert "ran run --budget-minutes 120" in (log.read_text() if log.exists() else
                                              next(p["logs"].glob("*.log")).read_text())


def test_job_lock_one_job_at_a_time(tmp_path):
    p = tablet.paths(tmp_path)
    held = tablet.JobLock(p)
    assert held.acquire(wait=False)
    t = Tablet(p, python="py", runner=FakeRunner())
    assert t.run_job(JOBS["followup"], manual=True) == tablet.BUSY
    held.release()
    assert t.run_job(JOBS["followup"], manual=True) == 0


def test_serve_runs_due_jobs_then_waits(tmp_path, monkeypatch):
    p = tablet.paths(tmp_path)
    tablet.write_settings(p["settings"], {**ON, "AUTO_UPDATE": "false"})
    monkeypatch.setattr(tablet, "git_head_short", lambda: "abc1234")
    clock = [ist(2026, 10, 10, 6, 0, 1).timestamp()]
    runner = FakeRunner()
    t = Tablet(p, python="py", runner=runner, now_fn=lambda: clock[0])
    # backup (23:30 yesterday) counts as done
    tablet.save_json(p["status"], {"jobs": {"backup": {"last_started": ist(2026, 10, 9, 23, 30).timestamp()}}})
    assert t.serve(max_loops=3, sleep_fn=lambda s: None) == 0
    assert [c[0][0] for c in runner.calls] == ["run", "email-hunt", "usp-refresh"]
    st = t.status()
    assert st["scheduler"]["version"] == "abc1234" and "heartbeat" in st["scheduler"]
    assert not p["pid"].exists()


def test_serve_restarts_after_an_update(tmp_path, monkeypatch):
    p = tablet.paths(tmp_path)
    tablet.write_settings(p["settings"], ON)
    monkeypatch.setattr(tablet, "git_head_short", lambda: "abc1234")
    monkeypatch.setattr(tablet, "update", lambda *a, **k: RESTART)
    t = Tablet(p, python="py", runner=FakeRunner(), now_fn=lambda: ist(2026, 10, 10, 5, 15, 3).timestamp())
    assert t.serve(max_loops=5, sleep_fn=lambda s: None) == RESTART


def test_backup_keeps_seven_days(tmp_path):
    p = tablet.paths(tmp_path)
    p["state"].mkdir(parents=True)
    con = sqlite3.connect(p["db"])
    con.execute("CREATE TABLE places (key TEXT)")
    con.execute("INSERT INTO places VALUES ('a')")
    con.commit()
    con.close()
    for d in range(1, 10):
        (p["backups"] / f"2026-09-{d:02d}").mkdir(parents=True)
    t = Tablet(p, python="py", runner=FakeRunner(), now_fn=lambda: ist(2026, 10, 10, 23, 30).timestamp())
    assert t.backup() == 0
    days = sorted(d.name for d in p["backups"].iterdir())
    assert len(days) == 7 and days[-1] == "2026-10-10"
    copy = sqlite3.connect(p["backups"] / "2026-10-10" / "leadgen.sqlite")
    assert copy.execute("SELECT key FROM places").fetchall() == [("a",)]


def test_status_text_renders(tmp_path, monkeypatch):
    p = tablet.paths(tmp_path)
    monkeypatch.setattr(tablet, "git_head_short", lambda: "abc1234")
    text = tablet.status_text(p, now_ts=ist(2026, 10, 10, 9, 0).timestamp())
    assert "NOT RUNNING" in text and "Lead generation" in text and "not imported yet" in text


# ------------------------------------------------------------------ GitHub memory import
def _openssl_encrypt(data: bytes, key: str) -> bytes:
    """What scripts/ci/state.sh does."""
    r = subprocess.run(["openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-md", "sha256", "-salt",
                        "-pass", "env:PP_STATE_KEY"], input=data, capture_output=True, env={**os.environ, "PP_STATE_KEY": key},
                       check=True)
    return r.stdout


def _make_db(path: Path, tables: list[str]) -> bytes:
    con = sqlite3.connect(path)
    for t in tables:
        con.execute(f"CREATE TABLE {t} (id INTEGER)")
    con.commit()
    con.close()
    return path.read_bytes()


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")
def test_import_github_state_zip(tmp_path):
    key = "mango-river-blue-tiger-lamp-cloud"
    lead = gzip.compress(_make_db(tmp_path / "l.sqlite", ["places", "tasks", "parts", "meta"]))
    outr = gzip.compress(_make_db(tmp_path / "o.sqlite", ["sends", "wa", "meta"]))
    files = []
    for name, payload in (("pp-state.zip", lead), ("pp-outreach.zip", outr)):
        zp = tmp_path / name
        with zipfile.ZipFile(zp, "w") as z:
            z.writestr("pp-state.enc", _openssl_encrypt(payload, key))     # both artifacts use this inner name
        files.append(zp)
    p = tablet.paths(tmp_path / "home")
    with pytest.raises(ValueError, match="wrong PP_STATE_KEY"):
        tablet.import_state(p, files, "not-the-key", log=lambda m: None)
    done = tablet.import_state(p, files, key + "\n", log=lambda m: None)
    assert done == {"leadgen": "pp-state.zip", "outreach": "pp-outreach.zip"}
    assert tablet.db_kind(p["db"]) == "leadgen" and tablet.db_kind(p["outreach_db"]) == "outreach"
    # importing again keeps the earlier copy
    tablet.import_state(p, files[:1], key, log=lambda m: None)
    assert list(p["backups"].glob("before-import-*/leadgen.sqlite"))


def test_service_account_key_detection(tmp_path):
    good = tmp_path / "plant-parlour-automation-1234.json"
    good.write_text('{"type": "service_account", "private_key": "-----BEGIN", "client_email": "bot@x.iam.gserviceaccount.com"}')
    other = tmp_path / "data.json"
    other.write_text('{"type": "something"}')
    assert tablet.is_service_account_key(good) == "bot@x.iam.gserviceaccount.com"
    assert tablet.is_service_account_key(other) == ""


# ------------------------------------------------------------------ self-update
def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    """origin (bare) + the tablet's clone; checks are faked: a file named BROKEN makes them fail."""
    origin, work, clone = tmp_path / "origin.git", tmp_path / "work", tmp_path / "clone"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    for k, v in (("user.email", "t@example.com"), ("user.name", "t")):
        _git(work, "config", k, v)
    (work / "requirements.txt").write_text("requests\n")
    (work / "app.txt").write_text("v1\n")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "v1")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main")
    _git(tmp_path, "clone", "-q", str(origin), str(clone))
    monkeypatch.setattr(tablet, "REPO", clone)
    monkeypatch.setattr(tablet, "pip_install", lambda python, log: True)
    monkeypatch.setattr(tablet, "send_alert", lambda *a, **k: True)
    monkeypatch.setattr(tablet, "run_checks", lambda python, log, runner=None: (not (clone / "BROKEN").exists(), set()))

    def commit(name, files):
        for f, text in files.items():
            if text is None:
                (work / f).unlink()
            else:
                (work / f).write_text(text)
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", name)
        _git(work, "push", "-q", "origin", "main")
        return _git(work, "rev-parse", "HEAD")
    return {"clone": clone, "commit": commit, "p": tablet.paths(tmp_path / "home")}


def test_update_installs_tested_versions_and_rolls_back_broken_ones(repos):
    clone, commit, p = repos["clone"], repos["commit"], repos["p"]
    quiet = lambda m: None  # noqa: E731
    assert tablet.update(p, python="py", log=quiet) == 0                       # nothing new
    v2 = commit("v2", {"app.txt": "v2\n"})
    assert tablet.update(p, python="py", log=quiet) == RESTART
    assert _git(clone, "rev-parse", "HEAD") == v2
    bad = commit("v3 broken", {"BROKEN": "x"})
    assert tablet.update(p, python="py", log=quiet) == 4                       # tested, failed, undone
    assert _git(clone, "rev-parse", "HEAD") == v2 and not (clone / "BROKEN").exists()
    rec = tablet.load_json(p["update"])
    assert bad in rec["bad_commits"] and rec["last_result"].startswith("rolled back")
    assert tablet.update(p, python="py", log=quiet) == 0                       # does not retry the same bad version
    v4 = commit("v4 fixed", {"BROKEN": None, "app.txt": "v4\n"})
    assert tablet.update(p, python="py", log=quiet) == RESTART
    assert _git(clone, "rev-parse", "HEAD") == v4
    # back to the version before by hand; automatic updates then skip v4 - and still skip the broken v3
    assert tablet.rollback(p, python="py", log=quiet) == RESTART
    assert _git(clone, "rev-parse", "HEAD") == v2
    assert tablet.update(p, python="py", log=quiet) == 0
    assert set(tablet.load_json(p["update"])["bad_commits"]) == {bad, v4}
    v5 = commit("v5", {"app.txt": "v5\n"})
    assert tablet.update(p, python="py", log=quiet) == RESTART
    assert _git(clone, "rev-parse", "HEAD") == v5


def test_update_keeps_local_changes_unless_asked(repos):
    clone, commit, p = repos["clone"], repos["commit"], repos["p"]
    quiet = lambda m: None  # noqa: E731
    (clone / "app.txt").write_text("edited on the tablet\n")
    commit("v2", {"app.txt": "v2\n"})
    assert tablet.update(p, python="py", log=quiet) == 3
    assert (clone / "app.txt").read_text() == "edited on the tablet\n"
    assert tablet.update(p, python="py", log=quiet, discard_local=True) == RESTART
    assert (clone / "app.txt").read_text() == "v2\n"
    patches = list((p["home"] / "update").glob("local-changes-*.patch"))
    assert patches and "edited on the tablet" in patches[0].read_text()


def test_junit_failures():
    xml = """<testsuites><testsuite><testcase classname="tests.test_a" name="test_ok"/>
    <testcase classname="tests.test_a" name="test_bad"><failure message="x"/></testcase>
    <testcase classname="tests.test_b" name="test_err"><error message="y"/></testcase></testsuite></testsuites>"""
    assert tablet.junit_failures(xml) == {"tests.test_a::test_bad", "tests.test_b::test_err"}


# ------------------------------------------------------------------ the tablet's shell scripts
@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_tablet_scripts_parse():
    scripts = sorted((REPO / "scripts" / "tablet").glob("*.sh"))
    assert scripts
    for s in scripts:
        r = subprocess.run(["bash", "-n", str(s)], capture_output=True, text=True)
        assert r.returncode == 0, f"{s.name}: {r.stderr}"
        assert os.access(s, os.X_OK), f"{s.name} is not executable"


def test_cli_dispatches_tablet_commands(tmp_path, monkeypatch, capsys):
    from leadgen import cli

    monkeypatch.setenv("PP_HOME", str(tmp_path))
    monkeypatch.setattr(tablet, "git_head_short", lambda: "abc1234")
    assert cli.main(["tablet", "status"]) == 0
    assert "Plant Parlour on this tablet" in capsys.readouterr().out


def test_log_tail_and_masks(tmp_path):
    f = tmp_path / "x.log"
    f.write_text("\n".join(str(i) for i in range(100)))
    assert tablet.log_tail(f, 3) == "97\n98\n99"
    assert tablet.mask("abcdefghijklmnop") == "ab************op"
    assert io  # keep the import used


def test_wizard_saves_answers_and_stops_cleanly_when_input_ends(tmp_path, monkeypatch):
    import json

    p = tablet.paths(tmp_path / "home")
    dl = tmp_path / "Download"
    dl.mkdir()
    key = dl / "plant-parlour-automation-0a1b2c.json"
    key.write_text(json.dumps({"type": "service_account", "private_key": "x",
                               "client_email": "bot@x.iam.gserviceaccount.com"}))
    monkeypatch.setattr(tablet, "download_dirs", lambda: [dl])
    monkeypatch.setattr(tablet, "check_sheet", lambda env: (True, "Google Sheet OK"))
    monkeypatch.setattr(tablet, "check_gmail", lambda s: (True, "Gmail login OK"))
    sid = "1AbCdEfGhIjKlMnOpQrStUvWxYz_0123456789-xyz"

    def scripted(answers):
        it = iter(answers)

        def fake(prompt=""):
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None
        monkeypatch.setattr("builtins.input", fake)
        monkeypatch.setattr("getpass.getpass", fake)

    scripted([f"https://docs.google.com/spreadsheets/d/{sid}/edit#gid=0", "y",      # sheet, delete key from Downloads
              "y", "pp.outreach@gmail.com",                                         # "y" is refused as an address
              "MyGmailPassword123", "Abcd Efgh Ijkl Mnop", "+91 90000 00000", "-"])
    assert tablet.wizard(p) == 0
    s = tablet.read_settings(p["settings"])
    assert s["PP_SHEET_ID"] == sid and s["OUTREACH_GMAIL_APP_PASSWORD"] == "abcdefghijklmnop"
    assert s["OUTREACH_NOTIFY_EMAIL"] == "" and s["GOOGLE_SERVICE_ACCOUNT_FILE"] == "service-account.json"
    assert p["key"].exists() and not key.exists()
    assert oct(p["key"].stat().st_mode & 0o777) == "0o600"
    # Running again: Enter keeps a value; input that ends early stops with a message, nothing is lost.
    scripted([""])
    with pytest.raises(SystemExit):
        tablet.wizard(p)
    assert tablet.read_settings(p["settings"])["OUTREACH_GMAIL_APP_PASSWORD"] == "abcdefghijklmnop"


def test_test_email_shows_first_and_sends_only_in_hours_after_yes(tmp_path, monkeypatch, capsys):
    p = tablet.paths(tmp_path)
    monkeypatch.setattr(tablet, "check_sheet", lambda env: (True, "Google Sheet OK"))
    monkeypatch.setattr(tablet, "check_gmail", lambda s: (True, "Gmail login OK"))
    runs = []

    class FakeOutreach:
        in_hours = True

        def __init__(self, cfg, store, live=None, max_emails=None):
            self.live, self.max_emails = live, max_emails
            self.preview = [["2026-10-10", "PP-1", "Peter Cat", "info@petercat.example", "Plants for Peter Cat", "Hello", "k1"]]
            self.sent_log, self.notes = [], ["dry-run: nothing sent; Gmail login OK"]

        def run(self):
            runs.append(self.live)
            if self.live:
                self.sent_log = [["PP-1", "Peter Cat", "info@petercat.example", "sent", "s", "b"]]
            return 0, {"notes": []}

        def _local(self):
            return None

        def _window(self, local):
            return (self.in_hours, "" if self.in_hours else "outside sending hours (10:00-18:30)", None)

    answers = iter(["y"])
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    FakeOutreach.in_hours = False
    assert tablet._test_email(None, None, 2, FakeOutreach) == 0
    assert runs == [False] and "Not sending now" in capsys.readouterr().out
    FakeOutreach.in_hours = True
    assert tablet._test_email(None, None, 2, FakeOutreach) == 0
    out = capsys.readouterr().out
    assert runs == [False, False, True] and "To: info@petercat.example" in out and "sent" in out
    assert p  # paths unused by the inner flow


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")
def test_guest_commands_use_ubuntus_own_programs(tmp_path):
    """On the tablet Termux had its own (Android) Python 3.14, and proot-distro appends Termux's bin folder to the
    PATH inside Ubuntu: `python3` found Termux's, the environment was built from it and duckdb could not install.
    Guest commands must get Ubuntu's PATH only, and an environment not made from /usr/bin is not accepted."""
    home = tmp_path / "home"
    (home / ".plant-parlour" / "venv").mkdir(parents=True)
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "proot-distro").write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
    (stub / "proot-distro").chmod(0o755)
    lib = REPO / "scripts" / "tablet" / "lib.sh"
    env = {"HOME": str(home), "PATH": f"{stub}:/usr/bin:/bin"}

    def sh(script):
        return subprocess.run(["bash", "-c", f". {lib}; {script}"], capture_output=True, text=True, env=env)

    args = sh("pp_guest python3 -m pip --version").stdout.splitlines()
    sep = args.index("--")
    assert args[sep + 1:sep + 3] == ["/usr/bin/env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]
    assert "/data/data/com.termux" not in " ".join(args[sep:])
    assert args[-4:] == ["python3", "-m", "pip", "--version"]
    cfg = home / ".plant-parlour" / "venv" / "pyvenv.cfg"
    cfg.write_text("home = /data/data/com.termux/files/usr/bin\nversion = 3.14.0\n")
    assert sh("pp_have_python_env").returncode == 1
    cfg.write_text("home = /usr/bin\nversion = 3.12.3\n")
    assert sh("pp_have_python_env").returncode == 0
    setup = (REPO / "scripts" / "tablet" / "setup.sh").read_text()
    assert "pp_guest python3 " not in setup and "/usr/bin/python3 -m venv" in setup
    assert "--only-binary=:all:" in setup


def test_downloads_listed_once_even_under_two_names(tmp_path, monkeypatch):
    real = tmp_path / "Download"
    real.mkdir()
    (real / "pp-state.zip").write_bytes(b"x")
    other = tmp_path / "sdcard-Download"
    other.symlink_to(real)            # the same folder under another name (on the tablet: separate bind mounts)
    monkeypatch.setattr(tablet, "download_dirs", lambda: [real, other])
    assert [f.name for f in tablet.find_downloads("pp-state*.zip")] == ["pp-state.zip"]
