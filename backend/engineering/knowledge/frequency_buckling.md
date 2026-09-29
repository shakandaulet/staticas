---
kind: api
domains: [frequency, buckling]
confidence: unverified
---

# Frequency and buckling: the two studies that answer a different question

## Frequency studies ignore your loads

<!-- confidence: verified -->

A modal analysis solves an eigenvalue problem in mass and stiffness.
Applied forces do not appear in it. Adding a 500 N load to a frequency
study and expecting the natural frequency to change is a very common
reflex, and the frequency comes back identical.

The exception is **preload**: a stress state genuinely does change
stiffness -- a guitar string tightened rises in pitch, a column near
its buckling load loses stiffness and its first mode falls towards
zero. Capturing that needs preload to be switched on explicitly, which
is a different setting from simply adding a force.

Mass, on the other hand, matters enormously. Frequency goes as
`sqrt(k/m)`, so a missing material density or a part modelled without
its attached mass gives a first mode that is far too high.

## Restraints are the whole answer in a modal study

<!-- confidence: verified -->

A completely unrestrained part has six rigid-body modes at
approximately zero frequency before any real mode appears. Seeing
"0.0001 Hz" as the first mode is not a solver problem -- it is the part
floating, and either the restraint is missing or the free-free result
is what was wanted and the first six should be ignored.

The same cantilever beam clamped at one end versus both ends differs by
a factor of about 6.4 in first frequency. There is no mesh refinement
that fixes a wrong restraint.

## What a first frequency is worth

<!-- confidence: verified -->

Cantilever, uniform section:

```
f1 = (1.875^2 / 2*pi) * sqrt(E*I / (rho*A*L^4))
```

The exponent on `L` is the useful part: frequency scales as `1/L²`.
Doubling a beam's length drops its first mode by a factor of four. That
is usually the quickest answer to "how do I stop it vibrating" --
shorten it or stiffen it, because adding mass moves it the wrong way.

The engineering question is almost never the frequency on its own; it
is whether that frequency sits near an excitation. A motor at 3000 rpm
is 50 Hz, and its blade-pass and second harmonic are higher. A margin
of about 20% either side of an excitation is the usual target.

## Buckling reports a factor, not a stress

<!-- confidence: working_code -->

A linear buckling study returns a **load factor**: multiply the applied
load by it to reach the predicted collapse load. A factor of 3 with a
10 kN load means collapse predicted at 30 kN.

Two consequences:

- A buckling study with no load applied has nothing to multiply and
  answers nothing.
- The factor is **not** a factor of safety. Linear buckling is an upper
  bound and a generous one: real columns fail below the Euler load
  because of initial crookedness, eccentric load and material
  yielding. A linear factor of 2 is not a safe column.

## When buckling matters at all

<!-- confidence: verified -->

Compare the Euler load with the squash load:

```
P_euler  = pi^2 * E * I / (K*L)^2
P_yield  = sigma_yield * A
```

Whichever is smaller is the failure mode. For a stocky member the yield
load wins and a static study answers the question. For a slender one --
roughly slenderness above 100 for steel -- buckling wins and a static
study will report a comfortable stress right up to the point the part
folds.

`K` depends on the end conditions: 0.5 fixed-fixed, 0.7 fixed-pinned,
1.0 pinned-pinned, 2.0 fixed-free. The range from 0.5 to 2.0 is a
factor of 16 in critical load, which makes end conditions the single
most consequential modelling choice in the study.

## What these studies actually returned

<!-- confidence: verified -->

Run on SOLIDWORKS 2024 against a 50 × 100 × 2000 mm plain carbon steel
cantilever, clamped at one end:

| quantity | the run | closed form |
|----------|---------|-------------|
| first mode | 10.50 Hz | 10.5 Hz |
| second mode | 20.95 Hz | 21.0 Hz |
| third mode | 65.58 Hz | 65.8 Hz |
| buckling factor, 10 kN along the span | 13.52 | 13.5 (Euler, K = 2) |

The first two modes are bending about the two different axes — the weak
one first — which is worth saying to anyone surprised that a plain beam
has two frequencies so close together. The third is the second bending
mode about the weak axis, at 6.27 times the first, exactly as
`beta_2^2 / beta_1^2` predicts.

The mode count is set through `FrequencyStudyOptions.NoOfFrequencies`.
`ICWStudy` has no `SetFrequencyOptions` method, and a study asked for
three modes returns exactly three.

Each run is now checked automatically against those formulas, with E
and density read from the material library (Plain Carbon Steel: 210 GPa,
7800 kg/m³): first mode 10.50 Hz against 10.48, buckling factor 13.52
against 13.49. Both use the WEAKER axis of the section -- the first mode
and the first buckle both go the easy way.

The mode-shape picture comes from the plot the solve makes by itself
("Amplitude1"): CreatePlot will not make a displacement plot for a
modal study on request.

## Study type codes

<!-- confidence: verified -->

`CreateNewStudy3` takes a code from `swsAnalysisStudyType_e`: static 0,
frequency 1, buckling 2, thermal 3, optimization 4, nonlinear 5, drop
test 6, fatigue 7, linear dynamic 8, pressure vessel 9.

The trap in that list is 4. It is Optimization, not Nonlinear, and a
study created with it takes the same calls and answers something else.
Static, frequency, buckling, thermal and nonlinear have each been run
here with the code above and read back off the created study; the
script still reads the type back and refuses to continue on a mismatch.
