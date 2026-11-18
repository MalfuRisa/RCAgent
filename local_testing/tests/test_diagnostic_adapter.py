"""Bounded v5 metric contracts; memory/storage results here are fixtures only."""

import unittest
from types import SimpleNamespace
from workspace.repository import utcnow, WorkspaceError
from workspace.runtime import bind
from workspace.query_execution import execute_query
from platforms.insights_v5.diagnostics import make_catalog
import test_unified_policies as policies


class DiagnosticAdapter(unittest.IsolatedAsyncioTestCase):
    def test_node_and_pod_numeric_ids_are_distinct_resource_namespaces(self):
        from platforms.insights_v5.diagnostic_cpu_events import resource_matches_event

        node = {"kind": "node", "cluster": "rd2", "platform_cluster_id": 1, "platform_id": 7}
        pod = dict(node, kind="pod")
        event = {"pod_cluster": "rd2", "pod_cluster_id": 1, "pod_node_id": 7, "pod_id": 0}
        self.assertTrue(resource_matches_event(node, event))
        self.assertFalse(resource_matches_event(pod, event))
        self.assertTrue(resource_matches_event(pod, dict(event, pod_id=7)))
        self.assertFalse(resource_matches_event(node, dict(event, pod_cluster="foreign")))

    async def asyncSetUp(self):
        await policies.UnifiedPolicies.asyncSetUp(self)
        resource = await self.b.repository.read(self.b.incident_id, "resource_node", "resource")
        resource.update(verified_at=utcnow(), platform_lcuuid="fixture-node", platform_cluster_id=1)
        await self.b.repository.save(
            self.b.incident_id, "resource", resource, expected=resource["revision"]
        )
        self.calls = []

        async def call(role, name, args):
            self.calls.append((name, args))
            if name == "insights_k8s_resource":
                return {
                    "data": {
                        "node": {"id": 7, "name": "test-node", "lcuuid": "fixture-node"},
                        "cluster": {"id": 1, "name": "rd2"},
                    },
                    "meta": {},
                }
            if name == "insights_get_tags":
                return {"data": [{"name": "pod_node_id"}, {"name": "time"}], "meta": {}}
            if name == "insights_meta_metrics":
                return {"data": [{"name": "memory_used"}, {"name": "disk_latency"}], "meta": {}}
            if name == "insights_query_metric":
                return {"data": {"value": 23}, "meta": {}}
            raise AssertionError(name)

        self.client = SimpleNamespace(
            call=call,
            schemas={
                name: {"inputSchema": {"type": "object"}}
                for name in (
                    "insights_list_databases",
                    "insights_list_tables",
                    "insights_get_tags",
                    "insights_meta_metrics",
                    "insights_list_cpu_events",
                    "insights_k8s_resource",
                )
            },
        )
        self.catalog = make_catalog(self.client, service_identity={"platform": "fixture-v5"})

    async def test_memory_and_storage_use_one_metric_adapter_with_target_and_window(self):
        for table, metric in [("system_memory", "memory_used"), ("system_disk", "disk_latency")]:
            await execute_query(
                self.b, self.catalog, "insights_get_tags", {"database": "metrics", "table": table}
            )
            await execute_query(
                self.b,
                self.catalog,
                "insights_meta_metrics",
                {"body": {"database": "metrics", "table": table}},
            )
            result = await execute_query(
                self.b,
                self.catalog,
                "metric_series",
                {
                    "database": "metrics",
                    "table": table,
                    "resource_id": "resource_node",
                    "metric": metric,
                    "aggregation": "Avg",
                },
            )
            self.assertEqual(result["data"]["value"], 23)
            name, args = self.calls[-1]
            self.assertEqual(name, "insights_query_metric")
            self.assertEqual(args["body"]["queries"][0]["where"], "`pod_node_id` = 7")
            self.assertEqual(args["body"]["page_size"], 20)
            self.assertEqual(args["body"]["queries"][0]["select"], "Avg(" + metric + ")")

    async def test_missing_metadata_and_expression_rejected_before_native_query(self):
        with self.assertRaises(WorkspaceError):
            await execute_query(
                self.b,
                self.catalog,
                "metric_series",
                {
                    "database": "metrics",
                    "table": "system",
                    "resource_id": "resource_node",
                    "metric": "x); delete",
                    "aggregation": "Avg",
                },
            )
        self.assertFalse(self.calls)

    async def test_expired_identity_is_refreshed_through_same_query_archive(self):
        resource = await self.b.repository.read(self.b.incident_id, "resource_node", "resource")
        resource["verified_at"] = "2020-01-01T00:00:00+00:00"
        await self.b.repository.save(
            self.b.incident_id, "resource", resource, expected=resource["revision"]
        )
        await execute_query(
            self.b, self.catalog, "insights_get_tags", {"database": "metrics", "table": "system"}
        )
        await execute_query(
            self.b,
            self.catalog,
            "insights_meta_metrics",
            {"body": {"database": "metrics", "table": "system"}},
        )
        await execute_query(
            self.b,
            self.catalog,
            "metric_series",
            {
                "database": "metrics",
                "table": "system",
                "resource_id": "resource_node",
                "metric": "memory_used",
                "aggregation": "Avg",
            },
        )
        self.assertEqual([n for n, a in self.calls].count("insights_k8s_resource"), 1)
        rows = await self.b.repository.all(self.b.incident_id, "evidence")
        self.assertEqual(sum(e["tool_name"] == "insights_k8s_resource" for e in rows), 1)

    async def test_semantically_invalid_original_is_quarantined_and_archived(self):
        from tools.diagnostic_catalog import DiagnosticResultError

        raw = {"data": {"secret_of_failure": "original malformed page"}, "meta": {}}

        async def invalid(b, args):
            raise DiagnosticResultError(raw, "fixture target mismatch")

        cap = self.catalog.get("insights_k8s_resource")
        cap.invoke = invalid
        result = await execute_query(
            self.b, self.catalog, "insights_k8s_resource", {"query": {"node_id": 7}}
        )
        evidence = await self.b.repository.read(
            self.b.incident_id, result["_rcagent_query"]["evidence_id"], "evidence"
        )
        self.assertEqual(evidence["transport_status"], "error")
        from workspace.queries import read_raw

        self.assertEqual(await read_raw(self.b, evidence["id"]), raw)
