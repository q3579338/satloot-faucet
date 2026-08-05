#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把闪电板块更新推到已上线的 satloot 水龙头(5.78.104.216)。

只更新代码,不碰 config.json 里的密钥/热钱包;LN 后端未就绪时页面自动
显示"节点同步中",不影响链上水龙头。

用法: python deploy_ln.py <server_ip> <root_password>
"""
import sys, os, paramiko

APP_DIR = os.path.dirname(os.path.abspath(__file__))
REMOTE = "/opt/btc_faucet"

def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)
    host, pw = sys.argv[1], sys.argv[2]

    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"[*] connecting {host} ...", flush=True)
    c.connect(host, username="root", password=pw, timeout=25)
    sftp = c.open_sftp()

    def run(cmd, check=True):
        print("  $", cmd, flush=True)
        _i, o, e = c.exec_command(cmd, timeout=300)
        out = o.read().decode(errors="replace"); err = e.read().decode(errors="replace")
        code = o.channel.recv_exit_status()
        if out.strip(): print(out.strip(), flush=True)
        if code != 0:
            print("[stderr]", err.strip(), flush=True)
            if check: raise RuntimeError(f"failed ({code}): {cmd}")
        return out

    print("== 1. 备份当前版本 ==")
    run(f"cp {REMOTE}/app.py {REMOTE}/app.py.bak && "
        f"cp {REMOTE}/templates/index.html {REMOTE}/templates/index.html.bak && echo backed-up")

    print("== 2. 上传新代码 ==")
    for local, remote in [("app.py", f"{REMOTE}/app.py"),
                          ("lnd_client.py", f"{REMOTE}/lnd_client.py"),
                          (os.path.join("templates", "index.html"), f"{REMOTE}/templates/index.html")]:
        print("  put", local, flush=True)
        sftp.put(os.path.join(APP_DIR, local), remote)

    print("== 3. 语法自检 ==")
    run(f"cd {REMOTE} && ./venv/bin/python -c \"import ast;ast.parse(open('app.py',encoding='utf-8').read());print('syntax ok')\"")

    print("== 4. 重启服务 ==")
    run("systemctl restart btcfaucet && sleep 3 && systemctl is-active btcfaucet")

    print("== 5. 冒烟测试 ==")
    run("curl -s -o /dev/null -w 'index HTTP %{http_code}\\n' http://127.0.0.1:8811/")
    run("curl -s http://127.0.0.1:8811/api/lninfo | head -c 300; echo")
    run("curl -s -o /dev/null -w 'status HTTP %{http_code}\\n' http://127.0.0.1:8811/api/status")

    sftp.close(); c.close()
    print("\n=== 部署完成 ===")
    print("回滚: cp /opt/btc_faucet/app.py.bak /opt/btc_faucet/app.py && "
          "cp /opt/btc_faucet/templates/index.html.bak /opt/btc_faucet/templates/index.html && "
          "systemctl restart btcfaucet")

if __name__ == "__main__":
    main()
