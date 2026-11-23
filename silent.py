"""
silent.py - run a real bot job without any visible effect.

`bot.py --silent` (and `/debug run`) use this. It must be activated before the
project modules are imported, because they resolve data/ paths on import.

  - state.json, player_map.json and ready_check.db are copied to a throwaway
    folder, so nothing is saved and the ready service never sees queued work.
  - Every HTTP request that isn't a read is blocked at the httpx level and
    recorded, so no Discord message, ping, event or gist write can leave.

Reads (E-Sparven, OpenDota, Discord event status) still happen, so the job
runs on live data.
"""

import atexit
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import httpx

READ_METHODS = {"GET", "HEAD", "OPTIONS"}
STATE_FILES = ("state.json", "player_map.json")
REPORT_NAME = "report.json"
LOG_LIMIT = 200

_actions: list[str] = []
_logs: list[str] = []
_queued_before: set[str] = set()
_work_dir: Path | None = None
_real_send = None
_real_async_send = None


def active() -> bool:
    return _work_dir is not None


def describe(request: httpx.Request) -> str | None:
    """What a blocked request would have done. None for an internal step."""
    try:
        body = json.loads(request.content or b"{}")
    except ValueError:
        body = {}
    path = urlparse(str(request.url)).path

    if path.endswith("/messages"):
        embeds = body.get("embeds") or [{}]
        title = embeds[0].get("title") or "message"
        pings = bool(body.get("content") and (body.get("allowed_mentions") or {}).get("roles"))
        return f"Post: {title} ({'pings the role' if pings else 'no ping'})"
    if "/scheduled-events" in path:
        if request.method == "POST":
            return f"Create event: {body.get('name', 'match')}"
        event_id = path.rsplit("/", 1)[-1]
        if body.get("status") == 2:
            return None  # Discord's required step on the way to completed
        if body.get("status") == 3:
            return f"Mark event {event_id} as completed"
        return f"Move event {event_id} to {body.get('scheduled_start_time', '?')}"
    if path.startswith("/gists"):
        name = str(body.get("description", "data")).removeprefix("tornknackarna: ")
        return f"{'Create' if request.method == 'POST' else 'Update'} gist: {name}"
    return f"{request.method} {request.url.host}{path}"


def _blocked_response(request: httpx.Request) -> httpx.Response:
    action = describe(request)
    if action:
        _actions.append(action)
    return httpx.Response(200, json={"id": f"silent-{len(_actions)}"}, request=request)


def install_guard() -> None:
    global _real_send, _real_async_send
    if _real_send is not None:
        return
    _real_send, _real_async_send = httpx.Client.send, httpx.AsyncClient.send
    real_send = _real_send

    def guarded_send(self, request, **kwargs):
        if request.method in READ_METHODS:
            return real_send(self, request, **kwargs)
        return _blocked_response(request)

    async def guarded_async_send(self, request, **kwargs):
        if request.method in READ_METHODS:
            return await _real_async_send(self, request, **kwargs)
        return _blocked_response(request)

    httpx.Client.send = guarded_send
    httpx.AsyncClient.send = guarded_async_send


def uninstall_guard() -> None:
    global _real_send, _real_async_send
    if _real_send is not None:
        httpx.Client.send, httpx.AsyncClient.send = _real_send, _real_async_send
        _real_send = _real_async_send = None


def _copy_database(source: Path, target: Path) -> None:
    """SQLite's backup API gives a consistent copy while the ready bot writes."""
    src = sqlite3.connect(f"{source.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def activate() -> Path:
    """Move this process onto throwaway data and block all writes."""
    global _work_dir
    if "jsonfile" in sys.modules:
        raise RuntimeError("silent.activate() must run before the project modules are imported")

    real_data = Path(os.environ.get("DATA_DIR") or Path(__file__).parent / "data")
    real_db = Path(os.environ.get("READY_DB_PATH") or real_data / "ready_check.db")

    if os.environ.get("SILENT_WORK_DIR"):
        work = Path(os.environ["SILENT_WORK_DIR"])  # the caller removes it
    else:
        work = Path(tempfile.mkdtemp(prefix="tornknackarna-silent-"))
        atexit.register(shutil.rmtree, work, ignore_errors=True)
    sandbox = work / "data"
    sandbox.mkdir(parents=True, exist_ok=True)

    # The guard goes first, so a failed copy can never leave writes enabled.
    install_guard()
    for name in STATE_FILES:
        if (real_data / name).exists():
            shutil.copyfile(real_data / name, sandbox / name)
    if real_db.exists():
        _copy_database(real_db, sandbox / "ready_check.db")

    os.environ["DATA_DIR"] = str(sandbox)
    os.environ["READY_DB_PATH"] = str(sandbox / "ready_check.db")
    _work_dir = work
    return work


class _ProblemLog(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        _logs.append(f"{record.levelname}: {record.getMessage()}"[:LOG_LIMIT])


def _queued_work() -> set[str]:
    """Ready checks and alerts the ready service would post from this database."""
    import ready_check as rc

    if not rc.enabled():
        return set()
    conn = rc.connect()
    try:
        now = int(__import__("time").time())
        posts = {f"Post: ready check vs {m['opponent']} (pings the role)" for m in rc.meetings_to_post(conn, now)}
        alerts = {
            f"Post: {a['kind'].replace('_', ' ')} alert vs {a['opponent']} (revision {a['revision']})"
            for a in rc.pending_announcements(conn)
        }
        return posts | alerts
    finally:
        conn.close()


def begin() -> None:
    """Call right before the job, after the project modules are imported."""
    global _queued_before
    logging.getLogger().addHandler(_ProblemLog(level=logging.WARNING))
    _queued_before = _queued_work()


def finish() -> dict:
    """Build the report and save it next to the sandbox for the caller."""
    report = {"actions": _actions + sorted(_queued_work() - _queued_before), "problems": list(_logs)}
    if _work_dir is not None:
        (_work_dir / REPORT_NAME).write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
    return report


def format_report(job: str, report: dict, limit: int = 3900) -> str:
    """The report as Discord markdown, cut to fit one embed."""
    actions = report.get("actions") or []
    problems = report.get("problems") or []
    lines = ["Nothing was posted, pinged, saved or changed.", "", f"**Would do ({len(actions)})**"]
    lines += [f"• {a}" for a in actions] or ["Nothing to do right now."]
    if problems:
        lines += ["", f"**Warnings and errors ({len(problems)})**"]
        lines += [f"• {p}" for p in problems]

    text = ""
    for index, line in enumerate(lines):
        more = f"\n…and {len(lines) - index} more lines"
        if len(text) + len(line) + 1 + len(more) > limit:
            return text + more
        text += f"\n{line}" if text else line
    return text
