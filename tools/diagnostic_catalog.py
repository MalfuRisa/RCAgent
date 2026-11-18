"""Capability contracts shared by all roles; no question/keyword classification."""

from dataclasses import dataclass, field
import hashlib
import json
from workspace.repository import WorkspaceError


class DiagnosticResultError(WorkspaceError):
    """Keep the exact returned payload when semantic validation quarantines it."""

    def __init__(self, raw_result, message):
        super().__init__(message)
        self.raw_result = raw_result


@dataclass
class QueryResult:
    raw: dict
    resource_ids: list[str] = field(default_factory=list)
    returned_window: dict | None = None
    source_status: str = "ok"
    truncated: bool = False
    warnings: list = field(default_factory=list)
    data_gaps: list = field(default_factory=list)
    freshness: str = "queried_live"


@dataclass
class Capability:
    name: str
    description: str
    schema: dict
    normalize: object
    invoke: object
    contract_version: str
    windowed: bool = False
    availability: str = "contract available; diagnostic quality not established"

    @property
    def fingerprint(self):
        return hashlib.sha256(
            json.dumps(self.schema, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


class CapabilityCatalog:
    def __init__(self, capabilities, *, service_identity, bootstrap=None):
        self.capabilities = {c.name: c for c in capabilities}
        self.service_identity, self.bootstrap = service_identity, bootstrap

    def directory(self):
        return [
            {
                "name": c.name,
                "description": c.description,
                "availability": c.availability,
            }
            for c in self.capabilities.values()
        ]

    def get(self, name):
        if name not in self.capabilities:
            raise WorkspaceError("capability_not_authorized: " + name)
        return self.capabilities[name]

    def inspect(self, name):
        c = self.get(name)
        return {
            "name": c.name,
            "description": c.description,
            "schema": c.schema,
            "availability": c.availability,
            "contract_version": c.contract_version,
        }
