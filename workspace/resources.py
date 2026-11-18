"""Resource identities are verified inventory facts, never model guesses."""
import json
from datetime import datetime, timezone
from config import settings
from .repository import WorkspaceError, new_id, utcnow
from .evidence_targets import query_matches


async def scope(binding):
    def read(db):
        row = db.execute("SELECT scope FROM incidents WHERE id=?", (binding.incident_id,)).fetchone()
        if not row:
            raise WorkspaceError("incident not registered")
        return json.loads(row[0])
    return await binding.repository.transact(read)


def allowed(access, cluster, namespace=None):
    return bool(access["cluster"] and cluster == access["cluster"] and
                (namespace is None or "*" in access["namespaces"] or namespace in access["namespaces"]))


async def register(binding, kind, name, namespace=None):
    def add(db):
        resources = binding.repository.list(db, binding.incident_id, "resource")
        for resource in resources:
            if resource["kind"] == kind and resource["name"] == name and resource.get("namespace") == namespace:
                return resource
        if namespace is None:
            matches = [r for r in resources if r["kind"] == kind and r["name"] == name
                       and r["identity_status"] == "resolved" and not r.get("replaced_by")]
            if len(matches) == 1:
                return matches[0]
        return binding.repository.put(db, binding.incident_id, "resource", {
            "id": new_id("resource"), "kind": kind, "name": name,
            "cluster": None, "namespace": namespace, "uid": None,
            "identity_status": "pending", "lookup_scope": {"namespace": namespace},
            "source_refs": [], "candidate_resource_ids": [], "verified_at": None})
    return await binding.repository.transact(add)


def candidates(raw):
    data = raw.get("pods", raw.get("candidates", raw.get("targets", []))) or []
    return data if isinstance(data, list) else []


def incomplete(raw):
    meta = raw.get("meta") or {}
    return bool(raw.get("ambiguous") or raw.get("truncated") or meta.get("truncated") or
                raw.get("status") in ("partial", "error", "ambiguous") or meta.get("status") in ("partial", "error", "ambiguous"))


async def accept_inventory(binding, raw, arguments, evidence_id):
    """Register all candidates; only a complete exact unique result resolves a hint."""
    access = await scope(binding)
    valid = []
    for item in candidates(raw):
        kind = str(item.get("kind", "pod")).lower()
        name = item.get("name") or item.get("pod_name")
        ns = item.get("namespace")
        cluster = item.get("cluster") or arguments.get("cluster")
        uid = item.get("pod_uid") or item.get("uid")
        if not name or not ns or not allowed(access, cluster, ns):
            continue
        valid.append({"kind": kind, "name": name, "cluster": cluster, "namespace": ns, "uid": uid})
    def commit(db):
        resources = binding.repository.list(db, binding.incident_id, "resource")
        for hint in resources:
            requested_kind = 'pod' if arguments.get('pod') else arguments.get('kind')
            if requested_kind in ('pod', 'service') and hint['kind'] != requested_kind:
                continue
            if arguments.get('namespace') and hint.get('namespace') and arguments['namespace'] != hint['namespace']:
                continue
            matches = [v for v in valid if v["name"] == hint["name"] and v["kind"] == hint["kind"]
                       and (not arguments.get("namespace") or v["namespace"] == arguments["namespace"])]
            if requested_kind == 'auto' and not matches:
                continue
            query_name = arguments.get("pod") or arguments.get("query")
            returned_auto_hint = arguments.get("kind") == "auto" and bool(matches)
            returned_backend_pod = arguments.get("verified_backend_inventory") and hint["kind"] == "pod" and bool(matches)
            if query_name != hint["name"] and not returned_auto_hint and not returned_backend_pod:
                continue
            update = dict(hint)
            update["source_refs"] = list(dict.fromkeys(hint["source_refs"] + [evidence_id]))
            update["candidate_resource_ids"] = []
            if len(matches) == 1 and matches[0].get('uid') and not incomplete(raw):
                identity = matches[0]
                if hint.get("verified_at") and hint["namespace"] != identity["namespace"]:
                    # A same-name resource in another namespace is a different entity.
                    if hint.get("uid"):
                        old = dict(hint, id=new_id("resource"), replaced_by=hint["id"])
                        binding.repository.put(db, binding.incident_id, "resource", old, action="identity_scope_changed")
                    candidate = dict(identity, id=new_id("resource"), identity_status="pending", source_refs=[evidence_id], verified_at=None)
                    candidate = binding.repository.put(db, binding.incident_id, "resource", candidate)
                    update.update(identity_status="ambiguous", uid=None, candidate_resource_ids=[candidate["id"]])
                    binding.repository.put(db, binding.incident_id, "resource", update, hint["revision"], "cross_namespace_candidate")
                    continue
                if hint.get("uid") and identity["uid"] != hint["uid"]:
                    old = dict(hint, id=new_id("resource"), replaced_by=hint["id"])
                    binding.repository.put(db, binding.incident_id, "resource", old, action="replaced_identity")
                    update["replaces"] = old["id"]
                update.update(identity, identity_status="resolved", verified_at=utcnow(), freshness=raw.get("freshness", (raw.get("meta") or {}).get("data_freshness", "unknown")))
            else:
                if hint.get("uid"):
                    old = dict(hint, id=new_id("resource"), replaced_by=hint["id"])
                    binding.repository.put(db, binding.incident_id, "resource", old, action="identity_invalidated")
                update["identity_status"] = "ambiguous" if matches else "pending"
                update["uid"] = None
                for identity in matches:
                    candidate = dict(identity, id=new_id("resource"), identity_status="pending",
                                     source_refs=[evidence_id], verified_at=None)
                    candidate = binding.repository.put(db, binding.incident_id, "resource", candidate)
                    update["candidate_resource_ids"].append(candidate["id"])
            binding.repository.put(db, binding.incident_id, "resource", update, hint["revision"], "inventory_checked")
    await binding.repository.transact(commit)


async def ensure_identity(binding, resource_id, required_fields=("cluster", "namespace", "uid"), force=False):
    resource = await binding.repository.read(binding.incident_id, resource_id, "resource")
    verified = resource.get("verified_at")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(verified)).total_seconds() if verified else float("inf")
    if not force and resource["identity_status"] == "resolved" and age < settings.IDENTITY_TTL_SECONDS and all(resource.get(k) for k in required_fields):
        await binding.repository.transact(lambda db: binding.repository.event(db, binding.incident_id, resource_id,
            "identity_reused", {"reason": "within_ttl", "freshness": resource.get("freshness", "unknown")}))
        return {"status": "resolved", "resource": resource}
    access = await scope(binding)
    if not access["cluster"] or resource["kind"] not in ("pod", "service"):
        return {"status": "partial", "resource": resource, "reason": "unknown cluster or kind"}
    from tools.observability_adapter import call_gateway_tool
    ns = resource.get("lookup_scope", {}).get("namespace")
    namespaces = [ns] if ns else [None]
    if "*" not in access["namespaces"]:
        namespaces = [ns] if ns in access['namespaces'] else [] if ns else list(access['namespaces'])
    namespaces = list(dict.fromkeys(namespaces))[:settings.IDENTITY_MAX_ATTEMPTS]
    raw = {}
    for namespace in namespaces:
        args = {"cluster": access["cluster"], "query": resource["name"], "kind": resource["kind"], "limit": 20}
        if namespace:
            args["namespace"] = namespace
        if getattr(binding, 'capability_catalog', None) is not None:
            from .query_execution import execute_query
            raw = await execute_query(binding, binding.capability_catalog, "search_k8s_targets", args,
                                      force_refresh=True)
            await accept_inventory(binding, raw, args, raw['_rcagent_query']['evidence_id'])
        else:
            raw = await call_gateway_tool("search_k8s_targets", args)
        resource = await binding.repository.read(binding.incident_id, resource_id, "resource")
        if resource["identity_status"] == "resolved" and all(resource.get(k) for k in required_fields):
            return {"status": "resolved", "resource": resource}
        if candidates(raw) or incomplete(raw) or raw.get("error"):
            break
    status = "error" if raw.get("error") or raw.get("error_type") or raw.get("status") == "error" else "partial" if incomplete(raw) or resource["identity_status"] == "resolved" else "ambiguous" if resource["identity_status"] == "ambiguous" else "not_found"
    return {"status": status, "resource": resource}


async def check_query(binding, name, args):
    access = await scope(binding)
    cluster, ns = args.get("cluster"), args.get("namespace")
    if name in ("get_trace", "trace_request"):
        # A trace may be expanded only when an earlier scoped query exposed its ID.
        from .queries import read_raw
        for evidence in await binding.repository.all(binding.incident_id, "evidence"):
            raw = await read_raw(binding, evidence["id"])
            if args.get("trace_id") and args["trace_id"] in trace_ids(raw):
                return
        raise WorkspaceError("trace ID has no scoped source")
    if not allowed(access, cluster, ns):
        raise WorkspaceError("query scope outside incident")
    if name in ("resolve_pod", "search_k8s_targets"):
        if ns is None and "*" not in access["namespaces"]:
            raise WorkspaceError("unscoped namespace search prohibited")
        return
    resources = await binding.repository.all(binding.incident_id, "resource")
    if binding.resource_id:
        resources = [r for r in resources if r["id"] == binding.resource_id]
    for r in resources:
        expected_kind = query_kind(name, args)
        if expected_kind and r["kind"] != expected_kind:
            continue
        if r["identity_status"] != "resolved" or r.get("replaced_by") or not allowed(access, r["cluster"], r["namespace"]):
            continue
        if r.get("verified_at") and (datetime.now(timezone.utc) - datetime.fromisoformat(r["verified_at"])).total_seconds() >= settings.IDENTITY_TTL_SECONDS:
            continue
        if query_matches(r, name, args, expected_kind):
            return
    raise WorkspaceError("query does not match a verified resource identity")


async def refresh_for_query(binding, name, args):
    if name in ('resolve_pod', 'search_k8s_targets', 'trace_request', 'get_trace'):
        return
    for resource in await binding.repository.all(binding.incident_id, 'resource'):
        if binding.resource_id and resource['id'] != binding.resource_id:
            continue
        if not query_matches(resource, name, args, query_kind(name, args)):
            continue
        at = resource.get('verified_at')
        if not at or (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds() >= settings.IDENTITY_TTL_SECONDS:
            result = await ensure_identity(binding, resource['id'], force=True)
            if result['status'] != 'resolved' or result['resource'].get('uid') != resource.get('uid') or not query_matches(result['resource'], name, args, query_kind(name, args)):
                raise WorkspaceError('identity_changed_or_unresolved: retry with current verified identity ' + json.dumps(result, ensure_ascii=False))
        return


def query_kind(name, args):
    if name in ('resolve_pod', 'search_k8s_targets'):
        return 'pod' if name == 'resolve_pod' else args.get('kind') if args.get('kind') in ('pod', 'service') else None
    if name in ("get_service_backends", "search_traces"):
        return "service"
    if name == "query_metrics":
        return args.get("target_kind")
    if name in settings.KYLIN_TOOLS_ALLOWED:
        return "pod"
    kind = args.get("kind")
    return kind if kind in ("pod", "service") else None


def trace_ids(value):
    if isinstance(value, dict):
        result = {str(v) for k, v in value.items() if k in ("trace_id", "traceID")}
        for v in value.values():
            result.update(trace_ids(v))
        return result
    if isinstance(value, list):
        return set().union(*(trace_ids(v) for v in value)) if value else set()
    return set()
