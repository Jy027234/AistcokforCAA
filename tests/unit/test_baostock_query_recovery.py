"""A broken SDK receive must not poison subsequent read-only queries."""
from types import SimpleNamespace
import socket
import sys

import pytest

from aquant.adapters.providers.baostock import (
    BaostockClient, BaostockUnavailable, _SocketEOFGuard,
)


class Result:
    fields = ['code', 'ipoDate']

    def __init__(self, code, rows=()):
        self.error_code = code
        self.error_msg = 'network receive failed' if code != '0' else 'success'
        self.rows = list(rows)
        self.index = -1

    def next(self):
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self):
        return self.rows[self.index]


def sdk(monkeypatch, responses):
    events = []
    remaining = iter(responses)

    def login():
        events.append('login')
        return Result('0')

    def logout():
        events.append('logout')
        return Result('0')

    def query_stock_basic(*, code):
        events.append(('query', code))
        return next(remaining)

    monkeypatch.setitem(sys.modules, 'baostock', SimpleNamespace(
        login=login, logout=logout, query_stock_basic=query_stock_basic))
    return events


@pytest.fixture(autouse=True)
def restore_default_timeout():
    previous = socket.getdefaulttimeout()
    yield
    socket.setdefaulttimeout(previous)


@pytest.mark.parametrize('initial', ['10002007', '10001001'])
def test_read_reconnects_once_and_only_archives_success(tmp_path, monkeypatch, initial):
    events = sdk(monkeypatch, [Result(initial), Result('0', [('sz.002001', '2004-06-25')])])
    with BaostockClient(tmp_path) as client:
        rows, receipt = client.stock_basic('sz.002001')
        assert rows == [{'code': 'sz.002001', 'ipoDate': '2004-06-25'}]
        assert receipt.record_count == 1 and len(client.receipts) == 1
        assert (tmp_path / receipt.stored_path).is_file()
    assert events == ['login', ('query', 'sz.002001'), 'logout', 'login',
                      ('query', 'sz.002001'), 'logout']


@pytest.mark.parametrize('codes,query_count', [(['10002007', '10002007'], 2), (['10004004'], 1)])
def test_exhausted_transport_or_nontransport_error_is_exposed(tmp_path, monkeypatch, codes, query_count):
    events = sdk(monkeypatch, [Result(code) for code in codes])
    with BaostockClient(tmp_path) as client:
        with pytest.raises(BaostockUnavailable, match=codes[-1]):
            client.stock_basic('sz.002001')
        assert not client.receipts
    assert sum(isinstance(event, tuple) for event in events) == query_count
    assert not list(tmp_path.rglob('*'))


class ScriptedSocket:
    def __init__(self, reads):
        self.reads = list(reads)
        self.recv_calls = 0

    def recv(self, _size):
        self.recv_calls += 1
        value = self.reads.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


def test_sdk_socket_guard_preserves_fragments_and_raises_on_eof():
    raw_socket = ScriptedSocket([b'first frame ', b'second frame', b''])
    guarded = _SocketEOFGuard(raw_socket)

    assert guarded.recv(8192) == b'first frame '
    assert guarded.recv(8192) == b'second frame'
    with pytest.raises(ConnectionResetError, match='closed before response'):
        guarded.recv(8192)
    assert raw_socket.recv_calls == 3


def test_sdk_socket_guard_preserves_socket_timeout():
    timeout = socket.timeout('timed out')
    guarded = _SocketEOFGuard(ScriptedSocket([timeout]))

    with pytest.raises(socket.timeout) as caught:
        guarded.recv(8192)

    assert caught.value is timeout


def test_login_guards_only_the_sdk_transport_socket(tmp_path, monkeypatch):
    raw_socket = ScriptedSocket([b''])
    context = SimpleNamespace(default_socket=None)

    class FakeSocketUtil:
        instance = None

        def __new__(cls):
            if cls.instance is None:
                cls.instance = super().__new__(cls)
            return cls.instance

        def connect(self, _api_key):
            context.default_socket = raw_socket

    sock = SimpleNamespace(SocketUtil=FakeSocketUtil)
    namespace = {'sock': sock, 'conx': context, 'Result': Result}
    exec(
        'def login():\n'
        '    sock.SocketUtil().connect("anonymous")\n'
        '    try:\n'
        '        conx.default_socket.recv(8192)\n'
        '    except ConnectionResetError:\n'
        '        return Result("10002007")\n'
        '    return Result("0")\n',
        namespace,
    )
    monkeypatch.setitem(sys.modules, 'baostock', SimpleNamespace(
        login=namespace['login'], logout=lambda: Result('0')))
    previous_timeout = socket.getdefaulttimeout()

    client = BaostockClient(tmp_path, query_timeout=1.0)
    with pytest.raises(BaostockUnavailable, match='10002007'):
        client.login()

    assert raw_socket.recv_calls == 1
    assert isinstance(context.default_socket, _SocketEOFGuard)
    assert 'connect' not in vars(FakeSocketUtil())
    assert socket.getdefaulttimeout() == previous_timeout
