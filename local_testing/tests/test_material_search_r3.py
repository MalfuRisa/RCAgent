"""Explicit history mounts never grant current-run field authority."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from config import settings
from local_testing.skill_fixture import seed
from workspace.runtime import bind
from workspace.investigation_materials import (
    search_investigation_materials,
    read_investigation_material,
)
from workspace.reference_registration import register, ReferenceRegistration
from workspace.repository import WorkspaceError
from workspace import investigation_phases as phases


class MaterialSearchR3(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        _, self.b, _, self.data = await seed(self.root / "workspace", "E1")
        archive = self.root / "archives" / "case"
        archive.mkdir(parents=True)
        (archive / "report.md").write_text(
            "Historical interpretation; not current health. " * 100
        )
        (archive / "outcome.json").write_text(json.dumps({"status": "inconclusive"}))
        manifest = self.root / "history.json"
        manifest.write_text(
            json.dumps(
                {
                    "cases": [
                        {
                            "root": "case",
                            "case_id": "historic-fixture",
                            "files": ["report.md", "outcome.json"],
                            "verification_type": "deterministic_fixture",
                            "conclusion_status": "inconclusive",
                        }
                    ]
                }
            )
        )
        for name, value in [
            ("INVESTIGATION_HISTORY_ROOT", self.root / "archives"),
            ("INVESTIGATION_HISTORY_MANIFEST", manifest),
        ]:
            p = patch.object(settings, name, value)
            p.start()
            self.addCleanup(p.stop)

    async def test_history_paging_is_versioned_and_cannot_register_current_facts(self):
        with bind(self.b):
            found = await search_investigation_materials.ainvoke(
                {"scope": "history", "limit": 1}
            )
            self.assertTrue(found["next_cursor"])
            with self.assertRaisesRegex(WorkspaceError, "invalid_cursor"):
                await search_investigation_materials.ainvoke(
                    {"scope": "current", "cursor": found["next_cursor"]}
                )
            material = found["items"][0]["material_id"]
            page = await read_investigation_material.ainvoke(
                {"material_id": material, "max_chars": 1024}
            )
            self.assertEqual(page["origin"], "history")
            self.assertTrue(page["truncated"])
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), 1024)
            task = await self.b.repository.read(
                self.b.incident_id, self.b.task_id, "task"
            )
            with self.assertRaisesRegex(WorkspaceError, "unknown_ref"):
                await register(
                    self.b,
                    ReferenceRegistration(
                        ref_ids=[material], question_ids=task["question_ids"]
                    ),
                )
            with patch.object(settings, "WORKER_EXPLORATION_STEPS", 0):
                await phases.initialize(self.b)
            with self.assertRaisesRegex(WorkspaceError, "phase_disallowed"):
                await search_investigation_materials.ainvoke({"scope": "history"})
            again = await read_investigation_material.ainvoke(
                {
                    "material_id": material,
                    "cursor": page["next_cursor"],
                    "max_chars": 1024,
                }
            )
            self.assertEqual(again["range"][0], page["range"][1])
            (self.root / "archives/case/report.md").write_text(
                "Changed historical source"
            )
            with self.assertRaisesRegex(WorkspaceError, "stale"):
                await read_investigation_material.ainvoke(
                    {"material_id": material, "cursor": again["next_cursor"]}
                )
        self.assertEqual(
            await self.b.repository.all(self.b.incident_id, "observation"), []
        )

    async def test_current_search_is_only_a_locator_and_reader_is_bounded(self):
        with bind(self.b):
            found = await search_investigation_materials.ainvoke(
                {"scope": "current", "filters": {"source_kind": "evidence"}}
            )
            self.assertTrue(found["items"])
            self.assertEqual(
                await self.b.repository.all(self.b.incident_id, "candidate"), []
            )
            page = await read_investigation_material.ainvoke(
                {
                    "material_id": found["items"][0]["material_id"],
                    "selector": "data.list",
                    "max_chars": 2048,
                }
            )
            self.assertEqual(page["origin"], "current")
            self.assertIn("read_receipt_id", page["content"])
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=False)), 2048)
            with self.assertRaises(WorkspaceError):
                await read_investigation_material.ainvoke({"material_id": "../../.env"})


if __name__ == "__main__":
    unittest.main()
