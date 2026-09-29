# Simulation assistant

A command line that sets up SolidWorks simulations — statics,
thermodynamics, fluid mechanics, aerodynamics, modal and buckling — from
a sentence like *"приложи 500 кН на этот торец и защеми другой конец"*.

It plans the study, checks the physics before anything runs, writes the
script, executes it, reads the numbers back and compares them against
the closed-form solution for the same idealised problem.

```
  message  →  classify  →  retrieve  →  plan  →  narrow  →  preflight
                                                              ↓
          answer  ←  cross-check  ←  run  ←  validate  ←  emit
```

---

## The one idea worth reading

**The model never writes the script.**

The previous generation of this project asked Gemini for finished
Python. That works until it doesn't, and when it doesn't the failure is
a wrong number buried in 200 lines of plausible COM calls. Every
safeguard around it — the validator, the mesh floor, the quirk list —
was clawing back reliability the architecture had given away.

Here Gemini fills in a **typed plan**: a list of operations with named,
unit-carrying quantities and a target for each. A deterministic emitter
turns that plan into code. The model does the part it is genuinely good
at — reading *"clamp the left end and hang 500 kN off the tip"* and
knowing that means a fixture plus a force — and none of the part it is
bad at, like remembering that `ForceEndEdit` takes no parentheses.

Two things fall out of that split:

- **Unit errors stop being possible.** The model reports `{500, "kN"}`
  and never converts. `core/units.py` converts, and it is tested. A
  silent factor of 1000 on a force is a simulation that runs, finishes
  and reports a plausible wrong answer; there is no good failure rate
  for that.
- **It works past statics.** A new physics domain is new rows in the
  operation catalogue plus an emitter — not a prompt retrained to be
  good at a second kind of script.

---

## Running it

```powershell
pip install -r requirements.txt
copy run.ps1.example run.ps1     # then put your key in run.ps1
.\run.ps1 "clamp the left end and hang 5 kN off the free end"
```

It prints the plan, asks about anything a wrong guess would change, and
writes the script beside you. Then:

```powershell
.\run.ps1 --session bracket "now make it 8 kN"   # edits the same plan
.\run.ps1 --run "..."                            # and runs it here
.\run.ps1 --run --converge "..."                 # solves twice, finer the second time
.\run.ps1 --check-models                         # which Gemini models answer right now
.\run.ps1 --eval                                 # the planner against reference requests
```

`--run` is only useful on the machine SolidWorks is installed on. A
run prints its results, the hand checks, and where it saved the plot
images (`runs/plots`) and -- for a part it built -- the part itself,
study and all (`runs/parts`).

**The free tier is small.** Planning uses `gemini-3.6-flash`, 5 calls a
minute and a daily cap; embedding the knowledge base takes about two
minutes the first time, because 100 texts a minute is the limit and the
index waits it out. When a model is overloaded or out of quota the
planner moves to the next one in `FALLBACK_MODELS`; `--check-models`
says which are answering.

The key is required: there is no offline mode. Without it the command
stops and says so, and a planning call that fails is an error rather
than a plan quietly built from keywords.

`run.ps1` is git-ignored; `run.ps1.example` is not. The key is read from
the environment and only from the environment — a key written into
source is a key that gets committed, and a committed key is a
compromised one.

There is no web layer in this repository: the server and the chat panel
belong to the rest of the team. Everything here is importable, so a
panel that wants a plan calls `core.planner` and `codegen.emitter` the
same way `cli.py` does.

### Tests

```powershell
python -m pytest engineering/tests/test_pipeline.py -q
```

80 tests, about three seconds, no SolidWorks, no Gemini, no network.
That covers most of the system on purpose: unit conversion is
arithmetic, narrowing is table lookup, sequencing is a sort, emission
is string formatting and validation is an AST walk. Only the planner
needs the API and only the runner needs SolidWorks.

On the machine with SolidWorks, the real runs:

```powershell
cd C:\
python -m engineering.tests.solidworks.real_parts          # all of them
python -m engineering.tests.solidworks.real_parts beam     # the ones matching
```

Twenty-two cases, about ten minutes: every study type on one reference
beam against its hand calculation; the loads and restraints one by one,
each on a part with an exact answer; statics on real shapes -- an
I-beam, a plate with a hole, an angle bracket (once by rule, once by a
face picked by hand), SolidWorks' own sample parts; and Flow Simulation
on SolidWorks' own examples. Each goes the way a plan does and then
checks the receipt -- where the restraint and the load actually went,
whether the supports carried the whole load -- because a load on the
wrong face solves just as happily. Run it after any change to
`templates/runtime.py`.

With the key, the planner itself: `.\run.ps1 --eval` plans a dozen
reference requests with the live model and checks the decisions that
matter in each -- the study type, the target, the direction, the
numbers. The model's side of the pipeline is otherwise untested.

---

## "Apply it to THIS edge"

A browser panel cannot see your cursor in SolidWorks, and guessing which
edge you meant is the worst available answer — the study runs and
reports a number either way.

So *"this face"*, *"этот торец"*, *"the edge I selected"* all resolve to
`active_selection`: **whatever you have highlighted in SolidWorks when
the script runs**. You point at the edge in the CAD window, where
pointing at things is easy. If nothing is selected the script stops and
says so, rather than falling back to a face you never chose. The
selection is read before anything else happens: a force stated along an
axis selects the Front plane, which replaces what you picked. And since
there is only one selection, a restraint and a load both on "this face"
become a question, not a script.

Targets that do not need you present are resolved from the part's own
shape at run time, never from a hardcoded coordinate:

- **The bolt holes** — round holes, told from fillets by being concave
  and closing all the way round. All of them, one size, or the row
  nearest one end: *"hold it by the big bore, load the eye at the far
  end"*.
- **The free end, the clamped end** — the flat end faces of the part's
  *longest* axis, whichever axis that is.
- **Top, bottom, front, back, side** — flat faces facing ±Y, ±Z, +X, as
  the SolidWorks views name them. A level further in is `index 1`: a
  shoulder, the top of a base under an upright.

Only flat faces facing the right way count. A rounded end has none, and
then the script refuses and lists what the part does have — its flat
directions and its holes — instead of putting a load that expected a
flat face onto a curved one. That rule came from SolidWorks' own sample
parts: the old ranking by bounding-box centre put "the free end" of a
control arm on its big flat side and "the top" of a round shaft on its
curved surface, and both solved without complaint.

---

## What is checked, and when

**Before emission** — `core/physics.py` runs the dimensionless groups.
Reynolds decides laminar versus turbulent; Mach decides whether
compressibility can be ignored; Biot decides whether the conduction
solve is telling you something you already knew; span/depth decides
whether beam theory applies. A turbulent flow with the laminar box
ticked is **blocked**, not warned about: it converges, and it
under-predicts pressure drop by several times.

**At emission** — `codegen/validator.py` walks the AST. Blocked imports,
COM properties called as methods, and the mesh floor, which **fails
closed**: a size argument that cannot be resolved to a number blocks,
because *"I could not check this"* and *"this is fine"* are different
answers. An earlier version of that check only inspected literals, a
script slipped through, and it hung SolidWorks during meshing.

**At run time** — the emitted script re-checks the mesh floor itself
(once downloaded it is out of this project's hands) and **reads the
study type back** off the study it just created, aborting on a
mismatch. That check exists because the previous generation passed `1`
for what its own docstring called a static study — and `1` is
frequency. Every "static" run was silently solving for mode shapes. The
call succeeded, the solve succeeded, and the numbers looked like
numbers.

**During the run** — every structural study also reads back:

- the **reaction at the supports**, against the load the script wrote.
  That is the check that caught SolidWorks applying a force to *each*
  selected face: 2 kN through the eye of the sample control arm, one
  hole split into two half-cylinders, went on as 4 kN and solved
  without complaint;
- the **stress away from the supports** — a fixed face is a
  singularity, its peak grows with every refinement — and the **factor
  of safety** against the material's yield, both at the peak and away
  from it. The material's E, density and yield are read from the
  SolidWorks library file itself;
- the **plot images**, stress and displacement, temperature, mode shape.

**After the run** — the result is compared against the textbook solution
for the same idealised problem: cantilever deflection about the axis
the load acts across, `M·c/I` a short way from the root, axial `F·L/EA`,
`Kt` at a hole, the cantilever frequency, Euler, the heat balance with
convection and radiation, a pressure drop as a loss coefficient,
Darcy–Weisbach for a pipe, `Cd`. Agreement is evidence the setup was
sane. Disagreement is not proof of error, but it is always worth a
sentence, and it is what decides whether the run is filed as a verified
example. `--converge` solves once more on a finer mesh and reports how
far the answer moved.

---

## The RAG system

`rag/index.py` — hybrid retrieval over `knowledge/` and `templates/`.

**Dense + sparse, fused by rank.** API documentation is full of tokens
embeddings are bad at: ask a dense model for `CreateNewStudy3` and it
returns `AddRestraint`, confidently, because it has learned they are
both "a method name". BM25 has the opposite blind spot — it cannot
match *"how do I clamp the end"* to a chunk about restraints. Both arms
run and the rankings are combined by Reciprocal Rank Fusion, which
fuses by **rank** rather than score: a cosine similarity and a BM25
score are not on the same scale, and any weighted sum of them is a
made-up number that needs re-tuning whenever the corpus grows.

Also in there:

- **MMR** for diversity, so the top five hits for *"apply a force"* are
  not five chunks that all say the same thing.
- **Domain filtering before ranking**, not after — filtering afterwards
  lets a thermal query retrieve eight aerodynamics chunks, discard
  them, and quietly starve the prompt.
- **CamelCase and trailing-digit splitting**, so `CreateNewStudy` finds
  `CreateNewStudy3`. SolidWorks version-suffixes nearly every method it
  has ever revised.
- **Three embedding backends** — Gemini, sentence-transformers, and a
  deterministic hashing vectoriser. The CLI uses Gemini, at 768
  dimensions asked for explicitly (gemini-embedding-001 returns 3072 by
  default, and the index is 768 wide). A rate limit is waited out; a
  batch that still fails is hashed at the same width, marked, kept out
  of the cache and embedded again on the next start; a query that fails
  is ranked by BM25 alone. The first live run found both halves of that
  broken: the stand-in was 512 wide and crashed the build.
- **Query expansion from the classified intent**, not the raw message.
  *"приложи силу на торец"* shares no vocabulary with a chunk titled
  "AddForce — ICWLoadsAndRestraintsManager"; the domain name and
  candidate operation names do. That is why retrieval works the same in
  both languages with no translation step.

**Pinned versus retrieved.** The quirk list, the domain's selection
recipes, and any section marked `<!-- pin -->` are injected whole, every
time. The pin is for facts that are useless when they are merely
*likely* to be retrieved: the entry point of an API is either in front
of the model or the model invents one — this corpus carried an invented
ProgID for Flow Simulation for weeks — and no similarity score can know
that a connection snippet matters to a question about pressure drop.
The reference block is capped at 48 000 characters, raised from 24 000
once the corpus had grown enough that the pinned list was being cut off
part-way through. A plan needs the *complete* set
of gotchas, not the top-k most similar ones — ranking the quirk list
against a thermal query drops the VARIANT-null entry, and the script
then reproduces the bug the list exists to prevent. The API reference
and the example library are retrieved, because they grow without bound
and only a slice is ever relevant.

**The learning loop.** A run whose numbers survive the cross-check is
filed as a retrievable example, and the next command re-indexes it, so
it reaches the *next* plan. A run that solved but disagreed is kept for
a human to look at and kept **out** of retrieval: a confidently wrong
example teaches the next plan to be wrong the same way, invisibly. A
design warning -- "it yields" -- is not a disagreement and does not
keep a run out. The previous generation's nine verified cantilever runs
were imported the same way (`python -m engineering.memory.library
<its examples folder>`); its earliest run, which solved a frequency
study while calling it static, was not.

---

## Confidence is part of the data

The corpus and the operation catalogue both carry how well each thing is
known, and it travels all the way to the chat window:

| level | meaning |
|-------|---------|
| `verified` | confirmed against code that ran and produced sane numbers |
| `working_code` | from the previous project's tested scripts |
| `unverified` | reconstructed from documentation, not executed here |

**Flow Simulation runs, but does not build projects.** It is a separate
add-in from SolidWorks Simulation, with its own licence and object
model. Its registered API solves a project and reads the goals back --
run here on SolidWorks' own examples: a ball valve losing 16.3 kPa, a
cylinder at Re = 10⁵ with Cd = 0.99 -- but does not name the
parameters a new project is made of, and the API that does is not
registered by the SDK installer. So a fluid script prints a **wizard
checklist** (every setting decided, converted and regime-checked, in
the order the wizard asks for them, then the boundary conditions and
the goals), and when the part already has a Flow project it solves it,
reads its goals and turns them into a pressure drop, a loss coefficient
or a drag coefficient. Building the project from the checklist takes a
minute; the script does the rest on the next run.

Everywhere else, unverified calls are wrapped so a failure names the
call and what it was trying to do, and the plan card flags the step.

---

## Layout

```
core/       schema.py      the typed plan
            ops.py         the operation catalogue — one table, four consumers
            domains.py     nine domains, study-type codes, classification
            units.py       every conversion, in one tested place
            physics.py     dimensionless groups and closed forms
            planner.py     the Gemini half
rag/        index.py       chunking, embeddings, hybrid store
            retriever.py   query expansion, pinning, citations
knowledge/  one markdown file per domain, chunked by section
templates/  runtime.py     the prelude every generated script inlines
codegen/    emitter.py     plan → Python, deterministically
            validator.py   the AST gate
            runner.py      subprocess, timeout, receipt
memory/     session.py     session state, so a follow-up edits the plan
            library.py     the verified-example loop
tests/      test_pipeline.py          everything that needs no SolidWorks
            planner_eval.py           the live model on reference requests
            solidworks/real_parts.py  real runs, every study type and real parts
cli.py      the command line: plan, questions, script, run
```

`core/ops.py` is worth reading first. The `OP_SPECS` table is the single
source of truth for every operation, and four different parts of the
project read it: the planner prompt is **generated** from it, narrowing
checks against it, clarifying questions come out of it, and the emitter
dispatches on it. That is what stops the model being told about an
operation the emitter cannot emit — the usual reason an LLM tool
confidently promises something and then produces nothing.

---

## Adding a physics domain

1. A row in `core/domains.py` — study type, what it answers, required
   operations, keywords in both languages.
2. Rows in `OP_SPECS` for any operation it needs that does not exist
   yet, with slots, units and confidence.
3. An emitter function in `codegen/emitter.py`, registered in
   `_EMITTERS`.
4. Runtime helpers in `templates/runtime.py`, marked CONFIRMED or
   UNCONFIRMED honestly.
5. A `knowledge/<domain>.md` with front matter. It is chunked and
   indexed automatically.
6. Regime checks in `physics.preflight` if the domain has a way of
   being set up self-consistently wrong. Most do.

The prompt, the UI, the question logic and the citation list all follow
from those. None of them needs touching.

---

## Limits, stated plainly

- The validator is a **static check, not a sandbox**. It stops a
  drifting model and an edited script. It does not stop someone who
  controls the prompt and wants to run code on this machine, so `--run`
  deserves the caution you would give a script a stranger sent you. The
  script is written out before it runs, and reading it costs a minute.
- **Meshes are second-order ("high") by default.** SolidWorks numbers
  draft 0 and high 1, and this project had them swapped: every run it
  called draft was high quality, which is why the reference beam agreed
  with beam theory to 0.03%. A true draft mesh on that beam is 10% stiff.
  `quality=draft` is there for a quick look and says so.
- **Peak stress at a fixed face is meaningless.** A restraint is a
  singularity in FE: the stress there grows with refinement instead of
  converging. Every structural run reports the peak away from the
  supports as well, and that is the one to hold against yield.
- The Simulation calls in `templates/runtime.py` were rewritten against
  the published API reference and then **run against SolidWorks 2024**.
  On one 50 × 100 × 2000 mm steel beam, against hand calculations using
  the library's own material data: 15.24 mm against 15.24, 117.0 MPa
  43 mm from the root against 117.4, a 2.69 K rise against 2.73, 10.50 Hz
  against 10.48, a buckling factor of 13.52 against 13.49, and the
  non-linear solve matching the linear one.
- **Loads and restraints**, each on a part with an exact answer: heat
  flux, radiation and prescribed temperatures within 2% of the heat
  balance; roller and symmetry carrying exactly F/A; hinges at bolt
  holes; immovable (which on a solid is fixed); a force per hole.
- **Real parts**: an I-beam, a plate with a hole (59.4 MPa at the hole
  against Kt × net = 64.6), a stepped shaft, an angle bracket bolted
  through its holes, and SolidWorks' anchor plate and control arm. The
  part builders — rectangular beam, box and hollow box, round bar, tube,
  I-beam, plate, plate with a hole, angle bracket — all ran.
- What has **not** been run: pressure and gravity (not needed for this
  project), drop test, fatigue, a temperature as a load in a static
  study, and building a Flow project from a plan. Parts with several
  solid bodies get their material and their targets on the first body
  only.
- **The planner has not been evaluated end to end yet.** `--eval` is
  written and ran far enough to find three bugs in the embedding path;
  the free-tier daily quota ran out before the reference requests could
  be planned.
- The Gemini Developer API is **not available in every country**. The
  same key works through a VPN and fails without it; the server
  translates that 400 into something actionable instead of retrying
  pointlessly.
