#!/usr/bin/env bash
# 在 manylinux_2_28 容器中编译 llama-server，产物只依赖 GLIBC <= 2.28，可直接在统信 UOS v20 运行。
# 环境变量：
#   LLAMA_CPP_REF  llama.cpp 的 tag、分支或提交号，默认 master
#   OUT_DIR        产物目录，默认 $PWD/dist/llama-server
set -euo pipefail

REF="${LLAMA_CPP_REF:-master}"
OUT="${OUT_DIR:-$PWD/dist/llama-server}"
WORK="${WORK_DIR:-/tmp/llama.cpp}"

# manylinux 镜像自带多个 Python，借其中一个安装 cmake 与 ninja，避免依赖系统包。
PY_BIN="$(ls -d /opt/python/cp3*-cp3*/bin | head -1)"
"$PY_BIN/pip" install -q cmake ninja
export PATH="$PY_BIN:$PATH"

rm -rf "$WORK"
if ! git clone --depth 1 --branch "$REF" https://github.com/ggml-org/llama.cpp "$WORK" 2>/dev/null; then
    # REF 是提交号时 --branch 不可用，退回完整克隆
    git clone https://github.com/ggml-org/llama.cpp "$WORK"
    git -C "$WORK" checkout "$REF"
fi

# 关闭 GGML_NATIVE 以免按构建机指令集优化；显式开启海光与兆芯都支持的 AVX2 与 FMA。
# 关闭 OpenMP 与 curl，静态链接 libstdc++，减少目标机的动态依赖。
cmake -S "$WORK" -B "$WORK/build" -G Ninja \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DGGML_NATIVE=OFF \
    -DGGML_AVX=ON -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON \
    -DGGML_AVX512=OFF \
    -DGGML_OPENMP=OFF \
    -DLLAMA_CURL=OFF \
    -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_TOOLS=ON \
    -DLLAMA_BUILD_SERVER=ON \
    -DCMAKE_EXE_LINKER_FLAGS="-static-libstdc++ -static-libgcc"
cmake --build "$WORK/build" --target llama-server -j"$(nproc)"

mkdir -p "$OUT"
cp "$WORK/build/bin/llama-server" "$OUT/"
{
    echo "llama.cpp ref: $REF"
    echo "commit: $(git -C "$WORK" rev-parse HEAD)"
    echo "built: $(date -u +%FT%TZ)"
    echo "image: manylinux_2_28_x86_64"
    echo "isa: AVX AVX2 FMA F16C (no AVX512)"
    echo "openmp: off"
} > "$OUT/BUILD_INFO.txt"
( cd "$OUT" && sha256sum llama-server > SHA256SUMS )
cat "$OUT/BUILD_INFO.txt"
