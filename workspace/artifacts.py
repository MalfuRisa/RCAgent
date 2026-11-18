"""Program-selected artifact paths and atomic file publication."""
import asyncio
import json
import os
from .repository import new_id, WorkspaceError


async def write_artifact(binding, value):
    artifact_id = new_id("artifact")
    data = json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
    relative = f"artifacts/{binding.incident_id}/{artifact_id}.json"
    path = binding.repository.root / relative
    def publish():
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    await asyncio.to_thread(publish)
    return {"id": artifact_id, "content_type": "application/json", "bytes": len(data),
            'run_id': binding.run_id, 'producer_role': binding.role,
            'task_id': binding.task_id, 'attempt_id': binding.attempt_id,
            "storage_reference": relative}


async def read_artifact_raw(binding, artifact_id):
    artifact = await binding.repository.read(binding.incident_id, artifact_id, "artifact")
    root = binding.repository.root.resolve()
    path = (root / artifact["storage_reference"]).resolve()
    if not path.is_relative_to(root / "artifacts" / binding.incident_id):
        raise WorkspaceError("artifact path outside incident")
    data = await asyncio.to_thread(path.read_bytes)
    return json.loads(data)
