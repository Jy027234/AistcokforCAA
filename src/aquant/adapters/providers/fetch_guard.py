"""抓取器网络守卫。

主文档 §17.3 明文要求：
    "抓取器限制域名、协议、重定向和文件大小，阻止访问内网、云元数据地址和本地文件。
     文档解析在受限进程中运行；模型工具不继承抓取器的任意网络权限。"

本模块把这些要求实现为**可测试的拒绝**，而不是写在文档里的承诺。
任何抓取都必须先经过 :func:`assert_url_allowed`。

设计原则：
  * 默认拒绝（allowlist），不是默认放行。
  * 校验在**每一跳重定向**后重新执行，防止"白名单域名 302 到内网"。
  * 主机名解析为 IP 后再检查，防止 DNS 指向内网（含 IPv6 与各类写法）。
  * 穿透式代理（fake-IP）会削弱这一层：当所有域名都解析到同一个私有网段时，
    本地无法证明连接的真实目的地。此时必须由运维方通过
    "trusted_proxy_networks" 显式声明，且应知悉 DNS 层防护已降级为
    "仅主机名白名单" 加协议/端口/体积/重定向检查。**默认不信任任何代理网段。**
  * 响应体有硬上限，且按流式读取时即截断，不是读完再判断。
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from urllib.parse import urlsplit

ALLOWED_SCHEMES = frozenset({"http", "https"})

#: CIDR 黑名单。覆盖回环、私有段、链路本地、共享地址、组播、保留段，以及常见云元数据端点。
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(cidr)
    for cidr in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",      # 运营商级 NAT
        "127.0.0.0/8",
        "169.254.0.0/16",     # 链路本地（含云元数据 169.254.169.254）
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::1/128",
        "fc00::/7",           # 唯一本地地址
        "fe80::/10",          # 链路本地
        "ff00::/8",           # 组播
    )
)

#: 云元数据与本地服务名，直接在主机名层面拒绝（不依赖 DNS 结果）。
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "metadata",
})


class FetchDenied(Exception):
    """抓取被守卫拒绝。携带为什么被拒，便于诊断与审计。"""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


@dataclass(slots=True)
class FetchPolicy:
    """抓取策略。默认值即安全值。"""

    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    allowed_schemes: frozenset[str] = ALLOWED_SCHEMES
    max_redirects: int = 3
    max_response_bytes: int = 8 * 1024 * 1024
    #: 解析主机名并校验 IP。测试可关闭以避免依赖 DNS。
    resolve_dns: bool = True
    #: 允许的子域后缀（例如 ".eastmoney.com"）。
    #: 注意：允许后缀是**放宽**操作，默认不使用。
    allowed_host_suffixes: frozenset[str] = field(default_factory=frozenset)
    #: 穿透式代理的伪 IP 网段（fake-IP）。
    #:
    #: 背景：某些代理/VPN 以伪 IP 实现 DNS，所有域名都解析到固定私有网段
    #: （实测本机：198.18.0.0/15 与 fdfe:dcba:9876::/48，网关 198.18.0.2）。
    #: 此时 DNS->IP 检查无法证明真实目的地，因为连接终止在本地代理。
    #:
    #: 因此**默认不使用**；仅在运维方已知并接受该代理时显式声明。
    #: 声明后该网段被放行，但主机名白名单、协议、端口、体积与重定向检查仍然生效。
    #: 安全含义：声明后 DNS 层防护降级为仅主机名白名单——见模块文档的说明。
    trusted_proxy_networks: frozenset[str] = field(default_factory=frozenset)

    def trusted_proxy_ips(self) -> tuple:
        return tuple(ipaddress.ip_network(c) for c in self.trusted_proxy_networks)

    def host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        if host in self.allowed_hosts:
            return True
        return any(host.endswith(suffix) for suffix in self.allowed_host_suffixes)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    if ip.is_loopback:
        return "loopback address"
    if ip.is_private:
        return "private address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_reserved:
        return "reserved address"
    if ip.is_unspecified:
        return "unspecified address"
    for net in _BLOCKED_NETWORKS:
        if ip.version == net.version and ip in net:
            return f"blocked network {net}"
    return None


def assert_url_allowed(url: str, policy: FetchPolicy) -> str:
    """校验 URL 是否允许抓取。返回规范化后的 URL。

    抛 :class:`FetchDenied` 而不是返回布尔值——调用方无法"忘记检查返回值"。
    """

    parts = urlsplit(url)

    # 1. 协议
    scheme = (parts.scheme or "").lower()
    if scheme not in policy.allowed_schemes:
        raise FetchDenied("scheme-not-allowed", f"{scheme!r} is not in {sorted(policy.allowed_schemes)}")

    # 2. 禁止内嵌凭证
    if parts.username or parts.password:
        raise FetchDenied("credentials-in-url", "URL must not embed credentials")

    # 3. 主机
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise FetchDenied("missing-host", "URL has no host")
    if host in _BLOCKED_HOSTNAMES:
        raise FetchDenied("blocked-hostname", f"{host!r} is a metadata/local service name")
    if not policy.host_allowed(host):
        raise FetchDenied("host-not-allowed", f"{host!r} is not in the allowlist")

    # 4. 端口：只允许显式协议默认端口，避免被当作内网端口扫描器
    if parts.port is not None:
        default = 443 if scheme == "https" else 80
        if parts.port != default:
            raise FetchDenied("port-not-allowed", f"port {parts.port} is not the {scheme} default")

    # 5. DNS 解析后校验 IP（防 DNS 指向内网）
    if policy.resolve_dns:
        try:
            infos = socket.getaddrinfo(host, parts.port or (443 if scheme == "https" else 80),
                                       proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise FetchDenied("dns-failure", f"cannot resolve {host!r}: {exc}") from exc
        seen: set[str] = set()
        for info in infos:
            addr = info[4][0]
            if addr in seen:
                continue
            seen.add(addr)
            try:
                ip = ipaddress.ip_address(addr)
            except ValueError:
                raise FetchDenied("bad-address", f"unparseable address {addr!r}") from None
            why = _is_blocked_ip(ip)
            if why:
                # 穿透式代理的伪 IP：目的地由代理决定，本地无法验证。
                # 只有当运维方显式把该网段声明为可信代理时才放行。
                if any(ip.version == net.version and ip in net
                       for net in policy.trusted_proxy_ips()):
                    continue
                raise FetchDenied("blocked-address", f"{host} resolves to {addr} ({why})")

    return url


def check_content_length(declared: str | None, policy: FetchPolicy, *,
                         max_bytes: int | None = None) -> None:
    """在读取正文前，用 Content-Length 做一次快速拒绝。

    max_bytes 允许单次调用覆盖策略上限（例如公告 PDF 天然大于 JSON 列表）。
    **覆盖的是数值，不是检查本身**——上限必须始终存在（§17.3）。
    """

    limit = policy.max_response_bytes if max_bytes is None else max_bytes
    if declared is None:
        return
    try:
        n = int(declared)
    except ValueError:
        raise FetchDenied("bad-content-length", f"Content-Length {declared!r} is not an integer") from None
    if n > limit:
        raise FetchDenied(
            "response-too-large",
            f"declared {n} bytes exceeds limit {limit}",
        )


def read_bounded(stream, policy: FetchPolicy, *,
                 max_bytes: int | None = None) -> bytes:
    """流式读取并在超过上限时立即中止。

    先读完再判断会先把内存打满，因此这里逐块累加并在越界的第一时间抛错。
    """

    limit = policy.max_response_bytes if max_bytes is None else max_bytes
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise FetchDenied(
                "response-too-large",
                f"body exceeded limit {limit} bytes while streaming",
            )
        chunks.append(chunk)
    return b"".join(chunks)
