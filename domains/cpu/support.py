"""Deprecated helpers delegate to common visibility, contract and stage services."""
import json
from langchain_core.messages import HumanMessage
from agents.investigation_rules import RULES
from workflow.structured import ask_structured
from workspace.observation_selection import current_candidates as candidates,register_observations
from workspace.investigations import active_checkpoint
async def ask(model,schema,payload,phase):
    return await ask_structured(model,schema,[RULES,HumanMessage(content=json.dumps(payload,ensure_ascii=False))],phase=phase)
async def register_selection(b,offered,ids,question_ids,limitations):
    return await register_observations(b,ids,[],question_ids,await active_checkpoint(b,b.role),limitations)
