# Security model, and the flight path

## Framing: an MVP of a product, not a demo toy

The corpus in this repo is public NASA GIBS MODIS imagery — **placeholder data for the MVP**.
The system is designed as if it were the real product: an Earth-observation constellation whose
imagery is commercial, licensed, or capability-revealing, and whose command path must not be
steerable by anyone but its ground station. So the security model is built for that product, and
scoped down to what an MVP can carry — implemented where it is checkable today, with the honest
gaps named.

Two distinct assets, and they are protected differently:

* **Integrity and authenticity of the control path** — the primary asset. A stranger on the RF
  link must not be able to say `grant`/`revoke`/`tx_ack` and steer the arbiter, and a byte
  flipped in flight must never be acted on. This is *always* the threat, public data or not.
* **Confidentiality of the imagery** — a product asset. Real downlinked imagery is sensitive;
  the payload can be encrypted so it is not readable on the air. (For this repo's *public*
  corpus it is off by default, which also keeps the bus log human-readable during a demo — but
  it is one config flag away, and it is real.)

Identity is deliberately public (a node's hostname is in every `from` field — that is how a
fourth satellite joins with zero config), and key material never travels: pre-shared and
per-device keys are provisioned out of band, never on the bus.

## What is implemented

Every layer is independent and **off by default** — an all-default configuration is a
pass-through, so the deterministic sim digest and every pre-existing test are unchanged. A
deployment turns layers on with environment variables. Python/ground side in
`orbit/protocol/{auth,ed25519,codec}.py`; the firmware halves are in
`firmware/satellite_esp32/`.

1. **HMAC-SHA256-128 over a canonical byte string** (`auth` field). Every node MACs everything
   it sends and drops anything that does not verify — on the raw bytes, before decode and before
   the duplicate filter, so a forgery never reaches the parser and a forged sequence number
   cannot move a real sender's replay watermark. The canonical form copies scalar tokens
   verbatim so ArduinoJson and Python cannot disagree; the two implementations are held
   byte-for-byte by `tests/test_crypto_parity.py`. Truncated to 128 bits (RFC 4868) so a
   worst-case `tx_chunk` still fits the datagram. This defeats an **outsider** completely.

2. **Sender pinning** (optional) — satellites obey control messages only from the configured
   ground hostname; the ground accepts satellite traffic only from an allowlist.

3. **Anti-replay** — with the MAC on, a datagram's `seq` must beat that sender's last *verified*
   sequence number.

4. **Ground Ed25519 signatures on state-changing commands** (`grant`/`revoke`/`tx_ack`, the
   `sig` field), over the same canonical bytes as the HMAC. This closes the one gap HMAC cannot:
   the HMAC key is *shared*, so anyone who extracted it from a satellite could forge a grant.
   Only the ground holds the Ed25519 private seed, so a leaked shared key can still forge
   `offers_open` or a satellite's `bid`, but not a command that makes a satellite transmit or
   drop a frame. Pure-Python RFC 8032, held to the RFC's known-answer vectors
   (`tests/test_ed25519.py`). *Status:* implemented and tested on the ground/Python side
   (`tests/test_ground_sig.py`); the **ESP32 verify** (`orbit_crypto.h` gaining Ed25519 +
   SHA-512, held to this module by `crypto_host` parity, then validated on hardware) is the
   remaining port. Until it lands, the real boards act on a grant via HMAC alone and the
   signature is enforced only by Python verifiers.

5. **AES-256-GCM payload confidentiality** (`orbit/protocol/codec.py`). The frame bytes are
   sealed with an AEAD — one pass gives confidentiality *and* integrity, so the GCM tag replaces
   any need for a separate MAC over the frame. Layered **compress-then-encrypt** (ciphertext
   does not compress); `enc` records the chain (`zlib+gcm`), and the sealed blob is
   self-describing on the wire (`nonce || ciphertext || tag`) so it rides the existing chunking
   with no new field. Only the bulk frame payload is sealed — control and telemetry JSON stay in
   the clear, so the bus stays readable and only the sensitive thing is hidden. A 12-byte random
   nonce per frame (safe under a static key without the counter-reuse trap). Enabling it is
   transparent to arbitration: the ground unseals and reconstructs the exact frame it would have
   in the clear (`tests/test_codec_crypto.py`). *Status:* implemented and tested on the
   Python/ground/sim side; the **ESP32 seal** (mbedtls hardware AES-GCM) is the remaining port.

Keys, all from the environment and redacted from every log/run file: `ORBIT_AUTH_KEY` (shared
HMAC), `ORBIT_GROUND_SIGN_KEY` (ground's Ed25519 seed, ground only), `ORBIT_GROUND_PUBKEY`
(ground's public key, on every verifier), `ORBIT_PAYLOAD_KEY` (AES-GCM key, 16/24/32 bytes hex).
Empty disables the layer.

## What is NOT implemented yet (named, not hidden)

* **Firmware ports** of the Ed25519 verify and the AES-GCM seal — the two items above. The
  Python side is the executable spec; the firmware mirrors it and is held by host-parity, the
  same pattern used for the scoring kernel and the HMAC canonicaliser.
* **Per-satellite keypairs (uplink identity).** Satellites verify the ground but do not sign
  their own telemetry, so a holder of the shared key can still forge a *satellite's* bid. For an
  MVP with boards you physically control, an attacker who has extracted a board's key is not in
  scope. Per-node signing keys are the next step (see flight path).
* **Session-key rotation / forward secrecy.** The payload key today is pre-shared and static.
  A static AES key is safe with per-frame random nonces, but does not rotate.

## The flight path

The same node keys extend cleanly to what a real constellation needs:

* **Per-node identity.** Each satellite generates its own Ed25519 keypair on first boot (the
  ESP32-S3 has a hardware RNG); the private key never leaves the board (NVS). The ground learns
  each public key by provisioning or trust-on-first-use and allowlists it. Now a compromised
  satellite can forge only its own bids, and every message is attributable.

* **Rotating, forward-secret session keys for confidentiality.** Instead of a static
  `ORBIT_PAYLOAD_KEY`, derive the AES-GCM session key from an **X25519 ECDH** exchange (each
  node's Ed25519 key converts to its Montgomery/X25519 form), rotated **per pass or every few
  hours**. The session key never travels the air, an adversary who captured every transmission
  cannot recompute it, and a key compromised later does not expose past passes (forward
  secrecy). This is, in outline, CCSDS SDLS / a Signal-style handshake.

  Two real costs make this a flight feature, not an MVP one: (a) **multicast group keying** —
  pairwise session keys break "one datagram, everyone hears it", so it needs a group session key
  with rekeying on membership change; and (b) GCM nonce management across reboots, which the
  per-pass key rotation conveniently solves. Forward secrecy protects *past* traffic — which for
  this repo's public corpus there is no reason to protect, another sign it belongs to the flight
  system.

## The one-line summary for the pitch

We authenticate the control path always (HMAC on everything, Ed25519 on the ground's commands,
so even a leaked shared key can't forge a grant) and encrypt the imagery payload with AES-256-GCM
when it is sensitive — off for this public MODIS corpus, one flag away for real data. The same
node keys extend to per-node identity and X25519 ECDH rotating session keys with forward secrecy
for a production constellation: a threat model we have built for and can scale into, not one we
fake by encrypting public pictures.
