"""
An engineering assistant for SolidWorks simulation.

Statics, thermodynamics, fluid mechanics, aerodynamics, modal and
buckling analysis: describe the problem in a sentence, get a typed plan
you can check, a script that is emitted rather than generated, and the
numbers read back and cross-checked against closed form.

    python -m engineering.cli "clamp the left end, 5 kN on the tip"

Layout:
    cli.py      the command line, and the only entry point
    core/       the typed plan, the operation catalogue, units, physics
    rag/        hybrid dense+sparse retrieval over the knowledge base
    knowledge/  the corpus, one markdown file per domain
    templates/  the runtime prelude every generated script inlines
    codegen/    plan -> Python, the validator, the runner
    memory/     session state and the verified-example library

There is no web layer in here: the server and the chat panel belong to
the rest of the team.
"""

__version__ = "2.0.0"
