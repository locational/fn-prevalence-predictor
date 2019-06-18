import json
import sys
import uuid

import numpy as np
import pandas as pd
import geopandas as gp
import requests
import disarm_gears


def run_function(params: dict):
    #
    # 1. Handle input
    #

    # Set random seed
    np.random.seed(1000)

    # redirecting STDOUT to avoid over-chatty PyGAM
    original = sys.stdout
    sys.stdout = open('dummy-stdout-file', 'w')

    layer_names = params.get('layer_names')
    exceedance_threshold = params.get('exceedance_threshold')
    point_data = params.get('point_data')

    # Make a GeoPandas DataFrame
    gdf = gp.GeoDataFrame.from_features(point_data['features'])
    # TODO: Stop faking the lat/lng - need to fix the mgcv formula below, and also the failing pandas2ri.DataFrame
    gdf['lat'] = gdf.geometry.y
    gdf['lng'] = gdf.geometry.x

    # Use some ~~unlikely~~ IMPOSSIBLE to collide column name
    id_column_name = f'ID{uuid.uuid4().int}_hard_to_collide_id'
    if id_column_name in gdf.columns:
        id_column_name = f'ID_seriously_{uuid.uuid4().int}_hard_to_collide_id'
    gdf[id_column_name] = list(range(len(gdf)))

    # TODO: Fix this hack, use GeoPandas DataFrame throughout (except for pandas2ri.DataFrame)
    input_data = pd.DataFrame(gdf[[col for col in gdf.columns if col != gdf._geometry_column_name]])


    #
    # 2. Process
    #

    # Drop NA coordinates
    # TODO: Check if Geopandas allows creating of a GeoDataFrame if some of the geoms are empty - would be a separate issue of checking params if not
    input_data.dropna(axis=0, subset=['lng', 'lat']) # TODO: this does nothing: should be catching return

    # Find covariates
    if layer_names is not None:
        # Call fn-covariate-extractor
        open_faas_link = 'http://faas.srv.disarm.io/function/fn-covariate-extractor'
        just_id_and_geom = gdf.filter(['geometry', id_column_name])
        req_options = {
            'points': json.loads(just_id_and_geom.to_json()),  # TODO: Need to to-and-from JSON here?
            'layer_names': layer_names
        }
        covs_response = requests.post(open_faas_link, json=req_options)
        # TODO define how to handle NA entries in the covariates

        covs_response_json = covs_response.json()
        if covs_response_json['type'] == 'error':
            msg = "Problem with remote function call: " + covs_response_json['result']
            raise Exception(msg)
        # Merge output into input_data
        # covs_data = disarm_gears.util.geojson_decoder_1(covs_response.json()['result'])
        covs_result = covs_response.json()['result']
        covs_gdf = gp.GeoDataFrame.from_features(covs_result['features'])
        covs_data = pd.DataFrame(covs_gdf[[col for col in covs_gdf.columns if col != covs_gdf._geometry_column_name]])
        covs_data = covs_data.drop('id', axis = 1) # TODO: Get fn-cov-extr to not return an `id` col
        input_data = pd.merge(input_data, covs_data, how='left', left_on=[id_column_name], right_on=[id_column_name])

    # Define mgcv model
    # TODO: Fix formula to use GeoPandas `geometry` column (e.g. `geometry.x`?)
    gam_formula = "cbind(n_positive, n_trials - n_positive) ~ te(lng, lat, bs='gp', m=c(2), k=-1)"
    if layer_names is not None:
        gam_formula = [gam_formula] + ['s(%s)' % i for i in layer_names]
        gam_formula = '+'.join(gam_formula)

    # Fit model and make predictions/simulations
    train_data = input_data.dropna(axis=0)
    gam = disarm_gears.r_plugins.mgcv_fit(gam_formula, family='binomial', data=train_data)
    gam_pred = disarm_gears.r_plugins.mgcv_predict(gam, data=input_data, response_type='response')
    link_sims = disarm_gears.r_plugins.mgcv_posterior_samples(gam, data=input_data, n_samples=200,
                                                              response_type='link')

    # Credible interval
    bci = np.percentile(link_sims, q=[2.5, 97.5], axis=0)
    bci = 1. / (1. + np.exp(-bci))

    # Exceedance probability
    ex_prob = None
    ex_uncert = None

    if exceedance_threshold is not None:
        link_threshold = np.log(exceedance_threshold / (1 - exceedance_threshold))
        ex_prob = (link_sims > link_threshold).mean(axis=0)
        ex_uncert = 0.5 - abs(ex_prob - 0.5)

    #
    # 3. Package output
    #
    input_data['prevalence_prediction'] = gam_pred
    input_data['prevalence_bci_width'] = bci[1] - bci[0]
    input_data['exceedance_probability'] = ex_prob
    input_data['exceedance_uncertainty'] = ex_uncert

    output_gdf = gp.GeoDataFrame(input_data, geometry=gp.points_from_xy(input_data.lng, input_data.lat))
    slimmer_gdf = output_gdf.drop(['lat', 'lng', id_column_name], axis=1)

    # Restore STDOUT
    sys.stdout = original

    # return response.get('point_data')
    return json.loads(slimmer_gdf.to_json())