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


class TestRedactionNeverRaises:
    def test_odd_input_survives(self):
        assert redact("") == ""
        weird = "\x00\xff high \\ // ??"
        assert isinstance(redact(weird, secrets=["", None]), str)  # type: ignore[list-item]
