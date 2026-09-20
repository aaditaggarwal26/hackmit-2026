"""The targeted Ed25519 hybrid: the ground signs grant/revoke/tx_ack, everyone verifies.

The property that matters, and that plain HMAC cannot give: a holder of the SHARED HMAC key
(a satellite whose key leaked, say) can still forge messages the HMAC alone protects, but it
CANNOT forge a state-changing ground command, because it lacks the ground's private seed.
"""

from __future__ import annotations

from orbit.protocol import auth, ed25519
from orbit.protocol import messages as M

SHARED = b"demo-shared-hmac-key"
SEED = bytes(range(32))
PUB = ed25519.secret_to_public(SEED)

EXAMPLES = dict(M.examples())


def _ground() -> auth.Policy:
    return auth.Policy(key=SHARED, sign_seed=SEED)


def _verifier() -> auth.Policy:
    return auth.Policy(key=SHARED, ground_pubkey=PUB)


def _leaked_key_attacker() -> auth.Policy:
    """Has the shared HMAC key but not the ground's private seed."""
    return auth.Policy(key=SHARED)


def test_ground_signed_control_verifies() -> None:
    for name in ("grant", "revoke", "tx_ack"):
        signed = _ground().sign(EXAMPLES[name].encode())
        canon_fields = auth.canonical(signed)[1]
        assert canon_fields.sig_lit is not None, f"{name} should carry a sig"
        assert canon_fields.auth_lit is not None, f"{name} should also carry the HMAC tag"
        assert _verifier().check(signed) == "", f"{name} must verify"


def test_leaked_hmac_key_cannot_forge_a_grant() -> None:
    # The attacker holds the shared key, so it can produce a valid HMAC tag, but no `sig`.
    forged = _leaked_key_attacker().sign(EXAMPLES["grant"].encode())
    assert auth.canonical(forged)[1].sig_lit is None  # HMAC only
    assert _verifier().check(forged) == "no_sig"  # rejected: a grant needs the ground's signature


def test_wrong_seed_is_rejected() -> None:
    other = auth.Policy(key=SHARED, sign_seed=bytes(range(1, 33)))
    forged = other.sign(EXAMPLES["grant"].encode())
    assert _verifier().check(forged) == "bad_sig"


def test_tampered_body_is_rejected() -> None:
    signed = _ground().sign(EXAMPLES["grant"].encode())
    # Flip the granted item's payload: the HMAC or the signature (both over the same canonical) fails.
    tampered = signed.replace(b'"item_id":14', b'"item_id":15')
    assert tampered != signed
    assert _verifier().check(tampered) != ""


def test_non_signed_control_needs_only_hmac() -> None:
    # offers_open is a control type but NOT state-changing, so it carries no sig; HMAC suffices.
    signed = _ground().sign(EXAMPLES["offers_open"].encode())
    assert auth.canonical(signed)[1].sig_lit is None
    assert _verifier().check(signed) == ""


def test_satellite_message_unaffected_by_the_ground_key() -> None:
    # A bid is not a ground command; a verifier holding the ground pubkey still accepts it on HMAC.
    signed = _leaked_key_attacker().sign(EXAMPLES["bid"].encode())
    assert _verifier().check(signed) == ""


def test_off_by_default_is_passthrough() -> None:
    # No keys anywhere: signing is a no-op and checking accepts, exactly as before this existed.
    data = EXAMPLES["grant"].encode()
    assert auth.OFF.sign(data) == data
    assert auth.OFF.check(data) == ""


def test_hmac_off_but_signature_required() -> None:
    # A deployment may run asymmetric-only (no shared key). A grant then needs a valid sig and nothing else.
    ground = auth.Policy(sign_seed=SEED)
    verifier = auth.Policy(ground_pubkey=PUB)
    signed = ground.sign(EXAMPLES["grant"].encode())
    assert verifier.check(signed) == ""
    assert verifier.check(EXAMPLES["grant"].encode()) == "no_sig"  # unsigned grant rejected
