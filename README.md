# Service Monitor

Monitor em Python (stdlib, zero dependências) que verifica serviços systemd, sites HTTP/HTTPS, portas TCP, memória e disco. Manda alerta no Telegram quando algo cai e quando volta, com **anti-spam estrutural**: lockfile (só 1 execução por vez), confirmação dupla (não alerta em blip de 1 ciclo) e batching de notificações por execução.

## O que ele verifica

1. **Serviços systemd** — `systemctl is-active <svc>` para cada nome em `SERVICES`.
2. **Sites HTTP/HTTPS** — uma URL por linha em `SITES`. Suporta código esperado customizado e match de substring no body.
3. **Portas TCP** — `host:porta` por linha em `TCP_CHECKS`. Útil pra Redis, Postgres, MySQL.
4. **Memória RAM** — alerta se uso passar do limite (default 85%).
5. **Disco** — por mount, com threshold default e overrides individuais.

## Por que não auto-discover do nginx?

A versão anterior em Bash lia `/etc/nginx/sites-enabled/` e forçava `--resolve 127.0.0.1`. Isso causava **falsos positivos** em sites cujo backend não responde por IP local (proxies reversos, containers, upstreams remotos). Esta versão usa **lista explícita** em `SITES` — você diz exatamente o que monitorar, e o `curl`/`urllib` resolve normalmente via DNS/`/etc/hosts`.

## Instalação

### 1. Criar o bot do Telegram

1. Fale com [@BotFather](https://t.me/BotFather), comando `/newbot`, escolha um nome.
2. Ele te dá um **token** (`123456:ABC...`).
3. Pegue seu **chat_id**: mande uma mensagem para o bot e abra:
   ```
   https://api.telegram.org/bot<SEU_TOKEN>/getUpdates
   ```
   Procure por `"chat":{"id": ...}`. Para grupo, adicione o bot, mande uma mensagem e o `id` virá negativo.

### 2. Configurar

```bash
cd /var/www/service-monitor
cp config.env.example config.env
chmod 600 config.env       # token sensível
nano config.env            # cole token, chat_id e ajuste as listas
chmod +x monitor.py
```

### 3. Testar

```bash
./monitor.py test          # mensagem de teste no Telegram
./monitor.py list          # mostra tudo que será verificado
./monitor.py run           # executa uma vez
./monitor.py history -n 20 # últimas notificações
tail -f monitor.log        # log das execuções
```

### 4. Agendar (cron)

A cada 2 minutos é um bom ponto de partida. Como root:

```
*/2 * * * * /var/www/service-monitor/monitor.py run >/dev/null 2>&1
```

> **Atenção**: NÃO ative cron E systemd timer ao mesmo tempo. O lockfile evita corrupção mas você desperdiça ciclos. Escolha um.

### 5. (Opcional) systemd timer em vez de cron

`/etc/systemd/system/service-monitor.service`:

```ini
[Unit]
Description=Service Monitor

[Service]
Type=oneshot
ExecStart=/var/www/service-monitor/monitor.py run
```

`/etc/systemd/system/service-monitor.timer`:

```ini
[Unit]
Description=Roda service-monitor a cada 2min

[Timer]
OnBootSec=2min
OnUnitActiveSec=2min

[Install]
WantedBy=timers.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now service-monitor.timer
```

## Configurando o que monitorar

Tudo em `config.env`. Sintaxe shell normal.

### Serviços systemd

```bash
SERVICES="nginx redis-server postgresql supervisor"
```

### Sites HTTP/HTTPS

Uma URL por linha em `SITES`. Sintaxe:

```
URL
URL | codigo_esperado
URL | codigo_esperado | substring_que_deve_aparecer_no_body
```

Exemplos:

```bash
SITES="
https://toshop.com.br
https://monit.sysout.com.br:445 | 200
https://api.exemplo.com/health | 200 | \"status\":\"ok\"
https://endpoint-com-auth.com | 401
"
```

- **Sem `--resolve 127.0.0.1`** — se o site usa DNS local, configure no `/etc/hosts` da máquina (o próprio nginx do servidor também precisa disso).
- **Redirects são seguidos** — uma URL que dá 302 para um 200 conta como OK.
- **Body match** reduz falso positivo: se a página de manutenção retorna 200, declare `| 200 | "expressão única do app saudável"`.

### Portas TCP

`host:porta` por linha:

```bash
TCP_CHECKS="
127.0.0.1:6379
127.0.0.1:5432
db.interno.local:3306
"
```

### Disco

```bash
DISK_THRESHOLD=85
DISK_OVERRIDES="
/var|80
/var/lib/docker|75
"
DISK_IGNORE="/snap /boot/efi /run /dev /sys /proc /run/user"
```

`DISK_OVERRIDES` sobrescreve o threshold por mount; mounts não listados usam `DISK_THRESHOLD`. `DISK_IGNORE` recebe prefixos (mounts começando com qualquer um deles são pulados).

### Memória

```bash
MEM_THRESHOLD=85
```

### Anti-spam

```bash
# Quantas falhas consecutivas antes de notificar DOWN. 2 = ignora blip de 1 ciclo.
CONFIRM_FAILURES=2

# Re-alertar a cada N minutos se continuar caído (0 desativa).
REALERT_MINUTES=60
```

### TLS

```bash
# Validar certificado TLS. "false" mantém o comportamento do script antigo (curl -k).
HTTP_VERIFY_TLS=false
```

## Como funciona o anti-spam

Três camadas, redundantes de propósito:

1. **Lockfile (`state/monitor.lock`)** — `fcntl.flock` exclusivo. Se uma execução já está rodando (cron concorrente, systemd timer + cron juntos, etc.), a segunda **sai silenciosamente** com `skipped: already running` no log. Resolve o caso clássico de receber 3 notificações no mesmo segundo.
2. **Confirmação dupla (`CONFIRM_FAILURES`)** — antes de notificar DOWN, o check precisa falhar N execuções consecutivas. Estado intermediário `pending_down` no `state/monitor.json` com contador. Um blip de rede de 1 ciclo nunca alerta.
3. **Batching por execução** — todas as notificações de uma execução são enviadas numa única mensagem do Telegram. Mesmo que 5 coisas caiam ao mesmo tempo, você recebe 1 mensagem com 5 linhas, não 5 mensagens.

## Comandos

```bash
./monitor.py run           # checagens (default; usado pelo cron)
./monitor.py test          # mensagem de teste no Telegram
./monitor.py list          # mostra tudo que será verificado
./monitor.py reset         # limpa o estado (zera tudo)
./monitor.py history -n 50 # últimas N notificações
```

## Arquivos

```
service-monitor/
├── monitor.py             # script principal
├── config.env.example     # exemplo (commit OK)
├── config.env             # config real com token (NÃO commitar)
├── monitor.log            # log das execuções
├── state/
│   ├── monitor.json       # estado atual de cada check
│   ├── notify.log.jsonl   # histórico append-only de notificações
│   └── monitor.lock       # lockfile (fcntl.flock)
├── tests/                 # python3 -m unittest discover tests/
└── README.md
```

## Testes

```bash
python3 -m unittest discover tests/
```

31 testes cobrindo parser de config, lógica de pending_down/recovery/re-alerta, checks HTTP/TCP (com servidor local de teste) e exclusão mútua do lockfile.

## Migração do script Bash antigo

O `monitor.sh` original ainda está no repo. Para rodar em paralelo durante o cutover:

1. Mantenha o cron atual do Bash em `*/2 1-59 * * *`.
2. Adicione o Python em `*/2 0-58 * * *` (offset de 1 min — eles não rodam no mesmo minuto).
3. Compare `monitor.log` (Bash) com `state/notify.log.jsonl` (Python) por 1-2 dias.
4. Quando confiar, remova o cron do Bash. Mantenha `monitor.sh` no repo por 2 semanas como fallback.

## Observações

- **`php artisan serve` em produção é frágil** — single-thread, cai com qualquer exception. Rode via systemd unit com `Restart=always` ou Supervisor. O monitor avisa, mas o ideal é o app voltar sozinho.
- **Causa raiz de memória cheia**: o alerta mostra os 5 top processos. Recorrência costuma ser workers Laravel (`queue:work` sem `--max-jobs`), logs gigantes em `storage/logs/`, ou MySQL/PG mal tunado.
- **Chave órfã no state**: se você remover um serviço do `config.env` enquanto ele estava DOWN, a chave fica no `state/monitor.json` mas não é mais checada. Rode `./monitor.py reset` quando reorganizar a config.
