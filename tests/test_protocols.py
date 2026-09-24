"""Declarative definitions, frozen identity vectors, and ledger-free decisions."""

from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from relay.agents.base import AgentRole
from relay.context.protocols import ProtocolLoadError, load_protocol
from relay.core.protocols import (
    BudgetExhaustionFact,
    EvaluationReason,
    EvaluationStatus,
    ProtocolDefinition,
    ProtocolFactsError,
    RequestState,
    StageAnswerFact,
    StageCompletion,
    StageContext,
    StageFacts,
    StageRequestFact,
    evaluate_protocol,
    evaluate_stage,
)
from relay.storage.models import MessageType

EXAMPLE = Path(__file__).resolve().parents[1] / "protocols" / "debate.yaml"


def debate():
    return replace(load_protocol(EXAMPLE), repeat=None)


def context(stage_id="independent_analysis", **kwargs):
    return StageContext("debate", "1", "run-1", stage_id, room_id="room-1", **kwargs)


def successful_facts(definition, index=0):
    stage = definition.stages[index]
    ctx = context(stage.id)
    return StageFacts(
        ctx,
        ctx.stage_key,
        tuple(
            StageRequestFact(
                output.role,
                f"{index}-req-{i}",
                RequestState.SUCCEEDED,
                f"{index}-reply-{i}",
                output.type,
            )
            for i, output in enumerate(stage.expected_outputs)
        ),
    )


def test_stage_identity_literal_golden_vector():
    ctx = context()
    assert ctx.canonical_bytes() == (
        b'["relay.stage.identity.v1","debate","1","run-1","independent_analysis",0,"room-1",null]'
    )
    assert (
        ctx.stage_key == "stage:v1:2e4001840a1b4deb608149726194d036f6230e1733e562ee6a9a9c1a7c1c78a8"
    )
    assert replace(ctx).stage_key == ctx.stage_key
    task_only = replace(ctx, room_id=None, task_id="null")
    assert task_only.canonical_bytes() == (
        b'["relay.stage.identity.v1","debate","1","run-1",'
        b'"independent_analysis",0,null,"null"]'
    )
    assert task_only.stage_key == (
        "stage:v1:6410c8875c1f7169fbd2bbc70bb209a00f7c417de27e51167e38480a34d37ef8"
    )


def test_identity_nullable_positions_and_unicode_are_unambiguous():
    ctx = context()
    variants = [
        ctx,
        replace(ctx, task_id="null"),
        replace(ctx, room_id=None, task_id="room-1"),
        replace(ctx, occurrence_index=1),
        replace(ctx, execution_key=" run-1 "),
        replace(ctx, execution_key='x,"y|é'),
        replace(ctx, execution_key='x,"y|e\u0301'),
    ]
    assert len({v.stage_key for v in variants}) == len(variants)
    assert replace(ctx, execution_key='x,"y|é').canonical_bytes() == (
        b'["relay.stage.identity.v1","debate","1","x,\\"y|\\u00e9",'
        b'"independent_analysis",0,"room-1",null]'
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"occurrence_index": True},
        {"occurrence_index": -1},
        {"occurrence_index": "0"},
        {"room_id": None, "task_id": None},
        {"room_id": ""},
        {"protocol_version": 1},
        {"execution_key": ""},
    ],
)
def test_invalid_context_rejected(changes):
    with pytest.raises(ValidationError):
        replace(context(), **changes)


@pytest.mark.parametrize(
    "case",
    [
        "unknown",
        "missing_edges",
        "duplicate_stage",
        "unknown_role",
        "unknown_type",
        "unknown_capability",
        "undeclared_role",
        "output_schedule",
        "system",
        "self_edge",
        "duplicate_edge",
        "blocking_edge",
        "budget_bool",
        "budget_zero",
        "edge_schedule",
        "stage_synthesis",
        "protocol_synthesis",
        "nested_unknown",
    ],
)
def test_definition_validation(case):
    data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    stage = data["stages"][0]
    edge = {"sender": "architect", "recipient": "critic", "types": ["opinion"]}
    if case == "unknown":
        data["provider"] = "forbidden"
    elif case == "missing_edges":
        del stage["edges"]
    elif case == "duplicate_stage":
        data["stages"].append(stage)
    elif case == "unknown_role":
        stage["participants"] = ["wizard"]
    elif case == "unknown_type":
        stage["allowed_message_types"].append("done")
    elif case == "unknown_capability":
        data["participants"][0]["required_capabilities"] = ["magic"]
    elif case == "undeclared_role":
        data["participants"] = data["participants"][1:]
    elif case == "output_schedule":
        stage["expected_outputs"][0]["type"] = "synthesis"
    elif case == "system":
        stage["allowed_message_types"].append("system")
    elif case == "self_edge":
        stage["edges"] = [{**edge, "recipient": "architect"}]
    elif case == "duplicate_edge":
        stage["edges"] = [edge, edge]
    elif case == "blocking_edge":
        stage["edges"] = [{**edge, "blocking_allowed": True}]
    elif case == "budget_bool":
        stage["budgets"]["max_agent_turns"] = True
    elif case == "budget_zero":
        stage["budgets"]["max_agent_turns"] = 0
    elif case == "edge_schedule":
        stage["edges"] = [{**edge, "types": ["rebuttal"]}]
    elif case == "stage_synthesis":
        stage["completion"] = {"require_synthesis": True}
    elif case == "protocol_synthesis":
        data["stages"].pop()
    elif case == "nested_unknown":
        stage["budgets"]["tokens"] = 10
    with pytest.raises(ValidationError):
        TypeAdapter(ProtocolDefinition).validate_python(data)


def test_yaml_duplicate_and_located_errors(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("name: a\nname: b\n", encoding="utf-8")
    with pytest.raises(ProtocolLoadError, match="duplicate YAML key.*name"):
        load_protocol(path)
    path.write_text("name: a\nversion: 1\n", encoding="utf-8")
    with pytest.raises(ProtocolLoadError, match="version"):
        load_protocol(path)


def test_complete_stage_wins_over_final_turn_exhaustion():
    definition = debate()
    facts = replace(
        successful_facts(definition), budget_exhaustion=BudgetExhaustionFact("stage", "turn")
    )
    assert evaluate_stage(definition, facts).status is EvaluationStatus.COMPLETE


@pytest.mark.parametrize(
    "state, expected",
    [
        (RequestState.MISSING, EvaluationStatus.CONTINUE),
        (RequestState.PENDING, EvaluationStatus.CONTINUE),
        (RequestState.FAILED, EvaluationStatus.BLOCKED),
    ],
)
def test_incomplete_request_states(state, expected):
    definition = debate()
    facts = successful_facts(definition)
    first = StageRequestFact(
        AgentRole.ARCHITECT, None if state is RequestState.MISSING else "q", state
    )
    assert (
        evaluate_stage(definition, replace(facts, requests=(first, *facts.requests[1:]))).status
        is expected
    )


def test_synthesis_and_early_stop_cannot_skip_required_outputs():
    definition = debate()
    stage = replace(
        definition.stages[3],
        completion=StageCompletion(require_synthesis=True, early_stop_on_answered=True),
    )
    definition = replace(definition, stages=(*definition.stages[:3], stage))
    facts = successful_facts(definition, 3)
    wrong = replace(facts.requests[0], reply_type=MessageType.NOTE)
    incomplete = replace(facts, requests=(wrong,), answers=(StageAnswerFact("seed", "answer"),))
    assert evaluate_stage(definition, incomplete).reason is EvaluationReason.SYNTHESIS_REQUIRED
    result = evaluate_stage(definition, facts)
    assert result.synthesis_message_ids == (facts.requests[0].reply_id,)


def test_early_stop_requires_nonempty_all_answered_and_stays_stage_local():
    definition = debate()
    stage = replace(definition.stages[0], completion=StageCompletion(early_stop_on_answered=True))
    definition = replace(definition, stages=(stage, *definition.stages[1:]))
    ctx = context()
    facts = StageFacts(
        ctx,
        ctx.stage_key,
        tuple(StageRequestFact(role, None, RequestState.MISSING) for role in stage.participants),
    )
    assert evaluate_stage(definition, facts).status is EvaluationStatus.CONTINUE
    half = replace(facts, answers=(StageAnswerFact("a", "reply"), StageAnswerFact("b")))
    assert evaluate_stage(definition, half).status is EvaluationStatus.CONTINUE
    result = evaluate_stage(definition, replace(facts, answers=(StageAnswerFact("a", "reply"),)))
    assert result.reason is EvaluationReason.ANSWERED
    assert evaluate_protocol(definition, (result,)).status is EvaluationStatus.CONTINUE


def test_protocol_completion_requires_order_identity_and_synthesis():
    definition = debate()
    results = tuple(evaluate_stage(definition, successful_facts(definition, i)) for i in range(4))
    assert evaluate_protocol(definition, results).status is EvaluationStatus.COMPLETE
    assert evaluate_protocol(definition, results[:3]).status is EvaluationStatus.CONTINUE
    with pytest.raises(ProtocolFactsError, match="order"):
        evaluate_protocol(definition, (results[1], results[0]))
    other = replace(results[1].context, execution_key="other")
    with pytest.raises(ProtocolFactsError, match="different executions"):
        evaluate_protocol(
            definition, (results[0], replace(results[1], context=other, stage_key=other.stage_key))
        )
    no_synthesis = (*results[:3], replace(results[3], synthesis_message_ids=()))
    assert evaluate_protocol(definition, no_synthesis).reason is EvaluationReason.SYNTHESIS_REQUIRED


def test_facts_reject_duplicates_and_wrong_identity():
    definition = debate()
    facts = successful_facts(definition)
    with pytest.raises(ProtocolFactsError, match="stage key"):
        evaluate_stage(definition, replace(facts, stage_key="foreign"))
    with pytest.raises(ProtocolFactsError, match="exactly one"):
        evaluate_stage(definition, replace(facts, requests=(facts.requests[0],) * 3))
