#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 satloot 水龙头迁到节点服务器(172.96.9.5)。

原 5.78.104.216 整机宕机;新机上 bitcoind-testnet + LND 已本地运行,
所以闪电改走 127.0.0.1,不再跨公网调 REST(顺带可以关掉对外的 8080)。

热钱包 WIF 从本地备份带过去(地址不变,余额还在链上);hmac_secret 重新生成
(只会让正在进行的 PoW 挑战失效);claim 历史(冷却)重置——旧库在宕机的机器上。

热钱包 WIF 不写在代码里:优先读环境变量 FAUCET_WIF,否则从本地(未纳入版本管理的)
HOTWALLET 备份文件里取。PushPlus token 同理读 PUSHPLUS_TOKEN,没有就留空(不推送告警)。

用法: python migrate_to_node.py <node_ip> <root_password> [domain]
"""
import sys, os, io, re, json, glob, secrets, paramiko

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE = "/opt/btc_faucet"
DOMAIN_DEFAULT = "faucet.satloot.com"


def load_wif():
    """环境变量优先;否则从本地 HOTWALLET* 备份里解析(那些文件被 .gitignore 挡着)。"""
    wif = os.environ.get("FAUCET_WIF", "").strip()
    if wif:
        return wif
    for path in glob.glob(os.path.join(APP_DIR, "HOTWALLET*")):
        try:
            text = open(path, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        m = re.search(r"\b([59KLc][1-9A-HJ-NP-Za-km-z]{50,51})\b", text)
        if m:
            return m.group(1)
    raise SystemExit(
        "找不到热钱包 WIF。设环境变量 FAUCET_WIF=<wif>,或把 HOTWALLET 备份放在本目录。")

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
    print(f"[*] connecting {host} ...", flush=True)
    c.connect(host, username="root", password=pw, timeout=25)
    sftp = c.open_sftp()

    def run(cmd, check=True, quiet=False):
        if not quiet: print("  $", cmd[:110], flush=True)
        _i, o, e = c.exec_command(cmd, timeout=600)
        out = o.read().decode(errors="replace"); err = e.read().decode(errors="replace")
        code = o.channel.recv_exit_status()
        if out.strip() and not quiet: print(out.strip()[:1500], flush=True)
        if code != 0:
            if err.strip(): print("[stderr]", err.strip()[:600], flush=True)
            if check: raise RuntimeError(f"failed ({code}): {cmd[:80]}")
        return out

    print("== 1. 系统依赖 ==")
    run("DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip nginx "
        ">/dev/null 2>&1; echo deps-ok")

    print("== 2. 上传应用 ==")
    run(f"mkdir -p {REMOTE}/templates {REMOTE}/lnd")
    for local, remote in [("app.py", f"{REMOTE}/app.py"),
                          ("lnd_client.py", f"{REMOTE}/lnd_client.py"),
                          ("requirements.txt", f"{REMOTE}/requirements.txt"),
                          ("llms.txt", f"{REMOTE}/llms.txt"),
                          (os.path.join("templates", "index.html"), f"{REMOTE}/templates/index.html")]:
        print("  put", local, flush=True)
        sftp.put(os.path.join(APP_DIR, local), remote)

    print("== 3. venv + 依赖 ==")
    run(f"cd {REMOTE} && (test -d venv || python3 -m venv venv) && "
        f"./venv/bin/pip install -q --upgrade pip && "
        f"./venv/bin/pip install -q -r requirements.txt segno && echo installed")

    print("== 4. LND 凭据(本机复制)+ 探测 REST ==")
    mac = "/home/lnd/.lnd/data/chain/bitcoin/testnet/admin.macaroon"
    run(f"cp {mac} {REMOTE}/lnd/admin.macaroon && cp /home/lnd/.lnd/tls.cert {REMOTE}/lnd/tls.cert && "
        f"chmod 600 {REMOTE}/lnd/admin.macaroon && echo lnd-creds-ok")
    # LND 自签证书未必包含 127.0.0.1,验证失败就退回不校验(本机回环,无中间人风险)
    probe = run(f"cd {REMOTE} && ./venv/bin/python -c \""
                "import requests,codecs;"
                "m=codecs.encode(open('lnd/admin.macaroon','rb').read(),'hex').decode();"
                "h={'Grpc-Metadata-macaroon':m};"
                "\nimport urllib3;urllib3.disable_warnings()\n"
                "r=requests.get('https://127.0.0.1:8080/v1/getinfo',headers=h,verify='lnd/tls.cert',timeout=8);"
                "print('CERT_OK',r.json().get('synced_to_chain'))\" 2>&1 || true", check=False)
    use_cert = "CERT_OK" in probe
    print(f"  TLS 证书对 127.0.0.1 {'有效' if use_cert else '不适用 -> 关闭校验(本机回环)'}")

    print("== 5. config.json ==")
    cfg = {
        "wif": load_wif(),
        "hmac_secret": secrets.token_hex(32),
        "return_address": "",
        "pushplus_token": os.environ.get("PUSHPLUS_TOKEN", ""),
        "trust_proxy": True,
        "behind_cloudflare": True,
        "site_url": f"https://{domain}",
        # 闪电:本机 LND,不再跨公网
        "ln_node_uri": "039e7672537f2c742141dfadfd1b2e9a4871f9c064873347ab0fc0671f73f093f8@172.96.9.5:9735",
        "lnd_rest_url": "https://127.0.0.1:8080",
        "lnd_macaroon_path": f"{REMOTE}/lnd/admin.macaroon",
        "lnd_tls_cert_path": (f"{REMOTE}/lnd/tls.cert" if use_cert else ""),
        "ln_node_host": "172.96.9.5:9735",
    }
    sftp.putfo(io.BytesIO(json.dumps(cfg, indent=2).encode()), f"{REMOTE}/config.json")
    run(f"chmod 600 {REMOTE}/config.json && echo config-ok")

    addr = run(f"cd {REMOTE} && ./venv/bin/python -c "
               f"\"import app;print(app.KEY.address)\"").strip().splitlines()[-1]

    print("== 6. systemd ==")
    service = f"""[Unit]
Description=Satloot Bitcoin testnet3 faucet
After=network.target bitcoind-testnet.service lnd.service

[Service]
WorkingDirectory={REMOTE}
ExecStart={REMOTE}/venv/bin/gunicorn -w 2 --timeout 60 -b 127.0.0.1:8811 app:app
Restart=always
User=root

[Install]
WantedBy=multi-user.target
"""
    sftp.putfo(io.BytesIO(service.encode()), "/etc/systemd/system/btcfaucet.service")
    run("systemctl daemon-reload && systemctl enable --now btcfaucet && sleep 3 && "
        "systemctl is-active btcfaucet")

    print("== 7. nginx(Cloudflare realip)==")
    realip = "\n".join(f"    set_real_ip_from {r};" for r in CF_IPS)
    vhost = f"""server {{
    listen 80;
    listen 443 ssl;
    server_name {domain};

    ssl_certificate     /etc/ssl/certs/satloot-selfsigned.crt;
    ssl_certificate_key /etc/ssl/private/satloot-selfsigned.key;

    # Trust Cloudflare's edges so $remote_addr is the real visitor IP.
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
        # Overwrite (never pass through) so a direct-to-origin request cannot forge it.
        proxy_set_header CF-Connecting-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
    }}
}}
"""
    run("test -f /etc/ssl/certs/satloot-selfsigned.crt || "
        "openssl req -x509 -nodes -days 3650 -newkey rsa:2048 "
        "-keyout /etc/ssl/private/satloot-selfsigned.key "
        f"-out /etc/ssl/certs/satloot-selfsigned.crt -subj '/CN={domain}' >/dev/null 2>&1; echo cert-ok")
    sftp.putfo(io.BytesIO(vhost.encode()), f"/etc/nginx/sites-available/{domain}")
    run(f"ln -sf /etc/nginx/sites-available/{domain} /etc/nginx/sites-enabled/{domain} && "
        "rm -f /etc/nginx/sites-enabled/default && nginx -t 2>&1 | tail -2 && systemctl reload nginx")

    print("== 8. 防火墙 ==")
    run("ufw allow 80/tcp >/dev/null 2>&1; ufw allow 443/tcp >/dev/null 2>&1; "
        "ufw delete allow from 5.78.104.216 to any port 8080 proto tcp >/dev/null 2>&1; "
        "ufw status | head -12", check=False)

    print("== 9. 冒烟测试 ==")
    run("curl -s -o /dev/null -w '  index      HTTP %{http_code}\\n' http://127.0.0.1:8811/")
    run("curl -s http://127.0.0.1:8811/api/lninfo; echo")
    run("curl -s http://127.0.0.1:8811/api/status; echo")

    sftp.close(); c.close()
    print("\n=== 迁移完成 ===")
    print(f"热钱包地址(不变): {addr}")
    print(f">>> 去 Cloudflare 把 {domain} 的 A 记录改成 {host} <<<")

if __name__ == "__main__":
    main()
