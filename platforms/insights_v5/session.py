"""Own one authenticated loopback gateway process; credentials stay in memory."""

import asyncio
import os
import secrets
import socket
from contextlib import asynccontextmanager
from pathlib import Path

import httpx


@asynccontextmanager
async def gateway(output, *, budget=None):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    token = secrets.token_urlsafe(32)
    env = dict(
        os.environ,
        QUERIES_HTTP_BASE_URL=os.environ.get("RCAGENT_V5_QUERIES_HTTP_URL", "http://10.44.56.107:30634"),
        SERVER_HTTP_BASE_URL=os.environ.get("RCAGENT_V5_CONTROLLER_HTTP_URL", "http://10.44.56.107:30417"),
        MCP_GATEWAY_ADDR=f"127.0.0.1:{port}",
        MCP_TOOLSET="observe",
        GATEWAY_AUTH_TOKEN=token,
    )
    log = (output / "gateway.log").open("wb")
    binary = os.environ.get("RCAGENT_V5_GATEWAY_BINARY", str(Path(__file__).resolve().parents[2] / "var/bin/rcagent-r31-v5-http-gateway"))
    process = None
    url = f"http://127.0.0.1:{port}"
    try:
        process = await asyncio.create_subprocess_exec(binary, env=env, stdout=log, stderr=log)
        async with httpx.AsyncClient() as http:
            for _ in range(60):
                if budget: budget.check("investigation")
                if process.returncode is not None:
                    raise RuntimeError("own v5 gateway exited during startup")
                try:
                    response = await http.get(url + "/ping", timeout=max(.01, min(1, budget.remaining("investigation"))) if budget else 1)
                    if response.status_code == 200:
                        if "insights-mcp-v5-http" not in response.text:
                            raise RuntimeError("unexpected gateway identity")
                        break
                except (httpx.ConnectError, httpx.TimeoutException):
                    pass
                await asyncio.sleep(0.2)
            else:
                raise RuntimeError("own v5 gateway did not become ready")
            denied = await http.post(url + "/mcp", json={})
            if denied.status_code != 401:
                raise RuntimeError("gateway inbound authentication not enforced")
        yield url + "/mcp", token
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await asyncio.wait_for(process.wait(), 2)
        log.close()
        import json

        (output / "cleanup.json").write_text(
            json.dumps(
                {
                    "own_gateway_started": process is not None,
                    "own_gateway_exited": process is None or process.returncode is not None,
                }
            ),
            encoding="utf-8",
        )
