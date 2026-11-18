"""R1 rollout: all four roles actually read and write the investigation contract.

The scripted model only uses what it can see in the messages. Each role hands off
a stage through its own real output path (Triage/Planner structured output,
Worker submission tool, Supervisor structured decision); the program verifies and
persists it. The assertions look at stored records, checkpoints and events.
"""

import json
import unittest

from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from models.schemas import RCATaskList, SymptomList, SupervisorDecision
import test_acceptance_rework as support
import test_live_parent as parent_support
import test_workspace_graph as graph_support

NEWLINE = chr(10)


def visible_question_id(messages):
    """The official question id as it appears in the workbench the model sees."""
    for message in messages:
        content = getattr(message, "content", "")
        if not isinstance(content, str) or not content.startswith("Incident workbench"):
            continue
        payload = json.loads(content.split(NEWLINE, 1)[1])
        investigation = payload.get("investigation") or {}
        questions = investigation.get("questions") or []
        if questions:
            return questions[0]["question_id"]
        listed = (investigation.get("overview") or {}).get("listed") or []
        if listed:
            return listed[0]
    return None


def handoff(
    key, goal, *, question_ids=(), questions=(), observations=(), judgments=(), dispositions=()
):
    return {
        "client_commit_key": key,
        "stage_goal": goal,
        "completion": "partial",
        "question_ids": list(question_ids),
        "question_proposals": list(questions),
        "observation_proposals": list(observations),
        "judgments": list(judgments),
        "coverage": {"stage": goal},
        "open_items": ["still unresolved"],
        "next_focus": ["continue"],
        "material_dispositions": list(dispositions),
    }


class ContractModel(parent_support.VisibleBatchModel):
    """Visible-only scripted model that hands off a stage after each role step."""

    def __init__(self):
        super().__init__()
        self.worker_submitted = False
        self.injections = []

    def with_structured_output(self, schema):
        async def respond(messages):
            from models.schemas import PacketSelection

            if schema == PacketSelection:
                payload = next(
                    json.loads(m.content.split(NEWLINE, 1)[1])
                    for m in messages
                    if str(m.content).startswith("Incident workbench")
                )["report_packet"]
                return PacketSelection(
                    packet_id=payload["packet_id"],
                    selected_statement_ids=[r["statement_id"] for r in payload["statements"]],
                    conclusion_status="inconclusive",
                    unresolved_question_ids=payload["unresolved_question_ids"],
                )
            view, evidence, resource = self.facts(messages)
            question_id = visible_question_id(messages)
            if schema == SymptomList:
                self.calls.append(("triage", schema.__name__, messages))
                return SymptomList(
                    symptoms=[
                        support.Symptom(
                            potential_symptom="Network errors",
                            resource_type=resource["kind"],
                            affected_resource=resource["name"],
                            evidence="tcp_errors=7",
                        )
                    ],
                    stage_proposal=handoff(
                        "triage-1",
                        "整理异常与未知",
                        question_ids=["q1"],
                        questions=[
                            {
                                "local_id": "q1",
                                "text": "网络计数是否与失败请求有关",
                                "resource_ids": [resource["id"]],
                                "origin_refs": [support.evidence_id(evidence)],
                            }
                        ],
                    ),
                )
            if schema == RCATaskList:
                self.calls.append(("planner", schema.__name__, messages))
                return RCATaskList(
                    rca_tasks=[
                        support.RCATask(
                            priority=1,
                            investigation_goal="Check network observations",
                            target_resource=resource["name"],
                            resource_type=resource["kind"],
                        )
                    ],
                    stage_proposal=handoff(
                        "planner-1",
                        "把问题拆成可核查任务",
                        question_ids=[question_id] if question_id else [],
                        judgments=[
                            {
                                "local_id": "j1",
                                "question_id": question_id,
                                "type": "interpretation",
                                "content": "计数只能说明发生过重传",
                                "limitations": ["机制未验证"],
                            }
                        ],
                    ),
                )
            self.calls.append(("supervisor", schema.__name__, messages))
            claim = {
                "claim_id": "c1",
                "type": "observation",
                "content": "Observed tcp_errors=7",
                "resource_id": resource["id"],
                "supporting_evidence_ids": [support.evidence_id(evidence)],
                "limitations": ["Only this pod/window"],
            }
            return SupervisorDecision(
                final_report={
                    "root_cause": "Unresolved",
                    "affected_resources": [resource["name"]],
                    "evidence_summary": "Observed tcp_errors=7",
                    "investigation_summary": "unverified",
                    "detection": True,
                },
                claims=[claim],
                report_items=[
                    {
                        "section": "observations",
                        "content": "Observed tcp_errors=7",
                        "claim_ids": ["c1"],
                    }
                ],
                stage_proposal=handoff(
                    "supervisor-1",
                    "分批核查并准备声明",
                    question_ids=[question_id] if question_id else [],
                    judgments=[
                        {
                            "local_id": "j3",
                            "question_id": question_id,
                            "type": "interpretation",
                            "content": "声明与观测一致",
                            "limitations": ["范围有限"],
                        }
                    ],
                ),
            )

        return RunnableLambda(respond)

    def bind_tools(self, tools, **kwargs):
        base = super().bind_tools(tools, **kwargs)
        if all(t.name in parent_support.SUPERVISOR_TOOL_NAMES for t in tools):

            async def review(messages):
                result = await base.ainvoke(messages)
                if result.tool_calls:
                    return result
                view, evidence, resource = self.facts(messages)
                qid = visible_question_id(messages)
                continued = any(
                    m.type == "human" and "continued_after_handoff" in str(m.content)
                    for m in messages
                )
                if not continued:
                    return AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "id": "supervisor-stage",
                                "name": "commit_investigation_stage",
                                "args": handoff(
                                    "supervisor-1",
                                    "核查并保留限制",
                                    question_ids=[qid],
                                    judgments=[
                                        {
                                            "question_id": qid,
                                            "content": "机制未验证",
                                            "limitations": ["仅当前窗口"],
                                        }
                                    ],
                                ),
                            }
                        ],
                    )
                observation = view["investigation"]["observations"][0]
                claim = {
                    "claim_id": "packet-c",
                    "type": "observation",
                    "resource_id": resource["id"],
                    "content": "Observed counter",
                    "supporting_evidence_ids": [support.evidence_id(evidence)],
                    "limitations": ["Only this pod/window; no mechanism established"],
                }
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "prepare-packet",
                            "name": "prepare_report_packet",
                            "args": {
                                "claims": [claim],
                                "observation_refs": {"packet-c": [observation["observation_id"]]},
                                "question_dispositions": {
                                    qid: "Unresolved; no request correlation"
                                },
                            },
                        }
                    ],
                )

            return RunnableLambda(review)

        async def respond(messages):
            if self.worker_submitted:
                return await base.ainvoke(messages)
            result = await base.ainvoke(messages)
            calls = getattr(result, "tool_calls", None) or []
            if not (
                isinstance(result, AIMessage)
                and calls
                and calls[0]["name"] == "submit_final_diagnosis"
            ):
                return result
            self.worker_submitted = True
            view, evidence, resource = self.facts(messages)
            question_id = visible_question_id(messages)
            arguments = dict(calls[0]["args"])
            arguments["stage_proposal"] = handoff(
                "worker-1",
                "核对网络观测并交接",
                question_ids=[question_id] if question_id else [],
                observations=[
                    {
                        "local_id": "o1",
                        "source_ref": {
                            "evidence_id": support.evidence_id(evidence),
                            "json_pointer": "/summary/tcp_errors",
                        },
                        "expected_value": 7,
                    }
                ],
                judgments=[
                    {
                        "local_id": "j2",
                        "question_id": question_id,
                        "type": "interpretation",
                        "content": "观测值已核对",
                        "supporting_observation_ids": ["o1"],
                        "limitations": ["仅这一 Pod/窗口"],
                    }
                ],
                dispositions=[
                    {
                        "source_id": support.evidence_id(evidence),
                        "field": "summary.tcp_errors",
                        "disposition": "covered_by",
                        "covered_by": ["o1"],
                    }
                ],
            )
            self.injections.append(sorted(arguments))
            return AIMessage(
                content=result.content or "",
                tool_calls=[dict(call, args=arguments) for call in calls],
            )

        return RunnableLambda(respond)


class InvestigationRollout(unittest.IsolatedAsyncioTestCase):
    run_graph = graph_support.ParentGraphTests.run_graph

    async def asyncSetUp(self):
        from unittest.mock import patch
        from config import settings

        mode = patch.object(settings, "CONTEXT_LIFECYCLE_MODE", "active")
        mode.start()
        self.addCleanup(mode.stop)

    async def test_all_four_roles_read_and_write_the_contract(self):
        """R2: verify the original safety intent with the common role contracts."""
        from local_testing.unified_scenarios import run_scenario

        state, rows, events, sent, model = await run_scenario()
        self.assertIsNone(state.get("execution_error"), state.get("execution_error"))
        self.assertEqual(
            {c["role"] for c in rows["context"]}, {"triage", "planner", "worker", "supervisor"}
        )
        self.assertTrue(all(c["context_policy"] == "unified_sre" for c in rows["context"]))
        self.assertTrue(rows["question"])
        self.assertTrue(rows["task"][0]["question_ids"])
        self.assertTrue(rows["observation"])
        self.assertTrue(rows["stage_commit"])
        self.assertTrue(rows["report_packet"])
        self.assertFalse(state["final_report"]["report_errors"])

    async def join_records(self, rows):
        kinds = (
            "question",
            "observation",
            "judgment",
            "stage_commit",
            "checkpoint",
            "material_usage",
        )
        missing = [kind for kind in kinds if kind not in rows]
        self.assertEqual(missing, [], "the parent flow must persist the contract records")
        return {kind: rows[kind] for kind in kinds}
