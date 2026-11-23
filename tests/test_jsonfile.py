import json
import os

import jsonfile


def test_write_is_atomic_and_keeps_permissions(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{}")
    os.chmod(path, 0o640)
    jsonfile.write(path, {"a": "ö"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"a": "ö"}
    assert path.stat().st_mode & 0o777 == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]  # no temp files left


def test_read_missing_or_empty(tmp_path):
    assert jsonfile.read(tmp_path / "nope.json", default={}) == {}
    (tmp_path / "empty.json").write_text("  \n")
    assert jsonfile.read(tmp_path / "empty.json", default=[]) == []


def test_data_path_copies_legacy_file_once(tmp_path, monkeypatch):
    app, data = tmp_path / "app", tmp_path / "data"
    app.mkdir()
    (app / "state.json").write_text('{"old": true}')
    monkeypatch.setattr(jsonfile, "APP_DIR", app)
    monkeypatch.setattr(jsonfile, "DATA_DIR", data)
    path = jsonfile.data_path("state.json")
    assert json.loads(path.read_text()) == {"old": True}
    path.write_text('{"new": true}')
    jsonfile.data_path("state.json")  # existing data/ copy wins
    assert json.loads(path.read_text()) == {"new": True}
