# Copyright (c) 2021, University of Washington
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# 3. Neither the name of the University of Washington nor the names of its
#    contributors may be used to endorse or promote products derived from this
#    software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE UNIVERSITY OF WASHINGTON AND CONTRIBUTORS
# “AS IS” AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
# TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
# PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE UNIVERSITY OF WASHINGTON OR
# CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
# EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
# PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
# OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
# WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
# OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF
# ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import itertools
import copy
import json
import ssl
import urllib.request
import datetime
import logging
import warnings
import numpy
import geopandas
from shapely.geometry.multipolygon import MultiPolygon
from shapely.geometry import Polygon
import sliderule


###############################################################################
# GLOBALS
###############################################################################

# create logger
logger = logging.getLogger(__name__)

# default maximum number of resources to process in one request
DEFAULT_MAX_REQUESTED_RESOURCES = 300
max_requested_resources = DEFAULT_MAX_REQUESTED_RESOURCES

# best effort match of datasets to providers and versions for earthdata
DATASETS = {
    "ATL03":                                {"provider": "NSIDC_ECS",   "version": "005"},
    "GEDI01_B":                             {"provider": "LPDAAC_ECS",  "version": "002"},
    "GEDI02_A":                             {"provider": "LPDAAC_ECS",  "version": "002"},
    "GEDI02_B":                             {"provider": "LPDAAC_ECS",  "version": "002"},
    "GEDI_L3_LandSurface_Metrics_V2_1952":  {"provider": "ORNL_CLOUD",  "version": None},
    "GEDI_L4A_AGB_Density_V2_1_2056":       {"provider": "ORNL_CLOUD",  "version": None},
    "GEDI_L4B_Gridded_Biomass_2017":        {"provider": "ORNL_CLOUD",  "version": None}
}

# page size for requests
CMR_PAGE_SIZE = 2000

###############################################################################
# NSIDC UTILITIES
###############################################################################
# The functions below have been adapted from the NSIDC download script and
# carry the following notice:
#
# Copyright (c) 2020 Regents of the University of Colorado
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the "Software"),
# to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense,
# and/or sell copies of the Software, and to permit persons to whom the
# Software is furnished to do so, subject to the following conditions:
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.

def __cmr_filter_urls(search_results):
    """Select only the desired data files from CMR response."""
    if 'feed' not in search_results or 'entry' not in search_results['feed']:
        return []
    entries = [e['links']
               for e in search_results['feed']['entry']
               if 'links' in e]
    # Flatten "entries" to a simple list of links
    links = list(itertools.chain(*entries))
    # Build unique filenames
    urls = []
    unique_filenames = set()
    for link in links:
        if 'href' not in link:
            # Exclude links with nothing to download
            continue
        if 'inherited' in link and link['inherited'] is True:
            # Why are we excluding these links?
            continue
        if 'rel' in link and 'data#' not in link['rel']:
            # Exclude links which are not classified by CMR as "data" or "metadata"
            continue
        if 'title' in link and 'opendap' in link['title'].lower():
            # Exclude OPeNDAP links--they are responsible for many duplicates
            # This is a hack; when the metadata is updated to properly identify
            # non-datapool links, we should be able to do this in a non-hack way
            continue
        filename = link['href'].split('/')[-1]
        if filename in unique_filenames:
            # Exclude links with duplicate filenames (they would overwrite)
            continue
        unique_filenames.add(filename)
        if ".h5" in link['href'][-3:]:
            resource = link['href'].split("/")[-1]
            urls.append(resource)
    # return filtered urls
    return urls

def __cmr_granule_metadata(search_results):
    """Get the metadata for CMR returned granules"""
    # GeoDataFrame with granule metadata
    granule_metadata = sliderule.emptyframe()
    # return empty dataframe if no CMR entries
    if 'feed' not in search_results or 'entry' not in search_results['feed']:
        return granule_metadata
    # for each CMR entry
    for e in search_results['feed']['entry']:
        # columns for dataframe
        columns = {}
        # granule title and identifiers
        columns['title'] = e['title']
        columns['collection_concept_id'] = e['collection_concept_id']
        # time start and time end of granule
        columns['time_start'] = numpy.datetime64(e['time_start'])
        columns['time_end'] = numpy.datetime64(e['time_end'])
        columns['time_updated'] = numpy.datetime64(e['updated'])
        # get the granule size and convert to bits
        columns['granule_size'] = float(e['granule_size'])*(2.0**20)
        # Create Pandas DataFrame object
        # use granule id as index
        df = geopandas.pd.DataFrame(columns, index=[e['id']])
        # Generate Geometry Column
        if 'polygons' in e:
            polygons = []
            # for each polygon
            for poly in e['polygons'][0]:
                coords = [float(i) for i in poly.split()]
                polygons.append(Polygon(zip(coords[1::2], coords[::2])))
            # generate multipolygon from list of polygons
            geometry = MultiPolygon(polygons)
        else:
            geometry, = geopandas.points_from_xy([None], [None])
        # Build GeoDataFrame (default geometry is crs=EPSG_MERCATOR)
        gdf = geopandas.GeoDataFrame(df, geometry=[geometry], crs=sliderule.EPSG_MERCATOR)
        # append to combined GeoDataFrame and catch warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            granule_metadata = granule_metadata.append(gdf)
    # return granule metadata
    # - time start and time end
    # - time granule was updated
    # - granule size in bits
    # - polygons as geodataframe geometry
    return granule_metadata

def __cmr_search(provider, short_name, version, time_start, time_end, **kwargs):
    """Perform a scrolling CMR query for files matching input criteria."""
    kwargs.setdefault('polygon',None)
    kwargs.setdefault('name_filter',None)
    kwargs.setdefault('return_metadata',False)
    # build params
    params = '&short_name={0}'.format(short_name)
    if version != None:
        params += '&version={0}'.format(version)
    params += '&temporal[]={0},{1}'.format(time_start, time_end)
    if kwargs['polygon']:
        params += '&polygon={0}'.format(kwargs['polygon'])
    if kwargs['name_filter']:
        params += '&options[producer_granule_id][pattern]=true'
        params += '&producer_granule_id[]=' + kwargs['name_filter']
    CMR_URL = 'https://cmr.earthdata.nasa.gov'
    cmr_query_url = ('{0}/search/granules.json?provider={1}'
                     '&sort_key[]=start_date&sort_key[]=producer_granule_id'
                     '&scroll=true&page_size={2}'.format(CMR_URL, provider, CMR_PAGE_SIZE))
    cmr_query_url += params
    logger.debug('cmr request={0}\n'.format(cmr_query_url))

    cmr_scroll_id = None
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    urls = []
    # GeoDataFrame with granule metadata
    metadata = sliderule.emptyframe()
    while True:
        req = urllib.request.Request(cmr_query_url)
        if cmr_scroll_id:
            req.add_header('cmr-scroll-id', cmr_scroll_id)
        response = urllib.request.urlopen(req, context=ctx)
        if not cmr_scroll_id:
            # Python 2 and 3 have different case for the http headers
            headers = {k.lower(): v for k, v in dict(response.info()).items()}
            cmr_scroll_id = headers['cmr-scroll-id']
        search_page = response.read()
        search_page = json.loads(search_page.decode('utf-8'))
        url_scroll_results = __cmr_filter_urls(search_page)
        if not url_scroll_results:
            break
        urls += url_scroll_results
        # query for granule metadata and polygons
        if kwargs['return_metadata']:
            metadata_results = __cmr_granule_metadata(search_page)
        else:
            metadata_results = [None for _ in url_scroll_results]
        # append granule metadata and catch warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            metadata = metadata.append(metadata_results)

    return (urls,metadata)


###############################################################################
# APIs
###############################################################################

#
#  Set Maximum Resources
#
def set_max_resources (max_resources):
    '''
    Sets the maximum allowed number of resources to be processed in one request.  This is mainly provided as a sanity check for the user.

    Parameters
    ----------
        max_resources : int
                        the maximum number of resources that are allowed to be processed in a single request

    Examples
    --------
        >>> from sliderule import icesat2
        >>> icesat2.set_max_resources(1000)
    '''
    global max_requested_resources
    max_requested_resources = max_resources

#
#  Common Metadata Repository
#
def cmr(provider=None, short_name=None, version=None, polygon=None, time_start='2018-01-01T00:00:00Z', time_end=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), return_metadata=False, name_filter=None):
    '''
    Query the `NASA Common Metadata Repository (CMR) <https://cmr.earthdata.nasa.gov/search>`_ for a list of data within temporal and spatial parameters

    Parameters
    ----------
        polygon:    list
                    either a single list of longitude,latitude in counter-clockwise order with first and last point matching, defining region of interest (see `polygons </rtd/user_guide/ICESat-2.html#id1>`_), or a list of such lists when the region includes more than one polygon
        time_start: str
                    starting time for query in format ``<year>-<month>-<day>T<hour>:<minute>:<second>Z``
        time_end:   str
                    ending time for query in format ``<year>-<month>-<day>T<hour>:<minute>:<second>Z``
        version:    str
                    dataset version as found in the `NASA CMR Directory <https://cmr.earthdata.nasa.gov/search/site/collections/directory/eosdis>`_
        short_name: str
                    dataset short name as defined in the `NASA CMR Directory <https://cmr.earthdata.nasa.gov/search/site/collections/directory/eosdis>`_

    Returns
    -------
    list
        files (granules) for the dataset fitting the spatial and temporal parameters

    Examples
    --------
        >>> from sliderule import icesat2
        >>> region = [ {"lon": -108.3435200747503, "lat": 38.89102961045247},
        ...            {"lon": -107.7677425431139, "lat": 38.90611184543033},
        ...            {"lon": -107.7818591266989, "lat": 39.26613714985466},
        ...            {"lon": -108.3605610678553, "lat": 39.25086131372244},
        ...            {"lon": -108.3435200747503, "lat": 38.89102961045247} ]
        >>> granules = icesat2.cmr(polygon=region)
        >>> granules
        ['ATL03_20181017222812_02950102_003_01.h5', 'ATL03_20181110092841_06530106_003_01.h5', ... 'ATL03_20201111102237_07370902_003_01.h5']
    '''
    # check parameters
    if short_name == None:
        raise sliderule.FatalError("Must supply short name to CMR query")

    # attempt to fil in provider
    if provider == None:
        if short_name in DATASETS:
            provider = DATASETS[short_name]["provider"]
        else:
            raise sliderule.FatalError("Unable to determine provider for CMR query")

    # attempt to fil in provider
    if version == None:
        if short_name in DATASETS:
            version = DATASETS[short_name]["version"]
        else:
            raise sliderule.FatalError("Unable to determine version for CMR query")

    # initialize return value
    resources = {} # [<url>] = <polygon>

    # create list of polygons
    polygons = [None]
    if polygon and len(polygon) > 0:
        if type(polygon[0]) == dict:
            polygons = [copy.deepcopy(polygon)]
        elif type(polygon[0] == list):
            polygons = copy.deepcopy(polygon)

    # iterate through each polygon (or none if none supplied)
    for polygon in polygons:
        urls = []
        metadata = sliderule.emptyframe()

        # issue CMR request
        for tolerance in [0.0001, 0.001, 0.01, 0.1, 1.0, None]:

            # convert polygon list into string
            polystr = None
            if polygon:
                flatpoly = []
                for p in polygon:
                    flatpoly.append(p["lon"])
                    flatpoly.append(p["lat"])
                polystr = str(flatpoly)[1:-1]
                polystr = polystr.replace(" ", "") # remove all spaces as this will be embedded in a url

            # call into NSIDC routines to make CMR request
            try:
                urls,metadata = __cmr_search(provider, short_name, version, time_start, time_end, polygon=polystr, return_metadata=return_metadata, name_filter=name_filter)
                break # exit loop because cmr search was successful
            except urllib.error.HTTPError as e:
                logger.error('HTTP Request Error: {}'.format(e.reason))
            except RuntimeError as e:
                logger.error("Runtime Error:", e)

            # simplify polygon
            if polygon and tolerance:
                raw_multi_polygon = [[(tuple([(c['lon'], c['lat']) for c in polygon]), [])]]
                shape = MultiPolygon(*raw_multi_polygon)
                buffered_shape = shape.buffer(tolerance)
                simplified_shape = buffered_shape.simplify(tolerance)
                simplified_coords = list(simplified_shape.exterior.coords)
                logger.warning('Using simplified polygon (for CMR request only!), {} points using tolerance of {}'.format(len(simplified_coords), tolerance))
                region = []
                for coord in simplified_coords:
                    point = {"lon": coord[0], "lat": coord[1]}
                    region.insert(0,point)
                polygon = region
            else:
                break # exit here because nothing can be done

        # populate resources
        for i,url, in enumerate(urls):
            resources[url] = metadata.iloc[i]

    # build return lists
    url_list = list(resources.keys())
    meta_list = list(resources.values())

    if return_metadata:
        return (url_list,meta_list)
    else:
        return url_list