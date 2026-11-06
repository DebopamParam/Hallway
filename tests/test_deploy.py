"""The live demo's settings (.env.demo.example) and production-only behaviour."""

import dataclasses
from pathlib import Path

import pytest

from app import config, main

ROOT = Path(__file__).resolve().parent.parent
FIELDS = {
    "HALLWAY_PUBLIC_URL": "public_url",
    "HALLWAY_ENV": "hallway_env",
    "SIGNIN_MODE": "signin_mode",
    "EMAIL_SENDER": "email_sender",
    "GUARD_API_KEY": "guard_api_key",
    "HALLWAY_ADMIN_KEY": "admin_key",
    "DAYPASS_WORKER_KEY": "daypass_worker_key",
    "INVITE_CODE": "invite_code",
}


def demo_env() -> dict[str, str]:
    env = {}
    for line in (ROOT / ".env.demo.example").read_text().splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def demo_settings(**overrides: str) -> config.Settings:
    env = {**demo_env(), **overrides}
    return dataclasses.replace(
        config.settings,
        **{f: env[k] for k, f in FIELDS.items() if k in env},
        invite_limit=int(env["INVITE_LIMIT"]),
        daypass_cap_usd=float(env["DAYPASS_CAP_USD"]),
    )


def test_demo_example_is_an_invite_only_production_demo():
    env = demo_env()
    assert env["SIGNIN_MODE"] == "demo" and env["HALLWAY_ENV"] == "production"
    assert env["INVITE_LIMIT"] == "10" and float(env["DAYPASS_CAP_USD"]) <= 0.25
    assert env["HALLWAY_PUBLIC_URL"].startswith("https://")
    # The keys and the invite code are placeholders scripts/up.sh --demo fills in, never real secrets.
    for k in ("GUARD_API_KEY", "HALLWAY_ADMIN_KEY", "DAYPASS_WORKER_KEY", "INVITE_CODE"):
        assert env[k] == "__RANDOM__", k


def test_demo_example_refuses_to_start_until_keys_are_generated():
    with pytest.raises(RuntimeError, match="random secret"):
        config.check_keys(demo_settings())


def test_demo_example_starts_with_generated_keys():
    s = demo_settings(GUARD_API_KEY="g" * 48, HALLWAY_ADMIN_KEY="a" * 48, DAYPASS_WORKER_KEY="w" * 48)
    config.check_signin(s)
    assert config.check_keys(s) == []
    assert not config.api_docs_enabled(s)


@pytest.mark.parametrize(
    "url, env, on",
    [
        ("http://localhost:8787", "", True),
        ("http://127.0.0.1:8799", "dev", True),
        ("http://localhost:8787", "production", False),
        ("https://hallway.example.com", "", False),
        ("https://hallway.example.com", "production", False),
    ],
)
def test_api_docs_only_on_a_dev_box(url, env, on):
    assert config.api_docs_enabled(dataclasses.replace(config.settings, public_url=url, hallway_env=env)) is on


def test_test_app_is_dev_so_its_docs_are_on():
    # The suite runs with HALLWAY_ENV=dev; a production app is built with all three URLs off.
    assert main.app.docs_url == "/docs" and main.app.openapi_url == "/openapi.json"
