"""Composing and sending refund requests.

The sender is pluggable (app.config["SENDER"]). A sender gets a dict with
to/cc/subject/body/attachments and returns a provider message id, or raises:
  SendingDisabled  - nothing was attempted
  DefiniteFailure  - provider confirmed the message was NOT sent
  UnknownOutcome   - we cannot tell whether it was sent (timeout etc.)
Gmail sending is stage 4; until then the default sender is disabled.
"""
import hashlib
import json
import os

from flask import current_app

from .db import get_db, get_setting

CATEGORIES = ["דלק", "חניה", "תחבורה ציבורית", "אש״ל", "אחר"]


class SendingDisabled(Exception):
    pass


class DefiniteFailure(Exception):
    pass


class UnknownOutcome(Exception):
    pass


def disabled_sender(_message):
    raise SendingDisabled("חיבור Gmail לשליחה טרם נבנה")


def format_amount(amount, currency):
    return f"{amount:.2f} ₪" if currency == "ILS" else f"{amount:.2f} {currency}"


def request_receipts(request_id):
    return get_db().execute(
        "SELECT r.* FROM receipts r JOIN request_receipts rr ON rr.receipt_id = r.id "
        "WHERE rr.request_id = ? ORDER BY r.id",
        (request_id,),
    ).fetchall()


def target_address():
    mode = get_setting("target_mode")
    return get_setting("accounting_email" if mode == "live" else "test_email")


def compose(request_row, user):
    """Build the message exactly as it will be sent. Recipient always comes from settings."""
    receipts = request_receipts(request_row["id"])
    lines = ["שלום,", "אבקש לטפל בבקשת הזיכוי עבור הקבלות המצורפות:", ""]
    for i, r in enumerate(receipts, 1):
        line = f"{i}. {r['original_name']} | {r['category']} | {format_amount(r['amount'], r['currency'])}"
        if r["category"] == "אחר":
            line += f" | {r['description']}"
        lines.append(line)
    lines += ["", "תודה,", user["full_name"]]
    message = {
        "to": target_address(),
        "cc": user["email"],
        "subject": f"בקשת זיכוי — {user['full_name']} — {request_row['id']}",
        "body": "\n".join(lines),
        "attachments": [
            {"receipt_id": r["id"], "name": r["original_name"], "mime": r["mime"],
             "sha256": r["sha256"], "path": receipt_path(r)}
            for r in receipts
        ],
    }
    message["hash"] = content_hash(message)
    return message


def content_hash(message):
    payload = {k: message[k] for k in ("to", "cc", "subject", "body")}
    payload["files"] = [(a["receipt_id"], a["sha256"]) for a in message["attachments"]]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def receipt_path(receipt):
    return os.path.join(current_app.config["UPLOAD_DIR"], str(receipt["owner_id"]), receipt["stored_name"])


def send_request(request_id, user):
    """Send an approved request at most once. Returns the resulting status."""
    db = get_db()
    # Atomic claim: only one caller can move approved -> sending.
    claimed = db.execute(
        "UPDATE requests SET status = 'sending' WHERE id = ? AND owner_id = ? AND status = 'approved'",
        (request_id, user["id"]),
    ).rowcount
    if not claimed:
        return db.execute("SELECT status FROM requests WHERE id = ?", (request_id,)).fetchone()["status"]

    req = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    message = compose(req, user)
    if message["hash"] != req["approved_hash"]:
        # Content changed after approval: approval is void.
        db.execute("UPDATE requests SET status = 'draft', approved_hash = NULL WHERE id = ?", (request_id,))
        return "draft"

    sender = current_app.config["SENDER"]
    message_id, detail = None, ""
    try:
        message_id = sender(message)
        result, status = "sent", "sent"
    except SendingDisabled as e:
        result, status, detail = "disabled", "held", str(e)
    except DefiniteFailure as e:
        result, status, detail = "failed", "failed", str(e)
    except UnknownOutcome as e:
        result, status, detail = "unknown", "unknown", str(e)
    except Exception as e:  # anything unexpected after a send call may have gone out
        result, status, detail = "unknown", "unknown", f"{type(e).__name__}"

    db.execute(
        "INSERT INTO send_attempts (request_id, result, detail, message_id) VALUES (?, ?, ?, ?)",
        (request_id, result, detail, message_id),
    )
    if status == "sent":
        db.execute(
            "UPDATE requests SET status = 'sent', sent_at = CURRENT_TIMESTAMP, message_id = ? WHERE id = ?",
            (message_id, request_id),
        )
        db.execute(
            "UPDATE receipts SET status = 'submitted' WHERE id IN "
            "(SELECT receipt_id FROM request_receipts WHERE request_id = ?)",
            (request_id,),
        )
    else:
        db.execute("UPDATE requests SET status = ? WHERE id = ?", (status, request_id))
    return status
