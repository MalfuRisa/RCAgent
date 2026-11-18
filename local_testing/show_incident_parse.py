"""Show exactly what RCAgent extracts from a natural-language incident report.

This is the evidence entry point for PRD §7 "输入层增加可选 incident_text":
it prints the parsed facts, what stayed unknown and what the resolver decided,
without contacting the Gateway or a model.

Usage (from the RCAgent repository root, inside WSL):

    python local_testing/show_incident_parse.py
    python local_testing/show_incident_parse.py --text "payment 服务最近 15 分钟一直报 500，帮我看看"
    python local_testing/show_incident_parse.py --text "..." --cluster prod-a --namespace payments
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.incident_text import parse_incident_text, resolve_incident_scope  # noqa: E402


#: Demo inputs, including the acceptance example from PRD §10.3.
DEFAULT_CASES = (
    "payment 服务最近 15 分钟一直报 500，帮我看看",
    "checkout service is slow for the last 30 minutes in cluster prod-a",
    "集群 prod-a 命名空间 payments 的 payment 服务一直 timeout",
    "payment 服务一直在报错",
    "帮我看看",
)


def render(text: str, structured: dict | None = None) -> dict:
    facts = parse_incident_text(text)
    scope = resolve_incident_scope({"incident_text": text, **(structured or {})})
    return {
        "incident_text": text,
        "structured_input": structured or {},
        "facts": facts.as_summary(),
        "scope": scope.as_summary(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", action="append", default=None,
                        help="incident text to parse (repeatable)")
    parser.add_argument("--cluster", default=None, help="structured target_cluster")
    parser.add_argument("--namespace", default=None, help="structured target_namespace")
    parser.add_argument("--pod", default=None, help="structured target_resource")
    args = parser.parse_args()

    texts = args.text or list(DEFAULT_CASES)
    structured = {
        key: value
        for key, value in (
            ("target_cluster", args.cluster),
            ("target_namespace", args.namespace),
            ("target_resource", args.pod),
        )
        if value
    }

    payload = [render(text, structured) for text in texts]
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()