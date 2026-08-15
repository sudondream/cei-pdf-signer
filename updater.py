#!/usr/bin/env python3
"""Self-update and self-install mechanism.

Deliberately inert: nothing here quits the app, shows UI, or replaces a
bundle. The most it does is hand back the argv of a command that would.
main.py owns process death, because main.py owns the card-driver drain rule
that dying carelessly violates.
"""

import collections
import hashlib
import json
import os
import pathlib
import plistlib
import re
import subprocess
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


def display_version(tag):
    """What macOS shows in the About panel: the tag, minus the leading 'v'.

    CFBundleVersion has to be dotted integers, but CFBundleShortVersionString
    is the human-facing one and Developer ID distribution does not enforce a
    shape on it. Using the numeric form for both gave "Version 0.14.0 (0.14.0)"
    - redundant, and it threw away the part that says this is a beta.
    """
    parsed = parse_version(tag)
    if parsed is None:
        return tag or 'dev'
    return tag[1:] if tag.startswith('v') else tag


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


# How many 0.1s ticks to wait for the app to exit before giving up. Patched
# down in tests. Reaching this limit means abandoning the update - swapping a
# bundle under a live process is worse than not updating at all.
WAIT_TICKS = 300

# Runs after the app is dead, so it cannot report anything: every branch has to
# end with a working application on disk.
#
# ditto, never cp -R. The bundle has ~45 symlinks and depends on POSIX execute
# bits; anything that drops them breaks the app silently (issues #4, #6, #7).
#
# Never written to a file. `sh -c` keeps the text in memory for the life of the
# shell, so there is nothing on disk to permission, tamper with, or clean up.
RELAUNCH_SCRIPT = '''
set -u
PID="$1"; SRC="$2"; DEST="$3"; CLEANUP="${4:-}"; TICKS="$5"
OLD="$DEST.old-$$"
LAUNCH="$DEST"

i=0
while kill -0 "$PID" 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -gt "$TICKS" ] && exit 1
    sleep 0.1
done

[ -e "$DEST" ] && { mv "$DEST" "$OLD" || exit 1; }

if ditto "$SRC" "$DEST"; then
    rm -rf "$OLD"
    [ -n "$CLEANUP" ] && rm -rf "$CLEANUP"
else
    rm -rf "$DEST"
    if [ -e "$OLD" ]; then
        mv "$OLD" "$DEST"
    else
        LAUNCH="$SRC"
    fi
fi

open "$LAUNCH"
'''


def relaunch_command(pid, src, dest, cleanup=''):
    """argv that installs `src` at `dest` once `pid` exits, then reopens it.

    Shared by the update and the move to /Applications: the two differ only in
    what they pass. Returns argv rather than running anything, because this
    module is not allowed to end the process.
    """
    return ['/bin/sh', '-c', RELAUNCH_SCRIPT, 'cei-relaunch',
            str(pid), str(src), str(dest), str(cleanup or ''), str(WAIT_TICKS)]


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
    """The newest installable release as GitHub's JSON, or None.

    Reads the full list rather than /releases/latest, for two reasons.

    That endpoint returns the newest *non-prerelease* release and 404s only
    when every release is a prerelease - so it cannot be used to notice that a
    prerelease exists, which an earlier version of this code assumed it could.
    With a stable release and a newer prerelease it simply answers with the
    stable one, no error, and the fallback that was supposed to catch it never
    runs.

    And the list arrives ordered by creation date, not by version. This
    repository has already seen GitHub order that list in a way nobody
    expected, so "the first entry" is not the same claim as "the newest
    version" - hence max() over parsed versions.

    Drafts and prereleases are skipped: flagging a release as either is a
    deliberate statement that it is not meant for everyone yet. Tags that do
    not parse are skipped too, since an update we cannot order is one we
    cannot know is an upgrade.
    """
    candidates = [release for release in (fetch(RELEASES_URL) or [])
                  if not release.get('draft')
                  and not release.get('prerelease')
                  and parse_version(release.get('tag_name')) is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda release: parse_version(release['tag_name']))


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


class VerificationError(Exception):
    """The download is not what it claims to be."""


# codesign prints "TeamIdentifier=not set" for an ad-hoc bundle, which a naive
# \S+ captures as the team "not" - so two unsigned bundles would compare equal
# and pass the check that exists to stop exactly that.
_TEAM_RE = re.compile(r'^TeamIdentifier=(?!not set$)(\S+)', re.MULTILINE)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def expected_sha(sums_text, filename):
    """The digest for `filename` out of a SHA256SUMS.txt body."""
    for line in (sums_text or '').splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip('*') == filename:
            return parts[0]
    return None


def team_identifier(path, run=subprocess.run):
    """The Developer ID team a bundle is signed by, or None."""
    done = run(['codesign', '-dv', '--verbose=2', str(path)],
               capture_output=True, text=True)
    match = _TEAM_RE.search(done.stderr or '')
    return match.group(1) if match else None


def download(url, dest, progress=None, timeout=30):
    """Fetch `url` to `dest`, calling progress(done_bytes, total_bytes)."""
    request = urllib.request.Request(
        url, headers={'User-Agent': 'cei-pdf-signer'})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        total = int(response.headers.get('Content-Length') or 0)
        done = 0
        with open(dest, 'wb') as handle:
            while True:
                chunk = response.read(256 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)


def verify(zip_path, bundle, want_sha, want_team, run=subprocess.run):
    """Raise VerificationError unless this is genuinely our next release.

    Order matters: the cheap digest runs before anything spawns a process.

    The signature checks carry the weight, not the checksum. A digest fetched
    from the same host as the file it describes proves only that the download
    was not truncated - whoever could swap the archive could swap the sums file
    beside it. Only the signature proves the bundle was signed by this
    project's Developer ID and notarized by Apple.
    """
    if sha256(zip_path) != want_sha:
        raise VerificationError('suma de control nu corespunde')

    if run(['codesign', '--verify', '--deep', '--strict', str(bundle)],
           capture_output=True, text=True).returncode != 0:
        raise VerificationError('semnatura este invalida')

    found_team = team_identifier(bundle, run=run)
    if not want_team or not found_team or found_team != want_team:
        raise VerificationError(
            'semnat de alta echipa: %s' % (found_team or 'nesemnat',))

    if run(['spctl', '-a', '-t', 'exec', str(bundle)],
           capture_output=True, text=True).returncode != 0:
        raise VerificationError('aplicatia nu este notarizata')

    # We fetch with Python, so LaunchServices never stamps this. Its presence
    # means something we do not understand happened; assert rather than strip.
    attrs = run(['xattr', str(bundle)], capture_output=True, text=True).stdout
    if 'com.apple.quarantine' in (attrs or ''):
        raise VerificationError('descarcarea este marcata ca fiind din internet')
