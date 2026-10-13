import socket

import pytest
from pytest_socket import SocketBlockedError


def test_test_suite_cannot_open_network_or_resolve_hosts():
    with pytest.warns(UserWarning, match=r"socket\.socket"), pytest.raises(SocketBlockedError):
        socket.socket()
    with pytest.warns(UserWarning, match=r"socket\.getaddrinfo"), pytest.raises(SocketBlockedError):
        socket.getaddrinfo("example.com", 443)
