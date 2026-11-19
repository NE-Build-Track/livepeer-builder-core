"""``FakeSigningTransport``: a scripted runner transport that also stands in for the signer behind a
clearinghouse (#166)."""

from dataclasses import dataclass, field

from livepeer_builder.contracts import SignerAnswer, SignerCall
from livepeer_builder.testing.fakes import ScriptedStreamTransport

_JSON = "application/json"
SIGNER_IDENTITY = b'{"address":"0x' + b"ab" * 20 + b'","signature":"0xsig"}'


@dataclass(frozen=True)
class ForwardedSignerCall:
    """One call as the signer received it: which of its two paths, the credential it was handed (an
    actor's Batteries key), and the body and content type as they arrived."""

    call: SignerCall
    credential: str
    body: bytes
    content_type: str


@dataclass
class FakeSigningTransport(ScriptedStreamTransport):
    """``ScriptedStreamTransport`` that also forwards signer calls (``SignerForwarder``), so an engine
    built with it serves the signer's two paths as a clearinghouse does.

    ``forwarded`` lists every call the signer received, in order. ``signer_answers`` are consumed one a
    call: a ``SignerAnswer`` is returned, and an exception (``SignerNotReached``,
    ``SignerOutcomeUnknown``) is raised after the call is recorded. With none left the signer answers as
    a healthy one does: its identity, or a payment with the ``state`` the SDK sends back next cycle."""

    signer_answers: list[SignerAnswer | Exception] = field(default_factory=list)
    forwarded: list[ForwardedSignerCall] = field(default_factory=list)

    async def forward_signer_call(  # NOSONAR - async by protocol (SignerForwarder)
        self, call: SignerCall, credential: str, body: bytes, content_type: str
    ) -> SignerAnswer:
        self.forwarded.append(ForwardedSignerCall(call, credential, body, content_type))
        if self.signer_answers:
            answer = self.signer_answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        if call == "sign-orchestrator-info":
            return SignerAnswer(status_code=200, content=SIGNER_IDENTITY, content_type=_JSON)
        signed = b'{"payment":"pay-%d","segCreds":"seg","state":{"n":%d}}' % ((len(self.forwarded),) * 2)
        return SignerAnswer(status_code=200, content=signed, content_type=_JSON)
