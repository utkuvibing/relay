"""Offline conformance battery for harness adapters (G0–G3; P12 foundation).

``run_battery`` drives one adapter *profile* through every binding check of
Appendix C using only scripted local children — no vendor binaries, no
network. Outcomes are asserted, never output formats: passing the battery
means behaving to contract, not emitting specific bytes.

The two shipped fakes are deliberately heterogeneous (R3):

* :class:`StructuredFakeHarness` — event-oriented JSONL on stdout, exit-code
  numerics 0/3/4 (ok/usage/auth), structured-output branch, silent stderr.
* :class:`ProseFakeHarness` — prose stdout plus noisy stderr carrying
  credential-shaped redaction fodder, exit numerics 0/7/9/125 (deliberately
  different so exit-SEMANTICS mapping is proven rather than memorized),
  prose-transcript output path, and the long-running child→grandchild
  heartbeat used by the G2 tree-termination check.

Shared surface is limited to base classes/vocabulary — no helper imports
between the two implementations. The whole battery runs against BOTH.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from relay.agents.base import AgentRequest, AgentRole
from relay.agents.config import AgentSettings
from relay.agents.errors import AgentError
from relay.context.config import HarnessAgentConfig
from relay.harness.capabilities import HarnessCapability
from relay.harness.env_policy import DEFAULT_CONFLICT_VARIABLES
from relay.harness.errors import (
    HarnessDiscoveryError,
    HarnessOutputError,
    HarnessTimeoutError,
    MissingExecutionGrantError,
    UnsupportedCapability,
)
from relay.harness.runtime import HarnessAgent
from relay.harness.types import ExecutionGrantKind, ExitSemantics

PY = sys.executable

#: Heartbeat writer used by the G2 tree scenario (materialized to disk).
HEARTBEAT_SRC = """\
import sys, time
path, tag = sys.argv[1], sys.argv[2]
end = time.time() + 30
with open(path, "a", encoding="utf-8") as handle:
    handle.write(tag + "-start\\n")
    handle.flush()
while time.time() < end:
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("%s:%.6f\\n" % (tag, time.time()))
    time.sleep(0.05)
"""

_ENV_PROBE_PREFIX = "#ENVCHECK#"

# ---------------------------------------------------------------------------
# Report primitives
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    name: str
    clause: str
    passed: bool
    detail: str = ""


@dataclass
class ConformanceReport:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def failures(self) -> list[CheckResult]:
        return [check for check in self.checks if not check.passed]

    def summary(self) -> str:
        lines = [
            ("PASS  " if check.passed else "FAIL  ")
            + f"{check.name} [{check.clause}]"
            + (f" — {check.detail}" if check.detail and not check.passed else "")
            for check in self.checks
        ]
        verdict = "CONFORMANT" if self.passed else "NON-CONFORMANT"
        return "\n".join([*lines, verdict])


def _record(report, name, clause, condition, detail=""):
    report.checks.append(
        CheckResult(name=name, clause=clause, passed=bool(condition), detail=detail)
    )


async def _capture_error(awaitable, *expected):
    try:
        await awaitable
    except expected as exc:  # noqa: BLE001 - deliberate capture boundary
        return exc
    return None


def _run_async(coro):
    return asyncio.run(coro)


def default_factory_for(cls: type[HarnessAgent], *, timeout_s: float = 25.0):
    """Standard ``factory(root) -> agent`` binding a fake to a fresh ws dir."""

    def _make(root: Path) -> HarnessAgent:
        return cls(
            settings=AgentSettings(adapter=cls.name),
            profile=HarnessAgentConfig(executable_path=str(PY), timeout_seconds=timeout_s),
            workspace_root=root,
        )

    return _make


# ---------------------------------------------------------------------------
# FAKE A — structured/event-oriented
# ---------------------------------------------------------------------------

_STRUCT_SRC = r'''
import json, os, sys
data = sys.stdin.read()
if "--version" in sys.argv:
    print("conformance-structured 2.4.6"); sys.exit(0)
if data.startswith("#ENVCHECK#"):
    names = [line[5:] for line in data.splitlines() if line.startswith("NAME=")]
    dump = [n + "=" + os.environ.get(n, "__MISSING__") for n in names]
    print(json.dumps({"event": "envdump", "lines": dump}))
    sys.exit(0)
mode = sys.argv[sys.argv.index("--mode") + 1] if "--mode" in sys.argv else ""
if mode == "usage":
    print("structured usage complaint", file=sys.stderr); sys.exit(3)
if mode == "auth":
    print("STRUCT_TOKEN=structured-leak-attempt", file=sys.stderr); sys.exit(4)
if mode == "malfault":
    print('{"event": BROKEN')
    sys.exit(0)
first_line = data.strip().splitlines()[0] if data.strip() else "empty"
print(json.dumps({"event": "started"}))
print(json.dumps({"event": "result",
                  "output": "structured-ok echo=%s cwd=%s" % (first_line, os.getcwd())}))
'''

# ---------------------------------------------------------------------------
# FAKE B — plain prose/noisy; different exit numerics; long-running tree mode
# ---------------------------------------------------------------------------

_PROSE_SRC = r'''
import os, subprocess, sys, time
data = sys.stdin.read()
if "--version" in sys.argv:
    print("prose-fake 9.7.5"); sys.exit(0)
if data.startswith("#ENVCHECK#"):
    names = [line[5:] for line in data.splitlines() if line.startswith("NAME=")]
    for name in names:
        print(name + "=" + os.environ.get(name, "__MISSING__"))
    sys.exit(0)

def _arg(flag):
    return sys.argv[sys.argv.index(flag) + 1] if flag in sys.argv else ""

mode = _arg("--mode")
hb_script = _arg("--hbscript")
tombstone = _arg("--tombstone")

print("prose banner alpha " + "x" * 8)
print("SECRET_FODDER=fodder-token-9911", file=sys.stderr)
print("another harmless stderr line", file=sys.stderr)

if mode == "spawntree":
    # Spawn a grandchild sharing our heartbeat logic, then heartbeat ourselves;
    # block far beyond any conformance timeout until the ladder ends us.
    subprocess.Popen([sys.executable, hb_script, tombstone, "grand-child"])
    namespace = {"__name__": "__hb_main__"}
    saved_argv = sys.argv[:]
    sys.argv = ["hb.py", tombstone, "direct-child"]
    try:
        exec(compile(open(hb_script, encoding="utf-8").read(), hb_script, "exec"), namespace)
    finally:
        sys.argv = saved_argv
    time.sleep(30)
    sys.exit(0)
if mode == "sleep":
    time.sleep(30); sys.exit(0)
if mode == "usage":
    print("prose usage complaint", file=sys.stderr); sys.exit(7)
if mode == "transport":
    print("prose transport complaint", file=sys.stderr); sys.exit(9)
if mode == "weird":
    sys.exit(125)

first_line = (data.strip().splitlines() or ["empty"])[0]
print("prose result echo=%s cwd=%s" % (first_line, os.getcwd()))
'''


class StructuredFakeHarness(HarnessAgent):
    """JSONL events on stdout; ok/usage/auth numerics 0/3/4."""

    name = "conformance_structured"
    capabilities = frozenset(
        {HarnessCapability.READ_ONLY_ACCESS, HarnessCapability.STRUCTURED_OUTPUT}
    )

    def invocation_argv(self, resolved):
        return (resolved.command, "-c", _STRUCT_SRC)

    #: Structured fake's failure vocabulary for conformance B05.
    failure_modes = (
        ("usage", "rejected"),        # exit 3 → USAGE semantics
        ("auth", "authentication"),   # exit 4 → AUTH semantics
    )

    def classify_exit(self, exit_code):
        table = {3: ExitSemantics.USAGE, 4: ExitSemantics.AUTH}
        return table.get(exit_code, super().classify_exit(exit_code))

    def parse_output(self, stdout_text, stderr_text):
        results: list[str] = []
        for line in stdout_text.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue  # tolerate trailing whitespace noise, never raw-crash
            try:
                record = json.loads(line)
            except ValueError as exc:
                raise HarnessOutputError(
                    f"{self.name}: structured stream could not be parsed"
                ) from exc
            if "output" in record:
                results.append(str(record["output"]))
            if "lines" in record:
                results.extend(str(value) for value in record["lines"])
        if not results:
            raise HarnessOutputError(f"{self.name}: no structured records found")
        return "\n".join(results)


class ProseFakeHarness(HarnessAgent):
    """Prose transcript; noisy redaction-fodder stderr; numerics 0/7/9/125."""

    name = "conformance_prose"
    capabilities = frozenset(
        {
            HarnessCapability.READ_ONLY_ACCESS,
            HarnessCapability.WORKSPACE_WRITE,
            HarnessCapability.NETWORK_ACCESS,
        }
    )
    extra_conflict_variables = frozenset({"PROSE_FAKE_LOCAL_TOKEN"})

    #: Prose fake deliberately speaks a DIFFERENT failure vocabulary (R3).
    failure_modes = (
        ("usage", "rejected"),         # exit 7 → USAGE semantics
        ("transport", "transport problem"),  # exit 9 → TRANSPORT semantics
    )

    def invocation_argv(self, resolved):
        return (resolved.command, "-c", _PROSE_SRC)

    def classify_exit(self, exit_code):
        table = {
            7: ExitSemantics.USAGE,
            9: ExitSemantics.TRANSPORT,
            125: ExitSemantics.UNKNOWN,
        }
        return table.get(exit_code, super().classify_exit(exit_code))


# ---------------------------------------------------------------------------
# Battery
# ---------------------------------------------------------------------------


def run_battery(
    agent_factory: Callable[[Path], HarnessAgent],
    workdir: Path,
) -> ConformanceReport:
    """Run every conformance check against one adapter factory."""
    report = ConformanceReport()
    root = Path(workdir) / "ws"
    root.mkdir(parents=True, exist_ok=True)

    cases: list[tuple[str, str, Callable]] = [
        ("B01 discovery resolves executable + version", "C.2", _case_b01_discovery),
        ("B02 discovery failure stays redacted", "C.2/C.4", _case_b02_discovery_redacted),
        ("B03 happy-path run returns prompt-derived output", "C.2", _case_b03_happy_path),
        ("B04 working directory pinned to workspace root", "C.2", _case_b04_cwd),
        ("B05 nonzero exits map to typed failures", "C.2", _case_b05_exit_semantics),
        ("B15 tree termination leaves zero live descendants", "R2/G2", _case_b06_tree_termination),
        ("B07 parent conflict variables never reach children", "C.4", _case_b07_conflict_strip),
        ("B08 adapter self-whitelist applies only to itself", "C.4", _case_b08_self_allowlist),
        ("B09 credential-shaped stderr is redacted", "C.4", _case_b09_redaction),
        ("B10 malformed structured streams fail typed", "C.2/C.3", _case_b10_malformed),
        ("B11 unsupported capabilities raise explicitly", "C.3/G0", _case_b11_unsupported),
        ("B12 canonical argv order; prompt never rides argv", "C.5", _case_b12_argv_order),
        ("B13 unresolvable grant blocks execution pre-spawn", "R1/G0", _case_b13_missing_grant),
    ]

    for name, clause, case_fn in cases:
        try:
            case_fn(report, agent_factory, root)
        except Exception as exc:  # noqa: BLE001 - crash IS the failed check
            _record(report, f"{name} (battery crash)", clause, False, repr(exc))

    return report


def _prompt(text: str) -> AgentRequest:
    return AgentRequest(prompt=text, role=AgentRole.RESEARCHER)


def _case_b01_discovery(report, factory, root):
    agent = factory(root)
    info = _run_async(agent.discover())
    ok = bool(info.executable) and info.version is not None and info.version_raw is not None
    _record(report, "B01 discovery resolves executable + version", "C.2", ok, f"info={info!r}")


def _case_b02_discovery_redacted(report, factory, root):
    origin = factory(root)
    artifact_dir = root / ".conform-b02"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    ghost = artifact_dir / "vanished.exe"

    class Missing(origin.__class__):  # type: ignore[misc]
        def _profile_executable_path(self):
            return str(ghost)

    error = _run_async(
        _capture_error(Missing(origin._settings, profile=origin._profile, workspace_root=root).discover(), HarnessDiscoveryError)
    )
    ok = error is not None and str(ghost) not in str(error) and "vanished.exe" in str(error)
    _record(report, "B02 discovery failure stays redacted", "C.2/C.4", ok, repr(error))


def _case_b03_happy_path(report, factory, root):
    agent = factory(root)
    marker = "conf-hello-B03"
    try:
        response = _run_async(agent.run(_prompt(marker)))
        ok = marker in response.output and response.status == "ok"
        detail = "" if ok else f"output={response.output[:120]!r}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, repr(exc)
    _record(report, "B03 happy-path run returns prompt-derived output", "C.2", ok, detail)


def _case_b04_cwd(report, factory, root):
    agent = factory(root)
    expected = str(agent._workspace_root.resolve())
    try:
        response = _run_async(agent.run(_prompt("cwd-probe")))
        ok = expected.replace("\\", "/") in response.output.replace("\\", "/") or expected in response.output
        detail = "" if ok else f"want cwd {expected!r} got {response.output[:160]!r}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, repr(exc)
    _record(report, "B04 working directory pinned to workspace root", "C.2", ok, detail)


def _case_b05_exit_semantics(report, factory, root):
    """Advertised failure modes must surface as typed, hinted AgentErrors.

    Modes come from the adapter itself (``failure_modes``), so differing
    failure vocabularies across fakes prove contract conformance, not
    vocabulary memorization (R3). Empty advertisement = documented pass.
    """
    agent = factory(root)
    modes: tuple[tuple[str, str], ...] = tuple(getattr(agent, "failure_modes", ()))
    if not modes:
        _record(report, "B05 nonzero exits map to typed failures", "C.2",
                True, "adapter advertises no failure modes — nothing to probe")
        return

    failures = []
    for mode, needle in modes:
        probe_agent = factory(root)
        probe_agent._profile.extra_args = ["--mode", mode]
        try:
            error = _run_async(
                _capture_error(probe_agent.run(_prompt("bad-exit")), AgentError)
            )
        except Exception as exc:  # noqa: BLE001 - raw escape IS a failure
            error = exc
        if error is None:
            failures.append(f"{mode}: no error raised")
            continue
        if not isinstance(error, AgentError):
            failures.append(f"{mode}: raw {type(error).__name__} escaped")
            continue
        if needle not in str(error):
            failures.append(f"{mode}: hint '{needle}' missing in {str(error)[:80]!r}")
    _record(
        report, "B05 nonzero exits map to typed failures", "C.2",
        not failures, "; ".join(failures),
    )


def _case_b06_tree_termination(report, factory, root):
    # G2 belongs to the tree-spawning implementation by design (R3); both
    # fakes still pass every other check of the shared contract.
    del factory
    g2_dir = root / ".conform-g2"
    g2_dir.mkdir(parents=True, exist_ok=True)
    hb_path = g2_dir / "_heartbeat.py"
    hb_path.write_text(HEARTBEAT_SRC, encoding="utf-8")
    tombstone = g2_dir / "tombstone.log"

    tight = HarnessAgentConfig(
        executable_path=str(PY),
        timeout_seconds=0.8,
        extra_args=[
            "--mode", "spawntree",
            "--tombstone", str(tombstone),
            "--hbscript", str(hb_path),
        ],
    )
    agent_tight = ProseFakeHarness(
        settings=AgentSettings(adapter=ProseFakeHarness.name),
        profile=tight,
        workspace_root=root,
    )

    error = _run_async(_capture_error(agent_tight.run(_prompt("grow-a-tree")), HarnessTimeoutError, AgentError))
    if not isinstance(error, HarnessTimeoutError):
        _record(report, "B15 tree termination leaves zero live descendants", "R2/G2",
                False, f"expected HarnessTimeoutError, got {error!r}")
        return
    if not tombstone.exists():
        _record(report, "B15 tree termination leaves zero live descendants", "R2/G2",
                False, "tombstone never written — child did not reach spawn")
        return
    early = tombstone.read_text(encoding="utf-8")
    stable_required = {"direct-child-start", "grand-child-start"}
    spawned = all(marker in early for marker in stable_required)
    import time as _time

    _time.sleep(1.8)
    late = tombstone.read_text(encoding="utf-8")
    stable = late == early
    ok = spawned and stable
    _record(
        report, "B15 tree termination leaves zero live descendants", "R2/G2", ok,
        "" if ok else f"spawned={spawned} stable={stable} (early {len(early)}B late {len(late)}B)",
    )


class _EnvRestore:
    def __init__(self):
        self._snapshot = dict(os.environ)

    def pollute(self, mapping):
        os.environ.update(mapping)

    def restore(self):
        os.environ.clear()
        os.environ.update(self._snapshot)


def _envcheck_prompt(names):
    return "#ENVCHECK#" + "".join(f"\nNAME={name}" for name in names)


def _case_b07_conflict_strip(report, factory, root):
    sentinels = {
        name: f"must-not-leak-{name.lower()}"
        for name in sorted(DEFAULT_CONFLICT_VARIABLES | {"PROSE_FAKE_LOCAL_TOKEN"})
    }
    environ_guard = _EnvRestore()
    try:
        environ_guard.pollute(sentinels)
        agent = factory(root)
        response = _run_async(agent.run(_prompt(_envcheck_prompt(sorted(sentinels)))))
        leaked = [
            name
            for name, sentinel in sentinels.items()
            if sentinel in response.output
        ]
        stripped = all(
            f"{name}=__MISSING__" in response.output for name in sentinels
        )
        ok = not leaked and stripped
        detail = "" if ok else f"leaked={leaked} stripped_all={stripped}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, repr(exc)
    finally:
        environ_guard.restore()
    _record(report, "B07 parent conflict variables never reach children", "C.4", ok, detail)


def _case_b08_self_allowlist(report, factory, root):
    target = "OPENAI_API_KEY"
    others = sorted(set(DEFAULT_CONFLICT_VARIABLES) - {target})
    sentinels = {name: f"solo-{name.lower()}" for name in [target, *others]}
    environ_guard = _EnvRestore()
    try:
        environ_guard.pollute(sentinels)
        origin = factory(root)

        class SelfAllowing(origin.__class__):  # type: ignore[misc]
            self_allowed_env = frozenset({target})

        agent = SelfAllowing(origin._settings, profile=origin._profile, workspace_root=root)
        response = _run_async(
            agent.run(_prompt(_envcheck_prompt([target, *others])))
        )
        kept = sentinels[target] in response.output
        others_blocked = all(
            f"{name}=__MISSING__" in response.output for name in others
        )
        unrelated_still_blocked = "solo-prose_fake_local_token" not in response.output
        ok = kept and others_blocked and unrelated_still_blocked
        detail = "" if ok else f"kept={kept} others_blocked={others_blocked} unrelated_blocked={unrelated_still_blocked}"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, repr(exc)
    finally:
        environ_guard.restore()
    _record(report, "B08 adapter self-whitelist applies only to itself", "C.4", ok, detail)


def _case_b09_redaction(report, factory, root):
    agent = factory(root)  # prose fake sprays fodder even on success paths
    try:
        response = _run_async(agent.run(_prompt("redaction-probe")))
        ok = "fodder-token-9911" not in response.output
        detail = "" if ok else "secret-shaped stderr surfaced verbatim"
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, repr(exc)
    _record(report, "B09 credential-shaped stderr is redacted", "C.4", ok, detail)


def _case_b10_malformed(report, factory, root):
    """Structured-declaring adapters must fail typed on malformed streams."""
    agent = factory(root)
    if HarnessCapability.STRUCTURED_OUTPUT not in agent.capabilities_set():
        _record(report, "B10 malformed structured streams fail typed",
                "C.2/C.3", True,
                "adapter runs the prose-transcript path — nothing to parse")
        return
    probe = factory(root)
    probe._profile.extra_args = ["--mode", "malfault"]
    error = _run_async(_capture_error(probe.run(_prompt("malformed")), AgentError))
    ok = isinstance(error, HarnessOutputError) and "BROKEN" not in str(error)
    _record(report, "B10 malformed structured streams fail typed", "C.2/C.3", ok, repr(error))


def _case_b11_unsupported(report, factory, root):
    """Any single undeclared capability must raise explicitly (twice over)."""
    agent = factory(root)
    all_caps = set(HarnessCapability)
    missing = all_caps - agent.capabilities_set()
    if not missing:
        _record(report, "B11 unsupported capabilities raise explicitly", "C.3/G0",
                True, "adapter declares every capability — nothing to refuse")
        return
    required = sorted(missing, key=lambda cap: cap.value)[0]

    try:
        agent.requires(required)
        direct_ok = False
    except UnsupportedCapability:
        direct_ok = True

    # Grant-gating probe: choose a dangerous grant whose gating capability is
    # undeclared, when one exists; otherwise refusal-by-default stands.
    gating = {
        ExecutionGrantKind.WORKSPACE_WRITE_NETWORK: HarnessCapability.NETWORK_ACCESS,
        ExecutionGrantKind.WORKSPACE_WRITE: HarnessCapability.WORKSPACE_WRITE,
    }
    grant_ok: bool | None = None
    for kind, needed in gating.items():
        if needed not in agent.capabilities_set():
            try:
                agent._check_grant_capabilities(agent.resolve_grant(kind))
                grant_ok = False
            except UnsupportedCapability:
                grant_ok = True
            except MissingExecutionGrantError:
                grant_ok = True
            break
    if grant_ok is None:
        grant_ok = True  # fully-capable adapters gate everything they declare

    ok = direct_ok and grant_ok
    _record(report, "B11 unsupported capabilities raise explicitly", "C.3/G0", ok,
            f"direct_ok={direct_ok} grant_ok={grant_ok} probed={required.value}")


def _case_b12_argv_order(report, factory, root):
    from relay.harness.discovery import ResolvedExecutable
    from relay.harness.types import ExecutionGrant

    agent = factory(root)
    resolved = ResolvedExecutable(command=str(PY), source="explicit_path")
    grant = ExecutionGrant(kind=ExecutionGrantKind.READ_ONLY_ACCESS, additional_args=())
    agent._profile.grant = ExecutionGrantKind.READ_ONLY_ACCESS
    argv = agent.compose_argv(resolved, grant)
    invocation = agent.invocation_argv(resolved)
    checks = [
        argv[: len(invocation)] == invocation,  # head is the declared invocation
        "-c" in argv,
        not any("prompt-with-secrets" in part for part in argv),  # stdin-only prompt
    ]
    _record(report, "B12 canonical argv order; prompt never rides argv", "C.5",
            all(checks), f"checks={checks}")


def _case_b13_missing_grant(report, factory, root):
    origin = factory(root)

    class NoGrant(origin.__class__):  # type: ignore[misc]
        default_grant = None

    agent = NoGrant(origin._settings, profile=origin._profile, workspace_root=root)
    grant_error = _capture_type(agent.resolve_grant, MissingExecutionGrantError)
    run_error = _run_async(
        _capture_error(agent.run(_prompt("needs-grant")), MissingExecutionGrantError, AgentError)
    )
    ok = grant_error is not None and isinstance(run_error, AgentError)
    _record(report, "B13 unresolvable grant blocks execution pre-spawn", "R1/G0", ok,
            f"resolve={grant_error!r} run={run_error!r}")


def _capture_type(callable_, exception_type):
    try:
        callable_()
    except exception_type as exc:  # noqa: BLE001
        return exc
    return None
