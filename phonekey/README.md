# phonekey: the paired phone as a key

Status: desktop and OS sides implemented and unit-tested on a build host.
**Never run against a real phone, a real PAM stack, or a booted DesOS image.**
The Android side is not written; section 4 is its specification.

A phone paired with the desktop through DES Connect can do three things. Each is
a separate switch, each is **off by default**, and each is switched on per
paired device:

| Capability | What it does | What it needs |
|---|---|---|
| **walk-away lock** | Locks the session when the phone has been unreachable for a set time. | Pairing only. |
| **unlock** | The phone answers a lock-screen unlock request for a session that is already signed in. | An enrolled signing key. |
| **approve** | The phone approves an administrator action (sudo; polkit only if asked for) instead of a typed password. | An enrolled signing key, plus root running `desos-phonekey enable`. |

Rules that hold everywhere:

* **The password always works.** Every phone path is an addition in front of the
  password path. Any failure, timeout, refusal or error falls back to it.
* **The phone never learns or stores the password**, and neither does any code
  here: the PAM rule does not use `expose_authtok`, and the daemon has no
  password input.
* **The first sign-in after boot always needs the password** at the greeter. The
  login keyring is unlocked by it and nothing here can stand in for that. The
  phone can only unlock the lock screen of a session that is already signed in,
  and (by policy, section 6.2) only after the password has been typed in that
  session and not too long ago.
* **Presence never unlocks or approves.** Absence may lock. Unlock and approve
  are signatures over a fresh challenge, not "the phone is nearby".

Contents: 1 where things live · 2 keys · 3 the signed message · 4 wire protocol
(the Android spec) · 5 desktop behaviour, limits and settings · 6 the three
capabilities end to end · 7 threat model · 8 what is and is not verified ·
9 decisions wanted.

---

## 1. Where things live

| Piece | Where | Runs as |
|---|---|---|
| Protocol types, verification, limits, audit | `des-ui/connect/src/phonekey.rs` | library |
| Transport, presence, walk-away lock request, IPC | `des-ui/connect/src/service.rs` (`des-connectd`) | the user |
| Commands | `des-connect-ctl phonekey-*` | the user |
| Administrator approval: PAM helper and `enable|disable|status` | `desos/phonekey/rootfs/usr/sbin/desos-phonekey` | root |
| Shared test vectors | `desos/phonekey/testvectors.json` = `des-ui/connect/tests/phonekey_vectors.json` | - |
| Phone side | DES Connect for Android, new `des.phonekey.v1` plugin | not written |

Two verifiers exist on purpose. The daemon verifies every answer (for the audit
log, limits and `des-connect-ctl`). For **approve**, the decision is made again
by the root helper, which makes its own nonce and checks the signature against a
root-owned copy of the key; it treats the user's daemon as an untrusted pipe.

## 2. Keys

### 2.1 What Connect already has, and why it is not used for signing

Read from `des-ui/connect/src/identity.rs`, `pairing.rs`, `service.rs` and the
Android fork's `helpers/security/RsaHelper.kt` / `SslHelper.kt`:

* **Desktop identity**: an RSA-2048 key and a self-signed X.509 certificate
  (CN = device ID, O = "DES UI", 10 years), in
  `~/.local/state/des-ui/connect/identity.json`, owner-only.
* **Phone identity**: an EC P-256 key (RSA-2048 on installs that predate the EC
  change) and a self-signed certificate. It is generated in software and the
  private key is stored base64-encoded in the app's default `SharedPreferences`.
* **Pairing** pins the peer's whole certificate (`peers.json` keeps its DER; the
  pin is its SHA-256). Sessions are mutual TLS 1.2 and a changed certificate is
  refused. The pairing code shown to the user is 4 bytes of
  SHA-256(sorted public keys ‖ timestamp).

That material gives an **authenticated, confidential channel to a specific
paired device**, and phone-key relies on it for exactly that. The phone's
identity key is **not fit to sign approvals**:

1. It is an exportable software key in app storage. A backup, a rooted phone or
   malware in the app's sandbox can copy it, and a copy signs forever.
2. Nothing ties its use to the person: any code in the app can sign with it with
   the phone locked in a drawer. There is no way to require a fingerprint.
3. It already signs TLS handshakes. Using one key in two protocols invites
   cross-protocol confusion, and it may be RSA on older installs.

### 2.2 The enrolled phone key

So the phone enrols a **second key, only for this**:

* ECDSA over **NIST P-256** with **SHA-256**. Nothing else is accepted.
* Generated in the **Android Keystore**, non-exportable, StrongBox when the
  device has one, otherwise the TEE.
* **User authentication required for every single use**: timeout 0, Class 3
  ("strong") biometric only, no device-PIN fallback (the desktop password is the
  fallback), and invalidated when a new biometric is enrolled. Exact
  `KeyGenParameterSpec` in section 4.7.
* One key per paired desktop. Its public half is sent as a DER
  SubjectPublicKeyInfo (always 91 bytes for P-256).
* **Key ID** = SHA-256 of that SubjectPublicKeyInfo. **Fingerprint** = the first
  8 bytes of the key ID as 16 uppercase hex digits in groups of four
  (`A7AE-9D8E-EBDE-E76C`). The phone and the desktop both show it at enrolment,
  and `desos-phonekey enable` shows it again.

**What the desktop can and cannot verify about the biometric.** It verifies that
the answer was signed by the enrolled private key over this exact request. It
**cannot** verify that a fingerprint was presented: that is enforced by the
phone's keystore, and the desktop takes it on trust that the key was created
with the parameters above. A modified app, or a phone whose keystore is
compromised, could create a key without that requirement and the desktop would
not know. Android key attestation could prove the key's properties to the
desktop; v1 carries an optional `attestation` field at enrolment so phones can
start sending it, but **the desktop does not check it** (decision 5 in
section 9).

Where the public key is stored:

* `~/.local/state/des-ui/connect/peers.json`, in the paired device's record:
  `"phonekey": {"public_key": <base64 SPKI>, "enrolled_at": <unix s>, "unlock": false, "approve": false}`.
  Unpairing deletes the whole record.
* For **approve** only, a root-owned copy in
  `/etc/desos/phonekey/users/<user>.json`, written by `desos-phonekey enable`.
  This is the copy that decides administrator approvals, because the first one
  can be rewritten by any program running as the user.

## 3. The signed message

Every signature (enrol, unlock, approve) is over these bytes, and only these:

```
message = "DES-PHONEKEY-v1" 0x00                  16 bytes, domain separation
        ‖ F(purpose)        ASCII: "enrol" | "unlock" | "approve"
        ‖ F(desktop_id)     UTF-8, the desktop's DES Connect device ID
        ‖ F(desktop_cert)   32 raw bytes: SHA-256 of the desktop's TLS certificate (DER)
        ‖ F(phone_id)       UTF-8, the phone's DES Connect device ID
        ‖ F(phone_cert)     32 raw bytes: SHA-256 of the phone's TLS certificate (DER)
        ‖ F(key_id)         32 raw bytes: SHA-256 of the signing key's SubjectPublicKeyInfo
        ‖ F(request_id)     16 raw bytes
        ‖ F(nonce)          32 raw bytes
        ‖ F(user)           UTF-8 login name of the requesting user
        ‖ F(action)         UTF-8, the exact text shown to the user
        ‖ U64(issued_at) ‖ U64(expires_at)        Unix seconds, big-endian

F(x)   = U32(length of x in bytes, big-endian) ‖ x
U32/U64 = unsigned big-endian integers
signature = ECDSA(P-256, SHA-256) over message, ASN.1 DER (r, s), as
            java.security.Signature "SHA256withECDSA" produces
```

Rules:

* **Each side builds the message itself.** `desktop_id`, `desktop_cert`,
  `phone_id` and `phone_cert` are never taken from a packet: the phone uses the
  certificates of the TLS session the request arrived on and its own identity;
  the desktop uses its own certificate and the pinned one. This binds a
  signature to one pairing: it does not verify on another desktop, or after
  either side's identity changed.
* The phone builds `user` and `action` from the fields **it displays**. It must
  never sign bytes it did not show.
* `user`: 1-64 bytes of `[A-Za-z0-9._-]`, not starting with `-`.
* `action`: 1-512 bytes of UTF-8, one line, no leading or trailing whitespace,
  no control characters, and none of U+061C, U+200E, U+200F, U+2028, U+2029,
  U+202A-U+202E, U+2066-U+2069, U+FEFF (nothing that can hide or reorder what is
  shown). A request that breaks these rules is never sent, and the phone must
  refuse one that does.
* `expires_at − issued_at` is 1-120 seconds.
* For `enrol`, `key_id` is the ID of the key being enrolled, so the signature
  proves possession of it.

`testvectors.json` holds four requests with their exact message bytes and valid
signatures. The Rust and Python implementations are tested against it and
against each other; the Android implementation must reproduce every `message`
and verify every `signature`.

## 4. Wire protocol (specification for the phone)

### 4.1 Transport and negotiation

* One packet type in both directions: **`des.phonekey.v1`**, an ordinary DES
  Connect (KDE Connect protocol 8) JSON packet on the paired TLS session, with
  no payload.
* A side supports phone-key if it lists `des.phonekey.v1` in **both**
  `incomingCapabilities` and `outgoingCapabilities` of its **encrypted**
  identity (the one exchanged after the TLS handshake). The desktop always lists
  it; that says nothing about what is enabled.
* The desktop sends a phone-key packet only to a **paired** peer that lists the
  capability, and ignores phone-key packets from any other peer. An old phone
  therefore never receives one, and an old desktop drops them like any unknown
  packet type. Neither needs to know the other is old.
* Every body carries `"v": 1` and `"op"`. A receiver **ignores** a body whose
  `v` is not 1, whose `op` it does not know, or that has a payload, and ignores
  fields it does not know. A later incompatible protocol will be a new packet
  type and capability (`des.phonekey.v2`) listed alongside this one.
* Encodings: `request_id` is 32 lowercase hex digits; `key_id` is 64 lowercase
  hex digits; `nonce`, `public_key` and `signature` are standard base64 with
  padding; times are JSON integers in Unix seconds.

### 4.2 Operations

Desktop → phone:

| `op` | Fields | Meaning |
|---|---|---|
| `enrol` | `purpose:"enrol"`, `request_id`, `nonce`, `user`, `action`, `issued_at`, `expires_at` | Create a key for this desktop and prove possession. |
| `request` | `purpose:"unlock"\|"approve"`, `request_id`, `nonce`, `user`, `action`, `issued_at`, `expires_at`, `key_id` | Ask the person to approve exactly `action`. |
| `cancel` | `request_id`, `reason:"timeout"\|"cancelled"` | Stop showing that request. No reply. |
| `presence` | `seq` (integer, increasing per session) | "Are you there?" Sent only while walk-away is on for this phone. |
| `revoke` | `reason` | The desktop forgot the key. Delete yours. No reply. |

Phone → desktop:

| `op` | Fields | Meaning |
|---|---|---|
| `enrolled` | `request_id`, `decision:"approved"\|"denied"`; if approved: `public_key`, `signature`; optional `attestation` (array of base64 DER certificates, leaf first) | Answer to `enrol`. |
| `response` | `request_id`, `decision:"approved"\|"denied"`; if approved: `signature`; optional `reason` (free text, logged only) | Answer to `request`. |
| `present` | `seq` (echoed) | Answer to `presence`. |
| `revoke` | - | The phone deleted its key (user action, or the keystore invalidated it). |

Example (`request` and its answer; whitespace added):

```json
{"id": 1794480002000, "type": "des.phonekey.v1", "body": {
  "v": 1, "op": "request", "purpose": "approve",
  "request_id": "03030303030303030303030303030303",
  "nonce": "kq0Jc1Wv…=", "user": "coxy",
  "action": "sudo apt install 'café tools' --yes",
  "issued_at": 1794480002, "expires_at": 1794480022,
  "key_id": "a7ae9d8eebdee76cc648329d1554b9e1607e85a6adaa49c2949020b8fd3093ef"}}

{"id": 1794480009000, "type": "des.phonekey.v1", "body": {
  "v": 1, "op": "response", "request_id": "03030303030303030303030303030303",
  "decision": "approved", "signature": "MEUCIQ…"}}
```

### 4.3 Request state machine (desktop side)

```
            issue                    answer with this request_id
 (none) ───────────▶ PENDING ─────────────────────────────────▶ DECIDED
                        │   approved + valid signature → approved
                        │   denied                       → denied
                        │   anything else                → bad_signature / malformed / no_key
                        │ deadline (monotonic)           → timeout
                        │ requester went away            → cancelled   (+ cancel sent)
                        │ device unpaired / key revoked  → revoked
                        ▼
                     DECIDED: handed to the requester exactly once, then forgotten
```

* At most **one pending request per phone** and four in total. A second request
  is refused (`busy`), not queued.
* **Any answer naming the pending `request_id` uses the request up**, valid or
  not. There is no second try against the same nonce.
* An answer whose `request_id` matches nothing pending (a replay, a late answer,
  a duplicate) is dropped and logged as `unknown_request`. An answer from a
  different paired device than the one asked is dropped as `wrong_device` and
  does not use the request up.
* The key and the certificate pin are looked up **when the answer arrives**, so
  unpairing or revoking refuses an answer already in flight.
* The deadline is `expires_at − issued_at` seconds on the desktop's monotonic
  clock from the moment of issue. The wall-clock fields are for the signature
  and the phone's display; a wrong desktop clock cannot extend a request.
* A `request_id` is never accepted twice (the last 1024 are remembered).

### 4.4 What the phone must do for `enrol` and `request`

1. Accept only on a paired, trusted session, with the plugin enabled by the user
   in the app (off by default on the phone too).
2. Validate before showing anything: `v == 1`; `purpose` is known and matches
   `op`; `request_id`, `nonce` well-formed; `user` and `action` obey section 3;
   `0 < expires_at − issued_at ≤ 120`; `|phone clock − issued_at| ≤ 300 s`; for
   `request`, `key_id` equals the ID of the key held for this desktop. On any
   failure answer `denied` (and, if the key for this desktop does not exist,
   also send `revoke`). Never display unvalidated text.
3. One request on screen at a time. Answer `denied` (`reason:"busy"`) to another.
4. Show a notification, and on tap a full-screen prompt containing, verbatim:
   the desktop's name, the purpose, `user`, and `action`. Start a timer for
   `expires_at − issued_at` seconds from receipt; when it ends, or on `cancel`
   with this `request_id`, dismiss everything and send nothing.
5. On **Approve**: build the message of section 3 from the displayed fields and
   the session's certificates, and sign it inside a `BiometricPrompt` bound to
   the key (`CryptoObject(Signature)`). The biometric prompt's own text must
   repeat the purpose and `action`. On success send the answer; on biometric
   failure or cancel send `denied`.
6. On **Deny**: send `decision:"denied"`.
7. Never: sign without a prompt, sign text that was not shown, keep a request
   past its timer, offer "always allow" / "trust for N minutes", or approve
   from a wearable or a notification action without the biometric.

For `enrol` additionally: generate the key pair first (replacing any earlier key
for this desktop), show the fingerprint of the new key on the confirmation
screen and keep it visible afterwards in the device's settings page, and answer
with `public_key`. If no Class 3 biometric is enrolled on the phone, answer
`denied` and tell the user why.

### 4.5 `presence`

Answer `{"v":1,"op":"present","seq":<same seq>}` immediately from the background
service. No UI, no notification, no wake lock beyond what the connection already
holds. Answer only the most recent `seq` once; the desktop ignores anything
else. The answer needs no signature: it rides the pinned TLS session and can
only ever postpone a lock.

### 4.6 `revoke`, unpairing, key loss

* On `revoke` from the desktop, or when the pairing is removed on either side,
  delete the keystore key for that desktop.
* If the keystore reports the key permanently invalidated (new biometric
  enrolled, lock screen removed), delete it and send `revoke`. The desktop then
  forgets its copy; the user must enrol again **from the desktop**.
* The phone never initiates enrolment.

### 4.7 Keystore parameters

```kotlin
KeyGenParameterSpec.Builder("des.phonekey." + desktopDeviceId, KeyProperties.PURPOSE_SIGN)
    .setAlgorithmParameterSpec(ECGenParameterSpec("secp256r1"))
    .setDigests(KeyProperties.DIGEST_SHA256)
    .setUserAuthenticationRequired(true)
    .setUserAuthenticationParameters(0, KeyProperties.AUTH_BIOMETRIC_STRONG)   // API 30+
    // API 28-29: .setUserAuthenticationValidityDurationSeconds(-1)
    .setInvalidatedByBiometricEnrollment(true)
    .setUnlockedDeviceRequired(true)
    .setIsStrongBoxBacked(true)        // retry without on StrongBoxUnavailableException
    .setAttestationChallenge(nonce)    // the enrol nonce; chain goes in "attestation"
    .build()
```

`public_key` is `keyPair.public.encoded` (X.509 SubjectPublicKeyInfo, 91 bytes).
The desktop refuses anything that is not exactly a P-256 SPKI with an
uncompressed point on the curve.

## 5. Desktop behaviour

### 5.1 Settings (all off by default)

Per paired device, kept in the daemon's owner-only trust store and changed only
through the daemon, **only while the session is unlocked**:

| Key | Values | Set with |
|---|---|---|
| `walkaway` | `false` / `true` | `des-connect-ctl phonekey-set DEVICE walkaway on\|off [SECONDS]` |
| `walkaway_seconds` | 30-3600, default 120 | same |
| `phonekey.unlock` | `false` / `true` | `des-connect-ctl phonekey-set DEVICE unlock on\|off` |
| `phonekey.approve` | `false` / `true` | `des-connect-ctl phonekey-set DEVICE approve on\|off` |

`unlock` and `approve` need an enrolled key (`des-connect-ctl phonekey-enrol
DEVICE`, confirmed on the phone). A new or re-enrolled key starts with both off.
Only one device may have `walkaway` on. A settings UI is not built.

System side (root): `/etc/desos/phonekey/users/<user>.json` exists only after
`desos-phonekey enable --user <user>`; `/etc/desos/phonekey/phonekey.conf` holds
`timeout_seconds` (20), `max_failures` (3), `lockout_seconds` (600),
`cooldown_seconds` (60), `max_requests_per_10_minutes` (10).

### 5.2 Commands

```
des-connect-ctl phonekey-status
des-connect-ctl phonekey-presence                  # exit 0 only if the walk-away phone is present
des-connect-ctl phonekey-enrol DEVICE_ID
des-connect-ctl phonekey-set DEVICE_ID walkaway|unlock|approve on|off [SECONDS]
des-connect-ctl phonekey-revoke DEVICE_ID
des-connect-ctl phonekey-approve [--device ID] [--ttl S] ACTION TEXT...   # exit 0 only on a verified signature
des-connect-ctl phonekey-unlock-check [--device ID]                       # prints the decision; unlocks nothing

desos-phonekey enable --user NAME [--device ID] [--service sudo] [--service polkit-1] [--yes]
desos-phonekey disable [--user NAME]
desos-phonekey status [--json]
```

Daemon IPC (one JSON line each way on the owner-only control socket): `op` is
`phonekey_status`, `phonekey_set` (`capability`, `enabled`, `seconds`),
`phonekey_enrol`, `phonekey_revoke`, or `phonekey_request` (`purpose`, `action`,
optional `user`, `ttl`, and optionally the caller's own `request_id`, `nonce`,
`issued_at`, `expires_at`). `phonekey_request` blocks until the decision and
returns everything a verifier needs: the challenge fields, `decision`,
`signature`, `public_key`, `key_id`, `desktop_id`, `desktop_cert`, `phone_cert`.
A caller that supplies its own challenge gets a signature over exactly that
challenge, which is how the root helper stays independent of the daemon.

### 5.3 Limits, lockout, audit, revocation

Daemon, per phone:

* requests at least 2 s apart, at most 6 a minute and 30 an hour;
* 3 failures in a row (denied, timed out, invalid) → no requests for 5 minutes;
* 5 invalid signatures → `unlock` and `approve` are switched off for that device
  until the user switches them on again from an unlocked session;
* `approve` requests are made only while the desktop session is **unlocked**,
  `unlock` requests only while it is **locked**; settings and enrolment only
  while unlocked;
* a request may name only the session's own user (`wrong_user` otherwise).

Root helper, per user: after any failure the phone is not asked again for 60 s
(so retyping a wrong password does not prompt the phone a second time); 3
failures in a row → 10 minutes without phone prompts; at most 10 prompts in 10
minutes. A daemon that could not ask at all (phone away, capability off) counts
as nothing. The password prompt is unaffected by all of this.

Audit: the daemon appends one JSON line per request, refusal, decision, setting
change, enrolment, revocation and walk-away lock to
`~/.local/state/des-ui/connect/phonekey-audit.log` (owner-only, rotated at
256 KiB). The root helper logs every request and decision to the auth log
(`desos-phonekey`, facility authpriv) and to `/var/log/desos-phonekey.log`
(root-only, rotated at 1 MiB). Both include the action text.

Revocation:

* **Unpairing** (either side) deletes the device record with its key and
  switches, cancels anything pending, and from that instant the daemon neither
  sends requests to nor believes answers from that device. `phonekey-revoke`
  does the same for the key alone.
* The **root record** for approve is removed by `desos-phonekey disable [--user]`.
  Unpairing does **not** remove it (the daemon is not root). After unpairing
  alone, approvals already fail in practice, because the daemon will not carry
  them; but the root record still trusts that key until `disable` is run. See
  threat T1 and decision 7.

## 6. The three capabilities end to end

### 6.1 Walk-away lock

While `walkaway` is on for a device and it is connected, the daemon sends
`presence` every 10 s. A `present` answer to the newest `seq` marks the phone
seen. Every 2 s the daemon evaluates one rule (`phonekey::Walkaway::tick`):

* session locked or no shell → disarm, do nothing;
* phone seen within `walkaway_seconds` → arm;
* otherwise, if armed → disarm and **ask the shell to lock**, once.

The lock request is the line `lock` on the shell's private control socket: the
same request `des-ui-ctl lock` makes. Consequences worth knowing: the phone must
have been present during the current unlocked period for its absence to lock
(unlocking with the password while the phone is away does not re-lock at once,
and a phone that is never there never locks); a phone on a flaky network locks
the session after the grace period; the normal idle lock is unaffected.

**Presence cannot unlock.** `Walkaway` has no output but "lock now"; the daemon
has no code that sends the shell anything but `state` and `lock`; and the
shell's control vocabulary has no unlock verb at all. An attacker who fakes or
relays presence can at most keep the session from auto-locking, which is the
state it would be in with the feature off.

### 6.2 Unlock (implemented up to the decision; the lock screen is not wired)

Implemented and tested: the `unlock` request, the phone's signed answer, its
verification in the daemon and again in the caller
(`des-connect-ctl phonekey-unlock-check`), the rule that unlock requests exist
only for a locked session, and `phonekey::UnlockPolicy`, which says when a lock
screen may accept a phone answer at all:

* never until the password has been accepted in this session (so never for the
  first unlock after sign-in, and never for a session that started locked
  without one);
* not once 12 hours (configurable 1-72) have passed since the password was last
  accepted;
* not after 3 failed phone unlocks, until the password is accepted again.

**Not done: the lock screen does not act on the decision.** Today exactly one
thing releases the lock screen: `Lock::show` in `des-ui/shell/src/lock.rs`
receiving `Ok(true)` from `unix_chkpwd`. Adding a second release path changes
the guarantee of the component the owner's daily session depends on, in drawing
code that could not be run or looked at in this task. It was left out rather
than shipped unseen. The hook is small and is described in
`des-ui/connect/PHONEKEY.md`; in short, the **shell itself** must make the
challenge, read the enrolled key and certificates from the trust store itself,
verify with `phonekey::verify_answer`, and only then take the same path a
correct password takes. The daemon must stay a carrier: a shell that unlocked on
the daemon's say-so would add a network-facing process to the lock screen's
trusted base.

### 6.3 Approve

One-time setup: enrol and switch `approve` on in the session; then, as root,
`desos-phonekey enable --user NAME`. That command reads the user's Connect store
(as that user), shows the phone's name and key fingerprint for confirmation,
writes the root-owned record, and adds this managed block before the first
`auth` rule of `/etc/pam.d/sudo` (and `sudo-i` if present):

```
# BEGIN desos-phonekey (managed; remove with: desos-phonekey disable)
auth	sufficient	pam_exec.so quiet stdout seteuid /usr/sbin/desos-phonekey pam-auth
# END desos-phonekey
```

`sufficient` means success authenticates and **any** other result is ignored and
the stack continues to `common-auth`, i.e. the password. The edit is planned and
checked for reversibility before anything is written, written atomically, backed
up under `/var/lib/desos-phonekey/pam/`, and `disable` removes exactly those
three lines (or the `/etc/pam.d/polkit-1` override it created from
`/usr/lib/pam.d/polkit-1`). **The image ships no PAM file and edits none**; a
fresh image's PAM stack is byte-for-byte stock.

Each `sudo`:

1. PAM runs the helper as root with `PAM_USER`, `PAM_SERVICE`, `PAM_TYPE`
   (and `PAM_RHOST`, `PAM_TTY`). If the user has no root record, or not for
   this service, it exits at once, silently. Otherwise it checks the record's
   ownership and permissions, then **where the request comes from** (below),
   then the lockout and rate state.
2. It works out the **action text itself**: the command line of the sudo process
   that called PAM (`/proc/<parent>/cmdline`, shell-quoted, unsafe characters
   replaced, long commands cut with a hash of the whole). Nothing the user's
   session says is used.
3. It makes `request_id` and `nonce` (`os.urandom`), `expires_at = now + 20 s`,
   prints "Approve on <phone>: <action>", and hands the challenge to the user's
   `des-connectd` through a child process that has dropped to that user.
4. The daemon checks the capability is on, the desktop session is unlocked and
   the limits, sends `request`, and returns the phone's answer.
5. The helper rebuilds the message from **its own** nonce, text and root-owned
   record, checks the expiry, and verifies the signature with
   `openssl dgst -sha256 -verify` against the root-owned key. Only then exit 0.
   Nothing else in the daemon's reply is read.

**Local sessions only.** The helper asks the phone only when it has positive
evidence that the request was made in a local login session; otherwise it
prints "Phone approval is only offered in a local session; use your
password.", logs `skipped` with reason `remote_session` or `locality_unknown`
(with `PAM_TTY` and `PAM_RHOST`), counts nothing against the phone and exits
1, so the password prompt follows. The rule, in order:

1. `PAM_RHOST` must be empty, a loopback name, or this machine's own host name
   (which is what sudo's `pam_rhost` option reports). Anything else is remote.
2. The session is read from the helper's **own control group**
   (`/proc/self/cgroup`): pam_exec forks the helper from the process that
   called PAM, so it sits in that process's `session-N.scope`. The user cannot
   move a process into another session's scope, and the caller's environment
   (`XDG_SESSION_ID`, `SSH_CONNECTION`) is never consulted. No session scope, or
   more than one, means "cannot tell".
3. `loginctl show-session N` must answer `Remote=no`, a non-empty `Seat`, and
   `Class=user`, and the session's service must not be `sshd`. `Remote=yes` or
   an sshd session is `remote_session`; no answer, no seat (serial console,
   `machinectl shell`), or another class is `locality_unknown`.

`PAM_TTY` is recorded in the audit trail but decides nothing: a terminal
emulator on the desktop and an ssh login are both `/dev/pts/N`.

Consequences that follow from failing closed: a `sudo` started outside any
login session scope is declined, which includes `systemd-run --user --scope
sudo ...` (otherwise an escape from an ssh session) and anything launched as a
systemd user service; and where the polkit agent helper runs as a system
service or under the user manager, polkit requests are always declined.
What it cannot see: someone attached over ssh to a terminal multiplexer that
was started in the local session is typing into local-session processes and is
treated as local, and code already running as the user can make a local shell
run sudo (T8; the phone still shows the true command).

**polkit.** PAM is not told which polkit action is being authorised, so for the
`polkit-1` service the helper cannot show or sign the action; the text is
"Administrator action through polkit as <user> (the action itself is not shown
here)". Because that is weaker than what the dialog on screen says, polkit is
**not enabled by `enable` unless named** (`--service polkit-1`). See decision 2.

**sudo's own memory.** After any successful authentication sudo skips
authentication on that terminal for `timestamp_timeout` (15 minutes by default),
exactly as after a password. Commands in that window never reach the phone.

## 7. Threat model

Trusted: the desktop's kernel and root; the shell and compositor for the lock
screen; the phone's keystore and biometric sensor; the unmodified phone app.
Not trusted: the network; other devices, paired or not; any process running as
the user, **for approve**; the user's `des-connectd`, for approve.

| # | Threat | What happens | Residual risk |
|---|---|---|---|
| T1 | **Stolen phone** | The key cannot sign without the owner's biometric (keystore-enforced), and a newly enrolled fingerprint invalidates it. The thief sees prompts only if near the desktop's LAN. Owner unpairs on the desktop (immediate) and runs `desos-phonekey disable --user`. | A thief who can pass the owner's biometric holds a working key until revoked. The phone PIN alone is not enough by design (decision 1). The root record outlives unpairing (decision 7). |
| T2 | **Stolen or unattended unlocked laptop** | Walk-away locks it after the grace period if the phone left. Approve still needs the phone's owner to approve a prompt naming the command. Nothing gets easier than without the feature. | Within the grace period the session is open, as with any idle timeout. sudo's timestamp window applies. |
| T3 | **Stolen locked laptop** (when the unlock hook is wired) | Unlock needs a signature from the enrolled key, so the owner's phone and biometric; never for the first sign-in; password required again after 12 h or 3 failures. | Relay, T7. |
| T4 | **Hostile device on the same Wi-Fi** | It cannot join the paired TLS session (pinned certificates, mutual TLS), so it can send no phone-key packet that is read and see none. Discovery spoofing with the phone's ID makes the desktop try a connection that fails the pin. | It can jam or drop traffic: the walk-away lock fires (acceptable: absence may lock) and phone requests time out to the password. |
| T5 | **Replay** | Fresh 256-bit nonce per request; a request is used up by its first answer; unknown or repeated `request_id`s are dropped; the root helper accepts only a signature over the nonce it made in that same process. | None known. |
| T6 | **Cross-use of signatures** | Purpose, both certificate digests, user and text are signed with length prefixes under a domain tag; an enrol or unlock signature is not an approval; a signature for another desktop or pairing does not verify. | None known. |
| T7 | **Relay** (attacker carries packets between a distant phone and the desktop) | The pinned TLS session cannot be terminated by a third party, but its packets can be tunnelled, so cryptography does not prove the phone is near. What is required instead is the owner's deliberate biometric on a prompt that names this desktop, the purpose and the exact text, within 30 s, and the desktop only talks to private/link-local addresses. For walk-away, relayed presence only postpones a lock. | An owner who approves a prompt they did not cause defeats this. There is no distance bounding (decision 8). |
| T8 | **Compromised unprivileged process asking for approvals** | It can run sudo or call the daemon, but the phone shows the text root computed (the real command), signed into the message, so it cannot disguise what is approved; it cannot swap the key (root-owned record) or reuse an old answer (T5); prompts are rate-limited and three refusals lock the phone path out. A request made while the session is locked is refused. | It can put truthful prompts on the phone and hope. With polkit enabled the prompt does not name the action (decision 2). It can read and rewrite the user-level store: that can break the feature or change which phone gets *unlock* requests, which matters once the unlock hook exists (a process running as the user is already inside the session). |
| T9 | **Prompt spam / denial of service** | One request at a time per phone; 2 s spacing; 6/min; 30/h; lockout after 3 failures; root-side cooldown and 10 per 10 min. Requests need a local process as the user; nothing on the network can cause one. | Up to those rates, a local process can annoy the owner. |
| T10 | **Malicious or modified phone app / rooted phone** | The desktop cannot tell. Such a phone could sign without a biometric, or with a key that never had one. | Accepted in v1; attestation would narrow it (decision 5). |
| T11 | **Man in the middle at pairing time** | Pairing is protected by KDE Connect's 32-bit comparison code. Someone who won that could enrol their own key later. The fingerprint shown on both the phone and the desktop at enrolment, and again by `desos-phonekey enable`, is the check. | Only as good as the person comparing. |
| T12 | **Mistake in the PAM edit** | One `sufficient` line that cannot deny; refused unless reversible; atomic write; backup; `disable` restores; nothing shipped enabled. | Not tested on a real PAM stack. Keep a root shell open when enabling. |
| T13 | **Password exposure** | No code path reads, forwards or stores a password. | - |
| T14 | **Approval of a remote user's sudo** (ssh, while the owner's desktop is unlocked) | The helper declines unless the request is in a local session on a seat (6.3, "Local sessions only"); the phone is not asked and the password is required. | A remote user attached to a multiplexer started locally, or acting through code planted in the local session, looks local. logind's session record and the cgroup layout are trusted. |

## 8. What is verified, and what is not

Proved by tests on the build host (no phone, no PAM, no root):

* `des-ui`: `cargo test -p des-connect` - 23 unit tests in `phonekey.rs`
  (signature coverage of every field, single-use nonces, replay, expiry by
  monotonic deadline, wrong device, wrong key, wrong user, wrong purpose,
  malformed and non-canonical input, P-256-only keys, enrolment proof of
  possession, rate limits, lockout, suspension, walk-away rule, unlock policy,
  audit log); 3 integration tests against a real `des-connectd` over real TLS on
  loopback with a stand-in phone and a stand-in shell (enrol → approve → replay
  → forgery → wrong user → caller's own challenge → locked/unlocked rules →
  unpair; walk-away locks once and returning presence unlocks nothing; a phone
  without the capability is never sent a packet); 1 cross-implementation vector
  test.
* `desos`: `python3 -m unittest tests.test_phonekey` - 42 tests: the component
  convention, that nothing PAM reads is shipped, PAM edit round-trips
  byte-for-byte, enable/disable against a temporary root, and the helper's
  decision against a stand-in phone signing with OpenSSL (every failure mode
  falls back; inert when not enabled; record ownership; cooldown, lockout, rate
  limit; action text), and the locality rule against stand-in `/proc` and
  logind answers (ssh session, `PAM_RHOST`, no session scope, user-manager
  scope, two scopes, logind silent or failing, no seat, wrong class; the
  caller's environment is ignored).

Not verified:

* Anything on Android: keystore behaviour, biometric prompt, background
  delivery of `presence` under Doze, notification behaviour while locked.
* `pam_exec` semantics on Ubuntu 26.04: that `sufficient` + `quiet stdout
  seteuid` behaves as documented, that the helper's parent is the sudo process
  (also under sudo-rs), where `polkit-1`'s PAM file lives, and how the DEs-UI
  polkit dialog behaves while the helper waits up to 20 s before the password
  prompt appears.
* The locality rule on a real system: that the helper really is in the
  caller's `session-N.scope` under sudo and sudo-rs, what `loginctl` reports
  for greetd/DEs-UI sessions (`Seat`, `Class`, `Remote`), that DEs-UI's
  terminal and launched applications stay in the session scope rather than
  the user manager's (if they do not, phone approval never triggers on the
  desktop), and where polkit's helper runs. No ssh login was made.
* The helper's privilege drop (`as_user` fork path) and syslog output: the tests
  run unprivileged, so that branch is not executed.
* The walk-away lock against the real shell (only against a stand-in socket),
  and its false-positive rate on real Wi-Fi.
* The lock-screen unlock hook: not written.
* Installing the component into an image and booting it.

## 9. Decisions wanted from a human

1. **Biometric only, or allow the phone's PIN/pattern as fallback?** Specified:
   Class 3 biometric only, key invalidated by new enrolment. Stricter, and locks
   out phones without a strong biometric.
2. **polkit approval without the action text**: keep it as an explicit opt-in,
   remove it, or invest in a root-side way to learn the action (needs polkitd
   cooperation; PAM cannot provide it)?
3. **sudo timestamp window**: accept that one approval covers 15 minutes on that
   terminal, or ship `timestamp_timeout=0` when phone approval is enabled
   (components may not ship `/etc/sudoers.d`, so the tool would have to write it)?
4. **Wire the lock-screen unlock?** If yes: confirm the 12 h / 3 failures policy,
   and whether a session started with `--locked` counts as password-authenticated
   (assumed: no).
5. **Key attestation**: require and verify the Android attestation chain at
   enrolment (needs Google's roots and a revocation story on the desktop), or
   stay with trust in the paired app?
6. **Remote shells** — *decided: refused.* Phone approval is declined for a
   request from a remote session and whenever locality cannot be established;
   the password is asked instead (6.3, "Local sessions only"; T14). Left for a
   human only if wanted: whether an inactive local session (switched-away VT)
   or a session owned by another user than the one authenticating should also
   be declined; both are currently allowed.
7. **Root record after unpairing**: should the root helper also refuse when the
   user's store no longer lists the device (cheap, but trusts user-writable
   data to revoke), or should unpairing prompt for `desos-phonekey disable`?
8. **Relay**: is "deliberate biometric on a prompt naming the desktop and
   action" enough without distance bounding (which would need Bluetooth/UWB
   ranging)?
9. **Walk-away defaults**: 120 s grace, 10 s heartbeat, arms only after the
   phone was seen while unlocked. Right trade between false locks and exposure?
10. **A Python script as root in the PAM path**, shelling out to
    `/usr/bin/openssl` for verification. It is small, isolated-mode and tested,
    but a compiled helper sharing `phonekey.rs` would remove the second
    implementation. Worth doing before release?
