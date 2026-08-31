# CLAUDE.md — Agronome's ODX fork

> **Read this before touching anything in this repo.** It is the operating manual for
> our fork: what we changed, why, what is already known, and which mistakes have
> already been made so they are not made again.
>
> **⚠️ This repository is PUBLIC.** Anyone can read this file. Registry paths, the
> consuming repo, deploy topology and customer data stay in the app repo — see §3.
>
> Companion documents:
> - **`FORK.md`** — the *outside-reader-facing* change document: what this fork changes,
>   why, what each change was measured on, and what we tried and reverted. It is the
>   landing page for anyone reviewing or forking us, and it carries the AGPL §5(a)
>   modification notice. **Update it in the same PR as any behaviour change.**
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
| origin | `git@github.com:Agronome-ai/ODX.git` — **public**, and the default branch is our shipping branch |
| upstream | `https://github.com/WebODM/ODX` |
| **shipping branch** | **`agronome/dji-m3m-multispectral`** — everything we ship |
| `master` | clean upstream mirror, **0 commits ahead**. Keep it that way. |
| merge base | `374db4aa` |
| ODX version at that base | **3.8.2** (`VERSION`) |
| size of the fork | 15 files, ~2,700 lines added |

**Never commit to `master`.** It is the reference we diff against to know what is ours —
and it is what makes `git diff master...HEAD` a complete, honest statement of the fork to
an outside reviewer. That property is now load-bearing for more than us.

### The files we touch

Four upstream source files, one upstream workflow, one upstream README, and our own:

```
opendm/multispectral.py           all radiometry, band alignment, view-angle,
                                  four-band reconstruction, provenance
opendm/photo.py                   five DJI XMP tags + ODM_Photo.is_m3m()
stages/run_opensfm.py             two call sites, the pose-copy guard, a model-level guard
stages/mvstex.py                  selectable seam levelling
.github/workflows/test-build-prs.yml  upstream's; trigger narrowed to master (2 lines)
README.md                         upstream's; a fork banner prepended above it

FORK.md                           the public change document — keep it current
CLAUDE.md                         this file
DJI_M3M_FINDINGS.md               upstream-facing evidence
agro.Dockerfile                   the overlay build
agro_verify.sh                    the applied-assertion (Dockerfile AND CI call it)
agro_test.sh                      ODX suite + known-failure list
.github/workflows/agronome-ci.yml our CI
tests/test_multispectral_view_angle.py
tests/test_multispectral_four_band.py
tests/test_multispectral_provenance.py
```

Note that `opendm/osfm.py` is **not** in this list even though four-band reconstruction
changes what it does. It already calls `get_photos_by_band`; we changed what that function
returns. Keeping the change inside `multispectral.py` is why the overlay is still four
Python files.

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

> 🛑 **`gh pr create` targets UPSTREAM by default here.** Because this repo is a GitHub
> fork, a bare `gh pr create` resolves the base to `WebODM/ODX` — the exact thing the
> rule above forbids. **Always pass `--repo Agronome-ai/ODX` explicitly:**
>
> ```bash
> gh pr create --repo Agronome-ai/ODX --base agronome/dji-m3m-multispectral --head <branch> ...
> ```
>
> It fails loudly today only because our branches share no commits with upstream's tip.
> That is luck, not a safeguard — after an upstream rebase it would succeed.

---

## 3. Build and deploy chain

There are **two images**, and confusing them wastes an afternoon:

| image | what it is |
|---|---|
| `engine-odx` | upstream ODX, **mirrored verbatim** into our AR |
| `engine-odx-agronome` | that mirror **+ this branch**, via `agro.Dockerfile` |

Both live in our internal Artifact Registry. **This repository is public** — the registry
path, the consuming repo, the tag convention and the deploy chain live in the app repo at
`.claude/context/odx-engine-deploy.md`, not here. The `engine-` prefix in the image names
is load-bearing; that file says why.

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

### ⚠️ CI verifies and tests, but does NOT build or push the image

`.github/workflows/agronome-ci.yml` runs the overlay assertion and the ODX test suite
on every PR. **It does not build or push `engine-odx-agronome`** — and it should not.

**Image build and push belong in the app repo, not here.** That is where the GCP
credentials already live, where the digest is consumed, and where the deploy path already
is. **Do not add cloud auth to this repository** — this fork is public and stays a source
repo whose CI needs no credentials at all, which is what keeps it 38 seconds.

Today that build is still run by hand, so a merged fix can sit undeployed. §7 is how to
check.

### 3a. What CI actually runs

Upstream's own CI (`test-build-prs.yml`) builds Docker images and a Windows installer
and **never runs the test suite** — ODX ships seven test files that no pipeline
executes. Ours does:

| job | what | needs |
|---|---|---|
| `overlay` | `agro_verify.sh .` — symbols, call sites, XMP tags, model-level gating, shape guard, no `log.ODM_WARNING`, `py_compile` | nothing. grep + python, seconds |
| `tests` | `agro_test.sh` — ODX's unittest suite inside a **pinned public** `webodm/odx` image | Docker only, no cloud auth |

**`agro_verify.sh` is the single source of the assertion.** `agro.Dockerfile` calls the
same script at image build, so the check cannot drift between CI and the image.

**`agro_test.sh` judges the suite against an explicit `KNOWN_FAILURES` list**, and fails
in *both* directions:

- an unexpected failure → regression
- a known failure that now **passes** → delete it from the list

That second rule is what stops the list becoming somewhere regressions hide. One entry
today, `test_photo.TestPhoto.test_jpeg_xl`, verified pre-existing and **not ours** by
swapping upstream's `photo.py`/`multispectral.py` into the same image and re-running.

Run either locally, exactly as CI does:

```bash
./agro_verify.sh .          # no Docker needed
./agro_test.sh --docker     # pulls the pinned image and runs the suite inside it
```

**We also made one edit to an inherited upstream workflow.** `test-build-prs.yml` ran
three full SuperBuilds (linux, GPU, Windows) on every PR — hours of runner time for
artifacts nobody consumes, since we ship a thin COPY overlay and never build from
source. Its trigger is now restricted to `master`, the branch that tracks upstream
verbatim and where a source-build regression still means something. **Expect a small
conflict there on an upstream rebase** — a 2-line trigger edit was chosen over deleting
the file precisely because it is cheap to re-apply.

### Who consumes it

The app repo's worker image pins `engine-odx-agronome` **by digest** as its base. Bumping
the engine means editing that digest, which is a normal app-repo PR.

> ⚠️ **There is no release gate between merging an engine bump and production.** The
> mechanism, and how to check what is actually running, are in the app repo at
> `.claude/context/odx-engine-deploy.md`.

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

**Radiometric provenance (DD-196, shipped)** — `write_radiometric_summary(...)`,
`RADIOMETRIC_SUMMARY_FILENAME = "agronome_radiometric.json"`

The gate above used to decide in silence. It now writes a sidecar at the project root —
inside the checkpoint mirror, so a **resumed** run whose opensfm stage never executes still
reports what its earlier attempt measured, exactly as `agronome_fragmentation.json` does.

Three properties are structural, asserted in `agro_verify.sh`, and each is a way this has
already failed:

- **The write is in a `finally`.** The body has four early returns and all four are
  *refusals* — the records that matter most and the ones a per-return call site forgets.
  The summary is seeded as a refusal with reason `"unknown"` so even an escaping exception
  lands a truthful "not applied".
- **An ABSENT file means "never a candidate", never "fine".** A non-M3M flight never
  reaches this code. Collapsing that with a present `applied=false` is how the skip stayed
  invisible in the first place.
- **Floats are sanitised** (`_finite_or_none`). `json.dump` writes bare `NaN`, which Python
  reads back happily and every strict parser rejects — and a control correlation is
  genuinely `NaN` on a degenerate profile, so the sidecar would have failed to parse on
  exactly the flights worth inspecting.

`_frame_signs` returns the flight axis it previously only logged: it is the one number that
explains a ramp verdict after the fact, since the correction only has something to find
when the across-swath axis lies near the solar principal plane.

**Four-band reconstruction** — `get_photos_by_band(multi_camera, user_band_name)`

Upstream reconstructs from **one** primary band and copies its pose onto the other three,
so three quarters of the imagery contributes nothing to the geometry. For an M3M this now
returns every band's photos. Track length rose 23–71% across three flights; two-view tracks
fell from 64.8% to 38.1% on the weakest.

Its single caller is `OSFMContext.setup` in `opendm/osfm.py`, which writes whatever comes
back into `image_list.txt` — **which is why no file outside this fork needed to change.**
Do not "clean this up" by moving the branch into `osfm.py`; that widens the overlay from
four files to five for no gain.

The per-band NVM `img_map` covers every image in the reconstruction, not just primary ones
— `replace_nvm_images` refuses a partial mapping, so with all bands present the export died
with *"not all band images have been matched"*. It degrades exactly to upstream behaviour
when only primary shots are present.

Band alignment is deliberately **left in place**. The bands should now land correctly from
their own geometry, but that is a claim to verify before removing a correction that
demonstrably fixed registration.

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

Plus the four-band pose-copy guard: `octx.add_shots_to_reconstruction(p2s)` is **skipped**
when every band was solved. That function exists to *give* the secondary bands a pose they
never computed; running it after a four-band solve would discard four real poses and
replace them with one band's.

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

- **12 symbols** exist in `multispectral.py`, plus `photo.is_m3m` (run the script for the
  live list rather than trusting a count written down here)
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

## 7. Is my change deployed? — derive it, do not trust this file

**Nothing here states a current digest on purpose.** A hardcoded snapshot in an
auto-loaded file goes stale silently and then actively misinforms, which is the same
class of failure as everything else in §6. Derive it:

```bash
# what the fork carries
git log --oneline origin/agronome/dji-m3m-multispectral -3
```

Then read what is actually deployed. The image tag encodes **both** parents
(`v<odx>-<upstream>-m3m-<fork>`), so the `m3m-<sha>` suffix answers it directly —
`git log` forward from that sha and anything listed is not in the image. **The registry
path and the exact commands are in the app repo at
`.claude/context/odx-engine-deploy.md`**, deliberately not in this public repo.

**This gap is real and recurring, because no CI builds the engine image** (see §3). To
close it: build `agro.Dockerfile`, push with a two-parent tag, bump the digest in the app
repo's worker Dockerfile. Worth doing the moment a fix merges — a merged fix that nobody
builds is indistinguishable from an unfixed bug in production.

### Owed

- **The engine image is still built and pushed by hand.** CI here verifies and tests
  (§3a) but deliberately does not build or push — that belongs in the app repo, which
  already holds the GCP credentials and consumes the digest. Automating it is app-repo
  work; **nothing is owed in this repository.**
- **Large flights are untested.** Every measurement behind this fork comes from
  ~300-capture flights. `--matcher-neighbors 0` should scale *better* than a tuned cap,
  since it uses graph rounds rather than N×64 GPS pairs — but that is reasoning, not a
  measurement.
- **Threshold calibration is still deferred, but no longer blocked.** DD-196 shipped
  (`b106d151`), so every M3M flight now leaves an `agronome_radiometric.json` recording
  what the gate decided and on what evidence. What is owed is the *aggregation*: the gate
  refused 2 of 3 re-driven flights, and one (Green control 0.62 against a 0.80 limit) is
  near enough the line to be worth understanding, but setting thresholds needs a corpus of
  sidecars, not three. **Do not touch the constants until that corpus exists.**
- **One reconstruction feeding both orthos.** The DD-200 revert (`b9f84514`) established
  that the multispectral and RGB orthos come from two *independent* reconstructions, so
  improving one in isolation moves them apart — MS↔RGB went 0.15 m → 0.62 m on a flat
  flight. The RGB frames are already in the multispectral dataset (885 usable = 177
  captures × 5 surfaces), just excluded from the reconstruction. Doing that would make
  them unable to disagree by construction. Needs its own DD; **do not re-land per-band
  camera models without it.**
- **Band alignment may now be redundant with four-band reconstruction.** Both correct
  registration and both currently run. Unverified either way — measure before removing.
- **`_M3M_MODELS` is a one-entry set standing in for a camera registry.** Every new DJI
  multispectral model needs a deliberate decision, not an automatic inclusion.
- **`FORK.md` must not go stale.** It is the public face of this fork and the AGPL §5(a)
  notice. This file went five commits stale before anyone noticed; `FORK.md` going stale
  is worse, because outside readers cannot check it against the code the way we can.

---

## 8. Working here

```bash
git checkout -b <type>/<slug> origin/agronome/dji-m3m-multispectral   # never master
./agro_verify.sh .                       # the overlay assertion — seconds, no Docker
./agro_test.sh --docker                  # the full ODX suite vs the known-failure list
./test.sh multispectral_view_angle       # one file; unittest discovery, NOT pytest
docker build -f agro.Dockerfile -t <tag> .   # assertion runs again inside the image
```

`test.sh` expands its argument to `tests/test_<arg>.py` and runs
`python3 -m unittest discover`. Match that style — a `pytest`-only test will not be
discovered by the repo's own runner and will look like it passed by never running.

`agro_verify.sh` is the cheapest real check you have — no Docker, no auth, seconds, and
it runs identically in CI and at image build. Run it before claiming anything ships.

**When you add a correction, add it to `agro_verify.sh`.** An unasserted correction is
one refactor away from being silently absent — which is the exact failure in §5.

Three test files, and they share one shape worth copying:

- `test_multispectral_view_angle.py` — the shape guard's four cases (matching frames
  contribute, a mismatched frame is skipped rather than raising, all frames mismatched
  yields no profile, an unreadable frame is still skipped).
- `test_multispectral_four_band.py` — the band selector and the pose-copy guard.
- `test_multispectral_provenance.py` — every exit path *including the exception one*, that
  the written JSON is strictly parseable, and that a refusal carries the evidence a later
  recalibration will need.

Follow that shape for new work: **assert the degenerate input does not silently pass**,
because "returned nothing" and "verified clean" being indistinguishable is the failure mode
this whole fork keeps rediscovering.

---

When you change anything here, update **this file and `FORK.md`** in the same PR. This
repository is public and its default branch is the shipping branch, so a stale document is
read by people who cannot check it against a deploy.
