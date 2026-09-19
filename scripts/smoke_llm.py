"""对本地 llama-server 做结构化抽取冒烟测试。

只用标准库。验证三件事：JSON Schema 约束输出是否合法、抽取值是否附带可在原文定位的证据、
预填充与生成速度。速度数字仅作相对参考，不代表目标机。

用法：python scripts/smoke_llm.py --base-url http://127.0.0.1:8080 --out results.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

SAMPLES = [
    {
        "text": (
            "采购合同 编号：HT-2026-0917。甲方：华北贸易有限公司，乙方：北方供应股份公司。"
            "本合同于2026年9月17日在北京签订，合同总金额为人民币壹佰贰拾伍万元整（¥1,250,000.00）。"
            "乙方应在合同签订后30日内完成交付。联系人：王强，电话 13800138000。"
        ),
        "fields": ["合同编号", "甲方", "乙方", "签订日期", "合同金额", "联系电话"],
    },
    {
        "text": (
            "关于2026年度设备维护服务的询价函。我单位拟采购服务器维护服务，服务期限自2026年10月1日至2027年9月30日，"
            "预算金额不超过48万元。请于2026年9月25日前提交报价。联系人李芳，电话010-88886666。"
        ),
        "fields": ["服务期限", "预算金额", "报价截止日期", "联系人", "联系电话"],
    },
]


def schema_for(fields: list[str]) -> dict:
    field_schema = {
        "type": ["object", "null"],
        "properties": {
            "value": {"type": "string"},
            "quote": {"type": "string", "description": "原文中包含该值的连续片段"},
        },
        "required": ["value", "quote"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {f: field_schema for f in fields},
        "required": fields,
        "additionalProperties": False,
    }


def call(base_url: str, text: str, fields: list[str], timeout: int) -> tuple[dict, dict]:
    body = {
        "messages": [
            {
                "role": "system",
                "content": "你是信息抽取器。只根据给定文本抽取字段，文本中没有的字段输出 null。quote 必须是原文中连续出现的片段。",
            },
            {"role": "user", "content": f"文本：\n{text}\n\n请抽取字段：{'、'.join(fields)}"},
        ],
        "temperature": 0,
        "max_tokens": 400,
        "response_format": {"type": "json_schema", "json_schema": {"name": "extract", "schema": schema_for(fields)}},
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"{base_url}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    content = payload["choices"][0]["message"]["content"]
    return json.loads(content), payload.get("timings", {})


def normalize(s: str) -> str:
    return "".join(ch for ch in s if not ch.isspace())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8080")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--min-valid-rate", type=float, default=0.8)
    args = parser.parse_args()

    results = []
    total_fields = 0
    grounded = 0
    for sample in SAMPLES:
        started = time.perf_counter()
        try:
            parsed, timings = call(args.base_url, sample["text"], sample["fields"], args.timeout)
            ok = True
        except Exception as exc:  # noqa: BLE001
            parsed, timings, ok = {"error": repr(exc)}, {}, False
        elapsed = time.perf_counter() - started

        field_report = {}
        for f in sample["fields"]:
            total_fields += 1
            item = parsed.get(f) if ok else None
            if isinstance(item, dict) and item.get("quote") and normalize(item["quote"]) in normalize(sample["text"]):
                grounded += 1
                field_report[f] = {"value": item["value"], "grounded": True}
            else:
                field_report[f] = {"value": item, "grounded": False}
        results.append(
            {
                "json_valid": ok,
                "elapsed_s": round(elapsed, 2),
                "prompt_tps": timings.get("prompt_per_second"),
                "gen_tps": timings.get("predicted_per_second"),
                "fields": field_report,
            }
        )
        print(json.dumps(results[-1], ensure_ascii=False, indent=2))

    valid_rate = sum(r["json_valid"] for r in results) / len(results)
    grounded_rate = grounded / total_fields
    summary = {"json_valid_rate": valid_rate, "grounded_rate": grounded_rate, "samples": results}
    print(f"\nJSON 合法率 {valid_rate:.0%}，证据可定位率 {grounded_rate:.0%}")
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if valid_rate >= args.min_valid_rate else 1


if __name__ == "__main__":
    sys.exit(main())
