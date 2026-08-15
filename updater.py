#!/usr/bin/env python3
"""Self-update and self-install mechanism.

Deliberately inert: nothing here quits the app, shows UI, or replaces a
bundle. The most it does is hand back the argv of a command that would.
main.py owns process death, because main.py owns the card-driver drain rule
that dying carelessly violates.
"""

import collections
import json
import os
import pathlib
import plistlib
import re
import sys
import urllib.error
import urllib.request

# Tags look like v0.13-beta. The suffix carries no ordering, so it is dropped.
_VERSION_RE = re.compile(r'^v?(\d+)\.(\d+)(?:\.(\d+))?')


def parse_version(tag):
    """(major, minor, patch) from a release tag, or None if unreadable."""
    match = _VERSION_RE.match(tag or '')
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


def numeric_version(tag):
    """Dotted integers for CFBundleVersion, which notarization is picky about."""
    parsed = parse_version(tag)
    return '.'.join(str(part) for part in parsed) if parsed else '0.0.0'


def is_newer(latest_tag, current_tag):
    """Whether latest_tag supersedes current_tag.

    False whenever either side is unreadable - including 'dev'. An update we
    cannot reason about is one we must not download.
    """
    latest, current = parse_version(latest_tag), parse_version(current_tag)
    if latest is None or current is None:
        return False
    return latest > current


def bundle_path():
    """The .app we are running from, or None when not frozen."""
    if not getattr(sys, 'frozen', False):
        return None
    # sys.executable is <bundle>/Contents/MacOS/<name>
    return pathlib.Path(sys.executable).parents[2]


def current_release_tag():
    """Our own release tag, or 'dev' when run from source.

    Read from Info.plist rather than baked into the source, so there is one
    source of truth (the git tag) rather than two that can disagree.
    """
    bundle = bundle_path()
    if bundle is None:
        return 'dev'
    try:
        with open(bundle / 'Contents' / 'Info.plist', 'rb') as handle:
            return plistlib.load(handle).get('CEIReleaseTag', 'dev')
    except (OSError, plistlib.InvalidFileException):
        return 'dev'


APPLICATIONS = pathlib.Path('/Applications')

# macOS runs an app launched from a quarantined download out of a randomized
# read-only mount under this directory, so the bundle cannot replace itself and
# the path does not say where the original came from.
_TRANSLOCATION_MARKER = '/AppTranslocation/'


def is_translocated(path):
    return _TRANSLOCATION_MARKER in str(path)


def is_installable(path):
    """Whether we could replace the bundle at this path in place."""
    if path is None or is_translocated(path):
        return False
    return os.access(path.parent, os.W_OK) and os.access(path, os.W_OK)


def move_destination(bundle, home=None):
    """Where this bundle should be moved to, or None if it should not be.

    None whenever the prompt would be pointless or impossible: not frozen,
    already installed, or nowhere writable to move to. Translocation is *not*
    excluded - a translocated app is exactly the one most worth moving, and it
    is readable even though it is not writable.
    """
    if bundle is None:
        return None
    user_applications = pathlib.Path(home or os.path.expanduser('~')) / 'Applications'
    for folder in (APPLICATIONS, user_applications):
        try:
            bundle.relative_to(folder)
            return None
        except ValueError:
            pass
    if not os.access(APPLICATIONS, os.W_OK):
        return None
    return APPLICATIONS / bundle.name


REPO = 'sudondream/cei-pdf-signer'
RELEASES_URL = 'https://api.github.com/repos/{}/releases'.format(REPO)

# The app archive. SHA256SUMS.txt is matched by exact name.
_ASSET_RE = re.compile(r'^CEI-PDF-Signer-.*-macOS\.zip$')

Update = collections.namedtuple(
    'Update', 'tag zip_url zip_size sums_url page_url')


class NotFound(Exception):
    """A 404 from the GitHub API."""


def fetch_json(url, timeout=5):
    """GET and parse JSON. Raises NotFound on 404, OSError on anything else."""
    request = urllib.request.Request(
        url, headers={'Accept': 'application/vnd.github+json',
                      'User-Agent': 'cei-pdf-signer'})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise NotFound() from error
        raise OSError(str(error)) from error


def latest_release(fetch=fetch_json):
    """The newest published release as GitHub's JSON, or None.

    /releases/latest excludes prereleases. Nothing published so far is flagged
    as one, but the day something is, that endpoint 404s and every installed
    app goes quiet - hence the fallback to the full list.
    """
    try:
        return fetch(RELEASES_URL + '/latest')
    except NotFound:
        pass
    for release in fetch(RELEASES_URL) or []:
        if not release.get('draft'):
            return release
    return None


def _to_update(release):
    """An Update, or None if the release is missing the assets we install."""
    archive = sums = None
    for asset in release.get('assets') or []:
        name = asset.get('name', '')
        if _ASSET_RE.match(name):
            archive = asset
        elif name == 'SHA256SUMS.txt':
            sums = asset
    if archive is None or sums is None:
        return None
    return Update(
        tag=release.get('tag_name', ''),
        zip_url=archive.get('browser_download_url', ''),
        zip_size=archive.get('size', 0),
        sums_url=sums.get('browser_download_url', ''),
        page_url=release.get('html_url', ''),
    )


def check(current_tag, fetch=fetch_json):
    """An Update worth installing, or None. Never raises.

    Swallowing every error is deliberate. This runs at startup, and an update
    check that can surface an error is an update check that can make a working
    signer look broken.
    """
    try:
        release = latest_release(fetch)
    except Exception:
        return None
    if not release or not is_newer(release.get('tag_name'), current_tag):
        return None
    return _to_update(release)
