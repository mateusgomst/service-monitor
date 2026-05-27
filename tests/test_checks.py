import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monitor  # noqa: E402


class _Handler(BaseHTTPRequestHandler):
    code = 200
    body = b"ok status here"

    def do_GET(self):
        self.send_response(self.code)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args, **kwargs):
        pass


def _start_http_server(code=200, body=b"ok"):
    _Handler.code = code
    _Handler.body = body
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class TestCheckHttp(unittest.TestCase):
    def test_200_ok(self):
        srv, url = _start_http_server(200, b"hello")
        try:
            r = monitor.check_http(monitor.SiteSpec(url=url), timeout=5, verify_tls=False)
            self.assertTrue(r.ok)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_500_falha(self):
        srv, url = _start_http_server(500, b"err")
        try:
            r = monitor.check_http(monitor.SiteSpec(url=url), timeout=5, verify_tls=False)
            self.assertFalse(r.ok)
            self.assertIn("HTTP 500", r.message)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_body_contains_ok(self):
        srv, url = _start_http_server(200, b'{"status":"ok"}')
        try:
            r = monitor.check_http(
                monitor.SiteSpec(url=url, body_contains='"status":"ok"'),
                timeout=5,
                verify_tls=False,
            )
            self.assertTrue(r.ok)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_body_contains_missing(self):
        srv, url = _start_http_server(200, b'{"status":"DOWN"}')
        try:
            r = monitor.check_http(
                monitor.SiteSpec(url=url, body_contains="ok"),
                timeout=5,
                verify_tls=False,
            )
            self.assertFalse(r.ok)
            self.assertIn("body não contém", r.message)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_codigo_customizado(self):
        srv, url = _start_http_server(401, b"unauthorized")
        try:
            r = monitor.check_http(
                monitor.SiteSpec(url=url, expected_code=401),
                timeout=5,
                verify_tls=False,
            )
            self.assertTrue(r.ok)
        finally:
            srv.shutdown()
            srv.server_close()

    def test_conexao_recusada(self):
        # Porta improvável de estar aberta
        r = monitor.check_http(
            monitor.SiteSpec(url="http://127.0.0.1:1"),
            timeout=2,
            verify_tls=False,
        )
        self.assertFalse(r.ok)


class TestCheckTcp(unittest.TestCase):
    def test_porta_aberta(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        try:
            r = monitor.check_tcp(monitor.TcpSpec(host="127.0.0.1", port=port), timeout=2)
            self.assertTrue(r.ok)
        finally:
            srv.close()

    def test_porta_fechada(self):
        # 0 = qualquer porta disponível, mas usar uma improvável
        r = monitor.check_tcp(monitor.TcpSpec(host="127.0.0.1", port=1), timeout=2)
        self.assertFalse(r.ok)


class TestLockfile(unittest.TestCase):
    def test_lock_excludes_second(self):
        import tempfile
        tmp = Path(tempfile.mkdtemp())
        lock_path = tmp / "test.lock"
        fd1 = monitor.acquire_lock(lock_path)
        try:
            with self.assertRaises(monitor.LockBusy):
                monitor.acquire_lock(lock_path)
        finally:
            monitor.release_lock(fd1)
        # Após release, deve adquirir
        fd2 = monitor.acquire_lock(lock_path)
        monitor.release_lock(fd2)


if __name__ == "__main__":
    unittest.main()
