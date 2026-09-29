"""
Turning a user message into retrieved context.

WHAT IS RETRIEVED AND WHAT IS ALWAYS INJECTED
---------------------------------------------
Not everything in the corpus should be ranked. This is the design
decision the previous generation of this project got right and it is
kept here, extended to more domains:

  ALWAYS INJECTED -- the API quirk list, and the runtime recipes for
  the domain in play. A generated plan needs the COMPLETE set of
  gotchas, not the top-k most similar ones. Ranking the quirk list
  against a query about heat flux returns the quirks that mention
  temperature and drops the one about VARIANT nulls, and the model then
  reproduces the VARIANT bug -- which is precisely what the quirk list
  exists to prevent. These chunks are small, they are needed every
  time, and there is nothing for similarity search to usefully decide.

  RETRIEVED -- the API reference and the verified-example library.
  These grow without bound and only a slice is ever relevant, which is
  where similarity search earns its place.

QUERY EXPANSION
---------------
A user writes "приложи 500 Н на торец" or "make the duct not whistle".
Neither shares vocabulary with a chunk titled "AddForce -
ICWLoadsAndRestraintsManager". The query sent to retrieval is therefore
built from the CLASSIFIED INTENT, not from the raw message: domain
name, candidate operation names and their labels. That turns an
untranslatable phrase into the vocabulary the corpus is actually
written in, and it is why retrieval works the same in both languages
without a translation step.
"""

from __future__ import annotations

from dataclasses import dataclass

from .. import config
from ..core import domains as D
from ..core.ops import OP_SPECS
from .index import Chunk, KnowledgeBase


@dataclass
class RetrievedContext:
    chunks: list[Chunk]
    scores: dict[str, float]
    query: str

    def as_prompt_block(self, budget_chars: int = 48000) -> str:
        """Render for the model, newest-and-most-relevant first.

        The budget is a hard stop rather than a soft target: a prompt
        that overflows the context window fails as an opaque API error,
        and losing the last two reference chunks is a far better outcome
        than losing the request.

        48 000 characters, not 24 000. The corpus grew as runs confirmed
        things, and at the old budget the pinned quirk list was being cut
        off mid-way -- the API entry point for Flow Simulation, which had
        just cost an afternoon to establish, never reached the model at
        all. A hard stop that quietly drops what was just learned is
        worse than a longer prompt: this is a fraction of the context
        window either way."""
        parts: list[str] = []
        used = 0
        for c in self.chunks:
            block = f"{c.header()}\n{c.text}"
            if used + len(block) > budget_chars:
                parts.append(f"[... {len(self.chunks) - len(parts)} further "
                             f"reference chunks omitted for length ...]")
                break
            parts.append(block)
            used += len(block)
        return "\n\n---\n\n".join(parts)

    def citations(self) -> list[dict]:
        """What the panel shows under an answer.

        Citations are not a flourish. The corpus deliberately contains
        material of different reliability, and a user deciding whether
        to run a Flow Simulation script on a machine that matters
        deserves to see that its API calls came from a chunk marked
        unverified."""
        return [{
            "id": c.id, "title": c.title, "kind": c.kind,
            "source": c.source, "confidence": c.confidence,
            "domains": c.domains,
            "score": round(self.scores.get(c.id, 0.0), 5),
        } for c in self.chunks]


def build_query(message: str, domain: str,
                op_hints: list[str] | None = None) -> str:
    """Expand a raw message into the corpus's own vocabulary."""
    spec = D.DOMAINS.get(domain)
    parts = [message.strip()]
    if spec:
        parts.append(spec.label)
        parts.append(spec.what_it_answers)
        parts.extend(spec.required_ops)
        parts.extend(spec.expected_ops)
    for op in (op_hints or []):
        ospec = OP_SPECS.get(op)
        if ospec:
            parts.append(f"{ospec.label} {op}")
    return " ".join(p for p in parts if p)[:2000]


def _always_injected(kb: KnowledgeBase, domain: str) -> list[Chunk]:
    """Quirks, recipes for this domain, and anything marked `<!-- pin -->`.

    The pin is for facts that are useless when they are merely LIKELY to
    be retrieved. The entry point of an API is the example: it is either
    in front of the model or the model invents one, and an invented
    ProgID for Flow Simulation is what this project carried in its
    corpus for weeks. A similarity score cannot know that a connection
    snippet matters to a question about pressure drop; the person
    writing the corpus can."""
    out: list[Chunk] = []
    for c in kb.chunks:
        scoped = not c.domains or domain in c.domains
        if c.kind == "quirk":
            out.append(c)
        elif c.kind == "recipe" and scoped:
            out.append(c)
        elif c.pinned and scoped:
            out.append(c)
    return out


def retrieve(kb: KnowledgeBase, message: str, domain: str,
             op_hints: list[str] | None = None,
             k: int | None = None) -> RetrievedContext:
    query = build_query(message, domain, op_hints)
    k = k or config.FINAL_K

    ranked = kb.search(query, k=k, domains=[domain],
                       kinds={"api", "concept", "example", "template"})

    pinned = _always_injected(kb, domain)
    seen = {c.id for c in pinned}
    merged = pinned + [c for c, _ in ranked if c.id not in seen]

    scores = {c.id: s for c, s in ranked}
    # Pinned chunks get a sentinel score so the citation list can show
    # them as always-included rather than as a suspiciously perfect hit.
    for c in pinned:
        scores.setdefault(c.id, -1.0)

    return RetrievedContext(chunks=merged, scores=scores, query=query)


def retrieve_for_question(kb: KnowledgeBase, question: str,
                          domain: str | None = None,
                          k: int = 6) -> RetrievedContext:
    """Plain question answering -- 'what does the Biot number tell me',
    'why does my internal analysis say the geometry is not closed'.

    No pinned chunks here: a question is not a plan, and injecting the
    whole quirk list into an answer about dimensionless groups pushes
    out the chunk that actually answers it."""
    dom = domain or D.classify(question)[0]
    ranked = kb.search(question, k=k, domains=[dom] if domain else None)
    return RetrievedContext(chunks=[c for c, _ in ranked],
                            scores={c.id: s for c, s in ranked},
                            query=question)
