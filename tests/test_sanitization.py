"""Persisted-error sanitization units (App. C.4; B09/B16 support)."""

from __future__ import annotations

from relay.harness.sanitization import _MASK, redact


class TestCredentialLiteralRedaction:
    def test_openai_style_key_masked(self):
        out = redact("auth failed for sk-proj-abcdefgh12345678")
        assert "sk-proj-abcdefgh12345678" not in out
        assert _MASK in out

    def test_github_and_aws_shapes_masked(self):
        text = "bad ghp_" + "a" * 30 + " AKIAIOSFODNN7EXAMPLE pat"
        out = redact(text.replace("pat", "github_pat_" + "b" * 24))
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "ghp_" + "a" * 30 not in out
        assert "github_pat_" + "b" * 24 not in out

    def test_bearer_header_masked(self):
        out = redact("Authorization: Bearer abc.def.ghi-jkl")
        assert "Bearer abc.def.ghi-jkl" not in out

    def test_jwt_shape_masked(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9.e30.abcDEF123_-"
        out = redact(f"token={jwt}")
        assert jwt not in out

    def test_env_assignment_masked(self):
        out = redact("launch failed: ANTHROPIC_API_KEY=super-secret-value here")
        assert "super-secret-value" not in out
        assert "ANTHROPIC_API_KEY=" in out  # name remains diagnosable


class TestExplicitSecretRedaction:
    def test_caller_supplied_secret_exact_replace(self):
        out = redact("curl -H 'X-Key: hunter2hunter2' failed", secrets=["hunter2hunter2"])
        assert "hunter2hunter2" not in out

    def test_short_fragments_are_not_redacted(self):
        assert redact("ok", secrets=["ab"]) == "ok"


class TestPathShortening:
    def test_userprofile_paths_are_shortened(self):
        home = r"C:\Users\secretuser"
        text = rf"{home}\fake.exe exited 7"
        out = redact(text, home=home)
        assert "secretuser" not in out
        assert "~\\fake.exe exited 7" in out

    def test_home_from_environment_is_applied(self, monkeypatch):
        monkeypatch.setenv("USERPROFILE", r"C:\Users\envuser")
        monkeypatch.delenv("HOME", raising=False)
        out = redact(r"C:\Users\envuser\harness.log boom")
        assert "envuser" not in out


class TestCrossPlatformPathShortening:
    """Redaction is transport/platform neutral (PR #2 hardening follow-up).

    A harness may print Windows-profile paths while Relay runs on POSIX
    (CI/WSL) and vice versa; masks must hold for either separator syntax
    regardless of which OS Relay itself executes on.
    """

    def test_windows_style_home_masked_when_given_explicitly(self):
        # Simulates a POSIX-hosted Relay whose harness echoes Windows paths;
        # the home value arrives via config/env rather than this host's user.
        out = redact(r"C:\Users\utku\AppData\fake.bin boom", home=r"C:\Users\utku")
        assert "utku" not in out
        assert out == r"~\AppData\fake.bin boom"

    def test_windows_env_home_masks_forward_slash_rendering(self, monkeypatch):
        monkeypatch.setenv("HOME", r"C:\Users\envuser")
        monkeypatch.delenv("USERPROFILE", raising=False)
        out = redact("C:/Users/envuser/harness.log exploded")
        assert "envuser" not in out

    def test_posix_home_variant_masks_on_foreign_host(self, monkeypatch):
        monkeypatch.setenv("HOME", "/opt/runner")
        monkeypatch.delenv("USERPROFILE", raising=False)
        out = redact("/opt/runner/harness.log exploded")
        assert out == "~/harness.log exploded"


class TestRedactionNeverRaises:
    def test_odd_input_survives(self):
        assert redact("") == ""
        weird = "\x00\xff high \\ // ??"
        assert isinstance(redact(weird, secrets=["", None]), str)  # type: ignore[list-item]
