# Satloot — Bitcoin testnet3 faucet

A self-hosted **Bitcoin testnet3** faucet: a single Flask app + one HTML template.
Bilingual (EN / 中文), light UI, real on-chain transaction feed, and a serious
anti-abuse stack — with **no third-party dependencies or user accounts**.

> Test coins have no real value. This faucet dispenses **testnet3** coins for
> development and testing only.

## Features

- **9-layer anti-abuse**, all self-contained:
  1. Proof-of-Work challenge (browser burns CPU per claim, difficulty configurable)
  2. Single-use HMAC-signed token with TTL (no replay, must load the page first)
  3. Per-IP cooldown · 4. Per-address cooldown · 5. Per-subnet cap (IPv4 /24, IPv6 /64)
  6. Honeypot field · 7. Recipient-balance cap · 8. Global rate limiter
  9. Automatic temporary ban after repeated abuse
- **Custom claim amount** (up to a configurable max), **daily payout cap** (UTC+8)
- **Bilingual UI** (EN / 中文), light theme, address balance lookup, QR + `bitcoin:` links
- **Live on-chain transaction feed** (via Esplora), recipient balance checker
- **WeChat push alert** (via [PushPlus](https://www.pushplus.plus/)) when the faucet runs dry
- Reliable broadcasting via **Esplora** (`bit`'s own testnet3 broadcasters are dead)

## Quick start

```bash
pip install -r requirements.txt
python app.py          # dev server on 127.0.0.1:8811
```

On first run, `config.json` is auto-generated with a **fresh testnet3 hot wallet**.
The console prints the deposit address — fund it with testnet3 coins, then the
faucet can dispense. In production run it behind gunicorn + a reverse proxy:

```bash
gunicorn -w 2 -b 127.0.0.1:8811 app:app
```

## Configuration

All settings live in `config.json` (see [`config.example.json`](config.example.json)).
Leave `wif` / `hmac_secret` blank to auto-generate. Highlights:

| key | meaning | default |
|---|---|---|
| `payout_sat` / `max_payout_sat` | default / max per claim (sat) | 10000 / 50000 |
| `pow_bits` | PoW difficulty (leading zero bits; +1 doubles cost) | 17 |
| `ip_cooldown_sec` / `address_cooldown_sec` | cooldowns | 86400 / 86400 |
| `daily_global_cap_sat` | daily payout cap (resets 00:00 UTC+8) | 10000000 |
| `pushplus_token` | WeChat push token (optional) | "" |
| `trust_proxy` | read real client IP from `X-Forwarded-For` (behind a proxy) | true |
| `behind_cloudflare` | also trust `CF-Connecting-IP` — **only** enable behind Cloudflare (see below) | false |

## Deployment (reverse proxy)

Run the app on `127.0.0.1:8811` and put nginx / Caddy / Apache in front. HTTPS via
Let's Encrypt or Cloudflare. `deploy.py` is a one-shot nginx + certbot deployer for
reference.

The public [`llms.txt`](llms.txt) describes the live site's purpose and public pages.
Both `deploy.py` and `migrate_to_node.py` copy it to `/opt/btc_faucet/llms.txt`
and serve `/llms.txt` directly through nginx as UTF-8 plain text. Proxied pages
advertise it with a `Link: </llms.txt>; rel="describedby"` response header, and
the HTML template includes the matching discovery link. Keep this manifest
aligned with the public pages when changing the deployment.

The per-IP / per-subnet limits are only as trustworthy as the client IP the app
sees, so make sure your proxy sets it correctly:

- **No Cloudflare** — keep `behind_cloudflare: false` (the default). Your proxy
  should overwrite `X-Forwarded-For` with the real connecting IP (nginx:
  `proxy_set_header X-Forwarded-For $remote_addr;`). Leave the flag off: the app
  ignores `CF-Connecting-IP`, which would otherwise be a client-forgeable header
  and let an attacker dodge the per-IP limit.
- **Behind Cloudflare** — set `behind_cloudflare: true`, and configure nginx's
  `ngx_http_realip_module` (`set_real_ip_from` the Cloudflare ranges +
  `real_ip_header CF-Connecting-IP`) and overwrite the header
  (`proxy_set_header CF-Connecting-IP $remote_addr;`) so it can't be spoofed by
  hitting the origin directly. `deploy.py` wires all of this up for you.

## Security notes

- `config.json` holds the hot-wallet **private key** — it is `.gitignore`d; never commit it.
- This is a **hot wallet**: keep only what you're willing to lose online; **testnet only**.
- All abuse-facing error responses return HTTP 200 with a JSON body so a proxy/CDN
  never masks the real reason.

## Author & attribution

**Satloot** — an original, self-built open-source project.

- Author: **Satloot** · admin@satloot.com
- Live demo: https://faucet.satloot.com
- This is my own original work. You're free to use, fork, and self-host it under
  the MIT license below; a star or a link back is appreciated. Please don't
  claim it as your own.

> 本项目为 **Satloot** 原创、自建的开源比特币 testnet3 水龙头。基于 MIT 协议开源,
> 欢迎自由使用、二次开发与自建部署;请勿声称为你自己的原创作品。作者:Satloot(admin@satloot.com)。

## License

MIT © 2026 Satloot — see [LICENSE](LICENSE).
