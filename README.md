# Service Monitor

Script simples em Bash que monitora os serviços críticos do servidor e os sites configurados no nginx, mandando alerta no Telegram quando algo cai (e quando volta).

## O que ele verifica

1. **Serviços systemd**: `nginx`, `redis-server`, `postgresql`, `supervisor` (configurável)
2. **Sites do nginx**: lê os arquivos em `/etc/nginx/sites-enabled/`, extrai cada `server_name` e faz uma requisição HTTP/HTTPS pra ele (usando `--resolve` pra forçar `127.0.0.1`, então funciona com domínios `.local`). Se nginx + backend (php-fpm, artisan serve, proxy, etc.) estiverem OK, retorna 2xx/3xx. Se o backend caiu, geralmente vem 502/504 e o alerta dispara.
3. **Memória RAM**: alerta se uso passar do limite (padrão 85%)
4. **Disco**: alerta por partição se passar do limite (padrão 85%)

Implementa **anti-spam**: ele só avisa quando o estado muda (caiu → 🔴, voltou → ✅). Opcionalmente re-alerta a cada N minutos se continuar caído.

## Instalação

### 1. Criar o bot do Telegram

1. Fale com [@BotFather](https://t.me/BotFather), comando `/newbot`, escolha um nome.
2. Ele te dá um **token** (`123456:ABC...`).
3. Pegue seu **chat_id**: mande qualquer mensagem para o bot, depois abra no navegador:
   ```
   https://api.telegram.org/bot<SEU_TOKEN>/getUpdates
   ```
   Procure por `"chat":{"id": ...}`. Para grupo, adicione o bot no grupo e mande uma mensagem lá — o `id` virá negativo.

### 2. Configurar

```bash
cd /var/www/service-monitor
cp config.env.example config.env
nano config.env   # cole token, chat_id e ajuste a lista de serviços
chmod +x monitor.sh
```

### 3. Testar

```bash
# Manda uma mensagem de teste no Telegram
./monitor.sh test

# Mostra os sites que ele detectou no nginx (útil pra depurar)
./monitor.sh sites

# Roda uma verificação completa agora
./monitor.sh run

# Vê o log
tail -f monitor.log
```

### 4. Agendar (cron)

A cada 2 minutos é um bom ponto de partida. Como root:

```bash
sudo crontab -e
```

Adicione:

```
*/2 * * * * /var/www/service-monitor/monitor.sh run >/dev/null 2>&1
```

> Roda como root pra ter permissão de ler `systemctl` e os configs do nginx. Se preferir um usuário dedicado, garanta que ele esteja em sudoers para `systemctl is-active`.

### 5. (Opcional) Rodar via systemd timer em vez de cron

Cria `/etc/systemd/system/service-monitor.service`:

```ini
[Unit]
Description=Service Monitor

[Service]
Type=oneshot
ExecStart=/var/www/service-monitor/monitor.sh run
```

E `/etc/systemd/system/service-monitor.timer`:

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

## Comandos úteis

```bash
./monitor.sh run     # executa todas as checagens (usado pelo cron)
./monitor.sh test    # manda mensagem de teste no Telegram
./monitor.sh sites   # lista os sites detectados do nginx
./monitor.sh reset   # limpa o estado (todos os "flag" de caído)
```

## Arquivos

```
service-monitor/
├── monitor.sh             # script principal
├── config.env.example     # exemplo de config (commit OK)
├── config.env             # config real com token (NÃO commitar)
├── monitor.log            # log das execuções
├── state/                 # flags de quem está caído (auto-gerenciado)
└── README.md
```

## Customizando

Edite `config.env`:

- `SERVICES="nginx redis-server postgresql supervisor mysql"` — adiciona/remove serviços
- `IGNORE_SITES="default _ localhost api.algumacoisa.com"` — ignora sites que não dá pra resolver localmente
- `MEM_THRESHOLD=90` — sobe o limite da memória
- `REALERT_MINUTES=60` — manda lembrete a cada hora se algo continuar caído (0 desativa)

## Observações importantes

- **`php artisan serve` em produção é frágil** — single-thread, cai com qualquer exception não tratada. Recomendado rodar via **systemd unit com `Restart=always`** ou **Supervisor** pra ele se auto-recuperar. O monitor avisa, mas o ideal é o app voltar sozinho.
- **Causa raiz de memória cheia**: o monitor avisa quando passa do limite e mostra os top 5 processos no alerta. Se for recorrente, investigue workers do Laravel (`queue:work` sem `--max-jobs`), logs gigantes em `storage/logs/`, ou o próprio MySQL/PG mal configurado.
- **HTTPS com certificado self-signed**: o script já usa `curl -k` (ignora cert), funciona pros `.local`.
- **Códigos HTTP aceitos como "OK"**: 2xx, 3xx, 401, 403, 405. Se algum dos seus sites retornar outro código em estado saudável (ex: 404 na raiz), ajuste a regex no `monitor.sh` na função `check_nginx_sites`.
