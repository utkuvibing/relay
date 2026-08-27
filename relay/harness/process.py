"""Async subprocess engine with guaranteed process-tree termination (G2).

Every harness run is one :class:`LaunchSpec` → one :class:`ProcessOutcome`:

* spawn with bounded, captured stdout/stderr (drain-discard past the limit so
  a chatty child can neither block nor balloon memory);
* optional stdin delivery (the default prompt channel — prompt text must not
  ride argv, which is world-readable in process listings);
* deadline enforcement through a graceful→hard→tree termination ladder;
* **tree guarantee:** on POSIX the child starts in its own session, so
  signals reach the whole process group; on Windows every child is assigned
  to a Job Object with ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`` (ctypes-only,
  no new dependency), so descendants die with the job even if Relay's own
  process exits abruptly. Job-assignment failure falls back to a
  ``taskkill /T /F`` sweep — direct-child-only killing is never accepted.

Scope note: ``execute()`` never raises for *child* misbehavior (nonzero
exits, timeouts return flag-complete outcomes); spawn failures raise OSError
which the R4 conversion point (:class:`relay.harness.runtime.HarnessAgent.run`)
translates. Cooperative mid-run task cancellation is intentionally out of
scope until Phase 3 loops need it; Windows containment survives interpreter
death via kill-on-close, POSIX does not (documented limitation).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from relay.harness.types import (
    DEFAULT_STREAM_LIMIT_BYTES,
    ExitSemantics,
    ProcessOutcome,
    StreamCapture,
)

_GRACE_SECONDS = 2.0
_PUMP_CLOSE_GRACE_SECONDS = 5.0
_CHUNK_SIZE = 65536


@dataclass(frozen=True)
class LaunchSpec:
    """Everything one child execution needs; environment arrives pre-built."""

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    timeout_s: float
    output_limit_bytes: int = DEFAULT_STREAM_LIMIT_BYTES
    #: Written to stdin then closed; ``None`` means stdin=DEVNULL.
    stdin_data: bytes | None = None


# ---------------------------------------------------------------------------
# Bounded stream capture
# ---------------------------------------------------------------------------


@dataclass
class _StreamSink:
    limit_bytes: int
    chunks: list[str] = field(default_factory=list)
    kept: int = 0
    truncated: bool = False
    lines_seen: int = 0

    def add(self, chunk: bytes) -> None:
        text = chunk.decode("utf-8", errors="replace")
        self.lines_seen += text.count("\n")
        if self.truncated:
            return  # keep draining (never block the child); discard content
        room = max(0, self.limit_bytes - self.kept)
        piece = text[:room]
        if len(piece) < len(text):
            self.truncated = True
        if piece:
            self.chunks.append(piece)
            self.kept += len(piece)

    def capture(self) -> StreamCapture:
        return StreamCapture(
            text="".join(self.chunks), truncated=self.truncated, lines_seen=self.lines_seen
        )


async def _pump(stream: asyncio.StreamReader | None, sink: _StreamSink) -> None:
    if stream is None:
        return
    while True:
        chunk = await stream.read(_CHUNK_SIZE)
        if not chunk:
            break
        sink.add(chunk)


# ---------------------------------------------------------------------------
# Windows job-object support (ctypes only; no third-party dependency)
# ---------------------------------------------------------------------------

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),  # ULONG_PTR
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _KERNEL32.CreateJobObjectW.restype = wintypes.HANDLE
    _KERNEL32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _KERNEL32.SetInformationJobObject.restype = wintypes.BOOL
    _KERNEL32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    _KERNEL32.AssignProcessToJobObject.restype = wintypes.BOOL
    _KERNEL32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _KERNEL32.TerminateJobObject.restype = wintypes.BOOL
    _KERNEL32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _KERNEL32.CloseHandle.restype = wintypes.BOOL
    _KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    _KERNEL32.OpenProcess.restype = wintypes.HANDLE
    _KERNEL32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]


def _create_kill_on_close_job() -> object | None:
    """Create a Job Object that kills all member processes when it closes.

    Returns ``None`` when unsupported or failed — callers fall back to the
    taskkill sweep instead of silently losing tree containment.
    """
    if not _IS_WINDOWS:
        return None
    job = _KERNEL32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = _KERNEL32.SetInformationJobObject(
        job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    )
    if not ok:
        _KERNEL32.CloseHandle(job)
        return None
    return job


def _assign_process_to_job(job: object | None, pid: int) -> bool:
    if not (_IS_WINDOWS and job):
        return False
    handle = _KERNEL32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_KERNEL32.AssignProcessToJobObject(job, handle))
    finally:
        _KERNEL32.CloseHandle(handle)


def _terminate_job_tree(job: object | None) -> bool:
    if not (_IS_WINDOWS and job):
        return False
    try:
        return bool(_KERNEL32.TerminateJobObject(job, 1))
    except OSError:
        return False


def _close_job(job: object | None) -> None:
    if _IS_WINDOWS and job:
        _KERNEL32.CloseHandle(job)


async def _taskkill_sweep(pid: int) -> None:
    """Best-effort ``taskkill /PID <pid> /T /F`` fallback (Windows only)."""
    if not _IS_WINDOWS:
        return
    try:
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(pid),
            "/T",
            "/F",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            await asyncio.wait_for(killer.wait(), timeout=10)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                killer.kill()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Termination ladder: graceful → hard tree → wait
# ---------------------------------------------------------------------------


def _signal_group(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    """Signal the whole process group (POSIX) or the child (Windows)."""
    if _IS_WINDOWS:
        if proc.returncode is None:
            proc.terminate()  # TerminateProcess — Windows has no soft TERM
        return
    os.killpg(os.getpgid(proc.pid), sig)


async def _kill_tree(proc: asyncio.subprocess.Process, job: object | None) -> None:
    """Graceful → hard-tree → last-resort. Never raises for lookup races."""
    # 1) graceful stop of the entire group where the OS allows one
    with contextlib.suppress(OSError, ProcessLookupError):
        if proc.returncode is None:
            _signal_group(proc, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(proc.wait(), timeout=_GRACE_SECONDS)

    # 2) hard tree termination
    hard_ok = _terminate_job_tree(job) if _IS_WINDOWS else True
    if _IS_WINDOWS and not hard_ok:
        await _taskkill_sweep(proc.pid)

    # 3) make certain the direct child itself is gone
    with contextlib.suppress(ProcessLookupError):
        if proc.returncode is None:
            proc.kill()
    if proc.returncode is None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_GRACE_SECONDS)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


async def execute(spec: LaunchSpec) -> ProcessOutcome:
    """Run one child to completion under the G2 guarantees."""
    started = time.monotonic()

    kwargs: dict[str, object] = {
        "cwd": str(spec.cwd),
        "env": dict(spec.env),
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "stdin": asyncio.subprocess.PIPE if spec.stdin_data is not None else subprocess.DEVNULL,
    }
    if _IS_WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True  # own session ⇒ own killable group

    proc = await asyncio.create_subprocess_exec(*spec.argv, **kwargs)

    job = _create_kill_on_close_job()
    _job_assigned = _assign_process_to_job(job, proc.pid)

    out_sink = _StreamSink(spec.output_limit_bytes)
    err_sink = _StreamSink(spec.output_limit_bytes)

    stdin_task: asyncio.Task[None] | None = None
    if spec.stdin_data is not None and proc.stdin is not None:

        async def _write_stdin() -> None:
            writer = proc.stdin
            assert writer is not None
            try:
                writer.write(spec.stdin_data or b"")
                await writer.drain()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass  # child exited before consuming everything — fine
            finally:
                with contextlib.suppress(Exception):
                    writer.close()

        stdin_task = asyncio.create_task(_write_stdin())

    stdout_pump = asyncio.create_task(_pump(proc.stdout, out_sink))
    stderr_pump = asyncio.create_task(_pump(proc.stderr, err_sink))

    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout=max(spec.timeout_s, 0.01))
    except TimeoutError:
        timed_out = True
        await _kill_tree(proc, job)

    # Pipes close when every holder dies; after tree termination that is all
    # done, but belt-and-braces: cut any pump still open rather than hang.
    pumps = [stdout_pump, stderr_pump, *(t for t in (stdin_task,) if t)]
    _, pending = await asyncio.wait(pumps, timeout=_PUMP_CLOSE_GRACE_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    _close_job(job)

    return ProcessOutcome(
        exit_code=proc.returncode,
        timed_out=timed_out,
        cancelled=False,
        stdout=out_sink.capture(),
        stderr=err_sink.capture(),
        duration_s=time.monotonic() - started,
        semantics=ExitSemantics.UNKNOWN,
    )
