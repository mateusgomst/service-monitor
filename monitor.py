#!/usr/bin/env python3
"""Service Monitor v2 — verifica serviços systemd, sites HTTP/HTTPS, portas TCP
e recursos (memória, disco). Envia alertas no Telegram com anti-spam estrutural:
lockfile (só 1 execução por vez), confirmação dupla (CONFIRM_FAILURES) e
batching de notificações na mesma execução.
"""

import argparse
import fcntl
import json
import os
import shlex
import socket
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "config.env"
STATE_DIR = SCRIPT_DIR / "state"
STATE_FILE = STATE_DIR / "monitor.json"
NOTIFY_LOG = STATE_DIR / "notify.log.jsonl"
LOCK_FILE = STATE_DIR / "monitor.lock"
LOG_FILE = SCRIPT_DIR / "monitor.log"


def log(msg: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"[{ts}] {msg}\n")
    except OSError:
        pass


# ============================================================================
# Config
# ============================================================================

def load_config(path: Path) -> dict:
    """Source config.env via bash para preservar sintaxe shell ($(hostname), etc.)."""
    if not path.is_file():
        print(
            f"ERRO: {path} não encontrado. Copie config.env.example para config.env.",
            file=sys.stderr,
        )
        sys.exit(1)
    result = subprocess.run(
        ["bash", "-c", f"set -a && . {shlex.quote(str(path))} && env -0"],
        capture_output=True,
        text=True,
        check=True,
    )
    env = {}
    for entry in result.stdout.split("\0"):
        if "=" in entry:
            k, v = entry.split("=", 1)
            env[k] = v
    return env


def _parse_lines(raw: str) -> list[str]:
    if not raw:
        return []
    out = []
    for line in raw.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


@dataclass
class SiteSpec:
    url: str
    expected_code: int = 200
    body_contains: Optional[str] = None

    @property
    def key(self) -> str:
        return f"http:{self.url}"


@dataclass
class TcpSpec:
    host: str
    port: int

    @property
    def key(self) -> str:
        return f"tcp:{self.host}:{self.port}"


def parse_sites(raw: str) -> list[SiteSpec]:
    out = []
    for line in _parse_lines(raw):
        parts = [p.strip() for p in line.split("|")]
        url = parts[0]
        if not url:
            continue
        try:
            code = int(parts[1]) if len(parts) > 1 and parts[1] else 200
        except ValueError:
            log(f"WARN: SITES — código inválido em {line!r}, usando 200")
            code = 200
        body = parts[2] if len(parts) > 2 and parts[2] else None
        out.append(SiteSpec(url=url, expected_code=code, body_contains=body))
    return out


def parse_tcp(raw: str) -> list[TcpSpec]:
    out = []
    for line in _parse_lines(raw):
        host, _, port = line.rpartition(":")
        if not host or not port.isdigit():
            log(f"WARN: TCP_CHECKS linha inválida: {line!r}")
            continue
        out.append(TcpSpec(host=host, port=int(port)))
    return out


def parse_disk_overrides(raw: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for line in _parse_lines(raw):
        if "|" not in line:
            continue
        mount, thresh = line.split("|", 1)
        try:
            out[mount.strip()] = int(thresh.strip())
        except ValueError:
            log(f"WARN: DISK_OVERRIDES linha inválida: {line!r}")
    return out


# ============================================================================
# State (JSON em disco)
# ============================================================================

@dataclass
class CheckEntry:
    status: str = "up"  # 'up' | 'pending_down' | 'down'
    pending_count: int = 0
    down_since: Optional[int] = None
    last_alert_at: Optional[int] = None
    last_message: Optional[str] = None


class State:
    def __init__(self, path: Path, notify_log_path: Path):
        self.path = path
        self.notify_log_path = notify_log_path
        self.data: dict[str, dict] = {}
        if path.is_file():
            try:
                self.data = json.loads(path.read_text() or "{}")
            except json.JSONDecodeError:
                log(f"WARN: state corrompido em {path}, recriando")
                self.data = {}

    def get(self, key: str) -> CheckEntry:
        d = self.data.get(key, {})
        return CheckEntry(
            status=d.get("status", "up"),
            pending_count=d.get("pending_count", 0),
            down_since=d.get("down_since"),
            last_alert_at=d.get("last_alert_at"),
            last_message=d.get("last_message"),
        )

    def set(self, key: str, entry: CheckEntry) -> None:
        self.data[key] = {
            "status": entry.status,
            "pending_count": entry.pending_count,
            "down_since": entry.down_since,
            "last_alert_at": entry.last_alert_at,
            "last_message": entry.last_message,
        }

    def remove(self, key: str) -> None:
        self.data.pop(key, None)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False))
        os.replace(tmp, self.path)

    def append_notify(self, key: str, kind: str, message: str) -> None:
        self.notify_log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"ts": int(time.time()), "key": key, "kind": kind, "message": message}
        with open(self.notify_log_path, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ============================================================================
# Alerter (Telegram + batching)
# ============================================================================

class Alerter:
    def __init__(self, token: str, chat_id: str, server_name: str, state: State):
        self.token = token
        self.chat_id = chat_id
        self.server_name = server_name
        self.state = state
        self.queue: list[tuple[str, str, str]] = []  # (key, kind, full_text)

    def enqueue_down(self, key: str, msg: str) -> None:
        text = f"🔴 *{self.server_name}* — {msg}"
        self.queue.append((key, "down", text))
        log(f"ENQUEUE down [{key}]: {msg}")

    def enqueue_up(self, key: str, msg: str) -> None:
        text = f"✅ *{self.server_name}* — {msg}"
        self.queue.append((key, "up", text))
        log(f"ENQUEUE up [{key}]: {msg}")

    def enqueue_raw(self, key: str, kind: str, text: str) -> None:
        self.queue.append((key, kind, text))

    def flush(self) -> None:
        if not self.queue:
            return
        if len(self.queue) == 1:
            text = self.queue[0][2]
        else:
            text = "\n\n".join(item[2] for item in self.queue)
        self._send(text)
        for key, kind, full_msg in self.queue:
            self.state.append_notify(key, kind, full_msg)
        self.queue.clear()

    def _send(self, text: str) -> None:
        if not self.token or self.token == "cole_o_token_aqui":
            log(f"Telegram não configurado, pulando envio: {text[:120]}")
            return
        url = f"https://api.telegram.org/bot{self.token}/sendMessage"
        data = urllib.parse.urlencode(
            {"chat_id": self.chat_id, "parse_mode": "Markdown", "text": text}
        ).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except Exception as e:
            log(f"Falha enviando telegram: {e}")


# ============================================================================
# Decisão (pending / down / up / re-alerta)
# ============================================================================

def record_failure(
    state: State,
    alerter: Alerter,
    key: str,
    message: str,
    confirm_failures: int,
    realert_minutes: int,
) -> None:
    entry = state.get(key)
    now = int(time.time())
    if entry.status == "up":
        if confirm_failures <= 1:
            entry.status = "down"
            entry.pending_count = 0
            entry.down_since = now
            entry.last_alert_at = now
            entry.last_message = message
            alerter.enqueue_down(key, message)
        else:
            entry.status = "pending_down"
            entry.pending_count = 1
            entry.last_message = message
            log(f"PENDING [{key}] 1/{confirm_failures}: {message}")
    elif entry.status == "pending_down":
        entry.pending_count += 1
        entry.last_message = message
        if entry.pending_count >= confirm_failures:
            entry.status = "down"
            entry.down_since = now
            entry.last_alert_at = now
            alerter.enqueue_down(key, message)
        else:
            log(f"PENDING [{key}] {entry.pending_count}/{confirm_failures}: {message}")
    else:  # down
        entry.last_message = message
        if realert_minutes > 0 and entry.last_alert_at:
            diff_min = (now - entry.last_alert_at) // 60
            if diff_min >= realert_minutes:
                entry.last_alert_at = now
                alerter.enqueue_down(key, f"(ainda caído há {diff_min}min) — {message}")
    state.set(key, entry)


def record_success(state: State, alerter: Alerter, key: str, message: str) -> None:
    entry = state.get(key)
    if entry.status == "up":
        return
    if entry.status == "pending_down":
        log(f"RECOVERED-PENDING [{key}] (falso alarme)")
        state.remove(key)
        return
    alerter.enqueue_up(key, message)
    state.remove(key)


# ============================================================================
# Checks
# ============================================================================

@dataclass
class CheckResult:
    key: str
    ok: bool
    message: str


def check_http(spec: SiteSpec, timeout: int, verify_tls: bool) -> CheckResult:
    ctx = None
    if not verify_tls:
        ctx = ssl._create_unverified_context()
    code: Optional[int] = None
    body = ""
    try:
        req = urllib.request.Request(
            spec.url, headers={"User-Agent": "service-monitor/2"}
        )
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            code = resp.status
            if spec.body_contains:
                body = resp.read(65536).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        code = e.code
        if spec.body_contains:
            try:
                body = e.read(65536).decode("utf-8", errors="replace")
            except Exception:
                body = ""
    except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
        return CheckResult(
            spec.key, False, f"site `{spec.url}` erro de conexão: {e}"
        )

    if code != spec.expected_code:
        return CheckResult(
            spec.key,
            False,
            f"site `{spec.url}` HTTP {code} (esperado {spec.expected_code})",
        )
    if spec.body_contains and spec.body_contains not in body:
        return CheckResult(
            spec.key,
            False,
            f"site `{spec.url}` body não contém `{spec.body_contains}`",
        )
    return CheckResult(spec.key, True, f"site `{spec.url}` voltou — HTTP {code}")


def check_tcp(spec: TcpSpec, timeout: int) -> CheckResult:
    try:
        with socket.create_connection((spec.host, spec.port), timeout=timeout):
            pass
    except (OSError, socket.timeout) as e:
        return CheckResult(
            spec.key, False, f"TCP `{spec.host}:{spec.port}` falhou: {e}"
        )
    return CheckResult(spec.key, True, f"TCP `{spec.host}:{spec.port}` voltou")


def check_service(name: str) -> CheckResult:
    key = f"svc:{name}"
    try:
        result = subprocess.run(
            ["systemctl", "is-active", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
        status = result.stdout.strip() or "unknown"
    except subprocess.TimeoutExpired:
        return CheckResult(key, False, f"serviço `{name}` timeout em systemctl")
    except FileNotFoundError:
        return CheckResult(key, False, f"serviço `{name}` systemctl não disponível")
    if status == "active":
        return CheckResult(key, True, f"serviço `{name}` voltou a rodar")
    return CheckResult(key, False, f"serviço `{name}` está *{status}*")


def check_memory(threshold: int) -> CheckResult:
    key = "mem"
    meminfo: dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                meminfo[k.strip()] = int(v.strip().split()[0])
    except OSError:
        return CheckResult(key, True, "memória: /proc/meminfo indisponível")
    total = meminfo.get("MemTotal", 0)
    avail = meminfo.get("MemAvailable", 0)
    if total == 0:
        return CheckResult(key, True, "memória desconhecida")
    used_pct = round(100 * (total - avail) / total)
    if used_pct >= threshold:
        top = "(falha ao listar processos)"
        try:
            ps = subprocess.run(
                ["ps", "-eo", "pid,user,%mem,comm", "--sort=-%mem"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            lines = ps.stdout.splitlines()[1:6]
            top = "\n".join(l.strip() for l in lines)
        except Exception:
            pass
        msg = (
            f"memória em *{used_pct}%* (limite {threshold}%)\n"
            f"Top processos:\n```\n{top}\n```"
        )
        return CheckResult(key, False, msg)
    return CheckResult(key, True, f"memória normalizou ({used_pct}%)")


def check_disks(
    default_threshold: int,
    overrides: dict[str, int],
    ignore: list[str],
) -> list[CheckResult]:
    try:
        df = subprocess.run(
            ["df", "--output=target,pcent"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    results: list[CheckResult] = []
    for line in df.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        mount, pct = parts[0], parts[1].rstrip("%")
        if any(mount == p or mount.startswith(p.rstrip("/") + "/") for p in ignore):
            continue
        try:
            p = int(pct)
        except ValueError:
            continue
        threshold = overrides.get(mount, default_threshold)
        key = f"disk:{mount}"
        if p >= threshold:
            results.append(
                CheckResult(
                    key,
                    False,
                    f"disco `{mount}` em *{p}%* (limite {threshold}%)",
                )
            )
        else:
            results.append(
                CheckResult(key, True, f"disco `{mount}` normalizou ({p}%)")
            )
    return results


# ============================================================================
# Lockfile
# ============================================================================

class LockBusy(Exception):
    pass


def acquire_lock(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        raise LockBusy()
    pid_str = f"{os.getpid()}\n".encode()
    os.ftruncate(fd, 0)
    os.write(fd, pid_str)
    return fd


def release_lock(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass
    os.close(fd)


# ============================================================================
# Comandos
# ============================================================================

def cmd_run(cfg: dict) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        lock_fd = acquire_lock(LOCK_FILE)
    except LockBusy:
        log("skipped: already running (lock busy)")
        return 0

    try:
        state = State(STATE_FILE, NOTIFY_LOG)
        alerter = Alerter(
            token=cfg.get("TELEGRAM_BOT_TOKEN", ""),
            chat_id=cfg.get("TELEGRAM_CHAT_ID", ""),
            server_name=cfg.get("SERVER_NAME", "server"),
            state=state,
        )
        confirm = max(1, int(cfg.get("CONFIRM_FAILURES", "2") or 2))
        realert = int(cfg.get("REALERT_MINUTES", "0") or 0)
        timeout = int(cfg.get("HTTP_TIMEOUT", "8") or 8)
        verify_tls = cfg.get("HTTP_VERIFY_TLS", "false").strip().lower() in (
            "true",
            "1",
            "yes",
        )

        results: list[CheckResult] = []

        for svc in (cfg.get("SERVICES", "") or "").split():
            results.append(check_service(svc))

        for spec in parse_sites(cfg.get("SITES", "")):
            results.append(check_http(spec, timeout=timeout, verify_tls=verify_tls))

        for spec in parse_tcp(cfg.get("TCP_CHECKS", "")):
            results.append(check_tcp(spec, timeout=timeout))

        results.append(check_memory(int(cfg.get("MEM_THRESHOLD", "85") or 85)))

        disk_threshold = int(cfg.get("DISK_THRESHOLD", "85") or 85)
        overrides = parse_disk_overrides(cfg.get("DISK_OVERRIDES", ""))
        ignore_raw = cfg.get("DISK_IGNORE", "") or "/snap /boot/efi /run /dev /sys /proc /run/user"
        ignore = ignore_raw.split()
        results.extend(check_disks(disk_threshold, overrides, ignore))

        for r in results:
            if r.ok:
                record_success(state, alerter, r.key, r.message)
            else:
                record_failure(state, alerter, r.key, r.message, confirm, realert)

        state.save()
        alerter.flush()
        return 0
    finally:
        release_lock(lock_fd)


def cmd_test(cfg: dict) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    state = State(STATE_FILE, NOTIFY_LOG)
    alerter = Alerter(
        token=cfg.get("TELEGRAM_BOT_TOKEN", ""),
        chat_id=cfg.get("TELEGRAM_CHAT_ID", ""),
        server_name=cfg.get("SERVER_NAME", "server"),
        state=state,
    )
    server = cfg.get("SERVER_NAME", "server")
    text = (
        f"🧪 Teste do service-monitor em *{server}* — "
        f"se você está lendo isso, o Telegram está OK."
    )
    alerter.enqueue_raw("test", "up", text)
    alerter.flush()
    return 0


def cmd_list(cfg: dict) -> int:
    print("Serviços systemd:")
    services = (cfg.get("SERVICES", "") or "").split()
    if not services:
        print("  (nenhum)")
    for svc in services:
        print(f"  - {svc}")

    print("\nSites HTTP:")
    sites = parse_sites(cfg.get("SITES", ""))
    if not sites:
        print("  (nenhum)")
    for spec in sites:
        extra = f" expected={spec.expected_code}"
        if spec.body_contains:
            extra += f" contains={spec.body_contains!r}"
        print(f"  - {spec.url}{extra}")

    print("\nTCP:")
    tcp = parse_tcp(cfg.get("TCP_CHECKS", ""))
    if not tcp:
        print("  (nenhum)")
    for spec in tcp:
        print(f"  - {spec.host}:{spec.port}")

    print("\nDisco:")
    print(f"  default: {cfg.get('DISK_THRESHOLD', '85')}%")
    overrides = parse_disk_overrides(cfg.get("DISK_OVERRIDES", ""))
    for m, t in overrides.items():
        print(f"  override {m} → {t}%")
    ignore = (cfg.get("DISK_IGNORE", "") or "/snap /boot/efi /run /dev /sys /proc /run/user").split()
    print(f"  ignore: {' '.join(ignore)}")

    print(f"\nMemória: {cfg.get('MEM_THRESHOLD', '85')}%")
    print(f"\nAnti-spam:")
    print(f"  CONFIRM_FAILURES = {cfg.get('CONFIRM_FAILURES', '2')}")
    print(f"  REALERT_MINUTES  = {cfg.get('REALERT_MINUTES', '0')}")
    return 0


def cmd_reset() -> int:
    if STATE_FILE.exists():
        STATE_FILE.unlink()
    print("Estado limpo.")
    return 0


def cmd_history(n: int) -> int:
    if not NOTIFY_LOG.exists():
        print("(sem histórico)")
        return 0
    lines = NOTIFY_LOG.read_text().splitlines()[-n:]
    for line in lines:
        try:
            d = json.loads(line)
            ts = datetime.fromtimestamp(d["ts"]).strftime("%Y-%m-%d %H:%M:%S")
            kind = d.get("kind", "?").upper()
            print(f"[{ts}] {kind:4} {d.get('key',''):40} {d.get('message','')}")
        except Exception:
            print(line)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="monitor.py")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="executa todas as checagens (default)")
    sub.add_parser("test", help="envia mensagem de teste no Telegram")
    sub.add_parser("list", help="lista o que será verificado")
    sub.add_parser("reset", help="limpa o estado")
    h = sub.add_parser("history", help="histórico de notificações")
    h.add_argument("-n", type=int, default=20, help="quantidade de linhas")
    args = parser.parse_args()

    cmd = args.cmd or "run"
    if cmd == "reset":
        sys.exit(cmd_reset())

    cfg = load_config(CONFIG_FILE)

    if cmd == "run":
        sys.exit(cmd_run(cfg))
    elif cmd == "test":
        sys.exit(cmd_test(cfg))
    elif cmd == "list":
        sys.exit(cmd_list(cfg))
    elif cmd == "history":
        sys.exit(cmd_history(args.n))


if __name__ == "__main__":
    main()
