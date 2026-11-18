"""v5 argument/schema helpers; no role or graph policy."""

import re
from workspace.repository import WorkspaceError


def object_schema(properties, required=()):
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", value):
        raise WorkspaceError("expected a dataset/field identifier, not an expression")
    return value


def metadata_names(raw):
    names = set()

    def visit(node):
        if isinstance(node, dict):
            for key in ("name", "tag_name", "server_name", "client_name", "metric_name", "value"):
                value = node.get(key)
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                    names.add(value)
            # Native resource tags expose the display name (pod/node), while
            # the corresponding numeric filter is <tag>_id.
            name = node.get("tag_name")
            if node.get("type") == "resource" and isinstance(name, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                names.add(name + "_id")
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(raw)
    return names
