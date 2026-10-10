from __future__ import annotations

import asyncio

import pytest
from pydantic import SecretStr

from threads_platform.api.contracts import ThreadsCredentialError
from threads_platform.credentials.environment import (
    EnvironmentThreadsCredentialSecretResolver,
    normalize_threads_credential_ref,
    validate_threads_credential_ref,
)

_ENV_NAME = "THREADS_PLATFORM_THREADS_TOKEN_SPLIT02_SYNTHETIC"
_REFERENCE = f"env://{_ENV_NAME}"
_SYNTHETIC_VALUE = "SYNTHETIC_ONLY_CREDENTIAL_VALUE"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (_ENV_NAME, _REFERENCE),
        (_REFERENCE, _REFERENCE),
    ],
)
def test_normalize_credential_reference(value: str, expected: str) -> None:
    assert normalize_threads_credential_ref(value) == expected
    assert validate_threads_credential_ref(expected) == _ENV_NAME


@pytest.mark.parametrize(
    "value",
    [
        "THREADS_TOKEN_SYNTHETIC",
        "env://THREADS_TOKEN_SYNTHETIC",
        "env://THREADS_PLATFORM_THREADS_TOKEN_",
        "env://THREADS_PLATFORM_THREADS_TOKEN_BAD-NAME",
        "env://THREADS_PLATFORM_THREADS_TOKEN_SYNTHETIC\n",
        "env://OTHER_SERVICE_TOKEN_SYNTHETIC",
    ],
)
def test_rejects_unapproved_or_malformed_reference_without_echo(value: str) -> None:
    with pytest.raises(ThreadsCredentialError) as error:
        normalize_threads_credential_ref(value)

    assert error.value.code == "THREADS_CREDENTIAL_INVALID"
    assert value not in str(error.value)


def test_resolver_reads_only_the_referenced_process_value() -> None:
    resolver = EnvironmentThreadsCredentialSecretResolver({_ENV_NAME: _SYNTHETIC_VALUE})

    secret = asyncio.run(resolver.resolve(_REFERENCE))

    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == _SYNTHETIC_VALUE
    assert _SYNTHETIC_VALUE not in repr(secret)


@pytest.mark.parametrize("value", [None, "", " leading", "trailing ", "line\nbreak"])
def test_missing_or_malformed_secret_is_sanitized(value: str | None) -> None:
    environment = {} if value is None else {_ENV_NAME: value}
    resolver = EnvironmentThreadsCredentialSecretResolver(environment)

    with pytest.raises(ThreadsCredentialError) as error:
        asyncio.run(resolver.resolve(_REFERENCE))

    assert error.value.code == "THREADS_CREDENTIAL_SECRET_UNAVAILABLE"
    if value:
        assert value not in str(error.value)


def test_resolver_does_not_enumerate_environment() -> None:
    class ReferencedOnly(dict[str, str]):
        def __iter__(self):
            raise AssertionError("credential resolver must not enumerate process environment")

        def items(self):
            raise AssertionError("credential resolver must not enumerate process environment")

        def keys(self):
            raise AssertionError("credential resolver must not enumerate process environment")

        def values(self):
            raise AssertionError("credential resolver must not enumerate process environment")

    resolver = EnvironmentThreadsCredentialSecretResolver(
        ReferencedOnly({_ENV_NAME: _SYNTHETIC_VALUE})
    )

    assert asyncio.run(resolver.resolve(_REFERENCE)).get_secret_value() == _SYNTHETIC_VALUE
