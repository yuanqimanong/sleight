"""Bounded requests through the current page's browser network stack."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from .errors import ProtocolError, ResponseTooLarge, TimeoutError

MAX_RESPONSE_BYTES = 1024 * 1024
_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
_HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
_FORBIDDEN_HEADERS = {
    "accept-charset",
    "accept-encoding",
    "access-control-request-headers",
    "access-control-request-method",
    "connection",
    "content-length",
    "cookie",
    "cookie2",
    "date",
    "dnt",
    "expect",
    "host",
    "keep-alive",
    "origin",
    "permissions-policy",
    "referer",
    "set-cookie",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "user-agent",
    "via",
}


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """Text response. HTTP errors remain responses; transport errors raise."""

    url: str
    status: int
    status_text: str
    headers: dict[str, str]
    text: str = field(repr=False)
    redirected: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        return json.loads(self.text)


def validate_referrer(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 8192:
        raise ValueError("referrer must be an HTTP(S) URL without credentials")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or any(c in value for c in "\r\n")
    ):
        raise ValueError("referrer must be an HTTP(S) URL without credentials")
    return value


def request_expression(url, *, method, headers, body, timeout, max_bytes):
    if not isinstance(url, str) or not url or len(url) > 8192 or any(c in url for c in "\r\n"):
        raise ValueError("fetch needs a URL of 1–8192 characters")
    parsed = urlsplit(url)
    if (
        (parsed.scheme and parsed.scheme not in {"http", "https"})
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("fetch supports HTTP(S) URLs without credentials")
    method = str(method).upper()
    if method not in _METHODS:
        raise ValueError("unsupported fetch method")
    if body is not None and (
        not isinstance(body, str) or len(body.encode("utf-8")) > MAX_RESPONSE_BYTES
    ):
        raise ValueError("fetch body must be text of at most 1 MiB")
    if body is not None and method in {"GET", "HEAD"}:
        raise ValueError("GET and HEAD cannot have a body")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0.1 <= timeout <= 120
    ):
        raise ValueError("fetch timeout must be 0.1–120 seconds")
    if (
        isinstance(max_bytes, bool)
        or not isinstance(max_bytes, int)
        or not 1 <= max_bytes <= MAX_RESPONSE_BYTES
    ):
        raise ValueError("fetch max_bytes must be 1–1048576")
    if headers is not None and not isinstance(headers, dict):
        raise ValueError("fetch headers must be a dictionary")
    clean = {}
    for name, value in (headers or {}).items():
        if (
            not isinstance(name, str)
            or not _HEADER_NAME.fullmatch(name)
            or not isinstance(value, str)
            or any(c in value for c in "\r\n")
            or name.lower() in _FORBIDDEN_HEADERS
            or name.lower().startswith(("sec-", "proxy-"))
        ):
            raise ValueError("invalid header or header controlled by the browser")
        clean[name] = value
    if len(json.dumps(clean).encode()) > 32768:
        raise ValueError("fetch headers exceed 32 KiB")
    options = {
        "url": url,
        "method": method,
        "headers": clean,
        "body": body,
        "timeout_ms": math.ceil(timeout * 1000),
        "max_bytes": max_bytes,
    }
    return "(" + _FETCH_JS + ")(" + json.dumps(options, ensure_ascii=True) + ")"


def response_from_result(result):
    if not isinstance(result, dict):
        raise ProtocolError("Browser fetch did not return a response")
    error = result.get("error")
    if error == "timeout":
        raise TimeoutError("Browser fetch timed out; the request was not replayed")
    if error == "too_large":
        raise ResponseTooLarge("Browser fetch response exceeded max_bytes; reading was cancelled")
    if error:
        raise ProtocolError("Browser fetch failed (network, CORS, CSP or invalid page context)")
    return FetchResponse(**result)


_FETCH_JS = r"""async function (options) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), options.timeout_ms);
  let reader;
  try {
    const url = new URL(options.url, location.href);
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password)
      return {error: 'invalid_url'};
    const response = await window.fetch(url.href, {
      method: options.method, headers: options.headers, body: options.body,
      credentials: 'include', mode: 'cors', signal: controller.signal
    });
    if (response.type === 'opaque' || response.type === 'opaqueredirect')
      return {error: 'opaque'};
    const headers = Object.fromEntries(response.headers.entries());
    if (JSON.stringify(headers).length > 32768) {
      controller.abort();
      return {error: 'too_large'};
    }
    let text = '', size = 0;
    const decoder = new TextDecoder('utf-8');
    if (response.body) {
      reader = response.body.getReader();
      while (true) {
        const chunk = await reader.read();
        if (chunk.done) break;
        size += chunk.value.byteLength;
        if (size > options.max_bytes) {
          controller.abort();
          return {error: 'too_large'};
        }
        text += decoder.decode(chunk.value, {stream: true});
      }
      text += decoder.decode();
    }
    return {url: response.url, status: response.status, status_text: response.statusText,
      headers, text, redirected: response.redirected};
  } catch (error) {
    return {error: controller.signal.aborted ? 'timeout' : 'network'};
  } finally {
    clearTimeout(timer);
    if (reader) reader.releaseLock();
  }
}"""
