"""Messages WAHA's list endpoint hides: read them from the GOWS store, read-only.

Why this exists (2026-10-07, whatsapp-bridge.md § Reading WhatsApp, rule 5). GOWS
keeps every message in gows_messages with an is_real column, and
GET /chats/{id}/messages returns only is_real = 1. When a group message arrives
bundled with a sender-key distribution (a member's or one of the owner's devices'
first message after a key change), whatsmeow emits two events under one message
id. GOWS stores the key event first as is_real = 0, and the real content that
follows keeps the 0. The text is in the row (and GET .../messages/{id} returns
it) but no list call ever does. Upstream: devlikeapro/waha#2289, open on 2026.9.1.

Measured 2026-10-07: 31% of group text/media rows since Sep 17 were hidden this
way (2,588), against 1.6% in direct chats. One of them was the owner's own "geht
auch 14:30?" in <GROUP_CHAT>, which the plan-today pre-run reported as absent.

The row's own JSON carries IsReal as GOWS computed it for the content it holds;
that is the flag used here. A second, unrecoverable loss is NOT fixed: when the
key event lands last it overwrites the content (gows-plus#12). Those rows hold
only a senderKeyDistributionMessage and are counted, never guessed at.

The store is opened with mode=ro (SQLite reads a WAL database read-only when the
-shm exists), so nothing here can write WAHA's session, which shares this file
with the pairing keys. Returned dicts mimic the REST shape the callers already
parse: id, timestamp, fromMe, body, hasMedia, participant, _data.Info.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

GOWS_DB = Path.home() / "waha" / "sessions" / "gows" / "default" / "gows.db"

_MEDIA = ("imageMessage", "videoMessage", "documentMessage", "audioMessage", "stickerMessage",
          "documentWithCaptionMessage", "locationMessage", "contactMessage")


def _body(m: dict) -> tuple[str, bool]:
    if m.get("conversation"):
        return m["conversation"], False
    if (m.get("extendedTextMessage") or {}).get("text"):
        return m["extendedTextMessage"]["text"], False
    for k in _MEDIA:
        if k in m:
            inner = m[k] or {}
            if k == "documentWithCaptionMessage":
                inner = ((inner.get("message") or {}).get("documentMessage")) or {}
            return inner.get("caption") or "", True
    return "", False


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(f"file:{GOWS_DB}?mode=ro", uri=True, timeout=10)


def hidden_messages(lo: int) -> tuple[dict[str, list[dict]], dict]:
    """Rows since epoch `lo` that are real messages but hidden by is_real = 0.

    Keyed by the chat id the REST overview uses: groups as-is, direct chats as
    <phone>@c.us (an @lid chat is mapped through whatsmeow_lid_map; one with no
    mapping stays @lid, so it is still returned rather than dropped).
    """
    since = datetime.fromtimestamp(lo, timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")
    out: dict[str, list[dict]] = {}
    stats = {"hidden_recovered": 0, "group_sender_key_only": 0}
    with _connect() as c:
        lid = {f"{a}@lid": f"{b}@c.us" for a, b in c.execute("SELECT lid, pn FROM whatsmeow_lid_map")}
        for jid, data in c.execute("SELECT jid, data FROM gows_messages "
                                   "WHERE is_real = 0 AND timestamp >= ?", (since,)):
            d = json.loads(data)
            m = d.get("Message") or {}
            if jid.endswith("@g.us") and set(m) - {"messageContextInfo"} == {"senderKeyDistributionMessage"}:
                stats["group_sender_key_only"] += 1
                continue
            if not d.get("IsReal"):
                continue
            body, media = _body(m)
            if not body and not media:
                continue
            info = d.get("Info") or {}
            ts = datetime.fromisoformat(info["Timestamp"].replace("Z", "+00:00")).timestamp()
            chat = lid.get(jid) or (jid.replace("@s.whatsapp.net", "@c.us"))
            out.setdefault(chat, []).append({
                "id": info.get("ID"), "timestamp": int(ts), "fromMe": bool(info.get("IsFromMe")),
                "body": body, "hasMedia": media, "participant": info.get("Sender"),
                "_data": {"Info": info}, "_hidden_by_waha": True})
            stats["hidden_recovered"] += 1
    return out, stats


# ---------------------------------------------------------------- edits
#
# Added 2026-10-07. An edited message arrives as a secretEncryptedMessage
# (secretEncType MESSAGE_EDIT) encrypted with the ORIGINAL message's secret.
# GOWS stores it as an is_real = 0 row and never applies it, so the REST API
# keeps serving the pre-edit text. Measured: a message\'s amount was edited
# two minutes later and the pre-run quoted the old one. The secret is in
# whatsmeow_message_secrets; the scheme is whatsmeow's msgsecret.go: key =
# HKDF-SHA256(secret, info = origID + origSender + editSender + "Message Edit"),
# AES-GCM, no additional data, senders without the device part.

def _pb(buf: bytes) -> dict[int, list]:
    """Minimal protobuf reader: field number -> list of raw values (bytes or int)."""
    out, i = {}, 0
    while i < len(buf):
        tag, i = _varint(buf, i)
        f, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(buf, i)
        elif wt == 2:
            n, i = _varint(buf, i)
            v, i = buf[i:i + n], i + n
        elif wt == 1:
            v, i = buf[i:i + 8], i + 8
        elif wt == 5:
            v, i = buf[i:i + 4], i + 4
        else:
            raise ValueError(f"wire type {wt}")
        out.setdefault(f, []).append(v)
    return out


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    v = s = 0
    while True:
        b = buf[i]
        i += 1
        v |= (b & 0x7F) << s
        s += 7
        if not b & 0x80:
            return v, i


def _edited_text(plain: bytes) -> str | None:
    """Message.protocolMessage(12).editedMessage(14) -> conversation(1) or
    extendedTextMessage(6).text(1), or a media caption."""
    try:
        proto = _pb(_pb(plain)[12][0])
        msg = _pb(proto[14][0])
        if 1 in msg:
            return msg[1][0].decode("utf-8", "replace")
        if 6 in msg:
            return _pb(msg[6][0])[1][0].decode("utf-8", "replace")
        for f in (3, 9):  # imageMessage, videoMessage: caption is field 3 / 2
            if f in msg:
                inner = _pb(msg[f][0])
                for cf in (3, 2):
                    if cf in inner and isinstance(inner[cf][0], bytes):
                        return inner[cf][0].decode("utf-8", "replace")
    except (KeyError, IndexError, ValueError):
        return None
    return None


def _non_ad(jid: str) -> str:
    user, _, server = jid.partition("@")
    return user.split(":")[0] + "@" + server


def edits(lo: int) -> tuple[dict[str, dict], dict]:
    """Edits made since epoch `lo`: {original bare id: {"text", "timestamp"}}.
    "text" is None when the edit could not be decrypted (secret missing, or
    the scheme changed): the caller must then say the message was edited, never
    present the old text as current. The latest edit wins."""
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    since = datetime.fromtimestamp(lo, timezone.utc).strftime("%Y-%m-%d %H:%M:%S+00:00")
    out: dict[str, dict] = {}
    stats = {"edits": 0, "edits_unreadable": 0}
    with _connect() as c:
        pn = {f"{a}@lid": f"{b}@s.whatsapp.net" for a, b in c.execute("SELECT lid, pn FROM whatsmeow_lid_map")}
        alt = pn | {v: k for k, v in pn.items()}
        rows = c.execute("SELECT data FROM gows_messages WHERE timestamp >= ? "
                         "AND data LIKE '%MESSAGE_EDIT%' ORDER BY timestamp", (since,)).fetchall()
        for (data,) in rows:
            d = json.loads(data)
            sem = (d.get("Message") or {}).get("secretEncryptedMessage") or {}
            if sem.get("secretEncType") != "MESSAGE_EDIT":
                continue
            info = d.get("Info") or {}
            orig = (sem.get("targetMessageKey") or {}).get("ID")
            ts = int(datetime.fromisoformat(info["Timestamp"].replace("Z", "+00:00")).timestamp())
            text = None
            sec = c.execute("SELECT sender_jid, key FROM whatsmeow_message_secrets WHERE message_id = ?",
                            (orig,)).fetchone()
            if sec and sem.get("encPayload") and sem.get("encIV"):
                payload, iv = base64.b64decode(sem["encPayload"]), base64.b64decode(sem["encIV"])
                editor = _non_ad(info.get("Sender") or "")
                for o in {sec[0], alt.get(sec[0], sec[0])}:
                    for e in {editor, alt.get(editor, editor)}:
                        k = HKDF(hashes.SHA256(), 32, None, f"{orig}{o}{e}Message Edit".encode()).derive(sec[1])
                        try:
                            text = _edited_text(AESGCM(k).decrypt(iv, payload, None))
                            break
                        except Exception:  # noqa: BLE001 - wrong JID form; try the next
                            continue
                    if text is not None:
                        break
            stats["edits"] += 1
            stats["edits_unreadable"] += text is None
            out[orig] = {"text": text, "timestamp": ts}
    return out, stats
