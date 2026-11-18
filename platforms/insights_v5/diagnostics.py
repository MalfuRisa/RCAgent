"""Assemble protocol capabilities for the single investigation workflow."""

from tools.diagnostic_catalog import CapabilityCatalog
from .diagnostic_policy import DiagnosticAccessPolicy, NATIVE_READS
from .diagnostic_contracts import object_schema, identifier, metadata_names
from .diagnostic_identity import IdentityAccess, resolved
from .diagnostic_metadata import make_metadata_capabilities
from .diagnostic_cpu_events import make_cpu_capability
from .diagnostic_datasets import make_dataset_capabilities


def make_catalog(client, *, service_identity, initial_resources=(), identity_source=None):
    metadata = {}
    identity = IdentityAccess(client, initial_resources, identity_source)
    capabilities = make_metadata_capabilities(client, metadata)
    capabilities.append(make_cpu_capability(client, identity.fresh_identity))
    capabilities.append(identity.capability())
    capabilities.extend(make_dataset_capabilities(client, metadata, identity.fresh_identity))
    catalog = CapabilityCatalog(
        capabilities, service_identity=service_identity, bootstrap=identity.bootstrap
    )
    identity.catalog = catalog
    return catalog
