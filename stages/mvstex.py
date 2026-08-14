import os, shutil

from opendm import log
from opendm import io
from opendm import multispectral
from opendm import system
from opendm import context
from opendm import types
from opendm.multispectral import get_primary_band_name
from opendm.photo import find_largest_photo_dim
from opendm.objpacker import obj_pack

class ODMMvsTexStage(types.ODM_Stage):
    def process(self, args, outputs):
        tree = outputs['tree']
        reconstruction = outputs['reconstruction']

        max_dim = find_largest_photo_dim(reconstruction.photos)
        max_texture_size = 8 * 1024 # default

        if max_dim > 8000:
            log.INFO("Large input images (%s pixels), increasing maximum texture size." % max_dim)
            max_texture_size *= 3

        class nonloc:
            runs = []

        def add_run(nvm_file, primary=True, band=None):
            subdir = ""
            if not primary and band is not None:
                subdir = band
            
            if not args.skip_3dmodel and (primary or args.use_3dmesh):
                nonloc.runs += [{
                    'out_dir': os.path.join(tree.odm_texturing, subdir),
                    'model': tree.odm_mesh,
                    'nadir': False,
                    'primary': primary,
                    'nvm_file': nvm_file,
                    'labeling_file': os.path.join(tree.odm_texturing, "odm_textured_model_geo_labeling.vec") if subdir else None
                }]

            if not args.use_3dmesh:
                nonloc.runs += [{
                    'out_dir': os.path.join(tree.odm_25dtexturing, subdir),
                    'model': tree.odm_25dmesh,
                    'nadir': True,
                    'primary': primary,
                    'nvm_file': nvm_file,
                    'labeling_file': os.path.join(tree.odm_25dtexturing, "odm_textured_model_geo_labeling.vec") if subdir else None
                }]

        if reconstruction.multi_camera:

            for band in reconstruction.multi_camera:
                primary = band['name'] == get_primary_band_name(reconstruction.multi_camera, args.primary_band)
                nvm_file = os.path.join(tree.opensfm, "undistorted", "reconstruction_%s.nvm" % band['name'].lower())
                add_run(nvm_file, primary, band['name'].lower())
            
            # Sort to make sure primary band is processed first
            nonloc.runs.sort(key=lambda r: r['primary'], reverse=True)
        else:
            add_run(tree.opensfm_reconstruction_nvm)
        
        # DJI multispectral seam leveling.
        #
        # Leveling runs once per band, so its corrections are per-band and can in
        # principle perturb the NIR/Red ratio. That was the reason both levelings
        # were forced off here. It was never measured, and the cost is visible:
        # with leveling off, each flight pass keeps its own radiometric level and
        # the mosaic shows banding along the flight lines. Pix4D's un-blended
        # export reproduces our banding exactly, and its blended export does not,
        # so blending -- not radiometry -- is what closes the gap.
        #
        # Mode is selectable so the tradeoff can be measured rather than assumed:
        #   off    both levelings skipped (previous behaviour)
        #   local  local (Poisson border) leveling only
        #   global global (per-vertex) leveling only
        #   full   both, as upstream ODM does for RGB
        dji_ms = reconstruction.multi_camera and \
            any(multispectral._is_m3m(p) for p in reconstruction.photos)
        seam_mode = os.environ.get("ODX_SEAM_LEVELING", "off").strip().lower()
        if seam_mode not in ("off", "local", "global", "full"):
            log.WARNING("Unknown ODX_SEAM_LEVELING=%s, using 'off'" % seam_mode)
            seam_mode = "off"
        if not dji_ms:
            seam_mode = None
        else:
            log.INFO("DJI multispectral: seam leveling mode '%s'" % seam_mode)

        progress_per_run = 100.0 / len(nonloc.runs)
        progress = 0.0

        for r in nonloc.runs:
            if not io.dir_exists(r['out_dir']):
                system.mkdir_p(r['out_dir'])

            odm_textured_model_obj = os.path.join(r['out_dir'], tree.odm_textured_model_obj)
            unaligned_obj = io.related_file_path(odm_textured_model_obj, postfix="_unaligned")

            if not io.file_exists(odm_textured_model_obj) or self.rerun():
                log.INFO('Writing MVS Textured file in: %s'
                              % odm_textured_model_obj)

                if os.path.isfile(unaligned_obj):
                    os.unlink(unaligned_obj)

                # Format arguments to fit Mvs-Texturing app
                skipGlobalSeamLeveling = ""
                skipLocalSeamLeveling = ""
                keepUnseenFaces = ""
                nadir = ""

                if args.texturing_skip_global_seam_leveling:
                    skipGlobalSeamLeveling = "--skip_global_seam_leveling"
                if seam_mode is not None:
                    if seam_mode in ("off", "local"):
                        skipGlobalSeamLeveling = "--skip_global_seam_leveling"
                    if seam_mode in ("off", "global"):
                        skipLocalSeamLeveling = "--skip_local_seam_leveling"
                if args.texturing_keep_unseen_faces:
                    keepUnseenFaces = "--keep_unseen_faces"
                if (r['nadir']):
                    nadir = '--nadir_mode'

                # mvstex definitions
                kwargs = {
                    'bin': context.mvstex_path,
                    'out_dir': os.path.join(r['out_dir'], "odm_textured_model_geo"),
                    'model': r['model'],
                    'dataTerm': 'gmi',
                    # gauss_clamping is a photometric outlier remover aimed at
                    # pedestrians/vehicles in urban scenes; on multispectral
                    # reflectance it manipulates values per band. texrecon's own
                    # default is none.
                    # Kept independent of the seam-leveling mode so only one
                    # variable changes between builds.
                    'outlierRemovalType': 'none' if dji_ms else 'gauss_clamping',
                    'skipGlobalSeamLeveling': skipGlobalSeamLeveling,
                    'skipLocalSeamLeveling': skipLocalSeamLeveling,
                    'keepUnseenFaces': keepUnseenFaces,
                    'toneMapping': 'none',
                    'nadirMode': nadir,
                    'numThreads': '--num_threads=%s' % args.max_concurrency,
                    'maxTextureSize': '--max_texture_size=%s' % max_texture_size,
                    'nvm_file': r['nvm_file'],
                    'intermediate': '--no_intermediate_results' if (r['labeling_file'] or not reconstruction.multi_camera) else '',
                    'labelingFile': '-L "%s"' % r['labeling_file'] if r['labeling_file'] else ''
                }

                mvs_tmp_dir = os.path.join(r['out_dir'], 'tmp')

                # mvstex creates a tmp directory, so make sure it is empty
                if io.dir_exists(mvs_tmp_dir):
                    log.INFO("Removing old tmp directory {}".format(mvs_tmp_dir))
                    shutil.rmtree(mvs_tmp_dir)

                # run texturing binary
                system.run('"{bin}" "{nvm_file}" "{model}" "{out_dir}" '
                        '-d {dataTerm} -o {outlierRemovalType} '
                        '-t {toneMapping} '
                        '{intermediate} '
                        '{skipGlobalSeamLeveling} '
                        '{skipLocalSeamLeveling} '
                        '{keepUnseenFaces} '
                        '{nadirMode} '
                        '{labelingFile} '
                        '{numThreads} '
                        '{maxTextureSize} '.format(**kwargs))

                if r['primary'] and (not r['nadir'] or args.skip_3dmodel):
                    # Single material?
                    if args.texturing_single_material:
                        log.INFO("Packing to single material")

                        packed_dir = os.path.join(r['out_dir'], 'packed')
                        if io.dir_exists(packed_dir):
                            log.INFO("Removing old packed directory {}".format(packed_dir))
                            shutil.rmtree(packed_dir)
                        
                        try:
                            obj_pack(os.path.join(r['out_dir'], tree.odm_textured_model_obj), packed_dir, _info=log.INFO)
                            
                            # Move packed/* into texturing folder
                            system.delete_files(r['out_dir'], (".vec", ))
                            system.move_files(packed_dir, r['out_dir'])
                            if os.path.isdir(packed_dir):
                                os.rmdir(packed_dir)
                        except Exception as e:
                            log.WARNING(str(e))


                # Backward compatibility: copy odm_textured_model_geo.mtl to odm_textured_model.mtl
                # for certain older WebODM clients which expect a odm_textured_model.mtl
                # to be present for visualization
                # We should remove this at some point in the future
                geo_mtl = os.path.join(r['out_dir'], 'odm_textured_model_geo.mtl')
                if io.file_exists(geo_mtl):
                    nongeo_mtl = os.path.join(r['out_dir'], 'odm_textured_model.mtl')
                    shutil.copy(geo_mtl, nongeo_mtl)

                progress += progress_per_run
                self.update_progress(progress)
            else:
                log.WARNING('Found a valid texture file in: %s'
                                % odm_textured_model_obj)
        
        if args.optimize_disk_space:
            for r in nonloc.runs:
                if io.file_exists(r['model']):
                    os.remove(r['model'])
            
            undistorted_images_path = os.path.join(tree.opensfm, "undistorted", "images")
            if io.dir_exists(undistorted_images_path):
                shutil.rmtree(undistorted_images_path)

