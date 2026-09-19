"""生成冒烟验证用的 PDF 夹具：标题、段落、一张带网格线的表格。

只用 ASCII 文本，避免依赖 CJK 字体文件；中文与无框线表格的夹具由真实样本提供。
用法：python scripts/make_fixture_pdf.py <输出路径>
"""

from __future__ import annotations

import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

EXPECTED_ROWS = 5  # 含表头，smoke_docling.py 据此断言
EXPECTED_COLS = 4


def build(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=A4)
    story = [
        Paragraph("Purchase Contract No. PC-2026-0917", styles["Title"]),
        Paragraph(
            "Party A: Alpha Trading Co., Ltd. Party B: Beta Supply Inc. "
            "Signed on 2026-09-17 in Beijing. Total amount: 1,250,000.00 CNY. "
            "Delivery within 30 days after signing.",
            styles["BodyText"],
        ),
        Spacer(1, 12),
        Paragraph("Item List", styles["Heading2"]),
    ]
    rows = [
        ["No.", "Item", "Qty", "Unit Price"],
        ["1", "Laptop", "20", "6500.00"],
        ["2", "Monitor", "20", "1800.00"],
        ["3", "Docking Station", "20", "900.00"],
        ["4", "Keyboard", "40", "150.00"],
    ]
    assert len(rows) == EXPECTED_ROWS and len(rows[0]) == EXPECTED_COLS
    table = Table(rows)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
            ]
        )
    )
    story.append(table)
    doc.build(story)


if __name__ == "__main__":
    build(Path(sys.argv[1]))
    print(f"已生成 {sys.argv[1]}")
