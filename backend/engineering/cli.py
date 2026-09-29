"""
The command line the assistant is driven from.

    python -m engineering.cli "clamp the left end and hang 5 kN off the tip"

WHY A COMMAND LINE AND NOT A SERVER
-----------------------------------
The web layer of this project -- the FastAPI server and the chat panel
-- belongs to other people on the team, so it is not in here. What is in
here is the part that decides what to simulate and writes the script:
the typed plan, the operation catalogue, retrieval, the emitter, the
validator and the runner. This file is the thin shell that joins them
up, and it is deliberately thin: everything it does is a call into a
module that can be tested without it.

WHAT IT DOES WITH A QUESTION IT CANNOT ANSWER
---------------------------------------------
The same thing the panel did. A plan with a missing film coefficient
does not become a script -- it becomes a question, and on a terminal
that can answer, the question is asked and the plan rebuilt with the
answer in it. Guessing would produce a study that runs, finishes and
answers something else.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config
from .codegen import emitter, runner
from .codegen.validator import validate
from .core import domains as D
from .core import physics, planner
from .core.schema import Question
from .memory import library, session as sessions
from .rag import retriever
from .rag.index import Embedder, KnowledgeBase

MAX_QUESTION_ROUNDS = 3


# --------------------------------------------------------------------------
# Printing
# --------------------------------------------------------------------------
def _rule(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def _print_plan(plan, resolved, questions, gaps, pre) -> None:
    spec = D.DOMAINS[plan.domain]
    _rule(f"Plan: {spec.label}")
    if plan.summary:
        print(plan.summary)
    for i, op in enumerate(resolved, 1):
        line = f"  {i}. {op.note or op.spec.label}"
        if op.spec.confidence == "unverified":
            line += "   [API UNVERIFIED]"
        print(line)
        for name, q in op.q.items():
            original = f" (you said {q.original})" if q.original and \
                q.original != "default" else ""
            print(f"       {name.replace('_', ' ')} = {q.value:g} "
                  f"{q.unit}{original}")
        for name, value in op.options.items():
            print(f"       {name} = {value}")
        if op.target:
            print(f"       -> {op.target.describe()}")

    assumptions = list(plan.assumptions)
    for op in resolved:
        assumptions.extend(op.assumptions)
    if assumptions:
        _rule("Assumed -- each of these could be wrong")
        for a in dict.fromkeys(assumptions):
            print(f"  - {a}")

    if pre.numbers:
        _rule("Computed")
        for k, v in pre.numbers.items():
            print(f"  {k.replace('_', ' ')} = {v:.4g}")
    for finding in pre.findings:
        _rule(f"Physics check ({finding.level})")
        print(f"  {finding.message}")
        if finding.detail:
            print(f"  {finding.detail}")
    if gaps:
        _rule("The plan is not complete")
        for g in gaps:
            print(f"  - {g}")


def _ask(questions) -> str:
    """Put the open questions to whoever is at the terminal."""
    answers = []
    _rule("I need a few things before this can be a script")
    for q in questions:
        print(f"\n{q.question}")
        if q.why:
            print(f"  why: {q.why}")
        if q.options:
            print(f"  options: {', '.join(q.options)}")
        try:
            answer = input("  > ").strip()
        except EOFError:
            answer = ""
        if answer:
            answers.append(f"{q.question} {answer}")
    return "  ".join(answers)


# --------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------
def _plan_once(gem, kb, message, domain, sess):
    context = retriever.retrieve(kb, message, domain)
    prompt = planner.build_prompt(
        message, domain, context, history=sess.recent(),
        current_plan=sess.plan, emittable=emitter.supported_ops())
    draft = gem.structured(planner.SYSTEM, prompt, planner.DraftPlan)
    return planner.draft_to_plan(draft), draft.reply


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m engineering.cli",
        description="Describe a simulation; get a SolidWorks script.")
    parser.add_argument("message", nargs="*",
                        help="what to simulate, in your own words")
    parser.add_argument("-o", "--out", default="",
                        help="where to write the script "
                             "(default: <study name>.py)")
    parser.add_argument("--domain", default=None, choices=sorted(D.DOMAINS),
                        help="force the study type instead of letting the "
                             "model choose")
    parser.add_argument("--session", default="",
                        help="keep the plan under this name so the next "
                             "message edits it instead of starting over")
    parser.add_argument("--force", action="store_true",
                        help="emit the script even with questions open")
    parser.add_argument("--run", action="store_true",
                        help="run the script here -- only useful on the "
                             "machine SolidWorks is installed on")
    parser.add_argument("--no-questions", action="store_true",
                        help="never prompt; report what is missing instead")
    parser.add_argument("--eval", action="store_true",
                        help="plan the reference requests in "
                             "tests/planner_eval.py with the live model and "
                             "check each plan; words after it filter cases")
    parser.add_argument("--converge", action="store_true",
                        help="solve twice, the second time on a finer mesh, "
                             "and report how far the answer moved")
    parser.add_argument("--check-models", action="store_true",
                        help="send one tiny request to each configured model "
                             "and say which are answering right now")
    args = parser.parse_args(argv)

    message = " ".join(args.message).strip()
    standalone = args.eval or args.check_models
    if not message and not standalone and not sys.stdin.isatty():
        message = sys.stdin.read().strip()
    if not message and not standalone:
        parser.error("say what to simulate")

    if not config.API_KEY:
        for line in config.NO_KEY_HELP.splitlines():
            print(f"  {line}")
        return 1

    gem = planner.Gemini()
    if not gem.available:
        print(f"  Gemini is not available: {gem.status}")
        return 1

    if args.check_models:
        return _check_models(gem)

    print("  Loading the knowledge base...", flush=True)
    kb = KnowledgeBase.load(Embedder())

    if args.eval:
        from .tests import planner_eval
        return planner_eval.run(gem, kb, _plan_once, args.message)

    sess = sessions.load(args.session or None)
    sess.add("user", message)
    guess, _ = D.classify(message, default=sess.domain or "static")
    domain = args.domain or guess

    try:
        plan, reply = _plan_once(gem, kb, message, domain, sess)
    except Exception as e:
        print(f"  The planner failed: {e}")
        return 1
    print(f"\n{reply}")

    resolved, questions, gaps = planner.finalise(plan)
    pre = physics.preflight(plan.domain, resolved)

    rounds = 0
    while (questions or pre.blocking) and not args.force \
            and not args.no_questions and sys.stdin.isatty() \
            and rounds < MAX_QUESTION_ROUNDS:
        rounds += 1
        extra = _ask(list(questions) + [
            Question(question=f.message, why=f.detail) for f in pre.blocking])
        if not extra:
            break
        message = f"{message}. {extra}"
        sess.plan = plan.model_dump()
        try:
            plan, reply = _plan_once(gem, kb, message, plan.domain, sess)
        except Exception as e:
            print(f"  The planner failed: {e}")
            return 1
        print(f"\n{reply}")
        resolved, questions, gaps = planner.finalise(plan)
        pre = physics.preflight(plan.domain, resolved)

    _print_plan(plan, resolved, questions, gaps, pre)

    sess.domain = plan.domain
    sess.plan = plan.model_dump()
    sess.resolved = [op.as_dict() for op in resolved]
    sess.add("assistant", reply)
    sess.save()
    if args.session:
        print(f"\n  Session saved as {sess.id}.")

    missing = emitter.unsupported(resolved)
    if missing:
        print(f"\n  This plan uses {', '.join(missing)}, which the emitter "
              f"cannot write yet. Describe the same thing another way -- a "
              f"torque as a force couple, a rotating region as a fixed "
              f"inlet velocity.")
        return 2

    if (questions or gaps or pre.blocking) and not args.force:
        print("\n  Not emitting a script while the plan is incomplete: it "
              "would run and answer a different question. Say more, or "
              "pass --force.")
        return 3

    out = emitter.emit(plan.domain, resolved, study_name=plan.study_name,
                       summary=plan.summary, assumptions=plan.assumptions,
                       warnings=plan.warnings + [f.message for f in pre.findings
                                                 if f.level != physics.INFO],
                       converge=args.converge)
    verdict = validate(out.code, plan.domain)
    if not verdict.ok:
        print("\n  The emitted script did not pass validation. That is a "
              "defect in the emitter, not in your request.")
        print(verdict.report())
        return 4

    target = Path(args.out or f"{plan.study_name or 'simulation'}.py")
    target.write_text(out.code, encoding="utf-8")
    print(f"\n  Script written to {target.resolve()}")
    if out.unverified_ops:
        print(f"  Unverified API calls in it: {', '.join(out.unverified_ops)}"
              f" -- read it before running it on a machine that matters.")
    if verdict.warnings:
        for w in verdict.warnings:
            print(f"  validator: {w}")

    if not args.run:
        return 0

    return _execute(gem, plan, resolved, out.code, message, sess)


def _check_models(gem) -> int:
    """Which models answer this key right now. 'High demand' and a used-up
    free-tier quota look the same from inside a failed plan; this tells
    them apart in a few seconds."""
    names = [config.PLANNER_MODEL] + [m for m in config.FALLBACK_MODELS
                                      if m != config.PLANNER_MODEL]
    answering = 0
    for name in names:
        try:
            gem.client.models.generate_content(model=name,
                                               contents=["Reply with: OK"])
            verdict = "ok"
            answering += 1
        except Exception as e:
            text = str(e)
            verdict = next((label for marker, label in (
                ("PerDay", "daily free-tier quota used up"),
                ("429", "per-minute quota -- wait a minute"),
                ("503", "overloaded right now"),
                ("404", "no such model"),
                ("NOT_FOUND", "no such model"),
                ("location", "not offered in this location"))
                if marker in text), text.splitlines()[0][:120])
        print(f"  {name:28} {verdict}")
    return 0 if answering else 1


def _execute(gem, plan, resolved, code, message, sess) -> int:
    """Run it here, then cross-check, explain and file the result."""
    if D.DOMAINS[plan.domain].engine == D.FLOW:
        print("\n  Flow Simulation: the script prints the wizard checklist. "
              "If the open part already has a Flow project, it solves that "
              "project and reads its goals; if not, build the project from "
              "the checklist and run again.")
    print("\n  Running. SolidWorks has to be open with the part active, and "
          "if a step targets your selection, select it now.")
    result = runner.run_script(code, plan.domain)
    print(result.stdout[-4000:] if result.stdout else "")
    if not result.ok:
        print(f"\n  {result.error}")
        return 5

    results = (result.receipt or {}).get("results", {}) or {}
    if results:
        _rule("Results")
        for k, v in results.items():
            print(f"  {k.replace('_', ' ')} = {v}")
    plots = (result.receipt or {}).get("plots") or []
    if plots:
        _rule("Plots")
        for p in plots:
            print(f"  {p}")

    checks = physics.crosscheck(plan.domain, resolved, results, result.receipt)
    if checks.findings:
        _rule("Independent cross-check")
        for f in checks.findings:
            print(f"  {f.message}")
            if f.detail:
                print(f"    {f.detail}")

    if result.receipt:
        example = library.ingest(result.receipt, message, sess.plan, resolved)
        print(f"\n  Filed as {example.status}: {example.verdict}")

    if results:
        try:
            _rule("What it means")
            print(planner.explain_results(gem, plan.summary, resolved, results,
                                          checks.as_dict(), message))
        except Exception as e:
            print(f"  (Explanation unavailable: {e})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
