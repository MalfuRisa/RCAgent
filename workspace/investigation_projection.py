"""Bounded original-value handoff shared by closeout and optional review."""

import json

from models.native_capabilities import RESOURCE_TOOLS
from platforms.business_policy import DATA_READS

from .native_checks import references, source_card
from .native_materials import BUSINESS_TOOLS
from .history_evidence import history_candidates
from .published_evidence import checked_history
from .stage_handoff import source_cards, stage_handoff


def compact_history(outputs, window=None):
    history = checked_history(outputs)
    if not history:
        return None
    groups = sorted(history['groups'], key=lambda group: bool(window and group['window']!=window))
    projected = []
    for group in groups[:6]:
        brief = {k:v for k,v in group.items() if k not in {'paths','single_path_candidates','same_time_candidates'}}
        brief['paths'] = [{'path': p['path'], 'source_id': p['source_id'], 'finite_points': p['finite_points']}
                          for p in group['paths'][:8]]
        brief['path_count'] = len(group['paths'])
        brief['single_path_candidates'] = group['single_path_candidates'][:1]
        brief['same_time_candidates'] = [c | {'rising':c['rising'][:3]} for c in group['same_time_candidates'][:2]]
        projected.append(brief)
    return {**history, 'groups': projected, 'groups_shown': len(projected)}


def decision_materials(outputs, main_window=None):
    """Rebuild one current Planner view from receipts and bounded archived evidence."""
    window = {'start':main_window.start, 'end':main_window.end} if main_window else None
    stages = [stage_handoff(outputs, task) for task in outputs.task_results[-8:]]
    relevant = RESOURCE_TOOLS | BUSINESS_TOOLS | DATA_READS
    ids = [sid for sid, source in outputs.sources.items() if source['tool_name'] in relevant]
    selected, size = [], 0
    for card in source_cards(outputs, list(reversed(ids))[:12]):
        length = len(json.dumps(card, ensure_ascii=False))
        if size + length <= 16000:
            selected.append(card)
            size += length
    visible = {card['source_id'] for card in selected}
    for stage in stages:
        target = [outputs.sources[sid] for sid in stage['source_ids'] if sid in ids]
        ok = [s for s in target if s['status']=='ok']
        stage['evidence_state'] = ('not_queried' if not target else 'failed' if not ok else
            'values_received' if any((s.get('business_value_count') or 0)>0 or (s.get('returned_rows') or 0)>0 for s in ok) else
            'queried_empty_or_unconfirmed')
    return {'main_window': window, 'task_results': stages, 'task_count':len(outputs.task_results),
            'evidence':selected, 'other_source_ids': [sid for sid in ids if sid not in visible][:30],
            'sources_received':len(ids), 'history_evidence':compact_history(outputs,window),
            'reading_contract':'任务结算不代表证据充分。按原题及待答条件判断。只有同口径且有相邻有效点才能比较涨幅。没有分钟P95证据时，窗口P95不能回答同刻P95变化。数值、缺失和范围以原文为准。'}


def bounded(value, limit=1800):
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= limit else {"fragment": text[:limit], "truncated": True}


def project_materials(outputs, *, max_sources=12, max_chars=18000):
    """Select mechanically, never claim to identify every semantic counterexample."""
    cited = references(outputs.answer("worker"))
    relevant = [s for s, card in outputs.sources.items()
                if card["tool_name"] in BUSINESS_TOOLS | RESOURCE_TOOLS | DATA_READS]
    anomalous = [s for s, card in outputs.sources.items()
                 if card["status"] != "ok" or card.get("returned_rows") == 0]
    fact_sources = {fact['source_id'] for task in outputs.task_results[-12:]
                    for fact in (task.get('facts') or {}).get('results', [])}
    ordered = [sid for sid in dict.fromkeys(cited + anomalous + relevant) if sid not in fact_sources]
    selected, omitted, used = [], [], 0
    for sid in ordered:
        if sid not in outputs.sources:
            omitted.append({"source_id": sid, "reason": "unknown_source"})
            continue
        source = outputs.sources[sid]
        raw = outputs.materials.raw.get(sid)
        item = {"source_id": sid, "call_id": source.get("call_id"),
                "mcp_call_started": source.get("mcp_call_started"),
                "scope": source_card(source, raw), "error": bounded(source.get("error")),
                "read_entry": {"source_id": sid}, "previews": []}
        if isinstance(raw, dict):
            structured = raw.get("structuredContent")
            data = structured.get("data") if isinstance(structured, dict) else None
            if isinstance(data, dict):
                pointer = "/structuredContent/data/list" if isinstance(data.get("list"), list) else "/structuredContent/data"
            elif structured is not None:
                pointer = "/structuredContent"
            else:
                pointer = "/content" if "content" in raw else ""
            item["previews"].append(outputs.materials.read(sid, pointer, limit=3, max_chars=2200))
        else:
            item["original_unavailable"] = True
        size = len(json.dumps(item, ensure_ascii=False, default=str))
        if len(selected) >= max_sources or used + size > max_chars:
            omitted.append({"source_id": sid, "reason": "projection_capacity"})
            continue
        selected.append(item)
        used += size
    # Index errors/empty results even when their original projection did not fit.
    anomalies = [{k: bounded(outputs.sources[s].get(k), 800) for k in
                  ("source_id", "call_id", "task_id", "status", "error", "returned_rows", "raw_path")}
                 for s in anomalous]
    stages = [stage_handoff(outputs, r) if r.get('facts') else
              {**{k: bounded(r.get(k), 1200) for k in ("task_id", "status", "end_reason", "source_ids", "errors")},
               "answer_excerpt": r.get("answer_text", "")[:1200],
               "answer_truncated": len(r.get("answer_text", "")) > 1200,
               "answer_path": r.get("answer_path")}
              for r in outputs.task_results[-12:]]
    receipts = [{**{k: e.get(k) for k in ("event_id", "type", "call_id", "task_id", "model_round_id", "sequence")},
                 "payload": bounded(e.get("payload"), 1600),
                 "original_entry": {"path": "events.jsonl", "event_id": e["event_id"]}}
                for e in outputs.control_events[-12:]]
    return {"original_values": selected, "omitted_sources": omitted[:40],
            "omitted_source_count": len(omitted), "eligible_source_count": len(ordered),
            "anomaly_index": anomalies[:24], "anomaly_count": len(anomalies),
            "task_results": stages, "task_count": len(outputs.task_results),
            "history_evidence": history_candidates(outputs),
            "execution_receipts": receipts, "execution_receipt_count": len(outputs.control_events),
            "archive_entry": str(outputs.root),
            "limitations": ["Finite mechanical projection; previews are not complete originals.",
                            "Unselected sources are not absent evidence; semantic counterevidence is not exhaustively identified.",
                            "Attribution, applied filters and causal claims remain unverified; consult pointers and scope limits."]}
