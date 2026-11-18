"""Prepare an explicitly supplied report projection through common storage/metering.

Compact history reduction and these report-view semantics remain different policies.
"""
import json
from langchain_core.messages import SystemMessage
from .snapshot import read_snapshot
from .publication import base_manifest, workbench_message, budget_basis, publish, refuse_context
from .request_budget import measure
from .selection import message_chars
from .budget_policy import hard_limit


async def prepare_projected(binding, messages, phase, request, *, projection, audit_fields, refusal_reason):
    snapshot = await read_snapshot(binding)
    payload = projection(binding,snapshot)
    text = json.dumps(payload,ensure_ascii=False,separators=(',',':'))
    visible = [SystemMessage(content=workbench_message(text))]+list(messages)
    manifest = base_manifest(binding,snapshot,phase)
    basis,v1,v2,_ = budget_basis(text,messages,request)
    _,components = measure(visible,request)
    manifest.update(view_version=payload['context_view_version'], **audit_fields,
        injected_evidence_ids=[e['evidence_id'] for e in payload['evidence']],
        input_chars=message_chars(visible),request_chars_v2=v2,message_chars_v1=v1,request_basis='v2',
        budget_enabled=True,nominal_limit=hard_limit(),component_chars=components,
        part_chars={'workbench':len(text),'messages':message_chars(messages)},
        )
    if v2>hard_limit():
        return await refuse_context(binding,manifest,payload,messages,phase,
            reason=refusal_reason,visible=visible)
    return await publish(binding,manifest,visible)
