"""The nudge Clerk pushes to the phone app through its UnifiedPush endpoint."""

from __future__ import annotations

import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from actual_clerk.clients.webpush import (
    PushError,
    WebPushClient,
    b64url_decode,
    b64url_encode,
    encrypt,
    valid_keys,
)
from actual_clerk.config import Settings

# RFC 8291, section 5 and appendix A: the worked example, with the salt and the
# application server's key fixed so the output is exactly the RFC's.
RFC_PLAINTEXT = "V2hlbiBJIGdyb3cgdXAsIEkgd2FudCB0byBiZSBhIHdhdGVybWVsb24"
RFC_UA_PUBLIC = (
    "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4"
)
RFC_UA_PRIVATE = "q1dXpw3UpT5VOmu_cf_v6ih07Aems3njxI-JWgLcM94"
RFC_AS_PRIVATE = "yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"
RFC_AUTH = "BTBZMqHH6r4Tts7J_aSIgg"
RFC_SALT = "DGv6ra1nlYgDCS1FRnbzlw"
RFC_OUTPUT = (
    "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6Tlz"
    "AC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
)


def private_key(encoded: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(b64url_decode(encoded), "big"), ec.SECP256R1())


def decrypt(sealed: bytes, *, receiver: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """What the phone does with the bytes, written out so the test does not trust `encrypt`."""
    salt, record_size, key_length = sealed[:16], sealed[16:20], sealed[20]
    assert int.from_bytes(record_size, "big") == 4096
    sender_public = sealed[21 : 21 + key_length]
    body = sealed[21 + key_length :]
    sender = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), sender_public)
    receiver_public = receiver.public_key().public_bytes(
        Encoding.X962, PublicFormat.UncompressedPoint
    )
    shared = receiver.exchange(ec.ECDH(), sender)
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=auth_secret,
        info=b"WebPush: info\x00" + receiver_public + sender_public,
    ).derive(shared)
    key = HKDF(
        algorithm=hashes.SHA256(), length=16, salt=salt, info=b"Content-Encoding: aes128gcm\x00"
    ).derive(ikm)
    nonce = HKDF(
        algorithm=hashes.SHA256(), length=12, salt=salt, info=b"Content-Encoding: nonce\x00"
    ).derive(ikm)
    padded = AESGCM(key).decrypt(nonce, body, None)
    assert padded.endswith(b"\x02")
    return padded[:-1]


def test_the_rfc_8291_example_is_reproduced_exactly():
    sealed = encrypt(
        b64url_decode(RFC_PLAINTEXT),
        receiver_public_key=b64url_decode(RFC_UA_PUBLIC),
        auth_secret=b64url_decode(RFC_AUTH),
        salt=b64url_decode(RFC_SALT),
        sender_private_key=private_key(RFC_AS_PRIVATE),
    )
    assert b64url_encode(sealed) == RFC_OUTPUT
    assert (
        decrypt(sealed, receiver=private_key(RFC_UA_PRIVATE), auth_secret=b64url_decode(RFC_AUTH))
        == b"When I grow up, I want to be a watermelon"
    )


def test_a_fresh_salt_and_key_are_drawn_for_every_message():
    receiver = private_key(RFC_UA_PRIVATE)
    public = receiver.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    first = encrypt(b"hello", receiver_public_key=public, auth_secret=b64url_decode(RFC_AUTH))
    second = encrypt(b"hello", receiver_public_key=public, auth_secret=b64url_decode(RFC_AUTH))
    assert first != second
    assert decrypt(first, receiver=receiver, auth_secret=b64url_decode(RFC_AUTH)) == b"hello"


def test_a_message_too_large_for_one_record_is_refused():
    with pytest.raises(PushError):
        encrypt(
            b"x" * 4000,
            receiver_public_key=b64url_decode(RFC_UA_PUBLIC),
            auth_secret=b64url_decode(RFC_AUTH),
        )


def test_the_phone_s_keys_are_checked_before_they_are_stored():
    assert valid_keys(RFC_UA_PUBLIC, RFC_AUTH)
    assert not valid_keys("not-a-key", RFC_AUTH)
    assert not valid_keys(RFC_UA_PUBLIC, "short")


def push_client(settings: Settings, receive):
    client = WebPushClient(settings)
    client.client = httpx.AsyncClient(transport=httpx.MockTransport(receive))
    return client


async def test_a_device_with_keys_is_sent_an_encrypted_unifiedpush_message():
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"id": "m1"})

    settings = Settings(ntfy_url="https://ntfy.example", ntfy_topic="clerk", ntfy_token="tok")
    client = push_client(settings, receive)
    try:
        await client.send(
            {
                "push_endpoint": "https://ntfy.example/upAbc",
                "push_key": RFC_UA_PUBLIC,
                "push_auth": RFC_AUTH,
            },
            {"event": "review", "title": "Needs a category: Valve"},
        )
    finally:
        await client.close()
    [request] = requests
    assert request.headers["content-encoding"] == "aes128gcm"
    assert request.headers["x-unifiedpush"] == "1"
    assert request.headers["urgency"] == "high"
    # Clerk's own ntfy token opens a self-hosted server that refuses anonymous writes.
    assert request.headers["authorization"] == "Bearer tok"
    plain = decrypt(
        request.content, receiver=private_key(RFC_UA_PRIVATE), auth_secret=b64url_decode(RFC_AUTH)
    )
    assert json.loads(plain) == {"event": "review", "title": "Needs a category: Valve"}


async def test_the_ntfy_token_never_goes_to_another_server():
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    settings = Settings(ntfy_url="https://ntfy.example", ntfy_topic="clerk", ntfy_token="tok")
    client = push_client(settings, receive)
    try:
        await client.send({"push_endpoint": "https://push.elsewhere/upAbc"}, {"event": "sync"})
    finally:
        await client.close()
    [request] = requests
    assert "authorization" not in request.headers
    # Without keys the message goes as it is; the phone treats any push as a nudge.
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"event": "sync"}


async def test_an_endpoint_the_distributor_forgot_is_reported_as_gone():
    client = push_client(Settings(), lambda request: httpx.Response(404, text="topic gone"))
    try:
        with pytest.raises(PushError) as refused:
            await client.send({"push_endpoint": "https://ntfy.sh/upAbc"}, {"event": "sync"})
    finally:
        await client.close()
    assert refused.value.gone
    client = push_client(Settings(), lambda request: httpx.Response(500, text="oops"))
    try:
        with pytest.raises(PushError) as failed:
            await client.send({"push_endpoint": "https://ntfy.sh/upAbc"}, {"event": "sync"})
    finally:
        await client.close()
    assert not failed.value.gone
