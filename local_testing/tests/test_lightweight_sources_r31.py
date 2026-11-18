"""R3.1: IDs, versions and exact read values, without digest gates."""
import json
from test_evidence_references_r3 import StableReferencesR3
from workspace.artifacts import read_artifact_raw
from workspace.evidence_references import resolve
from workspace.reference_registration import register
from workspace.receipts import matches_read
from workspace.repository import WorkspaceError


class LightweightSourcesR31(StableReferencesR3):
    async def artifact(self, chosen):
        evidence = await self.b.repository.read(
            self.b.incident_id, chosen["proposal"]["source_ref"]["evidence_id"], "evidence"
        )
        return await self.b.repository.read(self.b.incident_id, evidence["artifact_id"], "artifact")

    async def test_missing_and_wrong_legacy_hashes_allow_exact_registration(self):
        chosen = await self.selected()
        artifact = await self.artifact(chosen)
        original = await read_artifact_raw(self.b, artifact["id"])
        for marker in (None, "obsolete-digest"):
            artifact = await self.b.repository.read(self.b.incident_id, artifact["id"], "artifact")
            artifact.pop("sha256", None)
            if marker is not None:
                artifact["sha256"] = marker
            await self.b.repository.save(self.b.incident_id, "artifact", artifact, artifact["revision"])
            self.assertEqual(await read_artifact_raw(self.b, artifact["id"]), original)
        chosen["expected_value_hash"] = "obsolete-digest"
        chosen["proposal"]["source_ref"]["source_hash"] = "obsolete-digest"
        await self.b.repository.save(self.b.incident_id, "candidate", chosen, chosen["revision"])
        result = await register(self.b, await self.submission([chosen["id"]]))
        self.assertTrue(result["observation_ids"])

    async def test_missing_or_invalid_original_fails_without_formal_writes(self):
        chosen = await self.selected()
        artifact = await self.artifact(chosen)
        path = self.b.repository.root / artifact["storage_reference"]
        content = path.read_bytes()
        path.write_text("invalid JSON")
        with self.assertRaisesRegex(WorkspaceError, "source_unavailable"):
            await register(self.b, await self.submission([chosen["id"]]))
        path.unlink()
        with self.assertRaisesRegex(WorkspaceError, "source_unavailable"):
            await resolve(self.b, chosen["id"])
        path.write_bytes(content)
        self.assertEqual(await self.b.repository.all(self.b.incident_id, "observation"), [])
        self.assertTrue((await register(self.b, await self.submission([chosen["id"]])))["observation_ids"])

    async def test_receipt_compares_actual_body_and_exact_value_changes_still_fail(self):
        from workspace.artifacts import write_artifact
        body = {"read_receipt_id": "r", "range": [0, 1], "items": [1234567890123456789]}
        artifact = await write_artifact(self.b, body)
        await self.b.repository.save(self.b.incident_id, "artifact", artifact)
        receipt = {"result_artifact_id": artifact["id"], "result_hash": "obsolete"}
        self.assertTrue(await matches_read(self.b, receipt, body))
        self.assertFalse(await matches_read(self.b, receipt, {**body, "range": [0, 2]}))
        self.assertFalse(await matches_read(self.b, receipt, {**body, "items": [0]}))
        chosen = await self.selected()
        artifact = await self.artifact(chosen)
        raw = await read_artifact_raw(self.b, artifact["id"])
        from workspace.observations import pointer_tokens
        parts = pointer_tokens(chosen["proposal"]["source_ref"]["json_pointer"])
        node = raw
        for part in parts[:-1]:
            node = node[int(part)] if isinstance(node, list) else node[part]
        node[int(parts[-1]) if isinstance(node, list) else parts[-1]] = -1
        (self.b.repository.root / artifact["storage_reference"]).write_text(json.dumps(raw))
        with self.assertRaisesRegex(WorkspaceError, "source_stale: exact value changed"):
            await resolve(self.b, chosen["id"])

    def test_native_resource_metadata_exposes_numeric_target_filter(self):
        from platforms.insights_v5.diagnostic_contracts import metadata_names
        fields = metadata_names({"data": {"list": [{"list": {
            "pod": {"tag_name": "pod", "type": "resource"},
            "label": {"tag_name": "label", "type": "string"}
        }}]}})
        self.assertIn("pod_id", fields)
        self.assertNotIn("label_id", fields)

    async def test_resource_without_optional_cluster_accepts_bound_identity(self):
        from platforms.insights_v5.diagnostic_identity import IdentityAccess
        from tools.diagnostic_catalog import DiagnosticResultError
        known = (await self.b.repository.all(self.b.incident_id, "resource"))[0]
        known.update(platform_id=259, platform_lcuuid="current-uid", platform_cluster_id=1)
        await self.b.repository.save(self.b.incident_id, "resource", known, known["revision"])
        identity = {"id":259,"name":known["name"],"lcuuid":"current-uid",
                    "pod_cluster_id":1,"deleted":False}
        class Client:
            async def call(inner, *args):
                return {"data": {known["kind"]: identity}, "meta": {}}
        access = IdentityAccess(Client(), [], None)
        args = {"query": {known["kind"]+"_id":259}}
        self.assertEqual((await access.resource(self.b, args)).raw["data"][known["kind"]], identity)
        with self.assertRaisesRegex(DiagnosticResultError, "requested cluster identity missing"):
            await access.resource(self.b, {"query": {**args["query"], "include":"cluster"}})
        identity["pod_cluster_id"] = 999
        with self.assertRaisesRegex(DiagnosticResultError, "identity changed"):
            await access.resource(self.b, args)
        identity["pod_cluster_id"] = 1
        identity["lcuuid"] = "different-lifecycle"
        with self.assertRaisesRegex(DiagnosticResultError, "identity changed"):
            await access.resource(self.b, args)
