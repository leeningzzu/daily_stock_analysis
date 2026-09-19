# -*- coding: utf-8 -*-
from __future__ import annotations

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, ReadTimeoutError
from botocore.stub import Stubber
import pytest

from src.services.r2_s3_object_store import (
    R2S3LengthMismatch,
    R2S3MalformedResponse,
    R2S3ObjectStore,
)
from src.services.research_state_package_chain import (
    MAX_PACKAGE_BYTES,
    ObjectConflictError,
    PackageTooLarge,
)


ENDPOINT = "https://example.r2.cloudflarestorage.com"
BUCKET = "synthetic-research-state"
ACCESS = "synthetic-access"
SECRET = "synthetic-secret"


def _client_error(status: int, code: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": code, "Message": "synthetic"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        },
        "PutObject",
    )


class FakeBody:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.closed = False
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        return self.payload if size < 0 else self.payload[:size]

    def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(self):
        self.list_responses: list[dict] = []
        self.get_responses: list[dict | Exception] = []
        self.put_results: list[object] = []
        self.list_calls: list[dict] = []
        self.get_calls: list[dict] = []
        self.put_calls: list[dict] = []

    def list_objects_v2(self, **kwargs):
        self.list_calls.append(dict(kwargs))
        return self.list_responses.pop(0)

    def get_object(self, **kwargs):
        self.get_calls.append(dict(kwargs))
        result = self.get_responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    def put_object(self, **kwargs):
        self.put_calls.append(dict(kwargs))
        result = self.put_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _store(client, **kwargs) -> R2S3ObjectStore:
    return R2S3ObjectStore(
        endpoint_url=ENDPOINT,
        bucket_name=BUCKET,
        access_key_id=ACCESS,
        secret_access_key=SECRET,
        client=client,
        **kwargs,
    )


def test_client_factory_receives_exact_config_and_store_does_not_retain_secrets() -> None:
    captured = {}

    def factory(service_name, **kwargs):
        captured["service_name"] = service_name
        captured.update(kwargs)
        return FakeClient()

    store = R2S3ObjectStore(
        endpoint_url=ENDPOINT,
        bucket_name=BUCKET,
        access_key_id=ACCESS,
        secret_access_key=SECRET,
        client_factory=factory,
    )
    assert captured["service_name"] == "s3"
    assert captured["endpoint_url"] == ENDPOINT
    assert captured["aws_access_key_id"] == ACCESS
    assert captured["aws_secret_access_key"] == SECRET
    assert captured["region_name"] == "auto"

    config = captured["config"]
    assert isinstance(config, Config)
    assert config.signature_version == "s3v4"
    assert config.connect_timeout == 10
    assert config.read_timeout == 60
    assert config.retries == {"mode": "standard", "total_max_attempts": 4}
    assert config.s3 == {"addressing_style": "path"}
    assert config.request_checksum_calculation == "when_required"
    assert config.response_checksum_validation == "when_required"
    assert config.tcp_keepalive is True

    state = repr(store.__dict__)
    assert ACCESS not in state
    assert SECRET not in state
    assert not hasattr(store, "delete")
    assert not hasattr(store, "copy")
    assert not hasattr(store, "publish")


def test_list_keys_paginates_exact_prefix_and_sorts() -> None:
    client = FakeClient()
    client.list_responses = [
        {
            "Contents": [{"Key": "research/z"}, {"Key": "research/a"}],
            "IsTruncated": True,
            "NextContinuationToken": "next-token",
        },
        {
            "Contents": [{"Key": "research/m"}],
            "IsTruncated": False,
        },
    ]
    store = _store(client)
    assert store.list_keys("research/") == ["research/a", "research/m", "research/z"]
    assert client.list_calls == [
        {"Bucket": BUCKET, "Prefix": "research/", "MaxKeys": 1000},
        {
            "Bucket": BUCKET,
            "Prefix": "research/",
            "MaxKeys": 1000,
            "ContinuationToken": "next-token",
        },
    ]


def test_list_keys_fails_closed_on_bad_page() -> None:
    client = FakeClient()
    client.list_responses = [{"Contents": [{"Key": "other/key"}], "IsTruncated": False}]
    with pytest.raises(R2S3MalformedResponse):
        _store(client).list_keys("research/")


def test_list_keys_fails_closed_on_repeated_continuation_token() -> None:
    client = FakeClient()
    client.list_responses = [
        {"Contents": [], "IsTruncated": True, "NextContinuationToken": "same-token"},
        {"Contents": [], "IsTruncated": True, "NextContinuationToken": "same-token"},
    ]
    with pytest.raises(R2S3MalformedResponse, match="continuation token"):
        _store(client).list_keys("research/")


def test_get_bytes_enforces_length_bound_and_closes_stream() -> None:
    body = FakeBody(b"abc")
    client = FakeClient()
    client.get_responses = [{"Body": body, "ContentLength": 3}]
    assert _store(client, max_object_bytes=8).get_bytes("research/key") == b"abc"
    assert body.closed is True
    assert body.read_sizes == [9]
    assert client.get_calls == [{"Bucket": BUCKET, "Key": "research/key"}]


def test_get_bytes_rejects_length_mismatch_and_oversize() -> None:
    mismatch = FakeBody(b"abc")
    client = FakeClient()
    client.get_responses = [{"Body": mismatch, "ContentLength": 4}]
    with pytest.raises(R2S3LengthMismatch):
        _store(client, max_object_bytes=8).get_bytes("research/key")
    assert mismatch.closed is True

    oversize = FakeBody(b"x" * 9)
    client = FakeClient()
    client.get_responses = [{"Body": oversize, "ContentLength": 9}]
    with pytest.raises(PackageTooLarge):
        _store(client, max_object_bytes=8).get_bytes("research/key")
    assert oversize.closed is True


def test_put_if_absent_sends_only_narrow_conditional_request() -> None:
    client = FakeClient()
    client.put_results = [{}]
    payload = b"payload"
    assert _store(client).put_if_absent("research/key", payload) is True
    assert client.put_calls == [
        {
            "Bucket": BUCKET,
            "Key": "research/key",
            "Body": payload,
            "ContentLength": len(payload),
            "IfNoneMatch": "*",
        }
    ]


def test_put_if_absent_412_is_existing_false() -> None:
    client = FakeClient()
    client.put_results = [_client_error(412, "PreconditionFailed")]
    assert _store(client).put_if_absent("research/key", b"payload") is False
    assert client.get_calls == []


def test_put_if_absent_retries_409_then_succeeds() -> None:
    client = FakeClient()
    client.put_results = [
        _client_error(409, "ConditionalRequestConflict"),
        _client_error(409, "ConditionalRequestConflict"),
        {},
    ]
    assert _store(client, conflict_retries=2).put_if_absent("research/key", b"payload") is True
    assert len(client.put_calls) == 3


def test_exhausted_409_reconciles_identical_or_conflicting_bytes() -> None:
    payload = b"payload"
    client = FakeClient()
    client.put_results = [
        _client_error(409, "ConditionalRequestConflict"),
        _client_error(409, "ConditionalRequestConflict"),
    ]
    client.get_responses = [{"Body": FakeBody(payload), "ContentLength": len(payload)}]
    assert _store(client, conflict_retries=1).put_if_absent("research/key", payload) is False

    client = FakeClient()
    client.put_results = [_client_error(409, "ConditionalRequestConflict")]
    client.get_responses = [{"Body": FakeBody(b"different"), "ContentLength": 9}]
    with pytest.raises(ObjectConflictError):
        _store(client, conflict_retries=0).put_if_absent("research/key", payload)


def test_ambiguous_timeout_reconciles_exact_bytes() -> None:
    payload = b"payload"
    client = FakeClient()
    client.put_results = [ReadTimeoutError(endpoint_url=ENDPOINT)]
    client.get_responses = [{"Body": FakeBody(payload), "ContentLength": len(payload)}]
    assert _store(client).put_if_absent("research/key", payload) is False


def test_modeled_botocore_put_accepts_if_none_match_without_network() -> None:
    config = R2S3ObjectStore.build_client_config()
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id=ACCESS,
        aws_secret_access_key=SECRET,
        region_name="auto",
        config=config,
    )
    payload = b"payload"
    expected = {
        "Bucket": BUCKET,
        "Key": "research/key",
        "Body": payload,
        "ContentLength": len(payload),
        "IfNoneMatch": "*",
    }
    with Stubber(client) as stubber:
        stubber.add_response("put_object", {}, expected)
        assert _store(client).put_if_absent("research/key", payload) is True


def test_constructor_fails_closed_without_exposing_credentials() -> None:
    secret = "do-not-leak-this-secret"
    with pytest.raises(ValueError) as excinfo:
        R2S3ObjectStore(
            endpoint_url="http://example.invalid",
            bucket_name=BUCKET,
            access_key_id="private-access",
            secret_access_key=secret,
            client=FakeClient(),
        )
    text = str(excinfo.value)
    assert secret not in text
    assert "private-access" not in text

    with pytest.raises(ValueError, match="path"):
        R2S3ObjectStore(
            endpoint_url=ENDPOINT + "/unexpected-path",
            bucket_name=BUCKET,
            access_key_id=ACCESS,
            secret_access_key=SECRET,
            client=FakeClient(),
        )


def test_bounds_are_narrower_than_package_chain_cap() -> None:
    with pytest.raises(ValueError):
        _store(FakeClient(), max_object_bytes=MAX_PACKAGE_BYTES + 1)
    with pytest.raises(ValueError):
        _store(FakeClient(), conflict_retries=4)
