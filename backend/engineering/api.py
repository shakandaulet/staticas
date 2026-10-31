"""
The one call a web layer needs: a message in, a plan and a script out.

    from engineering import api
    result = api.generate("clamp the left end and hang 5 kN off the tip")

WHY THIS EXISTS
---------------
cli.py joins the modules up for a terminal. A server that wanted the
same thing had to repeat that sequence -- retrieve, plan, narrow,
preflight, emit, validate -- and the step it was most likely to skip is
the one that matters: a plan with an open question does not become a
script. Here the sequence lives once, and a question comes back as data
for the page to show, instead of as a terminal prompt.

A follow-up ("now make it 8 kN", or the answer to a question) is the
next call with the same session_id. The session carries the plan, so
the planner edits it rather than starting over -- exactly as
`cli.py --session` does.
"""

from __future__ import annotations

import threading

from . import config
from .codegen import emitter
from .codegen.validator import validate
from .core import domains as D
from .core import physics, planner
from .memory import session as sessions
from .rag import retriever
from .rag.index import Embedder, KnowledgeBase

# What the image reader is asked. The answer is folded into the message,
# so the planner sees a photo of a textbook problem as words and numbers
# with their units -- the same input it already knows how to plan.
IMAGE_ASK = (
    "This is a photo or drawing of an engineering problem. Write out, in "
    "the language of any text in the image (or English), everything a "
    "simulation would need: the geometry and its dimensions, the material, "
    "the supports, every load with its magnitude, unit and direction, any "
    "temperatures, flows or speeds, and what is being asked. Keep every "
    "number with the unit printed next to it. Do not solve the problem."
)

_lock = threading.Lock()
_gem: planner.Gemini | None = None
_kb: KnowledgeBase | None = None


def engine() -> tuple[planner.Gemini, KnowledgeBase]:
    """The Gemini client and the knowledge base, built once per process.

    Loading the index is the slow part of a first request, so it happens
    on the first call and is kept. The lock stops two first requests
    arriving together from both building it."""
    global _gem, _kb
    with _lock:
        if _gem is None:
            if not config.API_KEY:
                raise RuntimeError(config.NO_KEY_HELP)
            gem = planner.Gemini()
            if not gem.available:
                raise RuntimeError(f"Gemini is not available: {gem.status}")
            _gem = gem
        if _kb is None:
            _kb = KnowledgeBase.load(Embedder())
        return _gem, _kb


def domains() -> list[dict]:
    return D.describe_all()


def _plan_once(gem, kb, message, domain, sess):
    context = retriever.retrieve(kb, message, domain)
    prompt = planner.build_prompt(
        message, domain, context, history=sess.recent(),
        current_plan=sess.plan, emittable=emitter.supported_ops())
    draft = gem.structured(planner.SYSTEM, prompt, planner.DraftPlan)
    return planner.draft_to_plan(draft), draft.reply, context


def generate(message: str, session_id: str | None = None,
             image: bytes | None = None, image_mime: str = "",
             domain: str | None = None, force: bool = False) -> dict:
    """Plan one message and, when the plan is complete, emit its script.

    Never raises for a planning problem: a missing number, a blocked
    regime or an operation the emitter cannot write all come back with
    `ready: False` and say why. It raises only when Gemini itself cannot
    be reached, which the caller reports as a server error."""
    gem, kb = engine()

    message = (message or "").strip()
    if image:
        seen = gem.read_image(image, image_mime or "image/png", IMAGE_ASK)
        message = f"{message}\n\nFrom the attached image:\n{seen}".strip()
    if not message:
        raise ValueError("Describe the problem or attach a photo of it.")

    sess = sessions.load(session_id)
    sess.add("user", message)
    guess, _ = D.classify(message, default=sess.domain or "static")
    if domain not in D.DOMAINS:
        domain = guess

    plan, reply, context = _plan_once(gem, kb, message, domain, sess)
    resolved, questions, gaps = planner.finalise(plan)

    # Live runs showed the model sometimes stopping after one step, or
    # leaving "100×100×5 мм" in the note with no quantities. Neither is a
    # question for the user -- they said it -- so the model is shown its
    # own plan with what the check found, once, and asked to complete it.
    lost = [q.question for q in questions if q.field.startswith("set_geometry.")]
    if gaps or lost:
        found = "; ".join(gaps + lost)
        kept, sess.plan = sess.plan, plan.model_dump()
        try:
            plan2, reply2, _ = _plan_once(
                gem, kb,
                f"{message}\n\n[Check of the plan on screen: {found}. Every "
                f"number from the message must be in quantities, not only "
                f"in the note. Return the COMPLETE plan.]",
                plan.domain, sess)
            resolved2, questions2, gaps2 = planner.finalise(plan2)
            if len(gaps2) + len(questions2) < len(gaps) + len(questions):
                plan, reply = plan2, reply2
                resolved, questions, gaps = resolved2, questions2, gaps2
        except Exception:
            pass                    # the first plan still stands
        sess.plan = kept

    pre = physics.preflight(plan.domain, resolved)

    sess.domain = plan.domain
    sess.plan = plan.model_dump()
    sess.resolved = [op.as_dict() for op in resolved]
    sess.add("assistant", reply)

    assumptions = list(plan.assumptions)
    for op in resolved:
        assumptions.extend(op.assumptions)

    spec = D.DOMAINS[plan.domain]
    out = {
        "session_id": sess.id,
        "reply": reply,
        "domain": plan.domain,
        "domain_label": spec.label,
        "engine": spec.engine,
        "summary": plan.summary,
        "study_name": plan.study_name,
        "steps": [{**op.as_dict(), "needs_target": op.spec.needs_target}
                  for op in emitter.order_ops(resolved)],
        "assumptions": list(dict.fromkeys(assumptions)),
        "warnings": list(plan.warnings),
        "preflight": pre.as_dict(),
        "gaps": gaps,
        "questions": [q.model_dump() for q in questions] + [
            {"field": "", "question": f.message, "why": f.detail,
             "options": [], "suggested": ""} for f in pre.blocking],
        "citations": context.citations(),
        "ready": False,
        "blocked_by": "",
        "code": "",
        "filename": "",
        "unverified_ops": [],
        "validator_warnings": [],
    }

    missing = emitter.unsupported(resolved)
    if missing:
        out["blocked_by"] = (
            f"This plan uses {', '.join(missing)}, which the emitter cannot "
            f"write yet. Describe the same thing another way -- a torque as "
            f"a force couple, a rotating region as a fixed inlet velocity.")
    elif (questions or gaps or pre.blocking) and not force:
        out["blocked_by"] = (
            "The plan is not complete. A script now would run and answer a "
            "different question -- answer the questions above first.")
    else:
        emitted = emitter.emit(
            plan.domain, resolved, study_name=plan.study_name,
            summary=plan.summary, assumptions=plan.assumptions,
            warnings=plan.warnings + [f.message for f in pre.findings
                                      if f.level != physics.INFO])
        verdict = validate(emitted.code, plan.domain)
        if not verdict.ok:
            out["blocked_by"] = ("The emitted script did not pass validation. "
                                 "That is a defect in the emitter, not in "
                                 "your request.\n" + verdict.report())
        else:
            out.update(ready=True, code=emitted.code,
                       filename=f"{plan.study_name or 'simulation'}.py",
                       unverified_ops=list(emitted.unverified_ops),
                       validator_warnings=list(verdict.warnings))
            sess.last_code = emitted.code

    sess.save()
    return out
