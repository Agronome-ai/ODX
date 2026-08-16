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

# APPLIED-ASSERTION (DD-171 Phase 4b) — see agro_verify.sh for what it checks and why.
#
# The check lives in a script rather than inline here so that the SAME assertion runs in
# two places and cannot drift between them: at image build (below, against /code) and on
# every pull request (.github/workflows/agronome-ci.yml, against the checkout — no Docker
# and no registry auth needed, so it costs seconds).
COPY agro_verify.sh /code/agro_verify.sh
RUN bash /code/agro_verify.sh /code
