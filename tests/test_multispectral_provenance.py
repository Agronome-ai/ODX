"""The radiometric gate must say what it did — on every path (DD-196 Phase 1).

The defect these tests exist for is not a wrong verdict; the gate's verdicts were
right. Three M3M flights were re-driven on 2026-08-14/15, exactly one was corrected,
and nothing outside Cloud Logging recorded which. A decision that leaves no trace
cannot be reviewed, counted, or alerted on, so nothing can catch it being wrong.

So what is asserted here is the RECORD, not the correction. In particular the negative
paths: `normalize_view_angle` has four early returns, all of them refusals, and those
are precisely the ones a hand-written "write the summary here" would forget.
"""

import json
import os
import shutil
import unittest

import numpy as np
import rasterio
from rasterio.transform import from_origin

from opendm import multispectral

OUT = "tests/assets/output"


class _Photo:
    """The attributes `_frame_signs` and `_band_undistorted_paths` actually read."""

    def __init__(self, filename, vx=None, vy=None):
        self.filename = filename
        self.dji_flight_x_speed = vx
        self.dji_flight_y_speed = vy


def _radial(height, width, slope):
    """A frame whose brightness varies with distance from the centre.

    `slope` is the fractional change from centre to corner: +0.4 brightens outward,
    -0.4 darkens outward, 0 is flat. Two sets of frames with opposite slopes are what
    the control gate is built to catch, so this is how a genuine refusal is staged.
    """
    yy, xx = np.mgrid[0:height, 0:width]
    r = np.hypot(xx - (width - 1) / 2.0, yy - (height - 1) / 2.0)
    return (1000.0 * (1.0 + slope * (r / r.max()))).astype("float32")


def _write_frame(path, height, width, slope=0.0):
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs="EPSG:32620", transform=from_origin(0, 0, 1, 1),
    ) as ds:
        ds.write(_radial(height, width, slope), 1)


class ProvenanceCase(unittest.TestCase):
    """Shared scaffolding: a project dir with an `opensfm/undistorted/images` tree."""

    H, W = 32, 40

    def setUp(self):
        if os.path.exists(OUT):
            shutil.rmtree(OUT)
        self.project = os.path.join(OUT, "project")
        self.opensfm = os.path.join(self.project, "opensfm")
        self.undistorted = os.path.join(self.opensfm, "undistorted", "images")
        os.makedirs(self.undistorted)

    def _band(self, name, count, prefix="DJI_20260814120000", slopes=None):
        """One band with `count` captures on disk, plus the photo objects for it.

        `slopes` is an optional (first_half, second_half) pair of radial slopes. The
        gate splits the sorted path list in half and compares the two profiles, so
        opposing slopes here produce a genuine control failure rather than a
        degenerate one.
        """
        suffix = {"Green": "G", "Red": "R", "NIR": "NIR", "RedEdge": "RE"}[name]
        photos = []
        half = count // 2
        for i in range(count):
            filename = "%s_%04d_MS_%s.TIF" % (prefix, i, suffix)
            slope = 0.0 if slopes is None else (slopes[0] if i < half else slopes[1])
            _write_frame(os.path.join(self.undistorted, filename), self.H, self.W, slope)
            photos.append(_Photo(filename))
        return {"name": name, "photos": photos}

    def _run(self, multi_camera):
        return multispectral.normalize_view_angle(
            self.undistorted, multi_camera, self.W, self.H, decim=2,
            opensfm_dir=self.opensfm)

    def _sidecar(self):
        path = os.path.join(self.project, multispectral.RADIOMETRIC_SUMMARY_FILENAME)
        self.assertTrue(os.path.exists(path), "no summary written at %s" % path)
        with open(path) as handle:
            return json.load(handle)


class TestSummaryWrittenOnEveryPath(ProvenanceCase):

    def test_too_few_frames_is_recorded_not_silent(self):
        """The commonest refusal, and the one with nothing to log about it."""
        band = self._band("Green", multispectral.VIEW_ANGLE_MIN_FRAMES - 1)
        returned = self._run([band])

        doc = self._sidecar()
        self.assertEqual(doc, returned, "the returned summary must be what was written")
        self.assertIs(doc["applied"], False)
        self.assertEqual(doc["skip_reason"], "too_few_frames:Green")
        self.assertEqual(doc["bands"]["Green"]["frames"],
                         multispectral.VIEW_ANGLE_MIN_FRAMES - 1)

    def test_control_failure_names_the_band_that_failed(self):
        """Scott Smith's shape: the halves measured opposite profiles (corr -0.88).

        Staged with one half brightening outward and the other darkening, which is
        what the control is for. The record must carry the measured numbers, not just
        the verdict — recalibrating the threshold later needs the evidence, and this
        is the only place it survives.
        """
        band = self._band("Green", multispectral.VIEW_ANGLE_MIN_FRAMES + 2,
                          slopes=(0.4, -0.4))
        self._run([band])

        doc = self._sidecar()
        self.assertIs(doc["applied"], False)
        self.assertEqual(doc["skip_reason"], "control_failed:Green")
        green = doc["bands"]["Green"]
        self.assertLess(green["control_corr"], multispectral.VIEW_ANGLE_MIN_CORR)
        self.assertIsNotNone(green["edge_disagreement"])

    def test_the_applied_path_records_its_evidence_too(self):
        """A pass must carry the same numbers a refusal does.

        Constant frames pass their control cleanly — worth stating, because the radial
        bin grid is laid over a rectangular frame, so the outer bins are corner-only
        and the binned profile has variance even for a perfectly flat image. That is
        why `corrcoef` returns 1.00 here rather than NaN, and why the missing
        finiteness guard on this control is a latent gap rather than a live one.

        G4 is "how many flights skipped this month, and on what evidence". A record
        that only populates on the negative path answers half of that and cannot
        establish a baseline to compare against.
        """
        returned = self._run([self._band("Green", multispectral.VIEW_ANGLE_MIN_FRAMES + 2)])

        doc = self._sidecar()
        self.assertEqual(doc, returned)
        self.assertIs(doc["applied"], True)
        self.assertIsNone(doc["skip_reason"])
        green = doc["bands"]["Green"]
        self.assertGreaterEqual(green["control_corr"], multispectral.VIEW_ANGLE_MIN_CORR)
        self.assertLessEqual(green["edge_disagreement"],
                             multispectral.VIEW_ANGLE_MAX_DISAGREE)
        self.assertIsNotNone(green["edge_centre"])
        self.assertEqual(doc["frames_failed"], 0)

    def test_no_bands_is_distinct_from_a_failed_control(self):
        """"Nothing to measure" and "measured and refused" are different events."""
        self._run([])

        doc = self._sidecar()
        self.assertIs(doc["applied"], False)
        self.assertEqual(doc["skip_reason"], "no_bands")
        self.assertEqual(doc["bands"], {})

    def test_summary_survives_an_exception_in_the_body(self):
        """The seeded refusal is what makes the `finally` worth having.

        An unexpected raise must still leave a truthful "not applied" rather than the
        silence that reads as "never a candidate".
        """
        original = multispectral._band_undistorted_paths
        multispectral._band_undistorted_paths = lambda *a, **k: 1 / 0
        try:
            with self.assertRaises(ZeroDivisionError):
                self._run([{"name": "Green", "photos": []}])
        finally:
            multispectral._band_undistorted_paths = original

        doc = self._sidecar()
        self.assertIs(doc["applied"], False)
        self.assertEqual(doc["skip_reason"], "unknown")


class TestSummaryShape(ProvenanceCase):

    def test_thresholds_are_stored_not_assumed(self):
        """When a threshold moves, historical flights must stay explicable."""
        self._run([self._band("Green", 4)])

        thresholds = self._sidecar()["thresholds"]
        self.assertEqual(thresholds["min_corr"], multispectral.VIEW_ANGLE_MIN_CORR)
        self.assertEqual(thresholds["max_disagree"], multispectral.VIEW_ANGLE_MAX_DISAGREE)
        self.assertEqual(thresholds["min_frames"], multispectral.VIEW_ANGLE_MIN_FRAMES)
        self.assertEqual(thresholds["ramp_min_corr"], multispectral.RAMP_MIN_CORR)
        self.assertEqual(thresholds["ramp_max_amplitude"], multispectral.RAMP_MAX_AMPLITUDE)

    def test_ramp_refusal_is_recorded_separately_from_the_radial_one(self):
        """Two gates, two records — a ramp skip does not mean the radial was skipped."""
        self._run([self._band("Green", 4)])

        doc = self._sidecar()
        self.assertEqual(doc["ramp_skip_reason"], "no_headings",
                         "photos carry no ground speed, so there is no flight axis")
        self.assertIsNone(doc["flight_axis_deg"])
        self.assertIs(doc["ramp_applied"], False)

    def test_written_json_is_strictly_parseable(self):
        """A NaN control correlation must not produce a sidecar nothing can read.

        `json.dump` writes bare `NaN`, which Python reads back happily and every
        strict parser rejects — so the file would round-trip in this test suite and
        fail in the wrapper that consumes it.
        """
        self._run([self._band("Green", multispectral.VIEW_ANGLE_MIN_FRAMES + 2,
                              slopes=(0.4, -0.4))])

        with open(os.path.join(
                self.project, multispectral.RADIOMETRIC_SUMMARY_FILENAME)) as handle:
            raw = handle.read()
        for token in ("NaN", "Infinity", "-Infinity"):
            self.assertNotIn(token, raw)
        json.loads(raw, parse_constant=_reject)


class TestSummaryHelpers(unittest.TestCase):

    def test_finite_or_none_drops_the_unserialisable(self):
        self.assertIsNone(multispectral._finite_or_none(float("nan")))
        self.assertIsNone(multispectral._finite_or_none(float("inf")))
        self.assertIsNone(multispectral._finite_or_none(None))
        self.assertIsNone(multispectral._finite_or_none("not a number"))
        self.assertEqual(multispectral._finite_or_none(np.float32(0.5)), 0.5)

    def test_project_dir_is_the_opensfm_parent(self):
        self.assertEqual(
            multispectral._radiometric_project_dir("/p/opensfm/undistorted/images",
                                                   "/p/opensfm"),
            "/p")

    def test_project_dir_falls_back_without_an_opensfm_dir(self):
        """The signature allows omitting it, so the fallback must not misfile."""
        self.assertEqual(
            multispectral._radiometric_project_dir("/p/opensfm/undistorted/images", None),
            "/p")

    def test_write_never_raises_on_an_unwritable_path(self):
        """A diagnostic must not fail the flight it describes."""
        multispectral.write_radiometric_summary("/nonexistent/dir", {"applied": False})


def _reject(token):
    raise ValueError("non-JSON constant in the summary: %s" % token)


if __name__ == "__main__":
    unittest.main()
