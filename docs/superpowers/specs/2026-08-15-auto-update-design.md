# In-App Auto-Update and Move to Applications

Date: 2026-08-15
Status: approved

## Goal

The app checks GitHub for a newer release, tells the user, and — on one click —
downloads it, verifies it, replaces itself, and reopens. What the user sees is a
progress bar, the window closing, and the window coming back on the new version.

Today there is no update path at all. Users learn about new versions only by
revisiting the site, and every upgrade is a manual download-extract-drag.

Alongside it, the first-launch "Move to Applications folder?" prompt that most Mac
apps show. That is not a separate courtesy: an app running from `Downloads` is one
the updater has to refuse, so every install this relocates converts a
degrade-to-a-download-link case into a working auto-update — and moves the app out of
TCC-protected territory at the same time. The two features also share their
mechanism, since "wait for this process to die, put a bundle at this path, reopen it"
describes both.

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
opens the release page.

That gets its own route, `POST /api/update/open-download`, taking **no parameters**.
The obvious move is to reuse `/api/open-external` at `app.py:1047`, but that route
deliberately accepts only a key into a fixed `ABOUT_LINKS` table, and `test_app.py`
has a `test_raw_url_cannot_be_injected` guarding exactly that. A release URL varies
per version, so reusing the route would mean either widening it to accept URLs —
undoing the property that test exists to protect — or rewriting the table at runtime.
The new route instead reads the URL out of server-side update state that only a
GitHub response ever wrote. The client still cannot name a destination.

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
bundle_path()          -> Path | None               None when not frozen
is_translocated(path)  -> bool
is_installable(path)   -> bool                      writability + translocation
should_offer_move()    -> Path | None               destination, or None
download(url, dest, progress_cb)
verify(zip, extracted, expected_sha, our_team_id)   raises; returns nothing
relaunch_command(pid, src, dest, cleanup) -> argv   shared by update and move
```

Everything takes paths and returns values, so it is testable without launching an app
or publishing a release. `relaunch_command()` is the single place the helper script
exists; the update and the move differ only in the arguments they hand it.

A small `prefs.py` reads and writes
`~/Library/Application Support/ro.cei.pdfsigner/prefs.json`, holding the
"don't ask again" flag for the move prompt. Corrupt or unreadable JSON is treated as
an empty preferences file — the app must start even if this file is garbage.

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
app_module.set_relaunch_handler(_quit_and_relaunch)   # _quit_and_relaunch(argv)
```

When verification succeeds, the background thread moves the state to `ready` and
invokes the handler with the argv from `relaunch_command()`. `app.py` holds an opaque
callable and nothing more; `main.py` holds the only code that ends the process.

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

**Corrected after review.** An earlier version of this design used
`/releases/latest` with a 404 fallback, on the belief that flagging a release as
prerelease would make that endpoint 404 and silence every installed app. That is not
how it behaves: it returns the newest *non-prerelease* release and 404s only when
**every** release is a prerelease. With a stable release and a newer prerelease it
answers with the stable one, no error, and a 404 fallback never runs.

So the checker reads `/releases` directly and takes `max()` over parsed versions of
the non-draft, non-prerelease entries. That also fixes a second problem: the list is
ordered by creation date, not version, and this repository has already seen GitHub
order it unexpectedly — a live call today returns `v0.10-beta` *after* `v0.8-beta`.
"The first entry" and "the newest version" are different claims.

Skipping prereleases is deliberate rather than incidental: ticking that box is a
statement that a release is not meant for everyone yet.

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

The helper is written once and used by both features, because "wait for this process
to die, put a bundle at this path, reopen it" describes the update and the move to
`/Applications` equally. It takes paths and nothing else.

It is never written to disk. `sh -c '<script>' name arg1 arg2 ...` hands the shell the
text and the arguments directly, and the shell keeps the script in memory for as long
as it runs — so there is no temp file to create with the right permissions, no file for
anything else to tamper with between writing and execution, and no self-deleting
`rm -f "$0"` trick at the end. `updater.relaunch_command()` returns the argv, which
makes the whole thing a pure function that tests can assemble and inspect.

```sh
# $1 pid   $2 source bundle   $3 destination   $4 path to delete on success ('' = none)
set -u
PID="$1"; SRC="$2"; DEST="$3"; CLEANUP="${4:-}"
OLD="$DEST.old-$$"
LAUNCH="$DEST"

i=0
while kill -0 "$PID" 2>/dev/null; do
    i=$((i + 1))
    [ "$i" -gt 300 ] && exit 1        # 30s; never touch a bundle under a live process
    sleep 0.1
done

[ -e "$DEST" ] && { mv "$DEST" "$OLD" || exit 1; }

if ditto "$SRC" "$DEST"; then
    rm -rf "$OLD"
    [ -n "$CLEANUP" ] && rm -rf "$CLEANUP"
else
    rm -rf "$DEST"
    if [ -e "$OLD" ]; then
        mv "$OLD" "$DEST"             # a failed run ends with a working app...
    else
        LAUNCH="$SRC"                 # ...or with the one we were trying to move
    fi
fi

open "$LAUNCH"
```

Callers differ only in arguments:

| | `SRC` | `DEST` | `CLEANUP` |
|---|---|---|---|
| Update | staged bundle in temp dir | the running app's path | the temp dir |
| Move | the running app's path | `/Applications/CEI PDF Signer.app` | the running app's path |
| Move, translocated | the running (read-only) app's path | same | `''` — original is unreachable |

`ditto` rather than `cp -R`, for the same reason `build-release.sh:93` uses it: the
bundle contains ~45 symlinks and depends on POSIX execute bits, and tools that do not
preserve them break the app silently.

**When not to offer it.** `driver_busy()` blocks the start route — restarting
mid-PKCS#11 is the one thing this codebase most wants to avoid. A non-empty file list
puts a one-line confirm behind the button, since the restart drops the queue.

## Move to Applications on First Launch

The prompt every Mac app shows — LetsMove / `PFMoveApplication` — implemented on the
helper above.

**Where it fires.** The first thing `start_app` does in `main.py`, before Flask boots;
there is no sense starting a server we are about to kill. `window.create_confirmation_dialog()`
(confirmed present in the installed pywebview) puts a real native dialog over the
loading screen, which is what makes it read as a normal Mac app rather than an
in-page HTML box.

**Conditions, all required.** Frozen bundle; not already under `/Applications` or
`~/Applications`; destination writable; not previously declined. Run from source it
never fires, same as the updater.

**Translocation** is the awkward case and also the common one: an app launched
straight out of `Downloads` runs from a read-only randomized path under
`/private/var/folders/.../AppTranslocation/`, and that path does not say where the
original came from.

**Chosen:** copy the running bundle into `/Applications`, launch that, leave the
original where it is. The translocated bundle is a complete, readable, signed copy —
copying *it* produces a correct installation.

Rejected: recovering the original path via `SecTranslocateCreateOriginalPathForURL`
from Security.framework, which is what LetsMove does. It means hand-written ctypes
bindings and CFURL marshalling to solve a tidiness problem — the cost is a leftover
app in `Downloads`, sitting next to the ZIP the user still has anyway.

**"Don't ask again" needs a Python-side file.** The existing `pkcs11_path` preference
lives in the webview's `localStorage` (`templates/index.html:1227`), which Python
cannot reach this early in startup, and which the move happens before in any case.
So a small `~/Library/Application Support/ro.cei.pdfsigner/prefs.json` — conventional
location, a few lines, and useful for whatever comes next.

**Quarantine.** A moved bundle keeps its `com.apple.quarantine` attribute, so the
first launch from `/Applications` may show the one-time "downloaded from the Internet,
are you sure?" confirmation — a single OK rather than a block, because the app is
notarized. This does *not* contradict the updater's rule that quarantine must be
absent: that rule governs bundles we download ourselves via Python, which
LaunchServices never stamps. The two paths differ, and so do their expectations.

Dialog text is Romanian, matching the loading and closing screens — the other
Python-owned strings in the app.

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
| Move declined | Flag written to `prefs.json`; never asked again. App continues normally. |
| `prefs.json` corrupt or unreadable | Treated as empty. The app must start regardless. |
| `/Applications` already holds a copy | Helper moves it aside and restores it if the copy fails, same as an update. |
| Move fails to copy | Original is opened instead — the app the user launched still runs. |

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
- **Move conditions.** Already in `/Applications`; already in `~/Applications`;
  translocated; declined previously; not frozen. Each must suppress the prompt on its
  own.
- **Preferences.** Round-trip; missing file; corrupt JSON returning empty rather than
  raising.

**The helper script is tested directly**, because it is the code that runs when the
app is dead and cannot report anything. It is a shell script over paths, so a test
hands it a `sleep` process and two temp directories and asserts the bundle arrived,
the cleanup path was removed, and the app was reopened. Then the variants that matter:
a destination that already exists (the update case), an empty `CLEANUP` argument (the
translocated move), and an unwritable destination, which must leave the original
intact and open *it*. That last one is the rollback path, exercised for real rather
than reasoned about.

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
- Deleting the original after a translocated move; it needs Security.framework
  bindings to buy tidiness.
- Migrating the `pkcs11_path` preference out of `localStorage` into `prefs.json`.
  It works where it is; moving it is a separate change with its own risk.
