# CLAUDE.md — Agronome's ODX fork

> **Read this before touching anything in this repo.** It is the operating manual for
> our fork: what we changed, why, what is already known, and which mistakes have
> already been made so they are not made again.
>
> Companion documents:
> - **`DJI_M3M_FINDINGS.md`** — the *upstream-facing* evidence write-up (measurements,
>   controls, refuted hypotheses). Written for ODX/ODM maintainers. **Read it before
>   proposing any change to matching, seam levelling or compositing** — it will very
>   often already contain the measurement.
> - **`AGENTS.md`** — **upstream's** policy file, inherited from the merge base. See
>   "Upstream etiquette" below; it constrains what you may do *to upstream*, not here.
> - In the app repo: `.claude/design-docs/DD-171-ms-view-consistency.md` (the campaign),
>   `.claude/context/ms-mosaic-quality.md` (settings), `.claude/context/dd171-retro.md`
>   (what was refuted), `.claude/tools/dd171/` (the instruments).

---

## 1. What this repo is

A fork of [WebODM/ODX](https://github.com/WebODM/ODX) carrying **DJI Mavic 3
Multispectral (M3M) radiometric and band-handling corrections** that upstream does not
have. It exists because those corrections are camera-specific and ODX has no camera
registry to put them in (`DJI_M3M_FINDINGS.md` §6).

| | |
|---|---|
| origin | `git@github.com:Agronome-ai/ODX.git` |
| upstream | `https://github.com/WebODM/ODX` |
| **shipping branch** | **`agronome/dji-m3m-multispectral`** — everything we ship |
| `master` | clean upstream mirror, **0 commits ahead**. Keep it that way. |
| merge base | `374db4aa` |
| ODX version at that base | **3.8.2** (`VERSION`) |
| size of the fork | 7 files, ~1,400 lines added |

**Never commit to `master`.** It is the reference we diff against to know what is ours.

### The seven files we touch

```
opendm/multispectral.py    +753   all radiometry, band alignment, view-angle work
opendm/photo.py             +72   five DJI XMP tags + ODM_Photo.is_m3m()
stages/run_opensfm.py       +29   two call sites + a model-level guard
stages/mvstex.py            +43   selectable seam levelling
agro.Dockerfile             +67   the overlay build and its applied-assertion
DJI_M3M_FINDINGS.md        +358   upstream-facing evidence
tests/test_multispectral_view_angle.py  +88
```

Anything outside those files is upstream's. If a change wants to live elsewhere, stop
and ask whether it belongs in the app's worker instead — the smaller this fork stays,
the cheaper every upstream rebase is.

---

## 2. 🛑 Hard rules

1. **Never `FROM webodm/odx:latest`.** ODX is a rolling release with no version tags,
   and Docker Hub prunes unpulled images. `agro.Dockerfile` builds `FROM` our own
   mirror **pinned by digest**. A floating tag makes the build unreproducible and
   eventually unbuildable.
2. **Every correction is gated on camera MODEL, never make.** `_is_m3m(photo)` requires
   `make == "DJI"` *and* `model in _M3M_MODELS`. A make-level guard hands M3M
   coefficients to an M4E and produces a fully-formed, silently wrong product. The
   build assertion fails if a `camera_make == "DJI"` guard reappears.
3. **`log.ODM_WARNING` does not exist in ODX.** It is `log.WARNING` / `log.INFO`. The
   name is muscle memory from other codebases and will `AttributeError` at runtime, on
   the error path — the one place you find out last. Asserted at build time.
4. **A symbol is not a shipped correction.** See §5. The build proves symbol **and call
   site**; keep it that way when you add anything.
5. **Do not delete `odm_patches/` handling in the app repo and assume it is inert.**
   Removing those patch steps is *load-bearing*: `wire_dji_radiometry` rebinds
   `multispectral`'s functions to `dji_radiometry`'s copies and would silently override
   this fork with a partial port.

### Upstream etiquette

`AGENTS.md` at the repo root is **upstream ODX's** file, inherited at the merge base.
It states that agents must not automatically open pull requests or issues **against
ODX**, and that contributions must be manually written and must disclose AI use.

Respect it for anything aimed at `WebODM/ODX`: do not open PRs or issues there. If we
want to contribute something back, hand `DJI_M3M_FINDINGS.md` to a human to submit in
their own words.

It does **not** govern our own fork. PRs inside `Agronome-ai/ODX` are normal and
expected — PR #1 is how the shape guard landed.

---

## 3. Build and deploy chain

There are **two images**, and confusing them wastes an afternoon:

| image | what it is |
|---|---|
| `engine-odx` | upstream ODX, **mirrored verbatim** into our AR |
| `engine-odx-agronome` | that mirror **+ this branch**, via `agro.Dockerfile` |

Both live in `northamerica-northeast2-docker.pkg.dev/agronome-shared-services/agronome/`.
The `engine-` prefix is deliberate — the AR cost-cleanup rule excludes it.

**The overlay is a thin `COPY` of four Python files, not a source build.** Every change
on this branch is pure Python under `opendm/` and `stages/`, so there is nothing to
recompile; a source build would be hours of SuperBuild for four files.

That is safe **because it was verified, not assumed**: at the time it was written, all
four files inside the mirrored image were byte-identical to this branch's merge base,
and upstream had changed none of them in between. **If you re-mirror a newer upstream,
re-verify that.** The applied-assertion is what keeps it honest as upstream moves.

### Image tag convention — it encodes both parents

```
v<odx-version>-<upstream-sha>-m3m-<fork-sha>
e.g.   v3.8.2-0f3864ea-m3m-0cdec216
```

**This is the fastest way to answer "is my change deployed?"** Read the tag, compare
`<fork-sha>` against this branch's log.

### ⚠️ Nothing builds this image automatically

There is **no CI workflow** in either repo that builds or pushes
`engine-odx-agronome`. It is a manual `docker build -f agro.Dockerfile` + push. That
is precisely why a merged fix can sit undeployed — see §7.

### Who consumes it

`app-meridian/apps/worker/Dockerfile` pins `engine-odx-agronome` **by digest** as its
base. Bumping the engine means editing that digest, which is a normal app-repo PR.

> ⚠️ **Worker images reach production on merge, not on a `v*` tag.** Production pulls
> `:latest`, which the staging deploy overwrites. An engine bump is live in prod as
> soon as the app PR merges — there is no release gate in front of it.

---

## 4. What we changed, and where it is called from

### `opendm/multispectral.py`

**Camera gate.**
```python
_M3M_MODELS = {"M3M"}
def _is_m3m(photo) -> bool     # make == DJI AND model in _M3M_MODELS
```
Deliberately defensive about both attributes: a photo with no model must not fall
through, because falling through means "treat an unknown camera as an M3M".

**DLS irradiance normalisation** — `prepare_dji_irradiance(photos)`
Called once per run from `stages/run_opensfm.py:152`, before the undistort pipeline.
Builds a module-global `_dji_irradiance_map` keyed by filename: a **7-frame rolling
median** of `drone-dji:Irradiance` per band, clamped to 0.2×–5× the band median. Only
for M3M photos that have `spectral_irradiance` and no `horizontal_irradiance`.

Consumed by `compute_irradiance` / `dn_to_reflectance`, both of which branch on
`_is_m3m`.

**Vignette (Eq. 7)** — `vignette_map(photo)`
Note the DJI-specific branch, which is easy to "fix" wrongly:
```python
if photo.camera_make != "DJI":
    vignette = 1.0 / vignette
```
DJI's polynomial is already in the correction sense; inverting it doubles the error
instead of removing it.

**Factory band alignment** — `dji_hmatrix(photo)`, `dji_band_warp(...)`
Uses DJI's `drone-dji:CalibratedHMatrix` instead of estimating a homography.
`compute_homography` (line ~1053) tries the factory matrix first and falls back to ECC.
`ECC_MAX_SIZE_NATIVE = 4096` — the fallback used to solve at half resolution, which
quantised the result; the alignment is now solved at native resolution.

**View-angle normalisation and the across-swath ramp** — `normalize_view_angle(...)`
The largest and most delicate piece. Called from `stages/run_opensfm.py:213`.

```
_view_angle_profile(...)          radial profile, binned by normalised image radius
_frame_signs(multi_camera)        per-frame heading sign, from FlightX/YSpeed
_ramp_profile_groundfree(...)     across-swath profile from SfM track pairs
_ramp_profile(...)                across-swath profile from pixels (fallback)
```

Constants, all of them thresholds someone will eventually want to tune:
```python
VIEW_ANGLE_BINS = 32          RAMP_BINS = 16
VIEW_ANGLE_MIN_FRAMES = 30    RAMP_MIN_CORR = 0.7      # two disjoint halves must agree
VIEW_ANGLE_MIN_CORR = 0.8     RAMP_MAX_AMPLITUDE = 0.9
VIEW_ANGLE_MAX_DISAGREE = 0.15
VIEW_ANGLE_CLAMP = (0.6, 1.6) RAMP_MIN_SPEED = 1.5     # m/s, below which heading is meaningless
```

Two properties worth not breaking:

- **All-or-nothing.** It measures and gates *every* band before touching *any* of them.
  Correcting three bands and skipping the fourth changes band ratios — NDVI would move
  for a reason that has nothing to do with the field.
- **The control is the point.** These corrections are *estimated from the flight's own
  imagery*, so they are confounded with the field's real spatial gradient. The profile
  is measured on two disjoint halves and applied only if they agree. On one real flight
  the halves measured **opposite** profiles (r = −0.88); applying either would have
  flattened real agronomy and looked completely plausible. **Do not "improve" the
  acceptance rate by loosening the gate.**

### `opendm/photo.py`

Five XMP tags parsed that upstream ignores — `CalibratedOpticalCenterX/Y`,
`CalibratedHMatrix`, `FlightXSpeed`, `FlightYSpeed`, and `drone-dji:BlackLevel` (the
M3M stores black level only in the DJI namespace).

`ODM_Photo.is_m3m()` is duplicated here rather than imported, deliberately, so
`photo.py` keeps no import of `multispectral`. Used at `photo.py:769` in
`get_vignetting_center`.

### `stages/run_opensfm.py`

Two call sites, and **the order of the second one is load-bearing**:

```
:152   multispectral.prepare_dji_irradiance(photos)
:213   multispectral.normalize_view_angle(...)      <- AFTER the primary-band pass
```

`normalize_view_angle` must run **after** the primary undistort pass. The first
`convert_and_undistort` writes only the *secondary* bands. Called any earlier, the
primary band has zero undistorted frames on disk, the all-or-nothing gate correctly
refuses to correct anything, and the correction silently disables itself on **every**
M3M flight — Green is the primary band there.

### `stages/mvstex.py`

Seam levelling became selectable instead of hard-off, via `ODX_SEAM_LEVELING`:

| value | effect |
|---|---|
| `off` (default) | both levelings skipped |
| `local` | local (Poisson border) only |
| `global` | global (per-vertex) only |
| `full` | both, as upstream does for RGB |

Only applies when the reconstruction is multi-camera **and** M3M. Unknown values warn
and fall back to `off`.

Why off by default: mvs-texturing levels **per texture patch, per band, independently**,
which on multispectral manufactures a flight-line "quilt". Measured, and rejected on
sight by the operator. `DJI_M3M_FINDINGS.md` §2 has the detail.

---

## 5. The applied-assertion, and the failure it exists for

`agro.Dockerfile` ends in a `RUN` that fails the build unless the overlay is really
present. **This is not belt-and-braces; it is a scar.**

The view-angle and across-swath ramp corrections once reached the deployed engine as
**constants and roughly sixty lines of rationale with no implementing function**. The
module read as though the correction were running. Every measurement taken "with the
correction on" had in fact been taken with it off, and no instrument could have caught
it — they all measure the *output*, and the output was perfectly self-consistent.

Magnitude once it actually ran: **0.024 NDVI** peak-to-peak across a 26 m swath,
against the **0.0032** that blending removes. Blending cannot suppress it — the
gradient is heading-signed and systematic, so averaging N views gives no 1/√N.

Every mechanism in that chain fails *quietly*: a `COPY` of the wrong file succeeds, a
text-anchored patch that matches nothing exits 0, and the build goes green with the
engine unpatched.

So the assertion checks:

- **8 symbols** exist in `multispectral.py`, plus `photo.is_m3m`
- **2 call sites** exist in `run_opensfm.py` — *a function that exists but is never
  invoked is exactly the bug above, and a symbol-only check would have called it
  shipped*
- **5 XMP tags** are parsed in `photo.py`
- the `ODX_SEAM_LEVELING` switch is present
- the `_view_angle_profile` shape guard is present
- **no** make-level `camera_make == "DJI"` guard survives in `mvstex.py` /
  `run_opensfm.py`
- **no** `log.ODM_WARNING` anywhere
- all four files `py_compile`

**When you add a correction, add it to the assertion.** An unasserted correction is one
refactor away from being silently absent.

---

## 6. Traps already paid for

- **Verify the deployed artifact, not the repository.** Read the running job's
  `imageUri`, pull *that digest*, and grep for the symbol **and its call site**. This
  had to be learned three times: once by inspecting `odm-worker:latest` when the job had
  actually run `odx-worker:latest`, and once by reading a working tree 84 commits stale
  that showed one patch file where main had four.
- **`--matcher-neighbors N` silently disables graph matching.** `opendm/osfm.py:238` —
  passing *any* value sets `matcher_graph_rounds = 0`. The default (`0`) is not "no
  neighbours", it is a different and better strategy. We shipped `16` for months and it
  cost roughly half our reconstruction quality. The round count differs by engine: **20
  in ODX 3.8.2, 50 in ODM 3.6.0** — quote it per version or not at all.
- **`--matcher-order` is a no-op on georeferenced data.** ODM says so in its own log.
  Every drone dataset is georeferenced.
- **Order-dependent crashes hide behind the all-or-nothing gate.** `normalize_view_angle`
  returns on the *first* band that fails its control, so a defect in a later band only
  reproduces when band order happens to put it first. That is why the same flight
  processed cleanly several times and then died (TuplinSkips, job `9ca82421`,
  2026-08-16). If something is intermittent here, suspect ordering before suspecting
  data.
- **Do not trust a metric that has not been perturbed.** Four instruments in this
  campaign gave confident wrong answers, including one that *improved* when the bands
  were deliberately misregistered. `dd171-retro.md` §3 lists all four. Every instrument
  in `.claude/tools/dd171/` carries a control for this reason.
- **`hi-freq%` is monotone in blending** — a Gaussian blur scores perfectly. It is a
  valid spikiness metric and a worthless blend-vs-best-view metric.
- **Never compare two pipelines without solving alignment locally.** A fixed global
  shift was wrong by up to 1.75 m in places, and an unremoved 1.6 m registration offset
  is enough to make two identical products look unrelated.

---

## 7. State as of 2026-08-16

**⚠️ The branch is ahead of the deployed image.**

| | |
|---|---|
| branch tip | `3bc90641` (merge of `cbb203f4`, the `_view_angle_profile` shape guard) |
| deployed image | `sha256:04d780fa…`, tag `v3.8.2-0f3864ea-m3m-0cdec216`, built 2026-08-14 |
| **not in any image** | `cbb203f4` — the fix for the crash that killed job `9ca82421` |

Nothing builds the image automatically, so this gap does not close by itself. To ship
it: build `agro.Dockerfile`, push with a tag encoding both parents, then bump the
digest in `app-meridian/apps/worker/Dockerfile`.

### Owed

- **Large flights are untested.** Every measurement behind this fork comes from
  ~300-capture flights. `--matcher-neighbors 0` should scale *better* than a tuned cap,
  since it uses graph rounds rather than N×64 GPS pairs — but that is reasoning, not a
  measurement.
- **The radiometric gate records nothing.** It decides per flight, and only Cloud
  Logging knows. Tracked in the app repo as **DD-196**.
- **Threshold calibration is deferred and depends on DD-196 shipping first.** The gate
  refused 2 of 3 re-driven flights. One (Green control 0.62 against a 0.80 limit) is
  near enough the line to be worth understanding; the other (−0.88) is not. There is no
  dataset to calibrate against until the outcome is recorded.
- **`_M3M_MODELS` is a one-entry set standing in for a camera registry.** Every new DJI
  multispectral model needs a deliberate decision, not an automatic inclusion.

---

## 8. Working here

```bash
git checkout -b <type>/<slug> origin/agronome/dji-m3m-multispectral   # never master
./test.sh multispectral_view_angle       # ODX uses unittest discovery, NOT pytest
docker build -f agro.Dockerfile -t <tag> .                            # assertion runs here
```

`test.sh` expands its argument to `tests/test_<arg>.py` and runs
`python3 -m unittest discover`. Match that style — a `pytest`-only test will not be
discovered by the repo's own runner and will look like it passed by never running.

The build assertion is the cheapest real test you have — it runs on every image build
and fails loudly. Run a build before claiming a change ships.

The tests in `tests/test_multispectral_view_angle.py` cover the shape guard's four
cases (matching frames contribute, mismatched frame is skipped rather than raising, all
frames mismatched yields no profile, unreadable frame still skipped). Follow that shape
for new work: **assert the degenerate input does not silently pass**, because "returned
nothing" and "verified clean" being indistinguishable is the failure mode this whole
fork keeps rediscovering.

When you change anything here, update this file in the same PR.
