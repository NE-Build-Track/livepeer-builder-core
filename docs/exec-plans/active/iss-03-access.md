# ISS-03: Define replaceable authentication and access-policy interfaces

## Status

Owners: John, Mike. GitHub: #9. **The design was accepted on 12 October; the contract doc and the
protocols are in this scaffold.**

## Goal

Trusted adapters map standalone credentials or enterprise authentication into a validated actor,
ownership scope and permitted operations, across imported and service modes, without core patches.

## Acceptance

- [x] Protocols `Authenticator` and `AccessPolicy`; chain semantics: [contracts/access.md](../../contracts/access.md)
- [x] Standalone adapter (API keys) and `DefaultPolicy`: `tests/unit/test_access.py`
- [x] Browser and admin trust boundaries; credential lifecycle table
- [x] Enterprise service mode: JWT bearer and trusted backend defined
- [x] Request fields cannot establish identity (`test_request_fields_cannot_establish_identity`)
- [ ] Mike's review

## Progress

- [x] 2026-10-12: Contract written; API-key adapter, policy, one-time admin bootstrap and
      `SecretStore` implemented.
- [ ] M4: `JwtBearerAuthenticator`, `ServiceCredentialAuthenticator`, RFC 9728 metadata.

## Decision log

- 2026-10-12: The accepted design (D12–D16): define all modes, implement API key now and JWT in M4.
