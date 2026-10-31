"""SecretStore implementations. Enterprises may plug in Vault or a cloud secret manager instead.

- SettingsSecretStore: read-only secrets from configuration (per_application's shared key).
- EncryptedDbSecretStore: Fernet-encrypted rows in the engine database (default for per_actor keys).
"""

import secrets as _random
from collections.abc import Mapping

from cryptography.fernet import Fernet, InvalidToken

from livepeer_builder.contracts import SecretRef
from livepeer_builder.errors import InvalidRequest, NotFound
from livepeer_builder.protocols import Clock, SecretRepo

SETTINGS_PREFIX = "settings:"
DB_PREFIX = "db:"


class SettingsSecretStore:
    """Secrets supplied by the operator at startup. Refs look like ``settings:<name>``."""

    def __init__(self, values: Mapping[str, str]) -> None:
        self._values = dict(values)

    def ref(self, name: str) -> SecretRef:
        if name not in self._values:
            raise NotFound(f"no configured secret {name!r}")
        return SecretRef(ref=SETTINGS_PREFIX + name)

    async def put(self, name: str, secret: str) -> SecretRef:
        del name, secret
        raise InvalidRequest("configured secrets are read-only")

    async def get(self, ref: SecretRef) -> str:
        name = ref.ref.removeprefix(SETTINGS_PREFIX)
        if not ref.ref.startswith(SETTINGS_PREFIX) or name not in self._values:
            raise NotFound("unknown secret reference")
        return self._values[name]

    async def delete(self, ref: SecretRef) -> None:
        del ref  # nothing to delete: configuration owns these values


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
    """Routes refs by prefix, so configured and database secrets coexist behind one SecretStore."""

    def __init__(self, settings: SettingsSecretStore, database: EncryptedDbSecretStore | None) -> None:
        self._settings = settings
        self._database = database

    async def put(self, name: str, secret: str) -> SecretRef:
        if self._database is None:
            raise InvalidRequest("storing secrets needs LPB_SECRET_KEY (encrypted database store)")
        return await self._database.put(name, secret)

    async def get(self, ref: SecretRef) -> str:
        if ref.ref.startswith(SETTINGS_PREFIX):
            return await self._settings.get(ref)
        if self._database is None:
            raise InvalidRequest("reading stored secrets needs LPB_SECRET_KEY")
        return await self._database.get(ref)

    async def delete(self, ref: SecretRef) -> None:
        if ref.ref.startswith(DB_PREFIX) and self._database is not None:
            await self._database.delete(ref)
