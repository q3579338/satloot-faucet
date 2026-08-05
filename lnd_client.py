#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Minimal LND REST client (testnet3) — no gRPC, just `requests`.

Auth is the standard `Grpc-Metadata-macaroon` hex header; TLS verification
uses LND's own self-signed tls.cert when available. Amount fields are sent
as strings because that is what LND's REST proxy expects for int64.
"""
import base64
import binascii
import codecs
import json
import os

import requests


class LNDError(Exception):
    """Raised for transport errors and LND-level error responses."""


class LNDClient:
    def __init__(self, rest_url, macaroon_path=None, macaroon_hex=None,
                 tls_cert_path=None, timeout=8):
        self.rest_url = rest_url.rstrip("/")
        self.timeout = timeout
        if macaroon_hex:
            self.macaroon = macaroon_hex
        elif macaroon_path:
            path = os.path.expanduser(macaroon_path)
            try:
                with open(path, "rb") as f:
                    self.macaroon = codecs.encode(f.read(), "hex").decode()
            except OSError as e:
                raise LNDError(f"macaroon unreadable: {e}") from e
        else:
            raise LNDError("need macaroon_path or macaroon_hex")
        cert = os.path.expanduser(tls_cert_path) if tls_cert_path else None
        self.verify = cert if cert and os.path.exists(cert) else False
        if not self.verify:
            requests.packages.urllib3.disable_warnings()

    # ---------------------------------------------------------------- core
    def _call(self, method, path, body=None, timeout=None, stream=False):
        url = self.rest_url + path
        try:
            r = requests.request(
                method, url,
                headers={"Grpc-Metadata-macaroon": self.macaroon},
                json=body if body is not None else None,
                verify=self.verify,
                timeout=timeout or self.timeout,
                stream=stream)
        except requests.RequestException as e:
            raise LNDError(f"lnd unreachable: {e}") from e
        if stream:
            return r
        try:
            data = r.json()
        except ValueError:
            raise LNDError(f"lnd bad response ({r.status_code})")
        if r.status_code != 200:
            raise LNDError(data.get("message") or data.get("error") or f"http {r.status_code}")
        return data

    # ---------------------------------------------------------------- info
    def getinfo(self):
        return self._call("GET", "/v1/getinfo")

    def wallet_balance(self):
        return self._call("GET", "/v1/balance/blockchain")

    def channel_balance(self):
        return self._call("GET", "/v1/balance/channels")

    def new_address(self, addr_type="WITNESS_PUBKEY_HASH"):
        return self._call("GET", "/v1/newaddress?type=" + addr_type).get("address")

    def pending_channels(self):
        return self._call("GET", "/v1/channels/pending")

    def num_pending_open(self):
        return len(self.pending_channels().get("pending_open_channels") or [])

    # ---------------------------------------------------------------- peers
    def connect_peer(self, pubkey, host):
        """Connect to pubkey@host. 'already connected' is treated as success.
        Timeouts are kept well under a typical 30s gunicorn worker timeout so an
        unresponsive peer fails the request instead of killing the worker."""
        try:
            self._call("POST", "/v1/peers",
                       {"addr": {"pubkey": pubkey, "host": host},
                        "perm": False, "timeout": "8"},
                       timeout=10)
        except LNDError as e:
            if "already connected" in str(e).lower():
                return
            raise

    # ---------------------------------------------------------------- channels
    def open_channel(self, pubkey, local_funding_sat, push_sat, sat_per_vbyte=1):
        """Synchronous channel open; returns the funding txid as display hex."""
        resp = self._call("POST", "/v1/channels",
                          {"node_pubkey_string": pubkey,
                           "node_pubkey": base64.b64encode(bytes.fromhex(pubkey)).decode(),
                           "local_funding_amount": str(int(local_funding_sat)),
                           "push_sat": str(int(push_sat)),
                           "sat_per_vbyte": str(int(sat_per_vbyte)),
                           "min_confs": 1,
                           "spend_unconfirmed": False},
                          timeout=15)
        txid = resp.get("funding_txid_str")
        if not txid and resp.get("funding_txid_bytes"):
            # bytes come little-endian; reverse for the usual display order
            raw = base64.b64decode(resp["funding_txid_bytes"])
            txid = binascii.hexlify(raw[::-1]).decode()
        if not txid:
            raise LNDError("channel opened but no funding txid in response")
        return txid
