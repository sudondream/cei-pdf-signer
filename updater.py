#!/usr/bin/env python3
"""Self-update and self-install mechanism.

Deliberately inert: nothing here quits the app, shows UI, or replaces a
bundle. The most it does is hand back the argv of a command that would.
main.py owns process death, because main.py owns the card-driver drain rule
that dying carelessly violates.
"""

import os
import pathlib
import plistlib
import re
import sys

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
