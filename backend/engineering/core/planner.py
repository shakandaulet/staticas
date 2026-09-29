"""
The Gemini half: turning what a person said into a typed plan.

WHERE THE MODEL IS AND IS NOT USED
----------------------------------
Used for: reading intent out of free text in either language, deciding
which physics domain a question belongs to, knowing that "clamp the
left end and hang 500 kN off the tip" is a fixture plus a force,
filling slots the user left implicit, and explaining results
afterwards. All of these are judgement, and all of them are things it
is genuinely good at.

Not used for: arithmetic (units.py), sequencing (emitter.order_ops),
COM syntax (templates/runtime.py), safety (validator.py) or physics
regime checks (physics.py). Every one of those has a right answer that
can be computed, and computing it is strictly better than asking.

WHY THE SCHEMA THE MODEL SEES IS FLATTER THAN THE ONE WE USE
------------------------------------------------------------
Structured output degrades sharply with nesting depth and with
free-form maps. `options: dict[str, str]` becomes an object with
unconstrained additional properties, which the schema subset handles
badly; `target: Selector | None` becomes a nullable nested object,
which the model fills with a half-populated stub more often than with
null. So the draft schema below is flat -- `target_kind`, `target_name`,
a list of key/value pairs -- and it is converted into the real types
here, where a bad value becomes a question instead of a parse failure.

THERE IS NO OFFLINE MODE
------------------------
Every plan comes from the model. The server refuses to start without a
key, and a failed planning call is an error in the panel rather than a
keyword-built skeleton: a plan the model did not make is a form the
user has no way to finish.
"""

from __future__ import annotations

import json
import re
import time
from typing import Literal

from pydantic import BaseModel, Field

from .. import config
from ..rag.retriever import RetrievedContext
from . import domains as D
from .ops import ResolvedOp, capability_digest, narrow
from .schema import (Domain, EntityType, OpKind, Plan, Question, QSlot, RawOp,
                     Selector, SelectorKind)

# --------------------------------------------------------------------------
# The flattened schema the model fills in
# --------------------------------------------------------------------------
class KV(BaseModel):
    key: str
    value: str


class DraftOp(BaseModel):
    op: OpKind = Field(description="Which operation from the catalogue.")
    note: str = Field(
        default="",
        description="One short line of plain English for the plan card, "
                    "in the language the user wrote in.")
    target_kind: SelectorKind = Field(
        default="active_selection",
        description="How to find the geometry. Prefer 'active_selection' "
                    "whenever the user points at something ('this face', "
                    "'этот торец') -- it means whatever they have "
                    "highlighted in SolidWorks, which is what they meant.")
    target_entity: EntityType = "FACE"
    target_name: str = Field(default="", description="For target_kind='named'.")
    target_point: list[float] = Field(
        default_factory=list,
        description="[x, y, z] in metres, for target_kind='point'.")
    target_axis: str = Field(
        default="",
        description="x, y or z: required for 'extreme'; for 'holes', "
                    "optional, naming the end the holes are nearest.")
    target_side: str = Field(default="", description="min or max, with target_axis.")
    target_role: str = Field(
        default="",
        description="fixed_end, free_end, top, bottom, front, back, side, "
                    "inlet, outlet, wall, whole_body.")
    target_diameter_mm: float = Field(
        default=0.0,
        description="For 'holes': only holes of this diameter in mm, when "
                    "the user named a size. 0 = any size.")
    target_index: int = Field(
        default=0,
        description="0 = outermost. For 'extreme', 1 = the next flat level "
                    "in (a shoulder, the top of a base under an upright); "
                    "for 'holes' with an axis, 1 = the next row of holes in.")
    target_said: str = Field(
        default="",
        description="The user's own words for this target, verbatim.")
    quantities: list[QSlot] = Field(
        default_factory=list,
        description="Every number this step needs, each with the UNIT AS "
                    "THE USER WROTE IT. Never convert -- the server "
                    "converts and the conversion is tested.")
    options: list[KV] = Field(
        default_factory=list,
        description="Non-numeric choices for this step, as key/value pairs.")


class DraftPlan(BaseModel):
    domain: Domain = Field(
        description="Which kind of study answers the question asked.")
    study_name: str = Field(default="AI_Study",
                            description="Short, no spaces.")
    summary: str = Field(
        description="One sentence naming what this simulation answers, in "
                    "the user's language.")
    reply: str = Field(
        description="What to say to the user: 2-5 sentences, their "
                    "language. State what will be set up and flag anything "
                    "assumed. Do not list the steps -- the panel renders "
                    "them from the plan itself.")
    ops: list[DraftOp] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list,
        description="Anything filled in that the user did not say.")
    clarifications: list[str] = Field(
        default_factory=list,
        description="Questions worth asking, only where a wrong guess "
                    "would change the answer materially. Do not ask about "
                    "something with an obvious default.")


# --------------------------------------------------------------------------
# The Gemini client
# --------------------------------------------------------------------------
class Gemini:
    """A thin wrapper. Retrieval, prompt assembly and validation live
    elsewhere; this only talks to the API and translates its failures."""

    def __init__(self):
        self.client = None
        self.model = config.PLANNER_MODEL
        self.status = "no key"
        if not config.API_KEY:
            return
        try:
            from google import genai
            self.client = genai.Client(api_key=config.API_KEY)
            self.status = "ready"
        except Exception as e:
            self.status = f"client failed: {e}"

    @property
    def available(self) -> bool:
        return self.client is not None

    def probe(self) -> str:
        """Prove the model answers, at startup rather than on a first click.

        Membership in models.list() is not the same thing and gives a
        confident false OK: a model can appear in the catalogue and then
        return 404 on the first real request. Only a generation proves
        it."""
        if not self.available:
            return self.status
        try:
            self.client.models.generate_content(
                model=self.model, contents=["Reply with: OK"])
            return "ok"
        except Exception as e:
            text = str(e)
            if any(m in text for m in config.LOCATION_MARKERS):
                return "location-blocked"
            for candidate in config.FALLBACK_MODELS:
                if candidate == self.model:
                    continue
                try:
                    self.client.models.generate_content(
                        model=candidate, contents=["Reply with: OK"])
                    self.model = candidate
                    return f"ok (fell back to {candidate})"
                except Exception:
                    continue
            return f"unusable: {text.splitlines()[0][:160]}"

    def _call(self, contents, cfg):
        """One request, with backoff for the failures that are not ours.

        A whole class clicking Send at the same moment reliably produces
        503 'high demand'. That is not a bad request and should not look
        like one to the person who clicked second.

        Two kinds of busy, handled differently. A per-minute quota (the
        free tier allows 5 planning calls a minute) says how long to
        wait, and waiting that long works; retrying after 2 s just spends
        the next attempt. A model that stays overloaded, or whose daily
        quota is gone, is not coming back soon -- the next model in
        FALLBACK_MODELS has its own capacity and its own quota, so the
        request moves there and the session stays on it."""
        last = None
        models = [self.model] + [m for m in config.FALLBACK_MODELS
                                 if m != self.model]
        for model in models:
            for attempt in range(1, config.API_RETRIES + 1):
                try:
                    response = self.client.models.generate_content(
                        model=model, contents=contents, config=cfg)
                    if model != self.model:
                        print(f"  [gemini] {self.model} was unavailable; "
                              f"continuing with {model}.", flush=True)
                        self.model = model
                    return response
                except Exception as e:
                    last = e
                    text = str(e)
                    if any(m in text for m in config.LOCATION_MARKERS):
                        raise RuntimeError(config.LOCATION_HELP) from e
                    if "404" in text or "NOT_FOUND" in text:
                        break               # this model name does not exist
                    if not any(m in text for m in config.TRANSIENT_MARKERS):
                        raise
                    if "PerDay" in text:
                        break               # today's quota: move on
                    wait = _requested_wait(text)
                    if attempt == config.API_RETRIES:
                        break
                    if wait is not None:
                        print(f"  [gemini] per-minute quota on {model}; "
                              f"waiting {wait:.0f} s as the API asks.",
                              flush=True)
                        time.sleep(wait)
                    else:
                        time.sleep(2 ** attempt)
        raise RuntimeError(
            f"Gemini was unavailable on every model tried "
            f"({', '.join(models)}). This is usually temporary demand or a "
            f"used-up free-tier quota rather than a problem with the "
            f"request. Last error: {str(last)[:300]}")

    def structured(self, system: str, prompt: str, schema) -> BaseModel:
        from google.genai import types
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=0.1,   # planning, not prose
            # No tools are passed; with AFC left on the SDK warns on every
            # call, and the warning lands in the middle of the plan output.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(
                disable=True),
        )
        response = self._call([prompt], cfg)
        parsed = getattr(response, "parsed", None)
        if parsed is not None:
            return parsed
        # Some SDK versions return text only. Parse it rather than
        # failing, because the content is there and the user does not
        # care which field it arrived in.
        return schema.model_validate_json((response.text or "").strip())

    def prose(self, system: str, prompt: str, temperature: float = 0.4) -> str:
        from google.genai import types
        cfg = types.GenerateContentConfig(system_instruction=system,
                                          temperature=temperature)
        return (self._call([prompt], cfg).text or "").strip()

    def read_image(self, data: bytes, mime: str, ask: str) -> str:
        from google.genai import types
        cfg = types.GenerateContentConfig(temperature=0.0)
        response = self._call(
            [ask, types.Part.from_bytes(data=data, mime_type=mime)], cfg)
        return (response.text or "").strip()


def _requested_wait(text: str) -> float | None:
    """How long a rate-limited reply asks the caller to wait, capped."""
    m = re.search(r"retry in ([\d.]+)s", text) or \
        re.search(r"retryDelay'?:\s*'(\d+)s", text)
    return min(float(m.group(1)) + 1.0, 65.0) if m else None


# --------------------------------------------------------------------------
# Prompt assembly
# --------------------------------------------------------------------------
SYSTEM = """\
You are the planning half of an engineering assistant that drives \
SolidWorks simulations: statics, thermodynamics, fluid mechanics, \
aerodynamics, modal and buckling analysis.

You do NOT write code. You produce a typed plan. A deterministic \
emitter turns your plan into a script, so the API details are not your \
problem and inventing them is not helpful.

Rules, in order of importance:

1. NEVER convert units. Report every number with the unit the user \
wrote -- 500 kN stays {value: 500, unit: "kN"}. The server converts, \
and its conversion is tested. Yours is not.

2. Use ONLY the operations in the catalogue below, with only the slot \
names and option values it lists. An operation that is not there cannot \
be emitted, and promising it produces nothing.

3. When the user points at geometry -- "this face", "этот торец", "the \
edge I selected" -- use target_kind="active_selection". That means \
whatever they have highlighted in SolidWorks, which is exactly what \
they meant. Do not invent coordinates for it, and do not ask them to \
describe it in words. Only ONE thing can be selected at a time: never \
put a restraint and a load both on active_selection.

4. When the user NAMES geometry instead of pointing at it, the part's \
own shape answers, and only flat faces or round holes qualify:
   - "the bolt holes", "по отверстиям", "the hole at the far end" -> \
target_kind="holes"; add target_axis/target_side for one end, \
target_diameter_mm for a stated size.
   - "the free end" / "the clamped end" -> role free_end / fixed_end: the \
flat end faces of the part's LONGEST axis (fixed = min, free = max).
   - "top, bottom, front, back, side" -> the flat faces facing +Y, -Y, \
+Z, -Z, +X, as the SolidWorks views show them.
   - "the face at max X" -> extreme; target_index=1 is the next flat \
level in (a shoulder, the top of a base below an upright).
   A rounded end (an eye, a lug, a hook) has no flat face: use the holes \
there, or active_selection.

5. Pick the domain that answers the QUESTION, not the one whose words \
appear in the message. "Why does the bracket get hot" is thermal even \
if they said load; "will it survive the heat" may be a thermal study \
followed by a static one, and if so, say that in the reply and plan the \
first.

6. Fill in what is standard and say so in assumptions. Ask only where a \
wrong guess changes the answer materially: a missing film coefficient \
changes everything, a missing study name changes nothing.

7. Respond in the user's language. Technical terms may stay in English.

8. Every plan ends with solve and extract_results. Order does not \
matter -- the emitter sequences it.
"""


def build_prompt(message: str, domain: str, context: RetrievedContext,
                 history: list[dict] | None = None,
                 current_plan: dict | None = None,
                 emittable: set[str] | None = None) -> str:
    """Assemble the planning prompt.

    `emittable` is the set of operations the emitter can currently
    write, passed in by the server. It is not imported here because it
    lives in codegen, which imports this module -- and because the
    catalogue being wider than the emitter is deliberate, so the
    narrowing belongs at the call site rather than in the table."""
    parts: list[str] = []

    # The WHOLE catalogue, not the keyword-guessed domain's slice of it.
    # Filtering here meant a message with no domain keywords in it --
    # "how hot does this get at 20 W" has none -- was planned against the
    # static operations alone, so the model could not set up the thermal
    # study even when it correctly asked for one.
    parts.append("=== OPERATION CATALOGUE (the only operations that exist) ===")
    parts.append("The studies each operation is legal in are in brackets.")
    parts.append(capability_digest(None, emittable))

    parts.append("\n=== STUDY TYPES ===")
    for d in D.DOMAINS.values():
        parts.append(f"- {d.id} ({d.label}): {d.what_it_answers} "
                     f"Needs: {', '.join(d.required_ops)}.")
        if d.notes:
            parts.append(f"    {d.notes}")

    spec = D.DOMAINS.get(domain)
    if spec:
        parts.append(f"\nKeyword matching suggests {domain} ({spec.label}). "
                     f"That is a guess from the words in the message, not a "
                     f"decision: pick the study that answers the question.")

    parts.append("\n=== RETRIEVED REFERENCE ===")
    parts.append(context.as_prompt_block())

    if current_plan:
        # A follow-up ("make it 800 N", "теперь с конвекцией") is an EDIT,
        # not a new problem. Without the current plan in front of it the
        # model rebuilds from scratch and silently drops every earlier
        # step the user did not repeat.
        parts.append("\n=== THE PLAN CURRENTLY ON SCREEN ===")
        parts.append(json.dumps(current_plan, indent=1, ensure_ascii=False))
        parts.append("The user's message is almost certainly a change to "
                     "this plan. Return the COMPLETE updated plan, not just "
                     "the changed step.")

    if history:
        parts.append("\n=== CONVERSATION SO FAR ===")
        for turn in history[-6:]:
            parts.append(f"{turn['role']}: {turn['text'][:600]}")

    parts.append("\n=== USER MESSAGE ===")
    parts.append(message)
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Draft -> Plan
# --------------------------------------------------------------------------
_ROLE_VALUES = {"inlet", "outlet", "wall", "fixed_end", "free_end", "top",
                "bottom", "side", "front", "back", "whole_body"}


def draft_to_plan(draft: DraftPlan) -> Plan:
    """Convert the flat draft into the real types, tolerantly.

    Tolerantly because a half-filled selector is a normal model output
    and should become a usable target rather than a validation error:
    a target_kind of 'point' with no point is an active selection, which
    is the safe reading."""
    ops: list[RawOp] = []
    for d in draft.ops:
        kind: SelectorKind = d.target_kind or "active_selection"
        if kind == "point" and len(d.target_point) != 3:
            kind = "active_selection"
        if kind == "named" and not d.target_name:
            kind = "active_selection"
        if kind == "extreme" and (d.target_axis.lower() not in ("x", "y", "z")
                                  or d.target_side.lower() not in ("min", "max")):
            kind = "active_selection"
        if kind == "role" and d.target_role not in _ROLE_VALUES:
            kind = "active_selection"

        # Holes take an end only as a pair; half of one is dropped rather
        # than guessed, which leaves "every hole" -- wider, never wrong-end.
        axis = d.target_axis.lower()
        side = d.target_side.lower()
        has_end = axis in ("x", "y", "z") and side in ("min", "max")
        selector = Selector(
            kind=kind,
            entity_type=d.target_entity or "FACE",
            name=d.target_name or None,
            point=d.target_point if kind == "point" else None,
            axis=axis if kind == "extreme" or (kind == "holes" and has_end)   # type: ignore[arg-type]
            else None,
            side=side if kind == "extreme" or (kind == "holes" and has_end)   # type: ignore[arg-type]
            else None,
            role=d.target_role if kind == "role" else None,              # type: ignore[arg-type]
            diameter_mm=(d.target_diameter_mm or None) if kind == "holes" else None,
            index=max(0, d.target_index) if kind in ("extreme", "holes") else 0,
            said=d.target_said,
        )
        ops.append(RawOp(
            op=d.op, target=selector,
            quantities=d.quantities,
            options={kv.key: kv.value for kv in d.options},
            note=d.note))

    return Plan(domain=draft.domain, study_name=draft.study_name or "AI_Study",
                summary=draft.summary, ops=ops,
                assumptions=list(draft.assumptions),
                questions=[Question(question=q) for q in draft.clarifications])


def finalise(plan: Plan) -> tuple[list[ResolvedOp], list[Question], list[str]]:
    """Narrow every operation, then check the plan adds up.

    Two levels because they catch different things. Narrowing checks
    each step is well formed; completeness checks the steps make a study
    that can run. A static plan with a load and no restraint is two
    perfectly valid operations and not a simulation."""
    resolved: list[ResolvedOp] = []
    questions: list[Question] = []

    for raw in plan.ops:
        op, qs = narrow(raw, plan.domain)
        if op is not None:
            resolved.append(op)
        questions.extend(qs)

    questions.extend(_shared_selection(resolved))
    gaps = D.completeness(plan.domain, [o.op for o in resolved])
    return resolved, questions, gaps


# A boundary condition that pins a face, and the ones it silences there.
_PINS = (({"add_fixture"}, {"add_force", "add_pressure"}),
         ({"add_temperature"}, {"add_convection", "add_heat_flux",
                                "add_heat_power", "add_radiation"}))


def _shared_selection(ops: list[ResolvedOp]) -> list[Question]:
    """A restraint and a load both on "this face".

    SolidWorks holds one selection, so two steps pointed at it land on
    the same faces -- and a force on a fixed face goes straight into the
    support, so the study solves and reports almost nothing. It is the
    one case where 'this face' cannot be right twice."""
    live = [o for o in ops if o.target and o.target.kind == "active_selection"]
    out: list[Question] = []
    for pins, silenced in _PINS:
        pin = next((o for o in live if o.op in pins), None)
        hit = next((o for o in live if o.op in silenced), None)
        if pin and hit:
            out.append(Question(
                field="target",
                question=(f"Both the {pin.spec.label.lower()} and the "
                          f"{hit.spec.label.lower()} point at 'this face'. "
                          f"Which one is on the selected face, and where "
                          f"does the other go -- the other end, the "
                          f"holes, the bottom?"),
                why=("SolidWorks holds one selection at a time, so both "
                     "would land on the same faces, and a load on a "
                     "restrained face goes straight into the support.")))
    return out


# --------------------------------------------------------------------------
# Prose passes
# --------------------------------------------------------------------------
ANSWER_SYSTEM = """\
You answer engineering questions about SolidWorks simulation: statics, \
thermal, fluid mechanics, aerodynamics, modal analysis.

Answer from the retrieved reference where it covers the question, and \
say plainly when it does not rather than filling the gap with something \
that sounds right. Be concrete: numbers, ranges and the reason behind \
them beat general advice. Reply in the user's language, in a few short \
paragraphs, with no preamble.
"""

RESULTS_SYSTEM = """\
You explain what a finished simulation means.

You are given the plan that was run, the numbers that came out, and \
independent closed-form cross-checks computed separately from the \
solver. Lead with the answer to the question that was asked. Then say \
whether the cross-check agrees, and if it does not, give the likely \
cause -- a wrong restraint, a load applied as a total rather than per \
entity, a mesh too coarse to resolve bending.

Do not oversell a coarse-mesh result. Say what it is good for and what \
it is not. Reply in the user's language.
"""


def answer_question(gem: Gemini, question: str,
                    context: RetrievedContext) -> str:
    prompt = (f"=== RETRIEVED REFERENCE ===\n{context.as_prompt_block(12000)}"
              f"\n\n=== QUESTION ===\n{question}")
    return gem.prose(ANSWER_SYSTEM, prompt)


def explain_results(gem: Gemini, plan_summary: str, ops: list[ResolvedOp],
                    results: dict, checks: dict, question: str = "") -> str:
    plan_text = "\n".join(
        f"  - {o.note or o.spec.label}: "
        f"{', '.join(f'{k}={v}' for k, v in o.q.items()) or 'no numbers'}"
        f"{' on ' + o.target.describe() if o.target else ''}"
        for o in ops)
    prompt = (
        f"=== WHAT WAS ASKED ===\n{question or plan_summary}\n\n"
        f"=== WHAT WAS SET UP ===\n{plan_text}\n\n"
        f"=== WHAT CAME OUT ===\n{json.dumps(results, indent=1)}\n\n"
        f"=== INDEPENDENT CROSS-CHECKS ===\n"
        f"{json.dumps(checks, indent=1, ensure_ascii=False)}")
    return gem.prose(RESULTS_SYSTEM, prompt)
