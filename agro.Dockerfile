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
# Safe to overlay ONLY while the mirrored image is built from the same upstream commit
# this branch is based on. Overlay four files from ODX 3.8.3 onto a 3.8.2 image and you
# get a mixed engine: our corrections at 3.8.3, everything else — including OpenSfM,
# which 3.8.3 bumped in SuperBuild and which no COPY can deliver — still at 3.8.2. It
# runs, it looks right, and it is not the engine anyone thinks it is.
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
