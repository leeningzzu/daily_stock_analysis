# -*- coding: utf-8 -*-
"""Thin Cloudflare R2/S3 transport for the research-state ObjectStore seam."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Optional
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from src.services.research_state_package_chain import (
    MAX_CHAIN_GENERATIONS,
    MAX_PACKAGE_BYTES,
    ObjectConflictError,
    PackageTooLarge,
    ResearchStateError,
)


DEFAULT_CONFLICT_RETRIES = 2
MAX_CONFLICT_RETRIES = 3

_AMBIGUOUS_PUT_ERRORS = (
    ConnectTimeoutError,
    ConnectionClosedError,
    EndpointConnectionError,
    ReadTimeoutError,
)


class R2S3ObjectStoreError(ResearchStateError):
    """Fail-closed transport error for the narrow R2 ObjectStore seam."""

    code = "RESEARCH_STATE_R2_TRANSPORT_ERROR"


class R2S3MalformedResponse(R2S3ObjectStoreError):
    code = "RESEARCH_STATE_R2_MALFORMED_RESPONSE"


class R2S3LengthMismatch(R2S3ObjectStoreError):
    code = "RESEARCH_STATE_R2_LENGTH_MISMATCH"


class R2S3ObjectStore:
    """Implement exactly list/get/put-if-absent over a boto3 low-level S3 client."""

    def __init__(
        self,
        *,
        endpoint_url: str,
        bucket_name: str,
        access_key_id: str,
        secret_access_key: str,
        region_name: str = "auto",
        max_object_bytes: int = MAX_PACKAGE_BYTES,
        conflict_retries: int = DEFAULT_CONFLICT_RETRIES,
        client: Optional[Any] = None,
        client_factory: Optional[Callable[..., Any]] = None,
    ):
        self._endpoint_url = self._validate_endpoint(endpoint_url)
        self._bucket_name = self._required_text(bucket_name, "bucket_name")
        access_key = self._required_text(access_key_id, "access_key_id")
        secret_key = self._required_text(secret_access_key, "secret_access_key")
        region = self._required_text(region_name, "region_name")

        max_bytes = int(max_object_bytes)
        if max_bytes < 1 or max_bytes > MAX_PACKAGE_BYTES:
            raise ValueError(f"max_object_bytes must be between 1 and {MAX_PACKAGE_BYTES}")
        self._max_object_bytes = max_bytes

        retries = int(conflict_retries)
        if retries < 0 or retries > MAX_CONFLICT_RETRIES:
            raise ValueError(f"conflict_retries must be between 0 and {MAX_CONFLICT_RETRIES}")
        self._conflict_retries = retries

        if client is not None:
            self._client = client
            return

        factory = client_factory or boto3.client
        self._client = factory(
            "s3",
            endpoint_url=self._endpoint_url,
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
            config=self.build_client_config(),
        )

    @staticmethod
    def build_client_config() -> Config:
        return Config(
            signature_version="s3v4",
            connect_timeout=10,
            read_timeout=60,
            retries={"mode": "standard", "total_max_attempts": 4},
            s3={"addressing_style": "path"},
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            tcp_keepalive=True,
        )

    def list_keys(self, prefix: str) -> list[str]:
        prefix_text = self._required_text(prefix, "prefix")
        continuation: Optional[str] = None
        seen_tokens: set[str] = set()
        keys: list[str] = []
        page_count = 0

        while True:
            page_count += 1
            if page_count > MAX_CHAIN_GENERATIONS + 1:
                raise R2S3MalformedResponse("ListObjectsV2 exceeded the bounded page limit")
            request: dict[str, Any] = {
                "Bucket": self._bucket_name,
                "Prefix": prefix_text,
                "MaxKeys": 1000,
            }
            if continuation is not None:
                request["ContinuationToken"] = continuation

            response = self._client.list_objects_v2(**request)
            contents = response.get("Contents", [])
            if not isinstance(contents, list):
                raise R2S3MalformedResponse("ListObjectsV2 Contents must be a list")

            for item in contents:
                if not isinstance(item, Mapping):
                    raise R2S3MalformedResponse("ListObjectsV2 item must be an object")
                key = item.get("Key")
                if not isinstance(key, str) or not key:
                    raise R2S3MalformedResponse("ListObjectsV2 item is missing a valid Key")
                if not key.startswith(prefix_text):
                    raise R2S3MalformedResponse("ListObjectsV2 returned a key outside the requested prefix")
                keys.append(key)
                if len(keys) > MAX_CHAIN_GENERATIONS + 1:
                    raise R2S3MalformedResponse("ListObjectsV2 exceeded the bounded key limit")

            truncated = bool(response.get("IsTruncated"))
            if not truncated:
                break
            token = response.get("NextContinuationToken")
            if not isinstance(token, str) or not token:
                raise R2S3MalformedResponse("truncated ListObjectsV2 response is missing continuation token")
            if token in seen_tokens:
                raise R2S3MalformedResponse("ListObjectsV2 repeated a continuation token")
            seen_tokens.add(token)
            continuation = token

        if len(keys) != len(set(keys)):
            raise R2S3MalformedResponse("ListObjectsV2 returned duplicate keys")
        return sorted(keys)

    def get_bytes(self, key: str) -> bytes:
        key_text = self._required_text(key, "key")
        response = self._client.get_object(Bucket=self._bucket_name, Key=key_text)
        body = response.get("Body")
        if body is None or not callable(getattr(body, "read", None)) or not callable(getattr(body, "close", None)):
            raise R2S3MalformedResponse("GetObject response is missing a closeable body")

        content_length = response.get("ContentLength")
        try:
            if content_length is not None:
                try:
                    expected_length = int(content_length)
                except (TypeError, ValueError) as exc:
                    raise R2S3MalformedResponse("GetObject ContentLength is invalid") from exc
                if expected_length < 0:
                    raise R2S3MalformedResponse("GetObject ContentLength cannot be negative")
                if expected_length > self._max_object_bytes:
                    raise PackageTooLarge(
                        f"object bytes {expected_length} exceed transport cap {self._max_object_bytes}"
                    )
            else:
                expected_length = None

            payload = body.read(self._max_object_bytes + 1)
            if not isinstance(payload, bytes):
                raise R2S3MalformedResponse("GetObject body must return bytes")
            if len(payload) > self._max_object_bytes:
                raise PackageTooLarge(
                    f"object bytes exceed transport cap {self._max_object_bytes}"
                )
            if expected_length is not None and len(payload) != expected_length:
                raise R2S3LengthMismatch(
                    f"GetObject length mismatch: expected {expected_length}, received {len(payload)}"
                )
            return payload
        finally:
            body.close()

    def put_if_absent(self, key: str, payload: bytes) -> bool:
        key_text = self._required_text(key, "key")
        if not isinstance(payload, bytes):
            raise TypeError("payload must be bytes")
        if len(payload) > self._max_object_bytes:
            raise PackageTooLarge(
                f"object bytes {len(payload)} exceed transport cap {self._max_object_bytes}"
            )

        request = {
            "Bucket": self._bucket_name,
            "Key": key_text,
            "Body": payload,
            "ContentLength": len(payload),
            "IfNoneMatch": "*",
        }

        for attempt in range(self._conflict_retries + 1):
            try:
                self._client.put_object(**request)
                return True
            except ClientError as exc:
                status = self._client_error_status(exc)
                code = self._client_error_code(exc)
                if status == 412 or code == "PreconditionFailed":
                    return False
                if status == 409 or code == "ConditionalRequestConflict":
                    if attempt < self._conflict_retries:
                        continue
                    return self._reconcile_ambiguous_put(key_text, payload, exc)
                raise
            except _AMBIGUOUS_PUT_ERRORS as exc:
                return self._reconcile_ambiguous_put(key_text, payload, exc)

        raise R2S3ObjectStoreError("conditional put exhausted without a terminal result")

    def _reconcile_ambiguous_put(self, key: str, payload: bytes, original_error: Exception) -> bool:
        try:
            existing = self.get_bytes(key)
        except ClientError as get_error:
            status = self._client_error_status(get_error)
            code = self._client_error_code(get_error)
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                raise original_error
            raise
        except _AMBIGUOUS_PUT_ERRORS:
            raise original_error

        if existing == payload:
            return False
        raise ObjectConflictError(f"immutable object conflict after ambiguous put: {key}")

    @staticmethod
    def _client_error_status(exc: ClientError) -> Optional[int]:
        response = exc.response if isinstance(exc.response, Mapping) else {}
        metadata = response.get("ResponseMetadata", {})
        if not isinstance(metadata, Mapping):
            return None
        value = metadata.get("HTTPStatusCode")
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _client_error_code(exc: ClientError) -> str:
        response = exc.response if isinstance(exc.response, Mapping) else {}
        error = response.get("Error", {})
        if not isinstance(error, Mapping):
            return ""
        return str(error.get("Code") or "")

    @staticmethod
    def _required_text(value: str, field_name: str) -> str:
        text = str(value or "").strip()
        if not text:
            raise ValueError(f"{field_name} is required")
        return text

    @staticmethod
    def _validate_endpoint(value: str) -> str:
        endpoint = R2S3ObjectStore._required_text(value, "endpoint_url")
        parsed = urlparse(endpoint)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError("endpoint_url must be an absolute HTTPS URL")
        if parsed.username or parsed.password:
            raise ValueError("endpoint_url must not contain embedded credentials")
        if parsed.path not in {"", "/"}:
            raise ValueError("endpoint_url must not contain a path")
        if parsed.query or parsed.fragment:
            raise ValueError("endpoint_url must not contain query or fragment components")
        return endpoint.rstrip("/")
