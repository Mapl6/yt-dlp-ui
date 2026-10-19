"""Settings: every field saved, kept across a restart, sent through the API, and bad values coerced."""
import json

import pytest

from ytdl import manager as M
from helpers import stop_manager

VALUES = {"concurrency": 5, "cookies_browser": "firefox", "cookies_profile": "abc.default-release",
          "rate_limit_mb": 1.5, "sleep_between": 3.0, "proxy": "http://127.0.0.1:9", "wait_for_network": False,
          "total_rate_mb": 4.0, "schedule_on": True, "schedule_start": "02:30", "schedule_end": "06:15",
          "sub_langs": "en, fa", "notify": True}


def all_values(tmp_path):
    return {"output_dir": str(tmp_path / "elsewhere"), **VALUES}


def test_values_cover_every_setting(tmp_path):
    values = all_values(tmp_path)
    assert set(values) == set(M.DEFAULT_SETTINGS), set(values) ^ set(M.DEFAULT_SETTINGS)
    assert all(values[k] != M.DEFAULT_SETTINGS[k] for k in values)


def test_every_setting_survives_a_restart(tmp_path, make_manager):
    data = tmp_path / "lib"
    m = make_manager(data)
    values = all_values(tmp_path)
    assert m.update_settings(values) == values
    stop_manager(m)                                  # quit …
    m2 = make_manager(data)                          # … and start again on the same library
    assert m2.settings == values
    assert json.loads((data / "settings.json").read_text()) == values


def test_unknown_settings_ignored(tmp_path, make_manager):
    m = make_manager(tmp_path / "lib")
    saved = m.update_settings({"concurrency": 2, "evil": "x", "__class__": 1})
    assert "evil" not in saved and "__class__" not in saved and saved["concurrency"] == 2
    assert "evil" not in json.loads(M.SETTINGS_FILE.read_text())


def test_damaged_settings_file_falls_back(tmp_path, make_manager):
    data = tmp_path / "lib"
    data.mkdir()
    (data / "settings.json").write_text("{not json")
    m = make_manager(data)
    assert m.settings == M._coerce_settings({})


COERCE = [
    ({"concurrency": "2.7"}, "concurrency", 2),
    ({"concurrency": 0}, "concurrency", 1),
    ({"concurrency": 99}, "concurrency", M.MAX_WORKERS),
    ({"concurrency": None}, "concurrency", 3),
    ({"rate_limit_mb": -4}, "rate_limit_mb", 0.0),
    ({"rate_limit_mb": "2"}, "rate_limit_mb", 2.0),
    ({"total_rate_mb": "lots"}, "total_rate_mb", 0.0),
    ({"wait_for_network": "false"}, "wait_for_network", False),
    ({"wait_for_network": "yes"}, "wait_for_network", True),
    ({"notify": 1}, "notify", True),
    ({"notify": "0"}, "notify", False),
    ({"schedule_on": "on"}, "schedule_on", True),
    ({"proxy": "  socks5://127.0.0.1:1080  "}, "proxy", "socks5://127.0.0.1:1080"),
    ({"proxy": None}, "proxy", ""),
    ({"output_dir": ""}, "output_dir", M.DEFAULT_SETTINGS["output_dir"]),
    ({"schedule_start": "7:05"}, "schedule_start", "07:05"),
    ({"schedule_end": "25:00"}, "schedule_end", "07:00"),
    ({"schedule_start": "noon"}, "schedule_start", "01:00"),
]


@pytest.mark.parametrize("raw,key,want", COERCE, ids=[f"{k}={r[k]!r}" for r, k, _ in COERCE])
def test_coerce(raw, key, want):
    assert M._coerce_settings(raw)[key] == want


def test_coerced_settings_are_complete():
    assert set(M._coerce_settings({"junk": 1})) == set(M.DEFAULT_SETTINGS)
