# ODX — Agronome fork

**What this is:** a fork of [WebODM/ODX](https://github.com/WebODM/ODX) carrying DJI Mavic 3
Multispectral (M3M) radiometric and band-handling corrections that upstream does not have.

**Why it exists as a fork rather than a patch upstream:** every correction here is
*camera-specific*, and ODX has no camera registry to put camera-specific behaviour in.
Each correction is therefore gated on the camera model at its call site, which is a shape
upstream would reasonably not want in the general engine. See §8 and
[`DJI_M3M_FINDINGS.md`](DJI_M3M_FINDINGS.md) §6.

We run this in production on our own multispectral pipeline. It is published so it can be
forked, reviewed, and argued with — not as a supported product.

---

## 1. Provenance

| | |
|---|---|
| upstream | `https://github.com/WebODM/ODX` |
| merge base | [`374db4aa`](https://github.com/WebODM/ODX/commit/374db4aa) ("Fix OpenSfM credits", 2026-07-30) |
| ODX version at that base | **3.8.2** (`VERSION`) |
| shipping branch | **`agronome/dji-m3m-multispectral`** — the default branch of this repo |
| `master` | a **verbatim mirror of upstream**, deliberately 0 commits ahead |
| size | 15 files, ~2,700 lines added |
| modified | 2026-08-01 – 2026-08-19 |

`master` being a clean mirror is what makes the fork legible: `git diff master...HEAD` is
*exactly* what we changed and nothing else. Please keep that property if you contribute.

### License notice

ODX is licensed under the **GNU Affero General Public License v3.0**, and so is this fork.

In satisfaction of AGPL-3.0 §5(a): the following files have been **modified by Agronome AI
Inc. between 2026-08-01 and 2026-08-19**, relative to upstream commit `374db4aa`:

```
opendm/multispectral.py
opendm/photo.py
stages/run_opensfm.py
stages/mvstex.py
.github/workflows/test-build-prs.yml
README.md
```

These files are new in this fork and are not upstream's:

```
FORK.md  CLAUDE.md  DJI_M3M_FINDINGS.md
agro.Dockerfile  agro_verify.sh  agro_test.sh
.github/workflows/agronome-ci.yml
tests/test_multispectral_view_angle.py
tests/test_multispectral_four_band.py
tests/test_multispectral_provenance.py
```

Anything not listed above is upstream's, unmodified.

---

## 2. How to review this fork

```bash
git clone https://github.com/Agronome-ai/ODX.git && cd ODX

git diff master...HEAD --stat      # everything we changed, and nothing else
git diff master...HEAD -- opendm/multispectral.py   # the bulk of it

./agro_verify.sh .                 # the applied-assertion. seconds, no Docker, no auth
./agro_test.sh --docker            # ODX's own suite, in a pinned public webodm/odx image
```

Commit messages are the primary record. Each one states the measurement that motivated
the change, the flights it was measured on, and — where relevant — what was refuted. If
you are evaluating whether a change is sound, `git log master..HEAD` is denser than this
document.

---

## 3. What changed

Everything below is gated on **camera model**, never make. `_is_m3m(photo)` requires
`make == "DJI"` **and** `model in _M3M_MODELS`. A make-level guard would hand M3M
coefficients to a Mavic 3 Enterprise and produce a fully-formed, silently wrong product.
`agro_verify.sh` fails the build if a make-level guard reappears.

### 3.1 Radiometry — `opendm/multispectral.py`

**DLS irradiance normalisation** (`prepare_dji_irradiance`)
The M3M's downwelling light sensor reports `drone-dji:Irradiance` per frame, but no
`horizontal_irradiance`, so upstream's MicaSense-shaped path does not apply. We build a
7-frame rolling median per band, clamped to 0.2×–5× the band median, keyed by filename,
and consume it from `compute_irradiance` / `dn_to_reflectance`. Called once per run from
`stages/run_opensfm.py`, before the undistort pipeline.

**Vignette sense** (`vignette_map`)
DJI's vignette polynomial is already expressed in the *correction* sense, unlike the
vendors upstream handles. The DJI branch therefore does **not** invert it:

```python
if photo.camera_make != "DJI":
    vignette = 1.0 / vignette
```

Inverting it doubles the error instead of removing it. This looks like a bug and is not.

**Black level from the DJI namespace** — the M3M stores black level only in
`drone-dji:BlackLevel`; the standard tag is absent.

### 3.2 Band alignment — `opendm/multispectral.py`, `opendm/photo.py`

**Use the factory homography** (`dji_hmatrix`, `dji_band_warp`)
DJI ships `drone-dji:CalibratedHMatrix` per band. `compute_homography` now tries the
factory matrix first and falls back to ECC estimation. Five XMP tags are parsed in
`photo.py` that upstream ignores: `CalibratedHMatrix`, `CalibratedOpticalCenterX/Y`,
`FlightXSpeed`, `FlightYSpeed`, plus the DJI-namespace black level.

**Solve the ECC fallback at native resolution** (`ECC_MAX_SIZE_NATIVE = 4096`)
The fallback previously solved at half resolution, which quantised the result.

### 3.3 View-angle and across-swath normalisation — `opendm/multispectral.py`

The largest and most delicate piece (`normalize_view_angle`), called from
`stages/run_opensfm.py` **after** the primary-band undistort pass.

```
_view_angle_profile(...)      radial profile, binned by normalised image radius
_frame_signs(multi_camera)    per-frame heading sign, from FlightX/YSpeed
_ramp_profile_groundfree(...) across-swath profile from SfM track pairs
_ramp_profile(...)            across-swath profile from pixels (fallback)
```

Two properties are load-bearing:

- **All-or-nothing.** It measures and gates *every* band before touching *any* of them.
  Correcting three bands and skipping the fourth changes band ratios — NDVI would move
  for a reason that has nothing to do with the field.
- **The control is the point.** These corrections are estimated from the flight's own
  imagery, so they are confounded with the field's real spatial gradient. The profile is
  measured on **two disjoint halves** and applied only if they agree
  (`VIEW_ANGLE_MIN_CORR = 0.8`). On one real flight the halves measured *opposite*
  profiles (r = −0.88); applying either would have flattened real agronomy and looked
  entirely plausible.

  **Do not raise the acceptance rate by loosening the gate.** The gate refusing is the
  feature.

Magnitude of what it corrects when it does apply: **0.024 NDVI** peak-to-peak across a
26 m swath, against the **0.0032** that view blending removes. Blending cannot suppress
it — the gradient is heading-signed and systematic, so averaging N views gives no 1/√N.

**Ordering is load-bearing.** `normalize_view_angle` must run *after* the primary
undistort pass. The first `convert_and_undistort` writes only the *secondary* bands;
called any earlier, the primary band has zero undistorted frames on disk, the
all-or-nothing gate correctly refuses, and the correction silently disables itself on
**every** M3M flight — Green is the primary band there.

### 3.4 Radiometric provenance — `agronome_radiometric.json`

The gate above works, but originally decided in silence. Across three flights re-driven
on 2026-08-14/15, exactly one was corrected and nothing outside logs recorded which.

`normalize_view_angle` now writes a sidecar at the project root recording, per band: the
control correlation, edge disagreement, ramp amplitude and correlation, the flight axis,
and the verdict. Three properties are asserted at build time because each is a way this
has already failed:

- **The write happens in a `finally`.** The body has four early returns and all four are
  *refusals* — the records that matter most, and the ones a per-return call site forgets.
  The summary is seeded as a refusal with reason `"unknown"`, so even an escaping
  exception lands a truthful "not applied".
- **An absent file means "never a candidate", never "fine".** A non-M3M flight never
  reaches this code. That is deliberately distinct from a present file saying
  `applied=false`.
- **Floats are sanitised before serialising.** `json.dump` writes bare `NaN`, which Python
  reads back happily and every strict parser rejects — and a control correlation is
  genuinely `NaN` on a degenerate profile, so the sidecar would have failed to parse on
  exactly the flights worth inspecting.

The sidecar sits inside the checkpoint mirror, so a resumed run — whose opensfm stage does
not execute at all — still reports what its earlier attempt measured.

### 3.5 Four-band reconstruction — `opendm/multispectral.py`, `stages/run_opensfm.py`

Upstream reconstructs from **one** primary band and copies that band's solved pose onto
the other three (`add_shots_to_reconstruction`), so three quarters of the imagery
contributes nothing to the geometry — the bands are treated as a zero-baseline rig by
assignment rather than by evidence.

Measured across three flights (60 mid-flight captures each, production OpenSfM config,
one variable):

| flight | track length, 1 band → 4 | two-view tracks |
|---|---|---|
| TuplinSkips | 2.66 → 4.56 (**+71%**) | 64.8% → 38.1% |
| Zuppan | 3.91 → 4.80 (**+23%**) | 38.0% → 38.9% |
| Naufrage | 4.45 → 5.52 (**+24%**) | 33.3% → 33.8% |

Where the primary band is weak, four bands *repairs* the geometry; where it is healthy, it
*deepens* it. Three changes implement it:

- `get_photos_by_band` returns every band's photos for an M3M. Its single caller
  (`OSFMContext.setup`) writes whatever comes back into `image_list.txt`, so **no file
  outside this fork needs to change**.
- `add_shots_to_reconstruction` is skipped when every band was solved. That function
  exists to *give* the secondary bands a pose they never computed; running it now would
  discard four real poses and replace them with one band's.
- The per-band NVM `img_map` covers every image in the reconstruction, not just the
  primary ones. `replace_nvm_images` refuses a partial mapping, so with all bands present
  the export died with *"not all band images have been matched"*. This degrades exactly to
  upstream behaviour when only primary shots are present.

End-to-end on 40 TuplinSkips captures — the flight that shipped a blank NDVI —
multispectral ortho 100% valid pixels (was 41%), NDVI p5 0.66 (was 0.16), DSM span 16.2 m
(was 889.6 m) on ground the aircraft's GPS measured flat to 10 cm.

Band alignment (§3.2) is deliberately left in place. The bands should now land correctly
from their own geometry, but that is a claim to verify before removing a correction that
demonstrably fixed registration.

### 3.6 Seam levelling made selectable — `stages/mvstex.py`

Upstream always applies mvs-texturing's seam levelling. On multispectral it levels **per
texture patch, per band, independently**, which manufactures a flight-line "quilt" — see
`DJI_M3M_FINDINGS.md` §2. This fork makes it selectable via `ODX_SEAM_LEVELING`, and only
when the reconstruction is multi-camera **and** M3M:

| value | effect |
|---|---|
| `off` (default) | both levelings skipped |
| `local` | local (Poisson border) only |
| `global` | global (per-vertex) only |
| `full` | both, as upstream does for RGB |

Unknown values warn and fall back to `off`. RGB reconstructions are untouched.

### 3.7 CI — `.github/workflows/`

Upstream's own CI builds Docker images and a Windows installer and **never runs the test
suite**; ODX ships seven test files that no pipeline executes. Ours runs both an assertion
job and the actual suite, needs no cloud credentials, and finishes in well under a minute.

- `agro_verify.sh` — symbols, **call sites**, XMP tags, model-level gating, shape guard,
  four-band selector, provenance properties, no `log.ODM_WARNING`, `py_compile`.
- `agro_test.sh` — ODX's unittest suite inside a **pinned public** `webodm/odx` image,
  judged against an explicit `KNOWN_FAILURES` list that fails in *both* directions: an
  unexpected failure is a regression, and a known failure that now passes must be deleted
  from the list. One entry today (`test_photo.TestPhoto.test_jpeg_xl`), verified
  pre-existing and not ours by swapping upstream's files into the same image.

We also restricted upstream's inherited `test-build-prs.yml` to fire on `master` only. It
ran three full SuperBuilds per PR — hours of runner time for artifacts nobody consumes,
since we ship a thin `COPY` overlay and never build from source. It still runs on `master`,
the branch where a source-build regression means something. Expect a small conflict there
on rebase; a 2-line trigger edit was chosen over deleting the file precisely because it is
cheap to re-apply.

---

## 4. Why there is an applied-assertion

`agro_verify.sh` fails if the corrections are not really present. **This is a scar, not
belt-and-braces.**

The view-angle and across-swath corrections once reached a deployed engine as *constants
and roughly sixty lines of rationale with no implementing function*. The module read as
though the correction were running. Every measurement taken "with the correction on" had
in fact been taken with it off, and no instrument could have caught it — they all measure
the *output*, and the output was perfectly self-consistent.

Every mechanism in that chain fails quietly: a `COPY` of the wrong file succeeds, a
text-anchored patch that matches nothing exits 0, and the build goes green with the engine
unpatched.

So the assertion checks **symbols and call sites**. A function that exists and is never
invoked is exactly the bug above, and a symbol-only check would have called it shipped —
which it later did again, on the DD-200 work in §5.

**If you add a correction here, add it to the assertion.** An unasserted correction is one
refactor away from being silently absent.

---

## 5. What we tried and reverted

Recorded because a reviewer's first instinct will be to try it.

**Per-band camera models (DD-200) — implemented, measured, reverted.**

An M3M is four physically different lenses in one head. `camera_id` is
make + model + width + height + projection + focal_ratio, and every band reports
byte-identical values for all six — so all four bands collapsed into **one** camera model
and the reconstruction fit a single lens to four different lenses. Splitting them (with a
`brown` projection, since `perspective` has no principal-point field and the offset
between bands reaches 31 px) cut disagreement with a PPK track by 18–79%, six of six
measurements improved, at no runtime cost.

It was reverted anyway. The multispectral and RGB orthos come from two **independent**
reconstructions. The change moved the multispectral one 0.27 m closer to GPS truth and
left the RGB pass exactly where it was — so the two products that have to *agree* moved
apart: MS↔RGB registration went 0.15 m → 0.62 m on a flat flight with no pre-existing
offset, where terrain cannot explain it.

That trade is the wrong way round. The positional gain is real but only visible against a
PPK track; the RGB↔NDVI overlay is what a grower actually looks at.

Also eliminated in the process, so nobody retries them: giving the RGB pass a real DSM
instead of `--fast-orthophoto` changes nothing (1.956 m → 1.956 m), and the
terrain-relief explanation is refuted *within* a single flight (rougher tiles shift
**less**, r = +0.27).

The fix worth building is one reconstruction feeding both orthos — the RGB frames are
already in the multispectral dataset (885 usable = 177 captures × 5 surfaces), just
excluded from the reconstruction. Then they cannot disagree by construction.

**Seeding the split camera models from DJI's factory calibration.** Tested seven ways
across three flights — principal point as documented, sign-flipped, axis-swapped,
distortion alone — and it never beat a neutral start on both metrics. The values are
correct for the job DJI documents (undistorting pixels via OpenCV), but the solver has 240
in-situ images of the lens as it is today and does better from zero.

---

## 6. Known limits

Stated plainly, because a fork that only lists its wins is not reviewable.

- **Every measurement here comes from ~300-capture flights.** Large flights are untested.
- **`_M3M_MODELS` is a one-entry set standing in for a camera registry.** Every new DJI
  multispectral model needs a deliberate decision, not automatic inclusion.
- **The view-angle gate's thresholds are not calibrated against a labelled dataset.** It
  refused 2 of 3 re-driven flights. One (Green control 0.62 against a 0.80 limit) is near
  enough the line to be worth understanding; the other (−0.88) is not. §3.4 exists to
  build the dataset that would let those thresholds be set on evidence.
- **Band alignment and four-band reconstruction may now be redundant with each other**
  (§3.5). Unverified.
- **The corrections are validated on outputs, not against ground truth reflectance.** We
  have no calibrated reflectance panel measurements across these flights.

Traps we have already paid for — the ones most likely to waste your afternoon — are in
[`CLAUDE.md`](CLAUDE.md) §6. Two worth repeating here because they are not M3M-specific
and cost us real quality:

- **`--matcher-neighbors N` silently disables graph matching.** `opendm/osfm.py` — passing
  *any* value sets `matcher_graph_rounds = 0`. The default (`0`) is not "no neighbours",
  it is a different and better strategy. We shipped `16` for months.
- **`--matcher-order` is a no-op on georeferenced data**, and every drone dataset is
  georeferenced. ODM says so in its own log.

---

## 7. Building and running

The corrections are pure Python under `opendm/` and `stages/`, so there is nothing to
recompile. `agro.Dockerfile` overlays them onto a pre-built ODX image by `COPY`, and runs
`agro_verify.sh` inside the image so the build fails if the overlay did not apply:

```bash
docker build -f agro.Dockerfile -t odx-m3m .
```

The base image is pinned **by digest**, not by tag — ODX is a rolling release with no
version tags, and a floating tag makes the build unreproducible. If you build this
yourself, repoint the `FROM` at an ODX image you control and **re-verify** that the four
overlaid files are byte-identical to this fork's merge base first, or the `COPY` will
silently revert upstream changes made since.

Local development:

```bash
./agro_verify.sh .                      # the assertion — seconds, no Docker, no auth
./agro_test.sh --docker                 # ODX's suite vs the known-failure list
./test.sh multispectral_view_angle      # one file
```

`test.sh` expands its argument to `tests/test_<arg>.py` and runs
`python3 -m unittest discover` — **not pytest**. A pytest-only test will not be discovered
by the repo's own runner and will look like it passed by never running.

---

## 8. Relationship to upstream

We are not proposing these changes upstream as-is, and there is no open PR against
`WebODM/ODX`.

[`DJI_M3M_FINDINGS.md`](DJI_M3M_FINDINGS.md) is the upstream-facing write-up: measurements,
controls, and refuted hypotheses, written for ODX/ODM maintainers rather than for us. Some
of what it contains is not camera-specific and may be worth having in the engine —
particularly the `--matcher-neighbors` behaviour (§1) and the seam-levelling finding (§2).

`AGENTS.md` at the repo root is **upstream's** policy file, inherited at the merge base. It
asks that contributions be manually written and that AI use be disclosed, and that agents
not open PRs or issues against ODX automatically. We respect that: anything we want to
contribute back gets handed to a human to submit in their own words.

**Some commits in this fork were co-authored with Claude**, and say so in their trailers.
Every measurement quoted in this document and in the commit messages was run on real
flights against real imagery.

## 9. Contact

Issues and pull requests against **this fork** are welcome at
[Agronome-ai/ODX](https://github.com/Agronome-ai/ODX/issues). Please do not report
fork-specific behaviour to upstream WebODM/ODX — it is ours, not theirs.
