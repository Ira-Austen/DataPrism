#!/usr/bin/env bash
# 检查 ELF 二进制引用的 GLIBC 符号版本是否不超过目标版本。
# 用法：check_glibc_symbols.sh <二进制路径> [最大 GLIBC 版本，默认 2.28]
set -euo pipefail

BIN="${1:?缺少二进制路径}"
MAX="${2:-2.28}"

command -v objdump >/dev/null || { echo "缺少 objdump，请安装 binutils"; exit 2; }

syms="$(objdump -T "$BIN" | grep -oE 'GLIBC_[0-9]+(\.[0-9]+)+' | sort -u -V || true)"
max_found="$(echo "$syms" | sed 's/GLIBC_//' | sort -V | tail -1)"

echo "引用的 GLIBC 符号版本：$(echo "$syms" | tr '\n' ' ')"
echo "最高：${max_found:-无}    允许：$MAX"

if [ -n "$max_found" ]; then
    highest="$(printf '%s\n%s\n' "$MAX" "$max_found" | sort -V | tail -1)"
    if [ "$highest" != "$MAX" ]; then
        echo "失败：引用了高于 $MAX 的 GLIBC 符号，无法在目标机运行"
        exit 1
    fi
fi

echo "动态依赖："
objdump -p "$BIN" | grep NEEDED || echo "  无"
if objdump -p "$BIN" | grep -q 'NEEDED.*libstdc++'; then
    echo "警告：动态依赖 libstdc++，请确认目标机的 GLIBCXX 版本"
fi
if objdump -p "$BIN" | grep -q 'NEEDED.*libgomp'; then
    echo "警告：动态依赖 libgomp，目标机需安装 libgomp1"
fi
echo "通过"
