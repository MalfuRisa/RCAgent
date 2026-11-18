"""Append native public events and publish readable answers without a fact gate."""
import json
import re
import shutil
import time
from pathlib import Path


def public(value):
    if isinstance(value, dict):
        if value.get("type") in {"reasoning", "reasoning-start", "reasoning-delta", "reasoning-end"}:
            return None
        return {k: public(v) for k, v in value.items()
                if k not in {"reasoning_content", "reasoningText", "apiKey", "Authorization"}}
    if isinstance(value, list):
        return [item for v in value if (item := public(v)) is not None]
    return value


def returned_page(output):
    """Count only returned JSON rows; never trust total as coverage or causality."""
    try:
        raw = json.loads(output) if isinstance(output, str) else output
        data = raw.get("data", {})
        rows = data.get("list") if isinstance(data, dict) else data
        result = {"returned_rows": len(rows) if isinstance(rows, list) else None,
                  "backend_total": data.get("total") if isinstance(data, dict) else None}
        if isinstance(rows, list) and rows and isinstance(rows[0], dict):
            result["fields"] = list(rows[0])
        return result
    except (ValueError, TypeError, AttributeError):
        return {"returned_rows": None, "parse_status": "unparsed"}


class RunOutputs:
    def __init__(self, root, run_id):
        self.root, self.run_id = Path(root), run_id
        self.root.mkdir(parents=True, exist_ok=False)
        self.started = time.monotonic()
        self.session_id = None
        self.runtime_version = None
        self.sources = {}
        self.texts = {}
        self.seen = {}
        self.errors = []
        self.usage = []
        self.lines = 0
        self.secrets = []
        self.copied = {}

    def redact(self, text):
        for secret in self.secrets:
            if secret:
                text = text.replace(secret, "[REDACTED]")
        return text

    def save(self, name, value):
        path = self.root / name
        try:
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2, default=str)
            path.write_text(self.redact(text), encoding="utf-8")
            return str(path)
        except OSError as exc:
            self.errors.append("Archive write failed for " + name + ": " + str(exc))
            return None

    def accept(self, event, role):
        event = public(event)
        if event is None:
            return
        part = event.get("part", {})
        session = event.get("sessionID") or part.get("sessionID")
        if session:
            if self.session_id and self.session_id != session:
                raise RuntimeError("OpenCode continuation returned a different session")
            self.session_id = session
        key = (session, event.get("type"), part.get("id"), part.get("messageID"))
        if self.seen.get(key) == event:
            return
        self.seen[key] = event
        self.lines += 1
        item = {**event, "rcagent_role": role, "observed_elapsed_seconds": round(time.monotonic() - self.started, 3)}
        try:
            with (self.root / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(self.redact(json.dumps(item, ensure_ascii=False)) + "\n")
        except OSError as exc:
            self.errors.append("Event archive write failed: " + str(exc))
        if event.get("type") == "text" and part.get("text"):
            self.texts.setdefault(role, {})[part.get("id", str(self.lines))] = part["text"]
            # Text is durable before optional review/export/index work.
            self.save(role + "-answer.md", self.answer(role))
        if event.get("type") == "step_finish":
            self.usage.append({"role": role, "tokens": part.get("tokens"), "reported_cost": part.get("cost")})
        if event.get("type") == "tool_use":
            state = part.get("state", {})
            call_id = part.get("callID") or part.get("id") or str(self.lines)
            source = {"call_id": call_id, "session_id": session, "role": role,
                "tool": part.get("tool"), "arguments": state.get("input"), "status": state.get("status"),
                "error": state.get("error"), "event_path": str(self.root / "events.jsonl"),
                "event_line": self.lines, "elapsed_seconds": item["observed_elapsed_seconds"],
                "association": "unattributed; inspect actual filters/identity links before attributing to target",
                **returned_page(state.get("output", ""))}
            self.sources[call_id] = source
            self.preserve_external_output(state, source)

    def preserve_external_output(self, state, source):
        # Only native tool-output paths mentioned by this run are copied.
        text = json.dumps(state, ensure_ascii=False)
        roots = [Path.home() / ".local/share/opencode/tool-output", Path("/tmp/opencode/tool-output")]
        candidates = re.findall(r"/(?:root/\.local/share|tmp)/opencode/tool-output/[A-Za-z0-9_.-]+", text)
        for value in candidates:
            path = Path(value).resolve()
            if not any(path.is_relative_to(root.resolve()) for root in roots) or not path.is_file():
                continue
            try:
                destination = self.root / "tool-output" / path.name
                if value not in self.copied:
                    destination.parent.mkdir(exist_ok=True)
                    shutil.copyfile(path, destination)
                    self.copied[value] = str(destination)
                source.setdefault("external_outputs", []).append(self.copied[value])
            except OSError as exc:
                self.errors.append("Tool-output copy failed: " + str(exc))

    def answer(self, role):
        return "\n\n".join(self.texts.get(role, {}).values())

    def checkpoint(self):
        self.save("sources.json", list(self.sources.values()))
        self.save("usage.json", self.usage)
        self.save("archive-status.json", {"errors": self.errors, "session_id": self.session_id,
                  "external_outputs": self.copied})

    def count_summary(self):
        rows = [s for s in self.sources.values() if s.get("returned_rows") is not None]
        return [{k: s.get(k) for k in ("tool", "arguments", "returned_rows", "backend_total", "event_line")}
                for s in rows[-20:]]

    def publish(self, status="completed", limitation=None):
        worker, review = self.answer("worker"), self.answer("review")
        heading = "## 修订后的分析"
        if heading in review:
            notes, body = review.split(heading, 1)
            body = body.strip() or worker
        else:
            notes, body = review, worker or review
        limitations = list(self.errors)
        if limitation:
            limitations.append(limitation)
        if worker and not review:
            limitations.append("复核未完成，保留原始调查正文；不代表所有陈述已核验。")
        business = any(s.get("returned_rows", 0) and (s.get("tool") or "").endswith(
            ("query_list", "query_metric", "flow_log_detail", "app_log_detail", "list_cpu_events"))
            for s in self.sources.values())
        evidence = "mixed" if business and review else "unchecked" if business else "insufficient"
        if not body:
            body = "未取得模型调查正文，已收到的工具结果与错误保留在来源记录中。"
        report = {"run_id": self.run_id, "session_id": self.session_id, "runtime": "opencode",
            "runtime_version": self.runtime_version, "execution_status": status, "answer_source": "model" if worker or review else "program_partial",
            "answer_status": "completed" if worker and review and status == "completed" else "partial" if worker or review else "none",
            "evidence_status": evidence, "root_cause_status": "unresolved", "body": body,
            "worker_answer_path": str(self.root / "worker-answer.md") if worker else None,
            "review_answer_path": str(self.root / "review-answer.md") if review else None,
            "review_notes": [{"issue": notes.strip()}] if notes.strip() else [],
            "sources": list(self.sources.values()), "limitations": limitations, "usage_if_available": self.usage,
            "elapsed_seconds": time.monotonic() - self.started}
        self.checkpoint()
        self.save("result.json", report)
        text = body + "\n\n## 复核记录\n" + (notes.strip() or "未取得复核意见。")
        if limitations:
            text += "\n\n## 执行限制\n" + "\n".join("- " + x for x in limitations)
        text += "\n\n[工具来源索引](" + str(self.root / "sources.json") + ")\n"
        self.save("report.md", text)
        for error in self.errors:
            if error not in report["limitations"]:
                report["limitations"].append(error)
        return report
