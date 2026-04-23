# Hikvision LAN agent (`hikvision-sadp-proxy`)

On‑prem süreç: **yalnızca RabbitMQ** üzerinden Go API ile konuşur; **HTTP sunucusu yok**. LAN’da SADP (UDP 37020) ile cihaz tarar, `isapi` aksiyonunda herhangi bir `/ISAPI/…` yolunu Digest ile cihaza iletir.

## Gereksinimler

- Python 3.10+
- RabbitMQ (Go `HIKVISION_GATEWAY_AMQP_URL` ile aynı)
- Agent’ın çalıştığı makine, Hikvision cihazlarıyla aynı yayın domain’inde (SADP router geçmez)

## Kurulum

```bash
cd cmd/hikvision-sadp-proxy
cp env.example .env
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python main.py
```

## Docker (tek komut)

Bu dizinde:

```bash
docker compose up -d --build
```

Ayağa kalkanlar:

| Servis | Açıklama |
|--------|----------|
| **rabbitmq** | AMQP `5672`, yönetim arayüzü [http://localhost:15672](http://localhost:15672) — kullanıcı/şifre: `guest` / `guest` |
| **hikvision-agent** | `HIKVISION_GATEWAY_AMQP_URL=amqp://guest:guest@rabbitmq:5672/` ile aynı compose ağındaki broker’a bağlanır |

İsteğe bağlı (shell’de export veya `.env` dosyası compose ile otomatik okunur — proje kökü değil **bu dizinde** `.env`):

```bash
export HIKVISION_DEVICE_PASSWORD='cihaz-admin-şifresi'
docker compose up -d --build
```

### Go API’de kullanacağın URI (`HIKVISION_GATEWAY_AMQP_URL`)

Compose **5672 portunu makineye yayınladığı** için AccessDonkey’i Docker **dışında** (localhost’ta) çalıştırıyorsan `.env` içine şunu yaz:

```text
HIKVISION_GATEWAY_AMQP_URL=amqp://guest:guest@127.0.0.1:5672/
```

Özel kullanıcı/şifre tanımladıysan:

```text
HIKVISION_GATEWAY_AMQP_URL=amqp://KULLANICI:SIFRE@127.0.0.1:5672/
```

(Special characters in password must be URL-encoded, e.g. `@` → `%40`.)

Go API **aynı compose içinde** başka bir servis olarak çalışıyorsa, broker hostname’i `rabbitmq` olur:

```text
HIKVISION_GATEWAY_AMQP_URL=amqp://guest:guest@rabbitmq:5672/
```

Özet: URI = `amqp://` + broker kullanıcı + `:` + şifre + `@` + **Go sürecinin broker’ı gördüğü host** + `:5672/` + (isteğe bağlı sanal host, genelde boş).

### SADP (UDP keşif) uyarısı

Bridge ağındaki konteynerde multicast bazen **tüm LAN’ı göremez** (özellikle Docker Desktop). Keşif boş dönüyorsa agent’ı LAN’daki bir Linux host’ta doğrudan `python main.py` veya `network_mode: host` ile çalıştırmayı düşünün; broker yine bu compose’daki Rabbit olabilir (`amqp://guest:guest@HOST_IP:5672/`).

## Ortam değişkenleri

| Değişken | Zorunlu | Açıklama |
|----------|---------|----------|
| `HIKVISION_GATEWAY_AMQP_URL` | Evet | AMQP URI (broker) |
| `HIKVISION_GATEWAY_ORG_ID` | Evet | Bu agent’ın kuruluşu (Go/JWT `organization_id` string’i); kuyruk: `hikvision.gateway.requests.<sanitize(org)>` |
| `HIKVISION_DEVICE_USERNAME` | Hayır | RPC’de user yoksa varsayılan Digest kullanıcısı |
| `HIKVISION_DEVICE_PASSWORD` | Hayır | RPC’de şifre boşsa varsayılan şifre |
| `HIKVISION_ISAPI_INSECURE_SKIP_VERIFY` | Hayır | Varsayılan `true` (kendinden imzalı TLS) |
| `HIK_SADP_STARTUP_TIMEOUT` | Hayır | İlk SADP tarama süresi (sn), varsayılan 3 |
| `DEBUG` | Hayır | `true` → ayrıntılı log |

Go tarafı ISAPI isteğinde `username` / `password` gönderirse bunlar önceliklidir; şifre boş bırakılırsa agent `.env` şifresine düşer.

## RPC aksiyonları

`ping`, `discover`, `devices`, `isapi` — JSON şeması: `docs/RABBIT_HIKVISION_GATEWAY_KILAVUZU.md` ve `docs/postman/Hikvision_Gateway_RabbitMQ.postman_collection.json`.

`isapi` ile cihazın desteklediği tüm ISAPI uçları kullanılabilir (yol + method + isteğe bağlı `body_b64`).
