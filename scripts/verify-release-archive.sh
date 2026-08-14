#!/bin/bash
# Verifica integritatea unei arhive de release inainte de publicare.
#
# DE CE EXISTA ACEST SCRIPT
# -------------------------
# Un bundle .app produs de PyInstaller contine ~45 de symlink-uri si depinde
# de bitii de executie POSIX. Arhivele create cu unelte care nu pastreaza
# aceste atribute (Finder pe alte platforme, `python -m zipfile`, uploadere
# de artefacte, `zip` fara `-y`) distrug bundle-ul in mod silentios:
#
#   - Contents/MacOS/<app> isi pierde bitul de executie
#       -> macOS: "The application <app> can't be opened."   (issues #4, #6)
#   - Contents/Frameworks/python3.X (symlink -> python3__dot__X) devine
#     un director GOL
#       -> ModuleNotFoundError: No module named '_struct'     (issues #6, #7)
#   - Contents/Resources/templates devine un director GOL
#       -> jinja2.exceptions.TemplateNotFound: index.html
#
# Amprenta comuna: un symlink dereferentiat devine un director gol.
# Un build corect NU contine niciun director gol.
#
# Utilizare:
#   scripts/verify-release-archive.sh <arhiva.zip> [referinta.app]
#
# Daca se da si bundle-ul de referinta (cel din dist/), se compara si
# numarul de symlink-uri.

set -euo pipefail

ARCHIVE="${1:-}"
REFERENCE="${2:-}"

if [ -z "$ARCHIVE" ]; then
    echo "Utilizare: $0 <arhiva.zip> [referinta.app]" >&2
    exit 2
fi

if [ ! -f "$ARCHIVE" ]; then
    echo "EROARE: arhiva nu exista: $ARCHIVE" >&2
    exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAILURES=0
fail() { echo "  ✗ $1"; FAILURES=$((FAILURES + 1)); }
pass() { echo "  ✓ $1"; }

echo "=== Verificare arhiva de release ==="
echo "Arhiva: $ARCHIVE"
echo ""

# Extrage exact cum o face macOS pentru utilizator
ditto -x -k "$ARCHIVE" "$TMP/extracted"

APP="$(find "$TMP/extracted" -maxdepth 1 -name '*.app' -print -quit)"
if [ -z "$APP" ]; then
    echo "EROARE: arhiva nu contine niciun bundle .app in radacina" >&2
    exit 1
fi
APP_NAME="$(basename "$APP" .app)"
EXECUTABLE="$APP/Contents/MacOS/$APP_NAME"

echo "--- 1. Structura bundle-ului ---"

if [ -f "$EXECUTABLE" ]; then
    pass "executabilul principal exista"
else
    fail "lipseste $EXECUTABLE"
fi

# Bitul de executie: fara el macOS refuza sa deschida aplicatia
if [ -x "$EXECUTABLE" ]; then
    pass "executabilul are bitul de executie"
else
    fail "executabilul NU are bitul de executie (arhiva a pierdut modurile POSIX)"
fi

echo ""
echo "--- 2. Symlink-uri dereferentiate ---"

# Amprenta principala: symlink-urile dereferentiate devin directoare goale
EMPTY_DIRS="$(find "$APP" -type d -empty || true)"
if [ -z "$EMPTY_DIRS" ]; then
    pass "niciun director gol (symlink-urile sunt intacte)"
else
    COUNT="$(printf '%s\n' "$EMPTY_DIRS" | wc -l | tr -d ' ')"
    fail "$COUNT directoare goale - symlink-uri dereferentiate de arhivator:"
    printf '%s\n' "$EMPTY_DIRS" | sed "s|$APP|      .|"
fi

SYMLINKS="$(find "$APP" -type l | wc -l | tr -d ' ')"
if [ -n "$REFERENCE" ] && [ -d "$REFERENCE" ]; then
    REF_SYMLINKS="$(find "$REFERENCE" -type l | wc -l | tr -d ' ')"
    if [ "$SYMLINKS" -eq "$REF_SYMLINKS" ]; then
        pass "symlink-uri: $SYMLINKS (identic cu build-ul sursa)"
    else
        fail "symlink-uri: $SYMLINKS in arhiva vs $REF_SYMLINKS in $REFERENCE"
    fi
else
    if [ "$SYMLINKS" -gt 0 ]; then
        pass "symlink-uri pastrate: $SYMLINKS"
    else
        fail "arhiva nu contine niciun symlink - toate au fost dereferentiate"
    fi
fi

echo ""
echo "--- 2b. Radacina bundle-ului ---"

# Un bundle .app corect are EXACT un singur element in radacina: Contents.
# Orice altceva este "unsealed content": rupe sigiliul semnaturii
# (`codesign --verify` raporteaza "unsealed contents present in the bundle
# root") si calatoreste in arhiva pana la utilizator.
#
# S-a intamplat: `ln -sf ... "/Applications/CEI PDF Signer.app"` din build.sh
# urmarea symlink-ul deja existent si scria link-ul INAUNTRUL lui
# dist/CEI PDF Signer.app/. Un symlink de 75 de octeti catre calea de pe
# masina de build a fost livrat astfel in v0.10-beta si v0.11-beta.
ROOT_EXTRA="$(ls -A "$APP" | grep -v '^Contents$' || true)"
if [ -z "$ROOT_EXTRA" ]; then
    pass "radacina bundle-ului contine doar Contents"
else
    fail "elemente straine in radacina bundle-ului (rup sigiliul semnaturii):"
    echo "$ROOT_EXTRA" | sed 's/^/      /'
fi

echo ""
echo "--- 2c. Semnatura si notarizarea ---"

# Se verifica arhiva, nu dist/: tichetul de notarizare se lipeste pe .app
# INAINTE de arhivare, iar daca ordinea e gresita el nu ajunge la utilizator.
# Aici il vede exact cum il vede si el.
SIG="$(codesign -dv --verbose=2 "$APP" 2>&1 || true)"
if printf '%s' "$SIG" | grep -q 'flags=.*adhoc'; then
    echo "  - build nesemnat (ad-hoc): utilizatorii trec prin System Settings"
    echo "    la prima deschidere. Pentru un build de distributie ruleaza cu"
    echo "    SIGN_IDENTITY si NOTARY_PROFILE setate."
else
    # Odata ce buildul pretinde ca e semnat, orice abatere este fatala: un
    # bundle care pare semnat dar pe care Gatekeeper il refuza este mai rau
    # decat unul ad-hoc, pentru ca instructiunile noastre nu il mai acopera.
    if printf '%s' "$SIG" | grep -q 'Authority=Developer ID Application'; then
        pass "semnat cu Developer ID Application"
    else
        fail "semnatura nu este Developer ID Application"
    fi

    if printf '%s' "$SIG" | grep -q 'flags=.*runtime'; then
        pass "Hardened Runtime activ"
    else
        fail "Hardened Runtime lipseste - notarizarea il cere"
    fi

    # Fara acest entitlement aplicatia nu porneste deloc sub Hardened Runtime:
    # nu isi incarca nici propriul Python, si cu atat mai putin PKCS#11-ul
    # IDEMIA, semnat de alt team.
    if codesign -d --entitlements - "$APP" 2>/dev/null | grep -q 'disable-library-validation'; then
        pass "entitlementul pentru biblioteca IDEMIA este prezent"
    else
        fail "lipseste com.apple.security.cs.disable-library-validation"
    fi

    if xcrun stapler validate "$APP" >/dev/null 2>&1; then
        pass "tichetul de notarizare este lipit"
    else
        fail "tichet de notarizare lipsa sau invalid (stapler validate a esuat)"
    fi

    # Verdictul care conteaza: exact ce evalueaza macOS la prima deschidere.
    if spctl -a -t exec -vvv "$APP" 2>&1 | grep -q 'accepted'; then
        pass "Gatekeeper ACCEPTA aplicatia"
    else
        fail "Gatekeeper respinge aplicatia:"
        spctl -a -t exec -vvv "$APP" 2>&1 | sed 's/^/      /' | head -4
    fi
fi

echo ""
echo "--- 3. Modulele native Python (_struct & co.) ---"

# lib-dynload e accesibil doar prin symlink-ul python3.X -> python3__dot__X
DYNLOAD="$(find "$APP/Contents/Frameworks" -maxdepth 2 -type d -name 'lib-dynload' -print -quit 2>/dev/null || true)"
if [ -z "$DYNLOAD" ]; then
    fail "lib-dynload negasit in bundle"
else
    VERSIONED="$(find "$APP/Contents/Frameworks" -maxdepth 1 -name 'python3.*' -print -quit 2>/dev/null || true)"
    if [ -z "$VERSIONED" ]; then
        fail "lipseste Contents/Frameworks/python3.X"
    else
        # -L: urmareste symlink-ul; daca e rupt/gol, nu gaseste nimic
        STRUCT="$(find -L "$VERSIONED/lib-dynload" -maxdepth 1 -name '_struct*.so' -print -quit 2>/dev/null || true)"
        if [ -n "$STRUCT" ]; then
            pass "_struct accesibil prin $(basename "$VERSIONED")/lib-dynload"
        else
            fail "$(basename "$VERSIONED")/lib-dynload/_struct*.so INACCESIBIL (symlink rupt)"
        fi
    fi
fi

echo ""
echo "--- 4. Test de pornire (smoke test) ---"

if [ ! -x "$EXECUTABLE" ]; then
    echo "  - sarit: executabilul nu poate fi rulat"
    FAILURES=$((FAILURES + 1))
else
    LOG="$TMP/smoke.log"
    "$EXECUTABLE" >"$LOG" 2>&1 &
    PID=$!

    STARTED=0
    for _ in $(seq 1 60); do
        if grep -q "Running on http" "$LOG" 2>/dev/null; then
            STARTED=1
            break
        fi
        if ! kill -0 "$PID" 2>/dev/null; then
            break
        fi
        perl -e 'select(undef,undef,undef,0.5)'
    done

    STATUS=''
    if [ "$STARTED" -eq 1 ]; then
        pass "aplicatia porneste si serverul Flask raspunde"
        URL="$(sed -n 's/.*Running on \(http:\/\/127[^ ]*\).*/\1/p' "$LOG" | head -1)"
        STATUS="$(curl -fsS --max-time 10 "$URL/api/status" 2>/dev/null || true)"
    else
        fail "aplicatia nu a pornit; output:"
        sed 's/^/      /' "$LOG" | head -20
    fi

    kill "$PID" 2>/dev/null || true
    wait "$PID" 2>/dev/null || true

    # Interogat pe aplicatia impachetata, nu pe sursa: daca fontul nu se
    # rezolva in bundle, aplicatia porneste normal dar revine la Courier si
    # strica numele romanesti in tacere (issue #5).
    if [ -n "$STATUS" ]; then
        case "$STATUS" in
            *'"signature_font_embedded":true'*)
                pass "fontul pentru semnatura este activ in bundle" ;;
            *)
                fail "fontul pentru semnatura NU se rezolva in bundle; diacriticele"
                echo "      vor fi stricate. /api/status a raspuns:"
                echo "      $STATUS" ;;
        esac
    elif [ "$STARTED" -eq 1 ]; then
        fail "nu am putut interoga /api/status pentru verificarea fontului"
    fi
fi

echo ""
if [ "$FAILURES" -eq 0 ]; then
    echo "=== OK: arhiva este valida pentru publicare ==="
    exit 0
else
    echo "=== ESEC: $FAILURES probleme. NU publica aceasta arhiva. ==="
    echo ""
    echo "Recreeaza arhiva cu ditto (pastreaza symlink-uri si permisiuni):"
    echo "  ditto -c -k --sequesterRsrc --keepParent 'CEI PDF Signer.app' arhiva.zip"
    exit 1
fi
