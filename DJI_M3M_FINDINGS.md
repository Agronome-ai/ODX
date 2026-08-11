# DJI M3M multispectral: findings from a six-week measurement campaign

Notes for the ODX/ODM maintainers on DJI Mavic 3 Multispectral orthomosaic quality,
July–August 2026. Seven agricultural flights were processed through ODM/ODX and
through Pix4Dfields and the difference measured rather than eyeballed.

**Provenance:** the measurements below were made on **ODX 3.8.2**. Where numbers
come from a production pipeline that pin **ODM 3.6.0**, that is stated. The
`--matcher-neighbors` behaviour (§1) was verified in both.

Everything below is measured, with the control that established it. Where we were
wrong we say so — several of our own early conclusions are in the "refuted" section
because they cost us weeks and may save you the same.

---

## 1. `--matcher-neighbors N` silently disables graph matching

**This is the biggest single finding and it needs no code change — only a doc change.**

`opendm/osfm.py`:

```python
if args.matcher_neighbors > 0:
    matcher_graph_rounds = 0          # graph matching OFF
    matcher_neighbors = args.matcher_neighbors
else:
    matcher_graph_rounds = 20         # the default
    matcher_neighbors = 0
```

Passing **any** value to `--matcher-neighbors` turns off graph-based matching
entirely. The help text describes it as a limit on how many neighbours to match
against; it does not say that setting it swaps the matching *strategy*.

We had `--matcher-neighbors 16` set deliberately, believing it was a restriction
that would help on repetitive row crops. Measured on a 286-capture M3M flight,
against a Pix4Dfields export of the same flight:

| `--matcher-neighbors` | aligned r vs Pix4D | cross-pass frame disagreement |
|---|---|---|
| 16 | **0.490** | 0.133 m |
| 32 | 0.903 | 0.069 m |
| 64 | 0.966 | 0.027 m |
| **0 (default)** | **0.965** | 0.031 m |
| 40 000 features, still 16 | 0.459 | 0.161 m |

Pix4D's own no-blend export scores 0.963 against its blended one, so `0` and `64`
reach Pix4D's internal consistency.

The last row is the control that proves the mechanism: raising `--min-num-features`
to 40 000 while keeping `matcher-neighbors 16` made things **worse**, and dropped
views-per-point to 2. Features cannot rescue a starved match graph.

Generalises across five more flights (largest reconstruction, `16` → `0`):
312 → 68 images inverted on one flight, 620 vs 320 on another, and on a
249-capture flight `0` reconstructed **249/249 images in a single partial** where
`16` managed 135 across two.

**Suggested fix:** document the side effect in `--matcher-neighbors`' help string,
or split the strategy switch from the neighbour count so a user can cap neighbours
*without* disabling graph rounds.

### Related: `--matcher-order` is a no-op on georeferenced data

Confirmed by ODM's own log line — `"Georeferenced reconstruction, ignoring
--matcher-order"`. Every drone dataset is georeferenced, so for the drone use case
this flag never does anything. Worth saying so in the help text.

---

## 2. Per-band seam levelling manufactures a flight-line "quilt" on multispectral

mvs-texturing's global + local seam levelling applies a tonal offset **per texture
patch, per band, independently**. On a single-band or RGB product that is harmless.
On multispectral it is not: independent per-band offsets perturb the NIR/Red ratio,
so a patch that was radiometrically fine acquires an NDVI bias — and because patches
follow the flight lines, the bias appears as rectangular blocks aligned with the
survey passes.

This explained an artifact that had survived every frame-level correction we tried:
the leveller was re-adjusting tones *afterwards*.

Fix on our side: `--texturing-skip-global-seam-leveling` +
`--texturing-skip-local-seam-leveling` for the DJI camera-only path. The blocks
disappear and crop-row sharpness is fully preserved.

We later re-tested re-enabling levelling in three configurations (global-only,
local-only, both) and all three were rejected on sight — texrecon retones whole
patches, so the per-frame error survives at full magnitude *inside* each patch. It
trades stripes for blotches.

**Suggested fix:** default seam levelling off for multi-camera (multispectral)
reconstructions, or level all bands with a *shared* per-patch factor so ratios are
preserved.

---

## 3. Single-view compositing is where "spiky edges" come from — and Pix4D has it too

We spent a long time treating ragged NDVI plant edges as a defect in our radiometry.
It is not. Pix4Dfields' **no-blend** export carries *more* of it than ours
(high-frequency energy 0.563 vs 0.430 on the same flight). Their blended export does
not. The difference is entirely the compositing step.

ODM textures best-view: each mesh face takes its value from one frame. Neighbouring
output pixels can come from frames minutes apart, so the choice of frame becomes a
choice of brightness.

We implemented a DSM-based multi-view orthorectifier as a post-ODM stage: for each
output pixel, take Z from the DSM, project into every camera that saw it, and
accumulate a weighted average with weight `cos(view_zenith)^P`.

Choosing `P` — two independent measures bracket Pix4D at 60:

| P | high-freq energy | curvature (sharpness proxy) |
|---|---|---|
| best-view | 0.493 | 31.9 |
| 4 | 0.017 | 12.2 (visibly blurred) |
| 30 | 0.077 | 16.5 |
| **60** | **0.167** | **21.5** |
| 120 | 0.287 | 25.9 |
| **Pix4D blended** | **0.190** | **21.0** |

The ratio to best-view is stable across four flights: 0.32–0.34x high-freq,
0.60–0.68x curvature.

**⚠️ The important caveat, and the reason we are not proposing this upstream as-is:**
a DSM-based blender has **no visibility test**. It projects a ground point into every
camera that *geometrically could* see it, including cameras whose view is blocked by
a tree or a roof — then averages canopy into ground. We measured the damage by
binning the departure from best-view by local DSM step:

| height step over 1 m | 0–0.25 | 0.25–0.5 | 0.5–1 | 1–2 | 2–4 | >4 m |
|---|---|---|---|---|---|---|
| relative to flat | 1.00x | 1.00x | 0.97x | 1.14x | 1.64x | **2.63x** |

Across seven flights, terrain relief separates cleanly: flat cropland sits at
2.5–4.9% of pixels stepped >2 m, while a treed/built site sits at 30–33%.

### The fix: a per-pixel relief fallback

Rather than gate blending per flight, the weight is collapsed toward best-view
**per pixel**, driven by the same quantity the damage scales with:

```
alpha = smoothstep((4 m − local_step) / (4 m − 1 m))     # 1 = blend, 0 = best-view
out   = alpha * blended + (1 − alpha) * best_view
```

`local_step` is the height difference over a 1 m baseline, read from the DSM already
produced. The 1 m and 4 m bounds are taken directly from the dose-response above —
full blend where the ratio is 1.00x, pure best-view where it reaches 2.63x — not
tuned by hand. Both accumulators come from one pass, so the cost is one extra raster.

Measured on three flights spanning the relief range:

| flight | % pixels stepped >2 m | arm | flat | 1–2 m | 2–4 m | >4 m |
|---|---|---|---|---|---|---|
| flat cropland | 3.7% | cos^60 | 1.00x | 0.92x | 1.16x | 1.44x |
| | | **adaptive** | 1.00x | 0.85x | **0.33x** | **0.00x** |
| mixed, low altitude | 30.0% | cos^60 | 1.00x | 1.06x | 1.32x | 1.61x |
| | | **adaptive** | 1.00x | 0.96x | **0.35x** | **0.00x** |
| treed / built | 32.7% | cos^60 | 1.00x | 1.12x | 1.61x | **2.61x** |
| | | **adaptive** | 1.00x | 1.03x | **0.47x** | **0.00x** |

Two properties matter. On flat ground the adaptive output is **byte-identical** to
full cos^60 (rendered tiles differ by 0.0/255; hi-freq 0.36x vs 0.36x, curvature
0.71x vs 0.70x) — it costs nothing where it is not needed. And on the worst site it
still applies full blending to **64.6%** of pixels, so it is not "blending disabled
on hard flights": it protects only the pixels that need protecting.

This is an **approximation of a visibility test**: it cannot distinguish "occluded
from camera 7" from "near something tall", it just declines to average where
averaging is unsafe.

**We assumed a real per-camera visibility solve would be strictly better. It is not —
we built it and measured it worse.** Shadow mapping (per camera, scatter ground-point
distances into a depth buffer at camera resolution keeping the nearest; accept a
point only if its own distance matches) on the treed site:

| arm, vs the same best-view control | hi-freq | curvature |
|---|---|---|
| cos^60, no occlusion handling | 0.32x | 0.60x |
| **proximity fallback (above)** | **0.36x** | **0.71x** |
| shadow-map visibility, depth bias 1.0 m | **1.31x** | 0.89x |
| " " bias 2.5 m | 1.20x | 0.88x |
| " " bias 5.0 m | 1.05x | 0.86x |

Blending must **reduce** fine-scale energy — every other measurement here gives
0.32–0.36x. The visibility arm produces *more* than the un-blended control, and loses
coverage (3.29 M valid px vs 3.55 M). That is shadow acne: the depth test flipping
between visible and occluded on adjacent pixels, so each pixel averages a different
subset of cameras. Raising the bias converges on **1.0x — no different from
un-blended** — never on 0.36x. Too small and surfaces self-shadow; too large and the
test stops rejecting anything.

On flat terrain it is identical to the proximity rule (1.00x/1.00x), so there is no
false occlusion; the failure is specific to real relief, which is where it had to work.

**Why the approximation wins.** Shadow mapping is a razor-thin geometric test applied
to a surface *reconstructed from the very images being tested*, and a photogrammetric
DSM carries metre-scale noise over vegetation — so the visible/occluded answer flips
between neighbouring pixels, and the flipping is the speckle. "Is this pixel near
something tall?" is a smooth, spatially coherent property that noise does not flip.
The input cannot support the precision the exact method demands.

If the engine ever grows a multi-view compositing path, this is worth knowing before
reaching for the textbook solution. A true visibility solve would likely need an
independent, lower-noise surface model (lidar, or a mesh built with far more
aggressive smoothing) rather than the photogrammetric DSM.

The flat-field case turned out to be the strongest argument for per-pixel over
per-flight: that flight carries genuine occlusion (1.44x at >4 m steps) that is
invisible in any whole-flight statistic, because only 0.8% of its area is stepped
that far. A per-flight gate cannot see it; the per-pixel rule removes it without
touching the other 99%.

---

## 4. DJI M3M specifics worth having in the engine

Implemented against the *DJI Mavic 3M Image Processing Guide* (2026 edition).

- **Per-capture band alignment.** ODM computes one homography per secondary band
  from at most `max_samples` (30) captures and reuses it flight-wide. On the M3M the
  true NIR↔Red offset swings **~31.8 px between opposing flight headings** — DJI
  publishes a 30.66 mm `RelativeOpticalCenter` separation — so a single matrix
  smears every capture not flown on the sampled heading. Keeping a warp per capture
  and applying the matching one at undistort time fixes it, falling back to ODM's
  global matrix where a capture produced no match.
- **Vignette (Guide Eq. 7)** should be centred on `drone-dji:CalibratedOpticalCenterX/Y`,
  which on the M3M sits **~44 px** from `Camera:VignettingCenter`.
- **DLS irradiance** on the M3M is per-band and noisy frame to frame; a short running
  median over captures stabilises it. **But see the refuted section — do not use it
  to rescale DN.**

---

## 5. What we got wrong (so you don't repeat it)

Recorded because these were plausible, well-argued, and cost the most time.

- **Illumination instability does not drive the artifact.** Consecutive-frame
  brightness vs the sun sensor: **r = −0.153, inside a shuffled control** (max 0.326).
  Auto-exposure absorbs it before it reaches the pixels.
- **Irradiance equalisation by rescaling uint16 DN is actively harmful.** Scale
  factors are unbounded in practice (we measured **4.75x and 7.56x** on production
  flights). At 4.74x, **56% of NIR and 21% of Red pixels clipped at 65535** — and the
  two bands clip at different rates, which destroys the ratio. Compounding it, DJI
  writes `BlackLevel=3200` and ODM subtracts it *afterwards*, so scaling injects
  `3200*(scale−1)` of fake signal. Any irradiance work must happen in float **after**
  black-level and exposure normalisation.
- **`--radiometric-calibration camera+sun` is worse than `camera` for DJI.** Measured
  on a 5.5x illumination swing: `camera` gave an NDVI gap of 0.0228 between a bright
  and a dark frame; `camera+sun` gave 0.0348. DJI's per-band DLS is not proportional
  across bands, so it perturbs the NIR/Red ratio.
- **Per-image radiometric gain solved from overlap regressions did not work for us.**
  Our solver reported **67.5% per-frame gain on a flight with 1.8% illumination
  variation** that looks visually correct. It passed held-out validation, which
  proves generalisation, not correctness — pair disagreement grew with time gap
  (5.6% → 33.1%), i.e. it was fitting view geometry, not gain.
- **A tight `--gps-accuracy` prior was load-bearing.** On a flight whose own XMP
  reports 1.35 m we had `--gps-accuracy 0.159` — a 20x over-claim. Relaxing it toward
  the honest value made every metric monotonically **worse** (aligned r 0.490 →
  0.434 → 0.304, visible stitch seams at 3.0). The over-tight prior was holding the
  reconstruction together.

### Measurement notes

Four of our own metrics gave confident wrong answers before we caught them. If you
benchmark this kind of work: perturb the input deliberately and confirm the metric
moves the right way (one of ours *improved* 22% when we deliberately misregistered
the bands, peaking at 100x the true offset); use a placebo (a seam metric scored
mid-pass placebo lines higher than real seams); check your noise floor before
reading a bias (a "constant +4/−4 cm offset with zero scatter" was 1-px
quantisation — the true offset was 0.17 cm); and never compare products at different
resolutions (a "8x less spiky" blend prototype was entirely explained by its coarser
grid).

---

## 6. Camera-specific handling has no home in the engine

The corrections above are model-specific — vignette coefficients, band layout, DLS
response and optical-centre offsets differ across M3M, M4E, P4 Multispectral and
M2EA. But `opendm` has no camera database: geometry is derived from EXIF at runtime,
which works because every camera writes focal length and image size, and radiometry
has nowhere to live. The only model-level branch we found is a hardcoded thermal
check for `MAVIC2-ENTERPRISE-ADVANCED`.

Consequently our own patches guard on `camera_make == "DJI"`, which is too broad: an
M4E that happens to write `CalibratedOpticalCenterX/Y` would silently receive M3M's
vignette model.

We would be glad to contribute a model-keyed camera table if that is a direction the
project wants to take. Happy to split any of the above into individual PRs — the
changes are ~800 lines and almost entirely additive, sitting alongside existing logic
rather than replacing it.
