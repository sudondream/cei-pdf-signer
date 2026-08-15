# In-App Auto-Update

Date: 2026-08-15
Status: approved

## Goal

The app checks GitHub for a newer release, tells the user, and — on one click —
downloads it, verifies it, replaces itself, and reopens. What the user sees is a
progress bar, the window closing, and the window coming back on the new version.

Today there is no update path at all. Users learn about new versions only by
revisiting the site, and every upgrade is a manual download-extract-drag.

## Prerequisite: The App Does Not Know Its Own Version

`CEIPDFSigner.spec` hardcodes `CFBundleVersion: '1.0.0'` and
`CFBundleShortVersionString: '1.0.0'` for every build ever made. The real version
exists only as a git tag, read inside `build-release.sh` at build time and never
written into the product. There is nothing at runtime to compare against GitHub.

**Chosen:** the git tag becomes the single source of truth and flows into the
bundle.

- `build.sh` computes it (moved up from `build-release.sh:10`, so plain builds and
  release builds agree) and exports `APP_VERSION`.
- `CEIPDFSigner.spec` reads `os.environ.get('APP_VERSION', 'dev')` and writes three
  plist keys:

  ```python
  # v0.13-beta -> ('0.13.0', 'v0.13-beta')
  numeric = _numeric_version(APP_VERSION)   # Apple wants dotted integers here
  info_plist = {
      'CFBundleVersion': numeric,
      'CFBundleShortVersionString': numeric,
      'CEIReleaseTag': APP_VERSION,         # 'v0.13-beta', what we compare on
      ...
  }
  ```

  `CFBundleVersion` keeps dotted integers because notarization is picky about it;
  the unmodified tag lives in a custom key, which is what the updater reads.

- At runtime the app reads its own `Info.plist` with `plistlib`, at
  `Path(sys.executable).parents[1] / 'Info.plist'`.

Run from source there is no `Info.plist`, the version reads `dev`, and the updater
disables itself entirely. Development builds never try to replace themselves with a
release, and that falls out of the design rather than needing a flag.

## Decisions Taken

### Notify and let the user install; never install unasked

**Chosen:** the check runs by itself, the download does not. A banner announces the
new version; nothing leaves the network until the user clicks.

Dismissing the banner does not retract the offer — it collapses into a persistent
clickable pill beside the card status, styled like the existing `pkcs11-warning` at
`templates/index.html:967`. The banner and the pill never appear at once: the pill
is what the banner becomes.

Rejected: silent background download and swap-on-quit. It spends 28MB of someone's
bandwidth without asking and changes the app under them. For a tool that signs legal
documents with a national ID card, "the binary changed and nobody mentioned it" is
the wrong default at any level of convenience.

Rejected: a manual "check for updates" button only. Nobody presses it, and everyone
stays on old versions — which defeats the point of building this.

### Replace the bundle from a detached helper, not from inside the app

**Chosen:** the app stages a verified copy, spawns a small `/bin/sh` helper detached,
and exits through its normal close path. The helper waits for the process to die,
then does the swap and reopens the app.

The app is a PyInstaller *onedir* bundle: the interpreter loads `.so` files from
`Contents/Frameworks` lazily, throughout its life. Deleting that directory under a
live process risks the interpreter reaching for something that is no longer there and
dying hard — and dying hard is precisely the failure mode `main.py:119` exists to
prevent, because a process that dies mid-PKCS#11 wedges the Idemia driver for every
process on the machine until reboot. Waiting for the app to be genuinely dead before
touching its files is not caution, it is the whole reason to use a helper.

Rejected: swapping in-process and `exec`ing the new binary. Fewer moving parts, but
it puts file deletion and interpreter liveness in the same instant.

Rejected: Sparkle, the macOS standard. It would bring EdDSA-signed updates, delta
downloads and a proven UI. But it is an Objective-C framework to embed and drive over
PyObjC, it wants a hosted appcast XML, and it has to be reconciled with the Hardened
Runtime entitlements this app already needs for the IDEMIA library. PyInstaller plus
Sparkle is not a well-trodden path, and the integration would be larger than the
feature.

### Trust the signature, not the checksum

**Chosen:** verify in this order, all of it before anything is installed.

1. SHA256 of the download against the `SHA256SUMS.txt` release asset.
2. `ditto -x -k` to extract.
3. `codesign --verify --deep --strict` on the extracted bundle.
4. `TeamIdentifier` of the download equals the running app's, both read from
   `codesign -dv --verbose=2`.
5. `spctl -a -t exec` — proves Apple notarized it.
6. `com.apple.quarantine` is absent.

Steps 3–5 are the ones that carry the weight. A checksum fetched from the same host
as the file it describes proves only that the download was not truncated; anyone who
could swap the ZIP could swap the sums file beside it. The signature check proves the
bundle was signed by *this project's* Developer ID and notarized by Apple, which no
compromise of the GitHub release page can forge. The checksum stays because it is
free and catches ordinary corruption early, before the expensive extract.

Step 6 asserts rather than strips. We download with Python, so LaunchServices never
stamps the attribute and the relaunched app opens without a Gatekeeper prompt — its
absence is the expected state, and finding it present means something happened that
we do not understand and should not paper over.

### Degrade to a download link where in-place replacement is impossible

**Chosen:** detect it before offering anything. If the bundle's parent directory is
not writable, or the path contains `/AppTranslocation/` (macOS running the app
read-only from a randomized path, which is what happens to an app launched straight
out of `Downloads`), the notice still appears but reads *"Descarca v0.14-beta"* and
opens the release page through the `/api/open-external` route that already exists at
`app.py:1047`.

Rejected: escalating with `osascript ... with administrator privileges`. An app that
handles a national identity card asking for an admin password is indistinguishable
from the phishing it should be teaching users to refuse — and it cannot fix the
translocation case anyway, since that path is read-only to root as well.

### No automatic rollback from a bad new version

**Chosen:** the helper rolls back a *failed copy* — that is a file operation with a
knowable outcome. It does not attempt to detect that a successfully installed version
is broken and revert it. If a release is bad, recovery is downloading an older one by
hand.

Judging "the new version works" reliably enough to act on is a larger feature than
this entire document, and a wrong judgement would uninstall a good version.

## Components

A new `updater.py` that imports neither Flask nor webview. It is pure mechanism over
paths, and its most important property is what it cannot do: **it never quits the app
and never touches the UI.** The furthest it goes is returning "there is a verified
bundle at X and a helper script at Y, ready when you are."

```
current_release_tag()  -> 'v0.13-beta' | 'dev'      reads own Info.plist
check(timeout=5)       -> Update | None             GitHub API, no side effects
is_installable(path)   -> bool                      writability + translocation
download(url, dest, progress_cb)
verify(zip, extracted, expected_sha, our_team_id)   raises; returns nothing
stage(extracted, dest) -> helper_script_path
```

Everything takes paths and returns values, so it is testable without launching an app
or publishing a release.

`main.py` owns the trigger, because it already owns the window and the driver-drain
rule at `main.py:191`. Update-and-restart is the existing close path with one extra
step in front of it: same `wait_for_driver()`, then spawn the helper instead of a
plain exit. Putting process death anywhere else would mean a second file that has to
remember the card-driver rule — the rule this project has paid the most to learn.

**How the two halves connect.** `app.py` does the download and verification on its
background thread but cannot quit the app; `main.py` can quit but does not want to
know about HTTP. So `main.py` registers a callback at startup, in the same spirit as
the `driver_busy` / `wait_for_driver` functions it already imports:

```python
# main.py, before webview.start()
app_module.set_relaunch_handler(lambda script: _quit_and_relaunch(script))
```

When verification succeeds, the background thread moves the state to `ready` and
invokes the handler with the helper script path. `app.py` holds an opaque callable
and nothing more; `main.py` holds the only code that ends the process.

The overlay is driven by the **frontend**, not by `evaluate_js` from Python — unlike
the closing screen, this flow starts with a user click, so the page already knows it
is happening and is already polling `/api/update/status` for the percentage. Reusing
the `closing-screen` markup pattern does not mean reusing its Python-push mechanism.

`app.py` gains two routes in the style of the existing ones:

- `GET /api/update/status` — the frontend polls it
- `POST /api/update/start` — 409 if the state machine is not at `available`, and 409
  if `driver_busy()`

A background thread holds the state: `idle → checking → available → downloading(pct)
→ verifying → ready → failed`.

`templates/index.html` gains the banner, the pill, and an update overlay built on the
`closing-screen` pattern at line 954.

## Flow

**Check.** Fires a couple of seconds after the window loads, so it is not competing
with card detection for startup attention. One unauthenticated call to
`/repos/sudondream/cei-pdf-signer/releases/latest`, 5s timeout, failing silently to
`idle`. A signer that cannot reach GitHub is still a working signer; the update check
must never be able to make the app look broken.

Tags parse as `v?(\d+)\.(\d+)(?:\.(\d+))?` with the `-beta` suffix ignored, compared
as integer tuples — string comparison would rank `v0.9` above `v0.10`. An unparseable
tag counts as "no update," never as "update available."

`/releases/latest` **excludes prereleases.** Every tag this project has published ends
in `-beta` but none is flagged prerelease on GitHub, so it works today. The day that
box gets ticked, every installed app silently stops seeing updates. So: on 404, fall
back to the first non-draft entry of `/releases`.

**Install**, one click:

1. Full-screen overlay with a progress bar. No Finder, no Terminal, no second window.
2. 28MB to a `0700` `mkdtemp`, percentage live in the overlay.
3. Verification, in the order above. **Nothing outside the temp directory has been
   touched yet**, so any failure here is completely inert: delete the temp directory,
   turn the overlay into a failure message with a link to the download page, and the
   installed app is bit-for-bit unchanged.
4. State goes to `ready`; the relaunch handler fires. `main.py` spawns the helper with
   `start_new_session=True`, no shell, output to `/dev/null`, so nothing flashes on
   screen.
5. Overlay reads "Repornim aplicatia...", `wait_for_driver()` runs exactly as on a
   normal quit, process exits.
6. The helper takes over.

```sh
#!/bin/sh
# $1 pid   $2 staged bundle   $3 installed bundle   $4 temp dir
set -u
PID="$1"; NEW="$2"; DEST="$3"; TMP="$4"
OLD="$DEST.old-$$"

i=0
while kill -0 "$PID" 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -gt 300 ] && { rm -rf "$TMP"; exit 1; }   # 30s: never swap under a live app
    sleep 0.1
done

mv "$DEST" "$OLD" || { rm -rf "$TMP"; exit 1; }
if ditto "$NEW" "$DEST"; then
    rm -rf "$OLD"
else
    rm -rf "$DEST"
    mv "$OLD" "$DEST"      # a failed update ends with a working app
fi
open "$DEST"
rm -rf "$TMP"              # unlinks itself; the shell's open fd survives it
```

`ditto` rather than `cp -R`, for the same reason `build-release.sh:93` uses it: the
bundle contains ~45 symlinks and depends on POSIX execute bits, and tools that do not
preserve them break the app silently.

**When not to offer it.** `driver_busy()` blocks the start route — restarting
mid-PKCS#11 is the one thing this codebase most wants to avoid. A non-empty file list
puts a one-line confirm behind the button, since the restart drops the queue.

## Failure Modes

| Failure | Result |
|---|---|
| No network, GitHub down, rate limited (403) | Silent return to `idle`. No user-visible change. |
| Download interrupted | Temp directory removed, failure message, installed app untouched. |
| Checksum mismatch | Loud failure, worded distinctly from a network error. |
| Signature / Team ID / notarization fails | Loud failure, worded as "the download is not what it claims to be." |
| Quarantine attribute present | Treated as a verification failure. |
| Helper cannot rename the installed bundle | Aborts before copying; app is untouched, user reopens it. |
| `ditto` fails mid-copy | Original moved back and opened. |
| App process never exits | Helper times out at 30s having touched nothing. |
| Two updates started at once | Start route 409s outside the `available` state. |
| Installed version turns out to be broken | Out of scope; manual download of an older release. |

**Known wrinkle.** The helper writes wherever the app lives. `/Applications` needs no
special permission, which is where the install instructions already send people. But
an app living in `~/Desktop`, `~/Documents` or `~/Downloads` sits behind TCC, and the
helper may raise a one-time "wants to access files in your Desktop folder" prompt.
Not fatal and not fixable from inside the app; documented so it is not mistaken for a
bug.

## Testing

Alongside the existing 103 tests in `test_app.py`, plain `unittest`, no card, no
network:

- **Version comparison as a table.** `v0.13-beta` vs `v0.14-beta`; `v0.9` vs `v0.10`
  (the string-compare trap); equal versions; garbage tags; `dev`.
- **GitHub parsing against canned JSON.** Normal response; `/releases/latest` 404 →
  `/releases` fallback; drafts skipped; expected asset missing.
- **Installability detection.** Writable directory; read-only directory; a path
  containing `AppTranslocation`.
- **Verification refusing bad input.** Wrong checksum; a Team ID that does not match
  the running app's.

**The helper script is tested directly**, because it is the code that runs when the
app is dead and cannot report anything. It is a shell script over paths, so a test
hands it a `sleep` process and two temp directories and asserts the swap happened,
the temp directory was cleaned, and the app was reopened. The same test with an
unwritable destination asserts the original came back intact — the rollback path
exercised for real rather than reasoned about.

**Not covered by any of that:** a real signed bundle replacing itself in
`/Applications` and reopening. That is one manual run against a scratch copy before
release, documented as a manual step near `scripts/verify-release-archive.sh` rather
than pretended into a unit test.

## Out of Scope

- Delta updates. The full bundle is 28MB and the swap is atomic; per-file diffing
  buys seconds and costs a great deal of correctness.
- Update channels (stable vs beta). Every release is a beta today.
- Downgrades and pinning.
- Auto-rollback from a working install of a broken version.
