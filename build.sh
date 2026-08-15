#!/bin/bash
# Script pentru compilarea aplicatiei CEI PDF Signer
# Creeaza aplicatia nativa macOS folosind PyInstaller

set -e

cd "$(dirname "$0")"

# Versiunea din tag-ul git, exportata pentru CEIPDFSigner.spec. Se calculeaza
# aici, nu in build-release.sh, ca un build normal si unul de release sa nu
# produca aplicatii care raporteaza versiuni diferite.
export APP_VERSION="${APP_VERSION:-$(git describe --tags --abbrev=0 2>/dev/null || echo dev)}"

echo "=== CEI PDF Signer - Build Script ==="
echo "Versiune: $APP_VERSION"
echo ""

# Verifica daca exista environment virtual
if [ ! -d "venv" ]; then
    echo "Creez environment virtual..."
    python3 -m venv venv
fi

# Activeaza environment-ul virtual
source venv/bin/activate

# Instaleaza dependentele daca e nevoie
if [ ! -f "venv/.deps_installed" ]; then
    echo "Instalez dependentele..."
    pip install --upgrade pip
    pip install -r requirements.txt
    touch venv/.deps_installed
fi

# Instaleaza PyInstaller daca nu exista
if ! command -v pyinstaller &> /dev/null; then
    echo "Instalez PyInstaller..."
    pip install pyinstaller
fi

# Curata build-urile anterioare
echo "Curata build-urile anterioare..."
rm -rf build dist

# Compileaza aplicatia
echo "Compilez aplicatia..."
pyinstaller CEIPDFSigner.spec

# Symlink de convenienta in /Applications catre build-ul curent.
#
# ATENTIE la doua capcane, ambele s-au manifestat deja:
#
# 1. `ln -sf` URMARESTE un symlink existent si scrie inauntrul directorului
#    catre care arata. Cum tinta era chiar dist/CEI PDF Signer.app, link-ul
#    ajungea in RADACINA bundle-ului, rupea sigiliul semnaturii si pleca in
#    arhiva: v0.10-beta si v0.11-beta au fost publicate cu el. `-h` opreste
#    urmarirea.
# 2. Daca acolo exista o instalare reala (bundle propriu-zis, nu symlink),
#    nu o atingem: nu stergem si nu scriem in aplicatia instalata de cineva.
APP_LINK="/Applications/CEI PDF Signer.app"
if [ -e "$APP_LINK" ] && [ ! -L "$APP_LINK" ]; then
    echo "Sar peste symlink: $APP_LINK este o instalare reala, nu o ating."
elif ln -sfh "$(pwd)/dist/CEI PDF Signer.app" "$APP_LINK" 2>/dev/null; then
    echo "Symlink creat: $APP_LINK -> dist/"
else
    echo "Nu am putut crea symlinkul in /Applications (permisiuni) - ignor."
fi

echo ""
echo "=== Build Complet ==="
echo ""
echo "Aplicatia se afla in: dist/CEI PDF Signer.app"
echo ""
echo "Pentru a rula:     open 'dist/CEI PDF Signer.app'"
echo "Symlink creat in:  /Applications/CEI PDF Signer.app"
echo ""
