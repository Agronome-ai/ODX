# Agronome ODX engine — upstream ODX plus this branch's DJI M3M corrections.
#
# Built as a THIN OVERLAY on our own mirrored copy of upstream ODX, not from source:
# every change on this branch is pure Python under modules/ and stages/, so there is
# nothing to recompile. A source build would take hours of SuperBuild for four files.
#
# FROM our mirror pinned by DIGEST, never `webodm/odx:latest` (DD-171 D12): upstream
# is a rolling release with no version tags and Docker Hub prunes unpulled images, so
# a floating tag makes the build unreproducible and eventually unbuildable.
#
# Safe to overlay ONLY while the mirrored image is built from the same upstream commit
# this branch is based on. This COPY carries FOUR files; every other upstream change is
# whatever the base image already had. Overlay 3.8.4's four files onto a 3.8.2 base and
# you get a mixed engine — our corrections at 3.8.4, and gsd.py, orthophoto.py and
# config.py still at 3.8.2. It runs, it looks right, and it is not the engine anyone
# thinks it is.
#
# At 3.8.4 it is worse than mixed Python, and the COPY cannot even reach it: upstream
# renamed the package opendm/ -> modules/ (97c30679), so the COPYs below write
# /code/modules/*.py into an image whose code still lives in /code/opendm/. Nothing would
# import our files at all. The VERSION guard catches it first; that is the only reason it
# is not a silent disaster.
#
# 3.8.4 also moves OpenSfM (029bf053 -> a125a3d1), so re-mirroring genuinely recompiles it.
# Do NOT reuse the 3.8.2 -> 3.8.3 reasoning, which said the pin was unchanged: that was
# true then (verified, because the plausible-sounding version of the comment said
# otherwise) and is false now.
#
# The VERSION guard below is what makes that failure loud. It is the same reasoning as
# the applied-assertion: every mechanism in this chain otherwise fails silently.
#
# So: WHEN THIS BRANCH SYNCS TO A NEW UPSTREAM, RE-MIRROR engine-odx FIRST and repoint
# the digest below. The guard will fail the build until you do.
#
# BUILDING THIS OUTSIDE AGRONOME: the base below is our private mirror and you cannot
# pull it. Repoint FROM at an ODX image you control, built from the same upstream commit
# as this branch's merge base — the guard checks exactly that. See FORK.md §7.
FROM northamerica-northeast2-docker.pkg.dev/agronome-shared-services/agronome/engine-odx@sha256:c3db2d93abcbe216229be03f0806196b4a25d11b5ac54ee52d01abd0eee32c50

# VERSION GUARD — the base image must be built from the upstream commit this branch is
# based on, not merely "some ODX". Compares the repo's VERSION against the image's own.
# Cheap, and it catches the one failure a four-file COPY cannot otherwise reveal.
COPY VERSION /tmp/agro_expected_version
RUN test "$(cat /code/VERSION)" = "$(cat /tmp/agro_expected_version)" || { \
        echo "FATAL: base image is ODX $(cat /code/VERSION), this branch is ODX $(cat /tmp/agro_expected_version)."; \
        echo "       Re-mirror engine-odx from the matching upstream commit and repoint the FROM digest."; \
        echo "       Overlaying anyway would ship a mixed engine. See agro.Dockerfile header."; \
        exit 1; \
    }; rm -f /tmp/agro_expected_version

COPY modules/photo.py           /code/modules/photo.py
COPY modules/multispectral.py   /code/modules/multispectral.py
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
