"""Content compatibility gate for PvZ Heroes Casual matchmaking.

Compares the SHA-256 digest of the entire decompressed UnityFS serialized
card_data stream (NOT the outer file bytes or a manually provided version).
Fail closed if missing, unknown, or malformed. This is a desync filter, not
cryptographic proof of a trustworthy client (client headers can be spoofed).
"""
import hmac
import json
import re
from pathlib import Path

from flask import request, g

HEADER = 'X-PvZH-Content-SHA256'
ALGORITHM = 'unityfs-uncompressed-stream-sha256-v1'
CONFIG_PATH = Path(__file__).with_name('card_content_gate.json')
HEX256 = re.compile(r'[a-f0-9]{64}\Z')


def load_policy():
    with CONFIG_PATH.open(encoding='utf-8') as f:
        policy = json.load(f)
    enabled = policy.get('enabled', True)
    if type(enabled) is not bool:
        raise RuntimeError('card_content_gate.json: enabled must be true or false')
    if not enabled:
        return False, ()
    if policy.get('algorithm') != ALGORITHM:
        raise RuntimeError('Unsupported card content fingerprint algorithm')
    digests = policy.get('allowed_digests')
    if not isinstance(digests, list) or not digests or any(
        not isinstance(x, str) or not HEX256.fullmatch(x) for x in digests
    ):
        raise RuntimeError('card_content_gate.json needs valid digest(s)')
    return True, tuple(digests)


# Load once at worker startup (update policy => restart server).
ENABLED, ALLOWED = load_policy()


def verify_content_header():
    if not ENABLED:
        # Use one shared queue and session key for all clients while disabled.
        g.pvzh_content_digest = 'card-data-gate-disabled'
        return None
    reported = request.headers.get(HEADER, '')
    if not HEX256.fullmatch(reported):
        return {'ty': 'MatchFailed', 'error': 'CardDataNotInstalled'}, 403
    if not any(hmac.compare_digest(reported, valid) for valid in ALLOWED):
        return {'ty': 'MatchFailed', 'error': 'CardDataMismatch'}, 409
    g.pvzh_content_digest = reported
    return None

