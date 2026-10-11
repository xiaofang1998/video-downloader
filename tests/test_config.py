"""配置读写的边界情况。"""

from __future__ import annotations

import json

from xydl.config import Config


def test_load_accepts_utf8_bom(tmp_path):
    """Windows 记事本存「UTF-8」会带 BOM，带 BOM 的 config.json 必须能读。

    真踩过：用户改了一下配置就起不来服务，报 ``Unexpected UTF-8 BOM``。
    """
    path = tmp_path / "config.json"
    path.write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"server": {"port": 9000}}).encode("utf-8"))

    cfg = Config.load(path)
    assert cfg.get("server.port") == 9000
    assert cfg.get("server.host") == "127.0.0.1", "其余字段仍用默认值补全"


def test_load_without_bom_still_works(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"server": {"port": 9001}}), encoding="utf-8")
    assert Config.load(path).get("server.port") == 9001


def test_load_creates_default_file_when_missing(tmp_path):
    path = tmp_path / "sub" / "config.json"
    cfg = Config.load(path)
    assert path.exists()
    assert cfg.get("server.port") == 8765
    # 写出去的文件不能带 BOM，否则下一次读回来还得靠 utf-8-sig
    assert path.read_bytes()[:3] != b"\xef\xbb\xbf"


def test_save_and_load_roundtrip(tmp_path):
    path = tmp_path / "config.json"
    cfg = Config({"download": {"series": {"max_episodes": 12}}}, path=path)
    cfg.save()
    assert Config.load(path).get("download.series.max_episodes") == 12


def test_deep_merge_keeps_sibling_defaults(tmp_path):
    cfg = Config({"download": {"series": {"max_episodes": 5}}})
    assert cfg.get("download.series.max_episodes") == 5
    assert cfg.get("download.series.mode") == "zip", "同级默认值不该被覆盖掉"
    assert cfg.get("download.workers") == 2
