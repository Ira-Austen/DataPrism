"""在断网环境启动一个最小 NiceGUI 应用，抓取首页及其引用的静态资源，断言没有任何外部域名引用。

检查方式为静态扫描：解析 HTML、JS、CSS 中的 src、href、url()、import 引用；
同源资源逐个请求并要求 200，外部 http(s) 引用一律判失败。
不依赖浏览器，浏览器级验证留给 self-hosted runner。

用法：python scripts/check_no_external_requests.py --port 8099 --out results.json
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urljoin, urlparse

APP_CODE = """
from nicegui import ui
ui.label('offline probe')
ui.table(columns=[{'name': 'a', 'label': 'A', 'field': 'a'}], rows=[{'a': 1}])
ui.aggrid({'columnDefs': [{'field': 'a'}], 'rowData': [{'a': 1}]})
ui.run(host='127.0.0.1', port=%d, show=False, reload=False, title='probe')
"""

REF_PATTERN = re.compile(
    r"""(?:src|href)\s*=\s*["']([^"']+)["']|url\(\s*["']?([^"')]+)["']?\s*\)|from\s+["']([^"']+)["']|import\s*\(\s*["']([^"']+)["']\s*\)""",
    re.IGNORECASE,
)
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
TEXT_TYPES = ("text/html", "javascript", "text/css")


def fetch(url: str, timeout: int = 20) -> tuple[int, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": "dataprism-probe"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ctype = resp.headers.get("Content-Type", "")
            body = resp.read().decode("utf-8", errors="replace") if any(t in ctype for t in TEXT_TYPES) else ""
            return resp.status, ctype, body
    except urllib.error.HTTPError as exc:
        return exc.code, "", ""


def wait_ready(url: str, seconds: int) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if fetch(url, timeout=3)[0] == 200:
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1)
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--max-depth", type=int, default=3)
    args = parser.parse_args()

    base = f"http://127.0.0.1:{args.port}/"
    proc = subprocess.Popen([sys.executable, "-c", APP_CODE % args.port])
    try:
        if not wait_ready(base, 60):
            print("应用未在 60 秒内就绪")
            return 1

        seen: set[str] = set()
        external: list[tuple[str, str]] = []
        broken: list[tuple[str, int]] = []
        queue = [(base, 0)]
        while queue:
            url, depth = queue.pop()
            if url in seen:
                continue
            seen.add(url)
            status, ctype, body = fetch(url)
            if status != 200:
                broken.append((url, status))
                continue
            if depth >= args.max_depth or not body:
                continue
            for match in REF_PATTERN.finditer(body):
                ref = next(g for g in match.groups() if g)
                if ref.startswith(("data:", "blob:", "#", "about:")):
                    continue
                target = urljoin(url, ref)
                host = urlparse(target).hostname
                if urlparse(target).scheme in ("http", "https") and host not in LOCAL_HOSTS:
                    external.append((url, target))
                elif host in LOCAL_HOSTS:
                    queue.append((target, depth + 1))

        summary = {"checked": len(seen), "external": external, "broken": broken}
        print(f"抓取 {len(seen)} 个资源，外部引用 {len(external)} 个，失败 {len(broken)} 个")
        for src, target in external:
            print(f"  外部引用 {target}  来自 {src}")
        for url, status in broken:
            print(f"  失败 {status} {url}")
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1 if external or broken else 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
