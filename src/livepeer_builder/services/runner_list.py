"""The clearinghouse's runner list, served to the engines of its tenants (D87, #166).

An enterprise's engine reads the clearinghouse's list by default. The clearinghouse serves the
livepeer/node ``/discovery`` document its own discovery source last read, as it was served: nothing is
read into another shape and written again, so the enterprise's reader reads it as it reads node's own.
Nothing is enforced at signing in this version: the list is what the clearinghouse publishes, not a rule.

This module is in the clearinghouse half and knows nothing of the work half's discovery service. It
reaches the document through a port (``DiscoveryDocumentSource``) on the discovery source both use.
"""

from livepeer_builder.contracts import ActorContext, Operation
from livepeer_builder.protocols import AccessPolicy, DiscoveryDocumentSource


class RunnerList:
    def __init__(self, source: DiscoveryDocumentSource, policy: AccessPolicy) -> None:
        self._source = source
        self._policy = policy

    def document(self, reader: ActorContext) -> bytes | None:
        """The runner list as a livepeer/node discovery document, for any credential of a tenant: its
        admin's or its auditor's key, and a clearinghouse admin's. ``None`` when the clearinghouse has
        read no list yet. Once it has, the last one read is served, however long ago: a source that is
        down changes nothing here, as it changes nothing in the engine's own snapshot."""
        self._policy.check(reader, Operation.ADMIN_READ)
        return self._source.document()
