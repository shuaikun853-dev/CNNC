from __future__ import annotations

import pytest

from app.services.remote_diagnostics import sanitize_remote_diagnostic


@pytest.mark.parametrize(
    ("raw", "secret", "expected_marker"),
    [
        ("token=x", "x", "token=[REDACTED]"),
        ("Token=x", "x", "Token=[REDACTED]"),
        ('"token":"x"', "x", "token=[REDACTED]"),
        ("'token': 'x'", "x", "token=[REDACTED]"),
        ("accessToken=x", "x", "accessToken=[REDACTED]"),
        ("access_token=x", "x", "access_token=[REDACTED]"),
        ("refreshToken=x", "x", "refreshToken=[REDACTED]"),
        ("refresh_token=x", "x", "refresh_token=[REDACTED]"),
        ("clientSecret=x", "x", "clientSecret=[REDACTED]"),
        ("client_secret=x", "x", "client_secret=[REDACTED]"),
        ("apiKey=x", "x", "apiKey=[REDACTED]"),
        ("api_key=x", "x", "api_key=[REDACTED]"),
        ("Authorization: Bearer x", "x", "Authorization=[REDACTED]"),
        ("Authorization=Bearer x", "x", "Authorization=[REDACTED]"),
        ("Cookie=x", "x", "Cookie=[REDACTED]"),
        ("cookie: x", "x", "cookie=[REDACTED]"),
        (
            "Cookie: sessionid=alpha; csrftoken=beta",
            "beta",
            "Cookie=[REDACTED]",
        ),
        (
            "Authorization: Basic dXNlcjpwYXNz",
            "dXNlcjpwYXNz",
            "Authorization=[REDACTED]",
        ),
        ("password=x", "x", "password=[REDACTED]"),
        ("secret=x", "x", "secret=[REDACTED]"),
        (
            r'"password":"alpha\"bravo"',
            "bravo",
            "password=[REDACTED]",
        ),
        (
            "JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature123",
            "JWT [REDACTED]",
        ),
        (r"read E:\private\payload.xlsx failed", "payload.xlsx", "[REDACTED_PATH]"),
        (
            r"read E:\folder with spaces\payload.xlsx failed",
            "payload.xlsx",
            "[REDACTED_PATH]",
        ),
        (r"read C:\Users\xxx\secret.txt failed", "secret.txt", "[REDACTED_PATH]"),
        (r"read E:\private_dir failed", "private_dir", "[REDACTED_PATH]"),
        ("read /home/user/private/file.txt failed", "file.txt", "[REDACTED_PATH]"),
        ("read /tmp/private/token.json failed", "token.json", "[REDACTED_PATH]"),
    ],
)
def test_sanitize_remote_diagnostic_redacts_secret_and_path_shapes(
    raw, secret, expected_marker
):
    sanitized = sanitize_remote_diagnostic(raw)

    assert secret not in sanitized
    assert expected_marker in sanitized
    assert "\n" not in sanitized
    assert "\r" not in sanitized


def test_sanitize_remote_diagnostic_preserves_harmless_operational_context():
    raw = "PriorityScopeSafetyError: HTTP 503 during priority readback"
    assert sanitize_remote_diagnostic(raw) == raw


def test_sanitize_remote_diagnostic_is_bounded_and_handles_broken_stringification():
    class BrokenDiagnostic:
        def __str__(self):
            raise ValueError("malformed diagnostic")

    assert sanitize_remote_diagnostic(BrokenDiagnostic()) == "[UNPRINTABLE]"
    assert sanitize_remote_diagnostic("x" * 20, max_length=10) == "xxxxxxx..."
    assert sanitize_remote_diagnostic("x" * 20, max_length=2) == ".."
