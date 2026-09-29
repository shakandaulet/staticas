"""
The retrieval index: chunking, embedding, and a hybrid dense+sparse store.

WHY HYBRID AND NOT JUST EMBEDDINGS
-----------------------------------
This corpus is API documentation, and API documentation is full of
tokens that embeddings are bad at. "SelectByID2", "swsAnalysisStudyType",
"CreateNewStudy3", "ForceEndEdit" -- a dense model maps these to
roughly the same region as every other camel-cased identifier, because
it has learned they are all "a method name". Ask for CreateNewStudy3
and you get AddRestraint back, ranked confidently.

BM25 has the opposite failure: it cannot match "how do I clamp the end"
to a chunk about restraints, because they share no words.

Each arm covers the other's blind spot, so both run and the rankings
are fused. The fusion is Reciprocal Rank Fusion, which combines by
RANK rather than by score -- deliberately, because a cosine similarity
and a BM25 score are not on the same scale and any weighted sum of them
is a made-up number that has to be re-tuned whenever the corpus grows.

WHY THERE ARE THREE EMBEDDING BACKENDS
--------------------------------------
Gemini embeddings are the good ones, and since the server does not
start without a key they are the ones it uses. sentence-transformers is
the local alternative for when the Gemini client cannot be created. The
hashing vectoriser is neither good nor optional: it is what the test
suite runs on with no key and no network, and it stands in for chunks
whose embedding call failed -- at the index's own width, marked, and
embedded again on the next start rather than cached as if it were real.
A query whose embedding fails is ranked by BM25 alone.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import re
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from .. import config

# --------------------------------------------------------------------------
# Chunks
# --------------------------------------------------------------------------
@dataclass
class Chunk:
    """One retrievable unit.

    `domains` and `confidence` are not decoration: retrieval filters on
    domain so a thermal question never has its context window eaten by
    aerodynamics chunks, and confidence travels into the prompt so the
    model is told which parts of its own reference material are
    reconstructed rather than tested."""
    id: str
    kind: str                 # concept | api | recipe | quirk | template | example
    title: str
    text: str
    domains: list[str] = field(default_factory=list)
    confidence: str = "working_code"
    source: str = ""
    # Set by `<!-- pin -->` in a section. Some facts are useless when
    # they are merely LIKELY to be retrieved -- the entry point of an
    # API is either in front of the model or the model invents one --
    # and ranking has no way to know which those are.
    pinned: bool = False

    def digest(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()[:16]

    def header(self) -> str:
        tag = "" if self.confidence != "unverified" else " [UNVERIFIED API]"
        return f"[{self.kind}] {self.title}{tag}"


# --------------------------------------------------------------------------
# Chunking
# --------------------------------------------------------------------------
_FRONT_MATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """Read the small YAML-ish header at the top of a knowledge file.

    Hand-rolled rather than importing PyYAML: the only shapes used are
    `key: value` and `key: [a, b]`, and a dependency whose entire job is
    six lines of parsing is a dependency that will break someone's
    install for nothing."""
    m = _FRONT_MATTER.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            meta[key.strip()] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
        else:
            meta[key.strip()] = value
    return meta, text[m.end():]


def chunk_markdown(path: Path) -> list[Chunk]:
    """One chunk per '## ' section, inheriting the file's front matter.

    A section can override the file's domains with its own
    `<!-- domains: thermal, static -->` comment, because a file about
    boundary conditions legitimately contains sections that belong to
    different studies."""
    raw = path.read_text(encoding="utf-8")
    meta, body = _parse_front_matter(raw)
    file_domains = meta.get("domains", [])
    if isinstance(file_domains, str):
        file_domains = [file_domains]
    file_conf = meta.get("confidence", "working_code")
    kind = meta.get("kind", "concept")

    chunks: list[Chunk] = []
    parts = re.split(r"^## ", body, flags=re.MULTILINE)
    preamble = parts[0].strip()
    if preamble and len(preamble) > 120:
        chunks.append(Chunk(
            id=f"{path.stem}:intro", kind=kind,
            title=f"{path.stem.replace('_', ' ')} overview",
            text=preamble, domains=list(file_domains),
            confidence=file_conf, source=path.name))

    for part in parts[1:]:
        heading = part.splitlines()[0].strip()
        clean = re.sub(r"[*`]", "", heading).strip()
        slug = re.sub(r"[^a-z0-9]+", "-", clean.lower()).strip("-")[:60]

        domains = list(file_domains)
        dm = re.search(r"<!--\s*domains:\s*([^>]+?)\s*-->", part)
        if dm:
            domains = [d.strip() for d in dm.group(1).split(",") if d.strip()]
        conf = file_conf
        cm = re.search(r"<!--\s*confidence:\s*(\w+)\s*-->", part)
        if cm:
            conf = cm.group(1)
        pinned = bool(re.search(r"<!--\s*pin\s*-->", part))

        chunks.append(Chunk(
            id=f"{path.stem}:{slug}", kind=kind, title=clean,
            # The heading goes back into the text because it carries the
            # method name, which is exactly what the sparse arm matches on.
            text=f"## {part.strip()}",
            domains=domains, confidence=conf, source=path.name,
            pinned=pinned))
    return chunks


def chunk_python(path: Path, domains: list[str] | None = None) -> list[Chunk]:
    """One chunk per top-level function, plus the module docstring.

    ast rather than line-splitting, so the split follows real structure.
    Each function keeps its docstring AND its source: the docstring is
    what makes it match a natural-language query, the source is what is
    actually useful once retrieved."""
    source = path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines = source.splitlines()
    out: list[Chunk] = []

    doc = ast.get_docstring(tree)
    if doc:
        out.append(Chunk(
            id=f"{path.stem}:module", kind="template",
            title=f"{path.stem} overview", text=doc,
            domains=list(domains or []), source=path.name))

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = "\n".join(lines[node.lineno - 1:node.end_lineno])
        fdoc = ast.get_docstring(node) or ""
        out.append(Chunk(
            id=f"{path.stem}:fn:{node.name}", kind="template",
            title=node.name,
            text=f"Reference implementation `{node.name}`. {fdoc}\n\n{body}",
            domains=list(domains or []), source=path.name))
    return out


def chunk_examples(path: Path) -> list[Chunk]:
    """Verified runs, one chunk each.

    These are the highest-value chunks in the corpus and the only ones
    that grow on their own: a run whose numbers survived the beam-theory
    cross-check is evidence that a particular setup works, which is
    worth more to the next plan than any amount of documentation.

    ONLY verified runs. A run that solved but disagreed with the
    closed-form check is kept in the library for a human to look at and
    kept OUT of retrieval, because a confidently wrong example teaches
    the next plan to be wrong the same way and does it invisibly."""
    if not path.exists():
        return []
    out: list[Chunk] = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("status") != "verified":
            continue
        domain = rec.get("domain", "static")
        plan = json.dumps(rec.get("plan", {}), indent=1)[:2500]
        results = json.dumps(rec.get("results", {}), indent=1)[:800]
        out.append(Chunk(
            id=f"example:{rec.get('id', i)}", kind="example",
            title=f"Verified {domain} run: {rec.get('summary', '')[:70]}",
            text=(f"A {domain} study that ran and whose results were "
                  f"cross-checked.\nRequest: {rec.get('request', '')}\n"
                  f"Plan:\n{plan}\nResults:\n{results}\n"
                  f"Verdict: {rec.get('verdict', 'unknown')}"),
            domains=[domain], confidence="verified", source=path.name))
    return out


def build_corpus() -> list[Chunk]:
    chunks: list[Chunk] = []
    for md in sorted(config.KNOWLEDGE_DIR.glob("*.md")):
        chunks.extend(chunk_markdown(md))
    for py in sorted(config.TEMPLATES_DIR.glob("*.py")):
        if py.name == "__init__.py":
            continue
        chunks.extend(chunk_python(py))
    chunks.extend(chunk_examples(config.EXAMPLES_PATH))
    return chunks


# --------------------------------------------------------------------------
# Embeddings
# --------------------------------------------------------------------------
_TOKEN = re.compile(r"[a-zA-Z_][a-zA-Z0-9_]+|[а-яёА-ЯЁ]+|\d+")

# Split camelCase and PascalCase so the sparse arm can match "select by
# id" against "SelectByID2". Without this, every API name is one opaque
# token and BM25 only ever matches an exact retype of it.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


_TRAILING_DIGITS = re.compile(r"\d+$")


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for tok in _TOKEN.findall(text or ""):
        low = tok.lower()
        out.append(low)
        if any(c.isupper() for c in tok[1:]):
            for part in _CAMEL.split(tok):
                if len(part) < 2:
                    continue
                out.append(part.lower())
                # SolidWorks version-suffixes nearly every method it has
                # ever revised: SelectByID2, CreateNewStudy3,
                # GetSelectedObject6. Someone asking about
                # "CreateNewStudy" should match "CreateNewStudy3", and
                # without stripping the digit the sparse arm needs the
                # exact suffix retyped.
                stripped = _TRAILING_DIGITS.sub("", part).lower()
                if stripped and stripped != part.lower() and len(stripped) > 1:
                    out.append(stripped)
    return out


class Embedder:
    """Whichever embedding backend is actually available.

    Chosen once at construction and reported, so the /health endpoint
    can say which one is in use -- retrieval quality differs enough
    between them that 'which embedder' is the first question to ask when
    results look wrong."""

    def __init__(self, prefer: str = "auto"):
        self.backend = "hash"
        self.dim = 512
        self._st = None
        self._genai = None

        if prefer in ("auto", "gemini") and config.API_KEY:
            try:
                from google import genai
                self._genai = genai.Client(api_key=config.API_KEY)
                self.backend = "gemini"
                self.dim = 768
                return
            except Exception:
                self._genai = None

        if prefer in ("auto", "local"):
            try:
                from sentence_transformers import SentenceTransformer
                self._st = SentenceTransformer("all-MiniLM-L6-v2")
                self.backend = "sentence-transformers"
                self.dim = 384
                return
            except Exception:
                self._st = None

    def encode(self, texts: list[str], is_query: bool = False) -> np.ndarray | None:
        """Vectors for `texts`, or None when the backend failed.

        None rather than a stand-in: the old stand-in was a hashed vector
        of a DIFFERENT size (512 against Gemini's 768), which crashed the
        index build the first time the free-tier quota ran out, and would
        have been meaningless next to Gemini vectors even at the right
        size. The caller knows what a missing vector should mean -- a
        chunk to embed again next time, a query ranked by BM25 alone."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        if self.backend == "gemini":
            try:
                return self._encode_gemini(texts, is_query)
            except Exception as e:
                # Saying so matters: a broken key otherwise looks like bad
                # retrieval, which is a much harder thing to debug.
                print(f"  [rag] Gemini embedding failed: {_short(e)}")
                return None
        if self.backend == "sentence-transformers":
            vecs = self._st.encode(texts, convert_to_numpy=True)
            return _normalize(np.asarray(vecs, dtype=np.float32))
        return _hash_embed(texts, self.dim)

    def _encode_gemini(self, texts: list[str], is_query: bool) -> np.ndarray:
        """Batches of 32, waiting out a rate limit when the server says how
        long to wait.

        The free tier allows 100 embedded texts a minute, and the corpus
        is past that, so a full rebuild always hits the limit once. It
        is a pause, not a failure. A query does not wait: one planning
        request stalling for half a minute is worse than ranking that one
        request by BM25 alone.

        output_dimensionality is not optional: gemini-embedding-001
        returns 3072 numbers by default, and the index is 768 wide."""
        from google.genai import types
        # Asymmetric task types matter: a question and the passage that
        # answers it are not the same kind of text, and telling the model
        # which is which is free accuracy.
        task = "RETRIEVAL_QUERY" if is_query else "RETRIEVAL_DOCUMENT"
        cfg = types.EmbedContentConfig(task_type=task,
                                       output_dimensionality=self.dim)
        vectors: list[list[float]] = []
        for i in range(0, len(texts), 32):
            batch = [t[:8000] for t in texts[i:i + 32]]
            for attempt in range(4):
                try:
                    resp = self._genai.models.embed_content(
                        model=config.EMBED_MODEL, contents=batch, config=cfg)
                    break
                except Exception as e:
                    wait = _retry_delay(e)
                    if wait is None or is_query or attempt == 3:
                        raise
                    print(f"  [rag] embedding quota reached; waiting "
                          f"{wait:.0f} s as the API asks", flush=True)
                    time.sleep(wait)
            vectors.extend(e.values for e in resp.embeddings)
        return _normalize(np.asarray(vectors, dtype=np.float32))


def _retry_delay(exc: Exception) -> float | None:
    """Seconds to wait before retrying a rate-limited call, or None when
    the error is not a rate limit (a bad key must not be retried)."""
    text = str(exc)
    if "429" not in text and "RESOURCE_EXHAUSTED" not in text:
        return None
    m = re.search(r"retry in ([\d.]+)s", text) or \
        re.search(r"retryDelay'?:\s*'(\d+)s", text)
    return min(float(m.group(1)) + 1.0, 65.0) if m else 30.0


def _short(exc: Exception) -> str:
    text = str(exc)
    return text if len(text) < 240 else text[:240] + "..."


def _normalize(v: np.ndarray) -> np.ndarray:
    """L2-normalise so cosine similarity is a plain dot product."""
    if v.ndim == 1:
        v = v[None, :]
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    return v / np.clip(norms, 1e-9, None)


def _hash_embed(texts: list[str], dim: int) -> np.ndarray:
    """Deterministic bag-of-words hashing, with sub-word shingles.

    Not good. Good enough to retrieve the right chunk for an exact
    method name, which is the query that matters most when the embedding
    call has just failed -- and the one the tests rely on."""
    out = np.zeros((len(texts), dim), dtype=np.float32)
    for row, text in enumerate(texts):
        toks = tokenize(text)
        for tok in toks:
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest()[:8], 16)
            out[row, h % dim] += 1.0
            # A character trigram bucket gives partial credit for
            # 'CreateNewStudy' against 'CreateNewStudy3'.
            for i in range(len(tok) - 2):
                g = int(hashlib.md5(tok[i:i + 3].encode()).hexdigest()[:8], 16)
                out[row, g % dim] += 0.25
    return _normalize(out)


# --------------------------------------------------------------------------
# Sparse arm: BM25
# --------------------------------------------------------------------------
class BM25:
    """Okapi BM25 over the same chunks the dense arm sees.

    Implemented here rather than pulled in, because it is forty lines
    and the alternative is a dependency that has to be installed on
    every lab PC before the assistant will start."""

    K1 = 1.5
    B = 0.75

    def __init__(self, docs: list[list[str]]):
        self.docs = docs
        self.N = len(docs)
        self.lengths = np.array([len(d) for d in docs], dtype=np.float32)
        self.avg_len = float(self.lengths.mean()) if self.N else 0.0
        self.tf: list[Counter] = [Counter(d) for d in docs]
        df = Counter()
        for d in docs:
            df.update(set(d))
        self.idf = {
            term: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
            for term, n in df.items()
        }

    def scores(self, query: str) -> np.ndarray:
        out = np.zeros(self.N, dtype=np.float32)
        if not self.N:
            return out
        for term in set(tokenize(query)):
            idf = self.idf.get(term)
            if idf is None:
                continue
            for i, tf in enumerate(self.tf):
                f = tf.get(term, 0)
                if not f:
                    continue
                denom = f + self.K1 * (
                    1 - self.B + self.B * self.lengths[i] / max(self.avg_len, 1e-9))
                out[i] += idf * f * (self.K1 + 1) / denom
        return out


# --------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------
VECTORS_PATH = config.INDEX_DIR / "vectors.npz"
CHUNKS_PATH = config.INDEX_DIR / "chunks.json"


class KnowledgeBase:
    def __init__(self, chunks: list[Chunk], vectors: np.ndarray,
                 embedder: Embedder):
        self.chunks = chunks
        self.vectors = vectors
        self.embedder = embedder
        self.bm25 = BM25([tokenize(f"{c.title} {c.text}") for c in chunks])

    # -- lifecycle ---------------------------------------------------------
    @classmethod
    def build(cls, embedder: Embedder | None = None,
              reuse_cache: bool = True) -> "KnowledgeBase":
        """Embed the corpus, reusing vectors for text that has not changed.

        The cache is keyed on a hash of the chunk TEXT, not on the file's
        mtime. Editing one section of one knowledge file then costs one
        embedding call, not a full rebuild -- which is what makes it
        reasonable to re-index on every ingested run."""
        embedder = embedder or Embedder()
        chunks = build_corpus()

        cached: dict[str, np.ndarray] = {}
        if reuse_cache and VECTORS_PATH.exists() and CHUNKS_PATH.exists():
            try:
                blob = np.load(VECTORS_PATH)
                old = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))
                if blob["vectors"].shape[1] == embedder.dim and \
                        old.get("backend") == embedder.backend:
                    for rec, vec in zip(old["chunks"], blob["vectors"]):
                        if not rec.get("degraded"):
                            cached[rec["digest"]] = vec
            except Exception:
                cached = {}

        vectors = np.zeros((len(chunks), embedder.dim), dtype=np.float32)
        todo_idx, todo_text = [], []
        for i, c in enumerate(chunks):
            hit = cached.get(c.digest())
            if hit is not None:
                vectors[i] = hit
            else:
                todo_idx.append(i)
                todo_text.append(f"{c.title}\n{c.text}")

        degraded: set[str] = set()
        if todo_text:
            fresh = embedder.encode(todo_text, is_query=False)
            if fresh is None:
                # Keep going at the right width, and remember which rows
                # are stand-ins so the next start embeds them for real
                # instead of serving them from the cache forever.
                fresh = _hash_embed(todo_text, embedder.dim)
                degraded = {chunks[i].digest() for i in todo_idx}
                print(f"  [rag] {len(degraded)} chunks hashed for now; they "
                      f"will be embedded again on the next start.")
            for slot, i in enumerate(todo_idx):
                vectors[i] = fresh[slot]

        # The hashing backend is rebuilt in a fraction of a second and is
        # what the test suite runs on; writing it out used to overwrite the
        # real index, so the next start re-embedded the whole corpus and
        # spent the free-tier quota doing it.
        if embedder.backend != "hash":
            np.savez_compressed(VECTORS_PATH, vectors=vectors)
            CHUNKS_PATH.write_text(json.dumps({
                "backend": embedder.backend,
                "dim": embedder.dim,
                "chunks": [{**asdict(c), "digest": c.digest(),
                            "degraded": c.digest() in degraded}
                           for c in chunks],
            }, indent=1, ensure_ascii=False), encoding="utf-8")

        print(f"  [rag] {len(chunks)} chunks, {len(todo_text)} embedded "
              f"({len(chunks) - len(todo_text)} cached), "
              f"backend={embedder.backend}")
        return cls(chunks, vectors, embedder)

    @classmethod
    def load(cls, embedder: Embedder | None = None) -> "KnowledgeBase":
        """Load from disk, rebuilding if anything does not line up."""
        embedder = embedder or Embedder()
        if not (VECTORS_PATH.exists() and CHUNKS_PATH.exists()):
            return cls.build(embedder)
        try:
            blob = np.load(VECTORS_PATH)
            meta = json.loads(CHUNKS_PATH.read_text(encoding="utf-8"))
            if meta.get("backend") != embedder.backend or \
                    meta.get("dim") != embedder.dim or \
                    any(rec.get("degraded") for rec in meta["chunks"]):
                return cls.build(embedder)
            chunks = [Chunk(**{k: v for k, v in rec.items()
                               if k not in ("digest", "degraded")})
                      for rec in meta["chunks"]]
            # A knowledge file edited while the server was down must not
            # be served from a stale index.
            if [c.digest() for c in chunks] != \
                    [c.digest() for c in build_corpus()]:
                return cls.build(embedder)
            return cls(chunks, blob["vectors"], embedder)
        except Exception:
            return cls.build(embedder)

    # -- search ------------------------------------------------------------
    def search(self, query: str, k: int = 8,
               domains: list[str] | None = None,
               kinds: set[str] | None = None) -> list[tuple[Chunk, float]]:
        """Hybrid retrieval: dense + BM25, fused by rank, then MMR.

        The domain filter is applied BEFORE ranking rather than after.
        Filtering afterwards means a thermal query can retrieve eight
        aerodynamics chunks, discard them, and return two results --
        quietly starving the prompt of exactly the context it needed."""
        if not self.chunks:
            return []

        allowed = [
            i for i, c in enumerate(self.chunks)
            if (kinds is None or c.kind in kinds)
            and (not domains or not c.domains
                 or any(d in c.domains for d in domains))
        ]
        if not allowed:
            allowed = list(range(len(self.chunks)))

        got = self.embedder.encode([query], is_query=True)
        sparse = self.bm25.scores(query)
        sparse_rank = _rank_of(sparse, allowed, config.SPARSE_K)
        if got is None:
            # No query vector: rank by BM25 alone rather than against a
            # stand-in that would put noise into the fusion.
            ordered = sorted(sparse_rank, key=sparse_rank.get)[:k]
            return [(self.chunks[i], 1.0 / (60.0 + sparse_rank[i]))
                    for i in ordered]
        qv = got[0]
        dense = self.vectors @ qv
        dense_rank = _rank_of(dense, allowed, config.DENSE_K)

        # Reciprocal Rank Fusion. 60 is the constant from the original
        # paper; it flattens the top of each list enough that one arm
        # being confidently wrong cannot dominate the other.
        fused: dict[int, float] = {}
        for rank_map in (dense_rank, sparse_rank):
            for idx, rank in rank_map.items():
                fused[idx] = fused.get(idx, 0.0) + 1.0 / (60.0 + rank)

        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
        picked = self._mmr([i for i, _ in ordered], qv, k)
        return [(self.chunks[i], float(fused.get(i, 0.0))) for i in picked]

    def _mmr(self, candidates: list[int], qv: np.ndarray, k: int) -> list[int]:
        """Maximal Marginal Relevance.

        Without it the top five hits for "apply a force" are five chunks
        that all say the same thing about AddForce, and the prompt
        learns nothing from slots two through five. MMR trades a little
        relevance for the coverage that actually changes the answer."""
        selected: list[int] = []
        pool = candidates[:max(k * 4, 20)]
        lam = config.MMR_LAMBDA
        while pool and len(selected) < k:
            best, best_score = None, -1e9
            for idx in pool:
                rel = float(self.vectors[idx] @ qv)
                red = max((float(self.vectors[idx] @ self.vectors[s])
                           for s in selected), default=0.0)
                score = (1 - lam) * rel - lam * red
                if score > best_score:
                    best, best_score = idx, score
            selected.append(best)
            pool.remove(best)
        return selected

    def stats(self) -> dict:
        by_kind = Counter(c.kind for c in self.chunks)
        by_domain = Counter(d for c in self.chunks for d in c.domains)
        return {
            "chunks": len(self.chunks),
            "backend": self.embedder.backend,
            "dim": self.embedder.dim,
            "by_kind": dict(by_kind),
            "by_domain": dict(by_domain),
            "unverified": sum(1 for c in self.chunks
                              if c.confidence == "unverified"),
        }


def _rank_of(scores: np.ndarray, allowed: list[int], depth: int) -> dict[int, int]:
    """Top-`depth` indices from `allowed`, as {index: 0-based rank}."""
    subset = sorted(allowed, key=lambda i: float(scores[i]), reverse=True)[:depth]
    return {idx: rank for rank, idx in enumerate(subset)}
