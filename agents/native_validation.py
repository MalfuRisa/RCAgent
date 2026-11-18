"""Actionable local hints without changing arguments or remote MCP contracts."""

from jsonschema.exceptions import ValidationError
from jsonschema.validators import validator_for

from platforms.insights_v5.native_task_scope import LocalRejection


def helper_contract(schema):
    props = schema.get("properties", {})
    return set(props.get("endpoint", {}).get("enum", [])) == {
        "list",
        "metric",
        "dimensions",
    } and set(props.get("section", {}).get("enum", [])) == {
        "usage",
        "overview",
        "fields",
    }


def helper_hint(args, schema):
    if not helper_contract(schema):
        return {"contract": "版本合同未核对；仅依据实际发现的 JSON Schema"}
    skeleton = {
        "endpoint": "list",
        "section": "fields",
        "database": "<实际库>",
        "table": "<实际表>",
        "limit": 12,
    }
    if args.get("endpoint") in {"fields", "overview", "usage"}:
        return {
            "path": "/endpoint",
            "allowed": ["list", "metric", "dimensions"],
            "message": "usage/fields/overview 是 /section 的值，不是 endpoint",
            "skeleton": {"endpoint": "list", "section": "usage"} if args.get("endpoint") == "usage" else skeleton,
        }
    section = args.get("section", "usage")
    if section != "fields" and any(
        k in args for k in ("field_kind", "field_names", "keyword", "offset", "limit")
    ):
        return {
            "path": "/section",
            "message": "字段选择参数要求 section=fields，且 database/table 必填",
            "skeleton": skeleton,
        }
    if section in {"fields", "overview"} and not all(
        args.get(k) for k in ("database", "table")
    ):
        return {
            "paths": ["/" + k for k in ("database", "table") if not args.get(k)],
            "message": "此 section 要求同时指定数据集",
            "skeleton": skeleton,
        }
    if bool(args.get("database")) != bool(args.get("table")):
        return {
            "path": "/database or /table",
            "message": "database/table 必须同时提供或同时省略",
            "skeleton": skeleton,
        }
    return None


def validate(name, arguments, schema):
    hint = helper_hint(arguments, schema) if name == "insights_describe_query" else None
    try:
        validator_for(schema)(schema).validate(arguments)
    except ValidationError as exc:
        if name == "insights_meta_tag_values" and "offset" in arguments.get("body", {}):
            hint = {"path": "/body/offset", "message": "meta_tag_values没有offset。使用实际Schema中的db/table/tag_name/limit及有依据的where；有限候选不证明不存在。", "skeleton": {"body": {"db": "<实际库>", "table": "<实际表>", "tag_name": "<实际tag>", "limit": 20}}}
        raise LocalRejection(
            "ValidationError",
            exc.message,
            instance_path="/" + "/".join(map(str, exc.absolute_path)),
            schema_path="/" + "/".join(map(str, exc.absolute_schema_path)),
            expected=exc.validator_value,
            rcagent_hint=hint,
        ) from exc
    if hint and "contract" not in hint:
        raise LocalRejection(
            "invalid_argument",
            "Locally confirmed helper contract violation",
            rcagent_hint=hint,
        )
    return hint
