"""Sign-in modes (dev / demo / email), single-use expiring links, the email
sender, and keeping links and tokens out of the logs."""

import dataclasses
import logging
import time
from urllib.parse import parse_qs, urlparse

import pytest

from app import config, mailer, main

from .conftest import register, sign_in


def use(monkeypatch, **changes):
    monkeypatch.setattr(main, "settings", dataclasses.replace(main.settings, **changes))


def path_of(link: str) -> str:
    u = urlparse(link)
    return f"{u.path}?{u.query}"


class FakeSender:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to, subject, text):
        self.sent.append((to, subject, text))


@pytest.fixture
def outbox(monkeypatch) -> FakeSender:
    s = FakeSender()
    monkeypatch.setattr(main.mailer, "make_sender", lambda _settings: s)
    return s


def link_in(text: str) -> str:
    return next(w for w in text.split() if "/auth/magic?t=" in w)


# ------------------------------------------------------------------ modes
async def test_dev_mode_links_any_email(make_client):
    await register(make_client(), "Asha Rao", email="asha@example.com")
    r = await make_client().post("/api/auth/start", json={"email": "asha@example.com"})
    assert r.json()["sent"] is True and "/auth/magic?t=" in r.json()["dev_link"]


async def test_demo_mode_links_new_email(client, monkeypatch):
    use(monkeypatch, signin_mode="demo")
    r = await client.post("/api/auth/start", json={"email": "new@example.com"})
    body = r.json()
    assert body["sent"] is True and body["dev_link"]
    r = await client.get(path_of(body["dev_link"]))
    assert r.headers["location"] == "/#/onboard"


async def test_demo_mode_refuses_existing_email(make_client, monkeypatch):
    """No email provider: a link on screen for an existing account would be account takeover."""
    await register(make_client(), "Asha Rao", email="asha@example.com")
    use(monkeypatch, signin_mode="demo")
    mallory = make_client()
    r = await mallory.post("/api/auth/start", json={"email": " Asha@Example.com "})
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] is False and body["dev_link"] is None and body["already_joined"] is True
    assert "already joined" in body["message"]
    assert (await mallory.get("/api/me")).status_code == 401


async def test_email_mode_sends_and_never_returns_link(make_client, monkeypatch, outbox):
    await register(make_client(), "Asha Rao", email="asha@example.com")
    use(monkeypatch, signin_mode="email")
    existing = await make_client().post("/api/auth/start", json={"email": "asha@example.com"})
    new = await make_client().post("/api/auth/start", json={"email": "new@example.com"})
    # Same answer either way: no account enumeration.
    assert existing.status_code == new.status_code == 200
    assert existing.json() == new.json() == {"sent": True, "dev_link": None}
    assert [to for to, _, _ in outbox.sent] == ["asha@example.com", "new@example.com"]
    c = make_client()
    r = await c.get(path_of(link_in(outbox.sent[0][2])))
    assert r.headers["location"] == "/#/home"
    assert (await c.get("/api/me")).json()["handle"] == "asha"


async def test_email_mode_send_failure_is_503(client, monkeypatch):
    class Broken:
        async def send(self, *a):
            raise mailer.SendError("smtp: ConnectionRefusedError")

    monkeypatch.setattr(main.mailer, "make_sender", lambda _s: Broken())
    use(monkeypatch, signin_mode="email")
    r = await client.post("/api/auth/start", json={"email": "a@example.com"})
    assert r.status_code == 503


async def test_event_exposes_signin_mode(client, monkeypatch):
    use(monkeypatch, signin_mode="demo")
    assert (await client.get("/api/event")).json()["signin_mode"] == "demo"


# ------------------------------------------------------- single use, expiry
async def test_link_is_single_use_for_new_email(make_client):
    a = make_client()
    link = (await a.post("/api/auth/start", json={"email": "new@example.com"})).json()["dev_link"]
    assert (await a.get(path_of(link))).headers["location"] == "/#/onboard"
    r = await make_client().get(path_of(link))
    assert r.headers["location"] == "/#/?expired=1" and "set-cookie" not in r.headers


async def test_link_expires_after_15_minutes(client, hallway, monkeypatch):
    link = (await client.post("/api/auth/start", json={"email": "a@example.com"})).json()["dev_link"]
    token = parse_qs(urlparse(link).query)["t"][0]
    real = time.time
    monkeypatch.setattr(main.time, "time", lambda: real() + 15 * 60 + 1)
    r = await client.get(path_of(link))
    assert r.headers["location"] == "/#/?expired=1" and "set-cookie" not in r.headers
    assert hallway.store.kv_get(f"magic:{token}") is None


async def test_link_works_within_15_minutes(client, monkeypatch):
    link = (await client.post("/api/auth/start", json={"email": "a@example.com"})).json()["dev_link"]
    real = time.time
    monkeypatch.setattr(main.time, "time", lambda: real() + 14 * 60)
    assert (await client.get(path_of(link))).headers["location"] == "/#/onboard"


async def test_old_links_are_purged(client, hallway):
    hallway.store.kv_set("magic:stale", {"email": "x@example.com", "at": time.time() - 3600})
    hallway.store.kv_set("magic_other", {"at": 0})  # LIKE wildcards are escaped
    await client.post("/api/auth/start", json={"email": "a@example.com"})
    assert hallway.store.kv_get("magic:stale") is None
    assert hallway.store.kv_get("magic_other") is not None


async def test_kv_pop_is_one_shot(hallway):
    hallway.store.kv_set("magic:x", {"email": "a@example.com", "at": 1})
    assert hallway.store.kv_pop("magic:x") == {"email": "a@example.com", "at": 1}
    assert hallway.store.kv_pop("magic:x") is None


# ------------------------------------------------------------------- logs
@pytest.mark.parametrize("mode", ["dev", "demo", "email"])
async def test_nothing_secret_is_logged(make_client, monkeypatch, caplog, outbox, mode):
    use(monkeypatch, signin_mode=mode)
    caplog.set_level(logging.DEBUG)
    c = make_client()
    r = await c.post("/api/auth/start", json={"email": "asha.rao@example.com"})
    link = r.json()["dev_link"] or link_in(outbox.sent[0][2])
    await c.get(path_of(link))
    token = parse_qs(urlparse(link).query)["t"][0]
    text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert token not in text and link not in text and "asha.rao@example.com" not in text
    assert "sign-in link issued for a***@example.com" in text


def test_access_log_redacts_tokens():
    rec = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4:5", "GET", "/auth/magic?t=SECRET123&x=1", "1.1", 303),
        None,
    )
    main._RedactSecrets().filter(rec)
    assert "SECRET123" not in rec.getMessage() and "t=[redacted]&x=1" in rec.getMessage()
    rec = logging.LogRecord("uvicorn.access", logging.INFO, "", 0, "%s", ("/mcp?key=hw_abc",), None)
    main._RedactSecrets().filter(rec)
    assert rec.getMessage() == "/mcp?key=[redacted]"


def test_mask_email():
    assert mailer.mask_email("asha.rao@example.com") == "a***@example.com"
    assert mailer.mask_email("nope") == "***"


# ------------------------------------------------------- startup checks
def settings(**kw) -> config.Settings:
    base = dict(hallway_env="", public_url="https://hallway.example.com", signin_mode="demo", email_sender="console")
    return dataclasses.replace(config.settings, **{**base, **kw})


@pytest.mark.parametrize("url", ["http://localhost:8787", "http://127.0.0.1:8799"])
def test_dev_mode_allowed_on_localhost(url):
    config.check_signin(settings(signin_mode="dev", public_url=url))


def test_dev_mode_allowed_with_hallway_env_dev():
    config.check_signin(settings(signin_mode="dev", hallway_env="dev"))


@pytest.mark.parametrize("url", ["https://hallway.example.com", "http://localhost.evil.com", "http://10.0.0.5:8787"])
def test_dev_mode_refused_on_public_url(url):
    with pytest.raises(RuntimeError, match="SIGNIN_MODE=dev"):
        config.check_signin(settings(signin_mode="dev", public_url=url))


def test_demo_mode_runs_anywhere():
    config.check_signin(settings(signin_mode="demo"))


def test_email_mode_needs_a_real_sender_in_production():
    with pytest.raises(RuntimeError, match="real sender"):
        config.check_signin(settings(signin_mode="email", email_sender="console"))
    with pytest.raises(RuntimeError, match="SMTP_HOST"):
        config.check_signin(settings(signin_mode="email", email_sender="smtp", smtp_host="", smtp_from=""))
    config.check_signin(
        settings(
            signin_mode="email", email_sender="smtp", smtp_host="smtp.example.com", smtp_from="Hallway <hi@example.com>"
        )
    )
    with pytest.raises(RuntimeError, match="RESEND_API_KEY"):
        config.check_signin(settings(signin_mode="email", email_sender="resend"))
    config.check_signin(
        settings(signin_mode="email", email_sender="resend", resend_api_key="re_x", smtp_from="hi@example.com")
    )


def test_unknown_mode_or_sender_refused():
    with pytest.raises(RuntimeError, match="SIGNIN_MODE"):
        config.check_signin(settings(signin_mode="open"))
    with pytest.raises(RuntimeError, match="EMAIL_SENDER"):
        config.check_signin(settings(email_sender="pigeon"))


@pytest.mark.parametrize(
    "env, mode",
    [
        ({}, "dev"),
        ({"DEV_MAGIC_LINKS": "true"}, "dev"),
        ({"DEV_MAGIC_LINKS": "false"}, "email"),
        ({"DEV_MAGIC_LINKS": "true", "SIGNIN_MODE": "demo"}, "demo"),
        ({"SIGNIN_MODE": " Email "}, "email"),
    ],
)
def test_signin_mode_and_deprecated_alias(monkeypatch, env, mode):
    monkeypatch.delenv("DEV_MAGIC_LINKS", raising=False)
    monkeypatch.delenv("SIGNIN_MODE", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert config._signin_mode() == mode


# ------------------------------------------------------------------ senders
async def test_console_sender_prints(capsys):
    await mailer.ConsoleSender().send("a@example.com", "Hi", "body text")
    assert "body text" in capsys.readouterr().out


async def test_smtp_sender_uses_starttls_and_login(monkeypatch):
    seen = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            seen.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self):
            seen.append(("starttls",))

        def login(self, user, password):
            seen.append(("login", user))

        def send_message(self, msg):
            seen.append(("send", msg["To"], msg["From"], msg["Subject"]))

    monkeypatch.setattr(mailer.smtplib, "SMTP", FakeSMTP)
    s = settings(
        email_sender="smtp",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_user="u",
        smtp_password="p",
        smtp_from="hi@example.com",
        smtp_starttls=True,
    )
    sender = mailer.make_sender(s)
    assert isinstance(sender, mailer.SMTPSender)
    await sender.send("a@example.com", "Subj", "Body")
    assert seen == [
        ("connect", "smtp.example.com", 587),
        ("starttls",),
        ("login", "u"),
        ("send", "a@example.com", "hi@example.com", "Subj"),
    ]


async def test_smtp_failure_is_send_error(monkeypatch):
    def refuse(*a, **kw):
        raise ConnectionRefusedError()

    monkeypatch.setattr(mailer.smtplib, "SMTP", refuse)
    sender = mailer.make_sender(settings(email_sender="smtp", smtp_host="h", smtp_from="f@example.com"))
    with pytest.raises(mailer.SendError):
        await sender.send("a@example.com", "s", "t")


def test_make_sender_picks_resend():
    assert isinstance(mailer.make_sender(settings(email_sender="resend")), mailer.ResendSender)
    assert isinstance(mailer.make_sender(settings(email_sender="console")), mailer.ConsoleSender)


async def test_returning_attendee_dev_mode_still_lands_home(make_client):
    await register(make_client(), "Asha Rao", email="asha@example.com")
    r = await sign_in(make_client(), "asha@example.com")
    assert r.headers["location"] == "/#/home"


async def test_app_refuses_to_start_with_dev_links_on_public_url(monkeypatch):
    use(monkeypatch, signin_mode="dev", hallway_env="", public_url="https://hallway.example.com")
    with pytest.raises(RuntimeError, match="SIGNIN_MODE=dev"):
        async with main.lifespan(main.app):
            pass


# ---------------------------------------------- one email, two onboardings (QA #0)
async def test_second_onboarding_of_an_email_cannot_enter_the_account(make_client, monkeypatch, hallway):
    """Browser A opens a demo link for an email and waits on onboarding; the real
    owner B gets another link, joins and fills the vault. A's register must not
    hand it B's account."""
    use(monkeypatch, signin_mode="demo")
    attacker, owner = make_client(), make_client()
    await sign_in(attacker, "vic@example.com")
    await sign_in(owner, "vic@example.com")
    r = await owner.post("/api/register", json={"name": "Vic Owner", "contact": {"phone": "+91 99999 00001"}})
    assert r.status_code == 200
    r = await attacker.post("/api/register", json={"name": "Vic Attacker"})
    assert r.status_code == 409 and r.json()["code"] == "already_joined"
    assert "99999" not in r.text
    assert (await attacker.get("/api/me")).status_code == 401
    assert len(hallway.store.attendees()) == 1


async def test_join_link_opened_after_the_email_joined_is_not_a_sign_in(make_client, monkeypatch):
    use(monkeypatch, signin_mode="demo")
    attacker, owner = make_client(), make_client()
    r = await attacker.post("/api/auth/start", json={"email": "vic@example.com"})
    held = path_of(r.json()["dev_link"])  # not opened yet
    await register(owner, "Vic Owner", email="vic@example.com")
    r = await attacker.get(held)
    assert r.is_redirect and "joined=1" in r.headers["location"]
    assert (await attacker.get("/api/me")).status_code == 401


async def test_onboarding_session_expires(client, hallway):
    await sign_in(client, "asha@example.com")
    key = f"pre:{main.token_hash(client.cookies.get('hw_session'))}"
    rec = hallway.store.kv_get(key)
    assert rec["expires_at"] <= time.time() + main.ONBOARDING_TTL_S
    hallway.store.kv_set(key, {**rec, "expires_at": time.time() - 1})
    assert (await client.get("/api/me")).status_code == 401
    assert (await client.post("/api/register", json={"name": "Asha Rao"})).status_code == 401


# ------------------------------------------ registration response lost (QA #18)
async def test_registration_whose_response_was_lost_signs_in_on_return(client, monkeypatch, hallway):
    """The person left during the slow registration: the account was made but
    the new cookie never arrived. Their onboarding cookie gets them in."""
    use(monkeypatch, signin_mode="demo")
    await sign_in(client, "asha@example.com")
    pre = client.cookies.get("hw_session")
    assert (await client.post("/api/register", json={"name": "Asha Rao"})).status_code == 200
    client.cookies.clear()
    client.cookies.set("hw_session", pre)  # as if the response never arrived
    r = await client.get("/api/me")
    assert r.status_code == 200 and r.json()["handle"] == "asha"
    fresh = r.cookies.get("hw_session")
    assert fresh and fresh != pre
    client.cookies.clear()
    client.cookies.set("hw_session", fresh)
    assert (await client.get("/api/me")).json()["handle"] == "asha"
    # The onboarding cookie worked once.
    client.cookies.clear()
    client.cookies.set("hw_session", pre)
    assert (await client.post("/api/register", json={"name": "Asha Rao"})).status_code == 401
    assert len(hallway.store.attendees()) == 1
