"""文档自检：校验内部链接、源码引用、测试名引用是否真实存在。

为什么需要它：文档里写错一个文件名或测试名，比不写更糟 —— 接手的人会照着去做，
然后发现根本不存在。这个脚本把"文档里提到的东西"逐个落到文件系统上验证。

用法：
    python tools/check_docs.py          # 退出码 0 = 全部可落地
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = sorted((ROOT / "docs").glob("*.md")) + [ROOT / "README.md"]

#: 文档里允许提到、但**故意不存在**的东西（已删除的东西 / 分支名等）
ALLOW_MISSING = {
    "xydl/tikhub.py",          # 决策记录里写明"已删除"
    "docs/requirements",       # 分支名，不是路径
    "docs/architecture",
    "docs/download-flows",
    "docs/testing",
    "docs/deployment",
    "docs/collaboration",
    "docs/readme-rewrite",
    "docs/testing-accuracy",
    "docs/verification",
    "tests/test_tikhub.py",    # 同上，已删除
}


def _is_placeholder(text: str, *, bare_word_counts: bool = True) -> bool:
    """判断是不是文档里的**示例占位符**（如 ``[文字](路径)``、``test_xxx``）。

    文档在讲"链接怎么写/测试怎么命名"时会用到占位符，它们本来就不该存在，
    不能当成失效引用报出来 —— 否则这个脚本会把讲它自己的那篇文档判失败。

    ``bare_word_counts``：只对**路径**生效 —— "既没有 / 也没有 ." 的字符串
    不可能是文件路径（多半是中文占位词）。对**测试名**不能这么判，因为
    ``test_this_case_does_not_exist`` 也是既没斜杠也没点，那样会被误当成占位符
    （反向验证时真踩到了：注入的假测试名没被检出）。
    """
    text = (text or "").strip()
    if not text:
        return True
    if any(mark in text for mark in ("…", "...", "<", ">", "xxx", "文字", "路径", "某个")):
        return True
    if not bare_word_counts:
        return False
    return "/" not in text and "." not in text


def check_links() -> list[str]:
    """markdown 链接指向的本地文件必须存在。"""
    problems: list[str] = []
    for doc in DOCS:
        for label, target in re.findall(r"\[([^\]]+)\]\(([^)]+)\)",
                                        doc.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "#")):
                continue
            if _is_placeholder(target):
                continue
            path = (doc.parent / target.split("#")[0]).resolve()
            if not path.exists():
                problems.append(f"{doc.name}: 链接失效 [{label}]({target})")
    return problems


def check_code_refs() -> list[str]:
    """反引号里的源码路径必须存在（白名单除外）。"""
    problems: list[str] = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for ref in re.findall(r"`((?:xydl|tests|docs|webui|tools)/[\w/\-.]+)`", text):
            if ref.endswith("/") or ref in ALLOW_MISSING or _is_placeholder(ref):
                continue
            if not (ROOT / ref).exists():
                problems.append(f"{doc.name}: 引用了不存在的路径 `{ref}`")
    return problems


def check_test_names() -> list[str]:
    """反引号里形如 test_xxx 的名字必须在 tests/ 里真实存在。"""
    corpus = "\n".join(p.read_text(encoding="utf-8")
                       for p in (ROOT / "tests").glob("*.py"))
    problems: list[str] = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for name in set(re.findall(r"`(test_\w+)`", text)):
            if _is_placeholder(name, bare_word_counts=False):
                continue
            if f"def {name}(" not in corpus:
                problems.append(f"{doc.name}: 引用了不存在的测试 `{name}`")
    return problems


def check_commands() -> list[str]:
    """反引号里形如 run.py <cmd> 的子命令必须在 run.py 注册过。"""
    run_text = (ROOT / "run.py").read_text(encoding="utf-8")
    registered = set(re.findall(r'sub\.add_parser\(\s*"([\w-]+)"', run_text))
    problems: list[str] = []
    for doc in DOCS:
        text = doc.read_text(encoding="utf-8")
        for cmd in set(re.findall(r"run\.py\s+([a-z][\w-]*)", text)):
            if cmd in ("py",):            # "run.py ..." 已由正则保证
                continue
            if cmd not in registered:
                problems.append(f"{doc.name}: 引用了不存在的子命令 `run.py {cmd}`")
    return problems


def main() -> int:
    checks = {
        "内部链接": check_links(),
        "源码引用": check_code_refs(),
        "测试名引用": check_test_names(),
        "子命令引用": check_commands(),
    }
    failed = False
    for name, problems in checks.items():
        if problems:
            failed = True
            print(f"✗ {name}（{len(problems)} 条）")
            for item in problems:
                print(f"    {item}")
        else:
            print(f"✓ {name}")
    print(f"\n检查了 {len(DOCS)} 个文档。")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
