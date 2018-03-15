from pritunl import settings
from pritunl import logger

import urllib
import httplib
import time
import urlparse
import requests

def _getokta_url():
    parsed = urlparse.urlparse(settings.app.sso_saml_url)
    return '%s://%s' % (parsed.scheme, parsed.netloc)

def get_user_id(username):
    try:
        response = requests.get(
            _getokta_url() + '/api/v1/users/%s' % urllib.quote(username),
            headers={
                'Accept': 'application/json',
                'Authorization': 'SSWS %s' % settings.app.sso_okta_token,
            },
        )
    except httplib.HTTPException:
        logger.exception('Okta api error', 'sso',
            username=username,
        )
        return None

    if response.status_code != 200:
        logger.error('Okta api error', 'sso',
            username=username,
            status_code=response.status_code,
            response=response.content,
        )
        return None

    data = response.json()

    user_id = data.get('id')
    if not user_id:
        logger.error('Okta username not found', 'sso',
            username=username,
            status_code=response.status_code,
            response=response.content,
        )
        return None

    if data['status'].lower() != 'active':
        logger.warning('Okta user is not active', 'sso',
            username=username,
        )
        return None

    return user_id

def get_factor_id(username, user_id):
    try:
        response = requests.get(
            _getokta_url() + '/api/v1/users/%s/factors' % user_id,
            headers={
                'Accept': 'application/json',
                'Authorization': 'SSWS %s' % settings.app.sso_okta_token,
            },
        )
    except httplib.HTTPException:
        logger.exception('Okta api error', 'sso',
            username=username,
            okta_user_id=user_id,
        )
        return None

    if response.status_code != 200:
        logger.error('Okta api error', 'sso',
            username=username,
            okta_user_id=user_id,
            status_code=response.status_code,
            response=response.content,
        )
        return None

    not_active = False
    data = response.json()
    for factor in data:
        factor_id = factor.get('id')
        factor_provider = factor.get('provider')
        factor_status = factor.get('status')
        if not factor_id or not factor_provider or not factor_status:
            continue

        if factor_provider.lower() != 'okta' or \
                factor['factorType'].lower() != 'push':
            continue

        if factor_status.lower() != 'active':
            not_active = True
            continue

        return factor['id']

    if settings.app.sso_okta_skip_unavailable:
        logger.info('Okta push not available, skipped', 'sso',
            username=username,
            okta_user_id=user_id,
        )
        return True
    elif not_active:
        logger.warning('Okta push not active', 'sso',
            username=username,
            okta_user_id=user_id,
        )
    else:
        logger.warning('Okta push not available', 'sso',
            username=username,
            okta_user_id=user_id,
        )

    return None

def auth_okta(username):
    user_id = get_user_id(username)
    if not user_id:
        return False

    okta_app_id = settings.app.sso_okta_app_id
    if not okta_app_id:
        return True

    try:
        response = requests.get(
            _getokta_url() + \
            '/api/v1/apps?limit=50&filter=user.id+eq+"%s"' % user_id,
            headers={
                'Accept': 'application/json',
                'Authorization': 'SSWS %s' % settings.app.sso_okta_token,
            },
        )
    except httplib.HTTPException:
        logger.exception('Okta api error', 'sso',
            username=username,
        )
        return None

    if response.status_code != 200:
        logger.error('Okta api error', 'sso',
            username=username,
            status_code=response.status_code,
            response=response.content,
        )
        return None

    data = response.json()
    for application in data:
        if application['id'] == okta_app_id:
            return True

    logger.warning('Okta user is not assigned to application', 'sso',
        username=username,
        okta_app_id=okta_app_id,
    )

    return False

def auth_okta_push(username, strong=False, ipaddr=None, type=None, info=None):
    if not settings.app.sso_okta_push:
        return True

    user_id = get_user_id(username)
    if not user_id:
        return False

    factor_id = get_factor_id(username, user_id)
    if not factor_id:
        return False
    elif factor_id is True:
        return True

    try:
        response = requests.post(
            _getokta_url() + '/api/v1/users/%s/factors/%s/verify' % (
                user_id, factor_id),
            headers={
                'Accept': 'application/json',
                'Authorization': 'SSWS %s' % settings.app.sso_okta_token,
                'X-Forwarded-For': ipaddr,
            },
        )
    except httplib.HTTPException:
        logger.exception('Okta api error', 'sso',
            username=username,
            user_id=user_id,
            factor_id=factor_id,
        )
        return False

    if response.status_code != 201:
        logger.error('Okta api error', 'sso',
            username=username,
            user_id=user_id,
            factor_id=factor_id,
            status_code=response.status_code,
            response=response.content,
        )
        return False

    poll_url = None

    start = time.time()
    while time.time() - start < settings.app.sso_timeout:
        data = response.json()
        result = data.get('factorResult').lower()

        if result == 'success':
            return True
        elif result == 'waiting':
            pass
        else:
            logger.warning('Okta push rejected', 'sso',
                username=username,
                user_id=user_id,
                factor_id=factor_id,
                result=result,
            )
            return False

        if not poll_url:
            links = data.get('_links')
            if not links:
                logger.error('Okta cant find links', 'sso',
                    username=username,
                    user_id=user_id,
                    factor_id=factor_id,
                    data=data,
                )
                return False

            poll = links.get('poll')
            if not poll:
                logger.error('Okta cant find poll', 'sso',
                    username=username,
                    user_id=user_id,
                    factor_id=factor_id,
                    data=data,
                )
                return False

            poll_url = poll.get('href')
            if not poll_url:
                logger.error('Okta cant find href', 'sso',
                    username=username,
                    user_id=user_id,
                    factor_id=factor_id,
                    data=data,
                )
                return False

        time.sleep(settings.app.sso_okta_poll_rate)

        try:
            response = requests.get(
                poll_url,
                headers={
                    'Accept': 'application/json',
                    'Authorization': 'SSWS %s' % settings.app.sso_okta_token,
                },
            )
        except httplib.HTTPException:
            logger.exception('Okta poll api error', 'sso',
                username=username,
                user_id=user_id,
                factor_id=factor_id,
            )
            return False

        if response.status_code != 200:
            logger.error('Okta poll api error', 'sso',
                username=username,
                user_id=user_id,
                factor_id=factor_id,
                status_code=response.status_code,
                response=response.content,
            )
            return False
