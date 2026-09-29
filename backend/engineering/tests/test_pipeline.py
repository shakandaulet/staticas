"""
Tests for everything that does not need SolidWorks, Gemini or a network.

WHY THAT COVERS MOST OF IT
--------------------------
The design puts the consequential decisions in places that can be
tested: unit conversion is arithmetic, narrowing is table lookup,
sequencing is a sort, emission is string formatting and validation is an
AST walk. Only the planner needs the API, and only the runner needs
SolidWorks.

So this suite exercises the whole path -- plan to validated script --
with hand-built plans standing in for the model. A regression in
the part that turns "500 kN" into 500000.0 N, or in the part that
refuses a 2 mm mesh, fails here in under a second.

    python -m engineering.tests.test_pipeline        (from the parent dir)
    pytest engineering/tests/test_pipeline.py
"""

from __future__ import annotations

import ast
import math

import pytest

from ..codegen import emitter
from ..codegen.validator import validate
from ..core import domains as D
from ..core import physics, planner
from ..core import units as U
from ..core.ops import narrow
from ..core.schema import Plan, QSlot, RawOp, Selector


# ==========================================================================
# Units
# ==========================================================================
def test_scale_conversions():
    assert U.convert(500, "kN", U.FORCE).value == pytest.approx(500_000.0)
    assert U.convert(150, "psi", U.PRESSURE).value == pytest.approx(1_034_213.6, rel=1e-4)
    assert U.convert(60, "mph", U.VELOCITY).value == pytest.approx(26.8224)
    assert U.convert(2.5, "in", U.LENGTH).value == pytest.approx(0.0635)
    assert U.convert(10, "GPM", U.VOLUME_FLOW).value == pytest.approx(6.309e-4, rel=1e-3)


def test_temperature_is_not_a_scale_factor():
    """A LEVEL and a DIFFERENCE convert differently, and confusing them
    turns 'warm it by 20 degrees' into 'hold it at 20 degrees'."""
    assert U.convert(20, "degC", U.TEMPERATURE).value == pytest.approx(293.15)
    assert U.convert(20, "degC", U.TEMPERATURE_DELTA).value == pytest.approx(20.0)
    assert U.convert(68, "degF", U.TEMPERATURE).value == pytest.approx(293.15)
    assert U.convert(36, "degF", U.TEMPERATURE_DELTA).value == pytest.approx(20.0)


def test_absolute_zero_is_rejected():
    with pytest.raises(U.UnitError):
        U.convert(-300, "degC", U.TEMPERATURE)


def test_wrong_kind_is_rejected():
    """The check that stops a length landing in a force slot."""
    with pytest.raises(U.UnitError):
        U.convert(5, "m", U.FORCE)


def test_unknown_unit_names_the_alternatives():
    with pytest.raises(U.UnitError) as excinfo:
        U.convert(5, "furlongs", U.LENGTH)
    assert "furlongs" in str(excinfo.value)


def test_parse_accepts_a_comma_decimal():
    assert U.parse("2,5 m", U.LENGTH).value == pytest.approx(2.5)


def test_humanize_reaches_for_engineering_units():
    assert U.humanize(U.Quantity(3.4e8, U.PRESSURE)) == "340 MPa"
    assert U.humanize(U.Quantity(5000.0, U.FORCE)) == "5 kN"
    assert U.humanize(U.Quantity(373.15, U.TEMPERATURE)) == "100 degC"


# ==========================================================================
# Narrowing
# ==========================================================================
def _force_op(value=500.0, unit="kN", **options):
    return RawOp(op="add_force",
                 target=Selector(kind="active_selection"),
                 quantities=[QSlot(name="magnitude", value=value, unit=unit)],
                 options=options, note="Tip load")


def test_narrow_converts_to_si():
    op, questions = narrow(_force_op(), "static")
    assert not questions
    assert op.num("magnitude") == pytest.approx(500_000.0)
    # The original survives, so the plan card can show what was typed.
    assert op.q["magnitude"].original == "500 kN"


def test_missing_required_quantity_becomes_a_question():
    raw = RawOp(op="add_convection", target=Selector(),
                quantities=[QSlot(name="film_coefficient", value=25, unit="W/m2K")])
    op, questions = narrow(raw, "thermal")
    assert op is None
    assert any("bulk temperature" in q.question.lower() for q in questions)


def test_wrong_unit_kind_becomes_a_question_not_a_crash():
    op, questions = narrow(_force_op(value=5, unit="m"), "static")
    assert op is None
    assert questions and "force" in questions[0].question.lower()


def test_operation_outside_its_domain_is_refused():
    op, questions = narrow(_force_op(), "flow_internal")
    assert op is None
    assert "not available" in questions[0].question


def test_invalid_option_value_lists_the_valid_ones():
    op, questions = narrow(_force_op(direction="sideways"), "static")
    assert op is None
    assert "normal" in questions[0].options


def test_defaults_are_applied_and_recorded_as_assumptions():
    raw = RawOp(op="add_gravity", options={"direction": "against_y"})
    op, questions = narrow(raw, "static")
    assert not questions
    assert op.num("magnitude") == pytest.approx(9.80665)
    assert any("9.80665" in a for a in op.assumptions)


def test_a_stray_quantity_is_surfaced_rather_than_dropped():
    """A slot the operation has no home for means the model
    misunderstood the step. Dropping it silently hides that."""
    raw = RawOp(op="add_force", target=Selector(),
                quantities=[QSlot(name="magnitude", value=1, unit="kN"),
                            QSlot(name="temperature", value=300, unit="K")])
    op, questions = narrow(raw, "static")
    assert op is None
    assert any("temperature" in q.question for q in questions)


def test_missing_target_defaults_to_the_live_selection():
    raw = RawOp(op="add_fixture", options={"type": "fixed"})
    op, questions = narrow(raw, "static")
    assert not questions
    assert op.target.kind == "active_selection"
    assert any("selected in SolidWorks" in a for a in op.assumptions)


# ==========================================================================
# Domains
# ==========================================================================
@pytest.mark.parametrize("text,expected", [
    ("cantilever beam with a 500 N load, find the max stress", "static"),
    ("how hot does this heatsink get with 20 W", "thermal"),
    ("pressure drop through this pipe at 2 m/s", "flow_internal"),
    ("drag coefficient of this wing at 100 km/h", "flow_external"),
    ("what are the natural frequencies", "frequency"),
    ("напряжение в консольной балке под нагрузкой", "static"),
    ("温度 не важно: нагрев радиатора, конвекция", "thermal"),
    ("аэродинамическое сопротивление крыла", "flow_external"),
    ("перепад давления в трубе, расход", "flow_internal"),
    ("собственные частоты и резонанс", "frequency"),
])
def test_classification_in_both_languages(text, expected):
    """Russian is inflected, so the keyword table matches stems. The
    classifier is only a prior for the planner -- but it decides which
    operations the planner is shown, so it is worth testing."""
    assert D.classify(text)[0] == expected


def test_completeness_catches_a_load_with_no_restraint():
    gaps = D.completeness("static", ["apply_material", "add_force", "solve"])
    assert any("fixture" in g for g in gaps)


def test_completeness_catches_heat_with_no_way_out():
    gaps = D.completeness("thermal", ["apply_material", "add_heat_power", "solve"])
    assert any("leave" in g for g in gaps)


def test_completeness_catches_internal_flow_with_one_boundary():
    gaps = D.completeness("flow_internal",
                          ["set_flow_domain", "add_flow_bc", "add_goal", "solve"])
    assert any("inlet AND an outlet" in g for g in gaps)


# ==========================================================================
# Physics
# ==========================================================================
def test_reynolds_and_mach():
    # Water at 2 m/s in a 25 mm pipe: firmly turbulent.
    assert physics.reynolds(2.0, 0.025, "water") == pytest.approx(49_800, rel=0.02)
    assert physics.mach(343.2, "air") == pytest.approx(1.0, rel=1e-3)


def test_cantilever_closed_forms():
    """A 2 m steel beam, 50 x 100 mm, 5 kN on the tip."""
    E = 205e9
    I = physics.second_moment_rect(0.05, 0.1)
    assert I == pytest.approx(4.1667e-6, rel=1e-3)
    tip = physics.cantilever_tip_deflection(5000.0, 2.0, E, I)
    assert tip == pytest.approx(0.01561, rel=1e-3)
    # The same total spread along the span deflects 3/8 as much.
    udl = physics.cantilever_udl_deflection(5000.0, 2.0, E, I)
    assert udl / tip == pytest.approx(0.375, rel=1e-6)


def test_euler_end_conditions_span_a_factor_of_sixteen():
    E, I, L = 205e9, 4.1667e-6, 2.0
    free = physics.euler_critical_load(E, I, L, "fixed_free")
    fixed = physics.euler_critical_load(E, I, L, "fixed_fixed")
    assert fixed / free == pytest.approx(16.0, rel=1e-6)


def test_friction_factor_is_exact_in_the_laminar_range():
    assert physics.darcy_friction_factor(1000.0) == pytest.approx(0.064)


def test_preflight_blocks_a_turbulent_flow_solved_as_laminar():
    ops = _narrowed([
        RawOp(op="set_geometry", options={"source": "primitive",
                                          "primitive": "round_bar"},
              quantities=[QSlot(name="diameter", value=25, unit="mm"),
                          QSlot(name="length", value=1, unit="m")]),
        RawOp(op="set_flow_domain",
              options={"analysis_type": "internal", "fluid": "water",
                       "flow_type": "laminar"},
              quantities=[QSlot(name="ambient_temperature", value=20, unit="degC"),
                          QSlot(name="velocity", value=2, unit="m/s")]),
    ], "flow_internal")
    report = physics.preflight("flow_internal", ops)
    assert report.blocking
    assert any("laminar" in f.message for f in report.blocking)


def test_preflight_blocks_a_structural_study_with_no_restraint():
    ops = _narrowed([_force_op()], "static")
    report = physics.preflight("static", ops)
    assert any("No restraint" in f.message for f in report.blocking)


def test_preflight_flags_an_impossible_film_coefficient():
    ops = _narrowed([
        RawOp(op="add_convection", target=Selector(),
              quantities=[QSlot(name="film_coefficient", value=1e6, unit="W/m2K"),
                          QSlot(name="bulk_temperature", value=20, unit="degC")]),
    ], "thermal")
    report = physics.preflight("thermal", ops)
    assert any("not physical" in f.message for f in report.blocking)


def _narrowed(raws, domain):
    out = []
    for raw in raws:
        op, questions = narrow(raw, domain)
        assert not questions, questions
        out.append(op)
    return out


# ==========================================================================
# Emission
# ==========================================================================
def _static_plan():
    return _narrowed([
        RawOp(op="set_geometry",
              options={"source": "primitive", "primitive": "rectangular_beam"},
              quantities=[QSlot(name="width", value=50, unit="mm"),
                          QSlot(name="height", value=100, unit="mm"),
                          QSlot(name="length", value=2, unit="m")],
              note="Build the beam"),
        RawOp(op="apply_material", options={"name": "Plain Carbon Steel"}),
        RawOp(op="add_fixture", target=Selector(kind="extreme", axis="z",
                                                side="min"),
              options={"type": "fixed"}, note="Clamp the root"),
        _force_op(),
        RawOp(op="set_mesh", quantities=[
            QSlot(name="max_element", value=50, unit="mm"),
            QSlot(name="min_element", value=2.5, unit="mm")]),
        RawOp(op="solve"),
        RawOp(op="extract_results"),
    ], "static")


def test_emitted_script_is_valid_python():
    out = emitter.emit("static", _static_plan(), summary="Tip load on a beam")
    ast.parse(out.code)          # raises on failure


def test_emitted_script_passes_its_own_validator():
    out = emitter.emit("static", _static_plan())
    verdict = validate(out.code, "static")
    assert verdict.ok, verdict.report()


def test_emission_orders_the_plan_for_the_api():
    """The model emits steps in the order the user said them. SolidWorks
    needs material before mesh and mesh before loads."""
    shuffled = list(reversed(_static_plan()))
    out = emitter.emit("static", shuffled)
    code = out.code
    assert code.index("create_study") < code.index("apply_material")
    assert code.index("apply_material") < code.index("create_mesh")
    assert code.index("create_mesh") < code.index("add_force")
    assert code.index("add_force") < code.index("solve(study)")


def test_mesh_floor_survives_a_finer_request():
    """A request for a 2 mm mesh comes back at the floor, not honoured."""
    ops = _narrowed([
        RawOp(op="set_mesh", quantities=[
            QSlot(name="max_element", value=2, unit="mm"),
            QSlot(name="min_element", value=0.1, unit="mm")]),
        RawOp(op="add_fixture", options={"type": "fixed"}),
        RawOp(op="solve"),
    ], "static")
    out = emitter.emit("static", ops)
    assert "create_mesh(study, 25.0, 1.0" in out.code
    assert validate(out.code, "static").ok


def test_active_selection_reaches_the_script():
    """The whole point of 'apply it to THIS edge': the selector has to
    survive all the way into the emitted call."""
    out = emitter.emit("static", _narrowed([
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="active_selection", entity_type="EDGE",
                              said="этот край")),
        RawOp(op="solve"),
    ], "static"))
    assert '"kind": "active_selection"' in out.code
    assert '"entity_type": "EDGE"' in out.code


def test_unverified_operations_are_declared_in_the_header():
    ops = _narrowed([
        RawOp(op="set_flow_domain",
              options={"analysis_type": "external", "fluid": "air"},
              quantities=[QSlot(name="ambient_temperature", value=20, unit="degC"),
                          QSlot(name="velocity", value=100, unit="km/h")]),
        RawOp(op="add_goal", options={"quantity": "drag_force"}),
        RawOp(op="solve"),
    ], "flow_external")
    out = emitter.emit("flow_external", ops)
    assert "set_flow_domain" in out.unverified_ops
    assert "API CONFIDENCE" in out.code


def test_thermal_plan_emits_and_validates():
    ops = _narrowed([
        RawOp(op="apply_material", options={"name": "6061 Alloy"}),
        RawOp(op="add_heat_power", target=Selector(kind="body"),
              quantities=[QSlot(name="power", value=20, unit="W")]),
        RawOp(op="add_convection", target=Selector(kind="all_faces"),
              quantities=[QSlot(name="film_coefficient", value=12, unit="W/m2K"),
                          QSlot(name="bulk_temperature", value=25, unit="degC")]),
        RawOp(op="set_mesh"),
        RawOp(op="solve"),
        RawOp(op="extract_results"),
    ], "thermal")
    out = emitter.emit("thermal", ops)
    ast.parse(out.code)
    verdict = validate(out.code, "thermal")
    assert verdict.ok, verdict.report()
    # 25 degC must have become kelvin somewhere between here and there.
    assert "298.15" in out.code


# ==========================================================================
# Validation
# ==========================================================================
def test_validator_catches_a_com_property_called_as_a_method():
    verdict = validate("study.RunAnalysis()\n", "static")
    assert not verdict.ok
    assert "property" in verdict.report()


def test_validator_does_not_flag_prose_about_the_mistake():
    """The knowledge base quotes `study.RunAnalysis()` as a
    counter-example. A regex over raw text flags that prose; an AST
    walk does not."""
    code = ('"""Never write study.RunAnalysis() -- it is a property."""\n'
            "import win32com.client\n"
            "add_restraint(study, faces)\n"
            "create_mesh(study, 50.0, 2.5)\n")
    assert validate(code, "static").ok


def test_validator_blocks_dangerous_imports():
    verdict = validate("import subprocess\n", "static")
    assert not verdict.ok
    assert "subprocess" in verdict.report()


def test_validator_fails_closed_on_an_uncheckable_mesh_size():
    """The regression that hung SolidWorks: a size argument that cannot
    be resolved was being skipped instead of blocked."""
    code = ("import win32com.client\n"
            "add_restraint(study, f)\n"
            "study.CreateMesh(0, compute_size(), 2.5)\n")
    verdict = validate(code, "static")
    assert not verdict.ok
    assert "Cannot verify" in verdict.report()


def test_validator_resolves_a_named_constant_before_judging_it():
    code = ("import win32com.client\n"
            "add_restraint(study, f)\n"
            "MAX_MM = 2.0\n"
            "MIN_MM = 0.5\n"
            "study.CreateMesh(0, MAX_MM, MIN_MM)\n")
    verdict = validate(code, "static")
    assert not verdict.ok
    assert "too fine" in verdict.report()


def test_validator_blocks_a_structural_script_with_no_restraint():
    code = "import win32com.client\nstudy.CreateMesh(0, 50.0, 2.5)\n"
    verdict = validate(code, "static")
    assert not verdict.ok
    assert "restraint" in verdict.report()


def test_validator_blocks_thermal_with_no_heat_sink():
    code = ("import win32com.client\n"
            "add_heat_power(study, faces, 20.0)\n"
            "create_mesh(study, 50.0, 2.5)\n")
    verdict = validate(code, "thermal")
    assert not verdict.ok
    assert "heat to" in verdict.report()


# ==========================================================================
# Retrieval
# ==========================================================================
def test_index_builds_and_retrieves_offline():
    """The hashing backend is what this suite runs on and what takes over
    when an embedding call fails. It still has to find the right chunk
    for an exact method name."""
    from ..rag.index import Embedder, KnowledgeBase

    kb = KnowledgeBase.build(Embedder(prefer="hash"), reuse_cache=False)
    assert kb.stats()["chunks"] > 20

    hits = kb.search("SelectByID2 face selection by coordinate", k=5)
    assert hits
    assert any("select" in c.title.lower() or "SelectByID2" in c.text
               for c, _ in hits)


def test_retrieval_filters_by_domain():
    from ..rag.index import Embedder, KnowledgeBase

    kb = KnowledgeBase.build(Embedder(prefer="hash"), reuse_cache=False)
    hits = kb.search("boundary condition", k=6, domains=["thermal"])
    for chunk, _ in hits:
        assert not chunk.domains or "thermal" in chunk.domains


def test_a_failed_embedding_degrades_retrieval_instead_of_crashing(
        tmp_path, monkeypatch):
    """The first live run hit the free-tier embedding quota half-way
    through a rebuild, and the stand-in vectors were 512 wide in a 768
    index: the build crashed. Stand-ins now match the width, are marked,
    stay out of the cache, and a failed query falls back to BM25."""
    from ..rag import index as I

    monkeypatch.setattr(I, "VECTORS_PATH", tmp_path / "v.npz")
    monkeypatch.setattr(I, "CHUNKS_PATH", tmp_path / "c.json")

    class Failing(I.Embedder):
        def __init__(self):
            super().__init__(prefer="hash")
            self.backend, self.dim = "gemini", 768

        def encode(self, texts, is_query=False):
            return None

    kb = I.KnowledgeBase.build(Failing())
    assert kb.vectors.shape[1] == 768
    import json as _json
    meta = _json.loads((tmp_path / "c.json").read_text(encoding="utf-8"))
    assert all(rec["degraded"] for rec in meta["chunks"])

    hits = kb.search("CreateNewStudy3 study type", k=4)
    assert hits and any("CreateNewStudy3" in c.text for c, _ in hits)


def test_retry_delay_reads_the_rate_limit_and_ignores_other_errors():
    from ..rag.index import _retry_delay

    quota = Exception("429 RESOURCE_EXHAUSTED ... Please retry in 35.97s.")
    assert _retry_delay(quota) == pytest.approx(36.97)
    assert _retry_delay(Exception("400 API key not valid")) is None


def test_camel_case_is_split_for_the_sparse_arm():
    from ..rag.index import tokenize

    tokens = tokenize("CreateNewStudy3")
    assert "create" in tokens and "study" in tokens


def test_quirks_are_pinned_not_ranked():
    """A plan needs the COMPLETE quirk list. Ranking it against a
    thermal query drops the VARIANT-null entry, and the script then
    reproduces the bug the list exists to prevent."""
    from ..rag import retriever
    from ..rag.index import Embedder, KnowledgeBase

    kb = KnowledgeBase.build(Embedder(prefer="hash"), reuse_cache=False)
    context = retriever.retrieve(kb, "how hot does it get", "thermal")
    quirks = [c for c in context.chunks if c.kind == "quirk"]
    assert len(quirks) == len([c for c in kb.chunks if c.kind == "quirk"])


def test_a_pinned_section_reaches_the_context_unranked():
    """`<!-- pin -->` is for facts that are useless when they are merely
    LIKELY to be retrieved. The Flow Simulation entry point does not
    rank against a question about pressure drop, and a model that does
    not have it in front of it invents a ProgID -- which is exactly what
    this corpus carried for weeks."""
    from ..rag import retriever
    from ..rag.index import Embedder, KnowledgeBase

    kb = KnowledgeBase.build(Embedder(prefer="hash"), reuse_cache=False)
    pinned = [c for c in kb.chunks if c.pinned]
    assert pinned, "nothing in the corpus is marked <!-- pin --> any more"

    context = retriever.retrieve(kb, "перепад давления в трубе",
                                 "flow_internal")
    for chunk in pinned:
        if not chunk.domains or "flow_internal" in chunk.domains:
            assert chunk in context.chunks, f"{chunk.title} was not pinned in"


# ==========================================================================
# End to end, with a hand-built plan standing in for the model
# ==========================================================================
def test_full_path_from_plan_to_validated_script():
    """What the planner hands back for 'clamp the root, 500 kN on the
    tip', taken through narrowing, emission and validation."""
    plan = Plan(domain="static", summary="Cantilever under a tip load", ops=[
        RawOp(op="set_geometry", options={"source": "active_document"}),
        RawOp(op="apply_material", options={"name": "Plain Carbon Steel"}),
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="role", role="fixed_end")),
        _force_op(),
        RawOp(op="set_mesh"),
        RawOp(op="solve"),
        RawOp(op="extract_results"),
    ])
    resolved, questions, gaps = planner.finalise(plan)
    assert not questions, questions
    assert not gaps, gaps
    out = emitter.emit(plan.domain, resolved, summary=plan.summary)
    assert validate(out.code, plan.domain).ok


# ==========================================================================
# The catalogue is wider than the emitter, on purpose
# ==========================================================================
def test_an_op_with_no_emitter_is_reported_not_skipped():
    """Recognising a centrifugal load and saying it is not wired up beats
    not understanding it. Silently dropping the step does not: the script
    would run, finish, and answer a different question."""
    ops = _narrowed([
        RawOp(op="add_centrifugal",
              quantities=[QSlot(name="angular_velocity", value=3000, unit="rpm")],
              options={"axis": "Axis1"}),
        RawOp(op="add_fixture", options={"type": "fixed"}),
        RawOp(op="solve"),
    ], "static")
    assert emitter.unsupported(ops) == ["add_centrifugal"]

    out = emitter.emit("static", ops)
    # It must be a raise, never a comment: a commented-out step is a
    # script that runs to completion having skipped what was asked for.
    assert "raise RuntimeError" in out.code
    assert "add_centrifugal" in out.code
    assert not any(line.strip().startswith("# (no emitter")
                   for line in out.code.splitlines())


def test_the_planner_is_only_offered_operations_that_can_be_emitted():
    from ..core.ops import capability_digest

    offered = capability_digest("static", emitter.supported_ops())
    assert "add_force" in offered
    assert "add_torque" not in offered      # catalogued, not yet emittable


def test_build_prompt_is_callable_with_and_without_the_filter():
    """Regression: `emittable` was referenced in the body before it was a
    parameter, which imports fine and fails at call time."""
    from ..rag import retriever
    from ..rag.index import Embedder, KnowledgeBase

    kb = KnowledgeBase.build(Embedder(prefer="hash"), reuse_cache=False)
    ctx = retriever.retrieve(kb, "clamp the end and load the tip", "static")
    assert planner.build_prompt("clamp it", "static", ctx)
    filtered = planner.build_prompt("clamp it", "static", ctx,
                                    emittable=emitter.supported_ops())
    assert "add_force" in filtered and "add_torque" not in filtered


# ==========================================================================
# Real parts: more shapes, holes, and the live selection
# ==========================================================================
def test_every_catalogued_primitive_has_a_builder():
    """The catalogue offered i_beam, box and cylinder with no builder
    behind them: the plan was accepted, the script written, and it
    stopped in SolidWorks. The two lists are held together here."""
    from .. import config
    from ..core.ops import OP_SPECS

    from ..core.ops import PRIMITIVE_DIMENSIONS

    runtime = (config.TEMPLATES_DIR / "runtime.py").read_text(encoding="utf-8")
    offered = OP_SPECS["set_geometry"].options["primitive"].choices
    assert set(offered) == set(emitter.PRIMITIVES) == set(PRIMITIVE_DIMENSIONS)
    for shape, (fn, args) in emitter.PRIMITIVES.items():
        assert f"def {fn}(model, " in runtime, f"{shape}: no {fn} in runtime"
        for name, _ in args:
            assert name in OP_SPECS["set_geometry"].slots, (shape, name)
        assert set(PRIMITIVE_DIMENSIONS[shape]) <= {n for n, _ in args}, shape


def test_a_missing_dimension_is_asked_for_not_defaulted():
    """The emitter used to build a 50 x 100 mm section for a beam whose
    section nobody gave."""
    op, questions = narrow(RawOp(
        op="set_geometry",
        options={"source": "primitive", "primitive": "rectangular_beam"},
        quantities=[QSlot(name="length", value=2, unit="m")]), "static")
    assert op is None
    asked = {q.field for q in questions}
    assert asked == {"set_geometry.width", "set_geometry.height"}


def test_a_new_primitive_is_built_from_its_own_dimensions():
    ops = _narrowed([
        RawOp(op="set_geometry",
              options={"source": "primitive", "primitive": "angle_bracket"},
              quantities=[QSlot(name="length", value=120, unit="mm"),
                          QSlot(name="height", value=100, unit="mm"),
                          QSlot(name="width", value=60, unit="mm"),
                          QSlot(name="thickness", value=10, unit="mm"),
                          QSlot(name="fillet_radius", value=8, unit="mm"),
                          QSlot(name="hole_diameter", value=11, unit="mm")]),
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="holes")),
        RawOp(op="solve"),
    ], "static")
    code = emitter.emit("static", ops).code
    assert "build_angle_bracket(model, 0.12, 0.1, 0.06, 0.01, 0.008, 0.011)" in code
    assert validate(code, "static").ok


def test_holes_reach_the_script_with_their_end_and_size():
    draft = planner.DraftPlan(domain="static", summary="s", reply="r", ops=[
        planner.DraftOp(op="add_fixture", target_kind="holes",
                        target_axis="X", target_side="min",
                        target_diameter_mm=10.3,
                        options=[planner.KV(key="type", value="fixed")])])
    op = planner.draft_to_plan(draft).ops[0]
    assert (op.target.kind, op.target.axis, op.target.side) == ("holes", "x", "min")
    assert op.target.diameter_mm == pytest.approx(10.3)
    assert "10.3 mm holes nearest min x" in op.target.describe()

    code = emitter.emit("static", _narrowed([
        RawOp(op="add_fixture", options={"type": "fixed"}, target=op.target),
        RawOp(op="solve")], "static")).code
    assert '"kind": "holes"' in code and '"diameter_mm": 10.3' in code


def test_half_an_end_on_holes_is_dropped_not_guessed():
    """An axis with no side could be either end. Every hole is wider than
    asked, never the wrong end."""
    draft = planner.DraftPlan(domain="static", summary="s", reply="r", ops=[
        planner.DraftOp(op="add_fixture", target_kind="holes",
                        target_axis="x", target_side="")])
    target = planner.draft_to_plan(draft).ops[0].target
    assert target.kind == "holes" and target.axis is None and target.side is None


def test_a_restraint_and_a_load_on_the_same_selection_is_a_question():
    """SolidWorks holds one selection. A fixture and a force both on
    'this face' land on the same faces, and the load goes straight into
    the support -- the study solves and reports almost nothing."""
    plan = Plan(domain="static", summary="s", ops=[
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="active_selection")),
        _force_op(),
        RawOp(op="set_mesh"), RawOp(op="solve"), RawOp(op="extract_results"),
    ])
    _, questions, _ = planner.finalise(plan)
    assert any(q.field == "target" and "this face" in q.question
               for q in questions)

    plan.ops[0].target = Selector(kind="holes")
    _, questions, _ = planner.finalise(plan)
    assert not any(q.field == "target" for q in questions)


def test_the_selection_is_read_before_anything_can_clear_it():
    """A directional force selects the Front plane, which clears what the
    user picked. The capture has to come before the study exists."""
    ops = _narrowed([
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="holes")),
        _force_op(direction="against_y"),
        RawOp(op="solve"),
    ], "static")
    code = emitter.emit("static", ops).code
    assert (code.index("    capture_selection(model)")
            < code.index("    cw = connect_simulation(app)"))

    no_live = emitter.emit("static", _narrowed([
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="holes")),
        RawOp(op="solve")], "static")).code
    assert "    capture_selection(model)" not in no_live


def _geometry(shape, **dims_mm):
    return RawOp(op="set_geometry",
                 options={"source": "primitive", "primitive": shape},
                 quantities=[QSlot(name=k, value=v, unit="mm")
                             for k, v in dims_mm.items()])


def test_plate_with_hole_is_checked_against_kt_not_beam_theory():
    ops = _narrowed([
        _geometry("plate_with_hole", width=100, length=300, thickness=10,
                  hole_diameter=25),
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="role", role="fixed_end")),
        RawOp(op="add_force", target=Selector(kind="role", role="free_end"),
              quantities=[QSlot(name="magnitude", value=10, unit="kN")],
              options={"direction": "along_z"}),
    ], "static")
    net = 10_000 / (0.075 * 0.01)
    kt = physics.hole_kt_net(0.025, 0.1)
    assert kt == pytest.approx(2.4219, rel=1e-3)

    rep = physics.crosscheck("static", ops, {"von_mises_max": 0.95 * kt * net,
                                             "displacement_max": 1.5e-6})
    assert rep.compared
    assert rep.numbers["hole_peak_stress_Pa"] == pytest.approx(kt * net)
    assert "beam_theory_tip_deflection_m" not in rep.numbers
    assert any("agrees with Kt" in f.message for f in rep.findings)


def test_a_bracket_is_not_compared_with_a_cantilever():
    """It has a width and a height, which is all the old check looked at."""
    ops = _narrowed([
        _geometry("angle_bracket", length=120, height=100, width=60,
                  thickness=10),
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="holes")),
        _force_op(value=1, direction="against_x"),
    ], "static")
    rep = physics.crosscheck("static", ops, {"displacement_max": 1e-4,
                                             "von_mises_max": 5e7})
    assert not rep.compared and not rep.numbers
    assert not physics.preflight("static", ops).numbers.get("slenderness")


def _cantilever(geometry_op, newtons, direction, domain="static"):
    return _narrowed([
        geometry_op,
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="role", role="fixed_end")),
        RawOp(op="add_force", target=Selector(kind="role", role="free_end"),
              quantities=[QSlot(name="magnitude", value=newtons, unit="N")],
              options={"direction": direction}),
    ], domain)


STEEL_PROPS = {"material_props": {"E": 2.1e11, "density": 7800.0,
                                  "yield": 2.20594e8}}


def test_an_i_beam_is_checked_with_its_own_section():
    ops = _cantilever(_geometry("i_beam", flange_width=100, height=200,
                                web_thickness=8, flange_thickness=12,
                                length=2000), 10_000, "against_y")
    rep = physics.crosscheck("static", ops, {"displacement_max": 5.29e-3},
                             STEEL_PROPS)
    I = (0.1 * 0.2 ** 3 - 0.092 * 0.176 ** 3) / 12
    assert rep.numbers["beam_theory_tip_deflection_m"] == pytest.approx(
        10_000 * 2.0 ** 3 / (3 * 2.1e11 * I))
    assert rep.compared


def test_bending_is_checked_about_the_axis_the_load_acts_across():
    """50 wide, 100 deep: a load along X bends it about its weak axis,
    four times as far as the same load along Y."""
    beam = _geometry("rectangular_beam", width=50, height=100, length=2000)
    along_y = physics.crosscheck("static", _cantilever(beam, 5000, "against_y"),
                                 {}, STEEL_PROPS).numbers
    along_x = physics.crosscheck("static", _cantilever(beam, 5000, "along_x"),
                                 {}, STEEL_PROPS).numbers
    assert along_y["beam_theory_tip_deflection_m"] == pytest.approx(0.015238, rel=1e-3)
    assert along_x["beam_theory_tip_deflection_m"] == pytest.approx(
        4 * along_y["beam_theory_tip_deflection_m"])


def test_the_library_material_replaces_the_assumed_steel():
    """The hand check assumed 205 GPa; Plain Carbon Steel is 210. That
    2% was written up as mesh stiffness for months."""
    beam = _geometry("rectangular_beam", width=50, height=100, length=2000)
    rep = physics.crosscheck("static", _cantilever(beam, 5000, "against_y"),
                             {"displacement_max": 0.015243}, STEEL_PROPS)
    assert not any("assumes" in f.message for f in rep.findings)
    assert any("agrees" in f.message and f.level == physics.INFO
               for f in rep.findings)


def test_reactions_that_do_not_balance_the_load_are_reported():
    beam = _geometry("rectangular_beam", width=50, height=100, length=2000)
    ops = _cantilever(beam, 5000, "against_y")
    receipt = {"loads": {"forces": [{"total_N": 5000.0,
                                     "vector_N": [0.0, -5000.0, 0.0]}]}}
    ok = physics.crosscheck("static", ops,
                            {"reaction_force_N": [0.0, 4999.99, 0.0]}, receipt)
    assert any("carry the whole load" in f.message for f in ok.findings)
    # Spread per face when it was meant in total: twice the load arrives.
    bad = physics.crosscheck("static", ops,
                             {"reaction_force_N": [0.0, 10_000.0, 0.0]}, receipt)
    assert any(f.level == physics.WARN and "do not balance" in f.message
               for f in bad.findings)


def test_yielding_is_a_design_finding_not_a_failed_check():
    """A part that yields is the simulation doing its job; it must not
    keep a correct run out of the verified library."""
    rep = physics.crosscheck("static", [], {"factor_of_safety": 0.6,
                                            "factor_of_safety_away_from_supports": 0.8},
                             STEEL_PROPS)
    finding = next(f for f in rep.findings if "yields" in f.message)
    assert finding.level == physics.WARN and finding.kind == "design"
    assert "0.8" in finding.message


def test_frequency_buckling_and_heat_are_checked_against_closed_forms():
    beam = _geometry("rectangular_beam", width=50, height=100, length=2000)
    freq_ops = _narrowed([beam, RawOp(op="add_fixture", options={"type": "fixed"},
                                      target=Selector(kind="role", role="fixed_end"))],
                         "frequency")
    freq = physics.crosscheck("frequency", freq_ops, {"first_frequency": 10.50},
                              STEEL_PROPS)
    assert freq.compared and freq.numbers["beam_theory_first_frequency_Hz"] == \
        pytest.approx(10.49, rel=0.01)

    buck = physics.crosscheck("buckling", _cantilever(beam, 10_000, "against_z",
                                                      "buckling"),
                              {"buckling_load_factor": 13.52}, STEEL_PROPS)
    assert buck.compared and buck.numbers["euler_load_factor"] == \
        pytest.approx(13.49, rel=0.01)

    heat = physics.crosscheck("thermal", [], {"temperature_min": 300.74,
                                              "temperature_max": 300.94},
                              {"loads": {"heat_in_W": 20.0, "convection": [
                                  {"h": 12.0, "bulk_K": 298.15, "area_m2": 0.61}]}})
    assert heat.compared
    assert not [f for f in heat.findings if f.level == physics.WARN]


def test_mesh_quality_follows_the_type_library():
    """swsMeshQuality_e is draft 0, high 1; this emitted them swapped, so
    every 'draft' run was second-order."""
    high = emitter.emit("static", _narrowed([
        RawOp(op="set_mesh"), RawOp(op="add_fixture", options={"type": "fixed"},
                                    target=Selector(kind="holes"))], "static")).code
    draft = emitter.emit("static", _narrowed([
        RawOp(op="set_mesh", options={"quality": "draft"}),
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="holes"))], "static")).code
    assert "create_mesh(study, 50.0, 2.5, MESH_HIGH)" in high
    assert "create_mesh(study, 50.0, 2.5, MESH_DRAFT)" in draft
    assert "MESH_DRAFT = 0" in high and "MESH_HIGH = 1" in high


def test_a_flow_script_prints_the_checklist_then_runs_the_project():
    """The checklist has to be out before the add-in is touched, so a
    machine without Flow still gets it; the solve and the goals only
    happen when the part has a project."""
    ops = _narrowed([
        RawOp(op="set_flow_domain",
              options={"analysis_type": "internal", "fluid": "Water"},
              quantities=[QSlot(name="ambient_temperature", value=20, unit="degC")]),
        RawOp(op="add_flow_bc", options={"type": "inlet_velocity"},
              target=Selector(kind="role", role="inlet"),
              quantities=[QSlot(name="velocity", value=1, unit="m/s")]),
        RawOp(op="add_goal", options={"quantity": "pressure_drop"}),
        RawOp(op="solve"), RawOp(op="extract_results")], "flow_internal")
    code = emitter.emit("flow_internal", ops).code
    main = code[code.index("def main():"):]
    assert (main.index("print_flow_setup(RECEIPT)")
            < main.index("project = flow_project(app, model")
            < main.index("solve_flow(project)")
            < main.index("read_flow_goals(project)"))
    assert "connect_simulation" not in main
    assert validate(code, "flow_internal").ok


def test_a_pressure_drop_becomes_a_loss_coefficient():
    ops = _narrowed([
        RawOp(op="set_flow_domain",
              options={"analysis_type": "internal", "fluid": "Water"},
              quantities=[QSlot(name="ambient_temperature", value=20, unit="degC"),
                          QSlot(name="velocity", value=1, unit="m/s")])],
        "flow_internal")
    rep = physics.crosscheck("flow_internal", ops, {"pressure_drop_Pa": 16271.5})
    assert rep.numbers["loss_coefficient"] == pytest.approx(16271.5 / (0.5 * 998.2),
                                                            rel=1e-3)


def test_a_previous_version_run_becomes_a_plan_this_one_can_run():
    from ..memory.library import legacy_example

    request = {
        "id": "0005", "status": "verified",
        "description": "100x200x3000 mm I-beam, 9000 N distributed",
        "form_data": {
            "geometry": {"shape": "i_beam", "flange_width_mm": 100,
                         "height_mm": 200, "web_thickness_mm": 8,
                         "flange_thickness_mm": 12, "length_mm": 3000},
            "material": {"name": "Plain Carbon Steel"},
            "support": {"type": "fixed", "location": "start"},
            "load": {"type": "distributed_downward", "magnitude_N": 9000}},
        "results": {"max_von_mises_MPa": 61.39, "max_displacement_mm": 5.917},
        "sanity": {"verdict": "plausible",
                   "ratios": {"stress": 1.13, "deflection": 1.017}}}
    ex = legacy_example(request)
    assert ex.status == "verified"
    assert ex.results["displacement_max"] == pytest.approx(0.005917)
    plan = Plan(**ex.plan)
    resolved, questions, gaps = planner.finalise(plan)
    assert not questions and not gaps
    assert validate(emitter.emit("static", resolved).code, "static").ok

    # A point load part-way along has no face here; it is left out, not
    # filed as something it was not.
    request["form_data"]["load"] = {"type": "point_force", "magnitude_N": 5000,
                                    "location_mm_from_start": 1250}
    assert legacy_example(request) is None


def test_a_convergence_check_goes_between_the_results_and_the_receipt():
    ops = _narrowed([
        RawOp(op="set_mesh", options={"convergence": "check"}),
        RawOp(op="add_fixture", options={"type": "fixed"},
              target=Selector(kind="holes")),
        RawOp(op="solve"), RawOp(op="extract_results")], "static")
    code = emitter.emit("static", ops).code
    assert (code.index('    read_results(study, "static")')
            < code.index('    check_convergence(study, "static")')
            < code.index('    save_plots(study, "static")')
            < code.rindex("    write_receipt()"))
    assert validate(code, "static").ok


if __name__ == "__main__":
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
