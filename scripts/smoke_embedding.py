"""用 onnxruntime 离线运行 bge-small-zh-v1.5，验证字段名相似度是否符合预期并记录耗时。

用法：python scripts/smoke_embedding.py --model-dir models/embedding --out results.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

# 每组：应当相近的一对，以及一个应当远离的干扰项
CASES = [
    ("联系电话", "手机号", "合同金额"),
    ("签订日期", "签约时间", "甲方"),
    ("供应商名称", "乙方单位", "数量"),
]


class Embedder:
    def __init__(self, model_dir: Path) -> None:
        self.tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
        self.tokenizer.enable_truncation(max_length=512)
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 4
        self.session = ort.InferenceSession(str(model_dir / "model.onnx"), opts, providers=["CPUExecutionProvider"])
        self.input_names = {i.name for i in self.session.get_inputs()}

    def encode(self, texts: list[str]) -> np.ndarray:
        encoded = self.tokenizer.encode_batch(texts)
        max_len = max(len(e.ids) for e in encoded)
        ids = np.zeros((len(texts), max_len), dtype=np.int64)
        mask = np.zeros_like(ids)
        for row, e in enumerate(encoded):
            ids[row, : len(e.ids)] = e.ids
            mask[row, : len(e.ids)] = 1
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self.input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self.session.run(None, feeds)[0]
        cls = hidden[:, 0, :]  # bge 用 CLS 池化
        return cls / np.linalg.norm(cls, axis=1, keepdims=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    started = time.perf_counter()
    embedder = Embedder(args.model_dir)
    load_s = time.perf_counter() - started

    texts = [t for case in CASES for t in case]
    embedder.encode(texts[:1])  # 预热
    started = time.perf_counter()
    vectors = embedder.encode(texts)
    per_item_ms = (time.perf_counter() - started) * 1000 / len(texts)

    failures = []
    report = []
    for i, (a, b, far) in enumerate(CASES):
        va, vb, vf = vectors[3 * i], vectors[3 * i + 1], vectors[3 * i + 2]
        near = float(va @ vb)
        distant = float(va @ vf)
        report.append({"pair": [a, b], "near": round(near, 3), "distractor": far, "far": round(distant, 3)})
        if near <= distant:
            failures.append((a, b, far))
        print(f"{a} ~ {b}: {near:.3f}    {a} ~ {far}: {distant:.3f}")

    print(f"加载 {load_s:.2f}s，单条 {per_item_ms:.1f}ms，维度 {vectors.shape[1]}")
    summary = {"load_s": round(load_s, 2), "per_item_ms": round(per_item_ms, 2), "dim": int(vectors.shape[1]), "cases": report}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    if failures:
        print(f"失败：{len(failures)} 组相似度关系不符合预期")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
