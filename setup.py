"""
py2app build script for CEI PDF Signer

Build with:
    python setup.py py2app

For development/testing:
    python setup.py py2app -A
"""

from setuptools import setup

APP = ['main.py']
DATA_FILES = [
    ('templates', ['templates/index.html']),
]

OPTIONS = {
    'argv_emulation': False,
    'iconfile': 'icon.icns',
    'plist': {
        'CFBundleName': 'CEI PDF Signer',
        'CFBundleDisplayName': 'CEI PDF Signer',
        'CFBundleIdentifier': 'ro.cei.pdfsigner',
        'CFBundleVersion': '1.0.0',
        'CFBundleShortVersionString': '1.0.0',
        'NSHighResolutionCapable': True,
        'NSRequiresAquaSystemAppearance': False,  # Support dark mode
        'LSMinimumSystemVersion': '10.13',
    },
    'packages': [
        'flask',
        'flask_cors',
        'werkzeug',
        'jinja2',
        'pyhanko',
        'pyhanko.sign',
        'pyhanko.pdf_utils',
        'pyhanko.stamp',
        'pyhanko_certvalidator',
        'pkcs11',
        'cryptography',
        'asn1crypto',
        'webview',
        'certvalidator',
        'uritools',
        'oscrypto',
        'qrcode',
        'tzlocal',
        'yaml',
    ],
    'includes': [
        'app',
        'cffi',
        'certifi',
        'pkg_resources',
    ],
    'excludes': [
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
    'resources': [],
    'frameworks': [],
    'semi_standalone': False,
    'site_packages': True,
}

setup(
    name='CEI PDF Signer',
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
