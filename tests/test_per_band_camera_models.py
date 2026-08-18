"""Each M3M band must resolve to its own camera model (DD-200).

An M3M is four physically different lenses in one head. `camera_id` is built from
make + model + width + height + projection + focal_ratio, and every band reports
byte-identical values for all six, so without the band in the key all four collapse into
ONE camera model. The reconstruction then fits a single lens to four different lenses and
stays internally consistent by bending camera POSES to absorb the mismatch -- which is why
the symptom was never a wrong-looking map, only a map in slightly the wrong place.

Two properties are asserted hardest, because either alone is worse than neither:

  * the four bands must produce four DISTINCT ids -- without this the split does not happen
  * the projection must become `brown` -- `perspective` carries (focal, k1, k2) and has no
    principal-point field at all, so four perspective models would be four copies of the
    same limitation

The camera gate is the third: the engine has no camera registry, so `is_m3m()` IS the
registry. Splitting bands on a camera whose bands are not a rigid capture group would
produce a confidently wrong product rather than a visible failure.
"""

import unittest

from opendm.photo import ODM_Photo


def _photo(band, make="DJI", model="M3M", width=2592, height=1944, focal_ratio=0.85):
    """A Photo carrying only what camera_id() reads, without touching the filesystem."""
    p = ODM_Photo.__new__(ODM_Photo)
    p.camera_make = make
    p.camera_model = model
    p.width = width
    p.height = height
    p.camera_projection = "perspective"
    p.focal_ratio = focal_ratio
    p.band_name = band
    return p


class TestPerBandCameraModels(unittest.TestCase):
    def test_four_bands_yield_four_distinct_ids(self):
        """The whole intervention. Identical EXIF must no longer mean one camera."""
        ids = {_photo(b).camera_id() for b in ("Green", "Red", "RedEdge", "NIR")}
        self.assertEqual(len(ids), 4, f"bands collapsed into {len(ids)} camera(s): {ids}")

    def test_bands_use_the_brown_projection(self):
        """Perspective has no cx/cy, so splitting without brown changes nothing that matters."""
        for band in ("Green", "Red", "RedEdge", "NIR"):
            self.assertIn("brown", _photo(band).camera_id())
            self.assertNotIn("perspective", _photo(band).camera_id())

    def test_same_band_is_stable(self):
        """Two frames of one band are the same camera -- otherwise every frame self-calibrates."""
        self.assertEqual(_photo("Red").camera_id(), _photo("Red").camera_id())

    def test_non_m3m_keeps_one_shared_model(self):
        """Upstream behaviour for every other camera, unchanged and unsplit."""
        ids = {
            _photo(b, make="Parrot", model="Sequoia").camera_id()
            for b in ("Green", "Red", "RedEdge", "NIR")
        }
        self.assertEqual(len(ids), 1)
        self.assertIn("perspective", ids.pop())

    def test_m3m_rgb_frame_is_not_split(self):
        """The colour frame is a fifth, genuinely different lens -- but the RGB pass is a
        separate project that nothing has measured. Widening to it is a decision, not a
        side effect, so it must keep upstream behaviour."""
        rgb = _photo("RGB", width=5280, height=3956)
        self.assertIn("perspective", rgb.camera_id())
        self.assertNotIn("brown", rgb.camera_id())

    def test_missing_band_name_does_not_split(self):
        """A photo with no band cannot be assigned to a per-band model; falling back to the
        shared model is the safe direction, since the alternative invents a camera."""
        for missing in (None, ""):
            self.assertIn("perspective", _photo(missing).camera_id())

    def test_mavic_3_enterprise_is_not_treated_as_m3m(self):
        """Keyed on MODEL, not make: the M3E's payload is a different sensor, and applying
        M3M behaviour to it yields a fully-formed, silently wrong product (DD-171 D11/4d)."""
        ids = {
            _photo(b, model="M3E").camera_id()
            for b in ("Green", "Red", "RedEdge", "NIR")
        }
        self.assertEqual(len(ids), 1)

    def test_id_has_no_stray_whitespace(self):
        """The id is a dict key and a filename fragment downstream; a trailing separator on
        the non-split path would silently create a second camera for the same lens."""
        for p in (_photo("Red"), _photo("RGB", width=5280, height=3956), _photo(None)):
            cid = p.camera_id()
            self.assertEqual(cid, cid.strip())
            self.assertNotIn("  ", cid)


if __name__ == "__main__":
    unittest.main()
