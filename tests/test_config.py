import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monitor  # noqa: E402


class TestParseSites(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(monitor.parse_sites(""), [])
        self.assertEqual(monitor.parse_sites("\n\n  \n"), [])

    def test_url_only(self):
        sites = monitor.parse_sites("https://toshop.com.br")
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0].url, "https://toshop.com.br")
        self.assertEqual(sites[0].expected_code, 200)
        self.assertIsNone(sites[0].body_contains)

    def test_url_with_code(self):
        sites = monitor.parse_sites("https://x.com | 401")
        self.assertEqual(sites[0].expected_code, 401)

    def test_url_with_code_and_body(self):
        sites = monitor.parse_sites('https://x.com/health | 200 | "status":"ok"')
        self.assertEqual(sites[0].expected_code, 200)
        self.assertEqual(sites[0].body_contains, '"status":"ok"')

    def test_multiline(self):
        raw = """
        https://a.com
        https://b.com:445 | 200
        # comentário
        https://c.com | 200 | foo
        """
        sites = monitor.parse_sites(raw)
        self.assertEqual([s.url for s in sites], ["https://a.com", "https://b.com:445", "https://c.com"])

    def test_porta_nao_padrao(self):
        sites = monitor.parse_sites("https://monit.sysout.com.br:445")
        self.assertEqual(sites[0].url, "https://monit.sysout.com.br:445")

    def test_codigo_invalido_usa_default(self):
        sites = monitor.parse_sites("https://x.com | abc")
        self.assertEqual(sites[0].expected_code, 200)


class TestParseTcp(unittest.TestCase):
    def test_basic(self):
        tcp = monitor.parse_tcp("127.0.0.1:6379\n127.0.0.1:5432")
        self.assertEqual(len(tcp), 2)
        self.assertEqual(tcp[0].host, "127.0.0.1")
        self.assertEqual(tcp[0].port, 6379)
        self.assertEqual(tcp[1].port, 5432)

    def test_ignora_invalido(self):
        tcp = monitor.parse_tcp("127.0.0.1\ninvalido\n127.0.0.1:abc\n127.0.0.1:80")
        self.assertEqual(len(tcp), 1)
        self.assertEqual(tcp[0].port, 80)

    def test_hostname(self):
        tcp = monitor.parse_tcp("redis.local:6379")
        self.assertEqual(tcp[0].host, "redis.local")


class TestParseDiskOverrides(unittest.TestCase):
    def test_basic(self):
        d = monitor.parse_disk_overrides("/var|80\n/var/lib/docker|75")
        self.assertEqual(d, {"/var": 80, "/var/lib/docker": 75})

    def test_ignora_invalido(self):
        d = monitor.parse_disk_overrides("/var|abc\n/|85")
        self.assertEqual(d, {"/": 85})

    def test_empty(self):
        self.assertEqual(monitor.parse_disk_overrides(""), {})


if __name__ == "__main__":
    unittest.main()
