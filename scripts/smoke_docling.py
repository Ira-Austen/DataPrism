"""离线运行 Docling 解析夹具 PDF，验证模型能从本地目录加载并正确识别表格，记录每页耗时。

用法：python scripts/smoke_docling.py --artifacts models/layout --input sample.pdf --out results.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

EXPECTED_ROWS = 5
EXPECTED_COLS = 4


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    options = PdfPipelineOptions(artifacts_path=str(args.artifacts))
    options.do_ocr = False
    options.do_table_structure = True
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})

    started = time.perf_counter()
    result = converter.convert(str(args.input))
    elapsed = time.perf_counter() - started
    doc = result.document
    pages = max(len(doc.pages), 1)

    tables = []
    for table in doc.tables:
        frame = table.export_to_dataframe(doc=doc)
        tables.append({"rows": int(frame.shape[0]) + 1, "cols": int(frame.shape[1])})  # +1 计入表头
    ok = any(t["rows"] == EXPECTED_ROWS and t["cols"] == EXPECTED_COLS for t in tables)

    summary = {
        "status": str(result.status),
        "pages": pages,
        "elapsed_s": round(elapsed, 2),
        "per_page_s": round(elapsed / pages, 2),
        "tables": tables,
        "table_shape_ok": ok,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(doc.export_to_markdown()[:1500])
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
