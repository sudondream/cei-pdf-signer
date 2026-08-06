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
