# Subprocesses

Some plugins need to run an external program and keep it alive for the life of the plugin — a local model server, a media tool (mpv, ffmpeg), a protocol bridge, a helper binary in another language. Sapphire ships **`ProcessManager`** (`core/process_manager.py`) for exactly this: launch a child process, optionally supervise it, and kill it cleanly — including its whole process group — on unload, hot-reload, or shutdown.

Use it instead of calling `subprocess.Popen` yourself. Rolling your own teardown is where the orphaned-process and stuck-port bugs come from; `ProcessManager` already handles the cross-platform corner cases.

## Quick Start

Spawn the process when your plugin loads, tear it down when it unloads:

```python
# daemon.py — managing an external server process
from pathlib import Path
from core.process_manager import ProcessManager

_proc = None


def start(plugin_loader, settings):
    global _proc
    base_dir = Path(__file__).absolute().parents[2]   # sapphire project root (.absolute, not .resolve — symlink-safe)
    server = Path(__file__).parent / "bin" / "my-server"

    _proc = ProcessManager(
        script_path=server,
        log_name="my-plugin",        # → user/logs/my-plugin.log
        base_dir=base_dir,
        command_args=[str(server), "--port", "9876"],
    )
    _proc.start()
    _proc.monitor_and_restart(check_interval=10)   # optional: auto-restart if it dies


def stop():
    global _proc
    if _proc:
        _proc.stop()                  # SIGTERM the whole group, then SIGKILL
        _proc = None
```

That's the whole contract: `start()` in your plugin's `start()`, `stop()` in your plugin's `stop()`. Because daemons survive hot-reload (`stop()` then `start()` again), the child is rebuilt cleanly each reload instead of leaking.

## API

### `ProcessManager(script_path, log_name, base_dir, command_args=None, env_callback=None)`

| Param | Type | Description |
|-------|------|-------------|
| `script_path` | `Path` | The script or binary to run. Must be an existing file (point it at the actual path, not a bare PATH name). |
| `log_name` | `str` | Log file name — output goes to `user/logs/{log_name}.log` (truncated on each start). |
| `base_dir` | `Path` | Project root, used for log placement. |
| `command_args` | `list` | Optional full command + args to run. If omitted, runs `script_path` directly. |
| `env_callback` | `callable` | Optional zero-arg callable returning an `env` dict. Called fresh on **every** start (including monitor restarts), so settings-derived env vars stay current. |

If `script_path` ends in `.py`, the current Python interpreter is prepended automatically. For any other file the executable bit is set (Unix) and it's run directly. Use `command_args` when you need flags or a specific argv.

### Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `.start()` | `bool` | Spawn the process. Returns `False` if the file is missing or spawn fails. |
| `.stop()` | — | Stop the monitor and kill the process. **Unix:** `SIGTERM` the process group, wait 10s, then `SIGKILL`. **Windows:** `terminate()`, wait 10s, then `kill()`. |
| `.is_running()` | `bool` | Whether the child is currently alive. |
| `.monitor_and_restart(check_interval=10)` | — | Start a background thread that restarts the process if it exits. Call once, after `start()`. |

### `kill_process_on_port(port)`

Module-level helper (`from core.process_manager import kill_process_on_port`) that kills whatever is listening on a TCP port. Returns `True` if it killed something. Handy to reclaim a fixed port before `start()` in case a previous run was orphaned.

## Why ProcessManager and not raw Popen

It handles the things that bite you on teardown and reload:

- **Process-group kill (Unix).** The child is launched in a new session (`setsid`), and `stop()` kills the entire group — so a child that forks its own workers doesn't leave orphans.
- **Die-with-parent (Linux).** `PR_SET_PDEATHSIG` means the child receives `SIGTERM` if Sapphire itself dies unexpectedly — no zombies after a hard crash.
- **Cross-platform stop.** Process groups on Unix, `terminate()`→`kill()` on Windows.
- **Optional supervision.** `monitor_and_restart` brings the process back if it falls over.
- **Fresh env per spawn.** `env_callback` lets you inject current settings on every (re)start without monkey-patching.

## Advanced: async stdio streaming

If — and only if — your daemon needs to drive a child process over **async stdio pipes on its own event loop** (stream stdout turn-by-turn rather than launch-and-supervise), there is an advanced helper at `core/async_teardown.py` (`reap_subprocess`, `close_event_loop`) for tearing down an `asyncio` subprocess and its loop without leaking pipes or emitting `Event loop is closed` noise.

This path is advanced and currently **Linux/macOS-oriented** — `asyncio` subprocesses don't run under the `WindowsSelectorEventLoopPolicy` Sapphire sets at startup. Prefer `ProcessManager` for everything else; the async helper is expected to fold into `ProcessManager` as a supported mode later, so treat its API as not-yet-frozen.

## Reference for AI

SUBPROCESS MANAGEMENT:
- Use `core.process_manager.ProcessManager` to run an external program from a plugin — do not call `subprocess.Popen` directly.
- Construct with `ProcessManager(script_path, log_name, base_dir, command_args=None, env_callback=None)`; `.py` paths get the Python interpreter prepended, otherwise the file is run directly or via `command_args`.
- Methods: `.start() -> bool`, `.stop()` (Unix: process-group SIGTERM→SIGKILL; Windows: terminate→kill), `.is_running() -> bool`, `.monitor_and_restart(check_interval=10)` for auto-restart.
- `kill_process_on_port(port)` reclaims a TCP port from an orphaned process.
- Wire `.start()` into the plugin's `start()` and `.stop()` into `stop()`; the child is rebuilt cleanly across hot-reload.
- Unix safety: child runs in a new session (`setsid`) with `PR_SET_PDEATHSIG`, so process-group kill reaps children and the child dies if Sapphire dies.
- Logs go to `user/logs/{log_name}.log`, truncated each start. `env_callback` supplies a fresh env dict on every spawn.
- Advanced only: `core/async_teardown.py` (`reap_subprocess`, `close_event_loop`) tears down an asyncio subprocess + its loop for daemons that stream child stdio on their own event loop. Linux/macOS-oriented (asyncio subprocess is unavailable under the Windows Selector loop policy). Not-yet-frozen; prefer ProcessManager.
