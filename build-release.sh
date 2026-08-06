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

# Ruleaza build-ul normal
./build.sh

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

# Verifica arhiva rezultata inainte de a o publica. Fara acest pas,
# o arhiva stricata ajunge in Releases si aplicatia nu porneste pe
# niciun calculator in afara celui pe care s-a facut build-ul.
echo ""
./scripts/verify-release-archive.sh \
    "$RELEASE_DIR/$ZIP_NAME" \
    "dist/CEI PDF Signer.app"

# Calculeaza SHA256
echo ""
echo "Calculez SHA256..."
SHA256=$(shasum -a 256 "$RELEASE_DIR/$ZIP_NAME" | cut -d' ' -f1)

# Creeaza fisier cu checksums
echo "$SHA256  $ZIP_NAME" > "$RELEASE_DIR/SHA256SUMS.txt"

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
echo "  2. gh release create $VERSION '$RELEASE_DIR/$ZIP_NAME' --repo sudondream/cei-pdf-signer"
echo ""
echo "ATENTIE: incarca EXACT fisierul $ZIP_NAME generat mai sus."
echo "Nu re-arhiva aplicatia cu Finder sau cu alte unelte - se pierd"
echo "symlink-urile si aplicatia nu mai porneste (issues #4, #6, #7)."
echo ""
