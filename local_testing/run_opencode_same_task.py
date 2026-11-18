"""One clean OpenCode/native-MCP experiment; no RCAgent role or evidence adapters."""
import asyncio
import json
import os
import signal
import tempfile
import time
from pathlib import Path

from config import settings
from local_testing.preflight_v5_cpu import save
from local_testing.v5_gateway_session import gateway
from tools.diagnostic_client import V5DiagnosticClient
from platforms.insights_v5.diagnostic_policy import NATIVE_READS

ROOT = Path("/mnt/d/Business/Agents/RCAgent/学习材料归档/04-Agent学习/改动优化/观测提交与调查收尾Skill-2026-10-29/R3-2026-10-30")
OUTPUT = ROOT / "opencode-same-task-01"
CLI = "/root/.opencode/bin/opencode"
PROMPT = """目标：rd2 集群，kylin-insights 命名空间，Pod agent-d9l62。观察时段：2026-11-01 12:14:42—12:29:42（Asia/Shanghai，UTC+08:00）。

请观察 rd2 集群 kylin-insights 命名空间 Pod agent-d9l62 在给定15分钟窗口内的运行情况。预查已看到该 Pod 有网络活动；请自主核对实际可用数据，说明可见现象、支持依据、反证与缺口，并形成有据报告。正常数据也可分析，不要求找到故障；没有依据不得确认根因或把有流量等同整体健康。

通过本次已连接的 insights MCP 进行真实只读查询。请直接给出最终中文分析；接口不支持或证据不足时说明具体缺口。"""


def public(value):
    """Persist public text/tool output only, never hidden reasoning blocks."""
    if isinstance(value, dict):
        if value.get("type") in ("reasoning", "reasoning-start", "reasoning-delta", "reasoning-end"):
            return None
        return {k: public(v) for k, v in value.items() if k not in ("reasoning_content", "reasoningText")}
    if isinstance(value, list):
        return [item for v in value if (item := public(v)) is not None]
    return value


async def command(args, env, cwd, timeout=60):
    proc = await asyncio.create_subprocess_exec(CLI, *args, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, stdout.decode(), stderr.decode()


def configuration(url, token, work):
    provider = "rca-same-task"
    model = provider + "/" + settings.LLM_MODEL
    read_paths = ["/root/.local/share/opencode/tool-output/*", "/tmp/opencode/tool-output/*"]
    paths = {"*": "deny", **{p: "allow" for p in read_paths}, str(work / "*"): "allow"}
    permission = {"*": "deny", "read": paths, "grep": "allow", "glob": "allow",
        "external_directory": {"*": "deny", **{p: "allow" for p in read_paths}}, "todowrite": "allow"}
    tools = {"*": False, "read": True, "grep": True, "glob": True, "todowrite": True}
    for name in sorted(NATIVE_READS):
        permission["insights_" + name] = "allow"
        tools["insights_" + name] = True
    return {"$schema": "https://opencode.ai/config.json", "model": model, "small_model": model,
        "enabled_providers": [provider], "autoupdate": False, "share": "disabled",
        "instructions": [], "plugin": [], "snapshot": False,
        "provider": {provider: {"npm": "@ai-sdk/openai-compatible", "name": "Same RCAgent endpoint",
            "options": {"baseURL": settings.LLM_BASE_URL, "apiKey": settings.LLM_API_KEY},
            "models": {settings.LLM_MODEL: {"id": settings.LLM_MODEL, "name": settings.LLM_MODEL}}}},
        "mcp": {"insights": {"type": "remote", "url": url, "oauth": False,
            "headers": {"Authorization": "Bearer " + token}, "enabled": True}},
        "permission": permission, "tools": tools, "agent": {"build": {"permission": permission}}}


async def run():
    OUTPUT.mkdir(exist_ok=False)
    (OUTPUT / "initial-message.txt").write_text(PROMPT)
    os.environ["RCAGENT_V5_GATEWAY_BINARY"] = "/mnt/d/Business/Agents/RCAgent/var/bin/rcagent-r31-v5-http-gateway"
    with tempfile.TemporaryDirectory(prefix="opencode-same-task-") as temporary:
        work = Path(temporary)
        async with gateway(OUTPUT / "gateway") as (url, token):
            async with V5DiagnosticClient(url, token) as client:
                identity = await client.call("preflight", "insights_k8s_resource",
                    {"query": {"pod_id": 259, "include": "node,cluster"}})
                if (identity["data"]["pod"]["lcuuid"] != "faa7fb07-7357-4c03-8bef-e15b266c3b0a"
                        or identity["data"]["cluster"]["name"] != "rd2"):
                    raise ValueError("Original Pod identity changed; model experiment not started")
                body = json.loads((ROOT / "live-sampled/sample-04-request.json").read_text())["calls"][0][2]
                sample = await client.call("preflight", "insights_query_metric", body)
                if not sample["data"].get("list"):
                    raise ValueError("Original time window no longer has a sample")
                listed = await client.session.list_tools()
                save(OUTPUT / "tools.json", [{"name": t.name, "description": t.description,
                    "readOnlyHint": t.annotations.readOnlyHint if t.annotations else None,
                    "enabled_for_experiment": t.name in NATIVE_READS} for t in listed.tools])
                save(OUTPUT / "preflight.json", {"identity": identity, "sample": sample,
                    "calls": client.calls, "model_requests": 0})
            conf = configuration(url, token, work)
            env = {**os.environ, "OPENCODE_CONFIG_CONTENT": json.dumps(conf)}
            def redact(text):
                return text.replace(settings.LLM_API_KEY, "[REDACTED]").replace(token, "[REDACTED]")
            code, raw, err = await command(["debug", "config", "--pure"], env, work)
            if code:
                (OUTPUT / "configuration-error.txt").write_text(redact(err + raw))
                raise RuntimeError("OpenCode configuration rejected before model start")
            resolved = json.loads(raw)
            provider = resolved["provider"]["rca-same-task"]
            if (resolved["model"] != conf["model"] or provider["options"]["baseURL"] != settings.LLM_BASE_URL
                    or provider["options"]["apiKey"] != settings.LLM_API_KEY):
                raise ValueError("Effective model endpoint/credential alignment failed")
            safe = json.loads(redact(json.dumps(resolved)))
            save(OUTPUT / "effective-config-redacted.json", safe)
            code, raw, err = await command(["mcp", "list", "--pure"], env, work)
            (OUTPUT / "mcp-connection.txt").write_text(redact(raw + err))
            if code or "connected" not in raw.lower():
                raise RuntimeError("OpenCode MCP connection not confirmed; no model start")
            code, raw, err = await command(["debug", "skill", "--pure"], env, work)
            (OUTPUT / "skills.txt").write_text(redact(raw + err))
            save(OUTPUT / "conditions.json", {"opencode_version": "1.18.31", "model": conf["model"],
                "endpoint": settings.LLM_BASE_URL, "credentials_match_rcagent": True,
                "work_directory": str(work), "initial_work_files": list(work.iterdir()),
                "external_plugins": False, "global_custom_instructions_found": False,
                "allowed_native_tools": sorted(NATIVE_READS), "model_timeout_seconds": settings.INVESTIGATION_TIMEOUT_SECONDS,
                "temperature_output_reasoning": "OpenCode/provider defaults; no variant override",
                "actual_wire_requests": "unknown; public step events are counted separately"})
            started = time.monotonic()
            proc = await asyncio.create_subprocess_exec(CLI, "run", "--pure", "--agent", "build", "--format", "json",
                "--model", conf["model"], "--dir", str(work), PROMPT, cwd=work, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
                limit=16 * 1024 * 1024)
            session = None
            event_count = 0
            async def stream():
                nonlocal session, event_count
                with (OUTPUT / "events.jsonl").open("w") as out:
                    async for line in proc.stdout:
                        try:
                            obj = public(json.loads(line))
                            if obj is None:
                                continue
                            session = obj.get("sessionID", session)
                            event_count += 1
                            obj["observed_elapsed_seconds"] = round(time.monotonic() - started, 3)
                            out.write(redact(json.dumps(obj, ensure_ascii=False)) + "\n")
                            out.flush()
                        except ValueError:
                            with (OUTPUT / "non-json-stdout.txt").open("a") as other:
                                other.write(redact(line.decode()))
            async def errors():
                with (OUTPUT / "stderr.txt").open("w") as out:
                    async for line in proc.stderr:
                        out.write(redact(line.decode()))
                        out.flush()
            readers = [asyncio.create_task(stream()), asyncio.create_task(errors())]
            timed_out = False
            try:
                await asyncio.wait_for(proc.wait(), settings.INVESTIGATION_TIMEOUT_SECONDS)
            except TimeoutError:
                timed_out = True
                os.killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), 5)
                except TimeoutError:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
            finally:
                await asyncio.gather(*readers)
            save(OUTPUT / "execution.json", {"elapsed_seconds": time.monotonic() - started,
                "returncode": proc.returncode, "timed_out": timed_out, "session_id": session,
                "public_events": event_count, "own_opencode_process_exited": proc.returncode is not None})
            if session:
                code, raw, err = await command(["export", session, "--pure"], env, work)
                if code == 0:
                    try:
                        save(OUTPUT / "session-public.json", public(json.loads(raw)))
                    except ValueError:
                        (OUTPUT / "export-error.txt").write_text(
                            "CLI export returned incomplete JSON; complete public JSON events retained. No model retry.")
                else:
                    (OUTPUT / "export-error.txt").write_text(redact(err))
            env.pop("OPENCODE_CONFIG_CONTENT", None)
    save(OUTPUT / "cleanup.json", {"own_gateway_exited": True, "own_work_directory_removed": True,
        "persistent_config_changed": False, "credentials_written_to_config_file": False})


if __name__ == "__main__":
    asyncio.run(run())
