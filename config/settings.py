"""Configuration and environment settings for RCAgent."""

import os
from pathlib import Path
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from typing import Any, Mapping
from .chat_model import RCAChatOpenAI, validate_output_method

# RCAgent uses a flat repository layout: config/ lives directly below the root.
ROOT_DIR = Path(__file__).resolve().parent.parent
root_dir = str(ROOT_DIR)
INVESTIGATION_HISTORY_ROOT = Path(
    os.environ.get(
        "RCAGENT_INVESTIGATION_HISTORY_ROOT",
        str(ROOT_DIR.parent / "RCAgent/学习材料归档/04-Agent学习/改动优化"),
    )
)
INVESTIGATION_HISTORY_MANIFEST = Path(
    os.environ.get(
        "RCAGENT_INVESTIGATION_HISTORY_MANIFEST",
        str(ROOT_DIR / "local_testing/fixtures/investigation_history.json"),
    )
)

# Load environment variables from .env file in the root directory
load_dotenv(ROOT_DIR / ".env", verbose=True)

# Owned investigation transport/capacity settings, independent of legacy budgets.
INVESTIGATION_IDLE_TIMEOUT = float(os.getenv("RCAGENT_INVESTIGATION_IDLE_TIMEOUT", "300"))
NATIVE_MCP_TIMEOUT = float(os.getenv("RCAGENT_NATIVE_MCP_TIMEOUT", "300"))
NATIVE_CONCURRENCY = int(os.getenv("RCAGENT_NATIVE_CONCURRENCY", "8"))
NATIVE_WORKER_CONCURRENCY = int(os.getenv("RCAGENT_NATIVE_WORKER_CONCURRENCY", "5"))
NATIVE_MODEL_CONCURRENCY = int(os.getenv("RCAGENT_NATIVE_MODEL_CONCURRENCY", "1"))
NATIVE_TRACE_REQUESTS = os.getenv("RCAGENT_NATIVE_TRACE_REQUESTS", "false").lower() == "true"
MODEL_CONTEXT_TOKENS = int(os.getenv("RCAGENT_MODEL_CONTEXT_TOKENS", "0")) or None
MODEL_OUTPUT_RESERVE_TOKENS = int(os.getenv("RCAGENT_MODEL_OUTPUT_RESERVE_TOKENS", "0"))

# Keep the upstream MCP compatibility path available for the unchanged
# Triage/Planner implementation. The RCA worker uses the Kylin MCP Gateway.
import sys

mcp_server_path = str(ROOT_DIR / "MCP-server")
sys.path.insert(0, mcp_server_path)

# LLM Configuration
#
# Keep the historical variable names so the Agent implementation stays
# unchanged, while allowing any OpenAI-compatible Chat Completions endpoint.
# Secrets remain environment-only and are never stored in source control.
LLM_BASE_URL = os.environ.get("RCAGENT_LLM_BASE_URL", "").strip()
LLM_API_KEY = (
    os.environ.get("RCAGENT_LLM_API_KEY", "").strip()
    or os.environ.get("OPENAI_API_KEY", "").strip()
)
LLM_MODEL = os.environ.get("RCAGENT_LLM_MODEL", "gpt-5-mini").strip()
LLM_ADVANCED_MODEL = os.environ.get("RCAGENT_LLM_ADVANCED_MODEL", "gpt-5.1").strip()
LLM_STRUCTURED_OUTPUT_METHOD = os.environ.get(
    "RCAGENT_LLM_STRUCTURED_OUTPUT_METHOD", "function_calling"
).strip()
LLM_ADVANCED_STRUCTURED_OUTPUT_METHOD = os.environ.get(
    "RCAGENT_LLM_ADVANCED_STRUCTURED_OUTPUT_METHOD", LLM_STRUCTURED_OUTPUT_METHOD
).strip()


def _build_chat_model(
    model: str, *, structured_output_method: str | None = None
) -> ChatOpenAI:
    """Create a Chat Completions client from environment configuration."""
    if not model:
        raise ValueError("RCAgent LLM model name must not be empty")

    method = validate_output_method(
        LLM_STRUCTURED_OUTPUT_METHOD
        if structured_output_method is None
        else structured_output_method
    )
    # The SDK must not cancel a Planner request before the role's sender budget.
    kwargs: dict[str, Any] = {
        "model": model,
        "structured_output_method": method,
        "timeout": max(
            MODEL_TIMEOUT_SECONDS,
            PLANNER_MODEL_TIMEOUT_SECONDS,
            SUPERVISOR_MODEL_TIMEOUT_SECONDS,
        ),
        "max_retries": 0,
    }
    if LLM_API_KEY:
        kwargs["api_key"] = LLM_API_KEY
    if LLM_BASE_URL:
        kwargs["base_url"] = LLM_BASE_URL
    return RCAChatOpenAI(**kwargs)


_CHAT_MODELS: dict[str, ChatOpenAI] = {}


def get_chat_model(kind: str = "mini") -> ChatOpenAI:
    """Build (once) and return a Chat Completions client.

    Client construction requires credentials, so it is deferred rather than done
    at import time. Loading the MCP Gateway tool list must not depend on LLM
    configuration: a structured-Pod or discovery-only run has to be able to talk
    to the Gateway without an API key present.
    """
    if kind not in ("mini", "advanced"):
        raise ValueError("kind must be 'mini' or 'advanced'")
    if kind not in _CHAT_MODELS:
        model = LLM_MODEL if kind == "mini" else LLM_ADVANCED_MODEL
        method = (
            LLM_STRUCTURED_OUTPUT_METHOD
            if kind == "mini"
            else LLM_ADVANCED_STRUCTURED_OUTPUT_METHOD
        )
        _CHAT_MODELS[kind] = _build_chat_model(model, structured_output_method=method)
    return _CHAT_MODELS[kind]


def __getattr__(name: str) -> Any:
    """Resolve ``GPT5_MINI`` / ``GPT5_1`` lazily (PEP 562)."""
    if name == "GPT5_MINI":
        return get_chat_model("mini")
    if name == "GPT5_1":
        return get_chat_model("advanced")
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Investigation Budget
MAX_TOOL_CALLS = int(os.environ.get("MAX_TOOL_CALLS", 8))


def _positive_setting(name: str, default: int) -> int:
    value = int(os.environ.get("RCAGENT_" + name, default))
    if value <= 0:
        raise ValueError("RCAGENT_" + name + " must be positive")
    return value


def _nonnegative_setting(name, default, cast=int):
    value = cast(os.environ.get("RCAGENT_" + name, default))
    if value < 0 or value != value or value == float("inf"):
        raise ValueError("RCAGENT_" + name + " must be finite and nonnegative")
    return value


def _boolean_setting(name, default=True):
    value = os.environ.get("RCAGENT_" + name, str(default).lower())
    if value not in ("true", "false"):
        raise ValueError("RCAGENT_" + name + " must be true or false")
    return value == "true"


WORKSPACE_ROOT = Path(
    os.environ.get("RCAGENT_WORKSPACE_ROOT", ROOT_DIR / "var/incident-workspaces")
)
if not WORKSPACE_ROOT.is_absolute():
    raise ValueError("RCAGENT_WORKSPACE_ROOT must be absolute")
ALLOWED_NAMESPACES = [
    v.strip()
    for v in os.environ.get("RCAGENT_ALLOWED_NAMESPACES", "*").split(",")
    if v.strip()
]
if not ALLOWED_NAMESPACES:
    raise ValueError("RCAGENT_ALLOWED_NAMESPACES must not be empty")
IDENTITY_MAX_ATTEMPTS = _positive_setting("IDENTITY_MAX_ATTEMPTS", 2)
if IDENTITY_MAX_ATTEMPTS > 2:
    raise ValueError("identity lookup permits at most two attempts")
IDENTITY_TTL_SECONDS = _positive_setting("IDENTITY_TTL_SECONDS", 60)
WORKER_MAX_MODEL_STEPS = _positive_setting("WORKER_MAX_MODEL_STEPS", 16)
WORKER_EXPLORATION_STEPS = _nonnegative_setting("WORKER_EXPLORATION_STEPS", 14)
SUPERVISOR_EXPLORATION_STEPS = _nonnegative_setting("SUPERVISOR_EXPLORATION_STEPS", 8)
INVESTIGATION_CONSOLIDATION_STEPS = _positive_setting(
    "INVESTIGATION_CONSOLIDATION_STEPS", 4
)
INVESTIGATION_REPAIR_STEPS = _nonnegative_setting("INVESTIGATION_REPAIR_STEPS", 2)
INVESTIGATION_SKILLS_ENABLED = (
    os.environ.get("RCAGENT_INVESTIGATION_SKILLS_ENABLED", "true").lower() == "true"
)
SKILL_READ_EXTRA_ROUNDS = _nonnegative_setting("SKILL_READ_EXTRA_ROUNDS", 3)
SUPERVISOR_MAX_ROUNDS = _positive_setting("SUPERVISOR_MAX_ROUNDS", 3)
SUPERVISOR_MAX_READS = _positive_setting("SUPERVISOR_MAX_READS", 8)
MAX_NO_PROGRESS_ROUNDS = _positive_setting("MAX_NO_PROGRESS_ROUNDS", 2)
INVESTIGATION_TIMEOUT_SECONDS = _positive_setting("INVESTIGATION_TIMEOUT_SECONDS", 1800)
CLOSEOUT_MIN_INVESTIGATION_MARGIN = _nonnegative_setting(
    "CLOSEOUT_MIN_INVESTIGATION_MARGIN", 10, float
)
MODEL_TIMEOUT_SECONDS = _positive_setting("MODEL_TIMEOUT_SECONDS", 180)
PLANNER_MODEL_TIMEOUT_SECONDS = _positive_setting("PLANNER_MODEL_TIMEOUT_SECONDS", 300)
SUPERVISOR_MODEL_TIMEOUT_SECONDS = _positive_setting(
    "SUPERVISOR_MODEL_TIMEOUT_SECONDS", 300
)
SUPERVISOR_FINALIZATION_MAX_STEPS = _positive_setting(
    "SUPERVISOR_FINALIZATION_MAX_STEPS", 4
)
CONTEXT_MAX_CHARS = _positive_setting("CONTEXT_MAX_CHARS", 96000)
STAGE_REPAIR_MAX_CHARS = _positive_setting("STAGE_REPAIR_MAX_CHARS", 32768)
STAGE_HANDOFF_REQUIRED = _boolean_setting("STAGE_HANDOFF_REQUIRED", False)
CONTEXT_BUDGET_ENABLED = _boolean_setting("CONTEXT_BUDGET_ENABLED")


def _enum_setting(name, default, allowed):
    value = os.environ.get("RCAGENT_" + name, default).strip()
    if value not in allowed:
        raise ValueError("RCAGENT_" + name + " must be one of " + "/".join(allowed))
    return value


def _range_setting(name, default, minimum, maximum):
    value = int(os.environ.get("RCAGENT_" + name, default))
    if value < minimum or value > maximum:
        raise ValueError(
            "RCAGENT_" + name + " must be between %d and %d" % (minimum, maximum)
        )
    return value


# Presentation contract (round 1-3 of the workbench input rework). `compact` is
# the per-role field-projection format; `legacy` keeps the previous assembly for
# replay comparison and rollback. Invalid values fail at import time.
CONTEXT_VIEW_MODE = _enum_setting("CONTEXT_VIEW_MODE", "compact", ("compact", "legacy"))
CONTEXT_BUDGET_POLICY = _enum_setting(
    "CONTEXT_BUDGET_POLICY", "soft", ("soft", "strict")
)
CONTEXT_HARD_MAX_CHARS = _positive_setting("CONTEXT_HARD_MAX_CHARS", 512000)
if CONTEXT_BUDGET_POLICY == "soft" and CONTEXT_HARD_MAX_CHARS < CONTEXT_MAX_CHARS:
    raise ValueError(
        "CONTEXT_HARD_MAX_CHARS must be >= the soft CONTEXT_MAX_CHARS target"
    )
CONTEXT_LIFECYCLE_MODE = _enum_setting(
    "CONTEXT_LIFECYCLE_MODE", "observe", ("legacy", "observe", "active")
)
CONTEXT_HISTORY_RECENT_BATCHES = _positive_setting("CONTEXT_HISTORY_RECENT_BATCHES", 2)
CONTEXT_READ_MAX_CHARS = _range_setting("CONTEXT_READ_MAX_CHARS", 4000, 512, 8000)
SERVICE_FALLBACK_MAX_PODS = _positive_setting("SERVICE_FALLBACK_MAX_PODS", 3)
MODEL_MAX_IN_FLIGHT = _positive_setting("MODEL_MAX_IN_FLIGHT", 1)
MODEL_MIN_INTERVAL_SECONDS = _nonnegative_setting(
    "MODEL_MIN_INTERVAL_SECONDS", 2, float
)
MODEL_MAX_RETRIES = _nonnegative_setting("MODEL_MAX_RETRIES", 2)
#: 429 handling: `stop_run` stops the whole run without retrying (PRD §19.10);
#: `legacy` keeps the old bounded retry and is only used for explicit comparison.
RATE_LIMIT_POLICY = _enum_setting(
    "RATE_LIMIT_POLICY", "stop_run", ("stop_run", "legacy")
)
#: Which measured basis the engineering budget uses (PRD §19.8.1). ``v2`` also
#: counts bound tool schemas and the response schema. Runtime defaults to v2;
#: v1 remains an explicit historical comparison, and both are always recorded.
REQUEST_BUDGET_BASIS = _enum_setting("REQUEST_BUDGET_BASIS", "v2", ("v1", "v2"))
SUPERVISOR_MAX_CORRECTIONS = _nonnegative_setting("SUPERVISOR_MAX_CORRECTIONS", 1)
if MAX_TOOL_CALLS <= 0:
    raise ValueError("MAX_TOOL_CALLS must be positive")

# RCA tasks per iteration
RCA_TASKS_PER_ITERATION = int(os.environ.get("RCA_TASKS_PER_ITERATION", 3))

# Trace service starting point for investigations
TRACE_SERVICE_STARTING_POINT = os.environ.get(
    "TRACE_SERVICE_STARTING_POINT", "frontend"
)

# Daily OpenAI token limit
MAX_DAILY_OPENAI_TOKEN_LIMIT = int(
    os.environ.get("MAX_DAILY_OPENAI_TOKEN_LIMIT", 2_000_000)
)

AIOPSLAB_DIR = os.environ.get("AIOPSLAB_DIR")


def apply_config_overrides(overrides: Mapping[str, Any]) -> None:
    """Update runtime knobs (called before launching each agent run)."""
    global MAX_TOOL_CALLS, RCA_TASKS_PER_ITERATION, TRACE_SERVICE_STARTING_POINT

    if "MAX_TOOL_CALLS" in overrides:
        os.environ["MAX_TOOL_CALLS"] = str(overrides["MAX_TOOL_CALLS"])
    if "RCA_TASKS_PER_ITERATION" in overrides:
        os.environ["RCA_TASKS_PER_ITERATION"] = str(
            overrides["RCA_TASKS_PER_ITERATION"]
        )
    if "TRACE_SERVICE_STARTING_POINT" in overrides:
        os.environ["TRACE_SERVICE_STARTING_POINT"] = str(
            overrides["TRACE_SERVICE_STARTING_POINT"]
        )

    MAX_TOOL_CALLS = int(os.environ.get("MAX_TOOL_CALLS", MAX_TOOL_CALLS))
    RCA_TASKS_PER_ITERATION = int(
        os.environ.get("RCA_TASKS_PER_ITERATION", RCA_TASKS_PER_ITERATION)
    )
    TRACE_SERVICE_STARTING_POINT = os.environ.get(
        "TRACE_SERVICE_STARTING_POINT", TRACE_SERVICE_STARTING_POINT
    )


# MCP configuration. RCAgent defaults to the locally developed Kylin Gateway
# over Streamable HTTP. The legacy stdio mode remains available only for
# reproducing the upstream baseline without changing Agent algorithms.
MCP_MODE = os.environ.get("RCAGENT_MCP_MODE", "kylin_gateway").strip().lower()
_MCP_SERVER_PATH = os.environ.get("RCAGENT_LEGACY_MCP_SERVER", "")
_MCP_SERVER_DIR = str(Path(_MCP_SERVER_PATH).parent) if _MCP_SERVER_PATH else ""


def get_mcp_config() -> dict:
    """Build the selected MCP transport configuration.

    This function builds the MCP config dynamically, ensuring all relevant
    environment variables are passed to the MCP server subprocess.
    """
    if MCP_MODE == "kylin_gateway":
        gateway_url = os.environ.get(
            "MCP_GATEWAY_URL", "http://127.0.0.1:8888/mcp"
        ).strip()
        if not gateway_url:
            raise ValueError("MCP_GATEWAY_URL must not be empty in kylin_gateway mode")

        gateway_config: dict[str, Any] = {
            "url": gateway_url,
            "transport": "streamable_http",
        }
        gateway_token = os.environ.get("MCP_GATEWAY_AUTH_TOKEN", "").strip()
        if gateway_token:
            gateway_config["headers"] = {"Authorization": f"Bearer {gateway_token}"}
        return {"kylin_gateway": gateway_config}

    if MCP_MODE != "legacy_stdio":
        raise ValueError(
            "RCAGENT_MCP_MODE must be either 'kylin_gateway' or 'legacy_stdio'"
        )

    # Legacy upstream compatibility: spawn the two stdio MCP servers.
    if not _MCP_SERVER_PATH or not Path(_MCP_SERVER_PATH).is_file():
        raise ValueError("legacy_stdio requires RCAGENT_LEGACY_MCP_SERVER pointing to an installed MCP server")

    env_vars = {"ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS": "true"}

    # Add all observability-related environment variables
    env_keys_to_pass = [
        "TARGET_NAMESPACE",
        "PROMETHEUS_SERVER_URL",
        "JAEGER_URL",
        "NEO4J_URI",
        "NEO4J_USER",
        "NEO4J_PASSWORD",
        "TRACE_SERVICE_STARTING_POINT",
    ]

    for key in env_keys_to_pass:
        value = os.environ.get(key)
        if value is not None:
            env_vars[key] = value

    return {
        "kubernetes": {
            "command": "npx",
            "args": ["mcp-server-kubernetes"],
            "transport": "stdio",
            "env": {"ALLOW_ONLY_NON_DESTRUCTIVE_TOOLS": "true"},
        },
        "cluster_api": {
            "command": "poetry",
            "args": ["run", "python", _MCP_SERVER_PATH],
            "transport": "stdio",
            "env": env_vars,
        },
    }


# Initial MCP config - will be updated when creating client
MCP_CONFIG = get_mcp_config()

# Tool Configuration
K8S_TOOLS_ALLOWED = [
    "kubectl_get",
    "kubectl_describe",
    "explain_resource",
    "list_api_resources",
    "ping",
]

CUSTOM_TOOLS_ALLOWED = [
    "get_metrics",
    "get_metrics_range",
    "get_pods_from_service",
    "get_cluster_pods_and_services",
    "get_services_used_by",
    "get_dependencies",
    "get_logs",
    "get_traces",
    "get_trace",
]

KYLIN_TOOLS_ALLOWED = [
    "resolve_pod",
    "get_pod_runtime_status",
    "get_pod_network_summary",
    "get_pod_application_summary",
    "get_abnormal_requests",
    "trace_request",
    "compare_pod_with_peers",
    "diagnose_pod",
]

# Target-discovery and observability tools added for natural-language incidents.
# They are additive: the eight Pod-diagnosis tools above keep their names,
# arguments and behaviour, and the structured Pod path is unchanged.
KYLIN_DISCOVERY_TOOLS_ALLOWED = [
    "search_k8s_targets",
    "get_service_backends",
    "query_metrics",
    "search_traces",
    "get_trace",
]

#: Everything RCAgent may call on the Kylin Gateway.
KYLIN_ALL_TOOLS_ALLOWED = KYLIN_TOOLS_ALLOWED + KYLIN_DISCOVERY_TOOLS_ALLOWED

TOOLS_ALLOWED = (
    KYLIN_ALL_TOOLS_ALLOWED
    if MCP_MODE == "kylin_gateway"
    else K8S_TOOLS_ALLOWED + CUSTOM_TOOLS_ALLOWED
)
