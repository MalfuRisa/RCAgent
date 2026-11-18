"""One native OpenCode session, continuous MCP lifetime, streamed public records."""
import asyncio
import json
import os
import signal
import tempfile
import time
from contextlib import AsyncExitStack
from pathlib import Path
from .opencode_config import cli_path, connection, configuration
from config import settings


class OpenCodeSession:
    def __init__(self, outputs, *, cancellation=None, deadline=None, binary=None, connect=None, progress=None):
        self.outputs = outputs
        self.cancellation = cancellation or asyncio.Event()
        self.deadline = deadline
        self.binary = binary or cli_path()
        self.connect = connect or connection
        self.progress = progress or (lambda _: None)
        self.stack = AsyncExitStack()
        self.process = None
        self.env = None

    async def __aenter__(self):
        try:
            work = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="rcagent-opencode-"))
            self.work = Path(work)
            url, token, names = await self.stack.enter_async_context(self.connect(self.outputs.root))
            self.outputs.secrets = [settings.LLM_API_KEY, token]
            config, self.env = configuration(url, token, self.work, names)
            version = await self.command(["--version"])
            self.outputs.runtime_version = version.strip()
            # Resolve merged settings without printing or persisting credentials.
            resolved = json.loads(await self.command(["debug", "config", "--pure"]))
            extra = [name for name, value in resolved.get("mcp", {}).items()
                     if name != "insights" and value.get("enabled", True)]
            for name in extra:
                config["mcp"][name] = {"enabled": False}
            if extra:
                self.env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
                resolved = json.loads(await self.command(["debug", "config", "--pure"]))
            actual = resolved.get("provider", {}).get("rcagent-native", {}).get("options", {})
            if (resolved.get("model") != config["model"] or actual.get("baseURL") != settings.LLM_BASE_URL
                    or actual.get("apiKey") != settings.LLM_API_KEY):
                raise RuntimeError("Resolved OpenCode model configuration differs from requested configuration")
            self.model = config["model"]
            self.outputs.save("runtime.json", {"type": "opencode", "version": version.strip(),
                "model": self.model, "endpoint": settings.LLM_BASE_URL, "credential_alignment": True,
                "native_tools": names, "work_directory": str(self.work), "session_id": None,
                "application_deadline": self.deadline, "extra_mcp_disabled": extra,
                "native_generation_and_context_defaults": "not overridden"})
            return self
        except BaseException:
            await self.stack.aclose()
            raise

    async def command(self, args):
        proc = await asyncio.create_subprocess_exec(self.binary, *args, cwd=self.work, env=self.env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 60)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError(self.outputs.redact(stderr.decode(errors="replace"))[:1500])
        return stdout.decode()

    async def terminate(self):
        proc = self.process
        if proc is None or proc.returncode is not None:
            return
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except TimeoutError:
            os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()

    async def __aexit__(self, *exc):
        try:
            await self.terminate()
        finally:
            self.env = None
            await self.stack.aclose()
            self.outputs.save("cleanup.json", {"own_process_exited": self.process is None or self.process.returncode is not None,
                "own_work_directory_removed": not self.work.exists()})

    async def stream_stdout(self, stream, role):
        buffer = b""
        while chunk := await stream.read(65536):
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                self.consume(line, role)
        if buffer:
            self.consume(buffer, role)

    def consume(self, line, role):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            self.outputs.save(role + "-unparsed-stdout.txt", line.decode(errors="replace"))
            return
        self.outputs.accept(event, role)
        part = event.get("part", {})
        if event.get("type") == "error":
            self.phase_errors.append(self.outputs.redact(json.dumps(event, ensure_ascii=False)))
        if event.get("type") in {"tool_use", "step_finish", "text", "error"}:
            self.progress({"role": role, "event": event.get("type"), "tool": part.get("tool"),
                "session_id": self.outputs.session_id, "elapsed_seconds": round(time.monotonic() - self.outputs.started, 2)})

    async def stream_errors(self, stream, role):
        with (self.outputs.root / (role + "-stderr.txt")).open("a", encoding="utf-8") as out:
            while chunk := await stream.read(65536):
                out.write(self.outputs.redact(chunk.decode(errors="replace")))
                out.flush()

    async def run_phase(self, instruction, role="worker"):
        if self.cancellation.is_set():
            raise asyncio.CancelledError("Investigation cancelled before sending")
        if role != "worker" and not self.outputs.session_id:
            raise RuntimeError("Cannot continue without an explicit session ID")
        self.outputs.save(role + "-instruction.txt", instruction)
        args = [self.binary, "run", "--pure", "--agent", "build", "--format", "json",
                "--model", self.model, "--dir", str(self.work)]
        if self.outputs.session_id:
            args += ["--session", self.outputs.session_id]
        args.append(instruction)
        self.phase_errors = []
        self.process = await asyncio.create_subprocess_exec(*args, cwd=self.work, env=self.env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
        readers = [asyncio.create_task(self.stream_stdout(self.process.stdout, role)),
                   asyncio.create_task(self.stream_errors(self.process.stderr, role))]
        wait = asyncio.create_task(self.process.wait())
        cancelled = asyncio.create_task(self.cancellation.wait())
        try:
            timeout = max(0, self.deadline - time.time()) if self.deadline is not None else None
            done, _ = await asyncio.wait([wait, cancelled], timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                raise asyncio.CancelledError("Investigation cancelled")
            if wait not in done:
                raise TimeoutError("Explicit user deadline reached")
            await asyncio.gather(*readers)
            if self.process.returncode or self.phase_errors:
                raise RuntimeError("OpenCode phase failed: " + "; ".join(self.phase_errors or [str(self.process.returncode)]))
        finally:
            await self.terminate()
            cancelled.cancel()
            await asyncio.gather(cancelled, wait, *readers, return_exceptions=True)
            self.outputs.checkpoint()
        return self.outputs.answer(role)
