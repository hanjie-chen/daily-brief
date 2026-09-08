from __future__ import annotations

import ipaddress
import socket
from functools import partial
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlparse
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    build_opener,
)

from .contracts import ArticleFetchError


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, resolver) -> None:
        super().__init__()
        self.resolver = resolver

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_public_http_url(newurl, self.resolver)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _PinnedHTTPConnection(HTTPConnection):
    def __init__(self, *args, resolver, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = partial(
            _create_public_connection,
            resolver=resolver,
        )


class _PinnedHTTPSConnection(HTTPSConnection):
    def __init__(self, *args, resolver, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = partial(
            _create_public_connection,
            resolver=resolver,
        )


class _PinnedHTTPHandler(HTTPHandler):
    def __init__(self, resolver) -> None:
        super().__init__()
        self.resolver = resolver

    def http_open(self, request):
        connection = partial(_PinnedHTTPConnection, resolver=self.resolver)
        return self.do_open(connection, request)


class _PinnedHTTPSHandler(HTTPSHandler):
    def __init__(self, resolver) -> None:
        super().__init__()
        self.resolver = resolver

    def https_open(self, request):
        connection = partial(_PinnedHTTPSConnection, resolver=self.resolver)
        return self.do_open(connection, request, context=self._context)


def _build_safe_opener(resolver):
    return build_opener(
        ProxyHandler({}),
        _SafeRedirectHandler(resolver),
        _PinnedHTTPHandler(resolver),
        _PinnedHTTPSHandler(resolver),
    )


def _create_public_connection(
    address,
    timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address=None,
    *,
    resolver,
):
    host, port = address
    addresses = _resolve_public_addresses(host, port, resolver)
    errors = []
    for address_family, socket_type, protocol, _, socket_address in addresses:
        connection = None
        try:
            connection = socket.socket(address_family, socket_type, protocol)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                connection.settimeout(timeout)
            if source_address:
                connection.bind(source_address)
            connection.connect(socket_address)
            return connection
        except OSError as exc:
            errors.append(exc)
            if connection is not None:
                connection.close()
    if errors:
        raise errors[-1]
    raise OSError("hostname did not resolve to a usable public address")


def _resolve_public_addresses(host: str, port: int, resolver):
    try:
        addresses = resolver(host, port, type=socket.SOCK_STREAM)
        if not addresses:
            raise ValueError("hostname did not resolve")
        for address_info in addresses:
            address = address_info[4][0].split("%", 1)[0]
            if not ipaddress.ip_address(address).is_global:
                raise ValueError("destination is not public")
    except (OSError, ValueError) as exc:
        raise ArticleFetchError(
            "article URL is not a safe public HTTP destination",
            error_code="unsafe_url",
        ) from exc
    return addresses


def _validate_public_http_url(url: str, resolver) -> None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("unsupported URL")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise ArticleFetchError(
            "article URL is not a safe public HTTP destination",
            error_code="unsafe_url",
        ) from exc
    _resolve_public_addresses(parsed.hostname, port, resolver)


def _read_bounded(response, max_bytes: int) -> bytes:
    payload = response.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise ArticleFetchError(
            "article response is too large",
            error_code="response_too_large",
        )
    return payload


def _enforce_extracted_limit(text: str, max_bytes: int, *, extractor: str) -> None:
    if len(text.encode("utf-8")) > max_bytes:
        raise ArticleFetchError(
            "extracted article text is too large",
            error_code="extracted_content_too_large",
            extractor=extractor,
        )
