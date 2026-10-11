"""微信视频号视频解密：ISAAC64 密钥流 + 只解前 128 KiB。

为什么需要这个模块
------------------
视频号 CDN（``finder.video.qq.com/.../stodownload``）下发的 **不是** 可以直接
播放的 MP4：前 131072 字节（128 KiB）是被 ISAAC64 伪随机流 **XOR** 过的，
后面的部分是明文。所以「拿到直链 → 直接下载 → 交付给客户」这条路会交付一个
开头损坏、播放器打不开的文件（真事故：客户反馈"视频花屏/无法播放"）。

算法（逆向结论，已用公开样本 + 黄金向量验证）
---------------------------------------------
1. 密钥 = API 响应里的 ``decode_key``，是一个 **十进制字符串**，按 ``uint64`` 解析；
   例：``"2136343393"``。
2. 用它做 ISAAC64 的种子：``randrsl[0] = seed``，其余为 0，走标准 ``randinit``
   两轮混合（黄金比例 ``0x9e3779b97f4a7c13``）。
3. 取随机数的顺序是 ``randrsl[255] → randrsl[254] → … → randrsl[0]``，
   每 256 个词重新跑一次 ``isaac64()``。
4. 每个 64 位随机数按 **大端** 展开成 8 字节，拼成密钥流。
5. ``明文 = 密文 XOR 密钥流``，只作用于前 131072 字节。

验证方式（两层，都在 tests/test_wxdecrypt.py 里）
-------------------------------------------------
1. **前 64 字节**：用公开样本 ``wx_encrypted.mp4``（14,088,528 字节，
   ``decode_key=2136343393``）的真实密文，解密后必须得到
   ``00000020 66747970 69736f6d 00000200 …``（合法的 ``ftyp isom`` 头）。
2. **跨边界锚点**：样本第 57,600 字节起的真实密文，解密后必须是
   ``…00000008 free … mdat …``，且 ``mdat`` 的长度字段必须等于
   ``14,088,528 − 57,610 = 14,030,918``。这条约束来自**真实文件的字节数**
   （不是从本实现反推），而且位置在第 7200 个 64 位词附近 —— 既跨了词边界，
   也早过了第 256 词时 ``randcnt`` 归零重跑的边界。解密后的 128 KiB 完整
   box 链为 ``ftyp(32) + moov(57570) + free(8) + mdat(14030918，被前缀截断)``。

   第 2 条是本模块最有价值的一条断言：黄金向量只锚前 8 个词，而它把整段
    密钥流的**字节序与取数顺序**钉死了。

安全性设计：**只在 box 链校验通过时才替换原文件**（见 :func:`validate_mp4_prefix`）。
解密结果必须是自洽的 ISO-BMFF，否则回滚 —— 宁可交付一个「需要解密的原始文件 +
明确报错」，也绝不交付一个被错误 XOR 二次破坏的文件。
"""

from __future__ import annotations

import os
from pathlib import Path

#: uint64 掩码。Python 的整数是任意精度，所有位运算都要显式截断到 64 位。
MASK64 = 0xFFFFFFFFFFFFFFFF
#: ISAAC64 初始化用的黄金比例常量。
GOLDEN = 0x9E3779B97F4A7C13
#: 被加密的字节数：128 KiB。超过这个长度的部分原文未加密。
HEAD_BYTES = 131072


# ── 公开样本黄金向量（唯一真相源）──────────────────────────────────────
#: 来自公开项目 Evil0ctal/WeChat-Channels-Video-File-Decryption 的真实样本
#: ``wx_encrypted.mp4``（14,088,528 字节）与其响应 ``wx_response.json``。
#: **这三个常量是从真实样本里取的，不是从本实现推出来的** —— 自检、单测、
#: 集成测试全部引用这里，避免"同一个数字抄三份、改一处漏两处"。
SELFTEST_DECODE_KEY = "2136343393"
#: 样本前 16 字节的密钥流（即 ISAAC64 前两个 64 位输出的大端字节）
SELFTEST_KEYSTREAM16_HEX = "23766a3699fb876a75d5a232994844ab"
#: 样本前 64 字节密文
SELFTEST_CIPHER64_HEX = (
    "23766a16ff8ffe1a1ca6cd5f994846ab2973b9e615bf3a25211606a7d2236114"
    "dd49a8ff57f027621fa6df865bd6c942bc8c502d392680246e4588382403a26c"
)
#: 样本解密后的前 32 字节：一个自洽的 ISO-BMFF 头
#: （box size 0x20 / ``ftyp`` / major brand ``isom`` / minor 0x200 /
#:   compatible brands ``isom iso2 avc1 mp41``）
SELFTEST_PLAINTEXT32_HEX = (
    "000000206674797069736f6d0000020069736f6d69736f32617663316d703431"
)
#: 样本总字节数（真实文件大小，**不由本实现推导**）。
SELFTEST_FILE_SIZE = 14088528
#: ``mdat`` box 在样本里的偏移（= ftyp 32 + moov 57570 + free 8）。
SELFTEST_MDAT_OFFSET = 57610
#: 样本 57600..57632 的密文。这一小段是**跨边界锚点**：
#: 它落在第 7200 个 64 位词附近，远超前 8 个黄金词、也早过了第 256 词的
#: ``randcnt`` 归零重跑边界。解密后这里的 ``mdat`` 长度必须等于
#: ``SELFTEST_FILE_SIZE - SELFTEST_MDAT_OFFSET`` —— 这个约束来自真实文件的
#: 字节数，不是从本实现反推的，所以它能把整段密钥流的字节序与取数顺序钉住。
SELFTEST_CIPHER_SLICE_OFFSET = 57600
SELFTEST_CIPHER_AT_MDAT_HEX = (
    "0d46a6b61a406260d1c4ef321c5c05cda720615a007ceb3cf4a0b3643645d901"
)


class Isaac64:
    """ISAAC64 伪随机数生成器（Bob Jenkins）。

    只实现视频号解密需要的那条路径：用单个 64 位种子初始化 + 顺序吐字节。
    """

    __slots__ = ("randrsl", "mm", "aa", "bb", "cc", "randcnt")

    def __init__(self, seed: int):
        self.randrsl = [0] * 256
        self.mm = [0] * 256
        self.aa = 0
        self.bb = 0
        self.cc = 0
        self.randcnt = 0
        self.randrsl[0] = seed & MASK64
        self._randinit()

    # ── 初始化 ─────────────────────────────────────────────────────
    def _randinit(self) -> None:
        a = b = c = d = e = f = g = h = GOLDEN
        for _ in range(4):
            a, b, c, d, e, f, g, h = _mix(a, b, c, d, e, f, g, h)

        # 第一轮：把种子混进 mm
        for j in range(0, 256, 8):
            a = (a + self.randrsl[j]) & MASK64
            b = (b + self.randrsl[j + 1]) & MASK64
            c = (c + self.randrsl[j + 2]) & MASK64
            d = (d + self.randrsl[j + 3]) & MASK64
            e = (e + self.randrsl[j + 4]) & MASK64
            f = (f + self.randrsl[j + 5]) & MASK64
            g = (g + self.randrsl[j + 6]) & MASK64
            h = (h + self.randrsl[j + 7]) & MASK64
            a, b, c, d, e, f, g, h = _mix(a, b, c, d, e, f, g, h)
            self.mm[j:j + 8] = [a, b, c, d, e, f, g, h]

        # 第二轮：再把 mm 混一遍，打散残余规律
        for j in range(0, 256, 8):
            a = (a + self.mm[j]) & MASK64
            b = (b + self.mm[j + 1]) & MASK64
            c = (c + self.mm[j + 2]) & MASK64
            d = (d + self.mm[j + 3]) & MASK64
            e = (e + self.mm[j + 4]) & MASK64
            f = (f + self.mm[j + 5]) & MASK64
            g = (g + self.mm[j + 6]) & MASK64
            h = (h + self.mm[j + 7]) & MASK64
            a, b, c, d, e, f, g, h = _mix(a, b, c, d, e, f, g, h)
            self.mm[j:j + 8] = [a, b, c, d, e, f, g, h]

        self._isaac64()
        self.randcnt = 256

    # ── 迭代 ───────────────────────────────────────────────────────
    def _isaac64(self) -> None:
        self.cc = (self.cc + 1) & MASK64
        self.bb = (self.bb + self.cc) & MASK64
        mm = self.mm
        rsl = self.randrsl
        aa = self.aa
        bb = self.bb
        for j in range(256):
            x = mm[j]
            r = j & 3
            if r == 0:
                aa = (aa ^ ((aa << 21) & MASK64)) ^ MASK64
            elif r == 1:
                aa ^= aa >> 5
            elif r == 2:
                aa ^= (aa << 12) & MASK64
            else:
                aa ^= aa >> 33
            aa = (aa + mm[(j + 128) & 255]) & MASK64
            y = (mm[(x >> 3) & 255] + aa + bb) & MASK64
            mm[j] = y
            bb = (mm[(y >> 11) & 255] + x) & MASK64
            rsl[j] = bb
        self.aa = aa
        self.bb = bb

    # ── 出流 ───────────────────────────────────────────────────────
    def keystream(self, length: int) -> bytes:
        """取 ``length`` 字节密钥流（大端展开，序号 255 → 0 递减）。"""
        if length <= 0:
            return b""
        out = bytearray(length)
        pos = 0
        while pos < length:
            if self.randcnt == 0:
                self._isaac64()
                self.randcnt = 256
            self.randcnt -= 1
            val = self.randrsl[self.randcnt]
            # 大端：高位字节在前
            for shift in (56, 48, 40, 32, 24, 16, 8, 0):
                if pos >= length:
                    break
                out[pos] = (val >> shift) & 0xFF
                pos += 1
        return bytes(out)


def _mix(a: int, b: int, c: int, d: int, e: int, f: int,
         g: int, h: int) -> tuple[int, int, int, int, int, int, int, int]:
    """ISAAC64 的混合函数（Bob Jenkins 参考实现，全部按 64 位截断）。"""
    a = (a - e) & MASK64
    f ^= h >> 9
    h = (h + a) & MASK64
    b = (b - f) & MASK64
    g ^= (a << 9) & MASK64
    a = (a + b) & MASK64
    c = (c - g) & MASK64
    h ^= b >> 23
    b = (b + c) & MASK64
    d = (d - h) & MASK64
    a ^= (c << 15) & MASK64
    c = (c + d) & MASK64
    e = (e - a) & MASK64
    b ^= d >> 14
    d = (d + e) & MASK64
    f = (f - b) & MASK64
    c ^= (e << 20) & MASK64
    e = (e + f) & MASK64
    g = (g - c) & MASK64
    d ^= f >> 17
    f = (f + g) & MASK64
    h = (h - d) & MASK64
    e ^= (g << 14) & MASK64
    g = (g + h) & MASK64
    return a, b, c, d, e, f, g, h


def parse_decode_key(value: object) -> int | None:
    """把 ``decode_key`` 解析成 ISAAC64 种子。

    官方下发的是十进制字符串（如 ``"2136343393"``）；也兼容直接给整数。
    解析不出来返回 ``None`` —— 上层据此跳过解密而不是猜一个种子。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value & MASK64 if value >= 0 else None
    text = str(value).strip()
    if not text:
        return None
    try:
        number = int(text, 10)
    except ValueError:
        return None
    return number & MASK64 if number >= 0 else None


def generate_keystream(decode_key: object, length: int = HEAD_BYTES) -> bytes:
    """按 ``decode_key`` 生成 ``length`` 字节密钥流；无法解析时抛 ``ValueError``。"""
    seed = parse_decode_key(decode_key)
    if seed is None:
        raise ValueError(f"无法从 decode_key 解析出种子：{decode_key!r}")
    return Isaac64(seed).keystream(length)


#: MP4/ISO-BMFF 里可以出现在顶层的 box 类型。文件头不该出现这些以外的内容。
_BOX_TYPES = (
    b"ftyp", b"styp", b"moov", b"mdat", b"free", b"skip", b"wide", b"uuid",
    b"sidx", b"ssix", b"pdin", b"moof", b"mfra", b"meta", b"emsg", b"prft",
)


def looks_like_media(head: bytes) -> bool:
    """这段数据像不像一个正常的 MP4 头（只看前 32 字节）。

    这是**快速**判据，用于 ``needs_decrypt`` 之类的"粗略一问"。
    真正决定要不要接受解密结果的是 :func:`validate_mp4_prefix`。
    """
    limit = min(len(head), 32)
    for i in range(0, max(0, limit - 3)):
        if head[i:i + 4] in _BOX_TYPES:
            return True
    return False


def validate_mp4_prefix(data: bytes, *, max_boxes: int = 64) -> bool:
    """校验 ISO-BMFF **box 链**是否自洽，而不只是看开头有没有 ``ftyp``。

    为什么需要它：只查前 32 字节的话，一个「decode_key 不匹配」的结果也可能在
    前 32 字节里蒙对 ``ftyp``，然后交付给客户一个坏文件。box 链是自描述的
    （``size(4) + type(4)`` 首尾相接），密钥流一旦错位，链几乎立刻断掉。

    判定规则（对"正确解密"要宽容，对"随机数据"要严）：

    * **每一个**出现的 box 类型都必须在 :data:`_BOX_TYPES` 里；
    * 长度字段必须 ≥ 头部长度（支持 64 位 ``largesize`` 与 ``size == 0``）；
    * 允许最后一个 box 被前缀长度截断 —— 128 KiB 常常装不下整个 ``moov``，
      长视频的 ``moov`` 甚至可能超过 128 KiB，所以不能要求闭合两个 box。

    随机数据通过的概率 ≈ （已知类型数 / 2³²）² 量级，实际可忽略；
    而"合法但被截断"的真实文件不会被误杀。
    """
    if not data or len(data) < 16:
        return False
    pos = 0
    closed = 0
    while pos + 8 <= len(data) and closed < max_boxes:
        size = int.from_bytes(data[pos:pos + 4], "big")
        box_type = data[pos + 4:pos + 8]
        header = 8
        if size == 1:                      # 64 位 largesize
            if pos + 16 > len(data):
                return False
            size = int.from_bytes(data[pos + 8:pos + 16], "big")
            header = 16
        if box_type not in _BOX_TYPES:
            return False
        if size == 0:                      # 延伸到文件末尾
            return closed >= 1
        if size < header or size > (1 << 40):
            return False
        if pos + size > len(data):
            # 最后一个 box 被截断：类型已校验过，接受
            return closed >= 1
        closed += 1
        pos += size
    return closed >= 1


def read_head(path: str | Path, size: int = 32) -> bytes:
    """读文件开头若干字节；读不到返回空串。"""
    try:
        with open(path, "rb") as fh:
            return fh.read(size)
    except OSError:
        return b""


def needs_decrypt(path: str | Path) -> bool:
    """文件是不是「视频号加密 MP4」（即需要解密）。

    判据：头 32 字节里没有合法 box 类型。注意空文件/极小文件也返回 True，
    但那时解密同样无意义 —— 调用方应先确认文件有内容。
    """
    head = read_head(path)
    if not head:
        return False
    return not looks_like_media(head)


def decrypt_file(path: str | Path, decode_key: object, *,
                 length: int = HEAD_BYTES) -> tuple[bool, str]:
    """就地解密 ``path`` 的前 ``length`` 字节。

    返回 ``(是否成功, 说明)``。**只有校验通过才会写回**：
    先解密到内存 → 校验头 → 通过才覆盖，否则原样保留并返回失败原因。
    """
    target = Path(path)
    if not target.exists():
        return False, f"文件不存在：{target}"

    seed = parse_decode_key(decode_key)
    if seed is None:
        return False, f"decode_key 无效（{decode_key!r}），无法生成密钥流"

    try:
        size = target.stat().st_size
    except OSError as exc:
        return False, f"读取文件信息失败：{exc}"
    if size <= 0:
        return False, "文件是空的，无需解密"

    span = min(int(length), size)
    try:
        with open(target, "rb") as fh:
            cipher = fh.read(span)
    except OSError as exc:
        return False, f"读取文件失败：{exc}"

    stream = Isaac64(seed).keystream(span)
    plain = bytes(a ^ b for a, b in zip(cipher, stream))

    # 验收标准用**整段 box 链**而不是"开头有没有 ftyp"：后者对"解错了一半"
    # 的结果也可能放行，然后交付给客户一个坏文件。
    if span >= 16 and not validate_mp4_prefix(plain):
        return False, ("解密后不是自洽的 MP4（box 链断开，decode_key 可能与视频"
                       "不匹配）；已保留原文件")
    if not looks_like_media(plain[:32]):
        return False, ("解密后仍然不是有效的 MP4 头（decode_key 可能与视频不匹配）；"
                       "已保留原文件")

    try:
        with open(target, "r+b") as fh:
            fh.write(plain)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError as exc:
        return False, f"写回解密数据失败：{exc}"

    return True, f"已解密前 {span} 字节"


def ensure_plain_mp4(path: str | Path, decode_key: object) -> tuple[bool, str]:
    """把下载产物变成可播放的 mp4。

    * 已经是正常 MP4 → 什么都不做，返回 ``(True, "无需解密")``。
    * 需要解密且给了 ``decode_key`` → 解密。
    * 需要解密但没给 key → 返回 ``(False, ...)``，让上层把这单标成「需人工」，
      而不是悄悄交付一个坏文件。
    """
    if not needs_decrypt(path):
        return True, "无需解密"
    if parse_decode_key(decode_key) is None:
        return False, ("下载到的文件是视频号加密格式，但缺少 decode_key，无法解密。"
                       "请配置 TikHub API key（会同时返回直链与 decode_key）")
    return decrypt_file(path, decode_key)
