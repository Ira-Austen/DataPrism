#!/usr/bin/env bash
# 在断网的 debian10 容器内执行全部冒烟检查。由 offline-smoke workflow 调用。
# 期望目录：.ci/venv（uv 建的虚拟环境）、.ci/models、bin/linux-x86_64/llama-server、tests/fixtures/generated/sample.pdf
set -uo pipefail

PY=".ci/venv/bin/python"
MODELS=".ci/models"
RESULTS=".ci/results"
PORT=8080
mkdir -p "$RESULTS"
failed=0

step() {
    local name="$1"; shift
    echo "::group::$name"
    if "$@"; then
        echo "通过：$name"
    else
        echo "失败：$name"
        failed=1
    fi
    echo "::endgroup::"
}

# 断网自证：任何外部请求都应失败
step "网络已隔离" bash -c '! curl -sS --max-time 5 https://huggingface.co >/dev/null 2>&1'

# 启动 llama-server
GGUF="$(ls "$MODELS"/llm/*.gguf | head -1)"
bin/linux-x86_64/llama-server -m "$GGUF" --port "$PORT" --host 127.0.0.1 \
    --ctx-size 8192 --threads "$(nproc)" --jinja --log-disable >"$RESULTS/llama-server.log" 2>&1 &
SERVER_PID=$!
ready=0
for _ in $(seq 1 120); do
    if curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ready=1; break; fi
    sleep 2
done
if [ "$ready" = 1 ]; then
    step "llama-server 结构化抽取" "$PY" scripts/smoke_llm.py --base-url "http://127.0.0.1:$PORT" --out "$RESULTS/llm.json"
else
    echo "llama-server 未就绪"; tail -50 "$RESULTS/llama-server.log"; failed=1
fi
kill "$SERVER_PID" 2>/dev/null || true

step "bge 嵌入" "$PY" scripts/smoke_embedding.py --model-dir "$MODELS/embedding" --out "$RESULTS/embedding.json"
step "Docling 解析" "$PY" scripts/smoke_docling.py --artifacts "$MODELS/layout" --input tests/fixtures/generated/sample.pdf --out "$RESULTS/docling.json"
step "NiceGUI 无外部请求" "$PY" scripts/check_no_external_requests.py --port 8099 --out "$RESULTS/nicegui.json"

echo "环境：$("$PY" -c 'import platform; print(platform.platform(), platform.libc_ver())')" | tee "$RESULTS/env.txt"
nproc >> "$RESULTS/env.txt"
exit $failed
