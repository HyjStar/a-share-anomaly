"""Development/restricted-network transport backed by the system ``curl`` executable.

Some office networks reset Python's OpenSSL TLS handshakes to the Eastmoney quote
hosts while the OS-native curl client works. This module monkey-patches
``requests.Session.request`` so AkShare can still be exercised locally. It is
optional and disabled by default; normal Linux servers and GitHub Actions do not
need it.
"""

from __future__ import annotations

import shutil
import subprocess
from urllib.parse import urlencode, urlsplit, urlunsplit

import requests
from requests.models import Response
from requests.structures import CaseInsensitiveDict


def _curl_executable() -> str | None:
    for name in ("curl.exe", "curl"):
        path = shutil.which(name)
        if path:
            return path
    return None


def patch_requests_with_curl() -> None:
    if getattr(requests.Session, "_curl_patched", False):
        return
    executable = _curl_executable()
    if not executable:
        raise RuntimeError("找不到 curl 命令，无法启用 curl 传输适配器")
    original_request = requests.Session.request

    def _request(self, method, url, **kwargs):
        if method.upper() != "GET" or kwargs.get("stream"):
            return original_request(self, method, url, **kwargs)

        params = kwargs.get("params")
        if params:
            query = urlencode(params, doseq=True)
            parts = urlsplit(url)
            url = urlunsplit(
                (parts.scheme, parts.netloc, parts.path, query, parts.fragment)
            )

        headers = CaseInsensitiveDict(self.headers)
        headers.update(kwargs.get("headers") or {})
        timeout = kwargs.get("timeout", 30)
        if isinstance(timeout, tuple):
            timeout = max(float(item) for item in timeout if item)
        timeout = int(timeout or 30)

        cmd = [
            executable,
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(timeout),
            "--write-out",
            "\n__HTTP_CODE__%{http_code}",
        ]
        for key, value in headers.items():
            # 让请求尽量接近本机可用的原生 curl：
            # 不转发 Python UA / Connection / Accept-Encoding，也就不用处理压缩响应。
            if value is None or key.lower() in {
                "accept-encoding",
                "user-agent",
                "connection",
            }:
                continue
            cmd.extend(["-H", f"{key}: {value}"])
        cmd.append(url)

        proc = subprocess.run(
            cmd, check=False, capture_output=True, timeout=timeout + 10
        )
        raw = proc.stdout
        marker = b"\n__HTTP_CODE__"
        if marker in raw:
            body, _, code = raw.rpartition(marker)
        else:
            body, code = raw, b"0"
        try:
            status_code = int(code.strip() or b"0")
        except ValueError:
            status_code = 0
        if proc.returncode != 0 and status_code == 0:
            raise requests.ConnectionError(
                f"curl 失败 ({proc.returncode}): {proc.stderr.decode('utf-8', 'replace')[:500]}"
            )

        response = Response()
        response.status_code = status_code
        response._content = body
        response.encoding = "utf-8"
        response.url = url
        response.request = requests.Request(method, url).prepare()
        response.headers = CaseInsensitiveDict({"Content-Type": "application/json"})
        response.reason = "OK"
        return response

    requests.Session.request = _request
    requests.Session._curl_patched = True
