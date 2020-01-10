import argparse
from builtins import input
import datetime
import logging
import math
import pprint
import sys

import ee

import openet.ssebop as ssebop
import utils
# from . import utils


def main(ini_path=None, overwrite_flag=False, delay_time=0, gee_key_file=None,
         max_ready=-1, cron_flag=False, reverse_flag=False, update_flag=False):
    """Compute scene Tcorr images by WRS2 tile

    Parameters
    ----------
    ini_path : str
        Input file path.
    overwrite_flag : bool, optional
        If True, overwrite existing files if the export dates are the same and
        generate new images (but with different export dates) even if the tile
        lists are the same.  The default is False.
    delay_time : float, optional
        Delay time in seconds between starting export tasks (or checking the
        number of queued tasks, see "max_ready" parameter).  The default is 0.
    gee_key_file : str, None, optional
        Earth Engine service account JSON key file (the default is None).
    max_ready: int, optional
        Maximum number of queued "READY" tasks.  The default is -1 which is
        implies no limit to the number of tasks that will be submitted.
    cron_flag: bool, optional
        Not currently implemented.
    reverse_flag : bool, optional
        If True, process WRS2 tiles and dates in reverse order.
    update_flag : bool, optional
        If True, only overwrite scenes with an older model version.

    """
    logging.info('\nCompute scene Tcorr images by WRS2 tile')

    ini = utils.read_ini(ini_path)

    model_name = 'SSEBOP'
    # model_name = ini['INPUTS']['et_model'].upper()

    tmax_name = ini[model_name]['tmax_source']

    export_id_fmt = 'tcorr_scene_{product}_{scene_id}'
    asset_id_fmt = '{coll_id}/{scene_id}'

    tcorr_scene_coll_id = '{}/{}_scene'.format(
        ini['EXPORT']['export_coll'], tmax_name.lower())

    wrs2_coll_id = 'projects/earthengine-legacy/assets/' \
                   'projects/usgs-ssebop/wrs2_descending_custom'
    wrs2_tile_field = 'WRS2_TILE'
    wrs2_path_field = 'ROW'
    wrs2_row_field = 'PATH'

    try:
        wrs2_tiles = str(ini['INPUTS']['wrs2_tiles'])
        wrs2_tiles = sorted([x.strip() for x in wrs2_tiles.split(',')])
    except KeyError:
        wrs2_tiles = []
        logging.debug('  wrs2_tiles: not set in INI, defaulting to []')
    except Exception as e:
        raise e

    try:
        study_area_extent = str(ini['INPUTS']['study_area_extent']) \
            .replace('[', '').replace(']', '').split(',')
        study_area_extent = [float(x.strip()) for x in study_area_extent]
    except KeyError:
        study_area_extent = None
        logging.debug('  study_area_extent: not set in INI')
    except Exception as e:
        raise e

    # TODO: Add try/except blocks and default values?
    collections = [x.strip() for x in ini['INPUTS']['collections'].split(',')]
    cloud_cover = float(ini['INPUTS']['cloud_cover'])
    min_pixel_count = float(ini['TCORR']['min_pixel_count'])
    # min_scene_count = float(ini['TCORR']['min_scene_count'])

    if (tmax_name.upper() == 'CIMIS' and
            ini['INPUTS']['end_date'] < '2003-10-01'):
        logging.error(
            '\nCIMIS is not currently available before 2003-10-01, exiting\n')
        sys.exit()
    elif (tmax_name.upper() == 'DAYMET' and
            ini['INPUTS']['end_date'] > '2018-12-31'):
        logging.warning(
            '\nDAYMET is not currently available past 2018-12-31, '
            'using median Tmax values\n')
        # sys.exit()
    # elif (tmax_name.upper() == 'TOPOWX' and
    #         ini['INPUTS']['end_date'] > '2017-12-31'):
    #     logging.warning(
    #         '\nDAYMET is not currently available past 2017-12-31, '
    #         'using median Tmax values\n')
    #     # sys.exit()


    # Extract the model keyword arguments from the INI
    # Set the property name to lower case and try to cast values to numbers
    model_args = {
        k.lower(): float(v) if utils.is_number(v) else v
        for k, v in dict(ini[model_name]).items()}
    # et_reference_args = {
    #     k: model_args.pop(k)
    #     for k in [k for k in model_args.keys() if k.startswith('et_reference_')]}


    logging.info('\nInitializing Earth Engine')
    if gee_key_file:
        logging.info('  Using service account key file: {}'.format(gee_key_file))
        # The "EE_ACCOUNT" parameter is not used if the key file is valid
        ee.Initialize(ee.ServiceAccountCredentials('x', key_file=gee_key_file),
                      use_cloud_api=True)
    else:
        ee.Initialize(use_cloud_api=True)


    # Get a Tmax image to set the Tcorr values to
    logging.debug('\nTmax properties')
    tmax_source = tmax_name.split('_', 1)[0]
    tmax_version = tmax_name.split('_', 1)[1]
    if 'MEDIAN' in tmax_name.upper():
        tmax_coll_id = 'projects/earthengine-legacy/assets/' \
                       'projects/usgs-ssebop/tmax/{}'.format(tmax_name.lower())
        tmax_coll = ee.ImageCollection(tmax_coll_id)
        tmax_mask = ee.Image(tmax_coll.first()).select([0]).multiply(0)
    else:
        # TODO: Add support for non-median tmax sources
        raise ValueError('unsupported tmax_source: {}'.format(tmax_name))
    logging.debug('  Collection: {}'.format(tmax_coll_id))
    logging.debug('  Source:  {}'.format(tmax_source))
    logging.debug('  Version: {}'.format(tmax_version))


    logging.debug('\nExport properties')
    export_info = utils.get_info(ee.Image(tmax_mask))
    if 'daymet' in tmax_name.lower():
        # Custom smaller extent for DAYMET focused on CONUS
        export_extent = [-1999750, -1890500, 2500250, 1109500]
        export_shape = [4500, 3000]
        export_geo = [1000, 0, -1999750, 0, -1000, 1109500]
        # Custom medium extent for DAYMET of CONUS, Mexico, and southern Canada
        # export_extent = [-2099750, -3090500, 2900250, 1909500]
        # export_shape = [5000, 5000]
        # export_geo = [1000, 0, -2099750, 0, -1000, 1909500]
        export_crs = export_info['bands'][0]['crs']
    else:
        export_crs = export_info['bands'][0]['crs']
        export_geo = export_info['bands'][0]['crs_transform']
        export_shape = export_info['bands'][0]['dimensions']
        # export_geo = ee.Image(tmax_mask).projection().getInfo()['transform']
        # export_crs = ee.Image(tmax_mask).projection().getInfo()['crs']
        # export_shape = ee.Image(tmax_mask).getInfo()['bands'][0]['dimensions']
        export_extent = [
            export_geo[2], export_geo[5] + export_shape[1] * export_geo[4],
            export_geo[2] + export_shape[0] * export_geo[0], export_geo[5]]
    export_geom = ee.Geometry.Rectangle(
        export_extent, proj=export_crs,  geodesic=False)
    logging.debug('  CRS: {}'.format(export_crs))
    logging.debug('  Extent: {}'.format(export_extent))
    logging.debug('  Geo: {}'.format(export_geo))
    logging.debug('  Shape: {}'.format(export_shape))


    if study_area_extent is None:
        if 'daymet' in tmax_name.lower():
            # CGM - For now force DAYMET to a slightly smaller "CONUS" extent
            study_area_extent = [-125, 25, -65, 49]
            # study_area_extent =  [-125, 25, -65, 52]
        elif 'cimis' in tmax_name.lower():
            study_area_extent = [-124, 35, -119, 42]
        else:
            # TODO: Make sure output from bounds is in WGS84
            study_area_extent = tmax_mask.geometry().bounds().getInfo()
        logging.debug(f'\nStudy area extent not set in INI, '
                      f'default to {study_area_extent}')
    study_area_geom = ee.Geometry.Rectangle(
        study_area_extent, proj='EPSG:4326', geodesic=False)


    # For now define the study area from an extent
    if study_area_extent:
        study_area_geom = ee.Geometry.Rectangle(
            study_area_extent, proj='EPSG:4326', geodesic=False)
        export_geom = export_geom.intersection(study_area_geom, 1)
        # logging.debug('  Extent: {}'.format(export_geom.bounds().getInfo()))


    # If cell_size parameter is set in the INI,
    # adjust the output cellsize and recompute the transform and shape
    try:
        export_cs = float(ini['EXPORT']['cell_size'])
        export_shape = [
            int(math.ceil(abs((export_shape[0] * export_geo[0]) / export_cs))),
            int(math.ceil(abs((export_shape[1] * export_geo[4]) / export_cs)))]
        export_geo = [export_cs, 0.0, export_geo[2], 0.0, -export_cs, export_geo[5]]
        logging.debug('  Custom export cell size: {}'.format(export_cs))
        logging.debug('  Geo: {}'.format(export_geo))
        logging.debug('  Shape: {}'.format(export_shape))
    except KeyError:
        pass

    if not ee.data.getInfo(tcorr_scene_coll_id):
        logging.info('\nExport collection does not exist and will be built'
                     '\n  {}'.format(tcorr_scene_coll_id))
        input('Press ENTER to continue')
        ee.data.createAsset({'type': 'IMAGE_COLLECTION'}, tcorr_scene_coll_id)

    # Get current asset list
    logging.debug('\nGetting GEE asset list')
    asset_list = utils.get_ee_assets(tcorr_scene_coll_id)
    # if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
    #     pprint.pprint(asset_list[:10])

    # Get current running tasks
    tasks = utils.get_ee_tasks()
    if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
        logging.debug('  Tasks: {}\n'.format(len(tasks)))
        input('ENTER')


    # TODO: Decide if month and year lists should be applied to scene exports
    # # Limit by year and month
    # try:
    #     month_list = sorted(list(utils.parse_int_set(ini['TCORR']['months'])))
    # except:
    #     logging.info('\nTCORR "months" parameter not set in the INI,'
    #                  '\n  Defaulting to all months (1-12)\n')
    #     month_list = list(range(1, 13))
    # try:
    #     year_list = sorted(list(utils.parse_int_set(ini['TCORR']['years'])))
    # except:
    #     logging.info('\nTCORR "years" parameter not set in the INI,'
    #                  '\n  Defaulting to all available years\n')
    #     year_list = []


    # if cron_flag:
    #     # CGM - This seems like a silly way of getting the date as a datetime
    #     #   Why am I doing this and not using the commented out line?
    #     end_dt = datetime.date.today().strftime('%Y-%m-%d')
    #     end_dt = datetime.datetime.strptime(end_dt, '%Y-%m-%d')
    #     end_dt = end_dt + datetime.timedelta(days=-4)
    #     # end_dt = datetime.datetime.today() + datetime.timedelta(days=-1)
    #     start_dt = end_dt + datetime.timedelta(days=-64)
    # else:
    #     start_dt = datetime.datetime.strptime(
    #         ini['INPUTS']['start_date'], '%Y-%m-%d')
    #     end_dt = datetime.datetime.strptime(
    #         ini['INPUTS']['end_date'], '%Y-%m-%d')
    start_dt = datetime.datetime.strptime(
        ini['INPUTS']['start_date'], '%Y-%m-%d')
    end_dt = datetime.datetime.strptime(
        ini['INPUTS']['end_date'], '%Y-%m-%d')
    if end_dt >= datetime.datetime.today():
        logging.debug('End Date:   {} - setting end date to current '
                      'date'.format(end_dt.strftime('%Y-%m-%d')))
        end_dt = datetime.datetime.today()
    if start_dt < datetime.datetime(1984, 3, 23):
        logging.debug('Start Date: {} - no Landsat 5+ images before '
                      '1984-03-23'.format(start_dt.strftime('%Y-%m-%d')))
        start_dt = datetime.datetime(1984, 3, 23)
    start_date = start_dt.strftime('%Y-%m-%d')
    end_date = end_dt.strftime('%Y-%m-%d')
    # next_date = (start_dt + datetime.timedelta(days=1)).strftime('%Y-%m-%d')
    logging.debug('Start Date: {}'.format(start_date))
    logging.debug('End Date:   {}\n'.format(end_date))
    if start_dt > end_dt:
        raise ValueError('start date must be before end date')


    # Get the list of WRS2 tiles that intersect the data area and study area
    wrs2_coll = ee.FeatureCollection(wrs2_coll_id) \
        .filterBounds(export_geom) \
        .filterBounds(study_area_geom)
    if wrs2_tiles:
        wrs2_coll = wrs2_coll.filter(ee.Filter.inList(wrs2_tile_field, wrs2_tiles))
    wrs2_info = wrs2_coll.getInfo()['features']
    # pprint.pprint(wrs2_info)
    # input('ENTER')


    # Iterate over WRS2 tiles (default is from west to east)
    for wrs2_ftr in sorted(wrs2_info, key=lambda k: k['properties']['WRS2_TILE'],
                           reverse=not(reverse_flag)):
        wrs2_tile = wrs2_ftr['properties'][wrs2_tile_field]
        logging.info('{}'.format(wrs2_tile))

        wrs2_path = int(wrs2_tile[1:4])
        wrs2_row = int(wrs2_tile[5:8])
        # wrs2_path = wrs2_ftr['properties']['PATH']
        # wrs2_row = wrs2_ftr['properties']['ROW']

        wrs2_filter = [
            {'type': 'equals', 'leftField': 'WRS_PATH', 'rightValue': wrs2_path},
            {'type': 'equals', 'leftField': 'WRS_ROW', 'rightValue': wrs2_row}]
        filter_args = {c: wrs2_filter for c in collections}

        # Build and merge the Landsat collections
        model_obj = ssebop.Collection(
            collections=collections,
            start_date=start_date,
            end_date=end_date,
            cloud_cover_max=cloud_cover,
            geometry=ee.Geometry(wrs2_ftr['geometry']),
            model_args=model_args,
            filter_args=filter_args,
        )
        landsat_coll = model_obj.overpass(variables=['ndvi'])
        # pprint.pprint(landsat_coll.aggregate_array('system:id').getInfo())
        # input('ENTER')

        try:
            image_id_list = landsat_coll.aggregate_array('system:id').getInfo()
        except Exception as e:
            logging.warning('  Error getting image ID list, skipping tile')
            logging.debug(f'  {e}')
            continue

        if update_flag:
            assets_info = utils.get_info(
                ee.ImageCollection(tcorr_scene_coll_id)
                    .filterMetadata('wrs2_tile', 'equals', wrs2_tile)
                    .filterDate(start_date, end_date))
            asset_props = {
                f'{tcorr_scene_coll_id}/{x["properties"]["system:index"]}':
                    x['properties']
                for x in assets_info['features']}
        else:
            asset_props = {}

        # Sort by date
        for image_id in sorted(image_id_list,
                               key=lambda k: k.split('/')[-1].split('_')[-1],
                               reverse=reverse_flag):
            scene_id = image_id.split('/')[-1]
            logging.info(f'{scene_id}')

            export_dt = datetime.datetime.strptime(scene_id.split('_')[-1], '%Y%m%d')
            export_date = export_dt.strftime('%Y-%m-%d')
            # next_date = (export_dt + datetime.timedelta(days=1)).strftime('%Y-%m-%d')

            # # Uncomment to apply month and year list filtering
            # if month_list and export_dt.month not in month_list:
            #     logging.debug(f'  Date: {export_date} - month not in INI - skipping')
            #     continue
            # elif year_list and export_dt.year not in year_list:
            #     logging.debug(f'  Date: {export_date} - year not in INI - skipping')
            #     continue

            logging.debug(f'  Date: {export_date}')

            export_id = export_id_fmt.format(
                product=tmax_name.lower(), scene_id=scene_id)
            logging.debug(f'  Export ID: {export_id}')

            asset_id = asset_id_fmt.format(
                coll_id=tcorr_scene_coll_id, scene_id=scene_id)
            logging.debug(f'  Asset ID: {asset_id}')

            if update_flag:
                def version_number(version_str):
                    return list(map(int, version_str.split('.')))

                if export_id in tasks.keys():
                    logging.info('  Task already submitted, skipping')
                    continue
                # In update mode only overwrite if the version is old
                if asset_props and asset_id in asset_props.keys():
                    model_ver = version_number(ssebop.__version__)
                    asset_ver = version_number(
                        asset_props[asset_id]['model_version'])

                    if asset_ver < model_ver:
                        logging.info('  Asset model version is old, removing')
                        try:
                            ee.data.deleteAsset(asset_id)
                        except:
                            logging.info('  Error removing asset, skipping')
                            continue
                    else:
                        logging.info('  Asset is up to date, skipping')
                        continue
            elif overwrite_flag:
                if export_id in tasks.keys():
                    logging.debug('  Task already submitted, cancelling')
                    ee.data.cancelTask(tasks[export_id]['id'])
                # This is intentionally not an "elif" so that a task can be
                # cancelled and an existing image/file/asset can be removed
                if asset_id in asset_list:
                    logging.debug('  Asset already exists, removing')
                    ee.data.deleteAsset(asset_id)
            else:
                if export_id in tasks.keys():
                    logging.debug('  Task already submitted, exiting')
                    continue
                elif asset_id in asset_list:
                    logging.debug('  Asset already exists, skipping')
                    continue

            image = ee.Image(image_id)
            # TODO: Will need to be changed for SR or use from_image_id()
            t_obj = ssebop.Image.from_landsat_c1_toa(image_id, **model_args)
            t_stats = ee.Dictionary(t_obj.tcorr_stats) \
                .combine({'tcorr_p5': 0, 'tcorr_count': 0}, overwrite=False)
            tcorr = ee.Number(t_stats.get('tcorr_p5'))
            count = ee.Number(t_stats.get('tcorr_count'))

            # Write an empty image if the pixel count is too low
            tcorr_img = ee.Algorithms.If(
                count.gt(min_pixel_count),
                tmax_mask.add(tcorr),
                tmax_mask.updateMask(0))

            # Clip to the Landsat image footprint
            output_img = ee.Image(tcorr_img).clip(image.geometry())

            # Clear the transparency mask
            output_img = output_img.updateMask(output_img.unmask(0)) \
                .rename(['tcorr']) \
                .set({
                    'CLOUD_COVER': image.get('CLOUD_COVER'),
                    'CLOUD_COVER_LAND': image.get('CLOUD_COVER_LAND'),
                    # 'SPACECRAFT_ID': image.get('SPACECRAFT_ID'),
                    'coll_id': image_id.split('/')[0],
                    'count': count,
                    # 'cycle_day': ((export_dt - cycle_base_dt).days % 8) + 1,
                    'date_ingested': datetime.datetime.today().strftime('%Y-%m-%d'),
                    'date': export_dt.strftime('%Y-%m-%d'),
                    'doy': int(export_dt.strftime('%j')),
                    'model_name': model_name,
                    'model_version': ssebop.__version__,
                    'month': int(export_dt.month),
                    'scene_id': image_id.split('/')[-1],
                    'system:time_start': image.get('system:time_start'),
                    'tcorr': tcorr,
                    'tmax_source': tmax_source.upper(),
                    'tmax_version': tmax_version.upper(),
                    'wrs2_path': wrs2_path,
                    'wrs2_row': wrs2_row,
                    'wrs2_tile': wrs2_tile,
                    'year': int(export_dt.year),
                })
            # pprint.pprint(output_img.getInfo()['properties'])
            # input('ENTER')

            logging.debug('  Building export task')
            task = ee.batch.Export.image.toAsset(
                image=output_img,
                description=export_id,
                assetId=asset_id,
                crs=export_crs,
                crsTransform='[' + ','.join(list(map(str, export_geo))) + ']',
                dimensions='{0}x{1}'.format(*export_shape),
            )

            logging.info('  Starting export task')
            utils.ee_task_start(task)

        # Pause before starting the next date (not export task)
        utils.delay_task(delay_time, max_ready)
        logging.debug('')


def arg_parse():
    """"""
    parser = argparse.ArgumentParser(
        description='Compute/export scene Tcorr images by WRS2 tile',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '-i', '--ini', type=utils.arg_valid_file,
        help='Input file', metavar='FILE')
    parser.add_argument(
        '--delay', default=0, type=float,
        help='Delay (in seconds) between each export tasks')
    parser.add_argument(
        '--key', type=utils.arg_valid_file, metavar='FILE',
        help='JSON key file')
    parser.add_argument(
        '--ready', default=-1, type=int,
        help='Maximum number of queued READY tasks')
    parser.add_argument(
        '--cron', default=False, action='store_true',
        help='Cron mode')
    parser.add_argument(
        '--reverse', default=False, action='store_true',
        help='Process scenes/dates in reverse order')
    parser.add_argument(
        '--update', default=False, action='store_true',
        help='Update images with older model version numbers')
    parser.add_argument(
        '-o', '--overwrite', default=False, action='store_true',
        help='Force overwrite of existing files')
    parser.add_argument(
        '-d', '--debug', default=logging.INFO, const=logging.DEBUG,
        help='Debug level logging', action='store_const', dest='loglevel')
    args = parser.parse_args()

    return args


if __name__ == "__main__":
    args = arg_parse()

    logging.basicConfig(level=args.loglevel, format='%(message)s')
    logging.getLogger('googleapiclient').setLevel(logging.ERROR)

    main(ini_path=args.ini, overwrite_flag=args.overwrite,
         delay_time=args.delay, gee_key_file=args.key, max_ready=args.ready,
         cron_flag=args.cron, reverse_flag=args.reverse,
         update_flag=args.update)