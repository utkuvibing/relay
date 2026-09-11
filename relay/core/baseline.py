"""Durable build baselines: tracked-workspace scans and pinned snapshots.

P6.2 introduced the in-process baseline contract — one frozen tracked set
captured before the first implementation run so every DIFF is cumulative and
a later PASS certifies the whole change. P6.3 makes that baseline durable:
``persist_baseline`` publishes a crash-safe snapshot under
``.relay/baselines/<task_id>/`` and returns the pin payload the caller writes
to the ledger ONLY after publication is durable; ``load_baseline`` verifies
the pin against the manifest digest and every blob before trusting it — a
missing, partial, or corrupt snapshot fails closed.

Publication order (the pin is always LAST):
  staging dir → per-file tmp → flush+fsync → ``os.replace`` → manifest the
  same way → staging-dir fsync → atomic directory rename onto the final path
  → parent-dir fsync (POSIX; directory handles cannot be fsynced on Windows,
  where this degrades to a best-effort no-op) → caller writes the ledger pin.
"""

from __future__ import annotations

import hashlib
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

import pydantic

from relay.core.reviews import canonical_json
from relay.storage.models import (
    BuildBaselineManifestPayload,
    BuildBaselineRecordPayload,
)

__all__ = [
    "BaselineIntegrityError",
    "baseline_dir",
    "baselines_root",
    "load_baseline",
    "persist_baseline",
]


# One shared exclusion policy for every workspace scan — baseline capture,
# post-run snapshots, and the raw state digest must see exactly the same
# tracked file set, or an ignored tree manufactures phantom deletions.
# ``.relay`` being excluded also keeps the durable snapshots themselves out
# of the tracked set (they live under ``.relay/baselines/``).
_WORKSPACE_EXCLUDED_PARTS = frozenset({".git", ".relay", "node_modules", "__pycache__"})

_BASELINE_FILE_CAP_BYTES = 4 * 1024 * 1024
_BINARY_SNIFF_BYTES = 8000
_STREAM_CHUNK_BYTES = 1024 * 1024


def workspace_path_is_excluded(rel: Path) -> bool:
    """True when a workspace-relative path is outside Relay's tracked set."""
    return any(part in _WORKSPACE_EXCLUDED_PARTS for part in rel.parts)


@dataclass(frozen=True)
class TrackedFileState:
    """State of one tracked workspace path (P6.2).

    ``sha256`` is the file's content identity — always present. ``content``
    is resident only when the file is within the size bound; an oversized
    tracked member streams its SHA-256 in bounded chunks and keeps
    ``content=None``, so membership survives growth without ever loading
    the file wholesale.
    """

    sha256: str
    content: bytes | None


@dataclass(frozen=True)
class WorkspaceBaseline:
    """Frozen tracked-set contract for one build (P6.2).

    ``files`` holds pre-existing tracked contents; ``oversized_paths`` the
    pre-existing paths deliberately left untracked by the size bound. The
    split freezes membership across every attempt: a tracked file growing
    past the cap stays tracked, an oversized file shrinking under the cap
    stays untracked — threshold crossings can never manufacture a phantom
    deletion or creation.
    """

    files: dict[str, TrackedFileState]
    oversized_paths: frozenset[str]


def capture_baseline(root: Path) -> WorkspaceBaseline:
    """Snapshot the working tree Relay could later attribute to a run.

    Bounded: skips the shared exclusion set (``.git``, ``.relay``,
    ``node_modules``, ``__pycache__``) and records — rather than silently
    drops — paths over the size bound, so the post-run scans inherit a
    stable membership contract and pre-existing files are never
    re-adjudicated.
    """
    files: dict[str, TrackedFileState] = {}
    oversized: set[str] = set()
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if workspace_path_is_excluded(rel):
            continue
        name = str(rel).replace("\\", "/")
        try:
            if path.stat().st_size > _BASELINE_FILE_CAP_BYTES:
                oversized.add(name)
                continue
            content = path.read_bytes()
            files[name] = TrackedFileState(
                sha256=hashlib.sha256(content).hexdigest(), content=content
            )
        except OSError:
            continue
    return WorkspaceBaseline(files=files, oversized_paths=frozenset(oversized))


def sha256_file(path: Path) -> str:
    """Bounded streaming SHA-256 — never loads the file wholesale."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_STREAM_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def tracked_workspace_files(
    root: Path, baseline: WorkspaceBaseline
) -> dict[str, TrackedFileState]:
    """Read the current tracked workspace under the baseline's contract.

    Membership is frozen at baseline for pre-existing files: a contract
    member stays tracked whatever it grows to (growth past the cap can
    never fake a deletion); a baseline-oversized path stays untracked
    (shrinkage can never fake a creation). Membership is separate from
    content residency — an oversized contract member's SHA-256 is STREAMED
    in bounded chunks and its bytes never enter memory; files first created
    after the baseline keep the bounded-size policy, deterministic per scan.
    """
    files: dict[str, TrackedFileState] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if workspace_path_is_excluded(rel):
            continue
        name = str(rel).replace("\\", "/")
        if name in baseline.oversized_paths:
            continue
        try:
            if path.stat().st_size > _BASELINE_FILE_CAP_BYTES:
                if name not in baseline.files:
                    continue  # post-baseline files stay bounded
                # Oversized contract member: stream identity, keep no bytes.
                files[name] = TrackedFileState(
                    sha256=sha256_file(path), content=None
                )
                continue
            content = path.read_bytes()
            files[name] = TrackedFileState(
                sha256=hashlib.sha256(content).hexdigest(), content=content
            )
        except OSError:
            continue
    return files


def workspace_state_digest(files: dict[str, TrackedFileState]) -> str:
    """Collision-safe identity of the tracked workspace state (P6.2).

    Rendered diff text is a human-readable representation — binary files
    collapse to ``Binary files <path> differ`` and lossy UTF-8 replacement
    decoding can make distinct raw bytes render identically. No-progress
    decisions therefore hash the tracked identity: sorted normalized paths
    paired with each file's SHA-256 (streamed for oversized members — raw
    bytes are never required to be resident); presence and deletion are
    structural (a path absent from the map can never alias into the digest).
    """
    state = hashlib.sha256()
    for name in sorted(files):
        state.update(name.encode("utf-8"))
        state.update(b"\x00")
        state.update(files[name].sha256.encode("ascii"))
        state.update(b"\x00")
    return state.hexdigest()


def render_workspace_diff(
    baseline: dict[str, TrackedFileState],
    current_files: dict[str, TrackedFileState],
) -> str:
    """Human-readable cumulative diff between two tracked snapshots."""
    changed_paths: set[str] = set()
    for name, before in baseline.items():
        after = current_files.get(name)
        if after != before:
            changed_paths.add(name)
    for name in set(current_files) - set(baseline):
        changed_paths.add(name)

    lines: list[str] = []
    for name in sorted(changed_paths):
        before = baseline.get(name)
        after = current_files.get(name)
        before_bytes = before.content if before is not None else None
        after_bytes = after.content if after is not None else None

        def _text(blob: bytes | None) -> str:
            return blob.decode("utf-8", errors="replace") if blob is not None else ""

        is_binary_before = before_bytes is not None and b"\x00" in before_bytes[
            :_BINARY_SNIFF_BYTES
        ]
        is_binary_after = after_bytes is not None and b"\x00" in after_bytes[
            :_BINARY_SNIFF_BYTES
        ]
        if is_binary_before or is_binary_after:
            lines.append(f"Binary files {name} differ")
            continue
        if after is not None and after.content is None:
            # Oversized contract member — bounded marker instead of a
            # multi-MB unified diff; its streamed SHA-256 still feeds the
            # state digest, so no-progress stays exact.
            lines.append(f"oversized file {name} differs")
            continue

        before_text = _text(before_bytes).splitlines(keepends=True)
        after_text = _text(after_bytes).splitlines(keepends=True)
        if before is None:
            lines.append(f"new file: {name}")
        elif after is None:
            lines.append(f"deleted file: {name}")
        else:
            lines.append(f"modified: {name}")

        import difflib

        for diff_line in difflib.unified_diff(
            before_text, after_text, fromfile=f"a/{name}", tofile=f"b/{name}", lineterm=""
        ):
            lines.append(diff_line.rstrip("\n"))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Durable snapshot: staging → atomic publish → parent fsync → ledger pin last
# ---------------------------------------------------------------------------


class BaselineIntegrityError(RuntimeError):
    """A pinned baseline snapshot is missing, partial, or corrupt.

    Carries a stable ``code`` so callers can refuse with a precise reason —
    baseline problems always fail closed, never silently rebuild.
    """

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


def baselines_root(root: Path) -> Path:
    """``<root>/.relay/baselines`` — inside the shared exclusion set."""
    return root / ".relay" / "baselines"


def baseline_dir(root: Path, task_id: str) -> Path:
    """Canonical snapshot location for one task's pre-implementation tree."""
    return baselines_root(root) / task_id


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename inside it becomes durable (POSIX only).

    Windows cannot fsync directory handles; the call is a no-op there —
    file-level fsync + ``os.replace`` still order every byte write before
    the rename, which is the best the platform offers.
    """
    if os.name != "posix":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_dir(staging: Path, final: Path) -> None:
    """Atomic directory publish: rename the completed staging dir onto the
    canonical (non-existent) target in one operation."""
    os.replace(staging, final)


def persist_baseline(
    root: Path, task_id: str, baseline: WorkspaceBaseline
) -> BuildBaselineRecordPayload:
    """Publish the durable snapshot; return the pin payload for the ledger.

    The returned ``relay.build.baseline.v1`` payload is written to the store
    by the CALLER, strictly after this function returns — a pin can never
    point at a snapshot whose publication was interrupted. A pin-less final
    directory (a crash between rename and pin) is replaced wholesale; this
    function must only run while no pin exists and no attempt has been
    dispatched (the driver enforces both).
    """
    base = baselines_root(root)
    final = baseline_dir(root, task_id)
    # Staging is a sibling of the final snapshot — same volume, so the
    # rename is atomic. The task id alone keeps paths short: every extra
    # byte of staging name lands inside the 260-char Windows MAX_PATH the
    # content-addressed ``blobs/<sha256>`` names already strain.
    staging = base / f"{task_id}.staging"

    if final.exists():
        # Pin-less leftover (crash between rename and pin) — replaceable.
        shutil.rmtree(final)
    if staging.exists():
        shutil.rmtree(staging)
    blobs_dir = staging / "blobs"
    blobs_dir.mkdir(parents=True)

    for name in sorted(baseline.files):
        state = baseline.files[name]
        if state.content is None:
            # A capture-time baseline always carries resident bytes — the
            # size bound moved the file to ``oversized_paths`` instead.
            raise ValueError(f"baseline member '{name}' has no resident content")
        blob = blobs_dir / state.sha256
        if blob.exists():
            continue  # identical content dedupes onto one blob
        tmp = blobs_dir / f"{state.sha256}.tmp"
        with tmp.open("wb") as stream:
            stream.write(state.content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, blob)

    manifest = BuildBaselineManifestPayload(
        schema_version="relay.build.baseline.manifest.v1",
        task_id=task_id,
        files={name: baseline.files[name].sha256 for name in sorted(baseline.files)},
        oversized_paths=tuple(sorted(baseline.oversized_paths)),
    )
    manifest_text = canonical_json(manifest)
    tmp_manifest = staging / "manifest.json.tmp"
    with tmp_manifest.open("w", encoding="utf-8", newline="") as stream:
        stream.write(manifest_text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp_manifest, staging / "manifest.json")

    _fsync_dir(staging)
    _publish_dir(staging, final)
    _fsync_dir(base)

    return BuildBaselineRecordPayload(
        schema_version="relay.build.baseline.v1",
        task_id=task_id,
        manifest_digest=hashlib.sha256(manifest_text.encode("utf-8")).hexdigest(),
        file_count=len(baseline.files),
        oversized_count=len(baseline.oversized_paths),
    )


def load_baseline(
    root: Path, task_id: str, pin: BuildBaselineRecordPayload
) -> WorkspaceBaseline:
    """Verify a pinned snapshot and rebuild the tracked-set contract.

    Fails closed on every deviation — missing manifest, digest mismatch,
    non-canonical or invalid payload, foreign task, count drift, missing or
    corrupt blob — with a stable :class:`BaselineIntegrityError` code.
    """
    final = baseline_dir(root, task_id)
    manifest_path = final / "manifest.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise BaselineIntegrityError(
            "manifest_missing", f"baseline manifest missing for task '{task_id}'"
        ) from exc
    if hashlib.sha256(manifest_bytes).hexdigest() != pin.manifest_digest:
        raise BaselineIntegrityError(
            "manifest_digest_mismatch",
            f"baseline manifest digest drifted from the ledger pin for task '{task_id}'",
        )
    try:
        manifest = BuildBaselineManifestPayload.model_validate_json(manifest_bytes)
    except pydantic.ValidationError as exc:
        raise BaselineIntegrityError(
            "manifest_invalid", f"baseline manifest is invalid for task '{task_id}'"
        ) from exc
    if canonical_json(manifest).encode("utf-8") != manifest_bytes:
        raise BaselineIntegrityError(
            "manifest_invalid",
            f"baseline manifest is not in canonical encoding for task '{task_id}'",
        )
    if manifest.task_id != task_id:
        raise BaselineIntegrityError(
            "manifest_invalid", "baseline manifest names a different task"
        )
    if (
        len(manifest.files) != pin.file_count
        or len(manifest.oversized_paths) != pin.oversized_count
    ):
        raise BaselineIntegrityError(
            "count_mismatch", f"baseline member counts drift from the pin for '{task_id}'"
        )

    files: dict[str, TrackedFileState] = {}
    for name, sha in sorted(manifest.files.items()):
        blob_path = final / "blobs" / sha
        try:
            blob = blob_path.read_bytes()
        except OSError as exc:
            raise BaselineIntegrityError(
                "blob_missing", f"baseline blob '{sha[:12]}...' missing for '{name}'"
            ) from exc
        if hashlib.sha256(blob).hexdigest() != sha:
            raise BaselineIntegrityError(
                "blob_digest_mismatch", f"baseline blob for '{name}' is corrupt"
            )
        files[name] = TrackedFileState(sha256=sha, content=blob)
    return WorkspaceBaseline(files=files, oversized_paths=frozenset(manifest.oversized_paths))
