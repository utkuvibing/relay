"""Context Engine: workspace discovery and the two-file workspace layout.

SPEC reference: §12 (Context Engine), §13 (Project Discovery); App. B.3.

Files owned by this module:

* ``.relay/profile.yaml`` — regenerated discovery facts (SPEC §13). Re-init
  rewrites it; it is never hand-edited.
* ``relay.yaml`` — user-editable config at the workspace root (non-secret
  provider facts only, see :mod:`relay.context.config`).
* ``.relay/relay.sqlite3`` — the canonical Relay database.

The profile mirrors SPEC §13 exactly and carries discovered repository facts
only — no provider/model/backend fields ever. Backend placement is
``relay.yaml``'s job, not discovery's.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from relay.storage.models import Workspace, WorkspaceKind

#: File extensions → language names (SPEC §13 ``project.languages``).
_LANGUAGE_EXTENSIONS: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cc": "cpp",
    ".html": "html",
    ".css": "css",
    ".scss": "scss",
    ".sh": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
}

#: Marker files → framework names. File presence decides; content probes
#: (below) extend this for dependency-manifest files.
_FRAMEWORK_MARKERS: dict[str, tuple[str, ...]] = {
    "nextjs": ("next.config.js", "next.config.mjs", "next.config.ts"),
    "vite": ("vite.config.js", "vite.config.mjs", "vite.config.ts"),
    "sveltekit": ("svelte.config.js", "svelte.config.ts"),
    "astro": ("astro.config.js", "astro.config.mjs", "astro.config.ts"),
    "tailwindcss": ("tailwind.config.js", "tailwind.config.ts"),
    "django": ("manage.py",),
    "remix": ("remix.config.js", "remix.config.ts"),
}

#: Manifest files scanned for framework names, and what to look for.
_MANIFEST_PROBES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("pyproject.toml", ("fastapi", "django", "flask", "streamlit", "pytest")),
    ("package.json", ("react", "vue", "angular", "svelte", "express", "next")),
    ("Cargo.toml", ("axum", "actix", "rocket", "tokio")),
    ("go.mod", ("gin", "echo", "chi")),
)

#: Lock files → package managers (SPEC §13 ``project.package_managers``).
_PACKAGE_MANAGER_MARKERS: dict[str, tuple[str, ...]] = {
    "uv": ("uv.lock",),
    "pnpm": ("pnpm-lock.yaml",),
    "yarn": ("yarn.lock",),
    "npm": ("package-lock.json",),
    "bun": ("bun.lock", "bun.lockb"),
    "poetry": ("poetry.lock",),
    "pip": ("requirements.txt", "requirements-dev.txt"),
    "pipenv": ("Pipfile.lock",),
    "cargo": ("Cargo.lock",),
    "go": ("go.mod", "go.sum"),
}

#: Instruction files probed at the root and under docs/ (SPEC §13).
_INSTRUCTION_FILES = ("AGENTS.md", "CONTRIBUTING.md", "CLAUDE.md")

_EXCLUDED_DIRS = frozenset(
    {".git", ".venv", "venv", "node_modules", "dist", "build", "__pycache__", ".relay", ".idea", ".vscode"}
)


class ProjectProfile(BaseModel):
    """Discovered repository facts (SPEC §13) — never provider configuration.

    ``tests`` maps a label to the command that runs that test suite, e.g.
    ``{"backend": "uv run pytest", "frontend": "pnpm test"}``.
    """

    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    tests: dict[str, str] = Field(default_factory=dict)
    default_branch: str = "main"


@dataclass(frozen=True)
class WorkspaceLayout:
    """The three paths of one Relay workspace (SPEC §13, App. B.3)."""

    root: Path
    profile_path: Path
    config_path: Path
    db_path: Path

    @property
    def data_dir(self) -> Path:
        return self.profile_path.parent


def workspace_layout(root: str | Path) -> WorkspaceLayout:
    root = Path(root).expanduser()
    data_dir = root / ".relay"
    return WorkspaceLayout(
        root=root,
        profile_path=data_dir / "profile.yaml",
        config_path=root / "relay.yaml",
        db_path=data_dir / "relay.sqlite3",
    )


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------


def _walk_source_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIRS)
        files.extend(Path(dirpath) / name for name in filenames)
    return files


def _discover_languages(root: Path) -> list[str]:
    languages: set[str] = set()
    for path in _walk_source_files(root):
        language = _LANGUAGE_EXTENSIONS.get(path.suffix.lower())
        if language is not None:
            languages.add(language)
    return sorted(languages)


def _discover_frameworks(root: Path) -> list[str]:
    frameworks: set[str] = set()
    for framework, markers in _FRAMEWORK_MARKERS.items():
        if any((root / marker).is_file() for marker in markers):
            frameworks.add(framework)
    for manifest, probes in _MANIFEST_PROBES:
        path = root / manifest
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for probe in probes:
            if re.search(rf"\b{re.escape(probe)}\b", text):
                frameworks.add(probe)
    return sorted(frameworks)


def _discover_package_managers(root: Path) -> list[str]:
    managers: set[str] = set()
    for manager, markers in _PACKAGE_MANAGER_MARKERS.items():
        if any((root / marker).is_file() for marker in markers):
            managers.add(manager)
    return sorted(managers)


def _discover_instructions(root: Path) -> list[str]:
    instructions: list[str] = []
    for name in _INSTRUCTION_FILES:
        if (root / name).is_file():
            instructions.append(name)
        elif (root / "docs" / name).is_file():
            instructions.append(f"docs/{name}")
    return instructions


def _discover_tests(root: Path) -> dict[str, str]:
    """Best-effort test commands from manifest conventions."""
    tests: dict[str, str] = {}
    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
        if "[tool.pytest" in text:
            command = "uv run pytest" if (root / "uv.lock").is_file() else "pytest"
            tests["backend"] = command
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            data = yaml.safe_load(package_json.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            data = None
        script = (data or {}).get("scripts", {}).get("test") if isinstance(data, dict) else None
        if script:
            if (root / "pnpm-lock.yaml").is_file():
                prefix = "pnpm"
            elif (root / "yarn.lock").is_file():
                prefix = "yarn"
            elif (root / "bun.lock").is_file() or (root / "bun.lockb").is_file():
                prefix = "bun"
            else:
                prefix = "npm"
            tests["frontend"] = f"{prefix} test"
    if (root / "Cargo.toml").is_file():
        tests["rust"] = "cargo test"
    if (root / "go.mod").is_file():
        tests["go"] = "go test ./..."
    return tests


def _discover_default_branch(root: Path) -> str:
    head = root / ".git" / "HEAD"
    if head.is_file():
        try:
            ref = head.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            ref = ""
        if ref.startswith("ref: refs/heads/"):
            return ref.removeprefix("ref: refs/heads/")
    return "main"


def discover_profile(root: str | Path) -> ProjectProfile:
    """Scan a repository/folder and return its factual profile (SPEC §13)."""
    root = Path(root).expanduser()
    return ProjectProfile(
        languages=_discover_languages(root),
        frameworks=_discover_frameworks(root),
        package_managers=_discover_package_managers(root),
        instructions=_discover_instructions(root),
        tests=_discover_tests(root),
        default_branch=_discover_default_branch(root),
    )


# --------------------------------------------------------------------------
# profile.yaml read/write (regenerated facts; never hand-edited)
# --------------------------------------------------------------------------


def save_profile(root: str | Path, profile: ProjectProfile) -> Path:
    """Write ``.relay/profile.yaml`` in the SPEC §13 shape (``project:`` root)."""
    layout = workspace_layout(root)
    layout.data_dir.mkdir(parents=True, exist_ok=True)
    payload = {"project": profile.model_dump()}
    layout.profile_path.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8"
    )
    return layout.profile_path


def load_profile(root: str | Path) -> ProjectProfile | None:
    """Read ``.relay/profile.yaml``; ``None`` when the workspace is not initialized."""
    path = workspace_layout(root).profile_path
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(data, dict) or "project" not in data:
        return None
    return ProjectProfile.model_validate(data["project"])


# --------------------------------------------------------------------------
# Idempotent init (SPEC §13, M3 contract)
# --------------------------------------------------------------------------


def identity_key(root: str | Path) -> str:
    """Canonical filesystem identity for a workspace row.

    ``os.path.normcase`` makes the key case-insensitive on Windows while
    remaining a no-op on POSIX; ``realpath`` collapses ``..`` and symlinks.
    """
    return os.path.normcase(os.path.realpath(os.fspath(Path(root).expanduser())))


def workspace_kind(root: Path) -> WorkspaceKind:
    return WorkspaceKind.GIT_REPO if (root / ".git").exists() else WorkspaceKind.FOLDER


def initialize_workspace(root: str | Path, conn) -> Workspace:
    """Idempotent ``relay init``: one Workspace row per canonical path.

    Re-init on the same path reuses the existing Workspace id and history,
    refreshes ``.relay/profile.yaml``, and never duplicates the row.
    The caller owns the connection lifecycle (typically ``relay.storage.db``
    on ``.relay/relay.sqlite3``).
    """
    from relay.storage.store import SqliteRelayStore

    store = SqliteRelayStore(conn)
    root = Path(root).expanduser()
    key = identity_key(root)

    profile = discover_profile(root)
    save_profile(root, profile)

    workspace = store.workspace_for_identity(key)
    if workspace is None:
        workspace = Workspace(
            name=root.name or "relay-workspace",
            path=str(Path(os.path.realpath(root))),
            kind=workspace_kind(root),
        )
        store.save_model(workspace)
        store.register_identity(workspace, key)
    return workspace
