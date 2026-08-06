# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec file for CEI PDF Signer
Build with: pyinstaller CEIPDFSigner.spec
"""

import os
import sys

block_cipher = None

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
    ],
    hiddenimports=[
        'app',
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
    codesign_identity=None,
    entitlements_file=None,
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
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,
        'LSMinimumSystemVersion': '10.13',
    },
)
