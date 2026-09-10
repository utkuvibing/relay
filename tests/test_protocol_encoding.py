"""Literal v1 golden vectors and strict repetition/configuration projections."""

from dataclasses import replace

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from relay.agents.base import AgentRole, BackendType
from relay.agents.factory import RegistryAgentFactory
from relay.context.config import AgentConfig, ConfigError, HarnessAgentConfig, RelayConfig
from relay.context.protocols import load_protocol
from relay.core.protocol_encoding import (
    ParticipantConfig,
    decode_definition,
    definition_bytes,
    definition_digest,
    request_id,
    request_identity_bytes,
)
from relay.core.protocols import (
    EvaluationStatus,
    ExpectedOutput,
    ParticipantRequirement,
    ProtocolDefinition,
    ProtocolFactsError,
    ProtocolRepeat,
    RequestState,
    StageBudgets,
    StageContext,
    StageDefinition,
    StageFacts,
    StageRequestFact,
    evaluate_protocol,
    evaluate_stage,
    protocol_schedule,
)
from relay.harness.capabilities import HarnessCapability
from relay.harness.types import ExecutionGrantKind
from relay.storage.models import MessageType


def minimal():
    return ProtocolDefinition(
        "test",
        "1",
        (ParticipantRequirement(AgentRole.ARCHITECT),),
        (
            StageDefinition(
                "analysis",
                (AgentRole.ARCHITECT,),
                (),
                (MessageType.NOTE, MessageType.OPINION),
                StageBudgets(1, 0),
                (ExpectedOutput(AgentRole.ARCHITECT, MessageType.OPINION),),
            ),
        ),
    )


def test_definition_literal_vectors():
    plain = minimal()
    assert definition_bytes(plain) == (
        b'["relay.protocol.definition.v1","test","1",[["architect",[]]],'
        b'[["analysis",["architect"],[],["note","opinion"],[1,0],'
        b'[["architect","opinion"]],[false,false]]],[false],null]'
    )
    assert (
        definition_digest(plain)
        == "protocol-definition:v1:7911c20a0f429033083347396926f527acdf0f72144a7364976bd23ffbe8dbc7"
    )
    repeated = replace(plain, name='Débat,"x', repeat=ProtocolRepeat(("analysis",), 2))
    assert definition_bytes(repeated) == (
        b'["relay.protocol.definition.v1","D\\u00e9bat,\\"x","1",[["architect",[]]],'
        b'[["analysis",["architect"],[],["note","opinion"],[1,0],'
        b'[["architect","opinion"]],[false,false]]],[false],[["analysis"],2]]'
    )
    assert (
        definition_digest(repeated)
        == "protocol-definition:v1:fc396be43d2f25e0cbe236bcb9edb58fe6cef57e665f4a5fd6cd3f022668e14a"
    )
    for definition in (plain, repeated):
        assert (
            decode_definition(definition_bytes(definition).decode(), definition_digest(definition))
            == definition
        )


def test_snapshot_rejects_corruption_noncanonical_and_unknown_versions():
    definition = minimal()
    raw = definition_bytes(definition).decode()
    for bad in (
        raw + " ",
        raw.replace(".v1", ".v2"),
        raw.replace('"test"', '"changed"'),
        "{}",
        "[]",
        "null",
    ):
        with pytest.raises(ValueError, match="invalid persisted"):
            decode_definition(bad, definition_digest(definition))
    with pytest.raises(ValueError):
        decode_definition(raw, "incorrect")


def test_yaml_defaults_formatting_and_order_do_not_change_digest(tmp_path):
    data = TypeAdapter(ProtocolDefinition).dump_python(minimal(), mode="json")
    first = tmp_path / "one.yaml"
    second = tmp_path / "two.yaml"
    first.write_text(yaml.safe_dump(data), encoding="utf-8")
    del data["completion"]
    del data["repeat"]
    del data["stages"][0]["completion"]
    second.write_text("# comment\n" + yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    assert definition_digest(load_protocol(first)) == definition_digest(load_protocol(second))
    assert definition_digest(replace(minimal(), name="test ")) != definition_digest(minimal())
    changed = replace(minimal(), stages=(replace(minimal().stages[0], budgets=StageBudgets(2, 0)),))
    assert definition_digest(changed) != definition_digest(minimal())


def test_participant_and_request_literal_vectors():
    api = ParticipantConfig("a", BackendType.API, "offline", "m")
    assert (
        api.canonical_bytes()
        == b'["relay.protocol.participant-config.v1","a","api","offline","m",null,null,null,null,[],null,null]'
    )
    assert (
        api.fingerprint
        == "participant-config:v1:9912f0442b4b44cf996472d239752b84811b47c41f2e2fe6e9a859af9873b242"
    )
    harness = ParticipantConfig(
        "é",
        BackendType.HARNESS,
        "fake",
        executable="tool",
        timeout=300,
        auth_probe=True,
        capabilities=(HarnessCapability.STRUCTURED_OUTPUT, HarnessCapability.READ_ONLY_ACCESS),
        grant="read_only",
        workspace_root="C:/work",
    )
    assert harness.canonical_bytes() == (
        b'["relay.protocol.participant-config.v1","\\u00e9","harness","fake",null,null,'
        b'"tool",300.0,true,["read_only_access","structured_output"],"read_only","C:/work"]'
    )
    assert (
        harness.fingerprint
        == "participant-config:v1:8b76d29ffdab6dc84773b8450614b5d94283412a77a40adec921615156e511ed"
    )
    assert (
        replace(harness, capabilities=tuple(reversed(harness.capabilities))).fingerprint
        == harness.fingerprint
    )
    assert (
        request_identity_bytes("execution", "stage:v1:abc", AgentRole.ARCHITECT)
        == b'["relay.protocol.request.v1","execution","stage:v1:abc","architect"]'
    )
    assert (
        request_id("execution", "stage:v1:abc", AgentRole.ARCHITECT)
        == "protocol-request:v1:310c5aefa6bce096ed5607d5a56633c7170fba4b85575493d0a8fc25e6b7f290"
    )


@pytest.mark.parametrize("block", [(), ("missing",), ("b", "a"), ("a", "c"), ("a", "a")])
def test_repeat_requires_contiguous_unique_ordered_block(block):
    definition = minimal()
    with pytest.raises(ValueError):
        replace(
            definition,
            stages=tuple(replace(definition.stages[0], id=i) for i in "abc"),
            repeat=ProtocolRepeat(block, 2),
        )


@pytest.mark.parametrize("rounds", [0, 101, True, "2", 1.5])
def test_rounds_are_strict_bounded_integers(rounds):
    with pytest.raises(ValidationError):
        ProtocolRepeat(("analysis",), rounds)


def test_repeated_schedule_prefix_and_occurrence_validation():
    definition = minimal()
    definition = replace(
        definition,
        stages=tuple(replace(definition.stages[0], id=i) for i in "abcd"),
        repeat=ProtocolRepeat(("b", "c"), 2),
    )
    schedule = protocol_schedule(definition)
    assert [(s.id, i) for s, i in schedule] == [
        ("a", 0),
        ("b", 0),
        ("c", 0),
        ("b", 1),
        ("c", 1),
        ("d", 0),
    ]
    results = []
    for index, (stage, occurrence) in enumerate(schedule):
        context = StageContext("test", "1", "key", stage.id, occurrence, "room", None)
        facts = StageFacts(
            context,
            context.stage_key,
            (
                StageRequestFact(
                    AgentRole.ARCHITECT,
                    f"req-{index}",
                    RequestState.SUCCEEDED,
                    f"reply-{index}",
                    MessageType.OPINION,
                ),
            ),
        )
        results.append(evaluate_stage(definition, facts))
        expected = (
            EvaluationStatus.COMPLETE if index == len(schedule) - 1 else EvaluationStatus.CONTINUE
        )
        assert evaluate_protocol(definition, tuple(results)).status is expected
    with pytest.raises(ProtocolFactsError, match="order"):
        evaluate_protocol(definition, (*results[:3], results[1]))


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv("RELAY_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "decoy-secret-123")
    monkeypatch.setenv("UNRELATED_CONFIG", "another-secret")


def configured(*, harness=False):
    return RelayConfig(
        agents={
            "a": AgentConfig(
                backend=BackendType.HARNESS if harness else BackendType.API,
                adapter="codex_cli" if harness else "openai",
                model="model-v1",
            )
        },
        roles={"architect": "a"},
    )


def test_factory_allowlist_uses_effective_settings_and_ignores_unrelated_config(
    clean_env, monkeypatch
):
    config = configured()
    factory = RegistryAgentFactory(config)
    original = factory.protocol_participant(AgentRole.ARCHITECT)
    config.reviewer = "a"
    config.agents["unused"] = config.agents["a"].model_copy(update={"model": "unrelated"})
    assert factory.protocol_participant(AgentRole.ARCHITECT) == original
    assert b"secret" not in original.canonical_bytes()
    monkeypatch.setenv("RELAY_MODEL", "effective-model")
    changed = factory.protocol_participant(AgentRole.ARCHITECT)
    assert changed.model == "effective-model"
    assert changed.fingerprint != original.fingerprint
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.test/v1")
    assert factory.protocol_participant(AgentRole.ARCHITECT).fingerprint != changed.fingerprint


def test_harness_projection_defaults_read_only_and_execution_changes(clean_env):
    config = configured(harness=True)
    factory = RegistryAgentFactory(config, "C:/work")
    original = factory.protocol_participant(AgentRole.ARCHITECT)
    config.agents["a"].harness = HarnessAgentConfig(grant=ExecutionGrantKind.WORKSPACE_WRITE)
    assert factory.protocol_participant(AgentRole.ARCHITECT) == original
    for field, value in (
        ("executable_path", "other"),
        ("timeout_seconds", 42),
        ("auth_probe", False),
    ):
        config.agents["a"].harness = HarnessAgentConfig(**{field: value})
        assert factory.protocol_participant(AgentRole.ARCHITECT).fingerprint != original.fingerprint
    assert (
        RegistryAgentFactory(config, "C:/different")
        .protocol_participant(AgentRole.ARCHITECT)
        .fingerprint
        != original.fingerprint
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://user:decoy-secret@host/v1",
        "https://host/v1?key=decoy-secret",
        "https://host/v1#decoy-secret",
    ],
)
def test_secret_shaped_endpoint_refuses_without_echoing(clean_env, url):
    config = configured()
    config.agents["a"].base_url = url
    with pytest.raises(ConfigError) as error:
        RegistryAgentFactory(config).protocol_participant(AgentRole.ARCHITECT)
    assert "decoy-secret" not in str(error.value)


def test_unstructured_args_have_no_projection(clean_env):
    config = configured(harness=True)
    config.agents["a"].harness = HarnessAgentConfig(extra_args=["--credential=decoy-secret"])
    with pytest.raises(ConfigError) as error:
        RegistryAgentFactory(config).protocol_participant(AgentRole.ARCHITECT)
    assert "decoy-secret" not in str(error.value)
    with pytest.raises(ValidationError):
        ParticipantConfig("a", BackendType.API, "fake", extra_args=["secret"])
