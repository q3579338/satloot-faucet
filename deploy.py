#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
One-shot deployer for the Satloot testnet3 faucet.
Uploads app.py + template to the server, installs deps in a venv,
sets up a systemd service (gunicorn on 127.0.0.1:8811) and an nginx
vhost for faucet.satloot.com, then obtains a Let's Encrypt cert.

Usage:
    python deploy.py <server_ip> <root_password> [domain]

Prereqs on your side:
    - DNS A record: faucet.satloot.com  ->  <server_ip>   (set this FIRST)
    - You will fund the faucet hot wallet after first start.
"""
import sys, os, io, paramiko

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE = "/opt/btc_faucet"
DOMAIN_DEFAULT = "faucet.satloot.com"

# Cloudflare edge ranges (https://www.cloudflare.com/ips). nginx only honours the
# CF-Connecting-IP header for requests that actually originate from these — so the
# real-visitor IP cannot be forged by hitting the origin directly.
CF_IPS = [
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22",
    "141.101.64.0/18", "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20",
    "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13",
    "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
    "2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32",
    "2405:8100::/32", "2a06:98c0::/29", "2c0f:f248::/32",
]

def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    host, pw = sys.argv[1], sys.argv[2]
    domain = sys.argv[3] if len(sys.argv) > 3 else DOMAIN_DEFAULT

    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25)
    sftp = c.open_sftp()

    def run(cmd, check=True):
        print("  $", cmd)
        _i, o, e = c.exec_command(cmd, timeout=600)
        out = o.read().decode(errors="replace"); err = e.read().decode(errors="replace")
        code = o.channel.recv_exit_status()
        if out.strip(): print(out.strip())
        if code != 0:
            print("[stderr]", err.strip())
            if check: raise RuntimeError(f"command failed ({code}): {cmd}")
        return out

    def put(local, remote):
        print("  put", local, "->", remote)
        sftp.put(local, remote)

    print("== 1. system packages ==")
    run("apt-get update -y >/dev/null 2>&1; DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip nginx certbot python3-certbot-nginx >/dev/null 2>&1; echo done")

    print("== 2. upload app ==")
    run(f"mkdir -p {REMOTE}/templates")
    put(os.path.join(APP_DIR, "app.py"), f"{REMOTE}/app.py")
    put(os.path.join(APP_DIR, "templates", "index.html"), f"{REMOTE}/templates/index.html")
    put(os.path.join(APP_DIR, "requirements.txt"), f"{REMOTE}/requirements.txt")
    put(os.path.join(APP_DIR, "llms.txt"), f"{REMOTE}/llms.txt")

    print("== 3. python venv + deps ==")
    run(f"cd {REMOTE} && python3 -m venv venv && ./venv/bin/pip install -q --upgrade pip && ./venv/bin/pip install -q -r requirements.txt && echo installed")

    print("== 4. first run to generate config + hot wallet ==")
    # start once to create config.json (auto-generates WIF), then stop
    run(f"cd {REMOTE} && timeout 8 ./venv/bin/python -c \"import app\" 2>&1 | grep -i deposit || true")
    # this deployer always serves behind Cloudflare (realip vhost below), so the app
    # may trust CF-Connecting-IP for its per-IP limits
    run(f"cd {REMOTE} && ./venv/bin/python -c \""
        "import json; p='config.json'; c=json.load(open(p)); c['behind_cloudflare']=True; "
        "json.dump(c, open(p,'w'), indent=2)\"")
    wif_addr = run(f"cd {REMOTE} && ./venv/bin/python -c \"import json,app; print(app.KEY.address)\"").strip()

    print("== 5. systemd service ==")
    service = f"""[Unit]
Description=Satloot Bitcoin testnet3 faucet
After=network.target

[Service]
WorkingDirectory={REMOTE}
ExecStart={REMOTE}/venv/bin/gunicorn -w 2 -b 127.0.0.1:8811 app:app
Restart=always
User=root

[Install]
WantedBy=multi-user.target
"""
    sftp.putfo(io.BytesIO(service.encode()), "/etc/systemd/system/btcfaucet.service")
    run("systemctl daemon-reload && systemctl enable --now btcfaucet && sleep 2 && systemctl is-active btcfaucet")

    print("== 6. nginx vhost ==")
    realip = "\n".join(f"    set_real_ip_from {r};" for r in CF_IPS)
    vhost = f"""server {{
    listen 80;
    server_name {domain};

    # Trust Cloudflare's edges so $remote_addr becomes the real visitor IP.
{realip}
    real_ip_header CF-Connecting-IP;

    location = /llms.txt {{
        alias {REMOTE}/llms.txt;
        default_type text/plain;
        charset utf-8;
    }}

    location / {{
        add_header Link '</llms.txt>; rel="describedby"' always;
        proxy_pass http://127.0.0.1:8811;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Real-IP $remote_addr;
        # Overwrite (never pass through) so a direct-to-origin request cannot
        # forge this header — the app trusts CF-Connecting-IP for rate limits.
        proxy_set_header CF-Connecting-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""
    sftp.putfo(io.BytesIO(vhost.encode()), f"/etc/nginx/sites-available/{domain}")
    run(f"ln -sf /etc/nginx/sites-available/{domain} /etc/nginx/sites-enabled/{domain} && nginx -t && systemctl reload nginx")

    print("== 7. TLS cert (needs DNS already pointing here) ==")
    run(f"certbot --nginx -d {domain} --non-interactive --agree-tos -m admin@satloot.com --redirect || echo 'certbot skipped/failed - run manually after DNS propagates'", check=False)

    sftp.close(); c.close()
    print("\n=== DONE ===")
    print(f"Faucet hot wallet address: {wif_addr}")
    print(f">>> FUND THIS ADDRESS with testnet3 coins, then visit https://{domain} <<<")

if __name__ == "__main__":
    main()
