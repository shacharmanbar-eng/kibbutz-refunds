import functools

from flask import Blueprint, abort, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db

bp = Blueprint("auth", __name__)


@bp.before_app_request
def load_user():
    uid = session.get("uid")
    g.user = None
    if uid:
        g.user = get_db().execute(
            "SELECT * FROM users WHERE id = ? AND invite_status = 'active'", (uid,)
        ).fetchone()


def login_required(view):
    @functools.wraps(view)
    def wrapped(**kwargs):
        if g.user is None:
            return redirect(url_for("auth.login"))
        return view(**kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    @login_required
    def wrapped(**kwargs):
        if g.user["role"] != "admin":
            abort(403)
        return view(**kwargs)
    return wrapped


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        user = get_db().execute(
            "SELECT * FROM users WHERE email = ? AND invite_status = 'active'", (email,)
        ).fetchone()
        if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
            csrf = session.get("csrf")
            session.clear()
            session["uid"] = user["id"]
            session["csrf"] = csrf
            return redirect(url_for("views.home"))
        flash("מייל או סיסמה שגויים")
    return render_template("login.html")


@bp.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@bp.route("/invite/<token>", methods=["GET", "POST"])
def accept_invite(token):
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE invite_token = ?", (token,)).fetchone()
    if user is None:
        abort(404)
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        password = request.form.get("password", "")
        if not full_name or len(password) < 8:
            flash("נדרשים שם מלא וסיסמה של 8 תווים לפחות")
        else:
            db.execute(
                "UPDATE users SET full_name = ?, password_hash = ?, invite_status = 'active', "
                "invite_token = NULL WHERE id = ?",
                (full_name, generate_password_hash(password), user["id"]),
            )
            session["uid"] = user["id"]
            return redirect(url_for("views.home"))
    return render_template("invite.html", email=user["email"])
