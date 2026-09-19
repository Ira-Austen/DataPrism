#!/usr/bin/env bash
# 联网阶段下载冒烟验证所需模型，已存在的文件跳过。离线阶段只读取该目录。
# 用法：ci_download_models.sh <模型目录> <venv 的 python>
# 环境变量：GGUF_URL  BGE_REPO
set -euo pipefail

MODELS="${1:?缺少模型目录}"
PYTHON="${2:?缺少 python 路径}"
GGUF_URL="${GGUF_URL:?缺少 GGUF_URL}"
BGE_REPO="${BGE_REPO:-Xenova/bge-small-zh-v1.5}"

mkdir -p "$MODELS/llm" "$MODELS/embedding" "$MODELS/layout"

fetch() {
    local url="$1" dest="$2"
    if [ -s "$dest" ]; then
        echo "已存在：$dest"
        return 0
    fi
    echo "下载：$url"
    curl -L --fail --retry 3 --retry-delay 5 -o "$dest.part" "$url"
    mv "$dest.part" "$dest"
}

# 大语言模型
fetch "$GGUF_URL" "$MODELS/llm/$(basename "$GGUF_URL")"

# 嵌入模型：Xenova 转换版的量化文件名在不同时期不同，按顺序尝试
if [ ! -s "$MODELS/embedding/model.onnx" ]; then
    for name in model_int8.onnx model_quantized.onnx model.onnx; do
        if curl -L --fail --retry 3 -o "$MODELS/embedding/model.onnx.part" \
            "https://huggingface.co/$BGE_REPO/resolve/main/onnx/$name"; then
            mv "$MODELS/embedding/model.onnx.part" "$MODELS/embedding/model.onnx"
            echo "$name" > "$MODELS/embedding/VARIANT"
            break
        fi
    done
    [ -s "$MODELS/embedding/model.onnx" ] || { echo "嵌入模型下载失败"; exit 1; }
fi
fetch "https://huggingface.co/$BGE_REPO/resolve/main/tokenizer.json" "$MODELS/embedding/tokenizer.json"

# Docling 版面与表格模型
if [ ! -f "$MODELS/layout/.done" ]; then
    "$(dirname "$PYTHON")/docling-tools" models download -o "$MODELS/layout"
    touch "$MODELS/layout/.done"
fi

# 清单，供缓存键与审计使用
{
    echo "gguf: $GGUF_URL"
    echo "bge: $BGE_REPO ($(cat "$MODELS/embedding/VARIANT" 2>/dev/null || echo unknown))"
    echo "docling: $("$PYTHON" -c 'import docling, importlib.metadata as m; print(m.version("docling"))')"
} > "$MODELS/MANIFEST"
cat "$MODELS/MANIFEST"
du -sh "$MODELS"/*
