#!/bin/bash
# Script pentru crearea release-ului GitHub
# Compileaza aplicatia si creeaza un ZIP pentru distribuire

set -e

cd "$(dirname "$0")"

# Obtine versiunea din tag-ul git (sau foloseste "dev" daca nu exista tag)
VERSION=$(git describe --tags --abbrev=0 2>/dev/null || echo "dev")

echo "=== CEI PDF Signer - Release Build ==="
echo "Versiune: $VERSION"
echo ""

# Ruleaza build-ul normal (semneaza binarele interioare daca SIGN_IDENTITY e setat)
./build.sh

APP="dist/CEI PDF Signer.app"

# --- Semnare si notarizare (optionale) -------------------------------------
#
#   SIGN_IDENTITY   "Developer ID Application: Nume (TEAMID)"
#   NOTARY_PROFILE  numele profilului salvat cu `notarytool store-credentials`
#
# Fara ele, build-ul iese ad-hoc ca inainte si utilizatorii trec prin
# System Settings la prima deschidere.
if [ -n "${SIGN_IDENTITY:-}" ]; then
    echo ""
    echo "Semnez bundle-ul cu Hardened Runtime..."

    # Bundle-ul exterior, dupa ce PyInstaller a semnat interiorul. --timestamp
    # este ce face semnatura sa ramana valida dupa expirarea certificatului:
    # Gatekeeper compara data semnarii cu valabilitatea certificatului.
    #
    # Entitlement-ul nu este optional. Masurat: cu Hardened Runtime si fara el
    # aplicatia nu porneste deloc, nu isi incarca nici propriul Python
    # ("different Team IDs"). Iar biblioteca PKCS#11 a IDEMIA ramane semnata de
    # alt team (X28J878QBZ) oricum am semna noi.
    codesign --force --options runtime --timestamp \
        --entitlements entitlements.plist \
        --sign "$SIGN_IDENTITY" "$APP"

    codesign --verify --strict --verbose=2 "$APP"
    echo "  semnat: $(codesign -dv --verbose=2 "$APP" 2>&1 | grep -E '^Authority' | head -1)"

    if [ -n "${NOTARY_PROFILE:-}" ]; then
        echo ""
        echo "Trimit la notarizare (dureaza de obicei 1-5 minute)..."
        NOTARIZE_ZIP="$(mktemp -d)/upload.zip"
        ditto -c -k --sequesterRsrc --keepParent "$APP" "$NOTARIZE_ZIP"

        xcrun notarytool submit "$NOTARIZE_ZIP" \
            --keychain-profile "$NOTARY_PROFILE" --wait

        # Lipirea tichetului se face pe .app, INAINTE de arhiva finala. Un
        # ticket lipit dupa arhivare nu ajunge la utilizator, iar verificarea
        # offline pica.
        echo "Lipesc tichetul..."
        xcrun stapler staple "$APP"
        xcrun stapler validate "$APP"
    else
        echo ""
        echo "ATENTIE: NOTARY_PROFILE nu este setat - aplicatia e semnata dar"
        echo "NENOTARIZATA. Gatekeeper o refuza in continuare. Semnatura singura"
        echo "nu este suficienta."
    fi
fi

# Creeaza directorul pentru release
RELEASE_DIR="release"
rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"

# Numele fisierului ZIP
ZIP_NAME="CEI-PDF-Signer-${VERSION}-macOS.zip"

echo ""
echo "Creez arhiva pentru release..."

# IMPORTANT: se foloseste `ditto`, NU `zip`.
#
# Bundle-ul .app contine ~45 de symlink-uri (ex. Contents/Frameworks/python3.14
# -> python3__dot__14) si depinde de bitii de executie POSIX. Orice arhivator
# care nu le pastreaza distruge aplicatia in mod silentios: utilizatorul
# primeste "The application can't be opened." sau
# "ModuleNotFoundError: No module named '_struct'" (issues #4, #6, #7).
#
# `ditto -c -k --sequesterRsrc --keepParent` este metoda suportata de Apple
# pentru arhivarea bundle-urilor si pastreaza symlink-uri, permisiuni si
# atribute extinse. NU inlocui cu `zip`, `python -m zipfile` sau
# "Compress" din alte platforme.
ditto -c -k --sequesterRsrc --keepParent \
    "dist/CEI PDF Signer.app" \
    "$RELEASE_DIR/$ZIP_NAME"

# Calculeaza SHA256 INAINTE de verificare.
#
# Verificatorul are un smoke test care poate esua intermitent (o cursa la
# pornire, vezi README). Cu `set -e`, un asemenea esec oprea scriptul aici si
# SHA256SUMS.txt nu mai era scris niciodata - de aceea v0.10-beta si v0.11-beta
# au fost publicate fara checksum. Suma se calculeaza pe arhiva deja creata, nu
# depinde de verificare, deci nu are ce cauta dupa ea.
echo ""
echo "Calculez SHA256..."
SHA256=$(shasum -a 256 "$RELEASE_DIR/$ZIP_NAME" | cut -d' ' -f1)
echo "$SHA256  $ZIP_NAME" > "$RELEASE_DIR/SHA256SUMS.txt"

# Verifica arhiva rezultata inainte de a o publica. Fara acest pas,
# o arhiva stricata ajunge in Releases si aplicatia nu porneste pe
# niciun calculator in afara celui pe care s-a facut build-ul.
echo ""
./scripts/verify-release-archive.sh \
    "$RELEASE_DIR/$ZIP_NAME" \
    "dist/CEI PDF Signer.app"

echo ""
echo "=== Release Build Complet ==="
echo ""
echo "Fisiere create in folderul '$RELEASE_DIR/':"
echo "  - $ZIP_NAME"
echo "  - SHA256SUMS.txt"
echo ""
echo "SHA256: $SHA256"
echo ""
echo "Pentru a publica pe GitHub:"
echo "  1. git push origin main --tags"
echo "  2. gh release create $VERSION \\"
echo "       '$RELEASE_DIR/$ZIP_NAME' \\"
echo "       '$RELEASE_DIR/SHA256SUMS.txt' \\"
echo "       --repo sudondream/cei-pdf-signer"
echo "  3. Actualizeaza cele doua linkuri de download din docs/index.html"
echo "     (contin versiunea in URL si raman pe versiunea anterioara)"
echo ""
echo "ATENTIE: incarca EXACT fisierul $ZIP_NAME generat mai sus."
echo "Incarca si SHA256SUMS.txt, ca oricine sa poata verifica descarcarea."
echo "Nu re-arhiva aplicatia cu Finder sau cu alte unelte - se pierd"
echo "symlink-urile si aplicatia nu mai porneste (issues #4, #6, #7)."
echo ""
