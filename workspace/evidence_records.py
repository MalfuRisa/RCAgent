"""Source-neutral query provenance; identity, contract and persistence stay explicit."""
from .repository import utcnow


def query_record(binding, evidence_id, name, arguments, artifact, requested, duration, tool_call_id):
    return {'id': evidence_id, 'tool_name': name, 'arguments': arguments,
        'run_id': binding.run_id, 'producer_role': binding.role,
        'producer_task_id': binding.task_id, 'attempt_id': binding.attempt_id,
        'tool_call_id': tool_call_id, 'requested_at': requested, 'finished_at': utcnow(),
        'duration_ms': duration, 'artifact_id': artifact['id']}
