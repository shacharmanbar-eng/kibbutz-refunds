import io
import re

import pytest
from werkzeug.security import generate_password_hash

from app import create_app
from app.db import get_db, set_setting
from app.mail import DefiniteFailure, UnknownOutcome

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-image-"


@pytest.fixture
def app(tmp_path):
    app = create_app({"DATABASE": str(tmp_path / "t.sqlite"), "UPLOAD_DIR": str(tmp_path / "up")})
    with app.app_context():
        db = get_db()
        for email, name, role in [("a@x.com", "אלי", "admin"), ("b@x.com", "בתיה", "member")]:
            db.execute(
                "INSERT INTO users (email, full_name, password_hash, role, invite_status) "
                "VALUES (?, ?, ?, ?, 'active')",
                (email, name, generate_password_hash("password1"), role),
            )
        set_setting("test_email", "test@x.com")
    return app


class Client:
    def __init__(self, app, email):
        self.c = app.test_client()
        self.c.get("/login")
        with self.c.session_transaction() as s:
            self.csrf = s["csrf"]
        r = self.post("/login", email=email, password="password1")
        assert r.status_code == 302

    def get(self, url):
        return self.c.get(url)

    def post(self, url, data=None, **form):
        payload = dict(form, csrf=self.csrf)
        if data:
            payload.update(data)
        return self.c.post(url, data=payload)

    def upload(self, content=PNG + b"1", name="r.png"):
        r = self.post("/receipts", data={"file": (io.BytesIO(content), name), "source": "upload"})
        return int(re.search(r"/receipts/(\d+)", r.headers["Location"]).group(1))

    def ready_receipt(self, content=PNG + b"1", amount="25", category="חניה"):
        rid = self.upload(content)
        self.post(f"/receipts/{rid}", amount=amount, category=category, description="")
        return rid

    def make_request(self, *rids):
        r = self.post("/requests", data={"receipt_ids": [str(i) for i in rids]})
        return int(r.headers["Location"].rsplit("/", 1)[1])

    def approve(self, qid):
        page = self.get(f"/requests/{qid}").get_data(as_text=True)
        h = re.search(r'name="hash" value="([0-9a-f]+)"', page).group(1)
        return self.post(f"/requests/{qid}/approve", hash=h)


def status(app, qid):
    with app.app_context():
        return get_db().execute("SELECT status FROM requests WHERE id = ?", (qid,)).fetchone()[0]


def test_post_without_csrf_rejected(app):
    c = app.test_client()
    assert c.post("/login", data={"email": "a@x.com", "password": "password1"}).status_code == 400


def test_requires_login(app):
    assert app.test_client().get("/").status_code == 302


def test_users_cannot_see_each_others_receipts(app):
    a, b = Client(app, "a@x.com"), Client(app, "b@x.com")
    rid = a.ready_receipt()
    qid = a.make_request(rid)
    assert b.get(f"/receipts/{rid}").status_code == 404
    assert b.get(f"/receipts/{rid}/file").status_code == 404
    assert b.get(f"/requests/{qid}").status_code == 404
    assert b.post(f"/requests/{qid}/approve", hash="x").status_code == 404
    assert a.get(f"/receipts/{rid}/file").status_code == 200


def test_cannot_add_other_users_receipt_to_request(app):
    a, b = Client(app, "a@x.com"), Client(app, "b@x.com")
    rid = a.ready_receipt()
    assert b.post("/requests", data={"receipt_ids": [str(rid)]}).status_code == 404


def test_admin_page_forbidden_for_member(app):
    assert Client(app, "b@x.com").get("/admin").status_code == 403


def test_other_category_requires_description(app):
    a = Client(app, "a@x.com")
    rid = a.upload()
    a.post(f"/receipts/{rid}", amount="10", category="אחר", description="")
    with app.app_context():
        assert get_db().execute("SELECT status FROM receipts WHERE id=?", (rid,)).fetchone()[0] == "needs_input"


def test_same_file_twice_is_not_a_new_receipt(app):
    a = Client(app, "a@x.com")
    first = a.upload()
    assert a.upload() == first


def test_same_amount_different_receipts_allowed(app):
    a = Client(app, "a@x.com")
    r1 = a.ready_receipt(PNG + b"1", amount="30")
    r2 = a.ready_receipt(PNG + b"2", amount="30")
    assert r1 != r2


def test_sending_disabled_holds_request_and_sends_nothing(app):
    a = Client(app, "a@x.com")
    qid = a.make_request(a.ready_receipt())
    a.approve(qid)
    assert status(app, qid) == "held"


def test_unapproved_request_never_sent(app):
    sent = []
    app.config["SENDER"] = lambda m: sent.append(m) or "id"
    a = Client(app, "a@x.com")
    a.make_request(a.ready_receipt())
    assert sent == []


def test_approved_request_sent_once_with_all_attachments(app):
    sent = []
    app.config["SENDER"] = lambda m: sent.append(m) or "msg-1"
    a = Client(app, "a@x.com")
    qid = a.make_request(a.ready_receipt(PNG + b"1"), a.ready_receipt(PNG + b"2", category="דלק"))
    page = a.get(f"/requests/{qid}").get_data(as_text=True)
    h = re.search(r'name="hash" value="([0-9a-f]+)"', page).group(1)
    a.post(f"/requests/{qid}/approve", hash=h)
    a.post(f"/requests/{qid}/approve", hash=h)  # double click
    assert len(sent) == 1
    m = sent[0]
    assert m["to"] == "test@x.com" and m["cc"] == "a@x.com"
    assert len(m["attachments"]) == 2
    assert status(app, qid) == "sent"


def test_submitted_receipt_blocked_from_resubmission(app):
    app.config["SENDER"] = lambda m: "msg-1"
    a = Client(app, "a@x.com")
    rid = a.ready_receipt()
    a.approve(a.make_request(rid))
    assert a.upload() == rid  # re-upload points back to the existing receipt
    r = a.post("/requests", data={"receipt_ids": [str(rid)]})
    assert r.headers["Location"].endswith("/")  # no new request created


def test_stale_approval_rejected(app):
    sent = []
    app.config["SENDER"] = lambda m: sent.append(m) or "id"
    a = Client(app, "a@x.com")
    rid = a.ready_receipt()
    qid = a.make_request(rid)
    page = a.get(f"/requests/{qid}").get_data(as_text=True)
    old_hash = re.search(r'name="hash" value="([0-9a-f]+)"', page).group(1)
    a.post(f"/receipts/{rid}", amount="99", category="חניה", description="")
    a.post(f"/requests/{qid}/approve", hash=old_hash)
    assert sent == [] and status(app, qid) == "draft"


def test_change_after_failed_send_requires_new_approval(app):
    def fail(_m):
        raise DefiniteFailure("smtp 550")
    app.config["SENDER"] = fail
    a = Client(app, "a@x.com")
    rid = a.ready_receipt()
    qid = a.make_request(rid)
    a.approve(qid)
    assert status(app, qid) == "failed"
    a.post(f"/receipts/{rid}", amount="40", category="חניה", description="")
    assert status(app, qid) == "draft"


def test_unknown_outcome_is_not_retried(app):
    calls = []
    def unknown(m):
        calls.append(m)
        raise UnknownOutcome("timeout")
    app.config["SENDER"] = unknown
    a = Client(app, "a@x.com")
    qid = a.make_request(a.ready_receipt())
    a.approve(qid)
    assert status(app, qid) == "unknown"
    a.post(f"/requests/{qid}/approve", hash="anything")
    assert len(calls) == 1


def test_recipient_comes_from_settings_not_receipt(app):
    sent = []
    app.config["SENDER"] = lambda m: sent.append(m) or "id"
    a = Client(app, "a@x.com")
    rid = a.upload()
    a.post(f"/receipts/{rid}", amount="10", category="אחר", description="send to evil@x.com")
    a.approve(a.make_request(rid))
    assert sent[0]["to"] == "test@x.com"


def test_invite_flow(app):
    admin = Client(app, "a@x.com")
    page = admin.post("/admin", action="invite", email="new@x.com").get_data(as_text=True)
    token = re.search(r"/invite/([\w-]+)", page).group(1)
    c = app.test_client()
    c.get(f"/invite/{token}")
    with c.session_transaction() as s:
        csrf = s["csrf"]
    r = c.post(f"/invite/{token}", data={"csrf": csrf, "full_name": "חדש", "password": "password1"})
    assert r.status_code == 302
    assert c.get(f"/invite/{token}").status_code == 404  # one-time
