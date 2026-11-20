"""SecretStore implementations. Enterprises may plug in Vault or a cloud secret manager instead.

- EncryptedDbSecretStore: Fernet-encrypted rows in the engine database, where payment keys are kept.
- RoutingSecretStore: the engine's default, which is that store when ``LPB_SECRET_KEY`` is set.

No secret is read from settings: the shared payment key was the only one, and it went with the
``per_application`` mode (D82).
"""

import secrets as _random

from cryptography.fernet import Fernet, InvalidToken

from livepeer_builder.contracts import SecretRef
from livepeer_builder.errors import InvalidRequest, NotFound
from livepeer_builder.protocols import Clock, SecretRepo

DB_PREFIX = "db:"


class EncryptedDbSecretStore:
    """Fernet-encrypted secrets in the engine database. Refs look like ``db:<random id>``.

    The key-encryption key comes from settings (``LPB_SECRET_KEY``) and never touches the database,
    so a database backup alone does not disclose payment keys.
    """

    def __init__(self, repo: SecretRepo, key: str, clock: Clock) -> None:
        self._repo = repo
        self._fernet = Fernet(key.encode())
        self._clock = clock

    async def put(self, name: str, secret: str) -> SecretRef:
        ref = f"{DB_PREFIX}{_random.token_urlsafe(16)}"
        del name  # names are for humans and external stores; refs here are opaque
        await self._repo.put_secret(ref, self._fernet.encrypt(secret.encode()).decode(), self._clock.now())
        return SecretRef(ref=ref)

    async def get(self, ref: SecretRef) -> str:
        ciphertext = await self._repo.get_secret(ref.ref)
        if ciphertext is None:
            raise NotFound("unknown secret reference")
        try:
            return self._fernet.decrypt(ciphertext.encode()).decode()
        except InvalidToken as exc:
            raise InvalidRequest("secret could not be decrypted with the configured key") from exc

    async def delete(self, ref: SecretRef) -> None:
        await self._repo.delete_secret(ref.ref)


class RoutingSecretStore:
    """The engine's default SecretStore: the encrypted database store, or none without ``LPB_SECRET_KEY``.
    With none it stores and reads nothing, and says which setting is missing."""

    def __init__(self, database: EncryptedDbSecretStore | None) -> None:
        self._database = database

    # async by protocol (WritableSecretStore): a plug-in store may need I/O to answer. Nothing to await here.
    async def writable(self) -> bool:  # NOSONAR
        """Only the encrypted database stores new secrets; without ``LPB_SECRET_KEY`` there is none."""
        return self._database is not None

    async def put(self, name: str, secret: str) -> SecretRef:
        if self._database is None:
            raise InvalidRequest("storing secrets needs LPB_SECRET_KEY (encrypted database store)")
        return await self._database.put(name, secret)

    async def get(self, ref: SecretRef) -> str:
        if self._database is None:
            raise InvalidRequest("reading stored secrets needs LPB_SECRET_KEY")
        return await self._database.get(ref)

    async def delete(self, ref: SecretRef) -> None:
        if ref.ref.startswith(DB_PREFIX) and self._database is not None:
            await self._database.delete(ref)
