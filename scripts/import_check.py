"""在离线环境中逐个导入已安装分发包的顶层模块，验证轮子在目标 GLIBC 上可用。

导入失败通常意味着原生扩展与系统库不兼容，或缺少系统依赖。
scripts/import_skip.txt 中列出的模块名会被跳过（每行一个，# 开头为注释）。
"""

from __future__ import annotations

import importlib
import importlib.metadata as md
import platform
import sys
from pathlib import Path

SKIP_FILE = Path(__file__).with_name("import_skip.txt")


def load_skip() -> set[str]:
    if not SKIP_FILE.exists():
        return set()
    names = set()
    for line in SKIP_FILE.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            names.add(line)
    return names


def main() -> int:
    libc_name, libc_version = platform.libc_ver()
    print(f"Python {sys.version.split()[0]}  {platform.platform()}  {libc_name} {libc_version}")

    skip = load_skip()
    distributions = md.packages_distributions()
    failures: list[tuple[str, list[str], str]] = []
    checked = 0
    for name in sorted(distributions):
        if name.startswith("_") or name in skip:
            continue
        checked += 1
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 需要捕获任意导入期错误
            failures.append((name, distributions[name], repr(exc)))

    print(f"检查 {checked} 个顶层模块，失败 {len(failures)} 个")
    for name, providers, error in failures:
        print(f"  {name} (来自 {', '.join(providers)}): {error}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
