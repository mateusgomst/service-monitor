import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monitor  # noqa: E402


class StubAlerter:
    """Captura notificações sem mandar Telegram."""

    def __init__(self):
        self.queue: list[tuple[str, str, str]] = []

    def enqueue_down(self, key, msg):
        self.queue.append((key, "down", msg))

    def enqueue_up(self, key, msg):
        self.queue.append((key, "up", msg))

    def flush(self):
        self.queue.clear()


def fresh_state() -> monitor.State:
    tmp = Path(tempfile.mkdtemp())
    return monitor.State(tmp / "state.json", tmp / "notify.jsonl")


class TestPendingDown(unittest.TestCase):
    def test_primeira_falha_nao_notifica(self):
        state = fresh_state()
        alerter = StubAlerter()
        monitor.record_failure(state, alerter, "svc:x", "falhou", confirm_failures=2, realert_minutes=0)
        self.assertEqual(alerter.queue, [])
        self.assertEqual(state.get("svc:x").status, "pending_down")
        self.assertEqual(state.get("svc:x").pending_count, 1)

    def test_segunda_falha_consecutiva_notifica(self):
        state = fresh_state()
        alerter = StubAlerter()
        monitor.record_failure(state, alerter, "svc:x", "falhou 1", confirm_failures=2, realert_minutes=0)
        monitor.record_failure(state, alerter, "svc:x", "falhou 2", confirm_failures=2, realert_minutes=0)
        self.assertEqual(len(alerter.queue), 1)
        self.assertEqual(alerter.queue[0][0], "svc:x")
        self.assertEqual(alerter.queue[0][1], "down")
        self.assertEqual(state.get("svc:x").status, "down")

    def test_confirm_failures_1_notifica_imediato(self):
        state = fresh_state()
        alerter = StubAlerter()
        monitor.record_failure(state, alerter, "svc:x", "falhou", confirm_failures=1, realert_minutes=0)
        self.assertEqual(len(alerter.queue), 1)
        self.assertEqual(state.get("svc:x").status, "down")

    def test_sucesso_apos_pending_e_falso_alarme(self):
        state = fresh_state()
        alerter = StubAlerter()
        monitor.record_failure(state, alerter, "svc:x", "blip", confirm_failures=2, realert_minutes=0)
        monitor.record_success(state, alerter, "svc:x", "voltou")
        self.assertEqual(alerter.queue, [])  # nem down nem up — silencioso
        self.assertEqual(state.get("svc:x").status, "up")

    def test_recovery_apos_down_notifica(self):
        state = fresh_state()
        alerter = StubAlerter()
        for _ in range(2):
            monitor.record_failure(state, alerter, "svc:x", "fail", confirm_failures=2, realert_minutes=0)
        alerter.queue.clear()
        monitor.record_success(state, alerter, "svc:x", "voltou")
        self.assertEqual(len(alerter.queue), 1)
        self.assertEqual(alerter.queue[0][1], "up")
        self.assertEqual(state.get("svc:x").status, "up")


class TestRealert(unittest.TestCase):
    def test_realert_zero_nao_realerta(self):
        state = fresh_state()
        alerter = StubAlerter()
        monitor.record_failure(state, alerter, "k", "f", confirm_failures=1, realert_minutes=0)
        alerter.queue.clear()
        monitor.record_failure(state, alerter, "k", "f", confirm_failures=1, realert_minutes=0)
        self.assertEqual(alerter.queue, [])

    def test_realert_dispara_apos_tempo(self):
        state = fresh_state()
        alerter = StubAlerter()
        monitor.record_failure(state, alerter, "k", "f", confirm_failures=1, realert_minutes=60)
        alerter.queue.clear()
        entry = state.get("k")
        entry.last_alert_at = int(time.time()) - 70 * 60  # 70 min atrás
        state.set("k", entry)
        monitor.record_failure(state, alerter, "k", "f", confirm_failures=1, realert_minutes=60)
        self.assertEqual(len(alerter.queue), 1)
        self.assertIn("ainda caído", alerter.queue[0][2])


class TestStatePersistence(unittest.TestCase):
    def test_save_e_reload(self):
        tmp = Path(tempfile.mkdtemp())
        s1 = monitor.State(tmp / "s.json", tmp / "n.jsonl")
        e = monitor.CheckEntry(status="down", down_since=123, last_alert_at=456, last_message="x")
        s1.set("k", e)
        s1.save()
        s2 = monitor.State(tmp / "s.json", tmp / "n.jsonl")
        self.assertEqual(s2.get("k").status, "down")
        self.assertEqual(s2.get("k").down_since, 123)

    def test_remove(self):
        state = fresh_state()
        state.set("k", monitor.CheckEntry(status="down"))
        state.remove("k")
        self.assertEqual(state.get("k").status, "up")  # default


if __name__ == "__main__":
    unittest.main()
