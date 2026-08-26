"""Static guard-rails over CI/CD workflow definitions.

Adversarial review of PR #2 produced hardening requirements whose regressions
would be invisible to ordinary tests. Each check here binds ONE reviewed
security property so future workflow edits cannot silently drop it:

* immutable SHA pinning of every external action,
* no persisted git credentials in any checkout,
* least-privilege job permissions on Release (write only at publication),
* release-lineage gate present and fail-closed,
* ``--locked`` sync everywhere (lockfile currency), never ``--frozen``,
* installed-wheel smoke exercising init/status outside the checkout,
* tag/package version equality via stdlib tomllib,
* idempotent release publication with asset clobbering.

Purely local file parsing — no network, no runner interaction.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _raw_job_slice(workflow_file: str, job_name: str) -> str:
    """Raw text of one job block (safe_dump refolds multiline run scalars)."""
    text = (WORKFLOWS / workflow_file).read_text(encoding="utf-8")
    start = text.index(f"\n  {job_name}:")
    candidates = [
        text.find(f"\n  {other}:", start + len(job_name) + 3)
        for other in [j for j in _job_names(workflow_file) if j != job_name]
    ]
    ends = [c for c in candidates if c > 0]
    end = min(ends) if ends else len(text)
    return text[start:end]


def _job_names(workflow_file: str) -> list[str]:
    doc = yaml.safe_load((WORKFLOWS / workflow_file).read_text(encoding="utf-8"))
    return list(doc["jobs"])


@pytest.fixture(scope="module")
def ci() -> dict:
    return yaml.safe_load((WORKFLOWS / "ci.yml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def release() -> dict:
    return yaml.safe_load((WORKFLOWS / "release.yml").read_text(encoding="utf-8"))


def _iter_steps(workflow: dict):
    for job in workflow["jobs"].values():
        yield from job.get("steps", [])


def _steps_using(workflow: dict, prefix: str) -> list[dict]:
    return [
        step for step in _iter_steps(workflow) if str(step.get("uses", "")).startswith(prefix)
    ]


class TestImmutablePinning:
    @pytest.mark.parametrize("wf", ["ci", "release"])
    def test_every_external_use_is_sha40_pinned(self, ci, release, wf):
        doc = {"ci": ci, "release": release}[wf]
        refs = [step["uses"] for step in _iter_steps(doc) if step.get("uses")]
        assert refs, f"{wf}.yml: expected at least one action reference"
        for ref in refs:
            pinned = ref.split("@", 1)[1]
            assert SHA_PATTERN.fullmatch(pinned), f"{wf}.yml: '{ref}' is not SHA-pinned"

    def test_setup_uv_pinned_to_the_reviewed_release(self, ci, release):
        for doc in (ci, release):
            for step in _steps_using(doc, "astral-sh/setup-uv"):
                assert step["uses"].endswith(
                    "20cfd1bf945f4377ade1205e4dbc17946fc9a30d"
                ), step["uses"]


class TestCredentialHygiene:
    @pytest.mark.parametrize("wf", ["ci", "release"])
    def test_no_checkout_persists_git_credentials(self, ci, release, wf):
        doc = {"ci": ci, "release": release}[wf]
        checkouts = _steps_using(doc, "actions/checkout")
        assert checkouts, f"{wf}.yml: no checkout steps found"
        for step in checkouts:
            with_block = step.get("with") or {}
            assert (
                with_block.get("persist-credentials") is False
            ), f"{wf}.yml: checkout step '{step.get('name')}' persists credentials"


class TestLeastPrivilegeRelease:
    def test_workflow_scope_grants_only_read(self, release):
        assert release["permissions"] == {"contents": "read"}

    def test_write_appears_only_on_the_publication_job(self, release):
        writers = [
            name
            for name, job in release["jobs"].items()
            if (job.get("permissions") or {}).get("contents") == "write"
        ]
        assert writers == ["release"]

    def test_verify_job_cannot_write(self, release):
        verify = release["jobs"]["verify"]
        perms = verify.get("permissions") or {}
        write_keys = [key for key, value in perms.items() if value == "write"]
        assert not write_keys


class TestReleaseLineageGate:
    def test_gate_present_fail_closed_and_uses_full_history(self):
        verify_text = _raw_job_slice("release.yml", "verify")
        assert "merge-base --is-ancestor" in verify_text
        assert "::error::" in verify_text  # surfaced refusal message
        # fail-closed: an explicit exit 1 guards the refusal path
        assert "exit 1" in verify_text
        # full-history fetch on the verifying checkout
        assert "fetch-depth: 0" in verify_text

    def test_release_job_needs_verified_job(self, release):
        publish = release["jobs"]["release"]
        assert "verify" in publish["needs"]
        gate = publish["if"]
        assert "refs/tags/" in gate, "publication restricted to tag events"


class TestLockfileCurrency:
    @pytest.mark.parametrize("wf", ["ci", "release"])
    def test_syncs_are_locked_not_frozen(self, ci, release, wf):
        doc = {"ci": ci, "release": release}[wf]
        text = yaml.safe_dump(doc)
        assert "--frozen" not in text, f"{wf}.yml still uses --frozen somewhere"
        syncs = [
            step["run"]
            for step in _iter_steps(doc)
            if isinstance(step.get("run"), str) and "uv sync" in step["run"]
        ]
        assert syncs, f"{wf}.yml: expected uv sync steps"
        for run in syncs:
            assert "--locked" in run, f"sync step not currency-enforcing: {run!r}"


class TestInstalledWheelSmoke:
    SMOKE_MARKER: tuple[str, ...] = ("--help", "init", "status", "mktemp -d")

    @pytest.mark.parametrize("wf", ["ci", "release"])
    def test_smoke_goes_beyond_help_in_a_temp_dir(self, ci, release, wf):
        doc = {"ci": ci, "release": release}[wf]
        smoke_runs = [
            step["run"]
            for step in _iter_steps(doc)
            if isinstance(step.get("run"), str) and "--help" in step["run"]
        ]
        assert smoke_runs, f"{wf}.yml: no wheel-smoke step found"
        assert len(smoke_runs) == 1, f"{wf}.yml: unexpected extra smoke steps"
        smoke = smoke_runs[0]
        for marker in self.SMOKE_MARKER:
            assert marker in smoke, f"{wf}.yml smoke missing {marker!r}"
        assert "wc -l" in smoke, f"{wf}.yml smoke must assert exactly one wheel"

    def test_sdist_also_asserted_built_in_ci_package_job(self, ci):
        pkg_text = yaml.safe_dump(ci["jobs"]["package"])
        assert "*.tar.gz" in pkg_text, "package job must verify sdist presence too"


class TestVersionParityCheck:
    def test_tag_equality_uses_tomllib_before_publication(self):
        verify_text = _raw_job_slice("release.yml", "verify")
        assert "tomllib" in verify_text
        assert '["project"]["version"]' in verify_text
        # must be tagged-release-scoped, like the publication it protects
        assert "refs/tags/" in verify_text


class TestIdempotentPublication:
    def test_create_is_guarded_and_assets_clobbered(self):
        publish_text = _raw_job_slice("release.yml", "release")
        assert "gh release view" in publish_text, "must probe existing release first"
        assert "gh release upload" in publish_text and "--clobber" in publish_text
