#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "typer>=0.12",
#   "textual>=1.0",
#   "psutil>=5.9",
# ]
# ///
"""ai-task-commander: text UI to follow a codex / claude job queue, and optionally run it.

Usage:
    uv run ai-task-commander.py QUEUE_DIR [--refresh 15] [--jobs 5] [--runner auto|codex|claude]
                                          [--run [--force] [--workdir DIR]]
    uv run ai-task-commander.py --help    (or -h)

Press / in the UI to change the refresh interval, the visible job rows and "follow new jobs"
(select the newest job when one enters the queue; off by default) for the session.

Without --run it only reads the queue (another runner processes it).
With --run it is also the queue runner (replaces gpt-watcher / claude-watcher): every 5 s it runs the
CLI for each pending <name>.json, oldest first, one at a time, and writes:
    <name>.status    JSON with state (RUNNING, OK, ERR), start/stop, exit code and metadata.
                     No .status means the job is queued.
    <name>.pid       PID of the CLI while it runs (removed when it ends).
    <name>.md        Answer.   <name>.err   CLI stderr.
    <name>.json.done The job, renamed once it is finished (its .status is final by then).
    .monitor-runner.lock  Lock with the runner's PID while it runs (one runner per queue).
    ai-task-commander.pid  PID of the monitor while it runs the queue (removed on exit).
                     So "ai-task-commander" is not a valid job name.

Status shown for each job: from <name>.status when present; for jobs run by the legacy
PowerShell/bash watchers (no .status) it is inferred from .pid/.md/.err presence and timestamps.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import psutil
import typer
from rich.console import Group, RenderableType
from rich.markdown import Markdown
from rich.rule import Rule
from rich.table import Table
from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widget import Widget
from textual.widgets import Checkbox, DataTable, Input, Label, Static

# --- Constants ----------------------------------------------------------------

OK, ERR, RUNNING, QUEUED = "OK", "ERR", "RUNNING", "QUEUED"
STATUS_ICON = {OK: "✔", ERR: "✖", RUNNING: "●", QUEUED: "◌"}
STATUS_STYLE = {OK: "bold green", ERR: "bold red", RUNNING: "bold yellow", QUEUED: "bold cyan"}
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

MD_MAX = 1024 * 1024  # bytes of <name>.md shown (head)
ERR_MAX = 256 * 1024  # bytes of <name>.err shown (tail: errors are at the end)
SHELL_NAMES = {"powershell", "pwsh", "bash", "sh", "dash", "zsh"}
WATCHER_SCRIPTS = {
    "gpt-watcher.ps1": "codex",
    "gpt-watcher.sh": "codex",
    "claude-watcher.ps1": "claude",
    "claude-watcher.sh": "claude",
}
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
# model/effort reach the CLI command line (through cmd.exe for npm .cmd shims): plain characters only.
SAFE_ARG = re.compile(r"^[A-Za-z0-9._:/@+\[\]-]{1,100}$")
LOCK_NAME = ".monitor-runner.lock"
MONITOR_PID_NAME = "ai-task-commander.pid"  # PID of the --run monitor, plain text
POLL_SECONDS = 5


class Runner(str, Enum):
    auto = "auto"
    codex = "codex"
    claude = "claude"


# --- File helpers -------------------------------------------------------------


@dataclass(frozen=True)
class FileInfo:
    path: Path
    size: int
    mtime: float
    birth: float | None  # creation time; None where the OS does not expose it (Linux)

    @property
    def sig(self) -> tuple:
        return (self.path.name, self.size, self.mtime)


def stat_file(path: Path) -> FileInfo | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    birth = getattr(st, "st_birthtime", None)
    if birth is None and os.name == "nt":
        birth = st.st_ctime  # on Windows (Python < 3.12) st_ctime is the creation time
    return FileInfo(path, st.st_size, st.st_mtime, birth)


def decode(raw: bytes) -> str:
    """Decode queue files: UTF-8 (with or without BOM) or UTF-16 (old .err files)."""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace")
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    sample = raw[:4096]
    if len(sample) >= 16 and sample[1::2].count(0) > len(sample) // 4:
        return raw.decode("utf-16-le", errors="replace")  # UTF-16 without BOM
    return raw.decode("utf-8", errors="replace")


def read_text(path: Path, limit: int, tail: bool) -> tuple[str, bool]:
    """Read at most `limit` bytes (head or tail). Returns (text, truncated)."""
    try:
        with path.open("rb") as f:
            head = f.read(4)
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size <= limit:
                f.seek(0)
                return decode(f.read()), False
            start = size - limit if tail else 0
            utf16 = head.startswith((b"\xff\xfe", b"\xfe\xff"))
            if utf16 and start % 2:
                start += 1  # keep UTF-16 code units aligned
            f.seek(start)
            raw = f.read(limit)
    except OSError as e:
        return f"(cannot read {path.name}: {e})", False
    if utf16 and start:
        raw = head[:2] + raw  # re-add the BOM so the tail decodes with the right byte order
    return decode(raw), True


def read_job(path: Path) -> tuple[dict, str | None]:
    """Parse a job file. Returns (job, error) with the same validation as the watchers."""
    if path.name.split(".", 1)[0] + ".pid" == MONITOR_PID_NAME:
        return {}, f"reserved job name (its .pid would clash with {MONITOR_PID_NAME})"
    try:
        text = decode(path.read_bytes())
    except OSError as e:
        return {}, f"cannot read ({e})"
    if not text.strip():
        return {}, "empty file"
    try:
        job = json.loads(text)
    except ValueError as e:
        return {}, f"invalid JSON ({e})"
    if not isinstance(job, dict):
        return {}, "not a JSON object"
    if not all(job.get(k) for k in ("model", "effort", "prompt")):
        return job, "missing model, effort or prompt"
    bad = [k for k in ("model", "effort") if not SAFE_ARG.match(str(job[k]))]
    if bad:
        verb = "contains" if len(bad) == 1 else "contain"
        return job, f"{' and '.join(bad)} {verb} characters that are not allowed (only letters, digits and ._:/@+[]-)"
    return job, None


def pid_alive(pid: int) -> bool:
    # Never os.kill(pid, 0): on Windows it terminates the process.
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, OSError):
        return False


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts).astimezone().isoformat(timespec="seconds")


def parse_iso(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp() if value else None
    except ValueError:
        return None


def read_json_file(path: Path) -> dict | None:
    try:
        data = json.loads(decode(path.read_bytes()))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_json_atomic(path: Path, data: dict) -> None:
    """Write UTF-8 JSON through a temp file + rename, so readers never see a half-written file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes((json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    for attempt in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:  # Windows: a reader has the target open right now
            time.sleep(0.05 * (attempt + 1))
    os.replace(tmp, path)


def lock_owner(queue: Path) -> tuple[psutil.Process, dict] | None:
    """Process holding the queue's runner lock, if it is still alive (PID reuse is checked)."""
    info = read_json_file(queue / LOCK_NAME)
    if not info:
        return None
    try:
        proc = psutil.Process(int(info["pid"]))
        if abs(proc.create_time() - float(info["started"])) < 2:
            return proc, info
    except (psutil.Error, OSError, KeyError, TypeError, ValueError):
        pass
    return None


# --- Queue model --------------------------------------------------------------


@dataclass
class Job:
    name: str
    job_file: FileInfo  # <name>.json or <name>.json.done
    done: bool
    md: FileInfo | None
    err: FileInfo | None
    pidf: FileInfo | None
    statusf: FileInfo | None = None
    model: str = ""
    effort: str = ""
    prompt: str = ""
    status: str = QUEUED
    note: str = ""
    pid: int | None = None
    queued: float | None = None
    start: float | None = None
    stop: float | None = None
    exact: bool = False  # status and times come from <name>.status (not inferred)
    meta: dict | None = None  # contents of <name>.status

    @property
    def signature(self) -> tuple:
        return (
            self.status,
            self.note,
            self.job_file.sig,
            self.md.sig if self.md else None,
            self.err.sig if self.err else None,
            self.statusf.sig if self.statusf else None,
            self.pid,
        )

    def duration(self, now: float) -> float | None:
        if self.start is None:
            return None
        end = self.stop if self.stop is not None else (now if self.status == RUNNING else None)
        return None if end is None else max(0.0, end - self.start)


class QueueScanner:
    """Builds the job list from the queue directory. Read-only."""

    def __init__(self, queue: Path, runner: Runner):
        self.queue = queue
        self.runner_override = runner
        self.runner = runner.value if runner != Runner.auto else "unknown"
        self._job_cache: dict[Path, tuple[tuple, dict, str | None]] = {}
        self.watcher: psutil.Process | None = None
        self.watcher_exact = False  # True when the process command line names the watcher script
        self.watcher_kind: str | None = None
        self.watcher_builtin = False  # True when the runner is a ai-task-commander --run

    # -- jobs --

    def _parse(self, fi: FileInfo) -> tuple[dict, str | None]:
        key = (fi.size, fi.mtime)
        cached = self._job_cache.get(fi.path)
        if cached and cached[0] == key:
            return cached[1], cached[2]
        job, error = read_job(fi.path)
        self._job_cache[fi.path] = (key, job, error)
        return job, error

    def scan(self) -> list[Job]:
        pending: dict[str, Path] = {}
        done: dict[str, Path] = {}
        try:
            entries = list(os.scandir(self.queue))
        except OSError:
            return []
        for e in entries:
            if e.name.endswith(".json.done"):
                done[e.name[: -len(".json.done")]] = Path(e.path)
            elif e.name.endswith(".json"):
                pending[e.name[: -len(".json")]] = Path(e.path)

        jobs: list[Job] = []
        errors: dict[str, str | None] = {}
        for name in set(pending) | set(done):
            is_done = name not in pending  # a pending .json wins over an old .json.done
            fi = stat_file(pending.get(name) or done[name])
            if fi is None:
                continue  # renamed under our feet; next refresh will see it
            job = Job(
                name=name,
                job_file=fi,
                done=is_done,
                md=stat_file(self.queue / f"{name}.md"),
                err=stat_file(self.queue / f"{name}.err"),
                pidf=stat_file(self.queue / f"{name}.pid"),
                statusf=stat_file(self.queue / f"{name}.status"),
                queued=fi.mtime,
            )
            data, error = self._parse(fi)
            job.model = str(data.get("model") or "")
            job.effort = str(data.get("effort") or "")
            job.prompt = str(data.get("prompt") or "")
            errors[name] = error
            jobs.append(job)

        # The OK/ERR rules depend on the runner: detect it before classifying.
        self._detect_runner(jobs)
        for job in jobs:
            if errors[job.name] is None and self._from_status(job):
                continue
            if job.done:
                self._finished(job)
            else:
                self._pending(job, errors[job.name])

        live = {p for p in self._job_cache if p.exists()}
        self._job_cache = {p: v for p, v in self._job_cache.items() if p in live}
        jobs.sort(key=lambda j: (j.queued or 0, j.name), reverse=True)
        return jobs

    def _from_status(self, job: Job) -> bool:
        """Use <name>.status (written by --run). Returns False when there is none or it is outdated."""
        if job.statusf is None:
            return False
        meta = read_json_file(job.statusf.path)
        if not meta:
            return False
        start = parse_iso(meta.get("start"))
        if start is None or start < (job.queued or 0) - 2:
            return False  # left over from an older job with the same name
        state = meta.get("state")
        if state not in (OK, ERR, RUNNING) or (job.done and state == RUNNING):
            return False
        job.meta, job.exact = meta, not meta.get("inferred")  # backfilled files mark inferred values
        job.start, job.stop = start, parse_iso(meta.get("stop"))
        job.pid = meta.get("pid") if isinstance(meta.get("pid"), int) else None
        if state in (OK, ERR):
            job.status = state
            job.note = meta.get("reason") or f"exit code {meta.get('exit_code')}"
            return True
        host = meta.get("host")
        if host and host != socket.gethostname():
            job.status, job.note = RUNNING, f"running on {host}"
        elif job.pid is not None and pid_alive(job.pid):
            job.status = RUNNING
        else:
            job.status, job.stop = QUEUED, None
            job.note = (
                f"stale .status: process {job.pid} no longer exists, the runner died mid-job. "
                "It will run again when the runner is restarted with --force"
            )
        return True

    def _pending(self, job: Job, error: str | None) -> None:
        if error:
            job.status = ERR
            job.note = f"not a valid job ({error}); the watcher ignores it until the file changes"
            return
        if job.pidf is None:
            job.status = QUEUED
            return
        try:
            job.pid = int(job.pidf.path.read_text(encoding="utf-8", errors="replace").strip())
        except (OSError, ValueError):
            job.pid = None  # being written right now
        if job.pid is None or pid_alive(job.pid):
            job.status = RUNNING
            job.start = job.pidf.mtime
            if job.pid is not None:
                try:
                    job.start = psutil.Process(job.pid).create_time()
                except (psutil.Error, OSError):
                    pass
        else:
            job.status = QUEUED
            job.note = (
                f"stale .pid: process {job.pid} no longer exists, the watcher died mid-job. "
                "It will run again when the watcher is restarted with -Force/--force"
            )

    def _finished(self, job: Job) -> None:
        md, err = job.md, job.err
        runner = self.runner
        if md is None:
            ok, why = False, "no .md was written"
        elif runner == "claude":
            head = read_text(md.path, 256, tail=False)[0].lstrip() if md.size else ""
            if md.size == 0:
                ok, why = False, ".md is empty"
            elif err is not None and err.size > 0:
                ok, why = False, ".err is not empty"
            elif md.size < 1024 and head.startswith(("API Error", "Error:")):
                ok, why = False, ".md holds a CLI error message"
            else:
                ok, why = True, ".md written and .err empty"
        elif runner == "codex":
            ok, why = True, ".md written (codex only writes it on success)"
        else:
            ok = md.size > 0
            why = ".md written" if ok else ".md is empty"
        job.status = OK if ok else ERR
        job.note = f"inferred: {why}"
        # Start: creation time of the .err (or claude's .md), both created when the CLI starts.
        q = job.queued or 0
        births = [f.birth for f in (err, md) if f is not None and f.birth is not None and f.birth >= q - 1]
        job.start = min(births) if births else None
        mtimes = [f.mtime for f in (md, err) if f is not None]
        job.stop = max(mtimes) if mtimes else None
        if job.start is not None and job.stop is not None and job.stop < job.start:
            job.start = None

    # -- runner / watcher --

    def _detect_runner(self, jobs: list[Job]) -> None:
        if self.runner_override != Runner.auto:
            self.runner = self.runner_override.value
            return
        if self.watcher_kind:
            self.runner = self.watcher_kind
            return
        name = self.queue.name.lower()
        if "gpt" in name or "codex" in name:
            self.runner = "codex"
            return
        if "claude" in name:
            self.runner = "claude"
            return
        models = [j.model.lower() for j in jobs if j.model]
        if any(m.startswith("claude") for m in models):
            self.runner = "claude"
        elif any(m.startswith(("gpt", "o3", "o4", "codex")) for m in models):
            self.runner = "codex"
        else:
            self.runner = "unknown"

    def _default_queue(self, script: str) -> Path:
        kind = "gpt-queue" if script.startswith("gpt") else "claude-queue"
        if script.endswith(".ps1"):
            return Path(r"C:\Claude\HomeAssistant") / kind
        return Path.home() / "Claude" / "HomeAssistant" / kind

    def _queue_arg(self, cmd: list[str]) -> str | None:
        for i, tok in enumerate(cmd):
            low = tok.lower()
            if low.startswith("--queue="):
                return tok.split("=", 1)[1]
            if low.startswith("-queue:"):
                return tok.split(":", 1)[1]
            bare = low.lstrip("-")
            if low.startswith("-") and bare and "queue".startswith(bare) and i + 1 < len(cmd):
                return cmd[i + 1]  # -q, --queue, -Queue, -Qu (PowerShell prefix)
        return None

    def _same_path(self, a: Path, b: Path) -> bool:
        norm = lambda p: os.path.normcase(os.path.abspath(str(p)))  # noqa: E731
        return norm(a) == norm(b)

    def _scan_watcher(self) -> tuple[psutil.Process, str] | None:
        """Find a watcher process whose command line names the script and serves our queue."""
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                pname = (p.info["name"] or "").lower().removesuffix(".exe")
                if pname not in SHELL_NAMES:
                    continue
                cmd = p.info["cmdline"] or []
                script = next(
                    (os.path.basename(t.replace("\\", "/")).lower() for t in cmd
                     if os.path.basename(t.replace("\\", "/")).lower() in WATCHER_SCRIPTS),
                    None,
                )
                if not script:
                    continue
                qarg = self._queue_arg(cmd)
                if qarg is None:
                    target = self._default_queue(script)
                else:
                    target = Path(qarg)
                    if not target.is_absolute():
                        try:
                            target = Path(p.cwd()) / target
                        except (psutil.Error, OSError):
                            pass
                if self._same_path(target, self.queue):
                    return p, WATCHER_SCRIPTS[script]
            except (psutil.Error, OSError):
                continue
        return None

    def detect_watcher(self) -> None:
        """Find the runner: a --run monitor (lock file) or a legacy watcher by its command line."""
        owner = lock_owner(self.queue)
        if owner:
            self.watcher, info = owner
            self.watcher_exact, self.watcher_builtin = True, True
            self.watcher_kind = info.get("runner") if info.get("runner") in ("codex", "claude") else None
            return
        if self.watcher_builtin:
            self.watcher, self.watcher_exact, self.watcher_kind, self.watcher_builtin = None, False, None, False
        if self.watcher is not None:
            try:
                # A shell found only as the parent of a running job may outlive the watcher: re-check it.
                if self.watcher_exact and self.watcher.is_running():
                    return
            except psutil.Error:
                pass
            self.watcher, self.watcher_exact, self.watcher_kind = None, False, None
        found = self._scan_watcher()
        if found:
            self.watcher, self.watcher_kind = found
            self.watcher_exact = True

    def detect_watcher_parent(self, jobs: list[Job]) -> None:
        """Fallback after scan(): the shell that launched the running job."""
        if self.watcher is not None:
            return
        # Interactive shells (".\gpt-watcher.ps1" typed at a prompt) do not show the script in their
        # command line: fall back to the shell that launched the running job.
        for job in jobs:
            if job.status == RUNNING and job.pid:
                try:
                    proc = psutil.Process(job.pid)
                    for anc in proc.parents()[:3]:
                        if anc.name().lower().removesuffix(".exe") in SHELL_NAMES:
                            self.watcher, self.watcher_exact = anc, False
                            return
                except (psutil.Error, OSError):
                    pass


# --- Built-in runner (--run) --------------------------------------------------


def find_cli(name: str) -> str | None:
    """Path of the codex/claude CLI. On Windows a real executable (.exe/.cmd), not the npm .ps1 shim."""
    if os.name == "nt":
        for ext in (".exe", ".cmd"):
            found = shutil.which(name + ext)
            if found:
                return found
        return None
    return shutil.which(name)


def kill_tree(pid: int) -> None:
    """Kill a process and its children (on Windows the .pid of an npm shim is the cmd.exe wrapper)."""
    try:
        proc = psutil.Process(pid)
        procs = proc.children(recursive=True) + [proc]
    except psutil.Error:
        return
    for p in procs:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(procs, timeout=5)


class QueueRunner(threading.Thread):
    """Processes the queue like gpt-watcher / claude-watcher, and records each job in <name>.status."""

    def __init__(self, queue: Path, runner: str, cli: str, workdir: Path):
        super().__init__(daemon=True, name="queue-runner")
        self.queue, self.runner, self.cli, self.workdir = queue, runner, cli, workdir
        self.on_event = lambda message, severity: None  # set by the UI; called from this thread
        self.current: str | None = None
        self.finished = 0
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._aborted = False
        self._invalid: set[tuple[str, int]] = set()  # (path, mtime_ns) already reported
        self.lock_path: Path | None = None

    # -- control (any thread) --

    def stop(self) -> None:
        """Stop polling and abort the running job: the CLI is killed and its job stays queued."""
        self._stop_event.set()
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._aborted = True
                kill_tree(self._proc.pid)

    # -- loop (runner thread) --

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._pass()
            except Exception as e:  # never let the runner thread die silently
                self.on_event(f"Runner error: {e}", "error")
            self._stop_event.wait(POLL_SECONDS)

    def _pass(self) -> None:
        files = []
        for path in self.queue.glob("*.json"):
            try:
                files.append((path.stat().st_mtime, path))
            except OSError:
                pass
        for _, path in sorted(files):
            if self._stop_event.is_set():
                return
            try:
                stamp = (str(path), path.stat().st_mtime_ns)
            except OSError:
                continue  # taken or removed meanwhile
            if stamp in self._invalid:
                continue
            job, error = read_job(path)
            if error:
                # Like the watchers: report once and skip it until the file changes.
                self._invalid.add(stamp)
                self.on_event(f"Job {path.stem} ignored: {error}", "error")
                continue
            self._run_job(path, job)

    def _command(self, md: Path) -> tuple[list[str], int | None]:
        model, effort = self._job["model"], self._job["effort"]
        if self.runner == "codex":
            # The answer goes to <name>.md via -o; codex's stdout is discarded.
            return [
                self.cli, "exec", "--skip-git-repo-check", "--color", "never", "--model", model,
                "--cd", str(self.workdir), "-o", str(md), "-c", f"model_reasoning_effort={effort}",
                "-c", "model_reasoning_summary=none", "-",
            ], subprocess.DEVNULL
        # claude has no --cd: it runs in the working directory and its stdout is the answer.
        return [self.cli, "-p", "--model", model, "--effort", effort, "--output-format", "text"], None

    def _run_job(self, path: Path, job: dict) -> None:
        base = path.name[: -len(".json")]
        q = self.queue
        md, err, pidf, statf = (q / f"{base}{ext}" for ext in (".md", ".err", ".pid", ".status"))
        self._job = {k: str(job[k]) for k in ("model", "effort", "prompt")}
        args, stdout_spec = self._command(md)
        try:
            queued = path.stat().st_mtime
        except OSError:
            return
        status = {
            "job": base, "state": RUNNING, "reason": "", "exit_code": None,
            "queued": iso(queued), "start": None, "stop": None, "duration_s": None,
            "runner": self.runner, "model": self._job["model"], "effort": self._job["effort"],
            "pid": None, "cli": self.cli, "workdir": str(self.workdir),
            "host": socket.gethostname(), "runner_pid": os.getpid(),
            "output": md.name, "errors": err.name,
        }
        self.current = base
        self.on_event(f"▶ Started {base} ({self._job['model']}/{self._job['effort']})", "information")
        kwargs: dict = {}
        if os.name == "nt":  # keep the CLI off the TUI's console
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        env = dict(os.environ, NO_COLOR="1")  # no ANSI colour codes in .err
        t0 = time.time()
        rc: int | None = None
        reason = ""
        out_fh = err_fh = None
        try:
            err_fh = open(err, "wb")
            out_fh = open(md, "wb") if stdout_spec is None else stdout_spec
            with self._lock:
                if self._stop_event.is_set():
                    self._aborted = True
                    raise InterruptedError
                self._proc = subprocess.Popen(
                    args, cwd=self.workdir, stdin=subprocess.PIPE, stdout=out_fh, stderr=err_fh,
                    env=env, **kwargs,
                )
            proc = self._proc
            status.update(start=iso(t0), pid=proc.pid)
            write_json_atomic(statf, status)
            # While the CLI runs, <name>.pid holds its PID (same contract as the watchers).
            pidf.write_bytes(f"{proc.pid}\n".encode())
            try:  # the prompt goes through stdin: no command-line size or quoting limits
                proc.stdin.write(self._job["prompt"].encode("utf-8"))
                proc.stdin.close()
            except OSError:
                pass  # the CLI exited (or was killed) before reading everything
            rc = proc.wait()
        except InterruptedError:
            pass
        except OSError as e:
            rc, reason = -1, f"cannot start the CLI: {e}"
            if err_fh is not None:
                err_fh.write(f"ERROR: {reason}\n".encode("utf-8"))
        finally:
            for fh in (out_fh, err_fh):
                if fh not in (None, subprocess.DEVNULL):
                    fh.close()
            with self._lock:
                self._proc = None
            pidf.unlink(missing_ok=True)
        t1 = time.time()
        self.current = None
        if self._aborted:
            # Quit mid-job: like Ctrl+C on a watcher, the job stays pending (no .status = queued).
            statf.unlink(missing_ok=True)
            return
        ok = rc == 0 and md.exists()
        if not reason and not ok:
            reason = f"exit code {rc}" if rc != 0 else f"exit code 0 but {md.name} was not written"
        status.update(
            state=OK if ok else ERR, reason=reason or "exit code 0", exit_code=rc,
            start=status["start"] or iso(t0), stop=iso(t1), duration_s=round(t1 - t0, 1),
        )
        # Order matters for observers: .pid gone, then the final .status, then .json.done.
        write_json_atomic(statf, status)
        try:
            os.replace(path, path.with_name(path.name + ".done"))
        except OSError as e:
            self.on_event(f"Cannot rename {path.name} to .json.done: {e}", "error")
        self.finished += 1
        took = f"{t1 - t0:.1f} s"
        if ok:
            self.on_event(f"✔ {base}: OK ({took}) → {md.name}", "information")
        else:
            self.on_event(f"✖ {base}: ERROR ({took}, {reason})", "error")


def acquire_lock(queue: Path, runner: str, workdir: Path) -> Path:
    """Create the runner lock (one runner per queue). Raises RuntimeError if another one is alive."""
    lock = queue / LOCK_NAME
    owner = lock_owner(queue)
    if owner:
        raise RuntimeError(f"another ai-task-commander --run (PID {owner[0].pid}) is running this queue")
    lock.unlink(missing_ok=True)  # stale lock from a runner that died
    me = psutil.Process()
    info = {
        "pid": me.pid, "started": me.create_time(), "runner": runner, "workdir": str(workdir),
        "host": socket.gethostname(), "since": iso(time.time()),
    }
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("another runner created the lock at the same time") from None
    with os.fdopen(fd, "wb") as f:
        f.write((json.dumps(info, indent=2) + "\n").encode("utf-8"))
    # Plain PID file for scripts (same format as <name>.pid). The lock makes us the only runner,
    # so a leftover one from a monitor that died is simply overwritten.
    try:
        (queue / MONITOR_PID_NAME).write_bytes(f"{me.pid}\n".encode())
    except OSError as e:
        lock.unlink(missing_ok=True)
        raise RuntimeError(f"cannot write {MONITOR_PID_NAME} ({e})") from None
    return lock


def release_lock(lock: Path | None) -> None:
    """Remove the runner lock and ai-task-commander.pid, if they are still ours."""
    if lock is None:
        return
    pidf = lock.parent / MONITOR_PID_NAME
    try:
        if pidf.read_text(encoding="utf-8", errors="replace").strip() == str(os.getpid()):
            pidf.unlink(missing_ok=True)
    except OSError:
        pass
    info = read_json_file(lock)
    if info and info.get("pid") == os.getpid():
        lock.unlink(missing_ok=True)


def prepare_runner(queue: Path, runner: Runner, workdir: Path | None, force: bool) -> QueueRunner:
    """Startup checks of --run (same rules as the watchers). Exits with code 1 on failure."""

    def fail(msg: str) -> None:
        typer.secho(f"Error: {msg}", fg="red", err=True)
        raise typer.Exit(1)

    kind = runner.value
    if kind == "auto":
        name = queue.name.lower()
        kind = "codex" if ("gpt" in name or "codex" in name) else "claude" if "claude" in name else ""
        if not kind:
            fail(f"cannot tell the runner from the queue name '{queue.name}': use --runner codex|claude")
    cli = find_cli(kind)
    if not cli:
        fail(f"the {kind} CLI was not found in PATH" + (" (codex.exe/.cmd)" if os.name == "nt" else ""))
    workdir = (workdir or queue.parent).resolve()
    if not workdir.is_dir():
        fail(f"the working directory {workdir} does not exist")
    if queue.exists() and not queue.is_dir():
        fail(f"{queue} exists but is a file, not a directory")
    if not queue.exists():
        queue.mkdir(parents=True)
        typer.echo(f"Created queue directory {queue}")

    scanner = QueueScanner(queue, Runner(kind))
    scanner.detect_watcher()
    if scanner.watcher is not None and not scanner.watcher_builtin:
        fail(f"a watcher script (PID {scanner.watcher.pid}) is already running this queue: stop it first")
    for pidf in queue.glob("*.pid"):
        if pidf.name == MONITOR_PID_NAME:
            continue  # the lock (checked below) tells whether another monitor is running
        try:
            pid = int(pidf.read_text(encoding="utf-8", errors="replace").strip())
        except (OSError, ValueError):
            continue
        if pid_alive(pid) and (queue / f"{pidf.stem}.json").exists():
            fail(f"job {pidf.stem} is being run by another process (PID {pid}): stop that runner first. "
                 f"If PID {pid} is not a codex/claude CLI (PIDs are reused), delete {pidf.name}.")

    pending, invalid = [], []
    for path in sorted(queue.glob("*.json"), key=lambda p: p.stat().st_mtime):
        job, error = read_job(path)
        (invalid if error else pending).append((path.name, error))
    for name, error in invalid:
        typer.secho(f"Warning: {name} is not a job ({error}): ignored.", fg="yellow", err=True)
    done = len(list(queue.glob("*.json.done")))
    typer.echo(f"Queue {queue}: {done} jobs done, {len(pending)} pending")
    if pending:
        names = ", ".join(n for n, _ in pending)
        if not force:
            fail(f"there are {len(pending)} pending jobs ({names}). Review them and use --force to run "
                 "them, or delete them.")
        typer.secho(f"Warning: {len(pending)} pending jobs ({names}) will run now (--force).", fg="yellow")
    try:
        lock = acquire_lock(queue, kind, workdir)
    except RuntimeError as e:
        fail(str(e))
    qr = QueueRunner(queue, kind, cli, workdir)
    qr.lock_path = lock
    return qr


# --- Formatting ---------------------------------------------------------------


def fmt_ts(ts: float | None, full: bool = False) -> str:
    if ts is None:
        return "—"
    dt = datetime.fromtimestamp(ts)
    if full:
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    if dt.date() == datetime.now().date():
        return dt.strftime("%H:%M:%S")
    return dt.strftime("%m-%d %H:%M")


def fmt_dur(sec: float | None) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024 or unit == "MB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def fmt_path(path: Path) -> str:
    """Path for the title/status bars: on Linux/macOS the home directory is shown as ~."""
    text = str(path)
    if os.name == "nt":
        return text
    home = str(Path.home())
    if home != "/" and (text == home or text.startswith(home + "/")):
        return "~" + text[len(home):]
    return text


def status_text(status: str, frame: int = 0) -> Text:
    icon = SPINNER[frame % len(SPINNER)] if status == RUNNING else STATUS_ICON[status]
    return Text(f"{icon} {status}", style=STATUS_STYLE[status])


# --- Config dialog ------------------------------------------------------------


class SpaceCheckbox(Checkbox, inherit_bindings=False):
    """Checkbox toggled with Space only, so Enter reaches the dialog and applies it.
    Empty box when off (Textual shows a dimmed X)."""

    BINDINGS = [Binding("space", "toggle_button", "Toggle", show=False)]

    @property
    def BUTTON_INNER(self) -> str:  # noqa: N802 (overrides Textual's class attribute)
        return "X" if self.value else " "


class ConfigScreen(ModalScreen[tuple[int, int, bool] | None]):
    """Session settings: auto-refresh interval, visible job rows and follow new jobs.
    Returns (refresh, rows, follow) or None."""

    BINDINGS = [Binding("escape", "cancel", "Cancel"), Binding("enter", "apply", show=False)]

    def __init__(self, refresh: int, rows: int, follow: bool):
        super().__init__()
        self.refresh_secs = refresh
        self.rows = rows
        self.follow = follow

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("⚙  Configuration", id="dlg-title")
            yield Label("Auto-refresh interval (seconds, 1-3600)")
            yield Input(str(self.refresh_secs), type="integer", id="refresh-input")
            yield Label("Job rows visible at once (1-50)")
            yield Input(str(self.rows), type="integer", id="rows-input")
            yield SpaceCheckbox("Follow new jobs (select the newest)", self.follow, id="follow-check")
            yield Label("[b]Enter[/] apply   [b]Tab[/] next field   [b]Space[/] toggle\n"
                        "[b]Esc[/] cancel   (not saved, this session only)", id="dlg-help")

    def on_mount(self) -> None:
        inp = self.query_one("#refresh-input", Input)
        inp.focus()
        inp.action_select_all()

    @staticmethod
    def _value(inp: Input, low: int, high: int) -> int | None:
        try:
            value = int(inp.value)
        except ValueError:
            return None
        return value if low <= value <= high else None

    @on(Input.Submitted)
    def submit(self) -> None:
        self.action_apply()

    def action_apply(self) -> None:
        checks = [("#refresh-input", 1, 3600, "Refresh"), ("#rows-input", 1, 50, "Job rows")]
        values = []
        for sel, low, high, label in checks:
            inp = self.query_one(sel, Input)
            value = self._value(inp, low, high)
            if value is None:
                self.query_one("#dlg-help", Label).update(
                    f"[b red]{label}: enter a whole number between {low} and {high}[/]")
                inp.focus()
                inp.action_select_all()
                return
            values.append(value)
        self.dismiss((values[0], values[1], self.query_one("#follow-check", Checkbox).value))

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmQuitScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n", "answer(False)", "No"),
        Binding("escape", "answer(False)", "No"),
    ]

    def __init__(self, job: str):
        super().__init__()
        self.job = job

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("⚠  Quit while a job is running?", id="dlg-title")
            yield Label(Text(f"Job {self.job} will be aborted (the CLI is killed). It stays queued and "
                             "runs again when you restart with --run --force."))
            yield Label("[b]y[/] quit and abort   [b]n[/] / [b]Esc[/] keep running", id="dlg-help")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


# --- Main app -----------------------------------------------------------------

PANES = ["#jobs", "#request", "#output", "#errors"]
COLUMNS = [  # (key, label, width); model/effort/queued are dropped on narrow terminals
    ("n", "#", 2), ("status", "Status", 11), ("job", "Job", None), ("model", "Model", None),
    ("effort", "Effort", 7), ("queued", "Queued", 11), ("start", "Start", 11),
    ("stop", "Stop", 11), ("dur", "Duration", 9),
]
COMPACT_WIDTH = 110


class JobTable(DataTable):
    """Job list: Home/End jump to the newest/oldest job (DataTable scrolls sideways by default)."""

    BINDINGS = [
        Binding("home", "scroll_top", show=False),
        Binding("end", "scroll_bottom", show=False),
    ]


class Pane(VerticalScroll):
    """Focusable, scrollable pane that can be zoomed (containers are not maximizable by default)."""

    ALLOW_MAXIMIZE = True


class MonitorApp(App):
    TITLE = "ai-task-commander"
    ENABLE_COMMAND_PALETTE = False
    ALLOW_IN_MAXIMIZED_VIEW = "#statusbar"  # the zoomed pane keeps both status bars
    CSS = """
    Screen { background: $background; }
    #titlebar {
        height: 1; background: $primary-darken-2; color: $text; padding: 0 1;
        text-wrap: nowrap; text-overflow: ellipsis;
    }
    #jobs {
        border: round $primary 60%;
        border-title-color: $primary; border-title-style: bold; border-subtitle-color: $text-muted;
        background: $surface;
    }
    #jobs:focus { border: round $accent; border-title-color: $accent; }
    #jobs > .datatable--header { background: $panel; color: $text-muted; text-style: bold; }
    #jobs > .datatable--cursor { background: $accent 30%; }
    #jobs:focus > .datatable--cursor { background: $accent 45%; text-style: bold; }
    #panes { height: 1fr; }
    #request { width: 2fr; }
    #right { width: 3fr; }
    #output { height: 3fr; }
    #errors { height: 2fr; }
    #errors.empty { height: 5; }
    .pane {
        border: round $primary 40%; border-title-color: $text-muted; border-subtitle-color: $text-muted;
        background: $surface; padding: 0 1; scrollbar-size-vertical: 1;
    }
    .pane:focus { border: round $accent; border-title-color: $accent; border-title-style: bold; }
    /* zoomed pane fills the screen (its split-layout width/height, e.g. #request 2fr, would overflow) */
    .pane.-maximized { width: 1fr !important; height: 1fr !important; }
    #errors.bad { border: round $error 70%; border-title-color: $error; }
    #errors.bad:focus { border: round $error; }
    #request-body, #output-body, #errors-body { width: 100%; height: auto; }
    #statusbar { dock: bottom; height: 2; background: $panel; }
    #status-left { width: 1fr; height: 1; padding: 0 1; text-overflow: ellipsis; text-wrap: nowrap; }
    #status-bottom { height: 1; background: $panel-darken-1; }
    #status-keys {
        width: 1fr; height: 1; padding: 0 1; color: $text-muted;
        text-overflow: ellipsis; text-wrap: nowrap;
    }
    #status-follow, #status-md { width: auto; height: 1; padding: 0 1; text-wrap: nowrap; }
    ConfigScreen { align: center middle; background: $background 60%; }
    #dialog {
        width: 54; height: auto; padding: 1 2; border: thick $accent; background: $surface;
    }
    #dialog Label { width: 100%; }
    #dlg-title { text-style: bold; color: $accent; margin-bottom: 1; }
    #follow-check { width: 100%; margin-top: 1; }
    ConfirmQuitScreen { align: center middle; background: $background 60%; }
    #dlg-help { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", show=False, priority=True),
        Binding("slash", "config", "Config"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("f", "toggle_follow", "Follow new jobs"),
        Binding("m", "toggle_markdown", "Markdown"),
        Binding("z", "zoom", "Zoom pane"),
        Binding("escape", "back", show=False),
        Binding("tab", "pane(1)", show=False, priority=True),
        Binding("shift+tab", "pane(-1)", show=False, priority=True),
        Binding("right", "pane(1)", show=False, priority=True),
        Binding("left", "pane(-1)", show=False, priority=True),
        *[Binding(str(i), f"pick({i})", show=False) for i in range(1, 10)],
    ]

    def __init__(self, queue: Path, refresh: int, rows: int, runner: Runner,
                 job_runner: QueueRunner | None = None):
        super().__init__()
        self.queue = queue
        self.job_runner = job_runner  # built-in runner (--run), or None when only monitoring
        self.refresh_secs = refresh
        self.rows = rows  # job rows visible at once; the table scrolls through the whole queue
        self.scanner = QueueScanner(queue, runner)
        self.jobs: list[Job] = []
        self.all_jobs: list[Job] = []
        self.selected: str | None = None
        self.rendered_sig: tuple | None = None
        self.markdown = True
        self.follow = False  # follow new jobs: select the newest when a job enters the queue
        self.frame = 0
        self.started = time.time()
        self.next_refresh = time.monotonic() + refresh
        self.last_refresh: float | None = None
        self._refreshing = False
        self._refresh_again = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timer = None
        self._compact: bool | None = None

    # -- layout --

    def compose(self) -> ComposeResult:
        yield Static(id="titlebar")
        yield JobTable(id="jobs", cursor_type="row", zebra_stripes=True)
        with Horizontal(id="panes"):
            with Pane(id="request", classes="pane"):
                yield Static(id="request-body")
            with Vertical(id="right"):
                with Pane(id="output", classes="pane"):
                    yield Static(id="output-body")
                with Pane(id="errors", classes="pane"):
                    yield Static(id="errors-body")
        with Vertical(id="statusbar"):
            yield Static(id="status-left")
            with Horizontal(id="status-bottom"):
                yield Static(id="status-keys")
                yield Static(id="status-follow")
                yield Static(id="status-md")

    def on_mount(self) -> None:
        self.theme = "tokyo-night"
        table = self.query_one("#jobs", DataTable)
        table.styles.height = self.rows + 3  # rows + header + border
        table.border_title = "◆ Queue"
        self.query_one("#request").border_title = "Request"
        self.query_one("#output").border_title = "Output"
        self.query_one("#errors").border_title = "Errors"
        self.watch(self.screen, "maximized", self._render_keys)  # also catches Esc (handled by Textual)
        self._render_flags()
        table.focus()
        self._timer = self.set_interval(self.refresh_secs, self.do_refresh)
        self.set_interval(0.5, self.tick)
        self.call_later(self.do_refresh)
        self._loop = asyncio.get_running_loop()
        if self.job_runner is not None:
            self.job_runner.on_event = self._runner_event
            self.job_runner.start()

    # -- built-in runner events --

    def _runner_event(self, message: str, severity: str) -> None:
        """Called from the runner thread. Non-blocking: never waits on the UI loop."""
        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._on_runner_event, message, severity)
        except RuntimeError:  # the UI loop is already closed (app exiting)
            pass

    def _on_runner_event(self, message: str, severity: str) -> None:
        self.notify(message, severity=severity, timeout=10 if severity == "error" else 5)
        self.call_later(self.do_refresh)

    # -- refresh cycle --

    async def do_refresh(self) -> None:
        if self._refreshing:
            self._refresh_again = True  # e.g. a runner event during a refresh: refresh once more after
            return
        self._refreshing = True
        self.next_refresh = time.monotonic() + self.refresh_secs
        try:
            jobs = await asyncio.to_thread(self._collect)
        finally:
            self._refreshing = False
        self.all_jobs = jobs
        self.jobs = jobs
        self.last_refresh = time.time()
        self._fill_table()
        self._render_header()
        self.tick()
        if self._refresh_again:
            self._refresh_again = False
            self.call_later(self.do_refresh)

    def _collect(self) -> list[Job]:
        self.scanner.detect_watcher()
        jobs = self.scanner.scan()
        self.scanner.detect_watcher_parent(jobs)
        return jobs

    def _setup_columns(self, table: DataTable) -> None:
        compact = self.size.width < COMPACT_WIDTH
        if compact == self._compact:
            return
        self._compact = compact
        table.clear(columns=True)
        for key, label, width in COLUMNS:
            if not (compact and key in ("model", "effort", "queued")):
                table.add_column(label, key=key, width=width)

    def on_resize(self) -> None:
        if self._compact is not None and (self.size.width < COMPACT_WIDTH) != self._compact:
            self._fill_table()
        self._render_status(time.time())

    def _fill_table(self) -> None:
        table = self.query_one("#jobs", DataTable)
        self._setup_columns(table)
        names = [j.name for j in self.jobs]
        current = [k.value for k in table.rows]
        if self.follow and current and set(names) - set(current):
            self.selected = names[0]  # a new job entered the queue: jump to the newest
        if self.selected not in names:
            self.selected = names[0] if names else None
        now = time.time()
        rows = []
        for i, job in enumerate(self.jobs, 1):
            name = Text(job.name, style="bold")
            if job.status == QUEUED and job.note.startswith("stale"):
                name.append("  ⚠ stale pid", style="yellow")
            cells = {
                "n": Text(str(i), style="dim"),
                "status": status_text(job.status, self.frame),
                "job": name,
                "model": Text(job.model or "—", style="magenta"),
                "effort": Text(job.effort or "—", style="blue"),
                "queued": Text(fmt_ts(job.queued), style="dim"),
                "start": Text(fmt_ts(job.start)),
                "stop": Text(fmt_ts(job.stop)),
                "dur": Text(fmt_dur(job.duration(now)), style="yellow" if job.status == RUNNING else ""),
            }
            rows.append((job.name, [cells[k.value] for k in table.columns]))
        if current == names:
            # Same rows: update in place (no flicker, cursor untouched).
            for key, cells in rows:
                for col, cell in zip(table.columns, cells):
                    table.update_cell(key, col, cell)
        else:
            table.clear()
            for key, cells in rows:
                table.add_row(*cells, key=key)
            if self.selected:
                table.move_cursor(row=names.index(self.selected), animate=False)
        counts = {s: sum(1 for j in self.all_jobs if j.status == s) for s in (OK, ERR, RUNNING, QUEUED)}
        table.border_title = f"◆ Queue · {len(self.jobs)} jobs, newest first"
        table.border_subtitle = "  ".join(f"{STATUS_ICON[s]} {counts[s]} {s}" for s in counts)
        self._show_selected()

    def _render_header(self) -> None:
        counts = {s: sum(1 for j in self.all_jobs if j.status == s) for s in (OK, ERR, RUNNING, QUEUED)}
        t = Text()
        t.append(" ◆ AI TASK COMMANDER ", style="bold reverse")
        t.append("  ")
        t.append(self.queue.name, style="bold")
        t.append("   ")
        for s in (OK, ERR, RUNNING, QUEUED):
            t.append(f" {STATUS_ICON[s]} {counts[s]} ", style=STATUS_STYLE[s])
        if self.last_refresh:
            t.append(f"   refreshed {fmt_ts(self.last_refresh)}", style="dim")
        if self.job_runner is not None:
            t.append("   ")
            t.append(" ▶ RUNNER ", style="bold reverse green")
            path = fmt_path(self.job_runner.workdir)
            room = self.size.width - t.cell_len - 12
            if len(path) > room > 12:
                path = "…" + path[-(room - 1):]
            t.append(f" workdir {path}", style="dim")
        self.query_one("#titlebar", Static).update(t)

    def tick(self) -> None:
        """Every 0.5 s: spinner, live durations and status line."""
        self.frame += 1
        now = time.time()
        table = self.query_one("#jobs", DataTable)
        for job in self.jobs:
            if job.status == RUNNING:
                try:
                    table.update_cell(job.name, "status", status_text(RUNNING, self.frame))
                    table.update_cell(job.name, "dur", Text(fmt_dur(job.duration(now)), style="yellow"))
                except Exception:  # row removed by a refresh in between
                    pass
        self._render_status(now)

    def _render_status(self, now: float) -> None:
        sc = self.scanner
        t = Text()
        runner_style = {"codex": "bold green", "claude": "bold #d97757"}.get(sc.runner, "bold yellow")
        t.append("runner ", style="dim")
        t.append(sc.runner, style=runner_style)
        t.append("  │  ", style="dim")
        t.append("watcher ", style="dim")
        w = sc.watcher
        jr = self.job_runner
        if jr is not None:
            t.append("built-in ", style="bold green")
            t.append(f"⏱ {fmt_dur(now - self.started)}", style="bold green")
            t.append(f" pid {os.getpid()}", style="dim")  # also in <queue>/ai-task-commander.pid
            if jr.current:
                t.append(f" ▶ {jr.current}", style="bold yellow")
            else:
                t.append(" idle", style="dim")
        elif w is None:
            t.append("not detected", style="yellow")
        else:
            try:
                if sc.watcher_exact:
                    if sc.watcher_builtin:
                        t.append("monitor --run ", style="green")
                    t.append(f"⏱ {fmt_dur(now - w.create_time())}", style="bold green")
                    t.append(f" pid {w.pid}", style="dim")
                else:
                    t.append(f"pid {w.pid}", style="green")
                    t.append(" (uptime unknown)", style="dim")
            except psutil.Error:
                t.append("gone", style="red")
        if jr is None:
            t.append("  │  ", style="dim")
            t.append("monitor ", style="dim")
            t.append(f"⏱ {fmt_dur(now - self.started)}", style="cyan")
            t.append(f" pid {os.getpid()}", style="dim")
        t.append("  │  ", style="dim")
        left = max(0, int(self.next_refresh - time.monotonic() + 0.999))
        t.append(f"⟳ {self.refresh_secs}s", style="bold cyan")
        t.append(f" (next {left}s)", style="dim")
        t.append("  │  ", style="dim")
        t.append("📂 ")
        path = fmt_path(self.queue)
        room = self.size.width - t.cell_len - 2
        if len(path) > room > 12:  # keep the end of the path, which names the queue
            path = "…" + path[-(room - 1):]
        t.append(path, style="bold")
        self.query_one("#status-left", Static).update(t)

    # -- details panes --

    @on(DataTable.RowHighlighted, "#jobs")
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None or event.row_key.value is None:
            return
        if event.cursor_row != event.data_table.cursor_row:
            return  # stale event from a table rebuild
        if event.row_key.value != self.selected:
            self.selected = event.row_key.value
            self.rendered_sig = None
        self._show_selected()

    def _job(self, name: str | None) -> Job | None:
        return next((j for j in self.jobs if j.name == name), None)

    def _show_selected(self, force: bool = False) -> None:
        job = self._job(self.selected)
        req = self.query_one("#request", Pane)
        out = self.query_one("#output", Pane)
        errs = self.query_one("#errors", Pane)
        if job is None:
            self.rendered_sig = None
            empty = Text(f"No jobs in {self.queue}", style="dim italic")
            self.query_one("#request-body", Static).update(empty)
            self.query_one("#output-body", Static).update("")
            self.query_one("#errors-body", Static).update("")
            return
        sig = (job.name, job.signature, self.markdown)
        if sig == self.rendered_sig and not force:
            return
        same_job = self.rendered_sig is not None and self.rendered_sig[0] == job.name
        self.rendered_sig = sig

        # Remember scroll positions: keep them for the same job, follow the tail if we were at the end.
        keep = {}
        for pane in (req, out, errs):
            at_end = pane.max_scroll_y > 0 and pane.scroll_y >= pane.max_scroll_y - 1
            keep[pane] = ("end" if at_end else pane.scroll_y) if same_job else 0

        self.query_one("#request-body", Static).update(self._render_request(job))
        req.border_title = Text(f"Request · {job.job_file.path.name}")

        out_body, out_sub = self._render_output(job)
        self.query_one("#output-body", Static).update(out_body)
        out.border_title = Text(f"Output · {job.name}.md")
        out.border_subtitle = out_sub

        err_body, err_sub, has_err = self._render_errors(job)
        self.query_one("#errors-body", Static).update(err_body)
        log_label = "stderr / log" if self.scanner.runner == "codex" else "stderr"
        errs.border_title = Text(f"Errors · {job.name}.err ({log_label})")
        errs.border_subtitle = err_sub
        errs.set_class(not has_err, "empty")
        errs.set_class(job.status == ERR, "bad")

        def restore() -> None:
            for pane, pos in keep.items():
                if pos == "end":
                    pane.scroll_end(animate=False)
                else:
                    pane.scroll_to(y=pos, animate=False)

        self.call_after_refresh(restore)

    def _render_request(self, job: Job) -> RenderableType:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", no_wrap=True)
        grid.add_column()
        grid.add_row("Job", Text(job.name, style="bold"))
        st = status_text(job.status)
        if job.note:
            st.append(f"  {job.note}", style="yellow" if job.status != OK else "dim")
        grid.add_row("Status", st)
        grid.add_row("Model", Text(job.model or "—", style="magenta"))
        grid.add_row("Effort", Text(job.effort or "—", style="blue"))
        grid.add_row("Queued", fmt_ts(job.queued, full=True))
        approx = Text("  (approx.)", style="dim") if not job.exact else Text()
        grid.add_row("Started", Text(fmt_ts(job.start, full=True)) + (approx if job.done and job.start else Text()))
        grid.add_row("Finished", Text(fmt_ts(job.stop, full=True)) + (approx if job.stop else Text()))
        grid.add_row("Duration", Text(fmt_dur(job.duration(time.time())), style="yellow"))
        if job.pid:
            alive = " (alive)" if job.status == RUNNING else (" (dead)" if not job.done else "")
            grid.add_row("PID", f"{job.pid}{alive}")
        meta = job.meta or {}
        if meta.get("exit_code") is not None:
            rc = meta["exit_code"]
            grid.add_row("Exit code", Text(str(rc), style="green" if rc == 0 else "bold red"))
        if meta:
            grid.add_row("Runner", Text(f"{meta.get('runner', '?')} on {meta.get('host', '?')}", style="dim"))
            grid.add_row("Workdir", Text(str(meta.get("workdir", "—")), style="dim"))
        files = [job.job_file.path.name]
        for f in (job.statusf, job.md, job.err, job.pidf):
            if f:
                files.append(f"{f.path.name} {fmt_size(f.size)}")
        grid.add_row("Files", Text(" · ".join(files), style="dim"))
        if job.prompt:
            body: RenderableType = Markdown(job.prompt) if self.markdown else Text(job.prompt)
        else:
            body = Text("(no prompt)", style="dim italic")
        return Group(grid, Rule("Prompt", style="cyan"), body)

    def _render_output(self, job: Job) -> tuple[RenderableType, str]:
        if job.md is None:
            msg = {
                QUEUED: "Waiting for the watcher to pick this job up…",
                RUNNING: "Working… the answer appears here when the CLI finishes.",
            }.get(job.status, "No output: <name>.md was not written.")
            style = "red italic" if job.status == ERR else "dim italic"
            return Text(msg, style=style), ""
        text, truncated = read_text(job.md.path, MD_MAX, tail=False)
        if not text.strip():
            if job.status == RUNNING:  # claude-watcher creates the .md when the CLI starts
                return Text("Working… the answer appears here when the CLI finishes.", style="dim italic"), ""
            return Text("(empty)", style="dim italic"), fmt_size(job.md.size)
        body: RenderableType = Markdown(text) if self.markdown else Text(text)
        sub = fmt_size(job.md.size) + (" · first 1 MB" if truncated else "")
        return body, sub

    def _render_errors(self, job: Job) -> tuple[RenderableType, str, bool]:
        if job.err is None or job.err.size == 0:
            msg = "No errors." if job.err is not None else "No .err file yet."
            return Text(msg, style="green dim italic"), "", False
        text, truncated = read_text(job.err.path, ERR_MAX, tail=True)
        body = Text(ANSI_RE.sub("", text).replace("\r\n", "\n"))
        body.highlight_regex(r"(?mi)^.*\b(error|fatal|exception|traceback)\b.*$", "bold red")
        body.highlight_regex(r"(?mi)^.*\bwarn(ing)?\b.*$", "yellow")
        sub = fmt_size(job.err.size) + (" · last 256 KB" if truncated else "")
        return body, sub, True

    # -- actions --

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # App bindings stay active under the modal: keep keys for the config inputs (Tab moves between them).
        if isinstance(self.screen, ModalScreen) and action not in ("quit", "focus_next", "focus_previous"):
            return False
        return True

    async def action_quit(self) -> None:
        jr = self.job_runner
        if isinstance(self.screen, ConfirmQuitScreen):
            return  # already asking
        if jr is not None and jr.current:
            def answer(yes: bool | None) -> None:
                if yes:
                    self.exit()
            self.push_screen(ConfirmQuitScreen(jr.current), answer)
            return
        self.exit()

    def action_pane(self, step: int) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()
        focused = self.focused
        ids = [self.query_one(p) for p in PANES]
        idx = ids.index(focused) if focused in ids else 0
        ids[(idx + step) % len(ids)].focus()

    def action_back(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()
        else:
            self.query_one("#jobs").focus()

    def action_zoom(self) -> None:
        if self.screen.maximized is not None:
            self.screen.minimize()
            return
        target = self.focused if self.focused is not None and self.focused.id in {"request", "output", "errors"} \
            else self.query_one("#output")
        target.focus()
        self.screen.maximize(target, container=False)

    def action_pick(self, n: int) -> None:
        if 1 <= n <= len(self.jobs):
            table = self.query_one("#jobs", DataTable)
            table.move_cursor(row=n - 1)
            table.focus()

    def action_toggle_markdown(self) -> None:
        self.markdown = not self.markdown
        self._render_flags()
        self._show_selected(force=True)

    def action_toggle_follow(self) -> None:
        self.follow = not self.follow
        self._render_flags()

    def _render_keys(self, maximized: Widget | None = None) -> None:
        if maximized is None:
            keys = ("[b]↑↓ PgUp[/]/[b]PgDn[/] [b]Home[/]/[b]End[/] job  [b]⇥ [/]/[b]←→[/] pane  [b]z[/] zoom  "
                    "[b]f[/] follow  [b]m[/] md  [b]r[/] refresh  [b]/[/] config  [b]q[/] quit")
        else:  # zoomed: the keys scroll the pane, the job list is hidden
            keys = ("[b]↑↓ PgUp[/]/[b]PgDn[/] [b]Home[/]/[b]End[/] scroll  [b]z[/]/[b]Esc[/] unzoom  "
                    "[b]f[/] follow  [b]m[/] md  [b]r[/] refresh  [b]/[/] config  [b]q[/] quit")
        self.query_one("#status-keys", Static).update(Text.from_markup(keys))

    @staticmethod
    def _flag(label: str, on: bool) -> Text:
        t = Text(f"{label} ", style="dim")
        t.append("ON" if on else "OFF", style="bold green" if on else "dim cyan")
        return t

    def _render_flags(self) -> None:
        self.query_one("#status-follow", Static).update(self._flag("⤒", self.follow))
        self.query_one("#status-md", Static).update(self._flag("md", self.markdown))

    async def action_refresh_now(self) -> None:
        if self._timer is not None:
            self._timer.reset()
        await self.do_refresh()
        self.notify("Refreshed", timeout=1.5)

    def action_config(self) -> None:
        def apply(values: tuple[int, int, bool] | None) -> None:
            if values is None:
                return
            refresh, rows, follow = values
            changes = []
            if refresh != self.refresh_secs:
                self.refresh_secs = refresh
                if self._timer is not None:
                    self._timer.stop()
                self._timer = self.set_interval(refresh, self.do_refresh)
                self.next_refresh = time.monotonic() + refresh
                changes.append(f"auto-refresh every {refresh}s")
            if rows != self.rows:
                self.rows = rows
                table = self.query_one("#jobs", DataTable)
                table.styles.height = rows + 3  # rows + header + border
                # Keep the selected job in view once the table has its new height.
                self.call_after_refresh(lambda: table.move_cursor(row=table.cursor_row, animate=False))
                changes.append(f"{rows} job rows")
            if follow != self.follow:
                self.follow = follow
                self._render_flags()
                changes.append(f"follow new jobs {'on' if follow else 'off'}")
            if changes:
                self.notify(", ".join(changes).capitalize(), timeout=2)
                self._render_status(time.time())

        self.push_screen(ConfigScreen(self.refresh_secs, self.rows, self.follow), apply)


# --- CLI ----------------------------------------------------------------------

cli = typer.Typer(add_completion=False, rich_markup_mode="rich",
                  context_settings={"help_option_names": ["-h", "--help"]})


@cli.command()
def main(
    queue: Path = typer.Argument(
        ..., help="Queue directory (e.g. C:\\Claude\\HomeAssistant\\gpt-queue). Created with --run if missing.",
        file_okay=True, dir_okay=True, resolve_path=True,
    ),
    refresh: int = typer.Option(15, "--refresh", "-r", min=1, max=3600, help="Auto-refresh interval in seconds."),
    jobs: int = typer.Option(5, "--jobs", "-n", min=1, max=50, help="Job rows visible at once; the list scrolls through the whole queue."),
    runner: Runner = typer.Option(
        Runner.auto, "--runner", case_sensitive=False,
        help="CLI the queue runs (auto: from the runner process or the queue name: gpt/codex or claude).",
    ),
    run: bool = typer.Option(
        False, "--run",
        help="Also run the queue (replaces gpt-watcher / claude-watcher) and write a <name>.status per job.",
    ),
    force: bool = typer.Option(
        False, "--force", "-f", help="With --run: start even if there are pending jobs, and run them.",
    ),
    workdir: Path | None = typer.Option(
        None, "--workdir", "-w", file_okay=False, resolve_path=True,
        help="With --run: working directory of the CLI (default: the queue's parent directory).",
    ),
) -> None:
    """Text UI to monitor a codex / claude job queue and, with --run, to run it."""
    if not run and (force or workdir is not None):
        typer.secho("Error: --force and --workdir need --run.", fg="red", err=True)
        raise typer.Exit(2)
    job_runner = None
    if run:
        job_runner = prepare_runner(queue, runner, workdir, force)
        if runner == Runner.auto:
            runner = Runner(job_runner.runner)
    elif not queue.is_dir():
        typer.secho(f"Error: the queue directory {queue} does not exist.", fg="red", err=True)
        raise typer.Exit(1)
    try:
        MonitorApp(queue, refresh, jobs, runner, job_runner).run()
    finally:
        if job_runner is not None:
            # Abort the running job (its .json stays pending), wait for the cleanup, free the lock.
            job_runner.stop()
            job_runner.join(timeout=15)
            release_lock(job_runner.lock_path)


if __name__ == "__main__":
    cli()
