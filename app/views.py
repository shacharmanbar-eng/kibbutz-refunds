import hashlib
import os
import re
import secrets
import uuid

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template,
                   request, send_file, url_for)

from .auth import admin_required, login_required
from .db import get_db, get_setting, set_setting
from .mail import CATEGORIES, compose, format_amount, request_receipts, send_request, target_address

bp = Blueprint("views", __name__)

ALLOWED = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
           "webp": "image/webp", "heic": "image/heic", "pdf": "application/pdf"}
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

STATUS_LABELS = {
    "draft": "ממתין לאישור",
    "approved": "אושר",
    "sending": "בשליחה",
    "sent": "נשלח",
    "failed": "נכשל",
    "unknown": "מצב שליחה לא ידוע",
    "held": "אושר — השליחה מושבתת",
    "needs_input": "דורש השלמה",
    "ready": "מוכן",
    "submitted": "הוגש",
}


@bp.app_template_filter("status")
def status_label(value):
    return STATUS_LABELS.get(value, value)


@bp.app_template_filter("money")
def money(receipt):
    return "—" if receipt["amount"] is None else format_amount(receipt["amount"], receipt["currency"])


# --- ownership helpers: every lookup is scoped to the logged-in user ---

def own_receipt(receipt_id):
    row = get_db().execute(
        "SELECT * FROM receipts WHERE id = ? AND owner_id = ?", (receipt_id, g.user["id"])
    ).fetchone()
    if row is None:
        abort(404)
    return row


def own_request(request_id):
    row = get_db().execute(
        "SELECT * FROM requests WHERE id = ? AND owner_id = ?", (request_id, g.user["id"])
    ).fetchone()
    if row is None:
        abort(404)
    return row


def request_of(receipt_id):
    return get_db().execute(
        "SELECT q.* FROM requests q JOIN request_receipts rr ON rr.request_id = q.id "
        "WHERE rr.receipt_id = ?", (receipt_id,)
    ).fetchone()


EDITABLE_REQUEST = ("draft", "held", "failed")


def void_approval(request_id):
    get_db().execute(
        "UPDATE requests SET status = 'draft', approved_hash = NULL, approved_at = NULL "
        "WHERE id = ? AND status IN ('approved', 'held', 'failed')", (request_id,)
    )


# --- home & receipts ---

@bp.route("/")
@login_required
def home():
    db = get_db()
    loose = db.execute(
        "SELECT * FROM receipts WHERE owner_id = ? AND status != 'submitted' "
        "AND id NOT IN (SELECT receipt_id FROM request_receipts) ORDER BY id DESC",
        (g.user["id"],),
    ).fetchall()
    pending = db.execute(
        "SELECT q.*, COUNT(rr.receipt_id) AS n FROM requests q "
        "LEFT JOIN request_receipts rr ON rr.request_id = q.id "
        "WHERE q.owner_id = ? AND q.status NOT IN ('sent') GROUP BY q.id ORDER BY q.id DESC",
        (g.user["id"],),
    ).fetchall()
    return render_template("home.html", loose=loose, pending=pending)


@bp.post("/receipts")
@login_required
def upload_receipt():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("לא נבחר קובץ")
        return redirect(url_for("views.home"))
    ext = f.filename.rsplit(".", 1)[-1].lower() if "." in f.filename else ""
    if ext not in ALLOWED:
        flash("סוג קובץ לא נתמך. אפשר להעלות תמונה (JPG, PNG, WEBP, HEIC) או PDF.")
        return redirect(url_for("views.home"))

    data = f.read()
    digest = hashlib.sha256(data).hexdigest()
    db = get_db()
    existing = db.execute(
        "SELECT * FROM receipts WHERE owner_id = ? AND sha256 = ?", (g.user["id"], digest)
    ).fetchone()
    if existing:
        prev = request_of(existing["id"])
        if existing["status"] == "submitted":
            flash(f"הקבלה הזו כבר הוגשה בבקשה מספר {prev['id']}. היא לא תוגש שוב.")
        else:
            flash("הקובץ הזה כבר הועלה. הנה הקבלה הקיימת.")
        return redirect(url_for("views.receipt", receipt_id=existing["id"]))

    stored = f"{uuid.uuid4().hex}.{ext}"
    folder = os.path.join(current_app.config["UPLOAD_DIR"], str(g.user["id"]))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, stored), "wb") as out:
        out.write(data)
    source = "camera" if request.form.get("source") == "camera" else "upload"
    original = os.path.basename(f.filename)[:120]
    cur = db.execute(
        "INSERT INTO receipts (owner_id, original_name, stored_name, mime, sha256, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (g.user["id"], original, stored, ALLOWED[ext], digest, source),
    )
    flash("הקבלה נקלטה. פענוח אוטומטי עדיין לא מחובר, נא להשלים סכום וסוג הוצאה.")
    return redirect(url_for("views.receipt", receipt_id=cur.lastrowid))


@bp.route("/receipts/<int:receipt_id>", methods=["GET", "POST"])
@login_required
def receipt(receipt_id):
    r = own_receipt(receipt_id)
    req = request_of(receipt_id)
    locked = r["status"] == "submitted" or (req is not None and req["status"] not in EDITABLE_REQUEST)

    if request.method == "POST":
        if locked:
            flash("אי אפשר לשנות קבלה שנמצאת בשליחה או שכבר הוגשה.")
            return redirect(url_for("views.receipt", receipt_id=receipt_id))
        errors = []
        try:
            amount = round(float(request.form.get("amount", "").replace(",", ".")), 2)
            if amount <= 0:
                raise ValueError
        except ValueError:
            amount = None
            errors.append("נא להזין סכום חיובי")
        category = request.form.get("category")
        if category not in CATEGORIES:
            errors.append("נא לבחור סוג הוצאה")
        description = request.form.get("description", "").strip()[:300]
        if category == "אחר" and not description:
            errors.append("בסוג ״אחר״ נדרש תיאור")
        status = "needs_input" if errors else "ready"
        get_db().execute(
            "UPDATE receipts SET amount = ?, category = ?, description = ?, status = ? WHERE id = ?",
            (amount, category, description, status, receipt_id),
        )
        if req is not None:
            void_approval(req["id"])
        for e in errors:
            flash(e)
        if not errors:
            flash("נשמר")
            if req is not None:
                return redirect(url_for("views.request_view", request_id=req["id"]))
            return redirect(url_for("views.home"))
        return redirect(url_for("views.receipt", receipt_id=receipt_id))

    return render_template("receipt.html", r=r, req=req, locked=locked, categories=CATEGORIES)


@bp.get("/receipts/<int:receipt_id>/file")
@login_required
def receipt_file(receipt_id):
    r = own_receipt(receipt_id)
    path = os.path.join(current_app.config["UPLOAD_DIR"], str(r["owner_id"]), r["stored_name"])
    return send_file(path, mimetype=r["mime"], download_name=r["original_name"])


@bp.post("/receipts/<int:receipt_id>/delete")
@login_required
def delete_receipt(receipt_id):
    r = own_receipt(receipt_id)
    if r["status"] == "submitted" or request_of(receipt_id) is not None:
        flash("אפשר למחוק רק קבלה שלא צורפה לבקשה")
        return redirect(url_for("views.receipt", receipt_id=receipt_id))
    get_db().execute("DELETE FROM receipts WHERE id = ?", (receipt_id,))
    try:
        os.remove(os.path.join(current_app.config["UPLOAD_DIR"], str(r["owner_id"]), r["stored_name"]))
    except FileNotFoundError:
        pass
    flash("הקבלה נמחקה")
    return redirect(url_for("views.home"))


# --- requests ---

@bp.post("/requests")
@login_required
def create_request():
    ids = [int(i) for i in request.form.getlist("receipt_ids") if i.isdigit()]
    db = get_db()
    rows = [own_receipt(i) for i in ids]
    rows = [r for r in rows if r["status"] == "ready" and request_of(r["id"]) is None]
    if not rows:
        flash("נא לבחור לפחות קבלה אחת מוכנה (עם סכום וסוג הוצאה)")
        return redirect(url_for("views.home"))
    cur = db.execute("INSERT INTO requests (owner_id) VALUES (?)", (g.user["id"],))
    for r in rows:
        db.execute("INSERT INTO request_receipts (request_id, receipt_id) VALUES (?, ?)",
                   (cur.lastrowid, r["id"]))
    return redirect(url_for("views.request_view", request_id=cur.lastrowid))


@bp.get("/requests/<int:request_id>")
@login_required
def request_view(request_id):
    req = own_request(request_id)
    receipts = request_receipts(request_id)
    if req["status"] in ("draft", "held", "failed"):
        message = compose(req, g.user)
    else:  # show what was actually approved/sent
        message = {"to": req["mail_to"], "cc": req["mail_cc"], "subject": req["subject"],
                   "body": req["body"], "hash": req["approved_hash"]}
    attempts = get_db().execute(
        "SELECT * FROM send_attempts WHERE request_id = ? ORDER BY id DESC", (request_id,)
    ).fetchall()
    return render_template("request.html", req=req, receipts=receipts, message=message,
                           attempts=attempts, problems=approval_problems(receipts, message))


def approval_problems(receipts, message):
    problems = []
    if not receipts:
        problems.append("אין קבלות בבקשה")
    if any(r["status"] != "ready" for r in receipts):
        problems.append("יש קבלות שדורשות השלמה")
    if not message.get("to"):
        problems.append("כתובת היעד לא הוגדרה עדיין (מוגדרת בעמוד הניהול)")
    if not g.user["full_name"]:
        problems.append("נא להגדיר שם מלא בהגדרות")
    return problems


@bp.post("/requests/<int:request_id>/approve")
@login_required
def approve(request_id):
    req = own_request(request_id)
    if req["status"] not in EDITABLE_REQUEST:
        flash("הבקשה כבר אושרה או נשלחה")
        return redirect(url_for("views.request_view", request_id=request_id))
    message = compose(req, g.user)
    problems = approval_problems(request_receipts(request_id), message)
    if problems:
        for p in problems:
            flash(p)
        return redirect(url_for("views.request_view", request_id=request_id))
    # The user approves what they saw; if anything changed since, ask again.
    if request.form.get("hash") != message["hash"]:
        flash("תוכן הבקשה השתנה מאז שהוצג. נא לבדוק שוב ולאשר.")
        return redirect(url_for("views.request_view", request_id=request_id))

    claimed = get_db().execute(
        "UPDATE requests SET status = 'approved', approved_at = CURRENT_TIMESTAMP, approved_hash = ?, "
        "mail_to = ?, mail_cc = ?, subject = ?, body = ? "
        "WHERE id = ? AND status IN ('draft', 'held', 'failed')",
        (message["hash"], message["to"], message["cc"], message["subject"], message["body"], request_id),
    ).rowcount
    if claimed:
        status = send_request(request_id, g.user)
        flash({
            "sent": "הבקשה נשלחה. העתק יגיע לתיבת המייל שלך.",
            "held": "הבקשה אושרה ונשמרה. השליחה עדיין מושבתת (חיבור Gmail לא נבנה), לכן שום מייל לא נשלח.",
            "failed": "השליחה נכשלה. הבקשה שמורה ואפשר לנסות שוב.",
            "unknown": "לא ברור אם המייל נשלח. לא נשלח שוב עד שזה יתברר.",
            "draft": "תוכן הבקשה השתנה. נא לאשר מחדש.",
        }.get(status, status))
    return redirect(url_for("views.request_view", request_id=request_id))


@bp.post("/requests/<int:request_id>/remove/<int:receipt_id>")
@login_required
def remove_from_request(request_id, receipt_id):
    req = own_request(request_id)
    own_receipt(receipt_id)
    if req["status"] not in EDITABLE_REQUEST:
        abort(409)
    db = get_db()
    db.execute("DELETE FROM request_receipts WHERE request_id = ? AND receipt_id = ?", (request_id, receipt_id))
    void_approval(request_id)
    if not request_receipts(request_id):
        db.execute("DELETE FROM requests WHERE id = ?", (request_id,))
        return redirect(url_for("views.home"))
    return redirect(url_for("views.request_view", request_id=request_id))


@bp.get("/history")
@login_required
def history():
    rows = get_db().execute(
        "SELECT q.*, COUNT(rr.receipt_id) AS n FROM requests q "
        "LEFT JOIN request_receipts rr ON rr.request_id = q.id "
        "WHERE q.owner_id = ? AND q.status != 'draft' GROUP BY q.id ORDER BY q.id DESC",
        (g.user["id"],),
    ).fetchall()
    return render_template("history.html", rows=rows)


@bp.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        name = request.form.get("full_name", "").strip()[:100]
        if not name:
            flash("נדרש שם מלא")
        else:
            get_db().execute("UPDATE users SET full_name = ? WHERE id = ?", (name, g.user["id"]))
            flash("נשמר")
        return redirect(url_for("views.settings"))
    return render_template("settings.html")


# --- admin: no access to receipt content ---

@bp.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    db = get_db()
    invite_link = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "invite":
            email = request.form.get("email", "").strip().lower()
            if not EMAIL_RE.match(email):
                flash("כתובת מייל לא תקינה")
            elif db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
                flash("המשתמש כבר קיים")
            else:
                token = secrets.token_urlsafe(24)
                db.execute("INSERT INTO users (email, invite_token) VALUES (?, ?)", (email, token))
                invite_link = url_for("auth.accept_invite", token=token, _external=True)
        elif action == "settings":
            accounting = request.form.get("accounting_email", "").strip()
            test = request.form.get("test_email", "").strip()
            mode = request.form.get("target_mode")
            bad = [a for a in (accounting, test) if a and not EMAIL_RE.match(a)]
            if bad or mode not in ("test", "live"):
                flash("ערכים לא תקינים")
            elif mode == "live" and not accounting:
                flash("אי אפשר לעבור לשליחה אמיתית בלי כתובת הנהלת חשבונות")
            else:
                set_setting("accounting_email", accounting)
                set_setting("test_email", test)
                set_setting("target_mode", mode)
                flash("ההגדרות נשמרו")
    users = db.execute("SELECT id, email, full_name, role, invite_status FROM users ORDER BY id").fetchall()
    counts = db.execute("SELECT status, COUNT(*) AS n FROM requests GROUP BY status").fetchall()
    return render_template(
        "admin.html", users=users, counts=counts, invite_link=invite_link,
        accounting_email=get_setting("accounting_email"), test_email=get_setting("test_email"),
        target_mode=get_setting("target_mode"), target=target_address(),
    )
