"""Exercise the minimum offline MCP acceptance path without invoking an LLM."""

import asyncio
import json

from tools.mcp_tools import TOOLS


async def main() -> None:
    tools = {tool.name: tool for tool in TOOLS}
    target = {
        "cluster": "test-cluster",
        "namespace": "b01-http-5xx",
        "pod": "checkout-0",
    }
    pod_uid = "uid-b01-http-5xx-checkout-0"

    unknown_raw = await tools["resolve_pod"].ainvoke(
        {**target, "pod": "not-in-scenario"}
    )
    unknown_pod = json.loads(unknown_raw)
    if unknown_pod.get("pods") or unknown_pod.get("meta", {}).get("data_freshness") != "provider_error":
        raise RuntimeError(f"Mock accepted an undeclared Pod: {unknown_pod}")
    unknown_pod = {"rejected": True, "response": unknown_pod}

    results = {
        "resolve_pod": await tools["resolve_pod"].ainvoke(target),
        "get_pod_network_summary": await tools["get_pod_network_summary"].ainvoke(
            {"pod_uid": pod_uid, "cluster": "local"}
        ),
        "get_pod_application_summary": await tools[
            "get_pod_application_summary"
        ].ainvoke({"pod_uid": pod_uid, "cluster": "local"}),
        "diagnose_pod": await tools["diagnose_pod"].ainvoke(
            {
                **target,
                "symptom": "HTTP 5xx",
                "compare_with_peers": False,
            }
        ),
        "unknown_pod": unknown_pod,
    }

    print(
        json.dumps(
            {
                "tool_count": len(TOOLS),
                "called_tools": list(results),
                "results": results,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
