#!/usr/bin/env bash
#
# Assert that this tree/image really carries the Agronome M3M overlay.
#
# THE FAILURE THIS EXISTS FOR: the view-angle and across-swath ramp corrections once
# shipped as CONSTANTS AND SIXTY LINES OF RATIONALE with no implementing function, in a
# file that reads as though the correction runs. Every measurement taken "with the
# correction on" was in fact taken with it off, and no instrument could have caught it —
# they all measure the output, and the output was perfectly self-consistent.
#
# Every mechanism in that chain fails QUIETLY: a COPY of the wrong file succeeds, a
# text-anchored patch that matches nothing exits 0, and the build goes green with the
# engine unpatched. So the artifact has to prove it carries the corrections.
#
# Called from two places, deliberately, so the check cannot drift between them:
#   agro.Dockerfile          at image build, against /code
#   .github/workflows/       on every PR, against the checkout — no Docker, no auth
#
# Usage:  ./agro_verify.sh [root]      (root defaults to /code)
set -euo pipefail

ROOT="${1:-/code}"
fail=0

note() { printf '  %-58s %s\n' "$1" "$2"; }
bad()  { note "$1" "MISSING"; fail=1; }

MS="$ROOT/opendm/multispectral.py"
PHOTO="$ROOT/opendm/photo.py"
OSFM_STAGE="$ROOT/stages/run_opensfm.py"
MVSTEX="$ROOT/stages/mvstex.py"

for f in "$MS" "$PHOTO" "$OSFM_STAGE" "$MVSTEX"; do
    [ -f "$f" ] || { echo "NO SUCH FILE: $f"; exit 1; }
done

# --- symbols ----------------------------------------------------------------------
for sym in prepare_dji_irradiance dji_hmatrix dji_band_warp normalize_view_angle \
           _view_angle_profile _ramp_profile _ramp_profile_groundfree _frame_signs \
           _is_m3m; do
    grep -q "def ${sym}" "$MS" && note "symbol ${sym}" "ok" || bad "symbol ${sym}"
done
grep -q "def is_m3m" "$PHOTO" && note "symbol photo.is_m3m" "ok" || bad "symbol photo.is_m3m"

# --- call sites -------------------------------------------------------------------
# A function that EXISTS but is never INVOKED is precisely the bug above, which a
# symbol-only check would have called shipped.
for call in "prepare_dji_irradiance(" "normalize_view_angle("; do
    grep -q "${call}" "$OSFM_STAGE" && note "call site ${call}" "ok" || bad "call site ${call}"
done

# --- XMP tags ---------------------------------------------------------------------
for tag in dji_flight_x_speed dji_flight_y_speed dji_calibrated_hmatrix \
           dji_optical_center_x drone-dji:BlackLevel; do
    grep -q "${tag}" "$PHOTO" && note "xmp ${tag}" "ok" || bad "xmp ${tag}"
done

# --- model-level gating (DD-171 D11/4d) -------------------------------------------
# The engine has no camera registry, so the guard IS the registry. A make-level guard
# left in a path that applies M3M coefficients hands them to an M4E and produces a
# fully-formed, silently wrong product.
if grep -q 'camera_make == "DJI"' "$MVSTEX"; then
    note "no make-level guard in mvstex" "FOUND ONE"; fail=1
else
    note "no make-level guard in mvstex" "ok"
fi
if grep -q 'p.camera_make == "DJI" for p in photos' "$OSFM_STAGE"; then
    note "no make-level guard in run_opensfm" "FOUND ONE"; fail=1
else
    note "no make-level guard in run_opensfm" "ok"
fi

# --- selectable seam levelling ----------------------------------------------------
grep -q "ODX_SEAM_LEVELING" "$MVSTEX" && note "ODX_SEAM_LEVELING switch" "ok" \
    || bad "ODX_SEAM_LEVELING switch"

# --- shape guard ------------------------------------------------------------------
# A frame that undistorts to a size other than the reference photo's cannot be binned
# against `bin_idx`; without this guard np.bincount raises and kills the flight.
# Asserted rather than trusted because the crash is ORDER-DEPENDENT — the caller returns
# on the first band failing its control gate, so a regression reproduces only on runs
# where a mismatched band happens to sort first.
grep -q "img.shape != bin_idx.shape" "$MS" && note "view-angle shape guard" "ok" \
    || bad "view-angle shape guard"

# --- ODX log API ------------------------------------------------------------------
# log.ODM_WARNING does not exist in ODX. It is muscle memory from other codebases and
# AttributeErrors at runtime, on the error path — the one place you find out last.
if grep -q "log.ODM_WARNING" "$MVSTEX" "$MS" "$PHOTO" "$OSFM_STAGE"; then
    note "no log.ODM_WARNING" "FOUND ONE"; fail=1
else
    note "no log.ODM_WARNING" "ok"
fi

# --- it at least parses -----------------------------------------------------------
if python3 -m py_compile "$PHOTO" "$MS" "$OSFM_STAGE" "$MVSTEX"; then
    note "py_compile" "ok"
else
    note "py_compile" "FAILED"; fail=1
fi

echo
if [ "$fail" != "0" ]; then
    echo "AGRO OVERLAY VERIFICATION FAILED — this artifact does NOT carry the corrections."
    exit 1
fi
echo "agro overlay verified: 10 symbols, 2 call sites, 5 XMP tags, model-level gating,"
echo "seam switch, shape guard, no log.ODM_WARNING, all four files compile."
