# Auto-Update and Move to Applications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The app notices a newer GitHub release, and on one click downloads it, verifies it is genuinely ours, replaces itself and reopens — plus the standard first-launch "move me to Applications" prompt, which is what makes the first half work for people who run the app out of `Downloads`.

**Architecture:** A new `updater.py` holds all mechanism as pure functions over paths and never ends the process or touches the UI. `app.py` runs download and verification on a background thread behind two polled routes. `main.py` keeps sole ownership of process death, because it already owns the card-driver drain rule that dying carelessly violates. The bundle swap itself happens in a `/bin/sh` script passed to `sh -c`, run detached, after the app is fully dead.

**Tech Stack:** Python 3.14, Flask, pywebview, PyInstaller (onedir bundle), `unittest`, `ditto`/`codesign`/`spctl` from macOS.

**Spec:** `docs/superpowers/specs/2026-08-15-auto-update-design.md`

## Global Constraints

- **Tests run with `venv/bin/python test_app.py`.** Plain `unittest`, one file, no pytest, no network, no smart card. All new tests go in `test_app.py` alongside the existing 103.
- **Never `cp -R` a bundle. Always `ditto`.** The bundle has ~45 symlinks and depends on POSIX execute bits; anything else breaks the app silently (issues #4, #6, #7).
- **Nothing may touch the installed app until every verification step has passed.**
- **The updater must never be able to make the app look broken.** Network failure, GitHub being down, and 403 rate-limiting all return silently to `idle`.
- **Python-owned user-facing strings are Romanian, without diacritics**, matching `LOADING_HTML` in `main.py` and the closing screen at `templates/index.html:954` (`Se incarca...`, `Se inchide...`).
- **The GitHub repo is `sudondream/cei-pdf-signer`.**
- **Release asset names:** the app archive matches `CEI-PDF-Signer-*-macOS.zip`; the checksums file is exactly `SHA256SUMS.txt`.
- **Version tags look like `v0.13-beta`.** Compare as integer tuples, never as strings.
- **`dev` is the version when not running from a bundle**, and it disables both the updater and the move prompt.

---

### Task 1: Version identity

The app currently cannot name its own version — `CEIPDFSigner.spec` hardcodes `1.0.0` into every build ever made. Nothing else in this plan can work until the git tag reaches the product.

**Files:**
- Create: `updater.py`
- Modify: `CEIPDFSigner.spec:1-25` (imports), `CEIPDFSigner.spec:152-160` (`info_plist`)
- Modify: `build.sh:5-10` (compute and export `APP_VERSION`)
- Modify: `build-release.sh:9-13` (read the version instead of computing it)
- Test: `test_app.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `updater.parse_version(tag) -> tuple[int,int,int] | None`, `updater.numeric_version(tag) -> str`, `updater.is_newer(latest_tag, current_tag) -> bool`, `updater.bundle_path() -> pathlib.Path | None`, `updater.current_release_tag() -> str`.

- [ ] **Step 1: Write the failing tests**

Add to `test_app.py`, after the imports add `import updater`:

```python
class VersionParsingTests(unittest.TestCase):
    """Tags are compared as numbers. As strings, v0.9 outranks v0.10."""

    def test_beta_suffix_is_ignored(self):
        self.assertEqual(updater.parse_version('v0.13-beta'), (0, 13, 0))

    def test_leading_v_is_optional(self):
        self.assertEqual(updater.parse_version('0.13.2'), (0, 13, 2))

    def test_garbage_does_not_parse(self):
        for tag in ('dev', '', 'beta', 'v', None):
            self.assertIsNone(updater.parse_version(tag), tag)

    def test_ten_is_newer_than_nine(self):
        self.assertTrue(updater.is_newer('v0.10-beta', 'v0.9-beta'))
        self.assertFalse(updater.is_newer('v0.9-beta', 'v0.10-beta'))

    def test_same_version_is_not_newer(self):
        self.assertFalse(updater.is_newer('v0.13-beta', 'v0.13-beta'))

    def test_unparseable_never_counts_as_an_update(self):
        # A tag we cannot read must not trigger a download. Both directions.
        self.assertFalse(updater.is_newer('banana', 'v0.13-beta'))
        self.assertFalse(updater.is_newer('v0.99-beta', 'dev'))

    def test_numeric_version_is_dotted_integers_for_apple(self):
        # CFBundleVersion must be dotted integers or notarization complains.
        self.assertEqual(updater.numeric_version('v0.13-beta'), '0.13.0')
        self.assertEqual(updater.numeric_version('dev'), '0.0.0')


class CurrentVersionTests(unittest.TestCase):

    def test_unfrozen_reports_dev(self):
        # Run from source there is no Info.plist, so the updater switches off.
        with mock.patch.object(updater.sys, 'frozen', False, create=True):
            self.assertIsNone(updater.bundle_path())
            self.assertEqual(updater.current_release_tag(), 'dev')

    def test_tag_is_read_from_the_bundle_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            contents = os.path.join(tmp, 'X.app', 'Contents')
            os.makedirs(os.path.join(contents, 'MacOS'))
            with open(os.path.join(contents, 'Info.plist'), 'wb') as fh:
                plistlib.dump({'CEIReleaseTag': 'v0.13-beta'}, fh)
            exe = os.path.join(contents, 'MacOS', 'X')
            with mock.patch.object(updater.sys, 'frozen', True, create=True), \
                 mock.patch.object(updater.sys, 'executable', exe):
                self.assertEqual(updater.current_release_tag(), 'v0.13-beta')

    def test_missing_key_reports_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            contents = os.path.join(tmp, 'X.app', 'Contents')
            os.makedirs(os.path.join(contents, 'MacOS'))
            with open(os.path.join(contents, 'Info.plist'), 'wb') as fh:
                plistlib.dump({'CFBundleName': 'X'}, fh)
            exe = os.path.join(contents, 'MacOS', 'X')
            with mock.patch.object(updater.sys, 'frozen', True, create=True), \
                 mock.patch.object(updater.sys, 'executable', exe):
                self.assertEqual(updater.current_release_tag(), 'dev')
```

Add `import plistlib` to `test_app.py`'s imports (`os`, `re`, `tempfile`, `unittest`, `mock` are already there).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python test_app.py VersionParsingTests CurrentVersionTests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'updater'`

- [ ] **Step 3: Write the minimal implementation**

Create `updater.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python test_app.py VersionParsingTests CurrentVersionTests -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Stamp the version into the bundle**

In `CEIPDFSigner.spec`, after the `SIGN_IDENTITY` block at line 20-21, add:

```python
# Versiunea vine din tag-ul git, prin build.sh. Fara ea, fiecare build spunea
# ca este 1.0.0 si aplicatia nu avea cu ce sa se compare la verificarea
# actualizarilor. numeric_version() se importa din updater.py ca sa nu existe
# doua definitii care pot devia una de alta.
sys.path.insert(0, os.getcwd())
from updater import numeric_version

APP_VERSION = os.environ.get('APP_VERSION') or 'dev'
```

Then replace `CEIPDFSigner.spec:152-160`'s `info_plist` dict entries for the two version keys and add the tag key:

```python
    info_plist={
        'CFBundleName': 'CEI PDF Signer',
        'CFBundleDisplayName': 'CEI PDF Signer',
        'CFBundleVersion': numeric_version(APP_VERSION),
        'CFBundleShortVersionString': numeric_version(APP_VERSION),
        # Tag-ul neatins. Apple vrea numere in cheile de mai sus, noi vrem
        # 'v0.13-beta' ca sa comparam cu ce raporteaza GitHub.
        'CEIReleaseTag': APP_VERSION,
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'LSMinimumSystemVersion': '10.13',
    },
```

- [ ] **Step 6: Move version computation into build.sh**

In `build.sh`, after `cd "$(dirname "$0")"` (line 7), add:

```bash
# Versiunea din tag-ul git, exportata pentru CEIPDFSigner.spec. Se calculeaza
# aici, nu in build-release.sh, ca un build normal si unul de release sa nu
# produca aplicatii care raporteaza versiuni diferite.
export APP_VERSION="${APP_VERSION:-$(git describe --tags --abbrev=0 2>/dev/null || echo dev)}"
echo "Versiune: $APP_VERSION"
```

In `build-release.sh`, replace line 10 (`VERSION=$(git describe ...)`) with:

```bash
# build.sh calculeaza si exporta APP_VERSION; il folosim pe acelasi.
VERSION="${APP_VERSION:-$(git describe --tags --abbrev=0 2>/dev/null || echo dev)}"
```

- [ ] **Step 7: Verify the stamp reaches a real bundle**

Run: `./build.sh && /usr/libexec/PlistBuddy -c 'Print :CEIReleaseTag' 'dist/CEI PDF Signer.app/Contents/Info.plist'`
Expected: prints the current git tag, e.g. `v0.13-beta`

Run: `/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' 'dist/CEI PDF Signer.app/Contents/Info.plist'`
Expected: `0.13.0`

- [ ] **Step 8: Run the whole suite and commit**

Run: `venv/bin/python test_app.py`
Expected: all tests pass

```bash
git add updater.py test_app.py CEIPDFSigner.spec build.sh build-release.sh
git commit -m "Give the app a version it can read

Every build so far reported 1.0.0, because the git tag was computed in
build-release.sh and never written into the product. Nothing could compare
itself against a GitHub release."
```

---

### Task 2: Ask GitHub what the latest release is

**Files:**
- Modify: `updater.py`
- Test: `test_app.py`

**Interfaces:**
- Consumes: `updater.is_newer`, `updater.current_release_tag` (Task 1).
- Produces: `updater.Update` namedtuple with fields `tag, zip_url, zip_size, sums_url, page_url`; `updater.NotFound` exception; `updater.latest_release(fetch_json) -> dict | None`; `updater.check(current_tag, fetch_json) -> Update | None`; `updater.fetch_json(url, timeout=5) -> object`.

`fetch_json` is injected as a parameter throughout so tests never touch the network.

- [ ] **Step 1: Write the failing tests**

```python
def _release(tag, assets=('CEI-PDF-Signer-v0.14-beta-macOS.zip', 'SHA256SUMS.txt'),
             draft=False):
    return {
        'tag_name': tag,
        'draft': draft,
        'html_url': 'https://github.com/sudondream/cei-pdf-signer/releases/' + tag,
        'assets': [{'name': name, 'size': 29360128,
                    'browser_download_url': 'https://example.invalid/' + name}
                   for name in assets],
    }


class LatestReleaseTests(unittest.TestCase):
    """GitHub's /releases/latest hides prereleases. Every tag here is -beta."""

    def test_uses_the_latest_endpoint_when_it_answers(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return _release('v0.14-beta')

        self.assertEqual(updater.latest_release(fetch)['tag_name'], 'v0.14-beta')
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith('/releases/latest'))

    def test_falls_back_to_the_list_when_latest_is_absent(self):
        # The day someone ticks "prerelease", /latest 404s and every installed
        # app would silently stop seeing updates without this fallback.
        def fetch(url):
            if url.endswith('/latest'):
                raise updater.NotFound()
            return [_release('v0.14-beta'), _release('v0.13-beta')]

        self.assertEqual(updater.latest_release(fetch)['tag_name'], 'v0.14-beta')

    def test_drafts_are_skipped_in_the_fallback(self):
        def fetch(url):
            if url.endswith('/latest'):
                raise updater.NotFound()
            return [_release('v0.15-beta', draft=True), _release('v0.14-beta')]

        self.assertEqual(updater.latest_release(fetch)['tag_name'], 'v0.14-beta')

    def test_no_releases_at_all(self):
        def fetch(url):
            if url.endswith('/latest'):
                raise updater.NotFound()
            return []

        self.assertIsNone(updater.latest_release(fetch))


class CheckTests(unittest.TestCase):

    def test_newer_release_is_offered(self):
        found = updater.check('v0.13-beta', lambda url: _release('v0.14-beta'))
        self.assertEqual(found.tag, 'v0.14-beta')
        self.assertEqual(found.zip_url,
                         'https://example.invalid/CEI-PDF-Signer-v0.14-beta-macOS.zip')
        self.assertEqual(found.sums_url, 'https://example.invalid/SHA256SUMS.txt')
        self.assertEqual(found.zip_size, 29360128)

    def test_same_version_offers_nothing(self):
        self.assertIsNone(
            updater.check('v0.14-beta', lambda url: _release('v0.14-beta')))

    def test_release_without_our_archive_is_ignored(self):
        # A release carrying only source tarballs is not something to install.
        release = _release('v0.14-beta', assets=('Source code.zip', 'SHA256SUMS.txt'))
        self.assertIsNone(updater.check('v0.13-beta', lambda url: release))

    def test_release_without_checksums_is_ignored(self):
        release = _release('v0.14-beta', assets=('CEI-PDF-Signer-v0.14-beta-macOS.zip',))
        self.assertIsNone(updater.check('v0.13-beta', lambda url: release))

    def test_network_failure_is_silent(self):
        # A signer that cannot reach GitHub is still a working signer.
        def fetch(url):
            raise OSError('no route to host')

        self.assertIsNone(updater.check('v0.13-beta', fetch))

    def test_dev_build_never_updates(self):
        self.assertIsNone(updater.check('dev', lambda url: _release('v0.14-beta')))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python test_app.py LatestReleaseTests CheckTests -v`
Expected: FAIL with `AttributeError: module 'updater' has no attribute 'latest_release'`

- [ ] **Step 3: Write the implementation**

Add to `updater.py` (extend the imports with `import collections`, `import json`, `import urllib.error`, `import urllib.request`):

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python test_app.py LatestReleaseTests CheckTests -v`
Expected: PASS, 11 tests

- [ ] **Step 5: Commit**

```bash
git add updater.py test_app.py
git commit -m "Read the latest release from GitHub

Falls back to the full release list when /releases/latest 404s. Every tag
this project publishes ends in -beta; the day one is flagged prerelease,
that endpoint stops answering and installed apps would go quiet."
```

---

### Task 3: Decide where the app is and whether it can move

**Files:**
- Modify: `updater.py`
- Test: `test_app.py`

**Interfaces:**
- Consumes: `updater.bundle_path` (Task 1).
- Produces: `updater.is_translocated(path) -> bool`, `updater.is_installable(path) -> bool`, `updater.move_destination(bundle, home=None) -> pathlib.Path | None`.

- [ ] **Step 1: Write the failing tests**

```python
class BundleLocationTests(unittest.TestCase):

    def test_translocation_is_detected(self):
        # An app opened straight from Downloads runs read-only from a random
        # path under here, and cannot replace itself.
        path = pathlib.Path('/private/var/folders/ab/xy/T/AppTranslocation/'
                            '1234-5678/d/CEI PDF Signer.app')
        self.assertTrue(updater.is_translocated(path))
        self.assertFalse(updater.is_installable(path))

    def test_normal_path_is_not_translocated(self):
        self.assertFalse(
            updater.is_translocated(pathlib.Path('/Applications/CEI PDF Signer.app')))

    def test_writable_location_is_installable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = pathlib.Path(tmp) / 'CEI PDF Signer.app'
            bundle.mkdir()
            self.assertTrue(updater.is_installable(bundle))

    def test_read_only_parent_is_not_installable(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = pathlib.Path(tmp) / 'ro'
            parent.mkdir()
            bundle = parent / 'CEI PDF Signer.app'
            bundle.mkdir()
            os.chmod(parent, 0o500)
            try:
                self.assertFalse(updater.is_installable(bundle))
            finally:
                os.chmod(parent, 0o700)   # or TemporaryDirectory cannot clean up


class MoveDestinationTests(unittest.TestCase):
    """Each of these must suppress the move prompt on its own."""

    def test_offers_applications_for_a_bundle_in_downloads(self):
        with tempfile.TemporaryDirectory() as home:
            bundle = pathlib.Path(home) / 'Downloads' / 'CEI PDF Signer.app'
            with mock.patch.object(updater, 'APPLICATIONS',
                                   pathlib.Path(home) / 'Applications'):
                (pathlib.Path(home) / 'Applications').mkdir()
                self.assertEqual(
                    updater.move_destination(bundle, home=home),
                    pathlib.Path(home) / 'Applications' / 'CEI PDF Signer.app')

    def test_already_in_applications_offers_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            apps = pathlib.Path(home) / 'Applications'
            apps.mkdir()
            with mock.patch.object(updater, 'APPLICATIONS', apps):
                self.assertIsNone(
                    updater.move_destination(apps / 'CEI PDF Signer.app', home=home))

    def test_already_in_home_applications_offers_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            user_apps = pathlib.Path(home) / 'Applications'
            user_apps.mkdir()
            with mock.patch.object(updater, 'APPLICATIONS', pathlib.Path('/Applications')):
                self.assertIsNone(
                    updater.move_destination(user_apps / 'CEI PDF Signer.app',
                                             home=home))

    def test_unwritable_applications_offers_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            apps = pathlib.Path(home) / 'Applications'
            apps.mkdir()
            os.chmod(apps, 0o500)
            try:
                with mock.patch.object(updater, 'APPLICATIONS', apps):
                    self.assertIsNone(updater.move_destination(
                        pathlib.Path(home) / 'Downloads' / 'CEI PDF Signer.app',
                        home=home))
            finally:
                os.chmod(apps, 0o700)

    def test_unfrozen_offers_nothing(self):
        self.assertIsNone(updater.move_destination(None))
```

Add `import pathlib` to `test_app.py`'s imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python test_app.py BundleLocationTests MoveDestinationTests -v`
Expected: FAIL with `AttributeError: module 'updater' has no attribute 'is_translocated'`

- [ ] **Step 3: Write the implementation**

Add to `updater.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python test_app.py BundleLocationTests MoveDestinationTests -v`
Expected: PASS, 10 tests

- [ ] **Step 5: Commit**

```bash
git add updater.py test_app.py
git commit -m "Work out where the app lives and whether it can move

Translocation blocks in-place update but is precisely the case that most
wants the move-to-Applications prompt, so the two checks are separate."
```

---

### Task 4: The relaunch helper

This is the code that runs when the app is dead and cannot report anything, so it is tested by actually running it.

**Files:**
- Modify: `updater.py`
- Test: `test_app.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `updater.RELAUNCH_SCRIPT` (str), `updater.relaunch_command(pid, src, dest, cleanup='') -> list[str]`.

- [ ] **Step 1: Write the failing tests**

```python
class RelaunchHelperTests(unittest.TestCase):
    """The helper runs after the app is dead, so it is run for real here.

    'open' is shadowed by a stub on PATH: the tests must not launch anything.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.bin)
        self.opened = os.path.join(self.tmp, 'opened.txt')
        with open(os.path.join(self.bin, 'open'), 'w') as fh:
            fh.write('#!/bin/sh\necho "$1" >> %s\n' % self.opened)
        os.chmod(os.path.join(self.bin, 'open'), 0o755)

    def _bundle(self, name, marker):
        path = os.path.join(self.tmp, name)
        os.makedirs(os.path.join(path, 'Contents'))
        with open(os.path.join(path, 'Contents', 'marker'), 'w') as fh:
            fh.write(marker)
        return path

    def _marker(self, bundle):
        with open(os.path.join(bundle, 'Contents', 'marker')) as fh:
            return fh.read()

    def _run(self, pid, src, dest, cleanup=''):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ['PATH'])
        return subprocess.run(
            updater.relaunch_command(pid, src, dest, cleanup),
            env=env, capture_output=True, text=True, timeout=60)

    def _dead_pid(self):
        done = subprocess.run(['/bin/sh', '-c', 'exit 0'])
        return done.pid

    def test_moves_a_bundle_into_place_and_opens_it(self):
        src = self._bundle('New.app', 'new')
        dest = os.path.join(self.tmp, 'Dest.app')
        self._run(self._dead_pid(), src, dest)
        self.assertEqual(self._marker(dest), 'new')
        with open(self.opened) as fh:
            self.assertEqual(fh.read().strip(), dest)

    def test_replaces_an_existing_bundle(self):
        src = self._bundle('New.app', 'new')
        dest = self._bundle('Dest.app', 'old')
        self._run(self._dead_pid(), src, dest, cleanup=os.path.dirname(src))
        self.assertEqual(self._marker(dest), 'new')

    def test_cleanup_path_is_removed(self):
        # The update passes its temp dir here; a normal move passes the
        # original bundle, so the app does not end up installed twice.
        src = self._bundle('New.app', 'new')
        dest = os.path.join(self.tmp, 'Dest.app')
        self._run(self._dead_pid(), src, dest, cleanup=src)
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(dest))

    def test_empty_cleanup_leaves_the_source_alone(self):
        # The translocated move: the original is unreachable, so nothing is
        # deleted. An empty argument must not turn into `rm -rf ''`.
        src = self._bundle('New.app', 'new')
        dest = os.path.join(self.tmp, 'Dest.app')
        self._run(self._dead_pid(), src, dest, cleanup='')
        self.assertTrue(os.path.exists(src))
        self.assertTrue(os.path.exists(dest))

    def test_failed_copy_restores_the_original(self):
        # The rollback path, exercised rather than reasoned about. A missing
        # source makes ditto fail after the destination has been moved aside.
        dest = self._bundle('Dest.app', 'old')
        missing = os.path.join(self.tmp, 'Gone.app')
        self._run(self._dead_pid(), missing, dest)
        self.assertEqual(self._marker(dest), 'old')
        with open(self.opened) as fh:
            self.assertEqual(fh.read().strip(), dest)

    def test_no_leftover_aside_copy_on_success(self):
        src = self._bundle('New.app', 'new')
        dest = self._bundle('Dest.app', 'old')
        self._run(self._dead_pid(), src, dest)
        leftovers = [n for n in os.listdir(self.tmp) if '.old-' in n]
        self.assertEqual(leftovers, [])

    def test_it_waits_for_the_process_to_die(self):
        live = subprocess.Popen(['/bin/sh', '-c', 'sleep 1'])
        self.addCleanup(live.wait)
        src = self._bundle('New.app', 'new')
        dest = os.path.join(self.tmp, 'Dest.app')
        started = time.time()
        self._run(live.pid, src, dest)
        self.assertGreaterEqual(time.time() - started, 0.9)
        self.assertEqual(self._marker(dest), 'new')

    def test_a_process_that_never_dies_touches_nothing(self):
        # Swapping a bundle under a live process is worse than not updating.
        live = subprocess.Popen(['/bin/sh', '-c', 'sleep 60'])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        src = self._bundle('New.app', 'new')
        dest = self._bundle('Dest.app', 'old')
        with mock.patch.object(updater, 'WAIT_TICKS', 3):
            self._run(live.pid, src, dest)
        self.assertEqual(self._marker(dest), 'old')
        self.assertFalse(os.path.exists(self.opened))
```

Add `import shutil`, `import subprocess` and `import time` to `test_app.py`'s imports if absent (`time` is already imported).

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python test_app.py RelaunchHelperTests -v`
Expected: FAIL with `AttributeError: module 'updater' has no attribute 'relaunch_command'`

- [ ] **Step 3: Write the implementation**

Add to `updater.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python test_app.py RelaunchHelperTests -v`
Expected: PASS, 8 tests

- [ ] **Step 5: Commit**

```bash
git add updater.py test_app.py
git commit -m "Add the relaunch helper shared by update and move

Runs after the app is dead and cannot report anything, so every branch ends
with a working application on disk and the tests run it for real.

Passed to sh -c rather than written to a file: the shell holds the text for
its own lifetime, so there is nothing on disk to permission or clean up."
```

---

### Task 5: Download and verify

**Files:**
- Modify: `updater.py`
- Test: `test_app.py`

**Interfaces:**
- Consumes: `updater.Update` (Task 2).
- Produces: `updater.VerificationError` exception; `updater.sha256(path) -> str`; `updater.expected_sha(sums_text, filename) -> str | None`; `updater.team_identifier(path, run=subprocess.run) -> str | None`; `updater.verify(zip_path, bundle, want_sha, want_team, run=subprocess.run) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
class ChecksumTests(unittest.TestCase):

    def test_sha256_of_a_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b'hello')
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(
            updater.sha256(fh.name),
            '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824')

    def test_expected_sha_picks_the_right_line(self):
        sums = ('aaaa  OTHER.zip\n'
                'bbbb  CEI-PDF-Signer-v0.14-beta-macOS.zip\n')
        self.assertEqual(
            updater.expected_sha(sums, 'CEI-PDF-Signer-v0.14-beta-macOS.zip'), 'bbbb')

    def test_expected_sha_missing_entry(self):
        self.assertIsNone(updater.expected_sha('aaaa  OTHER.zip\n', 'ours.zip'))


class VerifyTests(unittest.TestCase):
    """Every check must be able to fail the whole verification on its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.zip = os.path.join(self.tmp, 'a.zip')
        with open(self.zip, 'wb') as fh:
            fh.write(b'hello')
        self.sha = ('2cf24dba5fb0a30e26e83b2ac5b9e29e'
                    '1b161e5c1fa7425e73043362938b9824')
        self.bundle = os.path.join(self.tmp, 'X.app')
        os.makedirs(self.bundle)

    def _run(self, ok=True, team='ABCD1234', quarantined=False):
        def fake(argv, **kwargs):
            if argv[0] == 'codesign' and '-dv' in argv:
                return subprocess.CompletedProcess(
                    argv, 0, '', 'TeamIdentifier=%s\n' % team)
            if argv[0] == 'xattr':
                out = 'com.apple.quarantine\n' if quarantined else ''
                return subprocess.CompletedProcess(argv, 0, out, '')
            return subprocess.CompletedProcess(argv, 0 if ok else 1, '', 'nope')
        return fake

    def test_a_good_bundle_verifies(self):
        updater.verify(self.zip, self.bundle, self.sha, 'ABCD1234',
                       run=self._run())

    def test_wrong_checksum_is_refused(self):
        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, 'deadbeef', 'ABCD1234',
                           run=self._run())

    def test_a_different_team_is_refused(self):
        # The check that actually matters: a bundle signed by someone else is
        # not ours, whatever the release page says.
        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, self.sha, 'ABCD1234',
                           run=self._run(team='EVIL0000'))

    def test_a_broken_signature_is_refused(self):
        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, self.sha, 'ABCD1234',
                           run=self._run(ok=False))

    def test_quarantined_download_is_refused(self):
        # We fetch with Python, so LaunchServices never stamps this. Finding it
        # means something happened that we do not understand.
        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, self.sha, 'ABCD1234',
                           run=self._run(quarantined=True))

    def test_checksum_is_checked_before_anything_expensive(self):
        seen = []

        def fake(argv, **kwargs):
            seen.append(argv[0])
            return subprocess.CompletedProcess(argv, 0, '', 'TeamIdentifier=X\n')

        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, 'deadbeef', 'X', run=fake)
        self.assertEqual(seen, [])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python test_app.py ChecksumTests VerifyTests -v`
Expected: FAIL with `AttributeError: module 'updater' has no attribute 'sha256'`

- [ ] **Step 3: Write the implementation**

Add to `updater.py` (extend imports with `import hashlib`, `import subprocess`):

```python
class VerificationError(Exception):
    """The download is not what it claims to be."""


_TEAM_RE = re.compile(r'^TeamIdentifier=(\S+)', re.MULTILINE)


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
    if found_team != want_team:
        raise VerificationError(
            'semnat de alta echipa: %s' % (found_team or 'necunoscut',))

    if run(['spctl', '-a', '-t', 'exec', str(bundle)],
           capture_output=True, text=True).returncode != 0:
        raise VerificationError('aplicatia nu este notarizata')

    # We fetch with Python, so LaunchServices never stamps this. Its presence
    # means something we do not understand happened; assert rather than strip.
    attrs = run(['xattr', str(bundle)], capture_output=True, text=True).stdout
    if 'com.apple.quarantine' in (attrs or ''):
        raise VerificationError('descarcarea este marcata ca fiind din internet')
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python test_app.py ChecksumTests VerifyTests -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add updater.py test_app.py
git commit -m "Verify a download before it is allowed anywhere near the install

The signature carries the weight, not the checksum: a digest served from the
same host as the file proves only that the download was not truncated."
```

---

### Task 6: Update state machine and routes

**Files:**
- Modify: `app.py` (append after the `/api/open-external` route, around line 1061)
- Test: `test_app.py`

**Interfaces:**
- Consumes: everything from `updater` (Tasks 1-5).
- Produces: `app.set_relaunch_handler(fn)` where `fn(argv)`; `app.start_update_check()`; `app._update_state`; routes `GET /api/update/status`, `POST /api/update/start`, `POST /api/update/open-download`.
- `GET /api/update/status` returns `{'stage': str, 'percent': int, 'tag': str|None, 'installable': bool, 'error': str|None}` where `stage` is one of `idle`, `checking`, `available`, `downloading`, `verifying`, `ready`, `failed`.

- [ ] **Step 1: Write the failing tests**

```python
class UpdateStatusTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        app_module.reset_update_state()

    def test_idle_by_default(self):
        body = self.client.get('/api/update/status').get_json()
        self.assertEqual(body['stage'], 'idle')
        self.assertIsNone(body['tag'])

    def test_available_after_a_check_finds_one(self):
        found = updater.Update('v0.14-beta', 'https://example.invalid/a.zip',
                               10, 'https://example.invalid/SHA256SUMS.txt',
                               'https://example.invalid/page')
        with mock.patch.object(app_module.updater, 'check', return_value=found), \
             mock.patch.object(app_module.updater, 'is_installable', return_value=True):
            app_module.start_update_check().join(5)
        body = self.client.get('/api/update/status').get_json()
        self.assertEqual(body['stage'], 'available')
        self.assertEqual(body['tag'], 'v0.14-beta')
        self.assertTrue(body['installable'])

    def test_a_failed_check_stays_idle(self):
        # A signer that cannot reach GitHub is still a working signer.
        with mock.patch.object(app_module.updater, 'check', return_value=None):
            app_module.start_update_check().join(5)
        self.assertEqual(
            self.client.get('/api/update/status').get_json()['stage'], 'idle')


class UpdateStartTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        app_module.reset_update_state()
        self.found = updater.Update('v0.14-beta', 'https://example.invalid/a.zip',
                                    10, 'https://example.invalid/SHA256SUMS.txt',
                                    'https://example.invalid/page')

    def _make_available(self):
        with mock.patch.object(app_module.updater, 'check', return_value=self.found), \
             mock.patch.object(app_module.updater, 'is_installable', return_value=True):
            app_module.start_update_check().join(5)

    def test_start_is_rejected_when_nothing_is_available(self):
        self.assertEqual(self.client.post('/api/update/start').status_code, 409)

    def test_start_is_rejected_while_the_card_driver_is_busy(self):
        # Restarting mid-PKCS#11 is what wedges the driver until reboot.
        self._make_available()
        with mock.patch.object(app_module, 'driver_busy', return_value=True):
            self.assertEqual(self.client.post('/api/update/start').status_code, 409)

    def test_start_is_rejected_twice(self):
        self._make_available()
        with mock.patch.object(app_module, '_run_update', lambda: None):
            self.assertEqual(self.client.post('/api/update/start').status_code, 200)
            self.assertEqual(self.client.post('/api/update/start').status_code, 409)


class UpdateDownloadLinkTests(unittest.TestCase):
    """The fallback opener must not become an arbitrary-URL opener.

    /api/open-external is guarded by test_raw_url_cannot_be_injected; this
    route takes no parameters at all, so the same property holds by shape.
    """

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        app_module.reset_update_state()

    def test_opens_the_stored_release_page(self):
        found = updater.Update('v0.14-beta', 'z', 10, 's',
                               'https://github.com/sudondream/cei-pdf-signer/releases/v0.14-beta')
        with mock.patch.object(app_module.updater, 'check', return_value=found), \
             mock.patch.object(app_module.updater, 'is_installable', return_value=False):
            app_module.start_update_check().join(5)
        with mock.patch.object(app_module.subprocess, 'run') as run:
            resp = self.client.post('/api/update/open-download')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            run.call_args[0][0],
            ['open', 'https://github.com/sudondream/cei-pdf-signer/releases/v0.14-beta'])

    def test_a_url_in_the_body_is_ignored(self):
        found = updater.Update('v0.14-beta', 'z', 10, 's', 'https://real.example')
        with mock.patch.object(app_module.updater, 'check', return_value=found), \
             mock.patch.object(app_module.updater, 'is_installable', return_value=False):
            app_module.start_update_check().join(5)
        with mock.patch.object(app_module.subprocess, 'run') as run:
            self.client.post('/api/update/open-download',
                             json={'url': 'https://evil.example.com'})
        self.assertEqual(run.call_args[0][0], ['open', 'https://real.example'])

    def test_nothing_to_open_is_rejected(self):
        with mock.patch.object(app_module.subprocess, 'run') as run:
            resp = self.client.post('/api/update/open-download')
        self.assertEqual(resp.status_code, 400)
        run.assert_not_called()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python test_app.py UpdateStatusTests UpdateStartTests UpdateDownloadLinkTests -v`
Expected: FAIL with `AttributeError: module 'app' has no attribute 'reset_update_state'`

- [ ] **Step 3: Write the implementation**

`app.py` already imports `os`, `tempfile`, `subprocess`, `threading` and `hashlib` (lines 7-14). Add only `import shutil`, `import urllib.request` and `import updater`, then append after the `/api/open-external` route:

```python
# --- Actualizare ------------------------------------------------------------
#
# Descarcarea si verificarea se fac aici, pe un fir de fundal. Oprirea
# procesului NU se face aici: main.py este singurul care stie sa astepte
# driverul de card inainte sa moara, iar a muri in mijlocul unui apel PKCS#11
# blocheaza cititorul pentru tot calculatorul pana la repornire.
class _UpdateState:
    def __init__(self):
        self.lock = threading.Lock()
        self.stage = 'idle'
        self.percent = 0
        self.update = None
        self.installable = False
        self.error = None

    def snapshot(self):
        with self.lock:
            return {
                'stage': self.stage,
                'percent': self.percent,
                'tag': self.update.tag if self.update else None,
                'installable': self.installable,
                'error': self.error,
            }


_update_state = _UpdateState()
_relaunch_handler = None


def reset_update_state():
    """Test helper: forget everything the updater has learned."""
    global _update_state
    _update_state = _UpdateState()


def set_relaunch_handler(handler):
    """Register the one function allowed to end the process (see main.py)."""
    global _relaunch_handler
    _relaunch_handler = handler


def start_update_check():
    """Ask GitHub whether there is a newer release. Returns the thread."""
    def run():
        with _update_state.lock:
            _update_state.stage = 'checking'
        found = updater.check(updater.current_release_tag())
        with _update_state.lock:
            if found is None:
                _update_state.stage = 'idle'
            else:
                _update_state.update = found
                _update_state.installable = updater.is_installable(
                    updater.bundle_path())
                _update_state.stage = 'available'

    thread = threading.Thread(target=run, daemon=True, name='update-check')
    thread.start()
    return thread


def _set(stage, percent=None, error=None):
    with _update_state.lock:
        _update_state.stage = stage
        if percent is not None:
            _update_state.percent = percent
        _update_state.error = error


def _run_update():
    """Download, verify, and hand main.py the command that installs it."""
    with _update_state.lock:
        found = _update_state.update
    bundle = updater.bundle_path()
    workdir = tempfile.mkdtemp(prefix='cei-update-')
    try:
        _set('downloading', percent=0)
        archive = os.path.join(workdir, os.path.basename(found.zip_url))
        updater.download(
            found.zip_url, archive,
            progress=lambda done, total: _set(
                'downloading', percent=int(done * 100 / total) if total else 0))

        _set('verifying', percent=100)
        sums = urllib.request.urlopen(found.sums_url, timeout=15).read().decode()
        want = updater.expected_sha(sums, os.path.basename(archive))
        if not want:
            raise updater.VerificationError('lipseste suma de control')

        extracted = os.path.join(workdir, 'x')
        subprocess.run(['ditto', '-x', '-k', archive, extracted], check=True)
        staged = os.path.join(extracted, os.path.basename(str(bundle)))
        updater.verify(archive, staged, want, updater.team_identifier(bundle))

        _set('ready')
        _relaunch_handler(updater.relaunch_command(
            os.getpid(), staged, str(bundle), workdir))
    except Exception as error:
        # Nimic din afara lui workdir nu a fost atins, deci esecul e inert.
        shutil.rmtree(workdir, ignore_errors=True)
        _set('failed', error=str(error))


@app.route('/api/update/status')
def api_update_status():
    return jsonify(_update_state.snapshot())


@app.route('/api/update/start', methods=['POST'])
def api_update_start():
    with _update_state.lock:
        if _update_state.stage != 'available' or not _update_state.installable:
            return jsonify({'error': 'Nicio actualizare disponibila'}), 409
        if driver_busy():
            return jsonify({'error': 'Se lucreaza cu cardul'}), 409
        _update_state.stage = 'downloading'
        _update_state.percent = 0

    threading.Thread(target=_run_update, daemon=True, name='update-run').start()
    return jsonify({'success': True})


@app.route('/api/update/open-download', methods=['POST'])
def api_update_open_download():
    """Open the release page. Takes no parameters, deliberately.

    The URL comes from update state that only a GitHub response ever wrote, so
    this cannot be turned into an arbitrary-URL opener - the same property
    /api/open-external has, arrived at by taking no input rather than by
    checking it.
    """
    with _update_state.lock:
        url = _update_state.update.page_url if _update_state.update else None
    if not url:
        return jsonify({'error': 'Nicio actualizare disponibila'}), 400
    subprocess.run(['open', url], check=False)
    return jsonify({'success': True})
```


- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python test_app.py UpdateStatusTests UpdateStartTests UpdateDownloadLinkTests -v`
Expected: PASS, 9 tests

- [ ] **Step 5: Commit**

```bash
git add app.py test_app.py
git commit -m "Add the update state machine and its routes

Download and verification run here; ending the process does not. The
download-page fallback is a parameterless route rather than a widening of
/api/open-external, whose no-arbitrary-URL property is under test."
```

---

### Task 7: Wire the relaunch into main.py

**Files:**
- Modify: `main.py:24` (import), `main.py:147-215` (`main`)
- Test: `test_app.py`

**Interfaces:**
- Consumes: `app.set_relaunch_handler` (Task 6), `updater.relaunch_command` (Task 4).
- Produces: `main._quit_and_relaunch(argv)`.

- [ ] **Step 1: Write the failing test**

```python
class RelaunchWiringTests(unittest.TestCase):
    """The quit path must drain the card driver before spawning anything."""

    def test_helper_is_detached_and_the_driver_is_drained_first(self):
        import main as main_module
        order = []
        window = mock.Mock()
        window.destroy.side_effect = lambda: order.append('destroy')

        with mock.patch.object(main_module, 'wait_for_driver',
                               side_effect=lambda: order.append('drain')), \
             mock.patch.object(main_module.subprocess, 'Popen',
                               side_effect=lambda *a, **k: order.append('spawn')) as popen, \
             mock.patch.object(main_module.os, '_exit',
                               side_effect=lambda code: order.append('exit')):
            main_module._quit_and_relaunch(['/bin/sh', '-c', 'true'], window=window)

        self.assertEqual(order[0], 'drain',
                         'spawning before the driver drains risks the wedge')
        self.assertIn('spawn', order)
        self.assertTrue(popen.call_args[1]['start_new_session'],
                        'the helper must outlive this process')
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `venv/bin/python test_app.py RelaunchWiringTests -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute '_quit_and_relaunch'`

- [ ] **Step 3: Write the implementation**

In `main.py`, add `import subprocess` to the imports and change line 24 to:

```python
from app import (app, driver_busy, wait_for_driver, set_relaunch_handler,
                 start_update_check)
```

Add before `main()`:

```python
def _quit_and_relaunch(argv, window=None):
    """Spawn the installer helper, then quit the same way a close would.

    The order is the point. wait_for_driver() runs first for exactly the reason
    it runs on a normal close: dying inside a PKCS#11 call strands the Idemia
    driver for every process on the machine until the Mac is restarted. An
    update that saves the user a download and costs them a reboot is not a
    saving.

    start_new_session so the helper survives us - it exists to act after we are
    gone. os._exit for the same reason the signal handler uses it: SystemExit
    cannot unwind a main thread parked in Cocoa's event loop.
    """
    wait_for_driver()
    subprocess.Popen(argv, start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if window is not None:
        try:
            window.destroy()
        except Exception:
            pass
    os._exit(0)
```

Inside `main()`, immediately after `window.events.closing += on_closing` (line 208), add:

```python
    set_relaunch_handler(lambda argv: _quit_and_relaunch(argv, window=window))
```

And inside `start_app`, after the successful `window.load_url(...)` call, add:

```python
            # Dupa ce interfata s-a incarcat, ca verificarea sa nu concureze cu
            # detectia cardului la pornire.
            time.sleep(2)
            start_update_check()
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `venv/bin/python test_app.py RelaunchWiringTests -v`
Expected: PASS

- [ ] **Step 5: Run the whole suite and commit**

Run: `venv/bin/python test_app.py`
Expected: all tests pass

```bash
git add main.py test_app.py
git commit -m "Let main.py install an update and relaunch

The driver drains before the helper is spawned, for the same reason it
drains on a normal close: dying inside a PKCS#11 call wedges the card
reader for the whole machine until reboot."
```

---

### Task 8: The banner, the pill, and the overlay

**Files:**
- Modify: `templates/index.html` — CSS after line 842, markup after line 975, JS inside `window.onload` at line 2262
- Test: `test_app.py`

**Interfaces:**
- Consumes: `/api/update/status`, `/api/update/start`, `/api/update/open-download` (Task 6).
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Write the failing tests**

These follow the existing source-inspection style used by `test_unfixable_states_do_not_invite_the_user_into_settings`.

```python
class UpdateUiTests(unittest.TestCase):

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'templates', 'index.html')) as fh:
            self.src = fh.read()

    def test_banner_and_pill_both_exist(self):
        self.assertIn('id="update-banner"', self.src)
        self.assertIn('id="update-pill"', self.src)

    def test_dismissing_the_banner_reveals_the_pill(self):
        # The offer must survive a dismissal, or it is gone forever.
        start = self.src.find('function dismissUpdateBanner()')
        self.assertNotEqual(start, -1, 'dismissUpdateBanner not found')
        body = self.src[start:self.src.find('function ', start + 10)]
        self.assertIn("getElementById('update-pill')", body)
        self.assertIn("classList.add('visible')", body)

    def test_the_frontend_never_supplies_a_download_url(self):
        # The route takes no parameters; the page must not pretend otherwise.
        call = re.search(r"fetch\('/api/update/open-download'[^)]*\)", self.src)
        self.assertIsNotNone(call, 'open-download is never called')
        self.assertNotIn('body', call.group(0))

    def test_unusable_locations_offer_a_download_instead(self):
        self.assertIn('installable', self.src)
        self.assertIn('Descarca', self.src)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python test_app.py UpdateUiTests -v`
Expected: FAIL on `id="update-banner"` not found

- [ ] **Step 3: Add the CSS**

Insert after `templates/index.html:842` (the end of `.pkcs11-warning.visible`):

```css
        .update-pill {
            display: none;
            background: rgba(0, 212, 255, 0.15);
            border: 1px solid rgba(0, 212, 255, 0.5);
            color: #00d4ff;
            font-size: 12px;
            padding: 4px 10px;
            border-radius: 15px;
            cursor: pointer;
            margin-right: 10px;
            transition: all 0.2s;
        }

        .update-pill:hover {
            background: rgba(0, 212, 255, 0.28);
            border-color: #00d4ff;
        }

        .update-pill.visible {
            display: flex;
            align-items: center;
            gap: 5px;
        }

        .update-banner {
            display: none;
            align-items: center;
            gap: 12px;
            padding: 10px 20px;
            background: rgba(0, 212, 255, 0.12);
            border-bottom: 1px solid rgba(0, 212, 255, 0.35);
            color: #cfefff;
            font-size: 13px;
        }

        .update-banner.visible { display: flex; }
        .update-banner .grow { flex: 1; }

        .update-banner button {
            background: #0099ff;
            border: none;
            color: #fff;
            padding: 5px 14px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 13px;
        }

        .update-banner .dismiss {
            background: transparent;
            color: #8fbfd6;
            font-size: 16px;
            padding: 0 4px;
        }

        .update-progress {
            width: 260px;
            height: 6px;
            border-radius: 3px;
            background: rgba(0, 212, 255, 0.15);
            margin-top: 25px;
            overflow: hidden;
        }

        .update-progress > div {
            height: 100%;
            width: 0;
            background: #00d4ff;
            transition: width 0.2s;
        }
```

- [ ] **Step 4: Add the markup**

Insert the banner immediately after the closing `</div>` of `.header` (after `templates/index.html:975`):

```html
    <!--
      Anuntul de actualizare. Daca utilizatorul il inchide, oferta nu dispare:
      devine pastila din header (#update-pill). Cele doua nu apar niciodata
      simultan.
    -->
    <div class="update-banner" id="update-banner">
        <span class="grow" id="update-banner-text"></span>
        <button id="update-banner-action" onclick="startUpdate()"></button>
        <button class="dismiss" onclick="dismissUpdateBanner()" title="Inchide">&times;</button>
    </div>
```

Add the pill inside `.header-status`, before the settings button at line 970:

```html
            <span id="update-pill" class="update-pill" onclick="startUpdate()"></span>
```

Add the update overlay next to the closing screen, after `templates/index.html:962`:

```html
    <!-- Actualizarea in curs. Pornita de utilizator, deci pagina o conduce
         singura - spre deosebire de ecranul de inchidere, pe care il aprinde
         Python prin evaluate_js. -->
    <div class="loading-screen hidden" id="update-screen">
        <div class="loading-logo">CEI PDF Signer</div>
        <div class="loading-spinner"></div>
        <div class="loading-text" id="update-screen-text">Se descarca actualizarea...</div>
        <div class="update-progress"><div id="update-progress-bar"></div></div>
    </div>
```

- [ ] **Step 5: Add the JavaScript**

Insert before `window.onload` (line 2262):

```javascript
        let updateInfo = null;
        let updateBannerDismissed = false;

        function renderUpdateOffer() {
            if (!updateInfo) return;
            const installable = updateInfo.installable;
            const verb = installable ? 'Actualizeaza' : 'Descarca';
            const text = 'Versiunea ' + updateInfo.tag + ' este disponibila';

            const banner = document.getElementById('update-banner');
            document.getElementById('update-banner-text').textContent = text;
            document.getElementById('update-banner-action').textContent = verb;
            banner.classList.toggle('visible', !updateBannerDismissed);

            const pill = document.getElementById('update-pill');
            pill.textContent = '↑ ' + verb + ' ' + updateInfo.tag;
            pill.classList.toggle('visible', updateBannerDismissed);
        }

        function dismissUpdateBanner() {
            // Oferta nu dispare, doar se muta in header.
            updateBannerDismissed = true;
            document.getElementById('update-banner').classList.remove('visible');
            document.getElementById('update-pill').classList.add('visible');
        }

        async function startUpdate() {
            if (!updateInfo) return;

            if (!updateInfo.installable) {
                await fetch('/api/update/open-download', {method: 'POST'});
                return;
            }

            const queued = document.querySelectorAll('#file-list .file-item').length;
            if (queued > 0 && !confirm(
                    'Actualizarea reporneste aplicatia si goleste lista de fisiere. Continui?')) {
                return;
            }

            const response = await fetch('/api/update/start', {method: 'POST'});
            if (!response.ok) {
                const body = await response.json();
                alert(body.error || 'Actualizarea nu poate porni acum.');
                return;
            }
            document.getElementById('update-screen').classList.remove('hidden');
            pollUpdate();
        }

        async function pollUpdate() {
            const status = await (await fetch('/api/update/status')).json();
            const text = document.getElementById('update-screen-text');
            const bar = document.getElementById('update-progress-bar');

            if (status.stage === 'downloading') {
                text.textContent = 'Se descarca actualizarea...';
                bar.style.width = status.percent + '%';
            } else if (status.stage === 'verifying') {
                text.textContent = 'Se verifica semnatura...';
                bar.style.width = '100%';
            } else if (status.stage === 'ready') {
                text.textContent = 'Repornim aplicatia...';
            } else if (status.stage === 'failed') {
                document.getElementById('update-screen').classList.add('hidden');
                alert('Actualizarea a esuat: ' + (status.error || 'motiv necunoscut') +
                      '\n\nAplicatia instalata nu a fost modificata.');
                return;
            }
            setTimeout(pollUpdate, 400);
        }

        async function checkForUpdate() {
            try {
                const status = await (await fetch('/api/update/status')).json();
                if (status.stage === 'available') {
                    updateInfo = status;
                    renderUpdateOffer();
                    return;
                }
                if (status.stage === 'checking') setTimeout(checkForUpdate, 1500);
            } catch (e) {
                // O verificare esuata nu are voie sa strice nimic vizibil.
            }
        }
```

Inside `window.onload`, after `updateSignButton();`, add:

```javascript
            // Verificarea porneste in Python la 2 secunde dupa incarcare.
            setTimeout(checkForUpdate, 2500);
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `venv/bin/python test_app.py UpdateUiTests -v`
Expected: PASS, 4 tests

The `#file-list .file-item` selector used in `startUpdate` is the real one — the container is at `templates/index.html:988` and items get `className = 'file-item'` at line 1355. Confirm it still holds:
Run: `grep -n 'id="file-list"' templates/index.html`
Expected: line 988 matches.

- [ ] **Step 7: Commit**

```bash
git add templates/index.html test_app.py
git commit -m "Show the update offer, and keep showing it after a dismissal

The banner becomes a header pill rather than disappearing, so closing it
postpones the update instead of hiding it forever."
```

---

### Task 9: Preferences file

**Files:**
- Create: `prefs.py`
- Test: `test_app.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `prefs.path() -> pathlib.Path`, `prefs.load() -> dict`, `prefs.save(values) -> None`, `prefs.get(key, default=None)`, `prefs.set(key, value) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
class PrefsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.file = os.path.join(self.tmp, 'prefs.json')
        patcher = mock.patch.object(prefs, 'path',
                                    return_value=pathlib.Path(self.file))
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(prefs.load(), {})

    def test_round_trip(self):
        prefs.set('move_declined', True)
        self.assertTrue(prefs.get('move_declined'))
        self.assertEqual(prefs.load(), {'move_declined': True})

    def test_corrupt_file_reads_as_empty(self):
        # The app must start even if this file is garbage. It holds nothing
        # worth failing a launch over.
        with open(self.file, 'w') as fh:
            fh.write('{not json')
        self.assertEqual(prefs.load(), {})

    def test_saving_creates_the_directory(self):
        nested = os.path.join(self.tmp, 'a', 'b', 'prefs.json')
        with mock.patch.object(prefs, 'path', return_value=pathlib.Path(nested)):
            prefs.set('x', 1)
            self.assertTrue(os.path.exists(nested))
```

Add `import prefs` to `test_app.py`'s imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python test_app.py PrefsTests -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'prefs'`

- [ ] **Step 3: Write the implementation**

Create `prefs.py`:

```python
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
    more than the app starting.
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `venv/bin/python test_app.py PrefsTests -v`
Expected: PASS, 4 tests

- [ ] **Step 5: Commit**

```bash
git add prefs.py test_app.py
git commit -m "Add a Python-side preferences file

The existing pkcs11_path preference lives in the webview's localStorage,
which Python cannot read - and the move prompt runs before the page loads."
```

---

### Task 10: Move to Applications on first launch

**Files:**
- Modify: `main.py` (imports, and the top of `start_app`)
- Modify: `CEIPDFSigner.spec:38-44` (`hiddenimports`)
- Test: `test_app.py`

**Interfaces:**
- Consumes: `updater.bundle_path`, `updater.move_destination`, `updater.relaunch_command`, `updater.is_translocated` (Tasks 1, 3, 4); `prefs.get`, `prefs.set` (Task 9); `main._quit_and_relaunch` (Task 7).
- Produces: `main.offer_move_to_applications(window) -> bool` (True when a move was started and the app is quitting).

- [ ] **Step 1: Write the failing tests**

```python
class MoveToApplicationsTests(unittest.TestCase):

    def setUp(self):
        import main as main_module
        self.main = main_module
        self.window = mock.Mock()

    def _patches(self, destination, declined=False, translocated=False):
        return [
            mock.patch.object(self.main.updater, 'bundle_path',
                              return_value=pathlib.Path('/x/CEI PDF Signer.app')),
            mock.patch.object(self.main.updater, 'move_destination',
                              return_value=destination),
            mock.patch.object(self.main.updater, 'is_translocated',
                              return_value=translocated),
            mock.patch.object(self.main.prefs, 'get', return_value=declined),
        ]

    def _run(self, patches):
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return self.main.offer_move_to_applications(self.window)

    def test_no_prompt_when_there_is_nowhere_to_move(self):
        self.assertFalse(self._run(self._patches(None)))
        self.window.create_confirmation_dialog.assert_not_called()

    def test_no_prompt_after_a_previous_refusal(self):
        dest = pathlib.Path('/Applications/CEI PDF Signer.app')
        self.assertFalse(self._run(self._patches(dest, declined=True)))
        self.window.create_confirmation_dialog.assert_not_called()

    def test_refusing_is_remembered(self):
        dest = pathlib.Path('/Applications/CEI PDF Signer.app')
        self.window.create_confirmation_dialog.return_value = False
        with mock.patch.object(self.main.prefs, 'set') as remember:
            self.assertFalse(self._run(self._patches(dest)))
        remember.assert_called_once_with('move_declined', True)

    def test_accepting_moves_and_deletes_the_original(self):
        dest = pathlib.Path('/Applications/CEI PDF Signer.app')
        self.window.create_confirmation_dialog.return_value = True
        with mock.patch.object(self.main, '_quit_and_relaunch') as relaunch:
            self.assertTrue(self._run(self._patches(dest)))
        argv = relaunch.call_args[0][0]
        self.assertEqual(argv[-2], '/x/CEI PDF Signer.app',
                         'the original must be cleaned up, not left behind')

    def test_a_translocated_original_is_left_alone(self):
        # We cannot find the real original from a translocated path, so the
        # cleanup argument must be empty rather than a guess.
        dest = pathlib.Path('/Applications/CEI PDF Signer.app')
        self.window.create_confirmation_dialog.return_value = True
        with mock.patch.object(self.main, '_quit_and_relaunch') as relaunch:
            self.assertTrue(self._run(self._patches(dest, translocated=True)))
        self.assertEqual(relaunch.call_args[0][0][-2], '')
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `venv/bin/python test_app.py MoveToApplicationsTests -v`
Expected: FAIL with `AttributeError: module 'main' has no attribute 'offer_move_to_applications'`

- [ ] **Step 3: Write the implementation**

In `main.py`, add `import prefs` and `import updater` to the imports, then add before `main()`:

```python
MOVE_TITLE = 'Muta in Applications'
MOVE_MESSAGE = (
    'CEI PDF Signer nu este in folderul Applications.\n\n'
    'Il mutam acolo si repornim aplicatia? Actualizarile automate '
    'functioneaza doar din Applications.'
)


def offer_move_to_applications(window):
    """Offer to install the app in /Applications. True if we are quitting.

    Runs before Flask starts, since there is no sense booting a server we are
    about to kill.

    A translocated app cannot be deleted afterwards - macOS runs it from a
    randomized read-only path that does not say where the original came from -
    so that case copies and leaves the original behind. Recovering the true
    path needs SecTranslocateCreateOriginalPathForURL from Security.framework,
    which is a lot of ctypes to buy tidiness.
    """
    bundle = updater.bundle_path()
    destination = updater.move_destination(bundle)
    if destination is None or prefs.get('move_declined', False):
        return False

    if not window.create_confirmation_dialog(MOVE_TITLE, MOVE_MESSAGE):
        prefs.set('move_declined', True)
        return False

    cleanup = '' if updater.is_translocated(bundle) else str(bundle)
    _quit_and_relaunch(
        updater.relaunch_command(os.getpid(), str(bundle), str(destination), cleanup),
        window=window)
    return True
```

Make `start_app` return early when a move starts — replace the body of `start_app` in `main.py:167-183` so it begins with:

```python
    def start_app():
        """Start Flask and navigate to it once ready"""
        if offer_move_to_applications(window):
            return   # we are quitting; do not boot a server we are about to kill
```

- [ ] **Step 4: Declare the new modules to PyInstaller**

`prefs` and `updater` are reached only at runtime, and `CEIPDFSigner.spec` assembles the bundle from `hiddenimports`. Add both to the list at `CEIPDFSigner.spec:38-44`, beside `'app'` and `'pcsc'`:

```python
        'app',
        'pcsc',
        # Ca 'app' si 'pcsc': module ajunse la doar in timpul rularii, deci
        # module care pot lipsi din bundle fara ca nimic sa se planga.
        'updater',
        'prefs',
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `venv/bin/python test_app.py MoveToApplicationsTests -v`
Expected: PASS, 5 tests

- [ ] **Step 6: Verify the modules actually reach the bundle**

Run: `./build.sh && ls 'dist/CEI PDF Signer.app/Contents/Resources' | head -30`

Then confirm the built app starts and serves its UI:
Run: `open 'dist/CEI PDF Signer.app'`
Expected: the window opens and reaches the signer UI, not the "Server failed to start" screen. A missing hidden import shows up exactly there.

- [ ] **Step 7: Run the whole suite and commit**

Run: `venv/bin/python test_app.py`
Expected: all tests pass

```bash
git add main.py CEIPDFSigner.spec test_app.py
git commit -m "Offer to move the app into Applications on first launch

Not just manners: an app running from Downloads is one the updater has to
refuse, so every install this relocates makes auto-update work.

A translocated original is left in place - macOS runs it from a randomized
read-only path that does not say where it came from."
```

---

### Task 11: Manual release verification and documentation

Unit tests cannot prove a real signed bundle replaces itself in `/Applications` and reopens. This makes that check a written step rather than an assumption.

**Files:**
- Create: `scripts/verify-update-manually.md`
- Modify: `build-release.sh:126-139` (the closing instructions)
- Modify: `README.md` (features list, both language sections)

**Interfaces:**
- Consumes: everything.
- Produces: nothing.

- [ ] **Step 1: Write the manual procedure**

Create `scripts/verify-update-manually.md`:

```markdown
# Verificarea manuala a actualizarii automate

Testele automate acopera fiecare bucata, dar nu si intregul: o aplicatie
semnata real care se inlocuieste in /Applications si se redeschide. Se face
o data inainte de fiecare release care atinge updater.py, prefs.py sau
scriptul de relansare.

Dureaza ~5 minute.

## 1. Pregatire

Se construieste release-ul curent, semnat si notarizat:

    SIGN_IDENTITY="Developer ID Application: ..." \
    NOTARY_PROFILE="..." ./build-release.sh

Se instaleaza versiunea ANTERIOARA in /Applications (dezarhivata din
release-ul precedent de pe GitHub), nu cea proaspata. Fara asta nu exista
nimic de actualizat.

## 2. Verificarea ofertei

- Se deschide aplicatia din /Applications.
- Dupa ~3 secunde apare bannerul "Versiunea vX.Y-beta este disponibila".
- Se apasa x. Bannerul dispare, in header apare pastila "↑ Actualizeaza".
  Oferta NU trebuie sa dispara complet.

## 3. Verificarea actualizarii

- Se apasa pastila.
- Bara de progres avanseaza, apoi "Se verifica semnatura...", apoi
  "Repornim aplicatia...".
- Fereastra se inchide si se redeschide singura in ~15 secunde.
- In "Despre" / Info.plist versiunea este cea noua:

      /usr/libexec/PlistBuddy -c 'Print :CEIReleaseTag' \
          '/Applications/CEI PDF Signer.app/Contents/Info.plist'

- NU trebuie sa ramana nimic in urma:

      ls -d '/Applications/CEI PDF Signer.app.old-'* 2>/dev/null
      ls -d "${TMPDIR}cei-update-"* 2>/dev/null

  Ambele trebuie sa nu gaseasca nimic.

## 4. Verificarea mutarii in Applications

- Se sterge /Applications/CEI PDF Signer.app.
- Se dezarhiveaza release-ul in ~/Downloads si se deschide de acolo.
- Apare dialogul nativ "Muta in Applications".
- Se apasa Cancel: aplicatia porneste normal si nu mai intreaba niciodata,
  nici dupa repornire. Se verifica:

      cat ~/Library/Application\ Support/ro.cei.pdfsigner/prefs.json

- Se sterge acel fisier, se redeschide din ~/Downloads, se apasa OK:
  aplicatia se inchide si se redeschide din /Applications.

## 5. Verificarea degradarii

- Se dezarhiveaza release-ul intr-un folder fara drept de scriere si se
  deschide de acolo. Bannerul trebuie sa spuna "Descarca", nu
  "Actualizeaza", iar apasarea lui deschide pagina de release in browser.
```

- [ ] **Step 2: Point the release script at it**

In `build-release.sh`, add before the final `echo ""` at line 139:

```bash
echo "Daca acest release atinge updater.py, prefs.py sau scriptul de"
echo "relansare, ruleaza inainte de publicare procedura manuala din"
echo "scripts/verify-update-manually.md - testele automate nu pot acoperi"
echo "o aplicatie semnata care se inlocuieste singura in /Applications."
echo ""
```

- [ ] **Step 3: Document the feature for users**

In `README.md`, add to the English features list:

```markdown
- **Automatic updates** — the app checks GitHub for new releases and can install them itself. Downloads are verified against the project's Developer ID signature and Apple's notarization before anything is replaced.
```

And to the Romanian features list:

```markdown
- **Actualizari automate** — aplicatia verifica singura daca a aparut o versiune noua si o poate instala. Descarcarea este verificata prin semnatura Developer ID a proiectului si prin notarizarea Apple inainte sa fie inlocuit ceva.
```

- [ ] **Step 4: Run the whole suite**

Run: `venv/bin/python test_app.py`
Expected: all tests pass

- [ ] **Step 5: Commit**

```bash
git add scripts/verify-update-manually.md build-release.sh README.md
git commit -m "Document the manual update check the tests cannot perform

No unit test can prove a real signed bundle replaces itself in /Applications
and reopens, so the check is written down rather than assumed."
```

---

## Execution Order

Tasks 1-5 build `updater.py` bottom-up and are independent of the app. Task 6 needs 1-5. Task 7 needs 6. Task 8 needs 6. Task 9 is independent and can run any time before Task 10. Task 10 needs 3, 4, 7 and 9. Task 11 is last.

Task 1 must go first regardless: nothing else can be tested against a version the app cannot read.
