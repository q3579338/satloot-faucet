#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
satloot.com  --  Bitcoin testnet3 faucet (anti-abuse hardened, bilingual)

Defence layers (all self-contained):
  1. Proof-of-Work challenge   2. Single-use HMAC token   3. Per-IP/address cooldown
  4. Per-subnet cap            5. Honeypot                6. Recipient-balance cap
  7. Global RPM limiter        8. Auto temporary ban      9. Daily payout cap (UTC+8) + reserve

WeChat (PushPlus) alert when the faucet runs dry or the daily cap is hit.
"""
import json, os, time, sqlite3, secrets, hmac, hashlib, ipaddress, threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import requests
from flask import Flask, request, jsonify, render_template

from bit import PrivateKeyTestnet
from bit.network import NetworkAPI
from bit.transaction import address_to_scriptpubkey

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
DB_PATH = os.path.join(BASE, "faucet.db")
LOCK_PATH = os.path.join(BASE, "payout.lock")   # cross-process payout mutex

CN_TZ = timezone(timedelta(hours=8))   # daily cap resets at 00:00 Beijing time (UTC+8)

DEFAULT_CONFIG = {
    "wif": "",
    "hmac_secret": "",
    "payout_sat": 10000,
    "max_payout_sat": 50000,               # user may request up to 0.0005 tBTC per claim
    "fee_sat_per_byte": 2,
    "site_title": "Satloot",
    "return_address": "",
    "trust_proxy": True,
    "behind_cloudflare": False,             # only trust CF-Connecting-IP when served behind Cloudflare
    "pow_bits": 17,
    "challenge_ttl_sec": 600,
    "ip_cooldown_sec": 86400,
    "address_cooldown_sec": 86400,
    "subnet_v4_prefix": 24,
    "subnet_v6_prefix": 64,
    "subnet_window_sec": 3600,
    "subnet_max_per_window": 4,
    "max_recipient_balance_sat": 2000000,
    "global_rpm": 30,
    "ban_strikes": 5,
    "ban_window_sec": 600,
    "ban_duration_sec": 3600,
    "honeypot_field": "website",
    "daily_global_cap_sat": 10000000,      # 0.1 tBTC / day (UTC+8)
    "min_reserve_sat": 0,
    "pushplus_token": "",                  # WeChat push (PushPlus); set in config.json
    "site_url": "https://satloot.com",
}

# ---------------------------------------------------------------- i18n (server messages)
MSG = {
 "busy":        {"en": "Faucet is busy, please try again later.", "zh": "水龙头繁忙,请稍后再试。"},
 "banned":      {"en": "Your IP is temporarily blocked due to abnormal requests.", "zh": "你的 IP 因异常请求被临时限制,请稍后再来。"},
 "rejected":    {"en": "Request denied.", "zh": "请求被拒绝。"},
 "need_params": {"en": "Missing verification, please refresh the page.", "zh": "缺少验证参数,请刷新页面重试。"},
 "bad_token":   {"en": "Invalid token, please refresh the page.", "zh": "验证令牌无效,请刷新页面。"},
 "bad_fmt":     {"en": "Malformed token.", "zh": "验证令牌格式错误。"},
 "expired":     {"en": "Verification expired, please refresh the page.", "zh": "验证已过期,请刷新页面重试。"},
 "bad_pow":     {"en": "Proof-of-work check failed.", "zh": "工作量证明校验失败。"},
 "token_used":  {"en": "This verification was already used, please refresh.", "zh": "该验证已被使用,请刷新页面。"},
 "bad_addr":    {"en": "Enter a valid testnet3 address (m / n / 2 / tb1...).", "zh": "请输入有效的 testnet3 地址(m / n / 2 / tb1 开头)。"},
 "addr_cd":     {"en": "This address already claimed. Try again in {h}h {m}m.", "zh": "该地址已领取过,请 {h} 小时 {m} 分后再来。"},
 "ip_cd":       {"en": "Too many claims from your IP. Try again in {m} min.", "zh": "你的 IP 领取太频繁,请 {m} 分钟后再来。"},
 "subnet":      {"en": "Too many claims from your network, please try later.", "zh": "你所在网段领取次数过多,请稍后再来。"},
 "rich":        {"en": "Your address already has enough test coins. Leave some for others.", "zh": "你的地址已有足够的测试币,把机会留给别人吧。"},
 "daily":       {"en": "Today's faucet quota is used up, please come back tomorrow.", "zh": "今日水龙头额度已用完,请明天再来。"},
 "bal_err":     {"en": "Failed to check faucet balance, try again later.", "zh": "水龙头查询余额失败,请稍后再试。"},
 "low":         {"en": "Faucet balance is too low, please wait for a refill.", "zh": "水龙头余额不足,请等待管理员补充。"},
 "pending":     {"en": "Faucet funds are still confirming on-chain — please try again in a few minutes.", "zh": "水龙头资金到账确认中,请几分钟后再试。"},
 "send_fail":   {"en": "Send failed: {e}", "zh": "发送失败:{e}"},
}

def req_lang():
    lang = (request.values.get("lang") or "").lower()
    if lang.startswith("zh"): return "zh"
    if lang.startswith("en"): return "en"
    al = request.headers.get("Accept-Language", "").lower()
    return "zh" if al.startswith("zh") else "en"

def M(lang, key, **kw):
    return MSG[key][lang].format(**kw)

# ---------------------------------------------------------------- config
def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    changed = False
    for k, v in DEFAULT_CONFIG.items():
        if k not in cfg:
            cfg[k] = v; changed = True
    if not cfg.get("wif"):
        key = PrivateKeyTestnet()
        cfg["wif"] = key.to_wif(); changed = True
        print("[faucet] generated hot wallet -> FUND:", key.address)
    if not cfg.get("hmac_secret"):
        cfg["hmac_secret"] = secrets.token_hex(32); changed = True
    if changed:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    return cfg

CONFIG = load_config()
KEY = PrivateKeyTestnet(CONFIG["wif"])
SECRET = CONFIG["hmac_secret"].encode()
if not CONFIG.get("return_address"):
    CONFIG["return_address"] = KEY.address

def make_qr(addr):
    try:
        import segno
        return segno.make(f"bitcoin:{addr}", error="m").svg_data_uri(
            scale=5, border=2, dark="#1a1a2e", light="#ffffff")
    except Exception:
        return None
RETURN_QR = make_qr(CONFIG["return_address"])

# ---------------------------------------------------------------- WeChat push (PushPlus)
def wechat_push(title, content):
    token = CONFIG.get("pushplus_token")
    if not token:
        return
    def _send():
        try:
            requests.post("https://www.pushplus.plus/send",
                          json={"token": token, "title": title,
                                "content": content, "template": "markdown"},
                          timeout=20)
        except Exception:
            pass
    threading.Thread(target=_send, daemon=True).start()

app = Flask(__name__)

# ---------------------------------------------------------------- db
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def init_db(*_a):
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS claims(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        address TEXT, ip TEXT, subnet TEXT, amount_sat INTEGER, txid TEXT, ts INTEGER);
    CREATE INDEX IF NOT EXISTS idx_ip     ON claims(ip, ts);
    CREATE INDEX IF NOT EXISTS idx_addr   ON claims(address, ts);
    CREATE INDEX IF NOT EXISTS idx_subnet ON claims(subnet, ts);
    CREATE TABLE IF NOT EXISTS used_tokens(jti TEXT PRIMARY KEY, exp INTEGER);
    CREATE TABLE IF NOT EXISTS strikes(id INTEGER PRIMARY KEY AUTOINCREMENT, ip TEXT, ts INTEGER);
    CREATE INDEX IF NOT EXISTS idx_strk   ON strikes(ip, ts);
    CREATE TABLE IF NOT EXISTS bans(ip TEXT PRIMARY KEY, until INTEGER);
    CREATE TABLE IF NOT EXISTS attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER);
    CREATE TABLE IF NOT EXISTS alerts(kind TEXT PRIMARY KEY, ts INTEGER);
    """)
    con.commit(); con.close()

init_db()

def maybe_alert(con, kind, cooldown, title, content):
    """Send a WeChat push at most once per `cooldown` seconds for this kind."""
    now = int(time.time())
    row = con.execute("SELECT ts FROM alerts WHERE kind=?", (kind,)).fetchone()
    if row and now - row["ts"] < cooldown:
        return
    con.execute("INSERT OR REPLACE INTO alerts(kind, ts) VALUES(?,?)", (kind, now))
    con.commit()
    wechat_push(title, content)

# ---------------------------------------------------------------- payout mutex
class PayoutBusy(Exception):
    """Raised when the payout lock can't be acquired within the timeout."""

@contextmanager
def payout_lock(timeout_sec=25):
    """Serialize the select-coins -> broadcast -> record critical section across
    processes. Two gunicorn workers can otherwise read the same UTXO set and build
    two txs spending the same input: one broadcast wins, the other is rejected with
    'bad-txns-inputs-missingorspent' and the user sees a spurious send failure.
    A dedicated SQLite file acts as the lock — BEGIN IMMEDIATE grabs its write lock,
    and the connection's busy timeout makes a second worker wait rather than collide.
    Holding it across the network calls is intentional: payouts are small and rare,
    so serializing them costs nothing and removes the race entirely."""
    con = sqlite3.connect(LOCK_PATH, timeout=timeout_sec)
    try:
        try:
            con.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            raise PayoutBusy()
        try:
            yield
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    finally:
        con.close()

# ---------------------------------------------------------------- helpers
def client_ip():
    if CONFIG.get("trust_proxy"):
        # CF-Connecting-IP is a plain header any client can forge, so only trust it
        # when explicitly served behind Cloudflare. In that setup nginx (see deploy.py)
        # validates the request really came from a Cloudflare edge and overwrites the
        # header with the realip-resolved visitor IP, making it unspoofable. Without
        # this flag we ignore it and fall back to the first X-Forwarded-For hop.
        if CONFIG.get("behind_cloudflare"):
            cf = request.headers.get("CF-Connecting-IP", "").strip()
            if cf:
                return cf
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[0].strip()
    return request.remote_addr or "0.0.0.0"

def subnet_of(ip):
    try:
        a = ipaddress.ip_address(ip)
        pfx = CONFIG["subnet_v4_prefix"] if a.version == 4 else CONFIG["subnet_v6_prefix"]
        net = ipaddress.ip_network(f"{ip}/{pfx}", strict=False)
        return f"{net.network_address}/{net.prefixlen}"
    except Exception:
        return ip

def is_valid_testnet3_address(addr):
    if not addr or len(addr) < 26 or len(addr) > 90:
        return False
    if addr[0] in "13" or addr.lower().startswith("bc1"):
        return False
    if not (addr[0] in "mn2" or addr.lower().startswith("tb1")):
        return False
    try:
        address_to_scriptpubkey(addr)
        return True
    except Exception:
        return False

def day_start_ts():
    """Epoch of the most recent 00:00 in UTC+8 (Beijing)."""
    now = datetime.now(CN_TZ)
    return int(datetime(now.year, now.month, now.day, tzinfo=CN_TZ).timestamp())

def paid_today(con):
    return con.execute("SELECT COALESCE(SUM(amount_sat),0) s FROM claims WHERE ts>=?",
                       (day_start_ts(),)).fetchone()["s"]

def leading_zero_bits(digest: bytes) -> int:
    n = 0
    for byte in digest:
        if byte == 0: n += 8
        else:
            n += 8 - byte.bit_length(); break
    return n

# ------- proof-of-work challenge -------
def make_challenge():
    jti = secrets.token_hex(8)
    exp = int(time.time()) + CONFIG["challenge_ttl_sec"]
    bits = CONFIG["pow_bits"]
    token = f"{jti}.{exp}.{bits}"
    sig = hmac.new(SECRET, token.encode(), hashlib.sha256).hexdigest()
    return {"token": token, "sig": sig, "bits": bits}

def verify_challenge(con, token, sig, nonce):
    if not token or not sig or nonce is None:
        return False, "need_params"
    expect = hmac.new(SECRET, token.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expect):
        return False, "bad_token"
    try:
        jti, exp, bits = token.split("."); exp = int(exp); bits = int(bits)
    except Exception:
        return False, "bad_fmt"
    if time.time() > exp:
        return False, "expired"
    h = hashlib.sha256(f"{token}:{nonce}".encode()).digest()
    if leading_zero_bits(h) < bits:
        return False, "bad_pow"
    try:
        con.execute("INSERT INTO used_tokens(jti, exp) VALUES(?,?)", (jti, exp))
    except sqlite3.IntegrityError:
        return False, "token_used"
    return True, None

# ------- ban / strike -------
def is_banned(con, ip):
    row = con.execute("SELECT until FROM bans WHERE ip=?", (ip,)).fetchone()
    return bool(row and row["until"] > int(time.time()))

def add_strike(con, ip):
    now = int(time.time())
    con.execute("INSERT INTO strikes(ip, ts) VALUES(?,?)", (ip, now))
    cnt = con.execute("SELECT COUNT(*) c FROM strikes WHERE ip=? AND ts>=?",
                      (ip, now - CONFIG["ban_window_sec"])).fetchone()["c"]
    if cnt >= CONFIG["ban_strikes"]:
        con.execute("INSERT OR REPLACE INTO bans(ip, until) VALUES(?,?)",
                    (ip, now + CONFIG["ban_duration_sec"]))
    con.commit()

# ---------------------------------------------------------------- routes
@app.route("/")
def index():
    try:
        bal = NetworkAPI.get_balance_testnet(KEY.address)
    except Exception:
        bal = None
    con = get_db()
    recent = [dict(r) for r in con.execute(
        "SELECT address, amount_sat, txid, ts FROM claims ORDER BY id DESC LIMIT 15").fetchall()]
    remaining = max(0, CONFIG["daily_global_cap_sat"] - paid_today(con))
    con.close()
    return render_template("index.html",
        title=CONFIG["site_title"],
        faucet_address=KEY.address,
        return_address=CONFIG["return_address"],
        balance_sat=bal,
        payout_sat=CONFIG["payout_sat"],
        max_payout_sat=CONFIG["max_payout_sat"],
        daily_cap_sat=CONFIG["daily_global_cap_sat"],
        daily_remaining_sat=remaining,
        ip_cooldown_min=CONFIG["ip_cooldown_sec"] // 60,
        honeypot_field=CONFIG["honeypot_field"],
        challenge=make_challenge(),
        return_qr=RETURN_QR,
        recent=recent)

@app.route("/api/challenge")
def challenge():
    return jsonify(make_challenge())

_TX_CACHE = {"ts": 0, "data": []}

def fetch_address_txs(addr, limit=25):
    """Real on-chain tx history for the faucet address (Esplora testnet3)."""
    base = "https://blockstream.info/testnet/api"
    try:
        txs = requests.get(f"{base}/address/{addr}/txs", timeout=15).json()
    except Exception:
        return []
    try:
        tip = int(requests.get(f"{base}/blocks/tip/height", timeout=10).text)
    except Exception:
        tip = None
    out = []
    for tx in txs[:limit]:
        vin_sum = sum(v.get("prevout", {}).get("value", 0) for v in tx.get("vin", [])
                      if v.get("prevout", {}).get("scriptpubkey_address") == addr)
        vout_sum = sum(o.get("value", 0) for o in tx.get("vout", [])
                       if o.get("scriptpubkey_address") == addr)
        st = tx.get("status", {})
        conf = 0
        if st.get("confirmed") and tip is not None and st.get("block_height"):
            conf = tip - st["block_height"] + 1
        out.append({"txid": tx["txid"], "net_sat": vout_sum - vin_sum,
                    "confirmations": conf, "time": st.get("block_time")})
    return out

def broadcast_tx(raw_hex):
    """Broadcast a signed tx via Esplora (bit's own broadcasters are dead for testnet3)."""
    for url in ("https://blockstream.info/testnet/api/tx",
                "https://mempool.space/testnet/api/tx"):
        try:
            r = requests.post(url, data=raw_hex, timeout=25)
            if r.status_code == 200 and len(r.text.strip()) == 64:
                return r.text.strip()
        except Exception:
            continue
    return None

@app.route("/api/txs")
def api_txs():
    now = int(time.time())
    if now - _TX_CACHE["ts"] > 45:
        _TX_CACHE["data"] = fetch_address_txs(KEY.address)
        _TX_CACHE["ts"] = now
    return jsonify(_TX_CACHE["data"])

@app.route("/api/addrbalance")
def addr_balance():
    addr = (request.args.get("address") or "").strip()
    if not is_valid_testnet3_address(addr):
        return jsonify(ok=False)
    try:
        j = requests.get(f"https://blockstream.info/testnet/api/address/{addr}", timeout=12).json()
        cs, ms = j["chain_stats"], j["mempool_stats"]
        bal = (cs["funded_txo_sum"] - cs["spent_txo_sum"]) + (ms["funded_txo_sum"] - ms["spent_txo_sum"])
        return jsonify(ok=True, balance_sat=bal)
    except Exception:
        return jsonify(ok=False)

@app.route("/api/status")
def status():
    try:
        bal = NetworkAPI.get_balance_testnet(KEY.address)
    except Exception:
        bal = None
    con = get_db(); remaining = max(0, CONFIG["daily_global_cap_sat"] - paid_today(con)); con.close()
    return jsonify(address=KEY.address, balance_sat=bal, payout_sat=CONFIG["payout_sat"],
                   daily_remaining_sat=remaining)

@app.route("/api/claim", methods=["POST"])
def claim():
    lang = req_lang()
    con = get_db()
    try:
        ip = client_ip()
        now = int(time.time())

        con.execute("INSERT INTO attempts(ts) VALUES(?)", (now,)); con.commit()
        rpm = con.execute("SELECT COUNT(*) c FROM attempts WHERE ts>=?", (now - 60,)).fetchone()["c"]
        if rpm > CONFIG["global_rpm"]:
            return jsonify(ok=False, error=M(lang, "busy")), 503

        if is_banned(con, ip):
            return jsonify(ok=False, error=M(lang, "banned")), 429

        f = request.form
        addr = (f.get("address") or "").strip()
        honey = (f.get(CONFIG["honeypot_field"]) or "").strip()
        token = f.get("token"); sig = f.get("sig"); nonce = f.get("nonce")

        # requested amount (clamped to [payout_sat, max_payout_sat])
        try:
            payout = int(float(f.get("amount_sat") or CONFIG["payout_sat"]))
        except Exception:
            payout = CONFIG["payout_sat"]
        payout = max(CONFIG["payout_sat"], min(payout, CONFIG["max_payout_sat"]))

        if honey:
            add_strike(con, ip)
            return jsonify(ok=False, error=M(lang, "rejected")), 400

        ok, err = verify_challenge(con, token, sig, nonce)
        if not ok:
            con.commit(); add_strike(con, ip)
            return jsonify(ok=False, error=M(lang, err)), 400

        if not is_valid_testnet3_address(addr):
            con.commit(); add_strike(con, ip)
            return jsonify(ok=False, error=M(lang, "bad_addr")), 400

        # Everything below reads the faucet's UTXO set and spends from it, and
        # re-checks the per-address/IP/subnet cooldowns and the daily cap. Run it
        # under the cross-process payout lock so two workers can't select the same
        # coin (double-spend -> spurious send failure) or both clear a cooldown and
        # pay the same address twice in the same instant.
        try:
            with payout_lock(timeout_sec=25):
                row = con.execute("SELECT ts FROM claims WHERE address=? ORDER BY id DESC LIMIT 1",
                                  (addr,)).fetchone()
                if row and now - row["ts"] < CONFIG["address_cooldown_sec"]:
                    w = CONFIG["address_cooldown_sec"] - (now - row["ts"]); con.commit()
                    return jsonify(ok=False, error=M(lang, "addr_cd", h=w//3600, m=w%3600//60)), 429

                row = con.execute("SELECT ts FROM claims WHERE ip=? ORDER BY id DESC LIMIT 1",
                                  (ip,)).fetchone()
                if row and now - row["ts"] < CONFIG["ip_cooldown_sec"]:
                    w = CONFIG["ip_cooldown_sec"] - (now - row["ts"]); con.commit()
                    return jsonify(ok=False, error=M(lang, "ip_cd", m=w//60)), 429

                snet = subnet_of(ip)
                scnt = con.execute("SELECT COUNT(*) c FROM claims WHERE subnet=? AND ts>=?",
                                   (snet, now - CONFIG["subnet_window_sec"])).fetchone()["c"]
                if scnt >= CONFIG["subnet_max_per_window"]:
                    con.commit()
                    return jsonify(ok=False, error=M(lang, "subnet")), 429

                try:
                    rbal = NetworkAPI.get_balance_testnet(addr)
                    if rbal is not None and int(rbal) > CONFIG["max_recipient_balance_sat"]:
                        con.commit()
                        return jsonify(ok=False, error=M(lang, "rich")), 403
                except Exception:
                    pass

                # daily hard cap (UTC+8)
                if paid_today(con) + payout > CONFIG["daily_global_cap_sat"]:
                    cap_btc = CONFIG["daily_global_cap_sat"] / 1e8
                    maybe_alert(con, "daily_cap", 86400,
                                "Satloot 水龙头今日额度已用完",
                                f"## 💧 今日额度已发满\n\n**{cap_btc:.8f} tBTC** 的当日上限已达到。\n\n"
                                f"次日 00:00 (UTC+8) 自动重置。\n\n{CONFIG.get('site_url','')}")
                    return jsonify(ok=False, error=M(lang, "daily")), 503

                try:
                    KEY.get_unspents()
                    bal = int(KEY.balance_as("satoshi"))
                except Exception:
                    con.commit()
                    return jsonify(ok=False, error=M(lang, "bal_err"))
                if bal - payout < CONFIG["min_reserve_sat"]:
                    maybe_alert(con, "empty", 6 * 3600,
                                "⚠️ Satloot 水龙头没币了",
                                f"## ⚠️ 水龙头余额不足\n\n当前余额 **{bal/1e8:.8f} tBTC**,已无法继续发放。\n\n"
                                f"请给热钱包充值:\n\n`{KEY.address}`\n\n{CONFIG.get('site_url','')}")
                    con.commit()
                    return jsonify(ok=False, error=M(lang, "low"))

                try:
                    # bit builds+signs; we broadcast via Esplora (bit's testnet3 broadcasters are dead)
                    amount_btc = payout / 1e8
                    raw = KEY.create_transaction([(addr, f"{amount_btc:.8f}", "btc")],
                                                 fee=CONFIG["fee_sat_per_byte"], combine=False)
                    txid = broadcast_tx(raw)
                    if not txid:
                        raise Exception("broadcast rejected by all endpoints")
                except Exception as e:
                    con.commit()
                    return jsonify(ok=False, error=M(lang, "send_fail", e=str(e)[:160]))

                con.execute("INSERT INTO claims(address, ip, subnet, amount_sat, txid, ts) VALUES(?,?,?,?,?,?)",
                            (addr, ip, snet, payout, txid, now))
                con.execute("DELETE FROM used_tokens WHERE exp < ?", (now,))
                con.execute("DELETE FROM attempts WHERE ts < ?", (now - 120,))
                con.commit()
                return jsonify(ok=True, txid=txid, amount_sat=payout,
                               explorer=f"https://mempool.space/testnet/tx/{txid}")
        except PayoutBusy:
            return jsonify(ok=False, error=M(lang, "busy")), 503
    finally:
        con.close()

if __name__ == "__main__":
    print("[faucet] hot wallet:", KEY.address, "| PoW bits:", CONFIG["pow_bits"],
          "| daily cap:", CONFIG["daily_global_cap_sat"]/1e8, "tBTC (UTC+8)")
    app.run(host="127.0.0.1", port=8811)
