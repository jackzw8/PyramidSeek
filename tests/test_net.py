"""`src/net.py`（IPv6 自检 → 本进程只用 IPv4）的逻辑测试（D-46）。

**全程不联网**：``getaddrinfo`` 与 ``socket`` 都用假实现替换，只验判定与包装行为。
真机背景见 ``src/net.py`` 顶部说明：本机 IPv6「有前缀没出口」，每个 AAAA 地址建连
白等 21s，``quotes.sina.cn`` 两个 AAAA ≈ 42s/次，表现为「一张财报 44s」。
"""

from __future__ import annotations

import socket

import pytest

from src import net

V6 = (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2409:8a4c::1", 443, 0, 0))
V4 = (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("39.156.6.102", 443))


class _FakeSocket:
    """假 socket：``connect`` 结果由构造参数决定。"""

    def __init__(self, *args: object, fail: bool = False, **_kwargs: object) -> None:
        self.fail = fail
        self.closed = False

    def settimeout(self, _timeout: float) -> None:
        return None

    def connect(self, _address: object) -> None:
        if self.fail:
            raise TimeoutError("连接超时（假）")

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _isolate() -> object:
    """每个用例前复位状态、用例后还原真 ``getaddrinfo``。"""
    original = socket.getaddrinfo
    net.reset()
    yield
    socket.getaddrinfo = original
    net.reset()


def _patch_infos(monkeypatch: pytest.MonkeyPatch, infos: list) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: list(infos))


def _patch_socket(monkeypatch: pytest.MonkeyPatch, *, fail: bool) -> None:
    monkeypatch.setattr(
        socket, "socket", lambda *a, **k: _FakeSocket(*a, fail=fail, **k)
    )


# ── getaddrinfo 包装 ──────────────────────────────────────────────────────
def test_install_filters_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_infos(monkeypatch, [V6, V4])
    net.install_ipv4_only()
    families = [info[0] for info in socket.getaddrinfo("quotes.sina.cn", 443)]
    assert families == [socket.AF_INET]


def test_install_keeps_pure_ipv6_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """纯 IPv6 目标（没有 A 记录）不能因为过滤而变成空集。"""
    _patch_infos(monkeypatch, [V6])
    net.install_ipv4_only()
    assert [info[0] for info in socket.getaddrinfo("ipv6-only.example", 443)] == [
        socket.AF_INET6
    ]


def test_install_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_infos(monkeypatch, [V6, V4])
    net.install_ipv4_only()
    first = socket.getaddrinfo
    net.install_ipv4_only()
    assert socket.getaddrinfo is first  # 不叠第二层包装
    assert [info[0] for info in socket.getaddrinfo("x", 443)] == [socket.AF_INET]


# ── 探测判定 ──────────────────────────────────────────────────────────────
def test_reachable_true_when_connect_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_infos(monkeypatch, [V6])
    _patch_socket(monkeypatch, fail=False)
    assert net.ipv6_reachable() is True


def test_reachable_false_when_all_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_infos(monkeypatch, [V6])
    _patch_socket(monkeypatch, fail=True)
    assert net.ipv6_reachable() is False


def test_reachable_none_without_aaaa(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_infos(monkeypatch, [])
    _patch_socket(monkeypatch, fail=True)
    assert net.ipv6_reachable() is None


# ── ensure_ipv4_preferred ────────────────────────────────────────────────
def test_probe_failure_switches_to_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_infos(monkeypatch, [V6, V4])
    monkeypatch.setattr(net, "ipv6_reachable", lambda *a, **k: False)
    assert net.ensure_ipv4_preferred() is True
    assert [info[0] for info in socket.getaddrinfo("quotes.sina.cn", 443)] == [
        socket.AF_INET
    ]


def test_probe_success_keeps_dual_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_infos(monkeypatch, [V6, V4])
    monkeypatch.setattr(net, "ipv6_reachable", lambda *a, **k: True)
    original = socket.getaddrinfo
    assert net.ensure_ipv4_preferred() is False
    assert socket.getaddrinfo is original
    assert len(socket.getaddrinfo("quotes.sina.cn", 443)) == 2


def test_probe_runs_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def _count(*_a: object, **_k: object) -> bool:
        calls.append(1)
        return False

    monkeypatch.setattr(net, "ipv6_reachable", _count)
    assert net.ensure_ipv4_preferred() is True
    assert net.ensure_ipv4_preferred() is True
    assert len(calls) == 1


def test_env_zero_forces_ipv4_without_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_infos(monkeypatch, [V6, V4])
    monkeypatch.setenv("PYRAMID_IPV6", "0")

    def _boom(*_a: object, **_k: object) -> bool:
        raise AssertionError("PYRAMID_IPV6=0 时不应探测")

    monkeypatch.setattr(net, "ipv6_reachable", _boom)
    assert net.ensure_ipv4_preferred() is True


def test_env_one_forces_dual_stack_without_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_infos(monkeypatch, [V6, V4])
    monkeypatch.setenv("PYRAMID_IPV6", "1")
    original = socket.getaddrinfo
    assert net.ensure_ipv4_preferred() is False
    assert socket.getaddrinfo is original
