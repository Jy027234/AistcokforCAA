"""抓取器网络守卫测试（主文档 §17.3）。

守卫的价值在于**真的会拒绝**。这里逐条验证拒绝行为，
包括最容易漏掉的两类：DNS 指向内网、白名单域名重定向到内网。
"""

from __future__ import annotations

import pytest

from aquant.adapters.providers.fetch_guard import (
    FetchDenied,
    FetchPolicy,
    assert_url_allowed,
    check_content_length,
    read_bounded,
)


def policy(**kw) -> FetchPolicy:
    base = dict(resolve_dns=False, allowed_hosts=frozenset({"quote.eastmoney.com"}))
    base.update(kw)
    return FetchPolicy(**base)


# ------------------------------------------------------------------ 协议
def test_http_and_https_allowed():
    for url in ("http://quote.eastmoney.com/x", "https://quote.eastmoney.com/x"):
        assert_url_allowed(url, policy())


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "ftp://quote.eastmoney.com/x",
    "gopher://quote.eastmoney.com/x",
    "data:text/plain;base64,AAAA",
    "javascript:alert(1)",
])
def test_non_http_schemes_rejected(url):
    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed(url, policy())
    assert exc.value.reason == "scheme-not-allowed"


def test_url_with_embedded_credentials_rejected():
    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed("https://user:pw@quote.eastmoney.com/x", policy())
    assert exc.value.reason == "credentials-in-url"


# ------------------------------------------------------------------ 主机
def test_host_not_in_allowlist_rejected():
    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed("https://evil.example.com/x", policy())
    assert exc.value.reason == "host-not-allowed"


@pytest.mark.parametrize("host", [
    "localhost", "metadata.google.internal", "instance-data", "metadata",
])
def test_metadata_and_local_hostnames_rejected(host):
    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed(f"http://{host}/x", policy(allowed_hosts=frozenset({host})))
    assert exc.value.reason == "blocked-hostname"


def test_suffix_allowlist_is_opt_in():
    p = FetchPolicy(resolve_dns=False, allowed_host_suffixes=frozenset({".eastmoney.com"}))
    assert_url_allowed("https://push2his.eastmoney.com/api", p)
    with pytest.raises(FetchDenied):
        assert_url_allowed("https://noteastmoney.com/api", p)


def test_non_default_port_rejected():
    """避免被抓取器当成内网端口扫描器。"""

    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed("https://quote.eastmoney.com:8080/x", policy())
    assert exc.value.reason == "port-not-allowed"


# ------------------------------------------------------------------ DNS -> 内网
def test_dns_resolving_to_loopback_rejected(monkeypatch):
    """白名单域名被指向 127.0.0.1（DNS 重绑定）。"""

    def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed("https://quote.eastmoney.com/x", policy(resolve_dns=True))
    assert exc.value.reason == "blocked-address"
    assert "loopback" in exc.value.detail


@pytest.mark.parametrize("addr", [
    "169.254.169.254",   # 云元数据
    "10.1.2.3",
    "192.168.1.1",
    "172.16.0.1",
    "100.64.0.1",        # 运营商级 NAT
    "0.0.0.0",
])
def test_dns_resolving_to_private_or_metadata_rejected(monkeypatch, addr):
    def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", (addr, port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed("https://quote.eastmoney.com/x", policy(resolve_dns=True))
    assert exc.value.reason == "blocked-address"


def test_public_ip_is_allowed(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    assert_url_allowed("https://quote.eastmoney.com/x", policy(resolve_dns=True))


def test_dns_failure_is_denied_not_ignored(monkeypatch):
    import socket as _socket

    def boom(host, port, **kw):
        raise _socket.gaierror("no such host")

    monkeypatch.setattr("socket.getaddrinfo", boom)
    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed("https://quote.eastmoney.com/x", policy(resolve_dns=True))
    assert exc.value.reason == "dns-failure"


# ------------------------------------------------------------------ 体积
def test_content_length_over_limit_rejected():
    with pytest.raises(FetchDenied) as exc:
        check_content_length("99999999", policy(max_response_bytes=1024))
    assert exc.value.reason == "response-too-large"


def test_content_length_absent_is_allowed():
    check_content_length(None, policy(max_response_bytes=1024))


def test_streamed_body_over_limit_aborts_early():
    """先读完再判断会打满内存，因此必须流式截断。"""

    import io

    class Counter(io.BytesIO):
        def __init__(self, data: bytes) -> None:
            super().__init__(data)
            self.read_calls = 0

        def read(self, n: int = -1) -> bytes:
            self.read_calls += 1
            return super().read(n)

    big = Counter(b"x" * (5 * 1024 * 1024))
    with pytest.raises(FetchDenied) as exc:
        read_bounded(big, policy(max_response_bytes=256 * 1024))
    assert exc.value.reason == "response-too-large"
    # 关键：没有把整个 5 MiB 读完
    assert big.read_calls <= 8, f"read {big.read_calls} chunks before aborting"


def test_read_bounded_returns_body_within_limit():
    import io

    assert read_bounded(io.BytesIO(b"abc"), policy()) == b"abc"

# ------------------------------------------------------------------ 穿透式代理
def test_fake_ip_proxy_range_is_blocked_by_default(monkeypatch):
    """默认不信任任何代理网段：伪 IP 仍按私有地址拒绝。"""

    def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", ("198.18.0.225", port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed("https://quote.eastmoney.com/x", policy(resolve_dns=True))
    assert exc.value.reason == "blocked-address"


def test_fake_ip_proxy_range_allowed_only_when_declared(monkeypatch):
    """运维方显式声明后放行；主机名白名单仍然生效。"""

    def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", ("198.18.0.225", port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    p = policy(resolve_dns=True,
               trusted_proxy_networks=frozenset({"198.18.0.0/15"}))
    assert_url_allowed("https://quote.eastmoney.com/x", p)
    # 白名单之外的主机仍然被拒——这是声明代理后仅存的 DNS 层防护
    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed("https://evil.example.com/x", p)
    assert exc.value.reason == "host-not-allowed"


def test_declaring_proxy_does_not_unblock_metadata_address(monkeypatch):
    """即使声明了代理网段，云元数据地址仍必须被拒。"""

    def fake_getaddrinfo(host, port, **kw):
        return [(2, 1, 6, "", ("169.254.169.254", port))]

    monkeypatch.setattr("socket.getaddrinfo", fake_getaddrinfo)
    p = policy(resolve_dns=True,
               trusted_proxy_networks=frozenset({"198.18.0.0/15"}))
    with pytest.raises(FetchDenied) as exc:
        assert_url_allowed("https://quote.eastmoney.com/x", p)
    assert exc.value.reason == "blocked-address"

