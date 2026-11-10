#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "typer>=0.12",
#   "textual>=1.0",
#   "psutil>=5.9",
# ]
# ///
"""cli-watcher-monitor: text UI to follow a gpt-watcher / claude-watcher queue.

Usage:
    uv run cli-watcher-monitor.py QUEUE_DIR [--refresh 15] [--jobs 5] [--runner auto|codex|claude]

Read-only: it never writes, renames or deletes anything in the queue.

Job status is derived from the files the watchers leave in the queue:
    <name>.json                      QUEUED
    <name>.json + <name>.pid (alive) RUNNING
    <name>.json + <name>.pid (dead)  QUEUED (stale .pid: the watcher died mid-job)
    <name>.json that is not a job    ERR (the watcher ignores it)
    <name>.json.done                 OK or ERR, inferred from <name>.md / <name>.err
The watchers do not persist the CLI exit code, so OK/ERR of finished jobs and their
start/stop times are inferred from file presence and timestamps.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import stat
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
from textual.widgets import DataTable, Input, Label, Static

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
    return job, None


def pid_alive(pid: int) -> bool:
    # Never os.kill(pid, 0): on Windows it terminates the process.
    try:
        p = psutil.Process(pid)
        return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
    except (psutil.Error, OSError):
        return False


# --- Queue model --------------------------------------------------------------


@dataclass
class Job:
    name: str
    job_file: FileInfo  # <name>.json or <name>.json.done
    done: bool
    md: FileInfo | None
    err: FileInfo | None
    pidf: FileInfo | None
    model: str = ""
    effort: str = ""
    prompt: str = ""
    status: str = QUEUED
    note: str = ""
    pid: int | None = None
    queued: float | None = None
    start: float | None = None
    stop: float | None = None

    @property
    def signature(self) -> tuple:
        return (
            self.status,
            self.note,
            self.job_file.sig,
            self.md.sig if self.md else None,
            self.err.sig if self.err else None,
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
            if job.done:
                self._finished(job)
            else:
                self._pending(job, errors[job.name])

        live = {p for p in self._job_cache if p.exists()}
        self._job_cache = {p: v for p, v in self._job_cache.items() if p in live}
        jobs.sort(key=lambda j: (j.queued or 0, j.name), reverse=True)
        return jobs

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
        """Find the watcher by its command line (gives its uptime and runner). Run before scan()."""
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


def status_text(status: str, frame: int = 0) -> Text:
    icon = SPINNER[frame % len(SPINNER)] if status == RUNNING else STATUS_ICON[status]
    return Text(f"{icon} {status}", style=STATUS_STYLE[status])


# --- Config dialog ------------------------------------------------------------


class ConfigScreen(ModalScreen[int | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: int):
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("⚙  Configuration", id="dlg-title")
            yield Label("Auto-refresh interval (seconds)")
            yield Input(str(self.current), type="integer", id="refresh-input")
            yield Label("[b]Enter[/] apply   [b]Esc[/] cancel   (not saved, this session only)", id="dlg-help")

    def on_mount(self) -> None:
        inp = self.query_one(Input)
        inp.focus()
        inp.action_select_all()

    @on(Input.Submitted)
    def submit(self, event: Input.Submitted) -> None:
        try:
            value = int(event.value)
        except ValueError:
            value = 0
        if not 1 <= value <= 3600:
            self.query_one("#dlg-help", Label).update("[b red]Enter a whole number between 1 and 3600[/]")
            return
        self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
    TITLE = "cli-watcher-monitor"
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Screen { background: $background; }
    #titlebar { height: 1; background: $primary-darken-2; color: $text; padding: 0 1; }
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
    #errors.bad { border: round $error 70%; border-title-color: $error; }
    #errors.bad:focus { border: round $error; }
    #request-body, #output-body, #errors-body { width: 100%; height: auto; }
    #statusbar { dock: bottom; height: 2; background: $panel; }
    #status-left { width: 1fr; height: 1; padding: 0 1; text-overflow: ellipsis; text-wrap: nowrap; }
    #status-keys {
        width: 1fr; height: 1; padding: 0 1; color: $text-muted; background: $panel-darken-1;
        text-overflow: ellipsis; text-wrap: nowrap;
    }
    ConfigScreen { align: center middle; background: $background 60%; }
    #dialog {
        width: 54; height: auto; padding: 1 2; border: thick $accent; background: $surface;
    }
    #dlg-title { text-style: bold; color: $accent; margin-bottom: 1; }
    #dlg-help { color: $text-muted; margin-top: 1; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("ctrl+c", "quit", show=False, priority=True),
        Binding("slash", "config", "Config"),
        Binding("r", "refresh_now", "Refresh"),
        Binding("m", "toggle_markdown", "Markdown"),
        Binding("z", "zoom", "Zoom pane"),
        Binding("escape", "back", show=False),
        Binding("tab", "pane(1)", show=False, priority=True),
        Binding("shift+tab", "pane(-1)", show=False, priority=True),
        Binding("right", "pane(1)", show=False, priority=True),
        Binding("left", "pane(-1)", show=False, priority=True),
        *[Binding(str(i), f"pick({i})", show=False) for i in range(1, 10)],
    ]

    def __init__(self, queue: Path, refresh: int, rows: int, runner: Runner):
        super().__init__()
        self.queue = queue
        self.refresh_secs = refresh
        self.rows = rows  # job rows visible at once; the table scrolls through the whole queue
        self.scanner = QueueScanner(queue, runner)
        self.jobs: list[Job] = []
        self.all_jobs: list[Job] = []
        self.selected: str | None = None
        self.rendered_sig: tuple | None = None
        self.markdown = True
        self.frame = 0
        self.started = time.time()
        self.next_refresh = time.monotonic() + refresh
        self.last_refresh: float | None = None
        self._refreshing = False
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
            yield Static(id="status-keys")

    def on_mount(self) -> None:
        self.theme = "tokyo-night"
        table = self.query_one("#jobs", DataTable)
        table.styles.height = self.rows + 3  # rows + header + border
        table.border_title = "◆ Queue"
        self.query_one("#request").border_title = "Request"
        self.query_one("#output").border_title = "Output"
        self.query_one("#errors").border_title = "Errors"
        self.query_one("#status-keys", Static).update(Text.from_markup(
            "[b]↑↓ PgUp/PgDn Home/End[/] job  [b]⇥/←→[/] pane  [b]z[/] zoom  "
            "[b]m[/] md  [b]r[/] refresh  [b]/[/] config  [b]q[/] quit"
        ))
        table.focus()
        self._timer = self.set_interval(self.refresh_secs, self.do_refresh)
        self.set_interval(0.5, self.tick)
        self.call_later(self.do_refresh)

    # -- refresh cycle --

    async def do_refresh(self) -> None:
        if self._refreshing:
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
        current = [k.value for k in table.rows]
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
        t.append(" ◆ CLI WATCHER MONITOR ", style="bold reverse")
        t.append("  ")
        t.append(self.queue.name, style="bold")
        t.append("   ")
        for s in (OK, ERR, RUNNING, QUEUED):
            t.append(f" {STATUS_ICON[s]} {counts[s]} ", style=STATUS_STYLE[s])
        if self.last_refresh:
            t.append(f"   refreshed {fmt_ts(self.last_refresh)}", style="dim")
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
        if w is None:
            t.append("not detected", style="yellow")
        else:
            try:
                if sc.watcher_exact:
                    t.append(f"⏱ {fmt_dur(now - w.create_time())}", style="bold green")
                    t.append(f" pid {w.pid}", style="dim")
                else:
                    t.append(f"pid {w.pid}", style="green")
                    t.append(" (uptime unknown)", style="dim")
            except psutil.Error:
                t.append("gone", style="red")
        t.append("  │  ", style="dim")
        t.append("monitor ", style="dim")
        t.append(f"⏱ {fmt_dur(now - self.started)}", style="cyan")
        t.append("  │  ", style="dim")
        left = max(0, int(self.next_refresh - time.monotonic() + 0.999))
        t.append(f"⟳ {self.refresh_secs}s", style="bold cyan")
        t.append(f" (next {left}s)", style="dim")
        t.append("  │  ", style="dim")
        t.append("📂 ")
        path = str(self.queue)
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
        approx = Text("  (approx.)", style="dim")
        grid.add_row("Started", Text(fmt_ts(job.start, full=True)) + (approx if job.done and job.start else Text()))
        grid.add_row("Finished", Text(fmt_ts(job.stop, full=True)) + (approx if job.stop else Text()))
        grid.add_row("Duration", Text(fmt_dur(job.duration(time.time())), style="yellow"))
        if job.pid:
            grid.add_row("PID", f"{job.pid}" + (" (alive)" if job.status == RUNNING else " (dead)"))
        files = [job.job_file.path.name]
        for f in (job.md, job.err, job.pidf):
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
        # App bindings stay active under the modal: keep keys for the config input.
        if isinstance(self.screen, ConfigScreen) and action not in ("quit",):
            return False
        return True

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
        self.notify(f"Markdown rendering {'on' if self.markdown else 'off (raw text)'}", timeout=2)
        self._show_selected(force=True)

    async def action_refresh_now(self) -> None:
        if self._timer is not None:
            self._timer.reset()
        await self.do_refresh()
        self.notify("Refreshed", timeout=1.5)

    def action_config(self) -> None:
        def apply(value: int | None) -> None:
            if value is None or value == self.refresh_secs:
                return
            self.refresh_secs = value
            if self._timer is not None:
                self._timer.stop()
            self._timer = self.set_interval(value, self.do_refresh)
            self.next_refresh = time.monotonic() + value
            self.notify(f"Auto-refresh every {value}s", timeout=2)
            self._render_status(time.time())

        self.push_screen(ConfigScreen(self.refresh_secs), apply)


# --- CLI ----------------------------------------------------------------------

cli = typer.Typer(add_completion=False, rich_markup_mode="rich")


@cli.command()
def main(
    queue: Path = typer.Argument(
        ..., help="Queue directory of a gpt-watcher / claude-watcher (e.g. C:\\Claude\\HomeAssistant\\gpt-queue).",
        exists=True, file_okay=False, dir_okay=True, resolve_path=True,
    ),
    refresh: int = typer.Option(15, "--refresh", "-r", min=1, max=3600, help="Auto-refresh interval in seconds."),
    jobs: int = typer.Option(5, "--jobs", "-n", min=1, max=50, help="Job rows visible at once; the list scrolls through the whole queue."),
    runner: Runner = typer.Option(
        Runner.auto, "--runner", case_sensitive=False,
        help="CLI the queue runs (auto: detect from the watcher process, queue name or job models).",
    ),
) -> None:
    """Text UI to monitor a gpt-watcher / claude-watcher job queue (read-only)."""
    MonitorApp(queue, refresh, jobs, runner).run()


if __name__ == "__main__":
    cli()
