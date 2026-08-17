"""The multispectral pass must reconstruct from every band, and only on the M3M.

Upstream reconstructs from one primary band and copies that band's solved pose onto the
other three, so three quarters of the imagery contributes nothing to the geometry.
Measured across three flights, using all four bands raises average track length by
23-71% and, on a flight whose primary band is weak, converts a broken reconstruction
into a usable one.

The camera gate is what these tests guard hardest. Handing four-band behaviour to a
camera whose bands are not a rigid capture group would produce a confidently wrong
product rather than a visible failure — the same reasoning as DD-171 D11/4d.
"""

import unittest

from opendm import multispectral


class _Photo:
    def __init__(self, filename, band, make="DJI", model="M3M"):
        self.filename = filename
        self.band_name = band
        self.camera_make = make
        self.camera_model = model


def _multi_camera(bands=("Green", "Red", "RedEdge", "NIR"), n=3, **kw):
    return [
        {"name": b, "photos": [_Photo(f"DJI_{i:04d}_MS_{b}.TIF", b, **kw) for i in range(n)]}
        for b in bands
    ]


class TestAllBandsReconstructable(unittest.TestCase):
    def test_m3m_multiband_is_reconstructable(self):
        self.assertTrue(multispectral.all_bands_reconstructable(_multi_camera()))

    def test_non_m3m_is_not(self):
        """A camera we have not measured keeps upstream behaviour."""
        mc = _multi_camera(make="MicaSense", model="Altum")
        self.assertFalse(multispectral.all_bands_reconstructable(mc))

    def test_a_single_foreign_photo_disqualifies_the_set(self):
        """Fail closed: a mixed set must not get M3M treatment."""
        mc = _multi_camera()
        mc[2]["photos"][0].camera_model = "M4E"
        self.assertFalse(multispectral.all_bands_reconstructable(mc))

    def test_single_band_is_not_reconstructable_as_multi(self):
        """One band is not a capture group; there is nothing extra to add."""
        self.assertFalse(multispectral.all_bands_reconstructable(_multi_camera(bands=("Green",))))

    def test_missing_camera_model_is_not_assumed_to_be_m3m(self):
        mc = _multi_camera()
        mc[0]["photos"][0].camera_model = None
        self.assertFalse(multispectral.all_bands_reconstructable(mc))


class TestGetPhotosByBand(unittest.TestCase):
    def test_m3m_returns_every_band(self):
        mc = _multi_camera(n=5)
        photos = multispectral.get_photos_by_band(mc, "auto")
        self.assertEqual(len(photos), 20, "4 bands x 5 captures")
        self.assertEqual(len({p.band_name for p in photos}), 4)

    def test_non_m3m_returns_only_the_primary_band(self):
        mc = _multi_camera(n=5, make="Parrot", model="Sequoia")
        photos = multispectral.get_photos_by_band(mc, "auto")
        self.assertEqual(len(photos), 5)
        self.assertEqual({p.band_name for p in photos}, {"Green"})

    def test_explicit_primary_band_still_honoured_for_other_cameras(self):
        mc = _multi_camera(n=4, make="Parrot", model="Sequoia")
        photos = multispectral.get_photos_by_band(mc, "NIR")
        self.assertEqual({p.band_name for p in photos}, {"NIR"})

    def test_every_photo_is_returned_exactly_once(self):
        """Duplicates would inflate the image list and be matched against themselves."""
        photos = multispectral.get_photos_by_band(_multi_camera(n=6), "auto")
        names = [p.filename for p in photos]
        self.assertEqual(len(names), len(set(names)))


if __name__ == "__main__":
    unittest.main()
