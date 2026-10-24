"""IPv6 自检：本机 IPv6 不通就让本进程只用 IPv4（D-46）。

背景（2026-10-24 实测）
--------------------
本机（移动家宽、光猫后）的 IPv6 是「有前缀、没出口」：WLAN 拿到 ``2409:8a4c:.../64``、
默认路由指向光猫 ``fe80::1``、光猫本身 ping 得通，但 ``ping 2400:3200::1`` 全超时、
``tracert -6`` 连第 1 跳都不回 TTL 超时、TCP 也不通。

而 Sina / 东财 / 连 ``www.baidu.com`` 的 AAAA 记录都排在 A 记录前面，``urllib3`` 建连时
按 ``getaddrinfo`` 的顺序**逐个地址试**：

- 每个不通的 IPv6 地址要等满 Windows 的 SYN 重试上限 **21s**；
- ``quotes.sina.cn`` 有 2 个 AAAA → 一次建连白等 **42s**；
- akshare 内部 ``requests.get`` 不传 timeout，只能干等。

于是表现为「一张财报 44s、一只 2 分钟」，历史上被误判成上游限速（D-38 / D-42）。
浏览器和 curl 有 Happy Eyeballs 会立刻切 IPv4，所以只有 requests 这类程序吃满。

做法
----
入口（``src.batch``，非 ``--offline``）调一次 :func:`ensure_ipv4_preferred`：
探测不通 → 把 ``socket.getaddrinfo`` 包一层，排掉 AF_INET6 结果。
显式 ``family=AF_INET6`` 的查询与纯 IPv6 域名原样放行（不影响局域网 IPv6 场景）。

开关（环境变量 ``PYRAMID_IPV6``）
- ``0``：强制只用 IPv4（跳过探测，探不通也不必等 3s）
- ``1``：强制保留 IPv6（跳过探测）
- 未设：自动探测（最多 2 个目标 × 2 个地址 × 1.5s ≈ 6s 上限，正常环境秒回）
"""

from __future__ import annotations

import logging
import os
import socket

from . import config

logger = logging.getLogger(__name__)

ENV_KEY = "PYRAMID_IPV6"

_installed = False  # getaddrinfo 是否已改成 IPv4 优先
_decision: bool | None = None  # 本进程的结论（只探测一次）


# ── 探测 ─────────────────────────────────────────────────────────────────
def ipv6_reachable(
    targets: tuple[str, ...] = config.IPV6_PROBE_TARGETS,
    *,
    port: int = config.IPV6_PROBE_PORT,
    timeout: float = config.IPV6_PROBE_TIMEOUT_SECONDS,
    max_addresses: int = config.IPV6_PROBE_MAX_ADDRESSES,
) -> bool | None:
    """TCP 建连探测 IPv6 是否真的能出去。

    :returns: 任一地址连通 → ``True``；查到 AAAA 但全不通 → ``False``；
        目标根本没有 AAAA 记录（无从判断）→ ``None``。
    """
    saw_aaaa = False
    for host in targets:
        try:
            infos = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)
        except OSError:  # 解析失败/无 AAAA → 换下一个目标
            continue
        for _family, socktype, proto, _canon, sockaddr in infos[:max_addresses]:
            saw_aaaa = True
            sock = socket.socket(socket.AF_INET6, socktype, proto)
            try:
                sock.settimeout(timeout)
                sock.connect(sockaddr)
                return True
            except OSError:  # 超时/不可达 → 试下一个地址
                continue
            finally:
                sock.close()
    return False if saw_aaaa else None


# ── 生效 ─────────────────────────────────────────────────────────────────
def install_ipv4_only() -> None:
    """把本进程的 ``socket.getaddrinfo`` 改成「只返回 IPv4」（幂等）。"""
    global _installed
    if _installed:
        return
    original = socket.getaddrinfo

    def _ipv4_first(
        host: str,
        port: object,
        family: int = 0,
        type: int = 0,  # noqa: A002 —— 与 socket.getaddrinfo 签名保持一致
        proto: int = 0,
        flags: int = 0,
    ) -> list:
        infos = original(host, port, family, type, proto, flags)
        ipv4 = [info for info in infos if info[0] == socket.AF_INET]
        return ipv4 or infos  # 纯 IPv6 目标 / 显式 AF_INET6 查询：原样返回

    socket.getaddrinfo = _ipv4_first
    _installed = True


def ensure_ipv4_preferred() -> bool:
    """入口调一次：本机 IPv6 不通就本进程只用 IPv4；返回是否已启用 IPv4 优先。

    同一进程内重复调用直接复用首次结论，不会重复探测。
    """
    global _decision
    if _decision is not None:
        return _decision

    override = os.environ.get(ENV_KEY, "").strip()
    if override == "0":
        install_ipv4_only()
        logger.warning("%s=0：跳过探测，本进程只用 IPv4", ENV_KEY)
        _decision = True
        return True
    if override == "1":
        logger.info("%s=1：跳过探测，保留 IPv6 双栈", ENV_KEY)
        _decision = False
        return False

    reachable = ipv6_reachable()
    if reachable is None:
        logger.info("IPv6 自检：探测目标无 AAAA 记录，按 IPv4 直连即可")
        _decision = False
        return False
    if reachable:
        logger.info("IPv6 自检：可用，保持双栈")
        _decision = False
        return False

    install_ipv4_only()
    logger.warning(
        "IPv6 自检：本机 IPv6 不通（有前缀无出口）→ 本进程改用 IPv4；"
        "否则每个 AAAA 地址建连要白等 21s（quotes.sina.cn 2 个 AAAA ≈ 42s/次）"
    )
    _decision = True
    return True


def reset() -> None:  # pragma: no cover —— 仅供测试复位
    """复位内部状态（不动已安装的 getaddrinfo 包装，测试请自行保存/还原）。"""
    global _installed, _decision
    _installed = False
    _decision = None
