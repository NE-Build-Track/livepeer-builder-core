"""``check_payments``: the read-only payments check, for the CLI and for an embedding app (D87, #168).

``lpb payments check`` prints what this returns. A self-hosted engine probes the signer and Batteries;
an engine that uses a payment clearinghouse probes the clearinghouse and its signer. Every probe is a
GET, nothing is written anywhere, and no engine is built: the check needs the settings alone, so it can
run beside a serving engine, or before one is ever started (``adapters/payment_probes.py``).
"""

from livepeer_builder.config import EngineSettings
from livepeer_builder.contracts import PaymentCheck


async def check_payments(settings: EngineSettings) -> PaymentCheck:
    """Probe what the payments of an engine with these settings need. It sends GET requests only."""
    from livepeer_builder.adapters.payment_probes import probe_payments  # here: importing loads no client

    return await probe_payments(settings)
