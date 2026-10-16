import os
import secrets

import click
from flask import Flask, abort, request, session

from . import db
from .mail import disabled_sender


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY") or "dev-only-change-me",
        DATABASE=os.path.join(app.instance_path, "hechzer.sqlite"),
        UPLOAD_DIR=os.path.join(app.instance_path, "uploads"),
        MAX_CONTENT_LENGTH=10 * 1024 * 1024,  # shortcut: 10MB until the real limit is decided
        SENDER=disabled_sender,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)

    app.teardown_appcontext(db.close_db)
    with app.app_context():
        db.init_db()

    @app.before_request
    def check_csrf():
        if request.method == "POST":
            token = session.get("csrf")
            if not token or request.form.get("csrf") != token:
                abort(400)

    @app.context_processor
    def inject_csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf"]}

    from .auth import bp as auth_bp
    from .views import bp as views_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(views_bp)

    @app.cli.command("create-admin")
    @click.argument("email")
    def create_admin(email):
        """Create the admin user and print a one-time invite link."""
        token = secrets.token_urlsafe(24)
        db.get_db().execute(
            "INSERT INTO users (email, role, invite_token) VALUES (?, 'admin', ?) "
            "ON CONFLICT(email) DO UPDATE SET role = 'admin', invite_token = excluded.invite_token",
            (email.strip().lower(), token),
        )
        click.echo(f"/invite/{token}")

    return app
