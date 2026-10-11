"""视频号解密（ISAAC64 + 前 128KiB XOR）的回归测试。

这里的断言用的是**真实样本**（公开项目 Evil0ctal/WeChat-Channels-Video-File-Decryption
的 ``wx_encrypted.mp4``，``decode_key=2136343393``）取出来的黄金向量：

* 密钥流前 16 字节
* 密文前 64 字节 → 解密后必须以 ``00 00 00 20 66 74 79 70``（``ftyp``）开头

一旦有人"重构" PRNG 的位运算或字节序，这两个断言会立刻炸 —— 这是这个模块
唯一能防住"静默产出花屏视频"的手段。
"""

from __future__ import annotations

import hashlib

import pytest

from xydl import wxdecrypt

#: 真实样本的黄金向量统一放在 xydl/wxdecrypt.py 里（唯一真相源）
GOLDEN_KEY = wxdecrypt.SELFTEST_DECODE_KEY
GOLDEN_KEYSTREAM16 = bytes.fromhex(wxdecrypt.SELFTEST_KEYSTREAM16_HEX)
GOLDEN_CIPHER = bytes.fromhex(wxdecrypt.SELFTEST_CIPHER64_HEX)
GOLDEN_PLAINTEXT = bytes.fromhex(wxdecrypt.SELFTEST_PLAINTEXT32_HEX)
#: 回归钉子：本实现的 131072 字节密钥流摘要，只用来防"重构改坏"。
#: **不构成正确性证明** —— 正确性由真实样本向量（前 64 字节）与 Go 参考实现
#: 的逐行对照保证（见 wxdecrypt 模块 docstring）。
#: 它兜住的是黄金向量覆盖不到的部分：跨 256 词边界后的取数顺序与字节序。
REGRESSION_KEYSTREAM_SHA256 = (
    "49b96d6fc75ba5215fbb773ce98f6b20f6441a7ac40abc9582b1e42c5f3cd9d8"
)
#: 跨越第 256 个词（2048 字节）边界之后的 16 字节，肉眼可读的锚点
REGRESSION_CROSS_BOUNDARY = bytes.fromhex("07cd2de253c1704f6f5594ad72c92d07")


# ── 黄金向量 ──────────────────────────────────────────────────────────
def test_keystream_matches_real_sample():
    """密钥流必须与真实样本逐字节一致（PRNG + 字节序 + 取数顺序）。"""
    assert wxdecrypt.generate_keystream(GOLDEN_KEY, 16) == GOLDEN_KEYSTREAM16


def test_decrypt_head_matches_real_sample():
    stream = wxdecrypt.generate_keystream(GOLDEN_KEY, len(GOLDEN_CIPHER))
    plain = bytes(a ^ b for a, b in zip(GOLDEN_CIPHER, stream))
    assert plain[: len(GOLDEN_PLAINTEXT)] == GOLDEN_PLAINTEXT
    assert wxdecrypt.looks_like_media(plain)


def test_keystream_boundary_anchored_by_real_file_size():
    """用一条**独立约束**锚定 57,600 字节处的密钥流：MP4 的 box 链。

    约束不来自本实现，而来自真实文件的两个事实：
      样本总长 14,088,528；``mdat`` 从偏移 57,610 开始。
    ⇒ 该处的 ``mdat`` 长度字段必须等于 ``14088528 - 57610 = 14030918``。

    为什么这条测试值钱：黄金向量只锚了前 8 个 64 位词，而这里在**第 7200 个词**
    附近 —— 既跨了词边界、也早过了第 256 词时 ``randcnt`` 归零重跑的边界。
    字节序或取数顺序一旦写错，这个长度立刻变成垃圾数。
    """
    slice_offset = wxdecrypt.SELFTEST_CIPHER_SLICE_OFFSET
    cipher = bytes.fromhex(wxdecrypt.SELFTEST_CIPHER_AT_MDAT_HEX)
    # 为了只取到这一小段，需要先算到 slice_offset+len 再切片（ISAAC 是流式的）
    full = wxdecrypt.generate_keystream(GOLDEN_KEY, slice_offset + len(cipher))
    plain = bytes(a ^ b for a, b in zip(cipher, full[slice_offset:]))

    # mdat 头在这段切片里的位置：切片从 57600 起，mdat 在 57610
    rel = wxdecrypt.SELFTEST_MDAT_OFFSET - slice_offset
    assert plain[rel + 4:rel + 8] == b"mdat", \
        f"57,610 处应该是 mdat，实际 {plain[rel + 4:rel + 8]!r}"
    mdat_size = int.from_bytes(plain[rel:rel + 4], "big")
    assert mdat_size == wxdecrypt.SELFTEST_FILE_SIZE - wxdecrypt.SELFTEST_MDAT_OFFSET
    # 切片开头是 moov 的尾巴，紧跟一个 8 字节的 free box
    assert plain[2:10] == b"\x00\x00\x00\x08free"


def test_golden_key_parsed_as_decimal():
    assert wxdecrypt.parse_decode_key(GOLDEN_KEY) == 2136343393
    assert wxdecrypt.parse_decode_key(2136343393) == 2136343393


# ── 种子解析 ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("value", ["", None, "abc", "-5", "1e9", True, "0x10"])
def test_bad_decode_key_rejected(value):
    assert wxdecrypt.parse_decode_key(value) is None


def test_generate_keystream_rejects_bad_key():
    with pytest.raises(ValueError):
        wxdecrypt.generate_keystream("not-a-number", 8)


def test_keystream_length_and_determinism():
    first = wxdecrypt.generate_keystream(1, 1000)
    second = wxdecrypt.generate_keystream(1, 1000)
    assert len(first) == 1000
    assert first == second, "同一个 key 必须产生同一段密钥流"


def test_different_keys_differ():
    assert wxdecrypt.generate_keystream(1, 64) != wxdecrypt.generate_keystream(2, 64)


def test_keystream_longer_than_one_isaac_round():
    """跨越 256 个词（2048 字节）的边界时不能重复/错位。

    黄金向量只锚了前 8 个词，所以这里补一个跨边界锚点 + 全长摘要：
    边界逻辑（``randcnt`` 归零后重跑 ``_isaac64()`` 并从索引 255 重新取数）
    一旦写错，下面两条断言立刻失败。
    """
    stream = wxdecrypt.generate_keystream(GOLDEN_KEY, wxdecrypt.HEAD_BYTES)
    assert len(stream) == wxdecrypt.HEAD_BYTES
    assert stream[2048:2064] == REGRESSION_CROSS_BOUNDARY
    assert hashlib.sha256(stream).hexdigest() == REGRESSION_KEYSTREAM_SHA256
    # 不是"两块一样"这种恒真断言：确实要前后不同
    assert stream[:2048] != stream[2048:]


# ── MP4 头识别 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("head", [
    b"\x00\x00\x00\x20ftypisom",
    b"\x00\x00\x00\x18stypiso6",
    b"\x00\x00\x00\x08moov",
    b"\x00\x00\x00\x08mdat",
    b"\x00\x00\x00\x08wide" + b"ftyp",
])
def test_looks_like_media_accepts_valid_headers(head):
    assert wxdecrypt.looks_like_media(head)


@pytest.mark.parametrize("head", [
    b"\x23\x76\x6a\x16\xff\x8f\xfe\x1a",   # 真实密文开头
    b"",
    b"GET / HTTP/1.1\r\n\r\n",
    b"<html><body>x</body></html>",
])
def test_looks_like_media_rejects_invalid_headers(head):
    assert not wxdecrypt.looks_like_media(head)


# ── 文件级解密 ────────────────────────────────────────────────────────
def _realistic_plaintext(span: int = wxdecrypt.HEAD_BYTES) -> bytes:
    """一个"像真的"明文前缀，结构照抄真实样本：

    ``ftyp(32)`` + ``mdat``（长度声明得比前缀长，所以会被截断）。
    只有形状对，才能真的走通 box 链校验 —— 用一堆零当明文的话，
    校验器会把"正确解密"也判成非法（那才是假阴性）。
    """
    head = bytes.fromhex(wxdecrypt.SELFTEST_PLAINTEXT32_HEX)
    mdat_size = wxdecrypt.SELFTEST_FILE_SIZE - wxdecrypt.SELFTEST_MDAT_OFFSET
    return head + mdat_size.to_bytes(4, "big") + b"mdat" + bytes(max(0, span - 40))


def _write_sample(path, span: int = wxdecrypt.HEAD_BYTES):
    """用真实密钥流把一个"合法 MP4 前缀"加密成视频号格式的密文。"""
    plain = _realistic_plaintext(span)
    cipher = bytes(a ^ b for a, b in zip(
        plain, wxdecrypt.generate_keystream(GOLDEN_KEY, span)))
    path.write_bytes(cipher)


def test_decrypt_file_roundtrip(tmp_path):
    target = tmp_path / "enc.mp4"
    _write_sample(target)
    assert wxdecrypt.needs_decrypt(target) is True

    ok, message = wxdecrypt.decrypt_file(target, GOLDEN_KEY)
    assert ok, message
    assert target.read_bytes()[: len(GOLDEN_PLAINTEXT)] == GOLDEN_PLAINTEXT
    assert wxdecrypt.needs_decrypt(target) is False


def test_plain_file_is_left_alone(tmp_path):
    target = tmp_path / "plain.mp4"
    target.write_bytes(GOLDEN_PLAINTEXT + b"\x00" * 100)
    assert wxdecrypt.needs_decrypt(target) is False

    ok, message = wxdecrypt.ensure_plain_mp4(target, GOLDEN_KEY)
    assert ok and message == "无需解密"
    assert target.read_bytes()[: len(GOLDEN_PLAINTEXT)] == GOLDEN_PLAINTEXT


def test_wrong_key_does_not_corrupt_file(tmp_path):
    """解错了也必须原样保留 —— 宁可报错，也不能交付被二次破坏的文件。"""
    target = tmp_path / "enc.mp4"
    _write_sample(target)
    before = target.read_bytes()

    ok, message = wxdecrypt.decrypt_file(target, "12345")
    assert not ok
    assert "已保留原文件" in message
    assert target.read_bytes() == before


def test_needs_key_but_none_given(tmp_path):
    target = tmp_path / "enc.mp4"
    _write_sample(target)
    ok, message = wxdecrypt.ensure_plain_mp4(target, "")
    assert not ok
    assert "decode_key" in message


# ── box 链校验（比"开头有没有 ftyp"强得多）────────────────────────────
def _box(box_type: bytes, payload: bytes) -> bytes:
    return (len(payload) + 8).to_bytes(4, "big") + box_type + payload


def test_validate_mp4_prefix_accepts_self_consistent_chain():
    ftyp = _box(b"ftyp", b"isom" + (0x200).to_bytes(4, "big") + b"isomiso2")
    moov = _box(b"moov", bytes(100))
    # 尾部被 128KB 截断的 mdat 也应当被接受
    data = ftyp + moov + (14030918).to_bytes(4, "big") + b"mdat" + bytes(64)
    assert wxdecrypt.validate_mp4_prefix(data) is True


def test_validate_mp4_prefix_rejects_broken_chain():
    ftyp = _box(b"ftyp", b"isom" + (0x200).to_bytes(4, "big") + b"isomiso2")
    # 长度字段小于 box 头（4 < 8）→ 明确非法，链断
    broken = ftyp + (4).to_bytes(4, "big") + b"moov" + bytes(64)
    assert wxdecrypt.validate_mp4_prefix(broken) is False
    # 类型不认识 → 也非法
    unknown = ftyp + _box(b"junk", bytes(64))
    assert wxdecrypt.validate_mp4_prefix(unknown) is False


def test_validate_mp4_prefix_rejects_unknown_first_box():
    data = _box(b"junk", bytes(64)) + _box(b"moov", bytes(64))
    assert wxdecrypt.validate_mp4_prefix(data) is False


def test_validate_mp4_prefix_rejects_too_short():
    assert wxdecrypt.validate_mp4_prefix(b"") is False
    assert wxdecrypt.validate_mp4_prefix(b"ftyp") is False


def test_validate_mp4_prefix_rejects_wrong_key_output():
    """真实密文用错 key 解密后，box 链必须断 —— 这正是运行时的验收闸门。"""
    offset = wxdecrypt.SELFTEST_MDAT_OFFSET
    cipher = bytes.fromhex(wxdecrypt.SELFTEST_CIPHER_AT_MDAT_HEX)
    stream = wxdecrypt.generate_keystream("2111111111", offset + len(cipher))
    bad = bytes(a ^ b for a, b in zip(cipher, stream[offset:]))
    assert not wxdecrypt.validate_mp4_prefix(bad)


def test_decrypt_file_rejects_wrong_key_even_if_header_looks_ok(tmp_path):
    """加密文件用错 key：即使前 32 字节碰巧像头，box 链校验也会拦住。"""
    target = tmp_path / "enc.mp4"
    _write_sample(target)
    before = target.read_bytes()

    ok, message = wxdecrypt.decrypt_file(target, "2222222222")
    assert not ok
    assert target.read_bytes() == before, "失败时必须原样保留"


def test_decrypt_missing_file(tmp_path):
    ok, message = wxdecrypt.decrypt_file(tmp_path / "nope.mp4", GOLDEN_KEY)
    assert not ok and "不存在" in message


def test_decrypt_empty_file(tmp_path):
    target = tmp_path / "empty.mp4"
    target.write_bytes(b"")
    ok, message = wxdecrypt.ensure_plain_mp4(target, GOLDEN_KEY)
    assert ok, "空文件不该被当成『需要解密』"
    assert message == "无需解密"
