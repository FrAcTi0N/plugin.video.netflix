# -*- coding: utf-8 -*-
"""
    Season posters and IMDb ids from TMDb

    Netflix does not provide an image for a season, and no IMDb id of the titles, so they are looked up
    on TMDb (optional, it requires a TMDb API key in the add-on settings).
    Nothing here raises an exception, when TMDb is not usable the results are empty.

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
CACHE_PREFIX_POSTERS = 'tmdb_season_posters_v1_'
CACHE_PREFIX_ID = 'tmdb_id_v1_'
CACHE_PREFIX_IMDB = 'tmdb_imdb_v1_'
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
    posters = _cached_or_request(f'{CACHE_PREFIX_POSTERS}{videoid_tvshow}_{language}', title,
                                 lambda ctx: _request_season_posters(ctx, 'tv', videoid_tvshow, title, year, language),
                                 {})
    # The numbering of TMDb is used for the seasons, when Netflix splits a season in more parts the
    # numbering would not match, in this case it is better to show no image than a wrong one
    if posters and seasons_count > max(posters):
        LOG.debug('TMDb season posters skipped, the seasons of Netflix ({}) do not match the ones of TMDb ({})',
                  seasons_count, max(posters))
        return {}
    return posters


def get_imdb_id(is_show, netflix_id, title, year, language):
    """
    Return the IMDb id ('tt1234567') of a movie or a tv show, an empty string when not available.
    The result is cached, so TMDb is requested only once for each title.
    :param is_show: True for a tv show (also for its episodes), False for a movie
    :param netflix_id: the Netflix id of the movie or of the tv show (for the cache identifier)
    """
    kind = 'tv' if is_show else 'movie'
    return _cached_or_request(f'{CACHE_PREFIX_IMDB}{kind}_{netflix_id}', title,
                              lambda ctx: _request_imdb_id(ctx, kind, netflix_id, title, year, language),
                              '')


def _cached_or_request(identifier, title, request_func, empty_value):
    """Return the cached value, if missing request it to TMDb, any failure returns 'empty_value'"""
    global _retry_not_before, _failed_key  # pylint: disable=global-statement
    api_key = (G.ADDON.getSetting('tmdb_api_key') or '').strip()
    if not api_key:
        LOG.debug('TMDb: no API key set in the settings')
        return empty_value
    if not title or (api_key == _failed_key and time.time() < _retry_not_before):
        return empty_value
    try:
        return G.CACHE.get(CACHE_METADATA, identifier)
    except CacheMiss:
        pass
    headers = {}
    params = {}
    if len(api_key) > 40:
        # The "API Read Access Token" of TMDb
        headers['Authorization'] = f'Bearer {api_key}'
    else:
        params['api_key'] = api_key
    try:
        value = request_func((params, headers))
    except (requests.exceptions.RequestException, ValueError, KeyError, TypeError) as exc:
        # Not cached, retried after a pause (a wrong key or a missing network would slow down every list)
        _retry_not_before = time.time() + RETRY_AFTER_FAILURE_SECS
        _failed_key = api_key
        LOG.warn('TMDb not available: {}', exc)
        return empty_value
    LOG.info('TMDb "{}": {} -> {}', identifier, title, value if isinstance(value, str) else f'{len(value)} items')
    G.CACHE.add(CACHE_METADATA, identifier, value, ttl=None if value else NOT_FOUND_TTL_SECS)
    return value


def _request_season_posters(ctx, kind, netflix_id, title, year, language):
    tmdb_id = _find_tmdb_id(ctx, kind, netflix_id, title, year, language)
    if not tmdb_id:
        return {}
    details = _get(f'/tv/{tmdb_id}', ctx, {'language': language})
    posters = {}
    for season in details.get('seasons') or []:
        number = season.get('season_number')
        if number and season.get('poster_path'):
            # The season 0 are the specials, Netflix has them not as a season
            posters[int(number)] = TMDB_IMAGE_URL + season['poster_path']
    return posters


def _request_imdb_id(ctx, kind, netflix_id, title, year, language):
    tmdb_id = _find_tmdb_id(ctx, kind, netflix_id, title, year, language)
    if not tmdb_id:
        return ''
    imdb_id = _get(f'/{kind}/{tmdb_id}/external_ids', ctx, {}).get('imdb_id') or ''
    return imdb_id if imdb_id.startswith('tt') else ''


def _find_tmdb_id(ctx, kind, netflix_id, title, year, language):
    """Return the TMDb id of the title (cached, shared by posters and IMDb id), None when not found"""
    identifier = f'{CACHE_PREFIX_ID}{kind}_{netflix_id}_{language}'
    try:
        return G.CACHE.get(CACHE_METADATA, identifier) or None
    except CacheMiss:
        pass
    tmdb_id = _search_id(ctx, kind, title, year, language)
    if tmdb_id:
        G.CACHE.add(CACHE_METADATA, identifier, tmdb_id)
    else:
        LOG.debug('TMDb: "{}" not found', title)
    return tmdb_id


def _search_id(ctx, kind, title, year, language):
    year_param = 'first_air_date_year' if kind == 'tv' else 'year'
    name_keys = ('name', 'original_name') if kind == 'tv' else ('title', 'original_title')
    # The year of Netflix is often of the last season, so the search without the year is the fallback
    for search_year in ((year, '') if year else ('',)):
        extra = {'language': language, 'query': title}
        if search_year:
            extra[year_param] = search_year
        results = _get(f'/search/{kind}', ctx, extra).get('results') or []
        if results:
            normalized = title.strip().lower()
            for result in results:
                if normalized in [(result.get(key) or '').strip().lower() for key in name_keys]:
                    return result['id']
            if search_year:
                continue
            return results[0]['id']
    return None


def _get(path, ctx, extra_params):
    params, headers = ctx
    response = requests.get(TMDB_API_URL + path, params=dict(params, **extra_params), headers=headers,
                            timeout=TMDB_TIMEOUT_SECS)
    response.raise_for_status()
    return response.json()
