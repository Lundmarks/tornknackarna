"""PlayerMap: keyed by applicationUserID, migrating old name keys."""

import json

from player_map import PlayerMap

UUID = "d88f7630-526d-4b73-871e-bd6703175329"


def member(name="Bängke", uuid=UUID):
    return {"inGameName": name, "applicationUserID": uuid.upper()}


def test_name_keyed_entry_moves_under_uuid(tmp_path):
    path = tmp_path / "player_map.json"
    path.write_text(json.dumps({"Bängke": {"account_id": 123, "confirmed": True}}))
    pm = PlayerMap.load(path)
    assert pm.account_id(member()) == 123
    assert pm.confirmed(member())
    pm.save()
    saved = json.loads(path.read_text())
    assert list(saved) == [UUID]
    assert saved[UUID]["name"] == "Bängke"


def test_rename_keeps_steam_id(tmp_path):
    pm = PlayerMap({}, tmp_path / "pm.json")
    pm.set(member("Old name"), 555)
    assert pm.account_id(member("New name")) == 555


def test_confirmed_entry_is_never_overwritten(tmp_path):
    pm = PlayerMap({UUID: {"account_id": 1, "confirmed": True}}, tmp_path / "pm.json")
    assert not pm.set(member(), 2)
    assert pm.account_id(member()) == 1


def test_missing_and_account_ids(tmp_path):
    pm = PlayerMap({UUID: {"account_id": 42}}, tmp_path / "pm.json")
    other = member("Other", "00000000-0000-0000-0000-000000000001")
    assert pm.missing([member(), other]) == [other]
    assert pm.account_ids([member(), other]) == {"42"}


def test_save_only_writes_when_changed(tmp_path):
    path = tmp_path / "pm.json"
    PlayerMap({}, path).save()
    assert not path.exists()
