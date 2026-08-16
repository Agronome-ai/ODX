# Agronome ODX engine — upstream ODX plus this branch's DJI M3M corrections.
#
# Built as a THIN OVERLAY on our own mirrored copy of upstream ODX, not from source:
# every change on this branch is pure Python under opendm/ and stages/, so there is
# nothing to recompile. A source build would take hours of SuperBuild for four files.
#
# FROM our mirror pinned by DIGEST, never `webodm/odx:latest` (DD-171 D12): upstream
# is a rolling release with no version tags and Docker Hub prunes unpulled images, so
# a floating tag makes the build unreproducible and eventually unbuildable.
#
# Safe to overlay because it was verified, not assumed: at the time of writing, all
# four files inside the mirrored image are BYTE-IDENTICAL to this branch's merge base
# (374db4aa), and upstream changed none of them between that base and the mirrored
# commit. So copying our copies applies exactly this branch's commits and reverts
# nothing. The assertion below is what keeps that true as upstream moves.
FROM northamerica-northeast2-docker.pkg.dev/agronome-shared-services/agronome/engine-odx@sha256:c3db2d93abcbe216229be03f0806196b4a25d11b5ac54ee52d01abd0eee32c50

COPY opendm/photo.py            /code/opendm/photo.py
COPY opendm/multispectral.py    /code/opendm/multispectral.py
COPY stages/run_opensfm.py      /code/stages/run_opensfm.py
COPY stages/mvstex.py           /code/stages/mvstex.py

# APPLIED-ASSERTION (DD-171 Phase 4b).
#
# The failure this guards is the one that already happened: the across-swath ramp and
# view-angle corrections shipped as CONSTANTS AND COMMENTS with no implementing
# function, in a file that reads as though the correction is present. Every mechanism
# in play fails quietly -- a COPY of the wrong file succeeds, a text-anchored patch
# that matches nothing exits 0, and the build goes green with the engine unpatched.
#
# So the image must prove it carries the corrections. Symbol AND call site: a function
# that exists but is never invoked is precisely the DD-171 "view-angle normalization
# never actually ran" bug, which a symbol-only check would have called shipped.
RUN set -eu; \
    fail=0; \
    for sym in prepare_dji_irradiance dji_hmatrix dji_band_warp normalize_view_angle \
               _view_angle_profile _ramp_profile _ramp_profile_groundfree _frame_signs \
               _is_m3m; do \
        grep -q "def ${sym}" /code/opendm/multispectral.py || { echo "MISSING SYMBOL: ${sym}"; fail=1; }; \
    done; \
    grep -q "def is_m3m" /code/opendm/photo.py || { echo "MISSING: photo.is_m3m"; fail=1; }; \
    # D11/4d — the engine has no camera registry, so the guard IS the registry. A
    # make-level guard left in a path that applies M3M coefficients would hand them to
    # an M4E and produce a fully-formed, silently wrong product.
    grep -q 'camera_make == "DJI"' /code/stages/mvstex.py && { echo "make-level guard left in mvstex"; fail=1; }; \
    grep -q 'p.camera_make == "DJI" for p in photos' /code/stages/run_opensfm.py && { echo "make-level guard left in run_opensfm"; fail=1; }; \
    for tag in dji_flight_x_speed dji_flight_y_speed dji_calibrated_hmatrix \
               dji_optical_center_x drone-dji:BlackLevel; do \
        grep -q "${tag}" /code/opendm/photo.py || { echo "MISSING XMP TAG: ${tag}"; fail=1; }; \
    done; \
    for call in "prepare_dji_irradiance(" "normalize_view_angle("; do \
        grep -q "${call}" /code/stages/run_opensfm.py || { echo "MISSING CALL SITE: ${call}"; fail=1; }; \
    done; \
    grep -q "ODX_SEAM_LEVELING" /code/stages/mvstex.py || { echo "MISSING: ODX_SEAM_LEVELING"; fail=1; }; \
    # A frame that undistorts to a size other than the reference photo's cannot be binned
    # against `bin_idx`; without this guard np.bincount raises and kills the flight. It is
    # asserted rather than trusted because the crash is ORDER-DEPENDENT — the caller
    # returns on the first band failing its control gate, so a regression here reproduces
    # only on the runs where a mismatched band happens to sort first.
    grep -q "img.shape != bin_idx.shape" /code/opendm/multispectral.py \
        || { echo "MISSING: _view_angle_profile shape guard"; fail=1; }; \
    grep -q "log.ODM_WARNING" /code/stages/mvstex.py && { echo "log.ODM_WARNING does not exist in ODX"; fail=1; }; \
    python3 -m py_compile /code/opendm/photo.py /code/opendm/multispectral.py \
                          /code/stages/run_opensfm.py /code/stages/mvstex.py \
        || { echo "py_compile FAILED"; fail=1; }; \
    [ "$fail" = "0" ] || { echo "AGRO ODX OVERLAY DID NOT APPLY"; exit 1; }; \
    echo "agro overlay verified: 8 symbols, 5 XMP tags, 2 call sites, seam-leveling switch, shape guard"
