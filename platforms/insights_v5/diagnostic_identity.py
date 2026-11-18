"""Scoped platform identity bootstrap, refresh and result validation."""

import copy
from datetime import datetime, timezone
from tools.diagnostic_catalog import Capability, QueryResult, DiagnosticResultError
from workspace.repository import WorkspaceError, utcnow
from workspace.artifacts import write_artifact


async def resolved(b, resource_id=None):
    rows = await b.repository.all(b.incident_id, "resource")
    known = [r for r in rows if r.get("identity_status") == "resolved" and not r.get("replaced_by")]
    if resource_id:
        known = [r for r in known if r["id"] == resource_id]
    if not known:
        raise WorkspaceError("verified resource identity required")
    from workspace.resources import scope, allowed

    access = await scope(b)
    if any(not allowed(access, r.get("cluster"), r.get("namespace")) for r in known):
        raise WorkspaceError("resource identity lies outside incident scope")
    return known


class IdentityAccess:
    def __init__(self, client, initial_resources, identity_source):
        self.client = client
        self.initial_resources = initial_resources
        self.identity_source = identity_source
        self.identity_checks = {}
        self.catalog = None

    async def fresh_identity(self, b, resource_id=None):
        from config import settings
        from workspace.query_execution import execute_query

        now = datetime.now(timezone.utc)
        for row in await resolved(b, resource_id):
            checked = self.identity_checks.get((b.incident_id, b.run_id, row["id"])) or row.get(
                "verified_at"
            )
            if (
                checked
                and (now - datetime.fromisoformat(checked)).total_seconds()
                < settings.IDENTITY_TTL_SECONDS
            ):
                continue
            key = {"node": "node_id", "pod": "pod_id"}.get(row["kind"])
            if key is None:
                raise WorkspaceError("no verified identity refresh adapter for resource kind")
            result = await execute_query(
                b,
                self.catalog,
                "insights_k8s_resource",
                {"query": {key: row["platform_id"], "include": "cluster"}},
                force_refresh=True,
            )
            evidence = await b.repository.read(
                b.incident_id, result["_rcagent_query"]["evidence_id"], "evidence"
            )
            if evidence["transport_status"] != "ok":
                raise WorkspaceError("identity refresh failed; query denied")

    async def bootstrap(self, b, state):
        if self.identity_source is not None:
            artifact = await write_artifact(
                b,
                {
                    "kind": "trusted_identity_preflight",
                    "source": self.identity_source,
                    "note": "Identity metadata only; not model investigation evidence",
                },
            )
            await b.repository.save(b.incident_id, "artifact", artifact)
        else:
            artifact = None
        for resource in self.initial_resources:
            row = copy.deepcopy(resource)
            from workspace.resources import allowed, scope

            if not allowed(await scope(b), row.get("cluster"), row.get("namespace")):
                raise WorkspaceError("trusted resource lies outside incident scope")
            row["source_refs"] = [artifact["id"]] if artifact else row.get("source_refs", [])
            await b.repository.save(b.incident_id, "resource", row)

    async def normalize_resource(self, b, args):
        query = args.get("query", {})
        keys = [k for k in ("node_id", "pod_id", "cluster_id") if k in query]
        if len(keys) != 1:
            raise WorkspaceError("exactly one platform resource ID is required")
        key = keys[0]
        known = await resolved(b)
        expected = "node" if key == "node_id" else "pod" if key == "pod_id" else "cluster"
        allowed = {r.get("platform_id") for r in known if r["kind"] == expected}
        if key == "cluster_id":
            allowed.update((r.get("platform_cluster_id") for r in known))
        if type(query[key]) is not int or query[key] not in allowed:
            raise WorkspaceError("resource ID has no verified scope binding")
        if query.get("include", "") not in ("", "node,cluster", "cluster", "node"):
            raise WorkspaceError("unbounded resource expansion is not authorized")
        return args

    async def resource(self, b, args):
        raw = await self.client.call("worker", "insights_k8s_resource", args)
        known = await resolved(b)
        try:
            data = raw["data"]
            cluster = data.get("cluster", {})
            query = args["query"]
            includes = {part.strip() for part in query.get("include", "").split(",")}
            cluster_required = "cluster_id" in query or "cluster" in includes
            expected_clusters = {row.get("platform_cluster_id") for row in known}
            if cluster:
                if (cluster.get("name") != known[0]["cluster"]
                        or cluster.get("id") not in expected_clusters
                        or cluster.get("deleted")):
                    raise WorkspaceError("foreign/deleted cluster identity")
            elif cluster_required:
                raise WorkspaceError("requested cluster identity missing")
            for row in known:
                key = {"node": "node_id", "pod": "pod_id"}.get(row["kind"])
                if not key or query.get(key) != row.get("platform_id"):
                    continue
                identity = data.get(row["kind"], {})
                if (
                    identity.get("id") != row["platform_id"]
                    or identity.get("name") != row["name"]
                    or identity.get("deleted")
                    or (identity.get("lcuuid") != row.get("platform_lcuuid"))
                    or identity.get("pod_cluster_id") != row.get("platform_cluster_id")
                ):
                    raise WorkspaceError(
                        "platform resource identity changed; re-discovery required"
                    )
                self.identity_checks[b.incident_id, b.run_id, row["id"]] = utcnow()
        except (WorkspaceError, KeyError, TypeError) as exc:
            raise DiagnosticResultError(
                raw, "resource identity validation failed: " + str(exc)
            ) from exc
        return QueryResult(raw, [r["id"] for r in known])

    def capability(self):
        return Capability(
            "insights_k8s_resource",
            "Read verified platform resource identity/cluster metadata; not runtime health.",
            self.client.schemas["insights_k8s_resource"]["inputSchema"],
            self.normalize_resource,
            self.resource,
            "v5-resource-native-v2",
        )
