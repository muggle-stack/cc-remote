"""Stable release discovery remains usable during anonymous REST throttling."""
import io
from urllib.error import HTTPError

import pytest

from cc_remote import installation_updates as updates


def test_rate_limited_release_discovery_uses_verified_stable_redirect(monkeypatch):
    class Redirect(io.BytesIO):
        def geturl(self):
            return 'https://github.com/example/project/releases/tag/v5.3.35'

    requests = []
    def respond(request, **_kwargs):
        requests.append(request.full_url)
        if 'api.github.com' in request.full_url:
            raise HTTPError(request.full_url, 403, 'Forbidden', {'X-RateLimit-Remaining': '0'}, None)
        return Redirect()

    monkeypatch.setattr(updates, 'urlopen', respond)
    monkeypatch.setattr(updates, '_catalog_cache', {})
    assert updates.release_catalog('example/project')['version'] == '5.3.35'
    assert updates.release_catalog('example/project')['version'] == '5.3.35'
    assert len(requests) == 2


@pytest.mark.parametrize('target', ['https://other.test/example/project/releases/tag/v5.3.35',
                                   'https://github.com/example/project/releases/tag/v5.3.35-beta'])
def test_catalog_redirect_cannot_change_repository_or_select_prerelease(monkeypatch, target):
    class Redirect(io.BytesIO):
        def geturl(self):
            return target

    monkeypatch.setattr(updates, 'read_with_retry', lambda *_args, **_kw: (_ for _ in ()).throw(
        HTTPError('https://api.github.com', 429, 'Rate limited', {}, None)))
    monkeypatch.setattr(updates, 'urlopen', lambda *_args, **_kw: Redirect())
    monkeypatch.setattr(updates, '_catalog_cache', {})
    with pytest.raises(ValueError):
        updates.release_catalog('example/project')
