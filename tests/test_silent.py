import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

import silent

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def guard(monkeypatch):
    sent = []
    monkeypatch.setattr(httpx.Client, "send", lambda self, request, **kw: sent.append(request) or httpx.Response(200, request=request))
    monkeypatch.setattr(silent, "_actions", [])
    silent.install_guard()
    yield sent
    silent.uninstall_guard()


def test_writes_are_blocked_and_reads_pass(guard):
    role = {"content": "<@&1>", "allowed_mentions": {"roles": ["1"]}, "embeds": [{"title": "Match day"}]}
    assert httpx.post("https://discord.com/api/v10/channels/1/messages", json=role).status_code == 200
    httpx.patch("https://api.github.com/gists/abc", json={"description": "tornknackarna: index"})
    httpx.delete("https://discord.com/api/v10/channels/1/messages/2")
    assert guard == []
    assert silent._actions[0] == "Post: Match day (pings the role)"
    assert silent._actions[1] == "Update gist: index"
    assert len(silent._actions) == 3
    httpx.get("https://example.com/")
    assert [r.method for r in guard] == ["GET"]


def test_event_completion_is_one_action(guard):
    url = "https://discord.com/api/v10/guilds/1/scheduled-events/9"
    httpx.patch(url, json={"status": 2})
    httpx.patch(url, json={"status": 3})
    assert silent._actions == ["Mark event 9 as completed"]


def test_sandbox_leaves_real_data_alone(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (real / "state.json").write_text('{"meetings": {}, "index_bin_id": "keep"}')
    code = (
        "import silent; silent.activate()\n"
        "import jsonfile, ready_check as rc\n"
        "jsonfile.write(jsonfile.data_path('state.json'), {'changed': True})\n"
        "c = rc.connect(); rc.register_meeting(c, 1, 'X', 9999999999); c.close()\n"
    )
    env = {k: v for k, v in os.environ.items() if k != "READY_DB_PATH"}
    env["DATA_DIR"] = str(real)
    con = sqlite3.connect(real / "ready_check.db")
    con.execute("CREATE TABLE marker (x)")
    con.commit()
    con.close()
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True)
    assert json.loads((real / "state.json").read_text())["index_bin_id"] == "keep"
    con = sqlite3.connect(real / "ready_check.db")
    assert [r[0] for r in con.execute("SELECT name FROM sqlite_master")] == ["marker"]
    con.close()


def test_silent_needs_a_one_shot_job(tmp_path):
    env = {**os.environ, "DATA_DIR": str(tmp_path)}
    result = subprocess.run([sys.executable, "bot.py", "--silent"], cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 2
    assert "--silent needs" in result.stderr


def test_report_fits_an_embed():
    text = silent.format_report("sync", {"actions": [f"Post: thing {i}" for i in range(1000)], "problems": []})
    assert len(text) <= 3900
    assert "more lines" in text
    assert "Nothing to do" in silent.format_report("sync", {"actions": []})
