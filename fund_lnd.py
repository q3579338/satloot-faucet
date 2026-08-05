#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从水龙头热钱包给本机 LND 注资(testnet3,零真实价值)。

在节点服务器上构建+签名,并用**我们自己的 bitcoind** 广播,不依赖外部 Esplora。
默认 dry-run,加 --send 才真发。

用法: python fund_lnd.py <node_ip> <root_pw> [--amount 0.3] [--send]
"""
import sys, io, json, paramiko

REMOTE = "/opt/btc_faucet"   # 私钥只在服务器的 config.json 里,本脚本不持有

SCRIPT = r'''#!/usr/bin/env bash
cd /opt/btc_faucet
./venv/bin/python - "$@" <<'PY'
import sys, json, subprocess
from bit import PrivateKeyTestnet
from bit.network import NetworkAPI

amount = float(sys.argv[1]); do_send = sys.argv[2] == "send"
KEY = PrivateKeyTestnet(json.load(open("config.json"))["wif"])

def cli(*args):
    out = subprocess.run(
        ["bitcoin-cli", "-conf=/home/bitcoin/.bitcoin/bitcoin.conf", "-testnet", *args],
        capture_output=True, text=True, timeout=90)
    if out.returncode != 0:
        raise SystemExit("bitcoin-cli failed: " + out.stderr.strip()[:300])
    return out.stdout.strip()

dest = json.loads(subprocess.run(
    ["lncli", "--network=testnet", "--lnddir=/home/lnd/.lnd", "newaddress", "p2wkh"],
    capture_output=True, text=True, timeout=60).stdout)["address"]

KEY.get_unspents()
bal = int(KEY.balance_as("satoshi"))
print(f"faucet wallet : {KEY.address}")
print(f"balance       : {bal/1e8:.8f} tBTC ({len(KEY.unspents)} utxo)")
print(f"LND deposit   : {dest}")
print(f"amount        : {amount:.8f} tBTC")

raw = KEY.create_transaction([(dest, f"{amount:.8f}", "btc")], fee=2, combine=False)
print(f"raw tx        : {len(raw)//2} bytes")

if not do_send:
    print("\n[dry-run] 未广播。加 --send 才真发。")
else:
    txid = cli("sendrawtransaction", raw)      # 用我们自己的节点广播
    print(f"\nBROADCAST OK  : {txid}")
    print(f"explorer      : https://mempool.space/testnet/tx/{txid}")
PY
'''

def main():
    host, pw = sys.argv[1], sys.argv[2]
    amount = "0.3"
    if "--amount" in sys.argv:
        amount = sys.argv[sys.argv.index("--amount") + 1]
    send = "send" if "--send" in sys.argv else "dry"

    c = paramiko.SSHClient(); c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(host, username="root", password=pw, timeout=25)
    sftp = c.open_sftp(); sftp.putfo(io.BytesIO(SCRIPT.encode()), "/root/fund-lnd.sh"); sftp.close()
    i, o, e = c.exec_command(f"bash /root/fund-lnd.sh {amount} {send} 2>&1", timeout=300)
    sys.stdout.write(o.read().decode(errors="replace"))
    c.close()

if __name__ == "__main__":
    main()
