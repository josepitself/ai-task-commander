# ai-task-commander

[Català](README.cat.md)

Lets Claude Code delegate work to another model through its CLI (`codex` or `claude`), using a file-based queue.

`ai-task-commander.py` is a text UI that shows the queue and, with `--run`, also runs it: a single terminal, on Windows and on Linux. For each job it writes a `<name>.status` file with the state, start and end times and the exit code.

The name comes from the classic [Norton Commander](https://en.wikipedia.org/wiki/Norton_Commander), whose interface it resembles: a title bar at the top, framed panels you move through with `Tab` and the arrow keys, a bottom bar with the available keys, and everything done from the keyboard. Here, instead of files, the panels show the AI jobs: the queue, the request, the answer and the errors.

It replaces the old PowerShell and bash watchers (`gpt-watcher` / `claude-watcher`, `.ps1` and `.sh`), which are no longer part of the repository (they are in the git history up to commit `c9e1708`). The queues they processed can still be opened with the monitor: see [Jobs without .status](#jobs-without-status).

Never run two runners on the same queue. The monitor refuses to start with `--run` if another one is running with `--run`, if there is a job with a `.pid` of a live process, or if it detects one of the old watchers.

## Synopsis

```
uv run ai-task-commander.py QUEUE_DIR [--run [-f|--force] [-w|--workdir DIR]]
                                      [-r|--refresh SECONDS] [-n|--jobs N] [--runner auto|codex|claude]
                                      [-p|--protocol FILE]
uv run ai-task-commander.py -h|--help

Shows the jobs in queue QUEUE_DIR and, with --run, runs the CLI (codex or claude) for each
<name>.json job that appears in it. Quit with q (if a job is running, asks for confirmation).

Options:
  --run                 Also run the queue (replaces gpt-watcher / claude-watcher) and write
                        a <name>.status per job. Without --run it only reads the queue.
  -f, --force           (with --run) Start even if there are pending jobs (*.json) and process
                        them in order. Without this option, if there are any it exits with code 1.
  -w, --workdir DIR     (with --run) Working directory of the CLI. Must exist.
                        Default: the queue's parent directory.
  --runner auto|codex|claude
                        CLI of the queue. With auto it is inferred from the queue name (gpt or
                        codex → codex, claude → claude) or, without --run, from the runner
                        running on it.
  -r, --refresh SECONDS Screen refresh interval (default 15; also with /).
  -n, --jobs N          Job rows visible at once (1-50, default 5; also with /);
                        the list scrolls.
  -p, --protocol FILE   Protocol file (e.g. PROTOCOL.md) declaring the verdict line of the
                        answers, such as `VERDICT: READY | VERDICT: CHANGES REQUESTED` inside
                        a ``` block, the OK one first (green) and the KO one last (red). Shows
                        each job's verdict, and key p shows the file. Without this option no
                        verdict is shown.
  -h, --help            Show the help and exit.

Exit codes at startup:
  1  Pending jobs without --force, the queue is a file or does not exist (without --run), the
     working directory does not exist, the CLI is not found, a runner is already on the queue,
     or the --protocol file declares no verdict line.
  2  Unknown option, or --force / --workdir without --run.
```

| Queue | CLI | `model` and `effort` values |
|---|---|---|
| `...\gpt-queue` | `codex` (OpenAI) | `codex exec --model` and `model_reasoning_effort` |
| `...\claude-queue` | `claude` (Claude Code) | `claude --model` and `claude --effort` (`low`, `medium`, `high`, `xhigh`, `max`) |

## Requirements

- [uv](https://docs.astral.sh/uv/). The monitor's dependencies (typer, textual, psutil) are declared inside the script and uv installs them on its own the first time.
- With `--run`, the matching CLI on the `PATH` and logged in. On Windows it must be a real executable (`codex.exe`/`codex.cmd`, `claude.exe`/`claude.cmd`), as installed by npm or the official installer. On WSL you need a Linux install of the CLI: the npm `codex` installed on Windows shows up on the WSL `PATH` but launches the Windows binary and fails.

## Running

```powershell
uv run ai-task-commander.py C:\Claude\HomeAssistant\gpt-queue --run           # monitor + runner
uv run ai-task-commander.py C:\Claude\HomeAssistant\gpt-queue --run --force   # and process pending jobs
uv run ai-task-commander.py D:\tests\claude-queue --run -w D:\tests
uv run ai-task-commander.py C:\Claude\HomeAssistant\gpt-queue                 # view only
uv run ai-task-commander.py C:\Claude\HomeAssistant\gpt-queue -p PROTOCOL.md  # with RP/1 verdicts
```

```bash
uv run ai-task-commander.py ~/Claude/HomeAssistant/gpt-queue --run
```

## Screen

At the top, all the jobs in the queue, newest first, in a table of N visible rows that scrolls: state (`OK`, `ERR`, `RUNNING` or `QUEUED`), model, effort, queued time, start, end and duration. With `--protocol FILE`, each job's review verdict: the last non-empty line of the `.md` when it is exactly one of the verdict lines the file declares. They are declared inside a ``` block as alternatives with the same label (in `PROTOCOL.md`, `VERDICT: READY | VERDICT: CHANGES REQUESTED`, the last line of RP/1 answers); no other form is inferred. The verdict appears in the last column, *Verdict*, and in the output panel title (`Output · issue94-terra-3.md · CHANGES REQUESTED`), in green the first declared alternative (the OK one, `READY`), in red the last one (the KO one, `CHANGES REQUESTED`) and in yellow any in between; jobs without a verdict show `—`, and if no job has one the column is hidden. Without `--protocol` there is no column and no verdict in the title. Below, the selected job's request (left) and the `.md` and `.err` (right). With `--run`, the top bar shows `▶ RUNNER` and the working directory, and each job start and end appears as a notification (`▶ Started`, `✔ ... OK`, `✖ ... ERROR`). The status line shows the runner, how long it has been running (and the current job), the monitor's PID (with `--run`, the same as in `ai-task-commander.pid`), the refresh interval and the queue path. On the right of the key bar, `⤒ ON/OFF` and `md ON/OFF` show whether the selection follows new jobs and whether Markdown is rendered.

| Key | Action |
|---|---|
| `↑` `↓` `PgUp` `PgDn` `Home` `End`, `1`-`9` | Select a job (the list scrolls through the whole queue) |
| `Tab`, `←` `→` | Switch panel; `↑` `↓` `PgUp` `PgDn` `Home` `End` scroll the active panel |
| `z` / `Esc` | Maximize the active panel / restore |
| `s` | With a maximized panel, show it next to another one: from *Request*, the request and the output (`.md`); from *Output* or *Session log*, the output and the log (`.err`). `Tab` or `←` `→` switch between the two and `s` returns to the single panel |
| `f` | Toggle *Follow new jobs*: when a new job enters the queue, the selection jumps to the newest |
| `m` | Rendered Markdown or raw text |
| `p` | Only with `--protocol`: show the protocol file (scrollable; `Esc`, `p` or `q` close it) |
| `r` | Refresh now |
| `/` | Settings (for the session only): refresh interval in seconds, visible job rows (`-n`) and *Follow new jobs* (when a new job enters the queue, the selection jumps to the newest; off by default). `Tab` switches field, `Space` toggles the checkbox, `Enter` applies |
| `q` | Quit. With a job running it asks for confirmation: `y` stops the CLI and the job stays pending |

Without `--run`, the monitor detects, through the lock file, the `--run` monitor that is running the queue and shows how long it has been running.

## Queue directory

When starting with `--run`, the monitor makes these checks before opening the screen:

| Path state | Behaviour |
|---|---|
| Does not exist | The directory is created and it starts. |
| Exists with no pending jobs | Shows the count of done and pending jobs and starts. |
| Exists with pending jobs | Warning with the names of the pending `.json` files and exit with code `1`. With `--force` the warning is shown, it starts and they are processed. |
| Exists with `.json` files that are not jobs | A warning for each file; they do not count as pending and do not block startup. |
| A runner is already running on it (another `--run`, an old watcher, or a `.pid` of a live process) | Error and exit with code `1`. |
| Exists but is a file | Error and exit with code `1`. |

Only `*.json` files that are valid jobs count as pending. The history (`*.json.done`, `*.status`, `*.md`, `*.err`) is kept and does not block startup. No existing directory is ever deleted or overwritten.

**Queue contract:** the queue directory only holds jobs, their history, the `<name>.pid` of the running job, and the runner's lock and `ai-task-commander.pid`. Files a job needs (data, diffs, long prompts) go elsewhere inside the working directory, or in the queue only if they don't have a `.json` extension. A `.json` that is not a job is warned about once and ignored until it changes; this is a safety net, not an invitation to store data there.

## Job format

A `<name>.json` file inside the queue, encoded in UTF-8:

```json
{
  "model": "gpt-5.5",
  "effort": "medium",
  "prompt": "Request text"
}
```

`model` and `effort` may only contain letters, digits and `._:/@+[]-`, because they end up on the CLI's command line. The `prompt` has no limit: it is sent to the CLI through standard input, in UTF-8.

Jobs are processed one at a time, in modification-date order, checking the queue every 5 seconds. A `<name>.json` that is not valid JSON, is empty, is missing a field or has disallowed characters shows up on screen as `ERR`, is notified once and left in the queue without renaming; it is retried when it is modified.

`claude` is run with `claude -p` (non-interactive mode): tools that would need confirmation are denied, so if the job has to modify files you need to set up permissions in the working directory's `settings.json`.

## Output files

| File | Contents |
|---|---|
| `<name>.status` | Job state in JSON (see [.status file](#status-file)). If it does not exist, the job is queued. |
| `<name>.md` | The model's final answer. With `codex`, if the job fails it is not created. With `claude`, if the job fails it contains the CLI's error message. |
| `<name>.err` | The CLI's standard error, as written. With `codex` it always has content (the session log) and, on failure, the `ERROR: ...` line at the end. With `claude` it is empty if everything went well. |
| `<name>.json.done` | The original job, renamed when it finishes. |
| `<name>.pid` | Only while the job runs: the PID of the CLI process (see [.pid file](#pid-file)). |
| `.monitor-runner.lock` | Only while a monitor with `--run` is running: PID and runner data. Prevents a second one from starting. |
| `ai-task-commander.pid` | Only while a monitor with `--run` is running: the monitor's PID, just the number on one line (like [`<name>.pid`](#pid-file)). Deleted on exit; if the monitor dies abruptly it stays, and the next `--run` overwrites it. That is why `ai-task-commander` is not a valid job name: an `ai-task-commander.json` shows up as `ERR` and is not run. |

### .status file

JSON in UTF-8, written atomically (temporary file and rename), so a reader never sees a half-written version:

```json
{
  "job": "review-v3",
  "state": "OK",
  "reason": "exit code 0",
  "exit_code": 0,
  "queued": "2026-11-10T08:33:07+01:00",
  "start": "2026-11-10T08:49:29+01:00",
  "stop": "2026-11-10T08:55:39+01:00",
  "duration_s": 370.2,
  "runner": "codex",
  "model": "gpt-5.5",
  "effort": "high",
  "pid": 12345,
  "cli": "C:\\Users\\...\\npm\\codex.cmd",
  "workdir": "C:\\Claude\\HomeAssistant",
  "host": "PC",
  "runner_pid": 6789,
  "output": "review-v3.md",
  "errors": "review-v3.err"
}
```

| Field | Value |
|---|---|
| `state` | `RUNNING` while the CLI works; `OK` or `ERR` when it finishes. |
| `reason` | `exit code 0`, `exit code N`, `exit code 0 but <name>.md was not written` or `cannot start the CLI: ...`. |
| `exit_code` | The CLI's exit code (`null` while running). `OK` means code `0` and `<name>.md` written. |
| `queued`, `start`, `stop` | ISO 8601 times with time zone: entry into the queue (date of the `.json`), start and end of the CLI. |
| `duration_s` | Duration in seconds. |
| `pid`, `runner_pid`, `host` | PID of the CLI, PID of the monitor running it, and machine name. |
| `inferred` | Optional. `true` in a `.status` generated afterwards from the files (jobs from the old watchers): the monitor marks the times as approximate. |

Order of changes: on start the `.status` (`RUNNING`) and the `.pid` are written; on finish the `.pid` is deleted, the final `.status` is written and the `.json` is renamed to `.json.done`. So if `<name>.json.done` exists, the `.status` is already final.

If you quit the monitor with a job running, the CLI is stopped, the `.pid` and the `.status` are deleted and the `.json` stays pending: it will run again when starting with `--run --force`. If the monitor dies abruptly (window closed, process killed, power loss), the `.status` stays at `RUNNING` and the `.json` pending. The CLI runs detached from the window and may keep working until it finishes: while its `pid` exists, the job shows as `RUNNING` and a new `--run` won't start; once the process no longer exists, it shows as `QUEUED` with the `stale` warning and will run again with `--force`.

#### Jobs without .status

Finished jobs without a `.status` (done with the old watchers) are also shown, but the state and times are inferred from the files and marked as approximate: with `codex` it is `OK` if there is a `.md`; with `claude`, if the `.md` has content, the `.err` is empty and the `.md` is not a short CLI error message (starting with `API Error` or `Error:`). The start is the creation date of the `.err` (not available on Linux) and the end, the last modification of the `.md` or the `.err`.

### .pid file

While the CLI works, `<name>.pid` holds its PID: just the number, on one line. It is the PID of the child process: on Windows, if the CLI is an npm `codex.cmd`/`claude.cmd`, it is that of the wrapping `cmd.exe`, which lives as long as the CLI. To check whether it still exists:

```powershell
Get-Process -Id (Get-Content C:\Claude\HomeAssistant\gpt-queue\review-v3.pid) -ErrorAction SilentlyContinue
```

```bash
kill -0 "$(cat ~/Claude/HomeAssistant/gpt-queue/review-v3.pid)" 2>/dev/null && echo "running" || echo "process does not exist"
```

PIDs are reused: an old `.pid` could by chance point to another process. If needed, check its name (`Get-Process` shows it; on Linux, `ps -o comm= -p <pid>`).

## Use from Claude Code

1. Claude writes a `<name>.json` to the queue.
2. It waits for `<name>.status` to say `OK` or `ERR` (or for `<name>.json.done` to appear). Meanwhile, if `state` is `RUNNING` and the `pid` exists, the CLI is still working.
3. If it is `OK`, it reads `<name>.md`. If it is `ERR`, `reason` gives the cause and the end of `<name>.err` the details.

If about 20 seconds after writing the `.json` there is no `<name>.status`, the runner is not running or has rejected the job (check the monitor screen). With Windows paths inside the `prompt`, watch out for escape sequences (`\a`, `\b`, `\n`, `\t`...): a path like `gpt-queue\aitc-...` written in a non-raw string ends up with a control character instead of `\a`. Using `/` slashes in paths avoids this.

### Example CLAUDE.md instructions

To have Claude Code delegate work to GPT autonomously, add a section like this to the project's `CLAUDE.md`. Adjust the queue path to the one the monitor uses.

````markdown
## Delegating work to GPT (codex) via the queue

When I ask for a second opinion, a review or an analysis from GPT, delegate it
to the codex queue instead of answering it yourself:

1. Write a job file to `C:/Claude/HomeAssistant/gpt-queue/<name>.json` with
   the Write tool (so it is UTF-8; PowerShell's `Set-Content` or `>` don't
   guarantee it). Use a unique name, for example `aitc-<topic>`. Contents:

   ```json
   {
     "model": "gpt-5.5",
     "effort": "high",
     "prompt": "Request text"
   }
   ```

   codex runs in the runner's working directory, so in the `prompt` you can
   refer to files by relative path. If you need to pass it a lot of text or
   data, put it in a separate file inside the working directory (never another
   `.json` in the queue) and mention the path in the `prompt`.

2. Wait by reading `C:/Claude/HomeAssistant/gpt-queue/<name>.status` (JSON):

   - `"state": "RUNNING"` and the `pid` exists (in PowerShell,
     `Get-Process -Id <pid>` doesn't error): the job is running, keep
     waiting with no time limit.
   - `"state": "RUNNING"` but the `pid` doesn't exist: the runner died mid-job.
     Stop and tell me so I can restart it with `--run --force`.
   - After about 20 seconds there is still no `<name>.status`: stop and tell me,
     the runner is not running or has rejected the job.

3. When `state` is `OK`, read `<name>.md`: it is GPT's answer. If it is
   `ERR`, tell me the `reason` and the `ERROR:` line at the end of `<name>.err`.
````

The `claude-queue` queue works the same way, with Claude Code's `model` and `effort` values.
