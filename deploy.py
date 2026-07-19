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

    print("== 3. python venv + deps ==")
    run(f"cd {REMOTE} && python3 -m venv venv && ./venv/bin/pip install -q --upgrade pip && ./venv/bin/pip install -q -r requirements.txt && echo installed")

    print("== 4. first run to generate config + hot wallet ==")
    # start once to create config.json (auto-generates WIF), then stop
    run(f"cd {REMOTE} && timeout 8 ./venv/bin/python -c \"import app\" 2>&1 | grep -i deposit || true")
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
    vhost = f"""server {{
    listen 80;
    server_name {domain};
    location / {{
        proxy_pass http://127.0.0.1:8811;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
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
