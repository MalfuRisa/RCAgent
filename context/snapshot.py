"""One consistent workspace snapshot, independent of role/domain projections."""
import json
from config import settings


async def read_snapshot(binding):
    """One transaction, one workspace revision for the whole call (§7.1)."""
    def snapshot(db):
        view = {kind: binding.repository.list(db, binding.incident_id, kind)
                for kind in ("resource", "task", "evidence", "assessment", "read_receipt", "question",
                        "observation", "judgment", "stage_commit", "checkpoint", "material_usage", "report_packet", "recovery_issue")}
        view["scope"] = json.loads(db.execute("SELECT scope FROM incidents WHERE id=?", (binding.incident_id,)).fetchone()[0])
        view["workspace_revision"] = db.execute(
            "SELECT COALESCE(MAX(seq),0) FROM events WHERE incident=?", (binding.incident_id,)).fetchone()[0]
        return view
    view = await binding.repository.transact(snapshot)
    view['query_window'] = binding.query_window
    view['mcp_budget'] = settings.MAX_TOOL_CALLS
    return view


