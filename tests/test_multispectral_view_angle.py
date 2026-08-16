import os
import shutil
import unittest

import numpy as np
import rasterio
from rasterio.transform import from_origin

from opendm import multispectral

OUT = "tests/assets/output"


def _write_tif(path, height, width, value=1000.0):
    with rasterio.open(
        path, "w", driver="GTiff", height=height, width=width, count=1,
        dtype="float32", crs="EPSG:32620", transform=from_origin(0, 0, 1, 1),
    ) as ds:
        ds.write(np.full((height, width), value, dtype="float32"), 1)


class TestViewAngleProfile(unittest.TestCase):
    """A frame that undistorts to an unexpected size must not kill the flight.

    `bin_idx` is built once from the largest photo's dimensions, while every frame is
    read at its own shape. Before the guard, a single mismatched frame raised
    "The weights and list don't have the same length" out of np.bincount — uncaught,
    because the try/except wraps only the read. It killed TuplinSkips (job 9ca82421)
    on 2026-08-16.
    """

    NBIN = 10
    DECIM = 2
    REF_H, REF_W = 64, 80          # reference frame, pre-decimation

    def setUp(self):
        if os.path.exists(OUT):
            shutil.rmtree(OUT)
        os.makedirs(OUT)
        hh, hw = self.REF_H // self.DECIM, self.REF_W // self.DECIM
        yy, xx = np.mgrid[0:hh, 0:hw]
        rn = np.hypot(xx - hw / 2.0, yy - hh / 2.0)
        self.bin_idx = np.clip((rn / rn.max() * self.NBIN).astype(int), 0, self.NBIN - 1)

    def _profile(self, paths):
        return multispectral._view_angle_profile(paths, self.bin_idx, self.NBIN, self.DECIM)

    def test_matching_frames_contribute(self):
        paths = []
        for i in range(3):
            p = os.path.join(OUT, f"match_{i}.tif")
            _write_tif(p, self.REF_H, self.REF_W)
            paths.append(p)
        prof, n = self._profile(paths)
        self.assertEqual(n, 3)
        self.assertIsNotNone(prof)

    def test_mismatched_frame_is_skipped_not_raised(self):
        """The regression: one odd-sized frame among good ones."""
        good = os.path.join(OUT, "good.tif")
        odd = os.path.join(OUT, "odd.tif")
        _write_tif(good, self.REF_H, self.REF_W)
        _write_tif(odd, self.REF_H + 8, self.REF_W + 8)   # different undistorted size

        prof, n = self._profile([good, odd])
        self.assertEqual(n, 1, "the mismatched frame must be skipped, not counted")
        self.assertIsNotNone(prof)

    def test_all_frames_mismatched_yields_no_profile(self):
        """Caller treats None as 'cannot correct' and declines — it must not raise."""
        odd = os.path.join(OUT, "all_odd.tif")
        _write_tif(odd, self.REF_H + 8, self.REF_W + 8)
        prof, n = self._profile([odd])
        self.assertEqual(n, 0)
        self.assertIsNone(prof)

    def test_unreadable_frame_still_skipped(self):
        """The pre-existing read guard must keep working alongside the shape guard."""
        good = os.path.join(OUT, "ok.tif")
        _write_tif(good, self.REF_H, self.REF_W)
        missing = os.path.join(OUT, "does_not_exist.tif")
        prof, n = self._profile([good, missing])
        self.assertEqual(n, 1)
        self.assertIsNotNone(prof)


if __name__ == "__main__":
    unittest.main()
