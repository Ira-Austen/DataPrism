"""校验 wheelhouse 中的 Linux 轮子能否在目标 GLIBC 版本上安装。

规则：
- 纯 Python 轮子（platform 为 any）直接通过。
- manylinux_X_Y 轮子要求 (X, Y) 不高于目标版本；manylinux1、2010、2014 按别名折算。
- 只允许 x86_64 架构；linux_x86_64 这类非 manylinux 轮子与 musllinux 轮子一律拒绝。
- 轮子的多个平台标签中只要有一个满足即通过。

用法：python scripts/check_wheel_tags.py <wheelhouse 目录> [--max-glibc 2.28]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from packaging.utils import parse_wheel_filename

LEGACY_ALIASES = {
    "manylinux1": (2, 5),
    "manylinux2010": (2, 12),
    "manylinux2014": (2, 17),
}
ARCH = "x86_64"


def glibc_requirement(platform: str) -> tuple[int, int] | None:
    """返回平台标签要求的最低 GLIBC 版本；非 manylinux 标签返回 None。"""
    if not platform.endswith(f"_{ARCH}"):
        return None
    if platform.startswith("manylinux_"):
        parts = platform.split("_")
        return int(parts[1]), int(parts[2])
    for alias, version in LEGACY_ALIASES.items():
        if platform.startswith(f"{alias}_"):
            return version
    return None


def wheel_ok(filename: str, max_glibc: tuple[int, int]) -> tuple[bool, str]:
    _, _, _, tags = parse_wheel_filename(filename)
    platforms = sorted({t.platform for t in tags})
    if platforms == ["any"]:
        return True, "纯 Python"
    for platform in platforms:
        req = glibc_requirement(platform)
        if req is not None and req <= max_glibc:
            return True, platform
    return False, ",".join(platforms)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheelhouse", type=Path)
    parser.add_argument("--max-glibc", default="2.28")
    args = parser.parse_args()

    major, minor = (int(x) for x in args.max_glibc.split("."))
    wheels = sorted(args.wheelhouse.glob("*.whl"))
    if not wheels:
        print(f"警告：{args.wheelhouse} 中没有轮子，跳过校验")
        return 0

    failures: list[tuple[str, str]] = []
    width = max(len(w.name) for w in wheels)
    for wheel in wheels:
        ok, reason = wheel_ok(wheel.name, (major, minor))
        mark = "通过" if ok else "拒绝"
        print(f"{mark}  {wheel.name.ljust(width)}  {reason}")
        if not ok:
            failures.append((wheel.name, reason))

    print(f"\n共 {len(wheels)} 个轮子，目标 GLIBC {args.max_glibc}，拒绝 {len(failures)} 个")
    for name, reason in failures:
        print(f"  {name}: {reason}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
