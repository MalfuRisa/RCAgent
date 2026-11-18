"""Gateway identity refresh uses the same query archive as normal diagnostics."""

from tools.diagnostic_catalog import Capability, CapabilityCatalog, QueryResult
from workspace.resources import ensure_identity
from test_workspace import WorkspaceCase


class GatewaySharedIdentity(WorkspaceCase):
    async def test_expired_identity_cannot_be_refreshed_from_the_query_cache(self):
        initial = await self.locate()
        resource_id = initial["resource"]["id"]
        sent = []

        async def normalize(binding, arguments):
            return arguments

        async def invoke(binding, arguments):
            sent.append(arguments)
            return QueryResult(self.response, [resource_id])

        self.b.capability_catalog = CapabilityCatalog(
            [
                Capability(
                    "search_k8s_targets",
                    "Exact scoped identity lookup",
                    {},
                    normalize,
                    invoke,
                    "gateway-identity-fixture",
                )
            ],
            service_identity={"fixture": "gateway"},
        )

        for _ in range(2):
            resource = await self.b.repository.read(self.b.incident_id, resource_id, "resource")
            resource["verified_at"] = "2020-01-01T00:00:00+00:00"
            await self.b.repository.save(
                self.b.incident_id, "resource", resource, expected=resource["revision"]
            )
            result = await ensure_identity(self.b, resource_id)
            self.assertEqual(result["status"], "resolved")

        self.assertEqual(len(sent), 2, "TTL refresh always sends a new identity request")
        evidence = await self.b.repository.all(self.b.incident_id, "evidence")
        shared = [
            row for row in evidence if row.get("contract_version") == "gateway-identity-fixture"
        ]
        self.assertEqual(len(shared), 2)
        self.assertTrue(all(row["artifact_id"] for row in shared))
