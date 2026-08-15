# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for CEI PDF Signer
Build with: pyinstaller CEIPDFSigner.spec
"""

import os
import sys

block_cipher = None

# Semnare optionala. Fara SIGN_IDENTITY setat, build-ul iese ad-hoc exact ca
# inainte - altfel nimeni fara certificatul de distributie nu ar mai putea
# construi aplicatia.
#
# PyInstaller semneaza fiecare binar pe care il colecteaza (~200 de fisiere).
# `codesign --deep` pe bundle-ul gata facut NU este un inlocuitor: Apple il
# documenteaza ca nesigur si depreciat, iar binarele interioare nesemnate
# corect sunt exact ce respinge notarizarea.
SIGN_IDENTITY = os.environ.get('SIGN_IDENTITY') or None
ENTITLEMENTS = os.path.join(os.getcwd(), 'entitlements.plist') if SIGN_IDENTITY else None

# Versiunea vine din tag-ul git, prin build.sh. Fara ea, fiecare build spunea
# ca este 1.0.0 si aplicatia nu avea cu ce sa se compare la verificarea
# actualizarilor. numeric_version() se importa din updater.py ca sa nu existe
# doua definitii care pot devia una de alta.
sys.path.insert(0, os.getcwd())
from updater import numeric_version

APP_VERSION = os.environ.get('APP_VERSION') or 'dev'

# Get the path to site-packages
import site
site_packages = site.getsitepackages()[0]

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates', 'templates'),
        ('icon.icns', '.'),
        # Signature appearance font. Without it the stamp falls back to
        # Courier, which cannot render Romanian diacritics.
        ('assets/fonts', 'assets/fonts'),
    ],
    hiddenimports=[
        'app',
        # Reader detection. Listed explicitly for the same reason 'app' is:
        # this bundle is assembled from hidden imports, and a module that only
        # gets reached at runtime is a module that can silently go missing.
        'pcsc',
        'flask',
        'flask_cors',
        'werkzeug',
        'jinja2',
        'jinja2.ext',
        'pyhanko',
        'pyhanko.sign',
        'pyhanko.sign.signers',
        'pyhanko.sign.signers.pdf_signer',
        'pyhanko.sign.signers.pdf_cms',
        'pyhanko.sign.signers.cms_embedder',
        'pyhanko.sign.pkcs11',
        'pyhanko.sign.fields',
        'pyhanko.sign.general',
        'pyhanko.pdf_utils',
        'pyhanko.pdf_utils.incremental_writer',
        'pyhanko.pdf_utils.reader',
        'pyhanko.pdf_utils.writer',
        'pyhanko.pdf_utils.text',
        'pyhanko.pdf_utils.content',
        'pyhanko.pdf_utils.layout',
        'pyhanko.pdf_utils.font',
        'pyhanko.pdf_utils.font.basic',
        # Embeds the signature font so Romanian names render.
        'pyhanko.pdf_utils.font.opentype',
        'fontTools',
        'fontTools.ttLib',
        'fontTools.subset',
        'uharfbuzz',
        'pyhanko.stamp',
        'pyhanko.stamp.text',
        'pyhanko_certvalidator',
        'pkcs11',
        'pkcs11.mechanisms',
        'pkcs11.attributes',
        'pkcs11.constants',
        'pkcs11.types',
        'pkcs11.defaults',
        'cryptography',
        'cryptography.hazmat.backends',
        'cryptography.hazmat.primitives',
        'asn1crypto',
        'webview',
        'certifi',
        'cffi',
        'oscrypto',
        'uritools',
        'qrcode',
        'tzlocal',
        'yaml',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'numpy',
        'scipy',
        'pandas',
        'PIL',
        'cv2',
        'test',
        'tests',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='CEI PDF Signer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=SIGN_IDENTITY,
    entitlements_file=ENTITLEMENTS,
    icon='icon.icns',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='CEI PDF Signer',
)

app = BUNDLE(
    coll,
    name='CEI PDF Signer.app',
    icon='icon.icns',
    bundle_identifier='ro.cei.pdfsigner',
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
)
