#!/usr/bin/env python3
"""Small Python-side preferences file.

The app's other preference (pkcs11_path) lives in the webview's localStorage,
which Python cannot reach - and the move-to-Applications prompt runs before the
page has loaded at all. Hence a separate file rather than a second reader for
the same store.
"""

import json
import os
import pathlib

BUNDLE_ID = 'ro.cei.pdfsigner'


def path():
    return (pathlib.Path(os.path.expanduser('~')) / 'Library' /
            'Application Support' / BUNDLE_ID / 'prefs.json')


def load():
    """Stored preferences, or {} if there are none or the file is unreadable.

    A corrupt file is not worth failing a launch over: nothing in here matters
    more than the app starting. A valid-but-wrong shape (a list, a string) is
    discarded for the same reason - callers expect to be able to .get().
    """
    try:
        with open(path()) as handle:
            values = json.load(handle)
        return values if isinstance(values, dict) else {}
    except (OSError, ValueError):
        return {}


def save(values):
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, 'w') as handle:
        json.dump(values, handle, indent=2)


def get(key, default=None):
    return load().get(key, default)


def set(key, value):
    values = load()
    values[key] = value
    save(values)
