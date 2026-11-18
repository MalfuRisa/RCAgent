"""Extract incident facts from a free-form natural-language description.

Design rules (PRD §7 "RCAgent 怎样使用这五个 Tool"):

* Only facts the user actually stated are extracted. Everything else stays
  :data:`UNKNOWN` -- this module never guesses a cluster, namespace or service.
* The deterministic pass is the authoritative extractor: it is offline and
  unit-testable, and it never needs a model.
* An optional LLM pass may only *propose* values. A proposal is accepted only
  when it is literally grounded in the raw text and the deterministic pass left
  that field unknown. Every rejected proposal is recorded in ``notes`` so the
  behaviour is auditable.
* Values coming from explicit structured graph input always win; see
  :func:`resolve_incident_scope`, which never lets parsed text overwrite a
  structured field that already has a value.

The module deliberately has no import-time dependency on ``config`` (and thus
no import-time LLM client construction) so that it can be imported by tests
without credentials.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import os
import re
from typing import Any, List, Mapping, Optional

from pydantic import BaseModel, Field

UNKNOWN = "unknown"

#: Symptoms this module can name. ``query_metrics``/``search_traces`` receive
#: the raw text separately; the canonical key is used for Triage bookkeeping.
SYMPTOM_HTTP_5XX = "http_5xx"
SYMPTOM_HTTP_4XX = "http_4xx"
SYMPTOM_TIMEOUT = "timeout"
SYMPTOM_LATENCY = "latency"
SYMPTOM_UNAVAILABLE = "unavailable"
SYMPTOM_CONNECTION_ERROR = "connection_error"
SYMPTOM_POD_RESTART = "pod_restart"
SYMPTOM_ERROR = "error"

KNOWN_SYMPTOMS = (
    SYMPTOM_HTTP_5XX,
    SYMPTOM_HTTP_4XX,
    SYMPTOM_TIMEOUT,
    SYMPTOM_LATENCY,
    SYMPTOM_UNAVAILABLE,
    SYMPTOM_CONNECTION_ERROR,
    SYMPTOM_POD_RESTART,
    SYMPTOM_ERROR,
)

#: Default investigation window, matching the Gateway's own default.
DEFAULT_WINDOW_MINUTES = 15
#: The Gateway rejects windows beyond 24h; clamp instead of sending a bad query.
MAX_WINDOW_MINUTES = 24 * 60

DEFAULT_LOOKBACK_ENV = "RCAGENT_DEFAULT_WINDOW_MINUTES"

# Words that can precede "service" in English prose but are not service names.
_SERVICE_STOPWORDS = frozenset(
    {
        "the", "a", "an", "this", "that", "these", "those", "our", "your",
        "my", "its", "his", "her", "their", "some", "any", "each", "every",
        "one", "two", "which", "what", "whose", "and", "or", "not", "no",
        "is", "are", "was", "were", "service", "services", "svc", "app",
        "pod", "pods", "cluster", "namespace", "prod", "production",
    }
)

_SERVICE_NAME = r"[A-Za-z][A-Za-z0-9_.-]*"
# A name must end here. ``\b`` cannot be used after Chinese characters because
# CJK characters are word characters too, so "服务最近" would never match.
_NAME_END = r"(?![A-Za-z0-9_.-])"

# "payment 服务" / "payment service" / "服务 payment" / "service=payment"
_SERVICE_PATTERNS = (
    # Explicit labels win over prose such as "namespace: x service: y",
    # where the preceding-name pattern would otherwise select x.
    re.compile(rf"\b(?:service[_-]?name|service|svc)\s*[:：=]\s*[\"']?(?P<name>{_SERVICE_NAME})", re.IGNORECASE),
    re.compile(rf"服务\s*[:：=]\s*[\"']?(?P<name>{_SERVICE_NAME})"),
    re.compile(rf"(?P<name>{_SERVICE_NAME})\s*(?:服务|service|svc){_NAME_END}", re.IGNORECASE),
    re.compile(rf"(?:服务|service|svc)\s*[:：=]?\s*(?P<name>{_SERVICE_NAME})", re.IGNORECASE),
    re.compile(rf"(?:service[_-]?name|svc)\s*[:：=]\s*[\"']?(?P<name>{_SERVICE_NAME})", re.IGNORECASE),
)

# "payment-7d9f-abcde" style Pod names, only when the user says "pod".
_POD_PATTERNS = (
    re.compile(rf"(?:pod|实例|副本)\s*[:：=]?\s*(?P<name>{_SERVICE_NAME})", re.IGNORECASE),
    re.compile(rf"(?P<name>{_SERVICE_NAME})\s*(?:这个)?\s*(?:pod|实例|副本){_NAME_END}", re.IGNORECASE),
)

_CLUSTER_PATTERNS = (
    re.compile(rf"(?:集群|cluster)\s*[:：=]?\s*(?P<name>{_SERVICE_NAME})", re.IGNORECASE),
    re.compile(rf"(?:target[_-]?cluster|cluster[_-]?name)\s*[:：=]\s*[\"']?(?P<name>{_SERVICE_NAME})", re.IGNORECASE),
    re.compile(rf"\bin\s+(?:the\s+)?(?P<name>{_SERVICE_NAME})\s+cluster\b", re.IGNORECASE),
)

_NAMESPACE_PATTERNS = (
    re.compile(rf"(?:命名空间|namespace|ns)\s*[:：=]?\s*(?P<name>{_SERVICE_NAME})", re.IGNORECASE),
    re.compile(rf"\bin\s+(?:the\s+)?(?P<name>{_SERVICE_NAME})\s+namespace\b", re.IGNORECASE),
)

_MINUTE_UNITS = r"minutes|minute|mins|min|m"
_HOUR_UNITS = r"hours|hour|hrs|hr|h"
_TIME_UNIT = rf"分钟|分|个?小时|钟头|{_MINUTE_UNITS}|{_HOUR_UNITS}"
_HOUR_UNITS_SET = frozenset({"小时", "钟头", "hour", "hours", "hr", "hrs", "h"})

_WINDOW_PATTERNS = (
    # "最近 15 分钟" / "过去 2 小时" / "最近 30 minutes"
    re.compile(
        rf"(?:最近|近|过去|刚刚过去的)\s*(?P<n>\d+)\s*(?P<unit>{_TIME_UNIT})(?![A-Za-z])",
        re.IGNORECASE,
    ),
    # "last 15 minutes" / "past 2 hours"
    re.compile(
        rf"(?:last|past|previous)\s+(?P<n>\d+)\s*(?P<unit>{_TIME_UNIT})(?![A-Za-z])",
        re.IGNORECASE,
    ),
    # "15 分钟内" / "2 小时以来"
    re.compile(r"(?P<n>\d+)\s*(?P<unit>分钟|小时)\s*(?:内|以来|之内)"),
)

_STATUS_CODE = re.compile(r"(?<![A-Za-z0-9.])(?P<code>[1-5]\d{2})(?![A-Za-z0-9])")

_SYMPTOM_KEYWORDS = (
    (SYMPTOM_TIMEOUT, re.compile(r"超时|timeout|timed?\s*out|响应慢到没返回|hang", re.IGNORECASE)),
    (SYMPTOM_CONNECTION_ERROR, re.compile(r"连接失败|连不上|connection\s*(refused|reset)|connect\s*error|ECONNREFUSED", re.IGNORECASE)),
    (SYMPTOM_POD_RESTART, re.compile(r"重启|restart|crashloop|oomkill|oom", re.IGNORECASE)),
    (SYMPTOM_UNAVAILABLE, re.compile(r"不可用|无法访问|宕机|挂掉|掉线|unavailable|down|outage", re.IGNORECASE)),
    (SYMPTOM_LATENCY, re.compile(r"延迟|变慢|很慢|慢|latency|slow|耗时高|响应时间", re.IGNORECASE)),
    (SYMPTOM_ERROR, re.compile(r"报错|错误|异常|失败|error|failure|exception|failing", re.IGNORECASE)),
)


def default_window_minutes() -> int:
    """Investigation lookback, configurable without touching code."""
    raw = os.environ.get(DEFAULT_LOOKBACK_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            return DEFAULT_WINDOW_MINUTES
        if value > 0:
            return min(value, MAX_WINDOW_MINUTES)
    return DEFAULT_WINDOW_MINUTES


class IncidentFacts(BaseModel):
    """Facts extracted from free text. ``unknown`` means "user did not say"."""

    service: str = UNKNOWN
    pod: str = UNKNOWN
    cluster: str = UNKNOWN
    namespace: str = UNKNOWN
    symptom: str = UNKNOWN
    status_code: Optional[int] = None
    window_minutes: Optional[int] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    source_text: str = ""
    #: Fields the user did not state. Never filled by guessing.
    missing: List[str] = Field(default_factory=list)
    #: Required fields the user did not state -> Triage must ask, not assume.
    missing_required: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)

    @property
    def has_service(self) -> bool:
        return self.service != UNKNOWN

    @property
    def has_pod(self) -> bool:
        return self.pod != UNKNOWN

    @property
    def is_actionable(self) -> bool:
        """True when at least one concrete target can be searched for."""
        return self.has_service or self.has_pod

    def effective_window_minutes(self) -> int:
        if self.window_minutes is None:
            return default_window_minutes()
        return max(1, min(int(self.window_minutes), MAX_WINDOW_MINUTES))

    def window(self, now: Optional[datetime] = None) -> tuple[str, str]:
        """Return an RFC3339 ``(start, end)`` window ending now."""
        end = now or datetime.now().astimezone()
        start = end - timedelta(minutes=self.effective_window_minutes())
        return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")

    def as_summary(self) -> dict[str, Any]:
        """Compact, log-safe summary (text itself is never logged as a secret)."""
        return {
            "service": self.service,
            "pod": self.pod,
            "cluster": self.cluster,
            "namespace": self.namespace,
            "symptom": self.symptom,
            "status_code": self.status_code,
            "window_minutes": self.effective_window_minutes() if self.window_minutes else None,
            "missing": list(self.missing),
            "missing_required": list(self.missing_required),
            "notes": list(self.notes),
        }


def _first_match(patterns, text: str, *, reject_stopwords: bool = False) -> Optional[str]:
    for pattern in patterns:
        for match in pattern.finditer(text):
            name = match.group("name").strip().strip("\"'")
            if not name:
                continue
            if reject_stopwords and name.lower() in _SERVICE_STOPWORDS:
                continue
            return name
    return None


def _parse_window(text: str) -> tuple[Optional[int], Optional[str]]:
    for pattern in _WINDOW_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        try:
            amount = int(match.group("n"))
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        unit = match.group("unit").lower().lstrip("个")
        minutes = amount * 60 if unit in _HOUR_UNITS_SET else amount
        return minutes, match.group(0).strip()
    return None, None


def _parse_status_code(text: str) -> Optional[int]:
    codes: list[int] = []
    for match in _STATUS_CODE.finditer(text):
        code = int(match.group("code"))
        if 400 <= code <= 599:
            codes.append(code)
    if not codes:
        return None
    # Prefer the first 5xx (the user's actual complaint), else the first 4xx.
    for code in codes:
        if code >= 500:
            return code
    return codes[0]


def _parse_symptom(text: str, status_code: Optional[int]) -> str:
    if status_code is not None and status_code >= 500:
        return SYMPTOM_HTTP_5XX
    for symptom, pattern in _SYMPTOM_KEYWORDS:
        if pattern.search(text):
            return symptom
    if status_code is not None and status_code >= 400:
        return SYMPTOM_HTTP_4XX
    return UNKNOWN


def parse_incident_text(text: Optional[str]) -> IncidentFacts:
    """Deterministically extract the facts the user actually stated.

    Never invents a cluster, namespace, service or symptom: values that are not
    present in ``text`` remain :data:`UNKNOWN` and are listed in ``missing``.
    """
    raw = (text or "").strip()
    facts = IncidentFacts(source_text=raw)
    if not raw:
        facts.missing = ["service"]
        facts.missing_required = ["service"]
        facts.notes.append("empty incident_text: nothing can be extracted")
        return facts

    service = _first_match(_SERVICE_PATTERNS, raw, reject_stopwords=True)
    pod = _first_match(_POD_PATTERNS, raw, reject_stopwords=True)
    cluster = _first_match(_CLUSTER_PATTERNS, raw, reject_stopwords=True)
    namespace = _first_match(_NAMESPACE_PATTERNS, raw, reject_stopwords=True)

    window_minutes, window_text = _parse_window(raw)
    status_code = _parse_status_code(raw)
    symptom = _parse_symptom(raw, status_code)

    # A Pod name captured by the service pattern ("payment-pod") should not
    # masquerade as a Service when the user never said "service".
    if service and service == pod:
        service = None

    if service:
        facts.service = service
    if pod:
        facts.pod = pod
    if cluster:
        facts.cluster = cluster
    if namespace:
        facts.namespace = namespace
    if symptom != UNKNOWN:
        facts.symptom = symptom
    if status_code is not None:
        facts.status_code = status_code
    if window_minutes is not None:
        facts.window_minutes = max(1, min(window_minutes, MAX_WINDOW_MINUTES))
        facts.notes.append(f"time window taken from text: {window_text}")

    for field, value in (
        ("service", facts.service),
        ("cluster", facts.cluster),
        ("namespace", facts.namespace),
        ("symptom", facts.symptom),
        ("window_minutes", facts.window_minutes),
    ):
        if value in (None, UNKNOWN):
            facts.missing.append(field)

    if not facts.is_actionable:
        facts.missing_required.append("service")
        facts.notes.append(
            "no service or pod name found in incident_text; ask the user instead of guessing"
        )

    return facts


# --------------------------------------------------------------------------
# Optional LLM pass -- proposals only, always re-validated against the text.
# --------------------------------------------------------------------------


class LlmIncidentProposal(BaseModel):
    """What the model is allowed to propose. ``None`` means "not stated"."""

    service: Optional[str] = None
    pod: Optional[str] = None
    cluster: Optional[str] = None
    namespace: Optional[str] = None
    symptom: Optional[str] = None
    status_code: Optional[int] = None
    window_minutes: Optional[int] = None


_LLM_SYSTEM_PROMPT = (
    "You extract incident facts from an operator's message. "
    "Return ONLY information the message states explicitly. "
    "If a field is not stated, return null for it. "
    "Never infer, complete or normalise a value the operator did not write: "
    "guessing a cluster, namespace or service is a critical error. "
    "symptom must be one of: " + ", ".join(KNOWN_SYMPTOMS) + ", or null."
)


def _grounded(value: Any, text: str) -> bool:
    """True only when ``value`` literally appears in the operator's message."""
    if value is None:
        return False
    needle = str(value).strip().strip("\"'").lower()
    if not needle:
        return False
    return needle in text.lower()


def merge_grounded_proposals(
    facts: IncidentFacts, proposal: LlmIncidentProposal, text: str
) -> IncidentFacts:
    """Accept only grounded proposals, and only for fields still unknown.

    Deterministic values are never overwritten, so the LLM cannot rewrite a
    fact the operator stated. Rejected proposals are recorded in ``notes``.
    """
    merged = facts.model_copy(deep=True)
    candidates = (
        ("service", proposal.service, merged.service),
        ("pod", proposal.pod, merged.pod),
        ("cluster", proposal.cluster, merged.cluster),
        ("namespace", proposal.namespace, merged.namespace),
        ("symptom", proposal.symptom, merged.symptom),
        ("status_code", proposal.status_code, merged.status_code),
        ("window_minutes", proposal.window_minutes, merged.window_minutes),
    )
    for field, proposed, current in candidates:
        if proposed is None:
            continue
        if current not in (None, UNKNOWN):
            merged.notes.append(f"llm_proposal_ignored:{field} (already known from text)")
            continue
        if field == "symptom" and str(proposed) not in KNOWN_SYMPTOMS:
            merged.notes.append(f"llm_proposal_rejected:{field} (not a known symptom)")
            continue
        if field == "window_minutes":
            try:
                minutes = int(proposed)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                merged.notes.append(f"llm_proposal_rejected:{field} (not an integer)")
                continue
            if minutes <= 0:
                merged.notes.append(f"llm_proposal_rejected:{field} (not positive)")
                continue
            merged.window_minutes = min(minutes, MAX_WINDOW_MINUTES)
            merged.notes.append(f"llm_proposal_accepted:{field}")
            continue
        if not _grounded(proposed, text):
            merged.notes.append(
                f"llm_proposal_rejected:{field} (value not present in incident_text)"
            )
            continue
        setattr(merged, field, proposed)
        merged.notes.append(f"llm_proposal_accepted:{field}")

    merged.missing = [
        field
        for field, value in (
            ("service", merged.service),
            ("cluster", merged.cluster),
            ("namespace", merged.namespace),
            ("symptom", merged.symptom),
            ("window_minutes", merged.window_minutes),
        )
        if value in (None, UNKNOWN)
    ]
    merged.missing_required = [] if merged.is_actionable else ["service"]
    return merged


async def parse_incident_text_with_llm(
    text: Optional[str], *, llm: Any = None, use_llm: bool = True
) -> IncidentFacts:
    """Deterministic parse, optionally refined by grounded LLM proposals.

    Any failure of the LLM path degrades to the deterministic result -- parsing
    must never make an investigation unavailable.
    """
    facts = parse_incident_text(text)
    if not use_llm or not (text or "").strip():
        return facts
    try:
        if llm is None:
            from config import GPT5_MINI  # imported lazily: no key needed at import time

            llm = GPT5_MINI
        chain = llm.with_structured_output(LlmIncidentProposal)
        proposal = await chain.ainvoke(
            [
                ("system", _LLM_SYSTEM_PROMPT),
                ("human", text or ""),
            ]
        )
        if isinstance(proposal, dict):
            proposal = LlmIncidentProposal(**proposal)
        return merge_grounded_proposals(facts, proposal, text or "")
    except Exception as exc:  # pragma: no cover - depends on external model
        facts.notes.append(f"llm_parse_unavailable:{type(exc).__name__}")
        return facts


# --------------------------------------------------------------------------
# Structured input always wins.
# --------------------------------------------------------------------------


class IncidentScope(BaseModel):
    """The resolved investigation scope handed to Triage."""

    #: ``structured`` | ``incident_text`` | ``insufficient``
    source: str = "insufficient"
    target_kind: str = UNKNOWN  # ``pod`` | ``service`` | ``unknown``
    cluster: str = UNKNOWN
    namespace: str = UNKNOWN
    pod: str = UNKNOWN
    service: str = UNKNOWN
    symptom: str = UNKNOWN
    status_code: Optional[int] = None
    #: True when the operator's text supplied the target (target discovery runs).
    needs_discovery: bool = False
    facts: IncidentFacts = Field(default_factory=IncidentFacts)
    #: Structured fields that took precedence over parsed text.
    structured_overrides: List[str] = Field(default_factory=list)
    reasons: List[str] = Field(default_factory=list)

    @property
    def is_insufficient(self) -> bool:
        return self.source == "insufficient"

    def as_summary(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "target_kind": self.target_kind,
            "cluster": self.cluster,
            "namespace": self.namespace,
            "service": self.service,
            "pod": self.pod,
            "symptom": self.symptom,
            "status_code": self.status_code,
            "needs_discovery": self.needs_discovery,
            "structured_overrides": list(self.structured_overrides),
            "reasons": list(self.reasons),
            "facts": self.facts.as_summary(),
        }


def _clean(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == UNKNOWN else text


def resolve_incident_scope(
    state: Mapping[str, Any], *, facts: Optional[IncidentFacts] = None
) -> IncidentScope:
    """Combine structured graph input with parsed ``incident_text``.

    Structured fields (``target_cluster`` / ``target_namespace`` /
    ``target_resource``) are authoritative: when they carry a value the parsed
    text can never overwrite them, and the legacy structured path is preserved
    unchanged. Parsed text only fills fields the caller left empty.
    """
    facts = facts if facts is not None else parse_incident_text(
        state.get("incident_text") if isinstance(state, Mapping) else None
    )
    scope = IncidentScope(facts=facts)

    structured_cluster = _clean(state.get("target_cluster")) or _clean(
        os.environ.get("RCAGENT_TARGET_CLUSTER", "")
    )
    structured_namespace = _clean(state.get("target_namespace"))
    structured_pod = _clean(state.get("target_resource")) or _clean(state.get("target_pod"))

    cluster = structured_cluster or _clean(facts.cluster)
    namespace = structured_namespace or _clean(facts.namespace)

    if structured_cluster:
        scope.structured_overrides.append("target_cluster")
    if structured_namespace:
        scope.structured_overrides.append("target_namespace")
    if structured_pod:
        scope.structured_overrides.append("target_resource")

    scope.cluster = cluster or UNKNOWN
    scope.namespace = namespace or UNKNOWN
    scope.symptom = _clean(facts.symptom) or UNKNOWN
    scope.status_code = facts.status_code

    if structured_pod:
        scope.pod = structured_pod
        scope.service = _clean(facts.service) or UNKNOWN
        scope.target_kind = "pod"
        scope.source = "structured"
        scope.needs_discovery = False
        return scope

    if facts.pod != UNKNOWN:
        scope.pod = facts.pod
        scope.target_kind = "pod"
        scope.source = "incident_text"
        scope.needs_discovery = True
        return scope

    if facts.has_service:
        scope.service = facts.service
        scope.target_kind = "service"
        scope.source = "incident_text"
        scope.needs_discovery = True
        return scope

    # Nothing usable: say so instead of guessing.
    scope.source = "insufficient"
    scope.target_kind = UNKNOWN
    scope.reasons.extend(facts.missing_required or ["service"])
    if not (state.get("incident_text") or "").strip():
        scope.reasons.append("no incident_text and no structured target_resource provided")
    return scope


__all__ = [
    "UNKNOWN",
    "KNOWN_SYMPTOMS",
    "SYMPTOM_HTTP_5XX",
    "SYMPTOM_HTTP_4XX",
    "SYMPTOM_TIMEOUT",
    "SYMPTOM_LATENCY",
    "SYMPTOM_UNAVAILABLE",
    "SYMPTOM_CONNECTION_ERROR",
    "SYMPTOM_POD_RESTART",
    "SYMPTOM_ERROR",
    "DEFAULT_WINDOW_MINUTES",
    "MAX_WINDOW_MINUTES",
    "IncidentFacts",
    "IncidentScope",
    "LlmIncidentProposal",
    "default_window_minutes",
    "parse_incident_text",
    "parse_incident_text_with_llm",
    "merge_grounded_proposals",
    "resolve_incident_scope",
]
