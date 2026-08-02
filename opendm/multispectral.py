import math
import re
import cv2
import os
from opendm import dls
import numpy as np
from opendm import log
from opendm.concurrency import parallel_map
from opensfm.io import imread

from skimage import exposure
from skimage.morphology import disk
from skimage.filters import rank, gaussian

# Loosely based on https://github.com/micasense/imageprocessing/blob/master/micasense/utils.py

# DJI sun-sensor (DLS) irradiance smoothing.
#
# The M3M stores a per-band, per-frame irradiance reading; dividing by it
# raw (the previous behavior) trusts that the sensor on the drone sees the
# same light as the ground below. Under scattered clouds it does not: on a
# cloudy test flight the frame medians ANTI-correlated (-0.54) with the raw
# readings, so raw division injects frame-to-frame error into every band
# ratio. A rolling median over neighboring captures keeps the real slow
# illumination trend (sun angle, broad overcast) while rejecting the fast
# sensor-vs-ground flicker, and a sanity clamp around the band median guards
# the extremes. Units are unchanged from the raw reading.
_dji_irradiance_map = {}
DJI_IRRADIANCE_SMOOTH_WINDOW = 7

def prepare_dji_irradiance(photos):
    global _dji_irradiance_map
    _dji_irradiance_map = {}
    by_band = {}
    for p in photos:
        if p.camera_make == "DJI" and p.spectral_irradiance is not None \
                and p.horizontal_irradiance is None:
            by_band.setdefault(p.band_name, []).append(p)

    half = DJI_IRRADIANCE_SMOOTH_WINDOW // 2
    for band_name, band_photos in by_band.items():
        # DJI filenames sort chronologically (DJI_YYYYMMDDHHMMSS_NNNN_...)
        band_photos.sort(key=lambda p: p.filename)
        values = np.array([float(p.spectral_irradiance) for p in band_photos])
        band_median = float(np.median(values))
        if band_median <= 0:
            continue

        for i, p in enumerate(band_photos):
            lo, hi = max(0, i - half), min(len(values), i + half + 1)
            smoothed = float(np.median(values[lo:hi]))
            smoothed = min(max(smoothed, 0.2 * band_median), 5.0 * band_median)
            _dji_irradiance_map[p.filename] = smoothed

        log.INFO("DJI irradiance smoothing: %s captures for band %s (band median %.1f)" % (
            len(band_photos), band_name, band_median))

def dn_to_radiance(photo, image):
    """
    Convert Digital Number values to Radiance values
    :param photo ODM_Photo
    :param image numpy array containing image data
    :return numpy array with radiance image values
    """

    image = image.astype("float32")
    if len(image.shape) != 3:
        raise ValueError("Image should have shape length of 3 (got: %s)" % len(image.shape))
    
    # Thermal (this should never happen, but just in case..)
    if photo.is_thermal():
        return image

    # All others
    a1, a2, a3 = photo.get_radiometric_calibration()
    dark_level = photo.get_dark_level()

    exposure_time = photo.exposure_time
    gain = photo.get_gain()
    gain_adjustment = photo.gain_adjustment

    V, x, y = vignette_map(photo)
    if x is None:
        x, y = np.meshgrid(np.arange(photo.width), np.arange(photo.height))

    if dark_level is not None:
        image -= dark_level

    # Normalize DN to 0 - 1.0
    bit_depth_max = photo.get_bit_depth_max()
    if bit_depth_max:
        image /= bit_depth_max
    else:
        log.WARNING("Cannot normalize DN for %s, bit depth is missing" % photo.filename)
    
    if V is not None:
        # vignette correction
        V = np.repeat(V[:, :, np.newaxis], image.shape[2], axis=2)
        image *= V

    if exposure_time and a2 is not None and a3 is not None:
        # row gradient correction
        R = 1.0 / (1.0 + a2 * y / exposure_time - a3 * y)
        R = np.repeat(R[:, :, np.newaxis], image.shape[2], axis=2)
        image *= R
    
    # Floor any negative radiances to zero (can happen due to noise around blackLevel)
    if dark_level is not None:
        image[image < 0] = 0
    
    # apply the radiometric calibration - i.e. scale by the gain-exposure product and
    # multiply with the radiometric calibration coefficient

    if gain is not None and exposure_time is not None:
        image /= (gain * exposure_time)
    
    if a1 is not None:
        # multiply with the radiometric calibration coefficient
        image *= a1

    if gain_adjustment is not None:
        image *= gain_adjustment

    return image

def vignette_map(photo):
    x_vc, y_vc = photo.get_vignetting_center()
    polynomial = photo.get_vignetting_polynomial()

    if x_vc and polynomial:
        # append 1., so that we can call with numpy polyval
        polynomial.append(1.0)
        vignette_poly = np.array(polynomial)

        # perform vignette correction
        # get coordinate grid across image
        x, y = np.meshgrid(np.arange(photo.width), np.arange(photo.height))

        # meshgrid returns transposed arrays
        # x = x.T
        # y = y.T

        # compute matrix of distances from image center
        r = np.hypot((x - x_vc), (y - y_vc))

        # compute the vignette polynomial for each distance - we divide by the polynomial so that the
        # corrected image is image_corrected = image_original * vignetteCorrection
        vignette = np.polyval(vignette_poly, r)

        # DJI is special apparently
        if photo.camera_make != "DJI":
            vignette = 1.0 / vignette

        return vignette, x, y
    
    return None, None, None

def dn_to_reflectance(photo, image, use_sun_sensor=True):
    radiance = dn_to_radiance(photo, image)
    irradiance = compute_irradiance(photo, use_sun_sensor=use_sun_sensor)

    if photo.camera_make == "DJI":
        # DJI M3M Image Processing Guide: reflectance-proportional values are
        # (DN - black) / (gain * exposure) / irradiance -- there is no pi
        # factor in DJI's model, and the scale is RELATIVE, so clamping at 1.0
        # would destroy legitimate bright values. Keep only the physical floor.
        reflectance = radiance / irradiance
        reflectance[reflectance < 0.0] = 0.0
        return reflectance.astype("float32")

    reflectance = radiance * math.pi / irradiance
    reflectance[reflectance < 0.0] = 0.0
    reflectance[reflectance > 1.0] = 1.0
    return reflectance.astype("float32")

def compute_irradiance(photo, use_sun_sensor=True):
    # Thermal (this should never happen, but just in case..)
    if photo.is_thermal():
        return 1.0

    # DJI: prefer the temporally-smoothed sun-sensor irradiance when the
    # flight-wide map has been prepared (see prepare_dji_irradiance)
    if photo.camera_make == "DJI":
        smoothed = _dji_irradiance_map.get(photo.filename)
        if smoothed is not None:
            return smoothed

    # Some cameras (Micasense, DJI) store the value (nice! just return)
    hirradiance = photo.get_horizontal_irradiance()
    if hirradiance is not None:
        return hirradiance

    # TODO: support for calibration panels

    if use_sun_sensor and photo.get_sun_sensor():
        # Estimate it
        dls_orientation_vector = np.array([0,0,-1])
        sun_vector_ned, sensor_vector_ned, sun_sensor_angle, \
        solar_elevation, solar_azimuth = dls.compute_sun_angle([photo.latitude, photo.longitude],
                                        photo.get_dls_pose(),
                                        photo.get_utc_time(),
                                        dls_orientation_vector)

        angular_correction = dls.fresnel(sun_sensor_angle)

        # TODO: support for direct and scattered irradiance

        direct_to_diffuse_ratio = 6.0 # Assumption, clear skies
        spectral_irradiance = photo.get_sun_sensor()

        percent_diffuse = 1.0 / direct_to_diffuse_ratio
        sensor_irradiance = spectral_irradiance / angular_correction

        # Find direct irradiance in the plane normal to the sun
        untilted_direct_irr = sensor_irradiance / (percent_diffuse + np.cos(sun_sensor_angle))
        direct_irradiance = untilted_direct_irr
        scattered_irradiance = untilted_direct_irr * percent_diffuse

        # compute irradiance on the ground using the solar altitude angle
        horizontal_irradiance = direct_irradiance * np.sin(solar_elevation) + scattered_irradiance
        return horizontal_irradiance
    elif use_sun_sensor:
        log.WARNING("No sun sensor values found for %s" % photo.filename)
    
    return 1.0

# Within-frame view-angle normalization.
#
# A frame's oblique parts view a canopy at a larger zenith angle than its
# centre, so they see more foliage and less soil. Red darkens toward the frame
# edge while NIR brightens (opposite signs, so this is not vignetting), which
# shifts NDVI by ~0.05 from frame centre to edge. Best-view texturing then
# tiles that gradient across the orthophoto as patches following the flight
# lines. Normalizing every pixel to its nadir equivalent removes it.
#
# The profile is measured from the flight's own frames rather than assumed:
# each frame is divided by its own median (removing illumination), then binned
# by image radius, which maps to view zenith. Scene content averages out over
# many frames. It is measured on two disjoint halves of the flight and only
# applied if they agree -- without that control the field's own gradient can
# masquerade as a view-angle effect.
#
# The decision is ALL-OR-NOTHING across bands. Band ratios (NDVI and friends)
# are what these products are for, so correcting one band while refusing
# another would change the ratio rather than the geometry -- the same mistake
# that makes per-band seam leveling print a quilt into NDVI. If any band fails
# its control, no band is touched.
VIEW_ANGLE_BINS = 32
VIEW_ANGLE_MIN_FRAMES = 30
VIEW_ANGLE_MIN_CORR = 0.8
VIEW_ANGLE_MAX_DISAGREE = 0.15
VIEW_ANGLE_CLAMP = (0.6, 1.6)

# --- across-swath (solar principal plane) ramp --------------------------------
#
# The radial profile above is symmetric about the frame centre by construction,
# so it can brighten the edges relative to the middle but can never make one side
# of a frame differ from the other. Over a canopy that asymmetry is real: the
# reflectance of vegetation changes between looking toward the sun and away from
# it, so as the view sweeps across the swath the crop genuinely changes
# brightness. Each pass then lays down a strip that is dark on one edge and
# bright on the other, and adjacent strips butt those edges together -- which is
# the faint banding seen along flight lines in the orthophoto.
#
# Measured on a 286-capture M3M flight (sun azimuth 192 deg, flight bearing 76
# deg, so the across-swath axis lies close to the solar principal plane):
#   the residual against a reference product ramps monotonically by 0.024 NDVI
#   from one side of the swath to the other, beating a shuffled-ownership
#   control 7x.
#
# The ramp is fixed in the WORLD, not in the camera: the aircraft turns around
# but the sun does not. Measured per frame, the left-right gradient flips sign
# between opposing passes at 17.4 sigma in Red and 6.6 sigma in NIR, with
# near-equal magnitude (-0.790 vs +0.785). So the correction MUST be signed by
# the direction of travel; applying a fixed image-space ramp would correct one
# heading and double the error on the other.
#
# Red ramps about 3.6x harder than NIR, which is exactly why this survives into
# NDVI -- a gain common to both bands would cancel in the ratio.
RAMP_BINS = 16
RAMP_MIN_CORR = 0.7          # two disjoint halves of the flight must agree
RAMP_MAX_AMPLITUDE = 0.5     # refuse implausible fits
RAMP_MIN_SPEED = 1.5         # m/s below which a frame has no meaningful heading


def _view_angle_profile(paths, bin_idx, nbin, decim):
    from rasterio.enums import Resampling
    import rasterio

    acc = np.zeros(nbin)
    n = 0
    for p in paths:
        try:
            with rasterio.open(p) as ds:
                img = ds.read(1, out_shape=(ds.height // decim, ds.width // decim),
                              resampling=Resampling.average).astype(np.float64)
        except Exception:
            continue
        ok = np.isfinite(img) & (img > 1e-7)
        if ok.mean() < 0.5:
            continue
        med = np.median(img[ok])
        if not np.isfinite(med) or med <= 0:
            continue
        v = np.where(ok, img / med, np.nan)
        s = np.bincount(bin_idx.ravel(), weights=np.nan_to_num(v).ravel(), minlength=nbin)
        c = np.bincount(bin_idx.ravel(), weights=np.isfinite(v).ravel().astype(float),
                        minlength=nbin)
        acc += s / np.maximum(c, 1)
        n += 1
    return (acc / n if n else None), n


def _capture_key(filename):
    """The part of a filename shared by all bands of one capture."""
    m = re.match(r"(.+?)_MS_[A-Za-z]+\.", os.path.basename(filename))
    return m.group(1) if m else os.path.basename(filename).split('.')[0]


def _frame_signs(multi_camera):
    """+1 / -1 per capture, by which way along the flight line it was taken.

    Uses the ground velocity in the frame's own metadata; bearing =
    atan2(y_speed, x_speed). Frames slower than RAMP_MIN_SPEED are at a
    turnaround and have no meaningful heading, so they inherit the sign of the
    nearest frame in capture order rather than guessing.

    Returns {} when the tags are absent, which disables the ramp entirely.
    """
    stamp = re.compile(r"(\d{14})")
    seen = {}
    for band in multi_camera:
        for p in band.get('photos', []):
            vx = getattr(p, 'dji_flight_x_speed', None)
            vy = getattr(p, 'dji_flight_y_speed', None)
            key = _capture_key(p.filename)
            if key in seen:
                continue
            m = stamp.search(p.filename)
            order = m.group(1) if m else key
            bearing = None
            if vx is not None and vy is not None and np.hypot(vx, vy) >= RAMP_MIN_SPEED:
                bearing = np.degrees(np.arctan2(vy, vx)) % 360.0
            seen[key] = (order, bearing)

    known = [b for _, b in seen.values() if b is not None]
    if len(known) < VIEW_ANGLE_MIN_FRAMES:
        return {}

    # dominant flight axis, from the doubled-angle mean so opposing passes agree
    rad = np.radians(np.array(known))
    axis = np.degrees(np.arctan2(np.sin(2 * rad).mean(),
                                 np.cos(2 * rad).mean())) / 2.0 % 180.0

    ordered = sorted(seen.items(), key=lambda kv: kv[1][0])
    signs = {}
    last = None
    for key, (_, bearing) in ordered:
        if bearing is None:
            signs[key] = last if last is not None else 1
            continue
        rel = (bearing - axis) % 360.0
        last = 1 if (rel < 90.0 or rel > 270.0) else -1
        signs[key] = last
    # frames before the first usable heading inherit backwards
    first = next((signs[k] for k, _ in ordered if seen[k][1] is not None), 1)
    for key, (_, bearing) in ordered:
        if bearing is None and signs[key] is None:
            signs[key] = first
    n_pos = sum(1 for v in signs.values() if v > 0)
    log.INFO("View-angle ramp: flight axis %.0f deg, %s frames one way, %s the other"
             % (axis, n_pos, len(signs) - n_pos))
    return signs


def _ramp_profile(paths, signs, nbin, decim, width):
    """Mean normalised value against signed across-swath position.

    The signed coordinate runs -1..+1 across the frame and is multiplied by the
    frame's heading sign, so every frame is expressed in the same world-fixed
    frame of reference before averaging.
    """
    from rasterio.enums import Resampling
    import rasterio

    acc = np.zeros(nbin)
    cnt = np.zeros(nbin)
    n = 0
    for p in paths:
        s = signs.get(_capture_key(p))
        if not s:
            continue
        try:
            with rasterio.open(p) as ds:
                img = ds.read(1, out_shape=(ds.height // decim, ds.width // decim),
                              resampling=Resampling.average).astype(np.float64)
        except Exception:
            continue
        ok = np.isfinite(img) & (img > 1e-7)
        if ok.mean() < 0.5:
            continue
        med = np.median(img[ok])
        if not np.isfinite(med) or med <= 0:
            continue
        v = np.where(ok, img / med, np.nan)
        w = v.shape[1]
        xn = (np.arange(w) + 0.5) / w * 2.0 - 1.0        # -1 .. +1
        idx = np.clip(((s * xn + 1.0) / 2.0 * nbin).astype(int), 0, nbin - 1)
        # MEDIAN, not mean: reflectance over a canopy is heavily right-skewed, and
        # a column mean is dominated by a few bright pixels. The radial profile
        # gets away with means because it is normalised into a ratio where the
        # inflation cancels; an antisymmetric difference would inherit it and
        # overstate the correction by several times.
        col = np.nanmedian(v, axis=0)
        good = np.isfinite(col)
        acc += np.bincount(idx[good], weights=col[good], minlength=nbin)
        cnt += np.bincount(idx[good], minlength=nbin)
        n += 1
    if n == 0 or (cnt == 0).any():
        return None, n
    prof = acc / cnt
    level = float(np.nanmean(prof))
    if not np.isfinite(level) or level <= 0:
        return None, n
    prof = prof / level                       # dimensionless, centred on 1.0
    # keep only the antisymmetric part: the symmetric part is already handled by
    # the radial profile, and taking only this guarantees the two cannot
    # double-correct the same signal
    anti = (prof - prof[::-1]) / 2.0
    return anti, n


def _band_undistorted_paths(undistorted_dir, band):
    """Undistorted files for a band, tolerant of the extension ODM appends."""
    try:
        listing = os.listdir(undistorted_dir)
    except OSError:
        return []
    index = {}
    for f in listing:
        index.setdefault(f, f)
        index.setdefault(os.path.splitext(f)[0], f)

    paths = []
    for p in band.get('photos', []):
        f = index.get(p.filename) or index.get(os.path.splitext(p.filename)[0])
        if f:
            paths.append(os.path.join(undistorted_dir, f))
    if paths:
        return sorted(paths)

    # Fall back to matching on the band suffix in the filenames themselves,
    # which is how the primary band can otherwise come up empty.
    suffix = None
    for p in band.get('photos', []):
        m = re.search(r"_MS_([A-Za-z]+)\.", p.filename)
        if m:
            suffix = m.group(1)
            break
    if suffix is None:
        suffix = {"red": "R", "green": "G", "blue": "B",
                  "nir": "NIR", "rededge": "RE"}.get(band['name'].lower().replace(" ", ""))
    if suffix is None:
        return []
    tail = "_MS_%s." % suffix
    return sorted(os.path.join(undistorted_dir, f) for f in listing if tail in f)


def normalize_view_angle(undistorted_dir, multi_camera, width, height, decim=8):
    """Flatten the view-zenith reflectance gradient inside every frame."""
    import rasterio

    scale = float(max(width, height))
    hw, hh = width // decim, height // decim
    yy, xx = np.mgrid[0:hh, 0:hw]
    rn = np.hypot(((xx * decim) - (width - 1) / 2.0) / scale,
                  ((yy * decim) - (height - 1) / 2.0) / scale)
    rmax = float(rn.max())
    bin_idx = np.clip((rn / rmax * VIEW_ANGLE_BINS).astype(int), 0, VIEW_ANGLE_BINS - 1)

    signs = _frame_signs(multi_camera)

    # --- measure and gate every band BEFORE touching any of them --------------
    plans = []
    for band in multi_camera:
        name = band['name']
        paths = _band_undistorted_paths(undistorted_dir, band)
        if len(paths) < VIEW_ANGLE_MIN_FRAMES:
            log.WARNING("View-angle normalization: %s has only %s undistorted "
                        "frames -- skipping correction for ALL bands"
                        % (name, len(paths)))
            return

        half = len(paths) // 2
        p1, n1 = _view_angle_profile(paths[:half], bin_idx, VIEW_ANGLE_BINS, decim)
        p2, n2 = _view_angle_profile(paths[half:], bin_idx, VIEW_ANGLE_BINS, decim)
        if p1 is None or p2 is None:
            log.WARNING("View-angle normalization: %s produced no usable profile "
                        "-- skipping correction for ALL bands" % name)
            return
        p1 = p1 / p1[:3].mean()
        p2 = p2 / p2[:3].mean()
        corr = float(np.corrcoef(p1, p2)[0, 1])
        disagree = float(abs(p1[-3:].mean() - p2[-3:].mean()))
        if corr < VIEW_ANGLE_MIN_CORR or disagree > VIEW_ANGLE_MAX_DISAGREE:
            log.WARNING("View-angle normalization: %s failed its control "
                        "(corr %.2f, edge disagreement %.2f) -- skipping "
                        "correction for ALL bands to keep band ratios intact"
                        % (name, corr, disagree))
            return

        prof = (p1 + p2) / 2.0
        prof = np.convolve(np.pad(prof, 2, mode="edge"), np.ones(5) / 5.0, mode="valid")
        prof = np.clip(prof / prof[:3].mean(), *VIEW_ANGLE_CLAMP)
        log.INFO("View-angle normalization: %s edge/centre %.3f (control corr "
                 "%.2f, %s+%s frames)" % (name, prof[-3:].mean(), corr, n1, n2))

        # across-swath ramp, gated on its own two-disjoint-halves control
        ramp = None
        if signs:
            r1, m1 = _ramp_profile(paths[:half], signs, RAMP_BINS, decim, width)
            r2, m2 = _ramp_profile(paths[half:], signs, RAMP_BINS, decim, width)
            if r1 is None or r2 is None:
                log.WARNING("View-angle ramp: %s produced no usable profile -- "
                            "skipping the ramp for ALL bands" % name)
                return
            rcorr = float(np.corrcoef(r1, r2)[0, 1])
            amp = float(np.nanmax(r1 + r2) - np.nanmin(r1 + r2)) / 2.0
            if rcorr < RAMP_MIN_CORR or not np.isfinite(amp) or amp > RAMP_MAX_AMPLITUDE:
                log.WARNING("View-angle ramp: %s failed its control (corr %.2f, "
                            "amplitude %.3f) -- skipping the ramp for ALL bands "
                            "to keep band ratios intact" % (name, rcorr, amp))
                return
            ramp = (r1 + r2) / 2.0
            ramp = np.convolve(np.pad(ramp, 2, mode="edge"), np.ones(5) / 5.0, mode="valid")
            ramp = ramp - ramp.mean()      # antisymmetric already; enforce zero mean
            log.INFO("View-angle ramp: %s across-swath %+.3f .. %+.3f "
                     "(control corr %.2f, %s+%s frames)"
                     % (name, ramp[0], ramp[-1], rcorr, m1, m2))
        plans.append((name, paths, prof, ramp))

    if not plans:
        return

    # --- every band passed, so apply to every band ---------------------------
    yyf, xxf = np.mgrid[0:height, 0:width]
    rnf = np.hypot((xxf - (width - 1) / 2.0) / scale, (yyf - (height - 1) / 2.0) / scale)
    pos = np.clip(rnf / rmax * VIEW_ANGLE_BINS - 0.5, 0, VIEW_ANGLE_BINS - 1)

    xn_full = (np.arange(width) + 0.5) / float(width) * 2.0 - 1.0
    ramp_pos = np.clip((xn_full + 1.0) / 2.0 * RAMP_BINS - 0.5, 0, RAMP_BINS - 1)

    log.INFO("View-angle normalization: all %s bands passed, applying" % len(plans))
    for name, paths, prof, ramp in plans:
        field = np.interp(pos.ravel(), np.arange(VIEW_ANGLE_BINS),
                          prof).reshape(height, width).astype(np.float32)
        ramp_row = None
        if ramp is not None:
            # one row is enough: the ramp varies only across the swath
            ramp_row = np.interp(ramp_pos, np.arange(RAMP_BINS), ramp).astype(np.float32)
        for fp in paths:
            try:
                with rasterio.open(fp) as ds:
                    profile, tags = ds.profile, ds.tags()
                    a = ds.read(1).astype(np.float32)
                if tags.get("ODM_VIEW_ANGLE_NORM"):
                    continue
                total = field
                if ramp_row is not None:
                    s = signs.get(_capture_key(fp))
                    if s:
                        # the ramp is fixed in the world, so a frame flown the
                        # other way sees it mirrored left-to-right
                        row = ramp_row if s > 0 else ramp_row[::-1]
                        total = field * np.clip(1.0 + row[None, :], *VIEW_ANGLE_CLAMP)
                a = (a / total).astype(profile["dtype"])
                with rasterio.open(fp, "w", **profile) as out:
                    out.write(a, 1)
                    out.update_tags(ODM_VIEW_ANGLE_NORM="1")
            except Exception as e:
                log.WARNING("View-angle normalization failed for %s: %s" % (fp, str(e)))


def get_photos_by_band(multi_camera, user_band_name):
    band_name = get_primary_band_name(multi_camera, user_band_name)

    for band in multi_camera:
        if band['name'] == band_name:
            return band['photos']


def get_primary_band_name(multi_camera, user_band_name):
    if len(multi_camera) < 1:
        raise Exception("Invalid multi_camera list")
    
    # Pick RGB, or Green, or Blue, in this order, if available, otherwise first band
    if user_band_name == "auto":
        for aliases in [['rgb', 'redgreenblue'], ['green', 'g'], ['blue', 'b']]:
            for band in multi_camera:
                if band['name'].lower() in aliases:
                    return band['name']
                
        return multi_camera[0]['name']

    for band in multi_camera:
        if band['name'].lower() == user_band_name.lower():
            return band['name']
    
    band_name_fallback = multi_camera[0]['name']

    log.WARNING("Cannot find band name \"%s\", will use \"%s\" instead" % (user_band_name, band_name_fallback))
    return band_name_fallback


def compute_band_maps(multi_camera, primary_band):
    """
    Computes maps of: 
     - { photo filename --> associated primary band photo } (s2p)
     - { primary band filename --> list of associated secondary band photos } (p2s)
    by looking at capture UUID, capture time or filenames as a fallback
    """
    band_name = get_primary_band_name(multi_camera, primary_band)
    primary_band_photos = None
    for band in multi_camera:
        if band['name'] == band_name:
            primary_band_photos = band['photos']
            break
    
    # Try using capture time as the grouping factor
    try:
        unique_id_map = {}
        s2p = {}
        p2s = {}

        for p in primary_band_photos:
            uuid = p.get_capture_id()
            if uuid is None:
                raise Exception("Cannot use capture time (no information in %s)" % p.filename)
            
            # Should be unique across primary band
            if unique_id_map.get(uuid) is not None:
                raise Exception("Unreliable UUID/capture time detected (duplicate)")

            unique_id_map[uuid] = p
        
        for band in multi_camera:
            photos = band['photos']

            for p in photos:
                uuid = p.get_capture_id()
                if uuid is None:
                    raise Exception("Cannot use UUID/capture time (no information in %s)" % p.filename)
                
                # Should match the primary band
                if unique_id_map.get(uuid) is None:
                    raise Exception("Unreliable UUID/capture time detected (no primary band match)")

                s2p[p.filename] = unique_id_map[uuid]

                if band['name'] != band_name:
                    p2s.setdefault(unique_id_map[uuid].filename, []).append(p)

        return s2p, p2s
    except Exception as e:
        # Fallback on filename conventions
        log.WARNING("%s, will use filenames instead" % str(e))

        filename_map = {}
        s2p = {}
        p2s = {}
        file_regex = re.compile(r"^(.+)[-_]\w+(\.[A-Za-z]{3,4})$")

        for p in primary_band_photos:
            filename_without_band = re.sub(file_regex, "\\1\\2", p.filename)

            # Quick check
            if filename_without_band == p.filename:
                raise Exception("Cannot match bands by filename on %s, make sure to name your files [filename]_band[.ext] uniformly." % p.filename)

            filename_map[filename_without_band] = p

        for band in multi_camera:
            photos = band['photos']

            for p in photos:
                filename_without_band = re.sub(file_regex, "\\1\\2", p.filename)

                # Quick check
                if filename_without_band == p.filename:
                    raise Exception("Cannot match bands by filename on %s, make sure to name your files [filename]_band[.ext] uniformly." % p.filename)
                
                if not filename_without_band in filename_map:
                    raise Exception("Cannot match bands by filename on %s, make sure to name your files [filename]_band[.ext] uniformly, check that your images have the appropriate CaptureUUID XMP tag and that no images are missing." % p.filename)
                    
                s2p[p.filename] = filename_map[filename_without_band]

                if band['name'] != band_name:
                    p2s.setdefault(filename_map[filename_without_band].filename, []).append(p)

        return s2p, p2s

def compute_alignment_matrices(multi_camera, primary_band_name, images_path, s2p, p2s, max_concurrency=1, max_samples=30):
    log.INFO("Computing band alignment")

    alignment_info = {}

    # For each secondary band
    for band in multi_camera:
        if band['name'] != primary_band_name:
            matrices = []

            def parallel_compute_homography(p):
                try:
                    # Compute a matrix for EVERY capture (no max_samples cap).
                    # On dual-lens rigs like the DJI M3M the true band offset
                    # swings with flight heading (~32px NIR<->Red between
                    # opposing legs from the 30.66mm lens separation), so one
                    # sampled matrix cannot fit the whole flight. This also
                    # matches the DJI M3M Image Processing Guide, which aligns
                    # each capture individually (ECC / feature matching).

                    # Find good matrix candidates for alignment

                    primary_band_photo = s2p.get(p['filename'])
                    if primary_band_photo is None:
                        log.WARNING("Cannot find primary band photo for %s" % p['filename'])
                        return

                    warp_matrix, dimension, algo = compute_homography(os.path.join(images_path, p['filename']),
                                                                os.path.join(images_path, primary_band_photo.filename),
                                                                photo=p.get('photo'),
                                                                align_photo=primary_band_photo)
                    
                    if warp_matrix is not None:
                        log.INFO("%s --> %s good match" % (p['filename'], primary_band_photo.filename))

                        matrices.append({
                            'filename': p['filename'],
                            'warp_matrix': warp_matrix,
                            'eigvals': np.linalg.eigvals(warp_matrix),
                            'dimension': dimension,
                            'algo': algo
                        })
                    else:
                        log.INFO("%s --> %s cannot be matched" % (p['filename'], primary_band_photo.filename))
                except Exception as e:
                    log.WARNING("Failed to compute homography for %s: %s" % (p['filename'], str(e)))

            parallel_map(parallel_compute_homography, [{'filename': p.filename, 'photo': p} for p in band['photos']], max_concurrency, single_thread_fallback=False)

            # Find the matrix that has the most common eigvals
            # among all matrices. That should be the "best" alignment.
            for m1 in matrices:
                acc = np.array([0.0,0.0,0.0])
                e = m1['eigvals']

                for m2 in matrices:
                    acc += abs(e - m2['eigvals'])

                m1['score'] = acc.sum()
            
            # Sort
            matrices.sort(key=lambda x: x['score'], reverse=False)
            
            if len(matrices) > 0:
                # Keep a per-capture lookup on the winning entry; captures
                # without their own match fall back to the consensus matrix.
                matrices[0]['per_file'] = {
                    m['filename']: {'warp_matrix': m['warp_matrix'], 'dimension': m['dimension']}
                    for m in matrices if m.get('filename')
                }
                log.INFO("per-capture alignment: %s matrices for band %s" % (
                    len(matrices[0]['per_file']), band['name']))
                alignment_info[band['name']] = matrices[0]
                log.INFO("%s band will be aligned using warp matrix %s (score: %s)" % (band['name'], matrices[0]['warp_matrix'], matrices[0]['score']))
            else:
                log.WARNING("Cannot find alignment matrix for band %s, The band might end up misaligned!" % band['name'])

    return alignment_info

def dji_hmatrix(photo):
    """DJI's factory per-band homography, or None if the tag is absent."""
    raw = getattr(photo, 'dji_calibrated_hmatrix', None) if photo is not None else None
    if not raw:
        return None
    try:
        v = [float(x) for x in str(raw).replace(';', ',').split(',') if x.strip() != '']
        if len(v) != 9:
            return None
        h = np.array(v, dtype=np.float64).reshape(3, 3)
        if not np.isfinite(h).all() or abs(np.linalg.det(h)) < 1e-9:
            return None
        return h
    except (ValueError, TypeError):
        return None


def dji_band_warp(image_gray, align_image_gray, photo, align_photo):
    """Align a band using DJI's factory calibration instead of estimating it.

    Every M3M frame carries drone-dji:CalibratedHMatrix, a per-band homography
    calibrated at the factory. Composing the secondary band's matrix with the
    inverse of the primary's gives the band-to-band transform directly, with no
    cross-spectral matching -- which is an ambiguous problem that ECC solves only
    to about 1.5px and occasionally gets badly wrong.

    A factory calibration is made at a fixed target distance, so at flight
    altitude the parallax between two lenses ~30mm apart leaves a small CONSTANT
    residual. One phase-correlated translation over the whole frame removes it;
    unlike ECC there are no local minima to fall into.

    Measured on a 286-capture M3M flight (residual after warping, phase
    correlation on 192px tiles, median / p90 / max in pixels):

        band  ECC (native res)      DJI + shift
        Red   1.64 / 3.21 / 4.99    0.69 / 1.30 / 1.92
        NIR   1.71 / 10.27 / 31.11  1.69 / 3.60 / 26.98

    Red more than halves; NIR holds its median and cuts the tail nearly 3x,
    which is what matters for a band ratio like NDVI -- a capture with a 10px
    NIR offset puts a badly wrong patch in the map.
    """
    Hs = dji_hmatrix(photo)
    Hp = dji_hmatrix(align_photo)
    if Hs is None or Hp is None:
        return None

    try:
        warp_matrix = np.linalg.inv(Hp) @ Hs
    except np.linalg.LinAlgError:
        return None

    h, w = align_image_gray.shape[:2]
    try:
        warped = cv2.warpPerspective(image_gray, warp_matrix, (w, h))
        mask = (warped > 0) & (align_image_gray > 0)
        if mask.mean() > 0.5:
            a = np.where(mask, warped, 0).astype(np.float32)
            b = np.where(mask, align_image_gray, 0).astype(np.float32)
            a -= a[mask].mean()
            b -= b[mask].mean()
            win = np.outer(np.hanning(a.shape[0]), np.hanning(a.shape[1])).astype(np.float32)
            (dx, dy), _ = cv2.phaseCorrelate(a * win, b * win)
            # a factory matrix should only need a small nudge; a large one means
            # the correlation locked onto the wrong peak, so leave it alone
            if np.isfinite(dx) and np.isfinite(dy) and abs(dx) < 25 and abs(dy) < 25:
                shift = np.eye(3, dtype=np.float64)
                shift[0, 2] = dx
                shift[1, 2] = dy
                warp_matrix = shift @ warp_matrix
    except cv2.error as e:
        log.WARNING("DJI band warp refinement failed, using the raw factory matrix: %s" % str(e))

    return warp_matrix


def compute_homography(image_filename, align_image_filename, photo=None, align_photo=None):
    try:
        # Convert images to grayscale if needed
        image = imread(image_filename, unchanged=True, anydepth=True)
        if image.shape[2] == 3:
            image_gray = to_8bit(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
        else:
            image_gray = to_8bit(image[:,:,0])

        max_dim = max(image_gray.shape)
        if max_dim <= 320:
            log.WARNING("Small image for band alignment (%sx%s), this might be tough to compute." % (image_gray.shape[1], image_gray.shape[0]))

        align_image = imread(align_image_filename, unchanged=True, anydepth=True)
        if align_image.shape[2] == 3:
            align_image_gray = to_8bit(cv2.cvtColor(align_image, cv2.COLOR_BGR2GRAY))
        else:
            align_image_gray = to_8bit(align_image[:,:,0])

        def compute_using(algorithm):
            try:
                h = algorithm(image_gray, align_image_gray)
            except Exception as e:
                log.WARNING("Cannot compute homography: %s" % str(e))
                return None, (None, None)

            if h is None:
                return None, (None, None)

            det = np.linalg.det(h)
            
            # Check #1 homography's determinant will not be close to zero
            if abs(det) < 0.25:
                return None, (None, None)

            # Check #2 the ratio of the first-to-last singular value is sane (not too high)
            svd = np.linalg.svd(h, compute_uv=False)
            if svd[-1] == 0:
                return None, (None, None)
            
            ratio = svd[0] / svd[-1]
            if ratio > 100000:
                return None, (None, None)

            return h, (align_image_gray.shape[1], align_image_gray.shape[0])
        
        warp_matrix = None
        dimension = None
        algo = None

        # Prefer the manufacturer's own calibration when the camera provides it.
        # Estimating cross-spectral alignment from the imagery is strictly harder
        # than reading a factory-calibrated matrix, and measurably worse.
        dji_warp = dji_band_warp(image_gray, align_image_gray, photo, align_photo)
        if dji_warp is not None:
            return dji_warp, (align_image_gray.shape[1], align_image_gray.shape[0]), 'dji'

        if max_dim > 320:
            algo = 'feat'
            result = compute_using(find_features_homography)

            if result[0] is None:
                algo = 'ecc'
                log.INFO("Can't use features matching, will use ECC (this might take a bit)")
                # Solve at native resolution; ECC occasionally refuses to converge
                # at full size on low-contrast frames, so keep the old downscaled
                # solve as a fallback rather than dropping the capture entirely.
                result = compute_using(lambda a, b: find_ecc_homography(a, b, max_size=ECC_MAX_SIZE_NATIVE))
                if result[0] is None:
                    log.INFO("ECC did not converge at native resolution, retrying downscaled")
                    result = compute_using(lambda a, b: find_ecc_homography(a, b, max_size=ECC_MAX_SIZE_FALLBACK))
                if result[0] is None:
                    algo = None

        else: # ECC only for low resolution images
            algo = 'ecc'
            log.INFO("Using ECC (this might take a bit)")
            result = compute_using(find_ecc_homography)
            if result[0] is None:
                algo = None
        
        warp_matrix, dimension = result
        return warp_matrix, dimension, algo

    except Exception as e:
        log.WARNING("Compute homography: %s" % str(e))
        return None, (None, None), None

# Working resolution for the ECC band-alignment solve.
#
# The historical cap was 2048, but a DJI M3M frame is 2592x1944, so every solve
# ran at 1296x972 and the resulting warp matrix was then scaled back up by 2 --
# doubling every sub-pixel error. Measured residual band misalignment over 10
# captures (phase correlation on 256px tiles after applying the warp):
#
#   cap 2048 (half res):  median 1.87 px, p90 4.92 px, max 9.54 px
#   cap 4096 (native):    median 1.40 px, p90 2.54 px, max 4.35 px
#
# That matters more than it looks: NDVI is a band ratio, so a Red/NIR offset puts
# a spurious value on every leaf edge. Measured in the orthophoto, our Red->NIR
# p90 was 15.5 cm against Pix4D's 5.7 cm.
ECC_MAX_SIZE_NATIVE = 4096
ECC_MAX_SIZE_FALLBACK = 2048


def find_ecc_homography(image_gray, align_image_gray, number_of_iterations=1000, termination_eps=1e-8, start_eps=1e-4, max_size=ECC_MAX_SIZE_NATIVE):
    pyramid_levels = 0
    h,w = image_gray.shape
    max_dim = max(h, w)
    downscale = 0

    while max_dim / (2**downscale) > max_size:
        downscale += 1

    if downscale > 0:
        f = 1 / (2**downscale)
        image_gray = cv2.resize(image_gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        h,w = image_gray.shape

    min_dim = min(h, w)

    while min_dim > 300:
        min_dim /= 2.0
        pyramid_levels += 1
    
    log.INFO("Pyramid levels: %s" % pyramid_levels)
    
    # Quick check on size
    if align_image_gray.shape[0] != image_gray.shape[0]:
        align_image_gray = to_8bit(align_image_gray)
        image_gray = to_8bit(image_gray)

        fx = image_gray.shape[1]/align_image_gray.shape[1]
        fy = image_gray.shape[0]/align_image_gray.shape[0]

        align_image_gray = cv2.resize(align_image_gray, None, 
                        fx=fx, 
                        fy=fy,
                        interpolation=(cv2.INTER_AREA if (fx < 1.0 and fy < 1.0) else cv2.INTER_LANCZOS4))

    # Build pyramids
    image_gray_pyr = [image_gray]
    align_image_pyr = [align_image_gray]

    for level in range(pyramid_levels):
        image_gray_pyr[0] = to_8bit(image_gray_pyr[0], force_normalize=True)
        image_gray_pyr.insert(0, cv2.resize(image_gray_pyr[0], None, fx=1/2, fy=1/2,
                                interpolation=cv2.INTER_AREA))
        align_image_pyr[0] = to_8bit(align_image_pyr[0], force_normalize=True)
        align_image_pyr.insert(0, cv2.resize(align_image_pyr[0], None, fx=1/2, fy=1/2,
                                interpolation=cv2.INTER_AREA))

    # Define the motion model, scale the initial warp matrix to smallest level
    warp_matrix = np.eye(3, 3, dtype=np.float32)

    for level in range(pyramid_levels+1):
        ig = gradient(gaussian(image_gray_pyr[level]))
        aig = gradient(gaussian(align_image_pyr[level]))

        if level == pyramid_levels and pyramid_levels == 0:
            eps = termination_eps
        else:
            eps = start_eps - ((start_eps - termination_eps) / (pyramid_levels)) * level
    
        # Define termination criteria
        criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT,
                number_of_iterations, eps)

        try:
            log.INFO("Computing ECC pyramid level %s" % level)
            _, warp_matrix = cv2.findTransformECC(ig, aig, warp_matrix, cv2.MOTION_HOMOGRAPHY, criteria, inputMask=None, gaussFiltSize=9)
        except Exception as e:
            if level != pyramid_levels:
                log.INFO("Could not compute ECC warp_matrix at pyramid level %s, resetting matrix" % level)
                warp_matrix = np.eye(3, 3, dtype=np.float32)
            else:
                raise e

        if level != pyramid_levels: 
            warp_matrix = warp_matrix * np.array([[1,1,2],[1,1,2],[0.5,0.5,1]], dtype=np.float32)

    if downscale > 0:
        return warp_matrix * (np.array([[1,1,2],[1,1,2],[0.5,0.5,1]], dtype=np.float32) ** downscale)
    else:
        return warp_matrix


def find_features_homography(image_gray, align_image_gray, feature_retention=0.7, min_match_count=10):

    # Detect SIFT features and compute descriptors.
    detector = cv2.SIFT_create(edgeThreshold=10, contrastThreshold=0.1)

    h,w = image_gray.shape
    max_dim = max(h, w)
    downscale = 0

    max_size = 4096
    while max_dim / (2**downscale) > max_size:
        downscale += 1
    
    if downscale > 0:
        f = 1 / (2**downscale)
        image_gray = cv2.resize(image_gray, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        h,w = image_gray.shape

    if align_image_gray.shape[0] != image_gray.shape[0]:
        fx = image_gray.shape[1]/align_image_gray.shape[1]
        fy = image_gray.shape[0]/align_image_gray.shape[0]

        align_image_gray = cv2.resize(align_image_gray, None, 
                        fx=fx, 
                        fy=fy,
                        interpolation=(cv2.INTER_AREA if (fx < 1.0 and fy < 1.0) else cv2.INTER_LANCZOS4))

    kp_image, desc_image = detector.detectAndCompute(image_gray, None)
    kp_align_image, desc_align_image = detector.detectAndCompute(align_image_gray, None)

    # Match
    FLANN_INDEX_KDTREE = 1
    index_params = dict(algorithm=FLANN_INDEX_KDTREE, trees=5)
    search_params = dict(checks=50)

    flann = cv2.FlannBasedMatcher(index_params, search_params)
    try:
        matches = flann.knnMatch(desc_image, desc_align_image, k=2)
    except Exception as e:
        return None

    # Filter good matches following Lowe's ratio test
    good_matches = []
    for m, n in matches:
        if m.distance < feature_retention * n.distance:
            good_matches.append(m)

    matches = good_matches

    if len(matches) < min_match_count:
        return None

    # Debug
    # imMatches = cv2.drawMatches(im1, kp_image, im2, kp_align_image, matches, None)
    # cv2.imwrite("matches.jpg", imMatches)

    # Extract location of good matches
    points_image = np.zeros((len(matches), 2), dtype=np.float32)
    points_align_image = np.zeros((len(matches), 2), dtype=np.float32)

    for i, match in enumerate(matches):
        points_image[i, :] = kp_image[match.queryIdx].pt
        points_align_image[i, :] = kp_align_image[match.trainIdx].pt

    # Find homography
    h, _ = cv2.findHomography(points_image, points_align_image, cv2.RANSAC)
    if h is None:
        return None
    
    if downscale > 0:
        return h * (np.array([[1,1,2],[1,1,2],[0.5,0.5,1]], dtype=np.float32) ** downscale)
    else:
        return h

def gradient(im, ksize=5):
    im = local_normalize(im)
    grad_x = cv2.Sobel(im,cv2.CV_32F,1,0,ksize=ksize)
    grad_y = cv2.Sobel(im,cv2.CV_32F,0,1,ksize=ksize)
    grad = cv2.addWeighted(np.absolute(grad_x), 0.5, np.absolute(grad_y), 0.5, 0)
    return grad

def local_normalize(im):
    width, _ = im.shape
    disksize = int(width/5)
    if disksize % 2 == 0:
        disksize = disksize + 1
    footprint = disk(disksize)
    im = rank.equalize(im, footprint=footprint)
    return im


def align_image(image, warp_matrix, dimension):
    image = resize_match(image, dimension)

    if warp_matrix.shape == (3, 3):
        return cv2.warpPerspective(image, warp_matrix, dimension)
    else:
        return cv2.warpAffine(image, warp_matrix, dimension)


def to_8bit(image, force_normalize=False):
    if not force_normalize and image.dtype == np.uint8:
        return image

    # Convert to 8bit
    try:
        data_range = np.iinfo(image.dtype)
        min_value = 0
        value_range = float(data_range.max) - float(data_range.min)
    except ValueError:
        # For floats use the actual range of the image values
        min_value = float(image.min())
        value_range = float(image.max()) - min_value

    image = image.astype(np.float32)
    image -= min_value
    image *= 255.0 / value_range
    np.around(image, out=image)
    image[image > 255] = 255
    image[image < 0] = 0
    image = image.astype(np.uint8)

    return image


def resize_match(image, dimension):
    h, w = image.shape[0], image.shape[1]
    mw, mh = dimension

    if w != mw or h != mh:
        fx = mw/w
        fy = mh/h
        image = cv2.resize(image, None, 
                fx=fx, 
                fy=fx,
                interpolation=(cv2.INTER_AREA if (fx < 1.0 and fy < 1.0) else cv2.INTER_LANCZOS4))

    return image
