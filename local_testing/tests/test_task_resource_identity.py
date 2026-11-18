"""A planned resource ID survives task registration without a name-only alias."""

from models.schemas import RCATask
from workspace.repository import WorkspaceError
from workspace.tasks import register_tasks
from test_workspace import WorkspaceCase


class TaskResourceIdentity(WorkspaceCase):
    async def test_same_name_pods_keep_explicit_namespace_and_uid(self):
        first = (await self.locate())["resource"]
        other = dict(first, id="resource_other_namespace", namespace="app", uid="other-uid")
        other.pop("revision", None)
        await self.b.repository.save(self.b.incident_id, "resource", other)
        selected = (
            await register_tasks(
                self.b,
                [
                    RCATask(
                        priority=1,
                        resource_type="pod",
                        target_resource=first["name"],
                        resource_id=first["id"],
                        investigation_goal="Check this exact Pod",
                    )
                ],
            )
        )[0]
        self.assertEqual(selected.resource_id, first["id"])
        resources = await self.b.repository.all(self.b.incident_id, "resource")
        self.assertEqual(len(resources), 2)
        task = await self.b.repository.read(self.b.incident_id, selected.task_id, "task")
        self.assertEqual(task["target_resource_ids"], [first["id"]])
        self.assertEqual(
            next(r for r in resources if r["id"] == selected.resource_id)["uid"], first["uid"]
        )

    async def test_mismatched_explicit_resource_is_rejected_before_task_registration(self):
        first = (await self.locate())["resource"]
        with self.assertRaises(WorkspaceError):
            await register_tasks(
                self.b,
                [
                    RCATask(
                        priority=1,
                        resource_type="pod",
                        target_resource="another-name",
                        resource_id=first["id"],
                        investigation_goal="Wrong target",
                    )
                ],
            )
        self.assertFalse(await self.b.repository.all(self.b.incident_id, "task"))
