# -*- coding: utf-8 -*-
"""
    Season posters from TMDb

    Netflix does not provide an image for a season, so the posters are looked up on TMDb (optional,
    it requires a TMDb API key in the add-on settings).

    SPDX-License-Identifier: MIT
    See LICENSES/MIT.md for more information.
"""
import time

import requests

from resources.lib.common.cache_utils import CACHE_METADATA
from resources.lib.common.exceptions import CacheMiss
from resources.lib.globals import G
from resources.lib.utils.logging import LOG

TMDB_API_URL = 'https://api.themoviedb.org/3'
TMDB_IMAGE_URL = 'https://image.tmdb.org/t/p/w500'
TMDB_TIMEOUT_SECS = 6
CACHE_PREFIX = 'tmdb_season_posters_v1_'
NOT_FOUND_TTL_SECS = 7 * 24 * 3600
RETRY_AFTER_FAILURE_SECS = 600
_retry_not_before = 0  # No TMDb requests until this time, avoids slow retries with a wrong key or no network
_failed_key = ''  # The pause is valid only for the key that failed, a new key is tried immediately


def get_season_posters(videoid_tvshow, title, year, language, seasons_count):
    """
    Return {season number: poster url} of a tv show, an empty dict when not available.
    The result is cached, so TMDb is requested only once for each tv show.
    :param videoid_tvshow: the Netflix id of the tv show (for the cache identifier)
    :param title: title of the tv show
    :param year: year of the tv show (can be empty)
    :param language: language code of the profile, like 'cs-CZ'
    :param seasons_count: number of the seasons that Netflix has for the tv show
    """
    global _retry_not_before, _failed_key  # pylint: disable=global-statement
    api_key = (G.ADDON.getSetting('tmdb_api_key') or '').strip()
    if not api_key:
        LOG.debug('TMDb season posters: no API key set in the settings')
        return {}
    if not title or (api_key == _failed_key and time.time() < _retry_not_before):
        return {}
    identifier = f'{CACHE_PREFIX}{videoid_tvshow}_{language}'
    try:
        posters = G.CACHE.get(CACHE_METADATA, identifier)
    except CacheMiss:
        try:
            posters = _request_season_posters(api_key, title, year, language)
        except (requests.exceptions.RequestException, ValueError, KeyError, TypeError) as exc:
            # Not cached, retried after a pause (a wrong key or a missing network would slow down every list)
            _retry_not_before = time.time() + RETRY_AFTER_FAILURE_SECS
            _failed_key = api_key
            LOG.warn('TMDb season posters not available: {}', exc)
            return {}
        LOG.info('TMDb season posters for "{}": {} found', title, len(posters))
        G.CACHE.add(CACHE_METADATA, identifier, posters, ttl=None if posters else NOT_FOUND_TTL_SECS)
    # The numbering of TMDb is used for the seasons, when Netflix splits a season in more parts the
    # numbering would not match, in this case it is better to show no image than a wrong one
    if posters and seasons_count > max(posters):
        LOG.debug('TMDb season posters skipped, the seasons of Netflix ({}) do not match the ones of TMDb ({})',
                  seasons_count, max(posters))
        return {}
    return posters


def _request_season_posters(api_key, title, year, language):
    headers = {}
    params = {'language': language}
    if len(api_key) > 40:
        # The "API Read Access Token" of TMDb
        headers['Authorization'] = f'Bearer {api_key}'
    else:
        params['api_key'] = api_key
    tmdb_id = _search_tv_id(title, year, params, headers)
    if not tmdb_id:
        LOG.debug('TMDb: tv show "{}" not found', title)
        return {}
    details = _get(f'/tv/{tmdb_id}', params, headers)
    posters = {}
    for season in details.get('seasons') or []:
        number = season.get('season_number')
        if number and season.get('poster_path'):
            # The season 0 are the specials, Netflix has them not as a season
            posters[int(number)] = TMDB_IMAGE_URL + season['poster_path']
    return posters


def _search_tv_id(title, year, params, headers):
    # The year of Netflix is often of the last season, so the search without the year is the fallback
    for search_year in ((year, '') if year else ('',)):
        search_params = dict(params, query=title)
        if search_year:
            search_params['first_air_date_year'] = search_year
        results = _get('/search/tv', search_params, headers).get('results') or []
        if results:
            normalized = title.strip().lower()
            for result in results:
                if normalized in ((result.get('name') or '').strip().lower(),
                                  (result.get('original_name') or '').strip().lower()):
                    return result['id']
            if search_year:
                continue
            return results[0]['id']
    return None


def _get(path, params, headers):
    response = requests.get(TMDB_API_URL + path, params=params, headers=headers, timeout=TMDB_TIMEOUT_SECS)
    response.raise_for_status()
    return response.json()
