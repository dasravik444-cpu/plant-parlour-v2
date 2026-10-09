"""Running the system on an Android tablet (Termux + Ubuntu in proot-distro), without GitHub Actions.

Two folders in the Termux home hold everything:
  ~/plant-parlour-v2   the code: a git clone that updates itself every morning (tested first, undone if broken)
  ~/.plant-parlour     the owner's data: settings.env (passwords), the Google key, the campaign memory, logs,
                       backups and the Python environment - never inside the code folder, so updates can't touch it

Commands (run inside Ubuntu; the `pp` command on the tablet calls them):
  python -m leadgen tablet serve        the scheduler: runs every job at its time, forever
  python -m leadgen tablet status       what ran, what runs next, today's numbers
  python -m leadgen tablet run JOB      run one job now: daily | followup | outreach | backup | update
  python -m leadgen tablet update       get the newest version, test it, keep it or go back
  python -m leadgen tablet rollback     go back to the version before the last update
  python -m leadgen tablet setup        ask for the passwords and keys, check them, save them (the wizard)
  python -m leadgen tablet check        test the Google Sheet and Gmail connections
  python -m leadgen tablet import F...  load the campaign memory GitHub saved (pp-state.zip, pp-outreach.zip)
  python -m leadgen tablet test-email   show the next 2 outreach e-mails, send them after a "yes"

Times are India time (the campaign's time zone). The jobs are the GitHub workflows' steps, one at a time:
  05:15         update (when switched on)
  06:00         lead generation: the day's new leads, then the e-mail hunt and USP lines for them
  11:30 16:30 21:30   follow-ups: unfinished enrichment, e-mail hunt, USP lines
  10:11 ... 18:11     outreach, every hour, Monday to Saturday (e-mails + WhatsApp queue + replies)
  23:30         backup of the campaign memory (7 days kept) and old logs removed
A slot missed while the tablet was off runs as soon as it is back (once).
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Asia/Kolkata")
RESTART = 10            # exit code of `update` (and of the scheduler after an update): start again with the new code
BUSY = 75               # another scheduler / another job is running
EXTRA_PACKAGES = ["duckdb>=1.1", "curl_cffi>=0.7", "pytest>=8"]

# settings.env keys, in the order the file is written
SETTING_KEYS = ["PP_SHEET_ID", "GOOGLE_SERVICE_ACCOUNT_FILE", "OUTREACH_GMAIL_ADDRESS", "OUTREACH_GMAIL_APP_PASSWORD",
                "OUTREACH_SENDER_PHONE", "OUTREACH_NOTIFY_EMAIL", "LEADGEN_ENABLED", "OUTREACH_ENABLED", "OUTREACH_LIVE",
                "AUTO_UPDATE", "GOOGLE_PLACES_API_KEY", "FOURSQUARE_API_KEY", "BRAVE_API_KEY"]
SECRET_KEYS = ("OUTREACH_GMAIL_APP_PASSWORD", "GOOGLE_PLACES_API_KEY", "FOURSQUARE_API_KEY", "BRAVE_API_KEY")
SWITCH_DEFAULTS = {"LEADGEN_ENABLED": "true", "OUTREACH_ENABLED": "true", "OUTREACH_LIVE": "true", "AUTO_UPDATE": "true"}
PASSED_TO_JOBS = ("PP_SHEET_ID", "OUTREACH_GMAIL_ADDRESS", "OUTREACH_GMAIL_APP_PASSWORD", "OUTREACH_SENDER_PHONE",
                  "OUTREACH_NOTIFY_EMAIL", "GOOGLE_PLACES_API_KEY", "FOURSQUARE_API_KEY", "BRAVE_API_KEY")


# ------------------------------------------------------------------ places
def home() -> Path:
    return Path(os.environ.get("PP_HOME") or (Path.home() / ".plant-parlour"))


def paths(base: Path | None = None) -> dict[str, Path]:
    h = base or home()
    return {"home": h, "settings": h / "settings.env", "key": h / "service-account.json", "state": h / "state",
            "db": h / "state" / "leadgen.sqlite", "outreach_db": h / "state" / "outreach.sqlite", "logs": h / "logs",
            "backups": h / "backups", "status": h / "status.json", "update": h / "update" / "update.json",
            "lock": h / "scheduler.lock", "job_lock": h / "job.lock", "pid": h / "scheduler.pid",
            "reports": h / "state" / "reports"}


class JobLock:
    """One job at a time: the scheduler waits for a job started by hand, a job by hand refuses while one runs."""

    def __init__(self, p: dict[str, Path]):
        self.path = p["job_lock"]
        self.fh = None

    def acquire(self, wait: bool) -> bool:
        import fcntl

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self.fh.close()
            self.fh = None
            return False

    def release(self) -> None:
        if self.fh is not None:
            try:
                import fcntl

                fcntl.flock(self.fh, fcntl.LOCK_UN)
            finally:
                self.fh.close()
                self.fh = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def busy_message() -> str:
    return "Another job is running right now. Try again when it has finished (pp status shows it)."


def now_local(ts: float | None = None) -> datetime:
    return datetime.fromtimestamp(time.time() if ts is None else ts, TZ)


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "never"
    return now_local(ts).strftime("%a %d %b %H:%M")


# ------------------------------------------------------------------ settings
def truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def read_settings(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        if re.fullmatch(r"[A-Z][A-Z0-9_]*", k):
            out[k] = v
    return out


def write_settings(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Plant Parlour settings - this file stays on this tablet. Never share it or put it on GitHub.",
             "# Change values with:  pp settings   (or edit this file; the scheduler reads it before every job)", ""]
    for k in SETTING_KEYS + sorted(k for k in values if k not in SETTING_KEYS):
        if k in values and values[k] is not None:
            v = str(values[k]).replace("\n", " ").strip()
            lines.append(f"{k}={v}")
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def effective_settings(p: dict[str, Path]) -> dict[str, str]:
    s = {**SWITCH_DEFAULTS, **read_settings(p["settings"])}
    return s


def job_env(p: dict[str, Path], settings: dict[str, str], base_env: dict | None = None) -> dict[str, str]:
    """Environment for the leadgen commands: the settings become the variables GitHub used to pass as secrets."""
    src = os.environ if base_env is None else base_env
    env = {k: v for k, v in src.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "SSL_CERT_FILE",
                                                  "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY",
                                                  "https_proxy", "http_proxy", "no_proxy")}
    env.update({"PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1", "PP_DB": str(p["db"]), "PP_OUTREACH_DB": str(p["outreach_db"]),
                "PP_HOME": str(p["home"])})
    for k in PASSED_TO_JOBS:
        if settings.get(k):
            env[k] = settings[k]
    key = settings.get("GOOGLE_SERVICE_ACCOUNT_FILE") or ""
    if key:
        kp = Path(key)
        env["GOOGLE_SERVICE_ACCOUNT_FILE"] = str(kp if kp.is_absolute() else p["home"] / kp)
    elif p["key"].exists():
        env["GOOGLE_SERVICE_ACCOUNT_FILE"] = str(p["key"])
    env["OUTREACH_LIVE"] = "true" if truthy(settings.get("OUTREACH_LIVE")) else ""
    return env


# ------------------------------------------------------------------ status file
def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {} if default is None else default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------------------------------------------ jobs
@dataclass
class Job:
    name: str
    title: str
    times: list[str]
    steps: list[list[str]] = field(default_factory=list)
    weekdays: tuple[int, ...] | None = None          # 0 = Monday; None = every day
    switch: str | None = None                        # settings key that turns the job on/off
    timeout_min: float = 60
    covers: tuple[str, ...] = ()                     # running this job also counts as running these

    def slots(self, day: datetime) -> list[datetime]:
        if self.weekdays is not None and day.weekday() not in self.weekdays:
            return []
        out = []
        for t in self.times:
            h, m = (int(x) for x in t.split(":"))
            out.append(day.replace(hour=h, minute=m, second=0, microsecond=0))
        return sorted(out)


OUTREACH_TIMES = [f"{h:02d}:11" for h in range(10, 19)]
JOBS: dict[str, Job] = {j.name: j for j in [
    Job("update", "Update", ["05:15"], switch="AUTO_UPDATE", timeout_min=30),
    Job("daily", "Lead generation", ["06:00"],
        [["run", "--budget-minutes", "120"],
         ["email-hunt", "--limit", "1000", "--budget-minutes", "40"],
         ["usp-refresh", "--limit", "600", "--budget-minutes", "20"]],
        switch="LEADGEN_ENABLED", timeout_min=200, covers=("followup",)),
    Job("followup", "Lead follow-up", ["11:30", "16:30", "21:30"],
        [["run", "--budget-minutes", "45"],
         ["email-hunt", "--limit", "600", "--budget-minutes", "20"],
         ["usp-refresh", "--limit", "400", "--budget-minutes", "10"]],
        switch="LEADGEN_ENABLED", timeout_min=100),
    Job("outreach", "Outreach", OUTREACH_TIMES, [["outreach"]], weekdays=(0, 1, 2, 3, 4, 5), switch="OUTREACH_ENABLED",
        timeout_min=55),
    Job("backup", "Backup", ["23:30"], timeout_min=20),
]}
# When several jobs are due, this order decides (an update first, so the others run the new code).
PRIORITY = ["update", "outreach", "daily", "followup", "backup"]


def due_slot(job: Job, now: datetime, last_started: float | None) -> datetime | None:
    """The latest of today's slots that has passed and that the job has not started since, else None."""
    passed = [s for s in job.slots(now) if s <= now]
    if not passed:
        return None
    latest = passed[-1]
    if not last_started or last_started < latest.timestamp():
        return latest
    return None


def next_slot(job: Job, now: datetime) -> datetime | None:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(8):
        for s in job.slots(day + timedelta(days=d)):
            if s > now:
                return s
    return None


def job_enabled(job: Job, settings: dict[str, str]) -> bool:
    return job.switch is None or truthy(settings.get(job.switch))


def pick_due(now: datetime, status: dict, settings: dict[str, str]) -> Job | None:
    jobs_state = status.get("jobs", {})
    for name in PRIORITY:
        job = JOBS[name]
        if not job_enabled(job, settings):
            continue
        if due_slot(job, now, (jobs_state.get(name) or {}).get("last_started")):
            return job
    return None


# ------------------------------------------------------------------ logging
class Log:
    def __init__(self, p: dict[str, Path], stream=None):
        self.p = p
        self.stream = stream

    def path(self) -> Path:
        self.p["logs"].mkdir(parents=True, exist_ok=True)
        return self.p["logs"] / f"{now_local().strftime('%Y-%m-%d')}.log"

    def __call__(self, msg: str) -> None:
        line = f"{now_local().strftime('%H:%M:%S')} [tablet] {msg}"
        with open(self.path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if self.stream is not None:
            print(line, file=self.stream, flush=True)


# ------------------------------------------------------------------ running a job
def _stop_group(proc: subprocess.Popen, grace: float = 120) -> None:
    for sig, wait_s in ((signal.SIGTERM, grace), (signal.SIGKILL, 10)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=wait_s)
            return
        except subprocess.TimeoutExpired:
            continue


def run_command(args: list[str], *, cwd: Path, env: dict, log_path: Path, timeout_s: float,
                heartbeat=None, stop_flag=None) -> int:
    """Run one command, its output appended to the day's log. Returns its exit code (124 = timed out)."""
    with open(log_path, "a", encoding="utf-8") as out:
        proc = subprocess.Popen(args, cwd=str(cwd), env=env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        end = time.time() + timeout_s
        while True:
            try:
                return proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                if heartbeat:
                    heartbeat()
                if (stop_flag and stop_flag()) or time.time() > end:
                    _stop_group(proc)
                    return 124 if time.time() > end else 143


class Tablet:
    """The scheduler and the jobs it runs."""

    def __init__(self, p: dict[str, Path] | None = None, *, python: str | None = None, runner=None, now_fn=time.time,
                 stream=None):
        self.p = p or paths()
        self.python = python or sys.executable
        self.runner = runner or run_command
        self.now = now_fn
        self.log = Log(self.p, stream)
        self.stop = False
        self.current: str | None = None

    # -- status --
    def status(self) -> dict:
        return load_json(self.p["status"], {"jobs": {}})

    def _save_status(self, st: dict) -> None:
        save_json(self.p["status"], st)

    def change_status(self, fn) -> dict:
        """Read-change-write the status file under a lock (the scheduler and a job started by hand both write it)."""
        import fcntl

        self.p["home"].mkdir(parents=True, exist_ok=True)
        with open(self.p["home"] / "status.lock", "w") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            st = self.status()
            fn(st)
            self._save_status(st)
            return st

    def heartbeat(self) -> None:
        def beat(st):
            st.setdefault("scheduler", {})["heartbeat"] = self.now()
            st["scheduler"]["running_job"] = self.current
        self.change_status(beat)

    # -- jobs --
    def run_job(self, job: Job, *, manual: bool = False) -> int:
        lock = JobLock(self.p)
        if not lock.acquire(wait=not manual):
            print(busy_message())
            return BUSY
        try:
            return self._run_job(job, manual)
        finally:
            lock.release()

    def _run_job(self, job: Job, manual: bool) -> int:
        settings = effective_settings(self.p)
        started = self.now()

        def begin(st):
            st.setdefault("jobs", {}).setdefault(job.name, {}).update(last_started=started, running=True)
            for other in job.covers:
                st["jobs"].setdefault(other, {})["last_started"] = started
        self.current = job.name
        self.change_status(begin)
        self.log(f"start: {job.title}{' (started by hand)' if manual else ''}")
        try:
            if job.name == "backup":
                code = self.backup()
            elif job.name == "update":
                code = update(self.p, python=self.python, log=self.log, runner=self.runner)
            else:
                code = self._run_steps(job, settings)
        except Exception as exc:  # noqa: BLE001 - the scheduler keeps going whatever one job does
            self.log(f"{job.title} crashed: {type(exc).__name__}: {exc}")
            code = 2
        finally:
            self.current = None
        ok = code in (0, RESTART)

        def finish(st):
            js = st.setdefault("jobs", {}).setdefault(job.name, {})
            js.update(running=False, last_finished=self.now(), last_code=code,
                      minutes=round((self.now() - started) / 60, 1),
                      fails_in_row=0 if ok else int(js.get("fails_in_row", 0)) + 1)
            if ok:
                js["last_ok"] = self.now()
        js = self.change_status(finish)["jobs"][job.name]
        self.log(f"end: {job.title} - {'OK' if ok else f'problem (exit code {code})'} after {js['minutes']} min")
        if not ok:
            self._maybe_alert(job, code, js, settings)
        return code

    def _run_steps(self, job: Job, settings: dict[str, str]) -> int:
        env = job_env(self.p, settings)
        self.p["state"].mkdir(parents=True, exist_ok=True)
        worst = 0
        end = self.now() + job.timeout_min * 60
        for step in job.steps:
            left = end - self.now()
            if left <= 60 or self.stop:
                self.log(f"  skipped {step[0]} (job time limit reached)" if left <= 60 else f"  skipped {step[0]} (stopping)")
                break
            self.log(f"  {' '.join(step)}")
            code = self.runner([self.python, "-m", "leadgen", *step], cwd=REPO, env=env, log_path=self.log.path(),
                               timeout_s=left, heartbeat=self.heartbeat, stop_flag=lambda: self.stop)
            if code:
                self.log(f"  {step[0]} ended with exit code {code}")
            worst = max(worst, code)
        return worst

    def _maybe_alert(self, job: Job, code: int, js: dict, settings: dict[str, str]) -> None:
        # One failure can be a passing network problem: alert from the second in a row (config errors at once).
        if code != 1 and js.get("fails_in_row", 0) < 2:
            return
        if job.name == "update" and code == 4:
            return
        key = f"{job.name}:{now_local(self.now()).date().isoformat()}"
        if key in self.status().get("alerts", {}):
            return
        tail = log_tail(self.log.path(), 40)
        body = (f"The tablet's '{job.title}' job had a problem (exit code {code}, {js.get('fails_in_row')} time(s) in a row).\n"
                f"It will try again at its next time. On the tablet, `pp status` and `pp logs` show more.\n\n"
                f"Last lines of today's log:\n\n{tail}\n")
        if send_alert(settings, f"{job.title} needs a look", body):
            self.change_status(lambda st: st.setdefault("alerts", {}).__setitem__(key, self.now()))

    # -- backup --
    def backup(self) -> int:
        day = now_local(self.now()).strftime("%Y-%m-%d")
        dest = self.p["backups"] / day
        dest.mkdir(parents=True, exist_ok=True)
        code = 0
        for db in (self.p["db"], self.p["outreach_db"]):
            if not db.exists():
                continue
            try:
                sqlite_backup(db, dest / db.name)
                self.log(f"  backup: {db.name} saved ({(dest / db.name).stat().st_size // 1024} KB)")
            except Exception as exc:  # noqa: BLE001
                self.log(f"  backup of {db.name} FAILED: {exc}")
                code = 2
        prune_dirs(self.p["backups"], keep=7)
        prune_files(self.p["logs"], "*.log", keep_days=30, now=self.now())
        return code

    # -- the loop --
    def serve(self, *, max_loops: int | None = None, sleep_fn=time.sleep) -> int:
        self.p["home"].mkdir(parents=True, exist_ok=True)
        lock = open(self.p["lock"], "w")
        try:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:  # pragma: no cover
            pass
        except OSError:
            print("The scheduler is already running.")
            return BUSY

        def on_signal(signum, _frame):
            self.stop = True
        for sig in (signal.SIGTERM, signal.SIGINT):     # not SIGHUP: closing a terminal must not stop it
            try:
                signal.signal(sig, on_signal)
            except (ValueError, OSError):
                pass
        self.p["pid"].write_text(str(os.getpid()), encoding="utf-8")   # `pp stop` asks this process to finish
        version = git_head_short()

        def started(st):
            st["scheduler"] = {"pid": os.getpid(), "started": self.now(), "heartbeat": self.now(), "version": version}
            for js in st.get("jobs", {}).values():
                js["running"] = False         # a job cut off by a restart is not running any more
        self.change_status(started)
        self.log(f"scheduler started (version {version})")
        loops = 0
        # The first update check after a (re)start happens when the last one is more than a day old.
        last_check = (load_json(self.p["update"]).get("last_check") or 0)
        try:
            while not self.stop:
                loops += 1
                settings = effective_settings(self.p)
                now = now_local(self.now())
                job = pick_due(now, self.status(), settings)
                if job is None and truthy(settings.get("AUTO_UPDATE")) and self.now() - last_check > 26 * 3600:
                    job = JOBS["update"]
                if job is not None:
                    code = self.run_job(job)
                    if job.name == "update":
                        last_check = self.now()
                        if code == RESTART:
                            self.log("restarting with the new version")
                            return RESTART
                else:
                    self.heartbeat()
                    if max_loops is not None and loops >= max_loops:
                        break
                    for _ in range(int(max(1.0, 30 - (self.now() % 30)))):
                        if self.stop:
                            break
                        sleep_fn(1)
                if max_loops is not None and loops >= max_loops:
                    break
        finally:
            self.log("scheduler stopped")
            try:
                self.p["pid"].unlink()
            except OSError:
                pass
            try:
                lock.close()
            except OSError:
                pass
        return 0


# ------------------------------------------------------------------ helpers
def log_tail(path: Path, n: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return "(no log yet)"
    return "\n".join(lines[-n:])


def send_alert(settings: dict[str, str], subject: str, body: str) -> bool:
    """A short e-mail to the owner through the outreach Gmail (when it is set up)."""
    addr = settings.get("OUTREACH_GMAIL_ADDRESS", "").strip()
    pw = settings.get("OUTREACH_GMAIL_APP_PASSWORD", "").strip()
    if not (addr and pw):
        return False
    to = settings.get("OUTREACH_NOTIFY_EMAIL", "").strip() or addr
    from email.message import EmailMessage

    from .outreach.mailer import GmailSender

    msg = EmailMessage()
    msg["From"] = f"Plant Parlour tablet <{addr}>"
    msg["To"] = to
    msg["Subject"] = f"[Plant Parlour tablet] {subject}"
    msg.set_content(body)
    try:
        res = GmailSender(addr, pw).send(msg)
        return bool(res.ok)
    except Exception:  # noqa: BLE001 - an alert must never break the scheduler
        return False


def sqlite_backup(src: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=60)
    try:
        out = sqlite3.connect(str(tmp))
        try:
            con.backup(out)
            if out.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("integrity check failed on the copy")
        finally:
            out.close()
    finally:
        con.close()
    os.replace(tmp, dest)


def prune_dirs(base: Path, keep: int) -> None:
    if not base.exists():
        return
    dated = sorted(d for d in base.iterdir() if d.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.name))
    for d in dated[:-keep] if keep else dated:
        shutil.rmtree(d, ignore_errors=True)


def prune_files(base: Path, pattern: str, keep_days: int, now: float) -> None:
    if not base.exists():
        return
    for f in base.glob(pattern):
        try:
            if now - f.stat().st_mtime > keep_days * 86400:
                f.unlink()
        except OSError:
            pass


def git(*args: str, check: bool = True, timeout: float = 300) -> str:
    r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout.strip()


def git_head_short() -> str:
    try:
        return git("rev-parse", "--short", "HEAD", timeout=30)
    except Exception:  # noqa: BLE001
        return "unknown"


def requirements_hash() -> str:
    h = hashlib.sha256()
    for name in ("requirements.txt",):
        f = REPO / name
        if f.exists():
            h.update(f.read_bytes())
    h.update(" ".join(EXTRA_PACKAGES).encode())
    return h.hexdigest()[:16]


# ------------------------------------------------------------------ update / rollback
def remote_default_branch() -> str:
    """The branch GitHub shows first (the repository's default) - the tablet follows it."""
    try:
        out = git("ls-remote", "--symref", "origin", "HEAD", timeout=60)
    except Exception:  # noqa: BLE001
        return ""
    m = re.search(r"ref:\s+refs/heads/(\S+)\s+HEAD", out)
    return m.group(1) if m else ""


def pip_install(python: str, log) -> bool:
    cmd = [python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--prefer-binary",
           "-r", str(REPO / "requirements.txt"), *EXTRA_PACKAGES]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        log(f"  pip install failed: {(r.stderr or r.stdout).strip()[-500:]}")
        return False
    return True


def run_checks(python: str, log, runner=None) -> tuple[bool, set[str]]:
    """Is this version safe to run? It must compile, its config must load, and the test suite must not show new
    failures. Returns (basic checks passed, names of failing tests)."""
    env = {**os.environ, "PYTHONUTF8": "1"}
    for args, what in (([python, "-m", "compileall", "-q", "leadgen"], "the code compiles"),
                       ([python, "-c", "from leadgen.config import load_config; load_config('config/plant-parlour.toml')"],
                        "the config loads")):
        r = subprocess.run(args, cwd=str(REPO), capture_output=True, text=True, timeout=600, env=env)
        if r.returncode != 0:
            log(f"  check failed - {what}: {(r.stderr or r.stdout).strip()[-400:]}")
            return False, set()
    if not (REPO / "tests").is_dir():
        return True, set()
    with tempfile.TemporaryDirectory() as td:
        xml = Path(td) / "junit.xml"
        r = subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml}", "tests"],
                           cwd=str(REPO), capture_output=True, text=True, timeout=1800, env=env)
        if not xml.exists():
            out = (r.stderr or r.stdout).strip()
            if "No module named pytest" in out:
                log("  tests skipped (pytest not installed)")
                return True, set()
            log(f"  tests could not run: {out[-400:]}")
            return False, set()
        failures = junit_failures(xml.read_text(encoding="utf-8", errors="replace"))
    log(f"  tests: {'all passed' if not failures else f'{len(failures)} failing'}")
    return True, failures


def junit_failures(xml_text: str) -> set[str]:
    import xml.etree.ElementTree as ET

    out = set()
    root = ET.fromstring(xml_text)
    for tc in root.iter("testcase"):
        if tc.find("failure") is not None or tc.find("error") is not None:
            out.add(f"{tc.get('classname', '')}::{tc.get('name', '')}")
    return out


def bad_commits(rec: dict) -> list[str]:
    """Versions that failed their checks (or were undone by hand): automatic updates never install them again."""
    out = list(rec.get("bad_commits") or [])
    if rec.get("bad_commit"):                  # older records kept one
        out.append(rec["bad_commit"])
    return out


def add_bad(rec: dict, commit: str) -> list[str]:
    out = [c for c in bad_commits(rec) if c != commit] + [commit]
    rec.pop("bad_commit", None)
    return out[-30:]


def update(p: dict[str, Path], *, python: str | None = None, log=print, discard_local: bool = False,
           runner=None, check_only: bool = False) -> int:
    """Fetch the newest version of the repository's default branch, install it, test it.
    0 = nothing new (or not updated), RESTART = updated (restart to use it), 3 = can't update now, 4 = rolled back."""
    python = python or sys.executable
    rec = load_json(p["update"])
    rec["last_check"] = time.time()
    save_json(p["update"], rec)
    branch = remote_default_branch() or git("rev-parse", "--abbrev-ref", "HEAD")
    try:
        git("fetch", "--quiet", "--prune", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}", timeout=600)
    except Exception as exc:  # noqa: BLE001
        log(f"  no update check possible (no internet or GitHub unreachable): {exc}")
        rec["last_result"] = "could not reach GitHub"
        save_json(p["update"], rec)
        return 3
    old = git("rev-parse", "HEAD")
    new = git("rev-parse", f"origin/{branch}")
    if old == new:
        rec["last_result"] = "up to date"
        save_json(p["update"], rec)
        log(f"  up to date ({old[:7]})")
        return 0
    if new in bad_commits(rec):
        rec["last_result"] = f"waiting for a fixed version ({new[:7]} failed its checks)"
        save_json(p["update"], rec)
        log(f"  {new[:7]} failed its checks earlier - waiting for a newer version")
        return 0
    changes = git("log", "--format=%h %s", f"{old}..{new}", check=False)
    if check_only:
        log("  new version available:\n" + "\n".join("    " + c for c in changes.splitlines()[:20]))
        return 0
    dirty = git("status", "--porcelain", "--untracked-files=no")
    if dirty:
        if not discard_local:
            rec["last_result"] = "not updated: files were changed on the tablet (pp update --discard-local)"
            save_json(p["update"], rec)
            log("  NOT updated: some files of the code folder were changed on the tablet:\n" + dirty +
                "\n  Run `pp update --discard-local` to keep a copy of the changes and update anyway.")
            return 3
        patch = p["home"] / "update" / f"local-changes-{now_local().strftime('%Y%m%d-%H%M%S')}.patch"
        patch.write_text(git("diff"), encoding="utf-8")
        log(f"  local changes saved to {patch}")
    req_before = requirements_hash()
    log(f"  updating {old[:7]} -> {new[:7]}:\n" + "\n".join("    " + c for c in changes.splitlines()[:20]))
    git("checkout", "--quiet", "--force", "-B", branch, f"origin/{branch}")
    req_changed = requirements_hash() != req_before
    ok = True
    if req_changed:
        log("  new Python packages needed - installing")
        ok = pip_install(python, log)
    failures: set[str] = set()
    if ok:
        ok, failures = run_checks(python, log, runner)
    baseline = set(rec.get("baseline_failures") or [])
    new_fail = failures - baseline
    if not ok or new_fail:
        why = "checks failed" if not ok else f"new test failures: {', '.join(sorted(new_fail)[:5])}"
        log(f"  the new version is NOT safe ({why}) - going back to {old[:7]}")
        git("checkout", "--quiet", "--force", "-B", branch, old)
        if req_changed:
            pip_install(python, log)
        rec.update(bad_commits=add_bad(rec, new), last_result=f"rolled back: {why}"[:300])
        save_json(p["update"], rec)
        send_alert(effective_settings(p), "an update was undone",
                   f"The tablet tried version {new[:7]} and went back to {old[:7]} because {why}.\n"
                   "Nothing else changed; the system keeps running the previous version.")
        return 4
    rec.update(previous=old, current=new, updated_at=time.time(), baseline_failures=sorted(failures),
               last_result=f"updated {old[:7]} -> {new[:7]}", changes=changes.splitlines()[:50])
    save_json(p["update"], rec)
    log(f"  updated to {new[:7]}")
    return RESTART


def rollback(p: dict[str, Path], *, python: str | None = None, log=print) -> int:
    python = python or sys.executable
    rec = load_json(p["update"])
    prev = rec.get("previous")
    if not prev:
        log("There is no earlier version to go back to.")
        return 3
    branch = git("rev-parse", "--abbrev-ref", "HEAD")
    cur = git("rev-parse", "HEAD")
    req_before = requirements_hash()
    git("checkout", "--quiet", "--force", "-B", branch, prev)
    if requirements_hash() != req_before:
        pip_install(python, log)
    # Don't update straight back to the version just left; a newer one is taken as usual.
    rec.update(bad_commits=add_bad(rec, cur), current=prev, previous="", last_result=f"rolled back by hand to {prev[:7]}")
    save_json(p["update"], rec)
    log(f"Back on version {prev[:7]}. Automatic updates skip {cur[:7]} and take the next newer version.")
    return RESTART


# ------------------------------------------------------------------ GitHub memory import
def decrypt_openssl(data: bytes, passphrase: str, iterations: int = 200000) -> bytes:
    """Decrypt `openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -md sha256 -salt` output (scripts/ci/state.sh)."""
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    if not data.startswith(b"Salted__") or len(data) < 32:
        raise ValueError("this is not a file saved by the GitHub workflow (no OpenSSL header)")
    salt = data[8:16]
    kiv = hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, iterations, 48)
    dec = Cipher(algorithms.AES(kiv[:32]), modes.CBC(kiv[32:])).decryptor()
    padded = dec.update(data[16:]) + dec.finalize()
    unpad = padding.PKCS7(128).unpadder()
    try:
        return unpad.update(padded) + unpad.finalize()
    except ValueError as exc:
        raise ValueError("wrong PP_STATE_KEY (the file could not be decrypted)") from exc


def read_encrypted(path: Path) -> bytes:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.endswith(".enc")]
            if not names:
                raise ValueError(f"{path.name} holds no saved state (.enc file)")
            return z.read(names[0])
    return path.read_bytes()


def db_kind(path: Path) -> str:
    con = sqlite3.connect(str(path))
    try:
        if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return "damaged"
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        con.close()
    if {"places", "tasks", "parts"} <= tables:
        return "leadgen"
    if {"sends", "wa"} <= tables:
        return "outreach"
    return "unknown"


def import_state(p: dict[str, Path], files: list[Path], key: str, log=print) -> dict[str, str]:
    """Decrypt the memory GitHub saved and put it where the tablet keeps it. Returns {kind: source file}."""
    done: dict[str, str] = {}
    p["state"].mkdir(parents=True, exist_ok=True)
    for f in files:
        raw = read_encrypted(f)
        plain = None
        last_err = None
        for k in dict.fromkeys([key, key.strip(), key.strip().strip("'\"")]):
            try:
                plain = gzip.decompress(decrypt_openssl(raw, k))
                break
            except (ValueError, OSError) as exc:
                last_err = exc
        if plain is None:
            raise ValueError(f"{f.name}: {last_err}")
        with tempfile.NamedTemporaryFile(dir=p["state"], suffix=".sqlite", delete=False) as tmp:
            tmp.write(plain)
            tmp_path = Path(tmp.name)
        kind = db_kind(tmp_path)
        if kind not in ("leadgen", "outreach"):
            tmp_path.unlink()
            raise ValueError(f"{f.name}: the decrypted file is not a campaign memory ({kind})")
        target = p["db"] if kind == "leadgen" else p["outreach_db"]
        if target.exists():
            keep = p["backups"] / f"before-import-{now_local().strftime('%Y%m%d-%H%M%S')}"
            keep.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, keep / target.name)
            log(f"  the tablet's earlier {target.name} was kept in {keep}")
        for suffix in ("-wal", "-shm"):
            stale = Path(str(target) + suffix)
            if stale.exists():
                stale.unlink()
        os.replace(tmp_path, target)
        done[kind] = f.name
        log(f"  {f.name}: {'lead generation' if kind == 'leadgen' else 'outreach'} memory imported "
            f"({target.stat().st_size // 1024} KB)")
    return done


def download_dirs() -> list[Path]:
    h = Path(os.environ.get("TERMUX_HOME_DIR") or Path.home())
    cands = [h / "storage" / "downloads", Path("/sdcard/Download"), Path("/storage/emulated/0/Download")]
    out, seen = [], set()
    for c in cands:
        try:
            r = c.resolve()
            if r.is_dir() and r not in seen:
                seen.add(r)
                out.append(c)
        except OSError:
            continue
    return out


def find_downloads(pattern: str) -> list[Path]:
    found: dict[Path, Path] = {}
    for d in download_dirs():
        try:
            for f in d.glob(pattern):
                if f.is_file():
                    found.setdefault(f.resolve(), f)
        except OSError:
            continue
    return sorted(found.values(), key=lambda f: f.stat().st_mtime, reverse=True)


def is_service_account_key(path: Path) -> str:
    """The service account's e-mail when this file is a Google service-account key, else ''."""
    try:
        if path.stat().st_size > 20000:
            return ""
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return ""
    if isinstance(info, dict) and info.get("type") == "service_account" and info.get("private_key") and info.get("client_email"):
        return str(info["client_email"])
    return ""


# ------------------------------------------------------------------ checks shown to the owner
def check_sheet(env: dict) -> tuple[bool, str]:
    """One quick, read-only look at the sheet's title, with a plain-language reason when it fails."""
    sid = env.get("PP_SHEET_ID", "")
    if not sid:
        return False, "no Google Sheet ID yet"
    key = env.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    who = is_service_account_key(Path(key)) if key else ""
    if not who:
        return False, "no valid Google key file yet (pp settings, step 2)"
    try:
        from google.auth.exceptions import GoogleAuthError
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account

        from .sheets import API, SCOPES

        creds = service_account.Credentials.from_service_account_file(key, scopes=SCOPES)
        r = AuthorizedSession(creds).get(f"{API}/{sid}", params={"fields": "properties.title"}, timeout=40)
    except GoogleAuthError as exc:
        return False, f"Google did not accept the key file ({exc}) - download a new key (docs/TABLET.md, step 3)"
    except ValueError as exc:
        return False, f"the key file is damaged ({exc}) - download it again"
    except Exception as exc:  # noqa: BLE001 - network
        return False, f"Google could not be reached (internet?): {type(exc).__name__}: {exc}"
    if r.status_code == 200:
        return True, f"Google Sheet OK: \"{r.json().get('properties', {}).get('title', '?')}\""
    if r.status_code == 403:
        return False, f"the sheet is not shared with {who} - share it with that address as Editor"
    if r.status_code == 404:
        return False, "no sheet with this ID - copy the sheet's address again"
    return False, f"Google Sheets answered HTTP {r.status_code}: {r.text[:200]}"


def check_gmail(settings: dict[str, str]) -> tuple[bool, str]:
    addr = settings.get("OUTREACH_GMAIL_ADDRESS", "").strip()
    pw = settings.get("OUTREACH_GMAIL_APP_PASSWORD", "").strip()
    if not (addr and pw):
        return False, "Gmail not set up yet (outreach e-mails and alerts need it)"
    from .outreach.mailer import GmailSender

    problem = GmailSender(addr, pw).check()
    return (False, f"Gmail login FAILED - {problem}") if problem else (True, f"Gmail login OK ({addr})")


# ------------------------------------------------------------------ status text
def status_text(p: dict[str, Path], now_ts: float | None = None) -> str:
    now_ts = time.time() if now_ts is None else now_ts
    now = now_local(now_ts)
    st = load_json(p["status"], {"jobs": {}})
    rec = load_json(p["update"])
    settings = effective_settings(p)
    sch = st.get("scheduler") or {}
    hb = sch.get("heartbeat") or 0
    alive = now_ts - hb < 180
    lines = ["Plant Parlour on this tablet", ""]
    if alive:
        running = sch.get("running_job")
        lines.append(f"Scheduler: RUNNING since {fmt_ts(sch.get('started'))}" +
                     (f" - working on: {JOBS[running].title}" if running in JOBS else " - waiting for the next job"))
    else:
        lines.append(f"Scheduler: NOT RUNNING (last seen {fmt_ts(hb)}). Start it with:  pp start")
    lines.append(f"Version: {git_head_short()}  |  automatic updates: {'on' if truthy(settings.get('AUTO_UPDATE')) else 'OFF'}"
                 f"  |  last update check: {fmt_ts(rec.get('last_check'))} ({rec.get('last_result', '-')})")
    rep = load_json(p["reports"] / f"report-{now.date().isoformat()}.json")
    if rep:
        lines += ["", f"Today ({now.strftime('%a %d %b')}): {rep.get('new_leads_today', 0)} new leads "
                      f"(target {rep.get('target', '?')}), {rep.get('leads_total', '?')} leads in total, last run: {rep.get('status', '?')}"]
    lines += ["", f"{'Job':<17}{'Last run':<34}{'Next':<16}"]
    for name in ("daily", "followup", "outreach", "backup", "update"):
        job = JOBS[name]
        js = st.get("jobs", {}).get(name) or {}
        if name == "update" and rec.get("last_check") and not (js.get("running") and alive):
            res = str(rec.get("last_result") or "")
            word = ("OK, new version" if res.startswith("updated") else "OK" if res.startswith(("up to date", "waiting"))
                    else "UNDONE (pp logs)" if res.startswith("rolled back") else "no internet" if "reach" in res
                    else "BLOCKED (pp logs)" if res.startswith("not updated") else res[:16])
            last = f"{fmt_ts(rec.get('last_check'))} {word}"
        elif js.get("running") and alive:
            last = f"running since {fmt_ts(js.get('last_started'))}"
        elif js.get("running"):
            last = f"{fmt_ts(js.get('last_started'))} interrupted"
        elif js.get("last_finished"):
            code = js.get("last_code")
            last = f"{fmt_ts(js.get('last_started'))} {'OK' if code in (0, RESTART) else f'PROBLEM ({code})'}"
        else:
            last = "never"
        if not job_enabled(job, settings):
            nxt = f"off ({job.switch})"
        elif due_slot(job, now, js.get("last_started")):
            nxt = "due now"
        else:
            ns = next_slot(job, now)
            nxt = ns.strftime("%a %H:%M") if ns else "-"
        lines.append(f"{job.title:<17}{last:<34}{nxt:<16}")
    lines += ["", "Switches: lead generation " + ("ON" if truthy(settings.get("LEADGEN_ENABLED")) else "OFF") +
              " | outreach " + ("ON" if truthy(settings.get("OUTREACH_ENABLED")) else "OFF") +
              (" (live)" if truthy(settings.get("OUTREACH_LIVE")) else " (dry-run: nothing is sent)")]
    if not p["db"].exists():
        lines.append("Campaign memory: not imported yet - the lead generation waits for it (pp import).")
    return "\n".join(lines)


# ------------------------------------------------------------------ setup wizard
def _ask(prompt: str, current: str = "", secret: bool = False, show_current: bool = True) -> str:
    import getpass

    hint = ""
    if current:
        hint = f" [Enter = keep {mask(current) if secret else current}]" if show_current else " [Enter = keep]"
    while True:
        try:
            raw = getpass.getpass(f"{prompt}{hint}: ") if secret else input(f"{prompt}{hint}: ")
        except EOFError:
            raise SystemExit(NO_INPUT) from None
        raw = raw.strip()
        if raw or current:
            return raw or current
        print("  (needed - please type it, or press Ctrl+C to stop and come back later)", flush=True)


def _yes(prompt: str, default: bool = True) -> bool:
    try:
        raw = input(f"{prompt} [{'Y/n' if default else 'y/N'}]: ").strip().lower()
    except EOFError:
        raise SystemExit(NO_INPUT) from None
    return default if not raw else raw in ("y", "yes")


NO_INPUT = "\nStopped (no more input). What you entered so far is saved; run `pp settings` to continue."


def mask(v: str) -> str:
    v = v or ""
    return (v[:2] + "*" * max(3, len(v) - 4) + v[-2:]) if len(v) > 6 else "***"


def sheet_id_from(text: str) -> str:
    text = text.strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})", text)
    if m:
        return m.group(1)
    return text if re.fullmatch(r"[A-Za-z0-9_-]{20,}", text) else ""


def email_ok(text: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[A-Za-z]{2,}", (text or "").strip()))


def app_password_ok(pw: str) -> bool:
    return bool(re.fullmatch(r"[a-z]{16}", pw.replace(" ", "").lower()))


def wizard(p: dict[str, Path]) -> int:
    print("\n=== Plant Parlour - settings ===\n"
          "Everything you type stays on this tablet (in ~/.plant-parlour/settings.env).\n"
          "Values already set are kept when you just press Enter. Ctrl+C stops; what you entered so far is saved.\n")
    s = {**SWITCH_DEFAULTS, **read_settings(p["settings"])}

    def save():
        write_settings(p["settings"], s)

    # 1. Google Sheet
    print("1) Your Google Sheet. Open it in the browser and copy its address (or just the long ID in it).")
    while True:
        sid = sheet_id_from(_ask("   Sheet address or ID", s.get("PP_SHEET_ID", "")))
        if sid:
            s["PP_SHEET_ID"] = sid
            break
        print("   That doesn't look like a Google Sheet address. It contains /spreadsheets/d/<long ID>/ .")
    save()

    # 2. Google key
    print("\n2) The Google key file (a .json file of the service account, e.g. lead-sync-bot@...).")
    have_key = bool(p["key"].exists() and is_service_account_key(p["key"]))
    keys = [f for f in find_downloads("*.json") if is_service_account_key(f)]
    if keys:
        f = keys[0]
        if len(keys) > 1:
            print("   Several key files are in Downloads:")
            for i, k in enumerate(keys, 1):
                print(f"     {i}. {k.name}  ({is_service_account_key(k)})")
            pick = input("   Which one? [1]: ").strip() or "1"
            f = keys[int(pick) - 1] if pick.isdigit() and 1 <= int(pick) <= len(keys) else keys[0]
        if not have_key or _yes(f"   Use {f.name} ({is_service_account_key(f)}) from Downloads?"):
            p["home"].mkdir(parents=True, exist_ok=True)
            tmp = p["key"].with_suffix(".tmp")
            shutil.copyfile(f, tmp)
            os.chmod(tmp, 0o600)
            os.replace(tmp, p["key"])
            have_key = True
            print("   Key saved on the tablet.")
            if _yes("   Delete it from Downloads now (other apps can read Downloads)?"):
                try:
                    f.unlink()
                    print("   Deleted from Downloads.")
                except OSError as exc:
                    print(f"   Could not delete it ({exc}); please delete it yourself.")
    if have_key:
        s["GOOGLE_SERVICE_ACCOUNT_FILE"] = "service-account.json"
        print(f"   Key: {is_service_account_key(p['key'])}\n"
              "   (your Google Sheet must be shared with this address as Editor - it already is if GitHub could write to it)")
        save()
    else:
        print("   No key file found in Downloads. Download it on this tablet (docs/TABLET.md, step 3), then run\n"
              "   `pp settings` again. Continuing with the other settings.")

    # 3. Gmail
    print("\n3) The outreach Gmail (the account that sends the e-mails) and its 16-letter App Password.\n"
          "   (Type - to skip this for now: the leads and the WhatsApp queue work without it, e-mails don't.)")
    while True:
        addr = _ask("   Gmail address", s.get("OUTREACH_GMAIL_ADDRESS", "")).strip()
        if addr == "-" or email_ok(addr):
            break
        print("   That is not an e-mail address (like name@gmail.com).", flush=True)
    if addr == "-":
        print("   Skipped - run `pp settings` later to add it.")
    else:
        s["OUTREACH_GMAIL_ADDRESS"] = addr
        while True:
            pw = _ask("   App Password (16 letters; typing is hidden; - to skip)", s.get("OUTREACH_GMAIL_APP_PASSWORD", ""),
                      secret=True)
            if pw.strip() == "-":
                print("   Skipped - run `pp settings` later to add it.")
                break
            pw = pw.replace(" ", "").lower()
            if app_password_ok(pw):
                s["OUTREACH_GMAIL_APP_PASSWORD"] = pw
                break
            print("   An App Password is exactly 16 letters (Google shows it in 4 groups of 4). Your normal Gmail\n"
                  "   password does not work here. Create one at https://myaccount.google.com/apppasswords", flush=True)
        save()

    # 4. Phone and alerts
    print("\n4) Your WhatsApp Business number (shown under every e-mail) and where alerts should go.")
    s["OUTREACH_SENDER_PHONE"] = _ask("   WhatsApp number, e.g. +91 98XXX XXXXX", s.get("OUTREACH_SENDER_PHONE", ""))
    while True:
        note = _ask("   E-mail for reply alerts (- = the outreach Gmail itself)", s.get("OUTREACH_NOTIFY_EMAIL", "") or "-")
        if note.strip() == "-" or email_ok(note):
            break
        print("   That is not an e-mail address (like name@gmail.com), or type - .", flush=True)
    s["OUTREACH_NOTIFY_EMAIL"] = "" if note.strip() == "-" else note.strip()
    save()
    print(f"\nSaved to {p['settings']} (only this tablet can read it).\n")

    # 5. Checks
    print("Checking the connections (up to a minute) ...", flush=True)
    env = job_env(p, s)
    ok_sheet, msg = check_sheet(env)
    print(("  [OK] " if ok_sheet else "  [!!] ") + msg)
    ok_mail, msg = check_gmail(s)
    print(("  [OK] " if ok_mail else "  [!!] ") + msg)
    if not ok_sheet or not ok_mail:
        print("\nFix the item marked [!!] and run `pp settings` again (Enter keeps everything else).")
    return 0 if (ok_sheet and ok_mail) else 1


def _set_switch(p: dict[str, Path], key: str, value: bool) -> None:
    s = {**SWITCH_DEFAULTS, **read_settings(p["settings"])}
    s[key] = "true" if value else "false"
    write_settings(p["settings"], s)


def wizard_import(p: dict[str, Path], files: list[Path] | None = None) -> int:
    """Find pp-state.zip / pp-outreach.zip in Downloads (or the given files) and import them."""
    import getpass

    given = bool(files)
    files = files or (find_downloads("pp-state*.zip") + find_downloads("pp-outreach*.zip") + find_downloads("pp-state*.enc"))
    if p["db"].exists() and not given:
        if not files:
            print("The campaign memory is already on this tablet.")
            return 0
        if not _yes("The campaign memory is already on this tablet. Replace it with the files in Downloads?", default=False):
            return 0
    if not files:
        print("No saved GitHub memory found in Downloads (pp-state.zip / pp-outreach.zip).\n"
              "Download both on this tablet (docs/TABLET.md, step 4) and run:  pp import\n"
              "Until then the lead generation waits (the outreach and the WhatsApp queue can run).")
        if not p["db"].exists() and _yes("Start the lead generation WITHOUT the old memory instead? (not recommended: for about\n"
                                         "3 days it re-finds the leads already in your sheet before it finds new ones)",
                                         default=False):
            _set_switch(p, "LEADGEN_ENABLED", True)
            print("Lead generation will start from scratch.")
            return 0
        if not p["db"].exists():
            _set_switch(p, "LEADGEN_ENABLED", False)
        return 3
    # newest file of each name only
    chosen: dict[str, Path] = {}
    for f in files:
        base = "pp-outreach" if f.name.startswith("pp-outreach") else "pp-state"
        if base not in chosen or f.stat().st_mtime > chosen[base].stat().st_mtime:
            chosen[base] = f
    print("Found: " + ", ".join(f.name for f in chosen.values()))
    for _attempt in range(3):
        key = getpass.getpass("PP_STATE_KEY (the long password you made up for GitHub; typing is hidden): ")
        try:
            done = import_state(p, list(chosen.values()), key)
        except ValueError as exc:
            print(f"  {exc}")
            continue
        print("Memory imported: " + ", ".join("lead generation" if k == "leadgen" else "outreach" for k in done) +
              ". The tablet continues where GitHub stopped.")
        if "leadgen" in done:
            _set_switch(p, "LEADGEN_ENABLED", True)
        if not given and _yes("Delete the downloaded files from Downloads now?"):
            for f in chosen.values():
                try:
                    f.unlink()
                except OSError:
                    pass
        return 0
    print("Three wrong tries. Find the PP_STATE_KEY you wrote down and run `pp import` again.")
    if not p["db"].exists():
        _set_switch(p, "LEADGEN_ENABLED", False)
    return 1


def test_email(p: dict[str, Path], count: int = 2) -> int:
    """Show the next `count` outreach e-mails exactly, then send them after a yes (in the sending hours)."""
    settings = effective_settings(p)
    env = job_env(p, settings)
    print("Checking the Google Sheet and Gmail first ...", flush=True)
    for ok, msg in (check_sheet(env), check_gmail(settings)):
        print(("  [OK] " if ok else "  [!!] ") + msg)
        if not ok:
            print("Fix this first (pp settings), then run  pp test-email  again. Nothing was sent.")
            return 1
    os.environ.update(env)
    from .config import load_config
    from .outreach.engine import Outreach
    from .outreach.store import OutreachStore

    cfg = load_config(str(REPO / "config" / "plant-parlour.toml"))
    p["state"].mkdir(parents=True, exist_ok=True)
    store = OutreachStore(str(p["outreach_db"]))
    try:
        return _test_email(cfg, store, count, Outreach)
    except Exception as exc:  # noqa: BLE001 - a plain message instead of a traceback
        print(f"Something went wrong: {type(exc).__name__}: {exc}\nNothing more was sent. `pp logs` and `pp check` help; "
              "or send this message to the person who set it up.")
        return 2
    finally:
        store.close()


def _test_email(cfg, store, count: int, Outreach) -> int:
    dry = Outreach(cfg, store, live=False, max_emails=count)
    dry.run()
    if not dry.preview:
        print("No e-mail would go out right now:\n  " + "\n  ".join(dry.notes or ["no lead with a usable e-mail is waiting"]))
        return 1
    for i, row in enumerate(dry.preview, 1):
        _day, _lid, business, to, subject, body, _key = row
        print(f"\n----- e-mail {i} of {len(dry.preview)} -----\nTo: {to}  ({business})\nSubject: {subject}\n\n{body}\n")
    print("-" * 40)
    in_window, why, _end = dry._window(dry._local())
    if not in_window:
        print(f"These are the e-mails that would go out. Not sending now: {why}.\n"
              "Run  pp test-email  again between 10:00 and 18:30, Monday to Saturday.")
        return 0
    if not _yes(f"Send these {len(dry.preview)} e-mails now?", default=False):
        print("Nothing sent.")
        return 0
    live = Outreach(cfg, store, live=True, max_emails=count)
    code, summary = live.run()
    for row in live.sent_log:
        print(f"  {row[3]:<8} {row[1]} -> {row[2]}")
    for n in summary.get("notes") or []:
        print("  " + n)
    if not live.sent_log:
        print("Nothing was sent (see the note above).")
    return code


# ------------------------------------------------------------------ command line
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m leadgen tablet", description="Plant Parlour on an Android tablet")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="run the scheduler (forever)")
    sub.add_parser("status", help="what ran and what runs next")
    r = sub.add_parser("run", help="run one job now")
    r.add_argument("job", choices=sorted(JOBS))
    u = sub.add_parser("update", help="get, test and install the newest version")
    u.add_argument("--discard-local", action="store_true", help="update even if files were changed on the tablet (a copy is kept)")
    u.add_argument("--check", action="store_true", help="only say whether a new version exists")
    sub.add_parser("rollback", help="go back to the version before the last update")
    sub.add_parser("setup", help="ask for the settings and check them")
    sub.add_parser("check", help="test the Google Sheet and Gmail connections")
    i = sub.add_parser("import", help="import the campaign memory GitHub saved")
    i.add_argument("files", nargs="*", help="pp-state.zip / pp-outreach.zip (default: look in Downloads)")
    t = sub.add_parser("test-email", help="show the next outreach e-mails and send them after a yes")
    t.add_argument("--count", type=int, default=2)
    sub.add_parser("record-tests", help="remember which tests fail on this tablet now (used by the updater)")
    sub.add_parser("replan", help="re-plan the area after the area or number of days changed (leads are kept)")
    w = sub.add_parser("switch", help="turn a part on or off")
    w.add_argument("key", choices=sorted(SWITCH_DEFAULTS))
    w.add_argument("state", choices=["on", "off"])
    return ap


def main(argv=None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\nStopped. Nothing was half-saved; run the same command again to continue.")
        return 130


def _main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    p = paths()
    p["home"].mkdir(parents=True, exist_ok=True)
    if args.cmd == "serve":
        return Tablet(p, stream=sys.stdout).serve()
    if args.cmd == "status":
        print(status_text(p))
        return 0
    if args.cmd == "run":
        return Tablet(p, stream=sys.stdout).run_job(JOBS[args.job], manual=True)
    if args.cmd in ("update", "rollback", "import", "test-email", "record-tests", "replan"):
        # These change the code or the memory: never while a job is running.
        lock = JobLock(p)
        if not lock.acquire(wait=False):
            print(busy_message())
            return BUSY
        try:
            return _locked_command(args, p)
        finally:
            lock.release()
    if args.cmd == "setup":
        return wizard(p)
    if args.cmd == "switch":
        _set_switch(p, args.key, args.state == "on")
        names = {"LEADGEN_ENABLED": "Lead generation", "OUTREACH_ENABLED": "Outreach",
                 "OUTREACH_LIVE": "Live sending (off = dry-run: nothing is sent)", "AUTO_UPDATE": "Automatic updates"}
        print(f"{names[args.key]}: {args.state.upper()} (applies from the next job; no restart needed)")
        return 0
    if args.cmd == "check":
        s = effective_settings(p)
        env = job_env(p, s)
        ok1, m1 = check_sheet(env)
        ok2, m2 = check_gmail(s)
        print(("[OK] " if ok1 else "[!!] ") + m1)
        print(("[OK] " if ok2 else "[!!] ") + m2)
        mem = "imported" if p["db"].exists() else "NOT imported yet (pp import)"
        print(f"[{'OK' if p['db'].exists() else '!!'}] campaign memory {mem}")
        return 0 if (ok1 and ok2) else 1
    return 2


def _locked_command(args, p: dict[str, Path]) -> int:
    if args.cmd == "update":
        log = Log(p, sys.stdout)
        return update(p, log=log, discard_local=args.discard_local, check_only=args.check)
    if args.cmd == "rollback":
        return rollback(p, log=Log(p, sys.stdout))
    if args.cmd == "import":
        return wizard_import(p, [Path(f) for f in args.files] or None)
    if args.cmd == "replan":
        env = job_env(p, effective_settings(p))
        return subprocess.run([sys.executable, "-m", "leadgen", "plan", "--force"], cwd=str(REPO), env=env).returncode
    if args.cmd == "test-email":
        return test_email(p, args.count)
    if args.cmd == "record-tests":
        ok, failures = run_checks(sys.executable, Log(p, sys.stdout))
        rec = load_json(p["update"])
        rec.update(baseline_failures=sorted(failures), current=git("rev-parse", "HEAD", check=False))
        save_json(p["update"], rec)
        print(f"recorded: {len(failures)} failing test(s) on this tablet" + (": " + ", ".join(sorted(failures)) if failures else ""))
        return 0 if ok else 1
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
