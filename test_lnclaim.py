#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测水龙头的闪电开通道:走公网 HTTP,自己算 PoW,和真实用户走同一条路。
用法: python test_lnclaim.py <node_uri> [--url https://faucet.satloot.com]"""
import sys, time, hashlib, requests

uri = sys.argv[1]
base = "https://faucet.satloot.com"
if "--url" in sys.argv:
    base = sys.argv[sys.argv.index("--url") + 1]

print(f"target : {uri}")
print(f"faucet : {base}")

info = requests.get(f"{base}/api/lninfo", timeout=20).json()
print(f"lninfo : synced={info['synced']} capacity={info['capacity_sat']} push={info['push_sat']}")
if not info["synced"]:
    raise SystemExit("faucet says not ready")

ch = requests.get(f"{base}/api/challenge", timeout=20).json()
print(f"pow    : {ch['bits']} bits ...", end="", flush=True)

def lzb(d):
    n = 0
    for b in d:
        if b == 0: n += 8
        else: n += 8 - b.bit_length(); break
    return n

t0 = time.time(); nonce = 0
while lzb(hashlib.sha256(f"{ch['token']}:{nonce}".encode()).digest()) < ch["bits"]:
    nonce += 1
print(f" solved nonce={nonce} in {time.time()-t0:.1f}s")

r = requests.post(f"{base}/api/lnclaim", timeout=120, data={
    "node_uri": uri, "token": ch["token"], "sig": ch["sig"],
    "nonce": str(nonce), "lang": "en"})
print(f"HTTP {r.status_code}")
try:
    print(r.json())
except ValueError:
    print(r.text[:400])
