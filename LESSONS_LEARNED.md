# CEI Web Signer - Technical Lessons Learned

## macOS CryptoTokenKit (CTK) and Smart Card Access

### CTK Architecture
- On modern macOS, there is **no standalone `pcscd`**. PC/SC is provided by `ctkpcscd`, an XPC service launched on-demand by `ctkd`.
- Key processes:
  - `ctkd` — CryptoTokenKit daemon (runs as both `_ctkd` system user and current user). Token watcher that monitors smart card readers.
  - `ctkpcscd` — PC/SC daemon (runs as current user). Provides the PC/SC API that all smart card tools use.
  - `ctkahp` — Authentication helper.
- `ctkd` runs in two domains: `system` (as `_ctkd` user) and `gui/<uid>` (as current user).
- `ctkpcscd` is an XPC service started by `ctkd`, not a standalone launchd service.

### CTK Does NOT Block python-pkcs11 — CORRECTED 2026-08-06
- **This section previously claimed CTK holds an exclusive PKCS#11 lock and that all
  PKCS#11 calls hang. That is wrong for `python-pkcs11`, and acting on it caused a
  real outage** (see "The kill_ctkd Regression" below).
- Measured with CTK fully running (`ctkd`, `ctkpcscd`, `ctkahp` all alive), nothing killed:
  - `pkcs11.lib(...)` loads in **0.2s**
  - `get_slots(token_present=True)` returns in **2–19s**
  - opening a public session, reading certificates: works
  - `opensc-tool --list-readers` still works *after* the PKCS#11 call — **no PC/SC poisoning**
- The old note "(This may not apply to python-pkcs11)" was the correct instinct. It doesn't.
- PyKCS11 may still hang — untested since the switch to `python-pkcs11`. Do not
  generalize from it back to `python-pkcs11`.
- **PyKCS11 was removed entirely on 2026-08-06.** No code had called it since the
  switch; it survived only as an unused import, a `/api/status` flag and a bundled
  dependency. Everything card-facing goes through `python-pkcs11`. A test fails the
  build if it is imported or listed as a dependency again.

### Slot Enumeration Is Progressive and Nondeterministic
- The Idemia driver discovers the card's applications **lazily**. Successive runs returned:
  `[1]` → `[1]` → `[1, 2, 3]` → `[1, 2]`.
- A cold `get_slots()` typically returns **only slot 1**. Slot 2 (ADVANCED SIGNATURE) can
  take **12–20 seconds** and several enumerations to appear. Slot 3 (QSCD) sometimes never does.
- **Never take a single snapshot of `get_slots()` and conclude a slot is missing.**
  Poll until the wanted slot appears or a timeout expires — see `find_slot()` in `app.py`.

### Warm-up Is Paid Once Per Process — Don't Cache Sessions
- `pkcs11.lib(path)` is already cached by python-pkcs11 (`lib(p) is lib(p)` → `True`).
- Measured across three sequential sign-shaped operations in one process:

  | request | `lib()` | `find_slot(2)` | `get_token()` |
  |---|---|---|---|
  | 1 (cold) | 0.19s | **7.97s** | 0.19s |
  | 2 | 0.00s | 0.00s | 0.12s |
  | 3 | 0.00s | 0.00s | 0.12s |

- So in the long-lived Flask process, only the **first** file pays the warm-up.
  Batch signing does **not** need a cached PKCS#11 session; adding one would mean
  holding an authenticated session in memory across requests for no measured gain.
- `/api/slots` caches its enumeration result per library path for the same reason
  (first call ~12.5s, subsequent ~0.03s), and drops the cache when the card is removed.

### The kill_ctkd Regression (what NOT to do)
- Commit `6e29cd6` added `kill_ctkd()` into the signing path, escalated to
  `osascript … "pkill -9 ctkd; pkill -9 ctkahp" with administrator privileges`.
- Result: `ctkd` was killed, taking `ctkpcscd` (its XPC child, and the actual PC/SC
  provider) down with it. **The reader went invisible to every tool** while still
  enumerated on USB (`ioreg` showed it present and active). Recovery required a physical re-plug.
- Signing then failed for all files with `Slot 2 not found`, producing 0 signed documents
  and a zero-entry ZIP.
- It was also **entirely unnecessary** — PKCS#11 works fine alongside CTK (above).
- `test_app.py::NoCtkKillTests` now fails the build if any CTK-killing code returns.

### PC/SC Level Access Works Fine
- `opensc-tool --list-readers` works instantly alongside CTK — it uses PC/SC, not PKCS#11.
- `pyscard` (Python PC/SC library) also works instantly — can connect to the card, send APDUs, read ATR.
- The distinction is: **PC/SC = shared access, PKCS#11 = exclusive access (blocked by CTK)**.

### NEVER Kill CTK
- Killing `ctkd` with `pkill -9`:
  - It respawns via launchd in ~0.5 seconds — too fast to sneak in a PKCS#11 call.
  - Keeping a background loop killing it continuously doesn't help — the reader becomes invisible.
- Killing `ctkpcscd`:
  - **Makes the smart card reader completely invisible** to all tools (including `opensc-tool`).
  - The reader stays invisible until re-plugged (sometimes to a different USB port).
- `launchctl disable system/com.apple.ctkpcscd`:
  - **CATASTROPHIC** — persists across reboots, completely disables PC/SC.
  - Must be re-enabled with `launchctl enable system/com.apple.ctkpcscd` (requires sudo).
  - Even after re-enabling, the service may be stuck in a crashed state requiring a **full system restart**.
- Non-sudo `pkill` cannot kill CTK processes running as root (`_ctkd`).
- sudo `pkill` via `osascript` works but causes the problems above.

### Recovery from Broken CTK State
- If CTK services are disabled (`launchctl print-disabled gui/<uid>` shows them as disabled):
  - Re-enable with `launchctl enable gui/<uid>/com.apple.ctkpcscd` and `launchctl enable gui/<uid>/com.apple.ctkahp`.
  - Also re-enable in system domain: `launchctl enable system/com.apple.ctkpcscd` (requires sudo).
- If CTK processes are in a `-9` crashed state (`launchctl list | grep ctk` shows exit code -9):
  - `launchctl start`, `launchctl kickstart`, `launchctl bootstrap` may all fail with I/O errors.
  - **A system restart (or at minimum logout/login) is required** to reset the services.
- Re-plugging the reader (especially to a different USB port) can sometimes restore visibility after a CTK kill.

---

## Romanian CEI (Carte Electronica de Identitate) Smart Card

### Card Hardware
- Manufactured by **Idemia**.
- Uses a **Generic Smart Card Reader Interface** (USB CCID).
- ATR: `3BDF96008131FE458073842 1E05569780000808307900024`
- Reader driver: `fr.apdu.ccid.smartcardccid` (ifd-ccid.bundle).

### PKCS#11 Driver
- Path: `/Library/Application Support/com.idemia.idplug/lib/libidplug-pkcs11.2.7.0.dylib`
- When it works (CTK not holding the reader), it exposes **3 slots**:
  - Slot 1: `PKI User PIN` (Authentication)
  - Slot 2: `ADVANCED SIGNATURE PIN` (Qualified Electronic Signature)
  - Slot 3: `QSCD PIN` (Qualified Signature Creation Device)
- Slot enumeration (`get_slots()`) takes ~7.5 seconds even when it works.
- **The Idemia PKCS#11 library poisons the PC/SC connection**: after any call to it via PyKCS11, even `opensc-tool` stops seeing the reader until re-plug. (Historical — observed via PyKCS11, which is no longer a dependency. Measured *not* to happen with python-pkcs11; see the correction above.)

### Card File Structure
- **Not standard PKCS#15** — `pkcs15-tool` returns "Card is invalid or cannot be handled".
- **No standard AIDs match**: IAS-ECC, ICAO eMRTD, PIV, OpenPGP, Idemia COSMO — all return 6A82 (file not found) or 6A86 (incorrect parameters).
- Master File (3F00) can be selected successfully (SW=9000).
- EF.DIR (2F00) does not exist.
- No child DFs found with standard file IDs (scanned 0x0000-0xFF30 range with P1=01 and P1=02).
- The card uses a **proprietary file structure** that only the Idemia PKCS#11 driver knows how to navigate.

### macOS Smart Card Integration
- `system_profiler SPSmartCardsDataType` sees the reader and ATR but **no CTK token driver claims the card**.
- Installed token drivers (OpenSC, paperLESS vTOKEN, Apple PIV) don't recognize the Idemia CEI.
- `security list-smartcards` returns "No smartcards found" — the card is not in the macOS keychain.
- Despite not recognizing the card, CTK still holds the PC/SC connection and blocks PKCS#11 access.

### Signing Architecture
- pyHanko's `PKCS11Signer` is used for PDF signing with the card.
- Certificate labels: `Certificate ECC Advanced Signature`, key labels: `Private Key ECC Advanced Signature`.
- The card uses **ECDSA** (not RSA) for signatures.
- Signing requires: open PKCS#11 session with PIN, find cert/key by label, pyHanko handles the rest.

---

## PDF Page Tree Traps

### /Pages/Kids Is a Tree, Not a Page List
- `pdf_reader.root['/Pages']['/Kids'][page_ix]` is **wrong**. `/Kids` may contain
  intermediate `/Pages` nodes, each with their own `/Kids`. It only works when the
  tree happens to be flat and one level deep.
- Measured on the real ANMAP documents:

  | file | `/Count` | `len(/Kids)` | kid types | pages where flat indexing failed |
  |---|---|---|---|---|
  | 01_Raspuns_la_intampinare_ANMAP_v9 | 10 | 2 | `/Pages`, `/Pages` | 8 of 10 |
  | 02_Anexa_1_Preturi_echipament_FINAL | 12 | 2 | `/Pages`, `/Pages` | 10 of 12 |
  | 03_Anexa_2_Panou_Fundata_FINAL | 6 | 6 | all `/Page` | 0 of 6 (flat, worked by luck) |

- Word/LibreOffice exports are often flat; anything that has been merged, split, or
  round-tripped through another tool usually is not. Testing on one flat file proves nothing.
- Use pyHanko's `PdfHandler.find_page_for_modification(page_ix)` — it walks the tree properly.

### /MediaBox Is Inheritable
- A `/Page` may carry no `/MediaBox` and inherit it from an ancestor `/Pages` node.
- `page.get('/MediaBox', [0, 0, 612, 792])` therefore silently yields **US Letter** for
  a page that is actually A4 (595.2 × 841.92) — misplacing the signature ~50pt vertically.
- Walk up `/Parent` until a `/MediaBox` is found. See `get_page_media_box()` in `app.py`.
- Known remaining gap: `/Rotate` is also inheritable and is **not** yet accounted for.
  Signature placement on a rotated page will be wrong.

---

## PyInstaller Bundling Issues

### `sys.executable` Points to App Binary
- In a PyInstaller bundle, `sys.executable` points to the app binary (e.g., `CEI PDF Signer.app/.../CEI PDF Signer`), NOT to a Python interpreter.
- Any `subprocess.run([sys.executable, '-c', ...])` will **re-launch the entire application**, causing an infinite loop.
- Fix: avoid subprocess with `sys.executable`; use `multiprocessing` with `fork` start method instead (which doesn't need a Python interpreter path).

### multiprocessing in PyInstaller
- Requires `freeze_support()` at the start of `main.py`.
- Must use `set_start_method('fork')` — the default `spawn` method on macOS would also try to re-launch the binary.

### PATH in Bundled App
- The bundled app may not have `/usr/local/bin` in PATH.
- Must explicitly add it for `opensc-tool` and other system tools to be found:
  ```python
  if '/usr/local/bin' not in os.environ.get('PATH', ''):
      os.environ['PATH'] = '/usr/local/bin:' + os.environ.get('PATH', '')
  ```

---

## Frontend Timing Considerations

- PKCS#11 slot enumeration takes ~7.5 seconds with the Idemia library.
- Frontend must have generous timeouts (30s+) and polling intervals (15s+).
- Must guard against overlapping detection requests (use an `isDetecting` flag).

---

## Viable vs Non-Viable Approaches

### What Works
| Tool/Library | Level | Works with CTK | Notes |
|---|---|---|---|
| `opensc-tool --list-readers` | PC/SC | Yes | Instant, reliable detection |
| `pyscard` (Python) | PC/SC | Yes | Can connect, send APDUs, read ATR |
| `system_profiler SPSmartCardsDataType` | System | Yes | Shows reader + ATR |

### What Doesn't Work (with CTK active)
| Tool/Library | Level | Issue |
|---|---|---|
| ~~PyKCS11~~ | PKCS#11 | Hung indefinitely. **Removed as a dependency 2026-08-06** — nothing called it |
| ~~python-pkcs11~~ | PKCS#11 | **Corrected 2026-08-06: works fine with CTK alive.** Slot enum is slow/progressive, not blocked |
| `pkcs11-tool` | PKCS#11 | Hangs indefinitely |
| `pkcs15-tool` | OpenSC/PC/SC | "Card is invalid" (proprietary card) |

### Approaches That Are Dangerous
| Approach | Why |
|---|---|
| Killing CTK (`pkill ctkd/ctkpcscd`) | Makes reader invisible, may require reboot to recover |
| `launchctl disable` CTK services | Persists across reboots, breaks all smart card access |
| Sudo password prompts in polling loops | Creates infinite password dialog loop |

---

## Open Questions / Future Investigation

1. **Can `pyscard` be used for the full signing flow?** Requires reverse-engineering the Idemia card's proprietary APDU protocol (file selection, PIN verification, certificate reading, signature generation). The PKCS#11 driver knows these APDUs but they're not publicly documented.

2. **Is there a way to make PKCS#11 coexist with CTK?** Perhaps a configuration flag in the Idemia driver, or a macOS setting to exclude the reader from CTK management.

3. **Can we use `sc_auth` or Security framework?** The card isn't recognized by any CTK token driver, so macOS keychain integration doesn't work. A custom CTK token extension could potentially bridge this.

4. **Does the Idemia macOS software include a CTK token driver?** The `com.idemia.idplug` package may have a `.appex` token plugin that should be installed to make CTK work properly with the card, potentially resolving the PKCS#11 conflict.

5. **Would disabling CTK's smart card pairing for this specific reader work?** `sudo defaults write /Library/Preferences/com.apple.security.smartcard DisabledTokens -array-add ...` or similar.
