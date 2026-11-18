"""The E2E runner must account for Worker tools as well as discovery tools."""

import json
import tempfile
import unittest
from pathlib import Path

from local_testing.run_incident_text_pipeline import summarize_graph_trace


class TestGraphTraceSummary(unittest.TestCase):
    def test_worker_calls_and_local_submission_are_separate(self):
        rows = [
            {"seq": 1, "event": "chain_start", "run_id": "root", "parent_run_id": None,
             "name": "LangGraph"},
            {"seq": 2, "event": "chain_start", "run_id": "worker-12345678",
             "parent_run_id": "root", "name": "rca_agent"},
            {"seq": 3, "event": "tool_start", "run_id": "tool-1",
             "parent_run_id": "worker-12345678", "name": "get_pod_network_summary",
             "input": {"pod_uid": "uid-db"}},
            {"seq": 4, "event": "tool_end", "run_id": "tool-1",
             "parent_run_id": "worker-12345678", "name": "get_pod_network_summary",
             "duration_ms": 35.4,
             "output": {"content": json.dumps({"meta": {"data_gaps": [
                 {"reason": "no_data"}]}, "summary": {}})}},
            {"seq": 5, "event": "tool_start", "run_id": "tool-2",
             "parent_run_id": "worker-12345678", "name": "submit_final_diagnosis",
             "input": {"diagnosis": "inconclusive"}},
            {"seq": 6, "event": "tool_end", "run_id": "tool-2",
             "parent_run_id": "worker-12345678", "name": "submit_final_diagnosis",
             "duration_ms": 1.2, "output": "success"},
            {"seq": 7, "event": "llm_end", "run_id": "llm", "parent_run_id": "worker-12345678"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            path.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
            summary = summarize_graph_trace(path)
        self.assertEqual(summary["gateway_tool_call_count"], 1)
        self.assertEqual(summary["local_submission_count"], 1)
        self.assertEqual(summary["llm_response_count"], 1)
        self.assertEqual(summary["gateway_tool_time_sum_ms"], 35.4)
        self.assertEqual(summary["calls"][0]["data_gaps"], ["no_data"])
        self.assertTrue(summary["calls"][0]["stage"].startswith("worker:"))


if __name__ == "__main__":
    unittest.main()
