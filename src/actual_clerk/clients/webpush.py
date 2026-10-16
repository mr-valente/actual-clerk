"""Reach the phone app through the push endpoint it registered.

The companion app registers with a UnifiedPush distributor on the phone (the
ntfy app is one) and hands Clerk the endpoint the distributor issued, with
the Web Push keys the app generated for it. Clerk encrypts each message for
those keys (RFC 8291, `aes128gcm`) and POSTs the bytes to the endpoint; the
distributor wakes the app, which decrypts them and shows its own
notification.

Nothing in the message is needed for correctness. The app treats any push
as "look at Clerk now" and reads the open reviews over its own connection,
so a lost or garbled push costs at most the quarter hour until the app's
next scheduled check.
"""

from __future__ import annotations

import base64
import json
import os
from typing import Any
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from actual_clerk.config import Settings

# RFC 8291 encrypts one record; the receiver's buffer is 4096 bytes, less the
# 86-byte header, the 16-byte tag, and the padding delimiter.
MAX_PLAINTEXT = 3993
_RECORD_SIZE = 4096
_PADDING_DELIMITER = b"\x02"


class PushError(RuntimeError):
    """A bounded push delivery failure."""

    def __init__(self, message: str, *, status: int = 0):
        super().__init__(message)
        self.status = status

    @property
    def gone(self) -> bool:
        """The distributor no longer knows this endpoint; the phone must register again."""
        return self.status in (404, 410)


def b64url_decode(value: str) -> bytes:
    cleaned = value.strip().replace("=", "")
    return base64.urlsafe_b64decode(cleaned + "=" * (-len(cleaned) % 4))


def b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def valid_keys(public_key: str, auth_secret: str) -> bool:
    """Whether the phone's key material is a P-256 point and a 16-byte secret."""
    try:
        point = b64url_decode(public_key)
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
        return len(b64url_decode(auth_secret)) == 16
    except (ValueError, TypeError):
        return False


def encrypt(
    plaintext: bytes,
    *,
    receiver_public_key: bytes,
    auth_secret: bytes,
    salt: bytes | None = None,
    sender_private_key: ec.EllipticCurvePrivateKey | None = None,
) -> bytes:
    """Seal `plaintext` for the receiver, RFC 8291 with the aes128gcm content coding.

    The salt and the sender's ephemeral key are drawn fresh for every message;
    the parameters exist so the RFC's own test vector can be reproduced.
    """
    if len(plaintext) > MAX_PLAINTEXT:
        raise PushError(f"a push message may carry at most {MAX_PLAINTEXT} bytes")
    salt = salt or os.urandom(16)
    sender = sender_private_key or ec.generate_private_key(ec.SECP256R1())
    sender_public = sender.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    receiver = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), receiver_public_key)
    shared = sender.exchange(ec.ECDH(), receiver)
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth_secret,
        info=b"WebPush: info\x00" + receiver_public_key + sender_public,
    ).derive(shared)
    key = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=salt,
        info=b"Content-Encoding: aes128gcm\x00",
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(),
        length=12,
        salt=salt,
        info=b"Content-Encoding: nonce\x00",
    ).derive(ikm)
    header = (
        salt
        + _RECORD_SIZE.to_bytes(4, "big")
        + len(sender_public).to_bytes(1, "big")
        + sender_public
    )
    return header + AESGCM(key).encrypt(nonce, plaintext + _PADDING_DELIMITER, None)


class WebPushClient:
    """POST one small message to a device's UnifiedPush endpoint."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(15))

    async def close(self) -> None:
        await self.client.aclose()

    async def send(self, device: dict[str, Any], payload: dict[str, Any]) -> None:
        endpoint = str(device.get("push_endpoint") or "")
        if not endpoint:
            raise PushError("The device has no push endpoint")
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            # Delivered within the day or not at all: a review nudge from
            # yesterday is answered by the app's own check long before then.
            "TTL": "86400",
            "Urgency": "high",
            # Tells an ntfy server this is a UnifiedPush message, so a binary
            # body is passed through to the app rather than kept as a file.
            "X-UnifiedPush": "1",
        }
        public_key = str(device.get("push_key") or "")
        auth_secret = str(device.get("push_auth") or "")
        if public_key and auth_secret:
            body = encrypt(
                body,
                receiver_public_key=b64url_decode(public_key),
                auth_secret=b64url_decode(auth_secret),
            )
            headers["Content-Encoding"] = "aes128gcm"
            headers["Content-Type"] = "application/octet-stream"
        else:
            headers["Content-Type"] = "application/json"
        # A self-hosted ntfy server that refuses anonymous writes accepts
        # Clerk's own token; it is only ever sent to that server.
        token = self.settings.secret_value("ntfy_token")
        if token and _same_host(endpoint, self.settings.ntfy_url):
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self.client.post(endpoint, content=body, headers=headers)
        except httpx.RequestError as exc:
            raise PushError(f"push request failed: {exc}") from exc
        if response.status_code >= 400:
            raise PushError(
                f"the push endpoint returned {response.status_code}: {response.text[:300]}",
                status=response.status_code,
            )


def _same_host(endpoint: str, server: str) -> bool:
    try:
        return bool(server) and urlsplit(endpoint).netloc.lower() == urlsplit(server).netloc.lower()
    except ValueError:
        return False
