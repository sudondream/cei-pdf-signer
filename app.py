#!/usr/bin/env python3
"""
CEI Web PDF Signer - Web-based PDF signing using Romanian CEI
Run this server and access via browser at http://localhost:5000
"""

import os
import sys
import base64
import tempfile
import hashlib
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
from werkzeug.utils import secure_filename

# Reader detection through PCSC.framework, which ships with macOS.
import pcsc

# PKCS#11 imports - python-pkcs11 talks to the card. PyKCS11 used to sit here
# too; it is gone. Nothing called it after the switch, and its wildcard import
# dumped ~200 CK* constants into this module's namespace.
try:
    import pkcs11
    PKCS11_AVAILABLE = True
except ImportError:
    PKCS11_AVAILABLE = False
    print("Warning: python-pkcs11 not installed. Install with: pip install 'pyhanko[pkcs11]'")

# PDF signing imports - using pyHanko for proper PKCS#11 ECDSA support
try:
    from pyhanko.sign import signers, fields
    from pyhanko.sign.general import SigningError
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko_certvalidator import ValidationContext
    from pyhanko.stamp import TextStampStyle
    from pyhanko.pdf_utils.text import TextBoxStyle
    from pyhanko.pdf_utils.content import RawContent
    PYHANKO_AVAILABLE = True
except ImportError:
    PYHANKO_AVAILABLE = False
    print("Warning: pyhanko not installed. Install with: pip install pyhanko 'pyhanko[pkcs11]'")

# Embedding a real font is what makes Romanian names render. pyHanko's default
# is Courier, a standard font declared /WinAnsiEncoding, which has no s-comma,
# t-comma or a-breve; pyHanko then writes the whole string as UTF-16BE and a
# simple font reads it byte by byte, so one diacritic garbles the entire line.
try:
    from pyhanko.pdf_utils.font.opentype import GlyphAccumulatorFactory
    EMBEDDED_FONT_AVAILABLE = True
except ImportError:
    EMBEDDED_FONT_AVAILABLE = False
    print("Warning: pyhanko[opentype] not installed, so Romanian diacritics "
          "will not render in signatures. Install with: pip install 'pyHanko[opentype]'")

# Handle bundled app paths (py2app)
if getattr(sys, 'frozen', False):
    # Running as a bundled app
    bundle_dir = os.path.dirname(sys.executable)
    resources_dir = os.path.join(os.path.dirname(bundle_dir), 'Resources')
    template_folder = os.path.join(resources_dir, 'templates')
else:
    # Running as script
    template_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'templates')

app = Flask(__name__, template_folder=template_folder)
CORS(app)
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB max
app.config['UPLOAD_FOLDER'] = tempfile.mkdtemp()

# Default PKCS#11 library for Romanian CEI
DEFAULT_PKCS11_LIB = "/Library/Application Support/com.idemia.idplug/lib/libidplug-pkcs11.2.7.0.dylib"

# Global state
pkcs11_lib = None
pkcs11_session = None


def get_pkcs11_lib_path(custom_path=None):
    """Get PKCS#11 library path - uses custom path if provided, otherwise env var or default"""
    if custom_path and custom_path.strip():
        return custom_path.strip()
    return os.environ.get('PKCS11_LIB', DEFAULT_PKCS11_LIB)


class CallTimeout(Exception):
    """A blocking driver call did not return within its deadline."""


class DetectTimeout(CallTimeout):
    """Reader detection did not finish within its deadline."""


class DriverTimeout(CallTimeout):
    """The PKCS#11 driver accepted a call and never returned.

    Seen for real: two threads inside libidplug deadlock - the notifier holds
    the driver's global lock while parked in SCardGetStatusChange, and the
    caller spins for that lock. Only a reboot clears it.
    """


class BoundedCaller:
    """Runs a blocking call under a deadline, and remembers a wedged one.

    A C call stuck inside a driver cannot be cancelled from Python, so the
    thread running it is abandoned. That is remembered: the frontend re-polls
    every 15s, and without this each poll would strand another thread against
    a driver that is never going to answer.
    """

    def __init__(self, name, timeout_error=CallTimeout):
        self.name = name
        self.timeout_error = timeout_error
        self._lock = threading.Lock()
        self._inflight = None
        self._workers = set()

    def reset(self):
        """Deliberately forget a wedged call, and stop tracking its thread.

        The thread may well still be spinning; this only stops us waiting on
        it. Reserved for tests and for a caller that knows the state is stale.
        """
        with self._lock:
            self._inflight = None
            self._workers = set()

    def is_busy(self):
        """Whether any call we started is still running - wedged ones included.

        Killing the process while one is in flight is what strands the driver,
        so a wedged call counts: it is exactly the case worth not dying inside.
        """
        with self._lock:
            self._workers = {w for w in self._workers if w.is_alive()}
            return bool(self._workers)

    def wait_until_idle(self, timeout, poll=0.1, now=time.monotonic, sleep=time.sleep):
        """Block until no call is running. False if the deadline passes first."""
        deadline = now() + timeout
        while self.is_busy():
            if now() >= deadline:
                return False
            sleep(poll)
        return True

    def call(self, func, timeout):
        with self._lock:
            if self._inflight is not None and self._inflight.is_alive():
                raise self.timeout_error(f'a previous {self.name} call is still blocked')
            self._inflight = None

        outcome = {}

        def run():
            try:
                outcome['value'] = func()
            except BaseException as exc:        # carried across to the caller
                outcome['error'] = exc

        worker = threading.Thread(target=run, daemon=True, name=f'bounded-{self.name}')
        with self._lock:
            self._workers = {w for w in self._workers if w.is_alive()}
            self._workers.add(worker)
        worker.start()
        worker.join(timeout)

        if worker.is_alive():
            with self._lock:
                self._inflight = worker
            raise self.timeout_error(f'no answer from {self.name} within {timeout:g}s')

        if 'error' in outcome:
            raise outcome['error']
        return outcome.get('value')


# A single get_slots() call. SLOT_SETTLE_TIMEOUT and SLOT_WAIT_TIMEOUT bound
# their polling loops, but both are checked only *between* calls - so a call
# that never returns was never bounded by anything.
PKCS11_CALL_TIMEOUT = 20.0

_pkcs11_caller = BoundedCaller('card driver', DriverTimeout)


def get_slots(lib):
    """lib.get_slots(), under a deadline. Raises DriverTimeout if it wedges."""
    return _pkcs11_caller.call(lambda: lib.get_slots(token_present=True),
                               PKCS11_CALL_TIMEOUT)


# How long to hold off quitting while the driver is mid-call. Long enough to
# cover a cold enumeration (~12s observed), because dying inside one is what
# strands the driver and costs the user a reboot.
SHUTDOWN_GRACE = 20.0


def driver_busy():
    """Whether a PKCS#11 call is in flight right now."""
    return _pkcs11_caller.is_busy()


def wait_for_driver(timeout=SHUTDOWN_GRACE):
    """Wait for in-flight PKCS#11 work to finish before the process exits.

    Returns False if it did not finish in time - the call is wedged and will
    never return, so the caller should quit anyway rather than hang.
    """
    return _pkcs11_caller.wait_until_idle(timeout)


# The Idemia driver discovers the card's applications progressively: a cold
# get_slots() returns only slot 1, and slots 2 (ADVANCED SIGNATURE) and 3 (QSCD)
# show up ~20s later. A single snapshot loses that race and reports "slot not found".
SLOT_WAIT_TIMEOUT = 45.0
SLOT_POLL_INTERVAL = 2.0


def find_slot(lib, slot_id, timeout=SLOT_WAIT_TIMEOUT, poll_interval=SLOT_POLL_INTERVAL,
              now=time.monotonic, sleep=time.sleep):
    """Find a PKCS#11 slot by ID, re-enumerating while the driver warms up.

    Returns (slot, seen_slot_ids). slot is None if it never appeared before the
    timeout; seen_slot_ids is from the last enumeration, for the error message.
    """
    deadline = now() + timeout
    seen = []
    while True:
        slots = get_slots(lib)
        seen = [s.slot_id for s in slots]
        for slot in slots:
            if slot.slot_id == slot_id:
                return slot, seen
        if now() >= deadline:
            return None, seen
        sleep(poll_interval)


# Enumerating the card's real slots has to outlast the same warm-up, but must
# finish inside the frontend's 30s abort.
SLOT_SETTLE_TIMEOUT = 20.0

_slot_cache = {}          # lib_path -> [slot dicts]
_slot_lock = threading.Lock()


def clear_slot_cache():
    """Forget cached slots - called when the card is no longer present."""
    with _slot_lock:
        _slot_cache.clear()


# PC/SC normally answers in milliseconds. It can also block: this project has
# watched a call sit in SCardGetStatusChange for 2048 seconds, released only by
# physically re-plugging the reader. SCardEstablishContext and SCardListReaders
# take no timeout argument, so the deadline has to be imposed from outside.
#
# This has to be read together with SLOT_SETTLE_TIMEOUT: a single /api/slots
# call can spend both, one after the other, and the frontend aborts the request
# at 30s. At 10s here the two summed to exactly that abort, so a slow poll was
# cut off in the browser and the user saw the generic "Reader timeout" instead
# of the specific message this endpoint had prepared. 4s is still ~1000x what a
# healthy service needs. TimeoutBudgetTests holds the two files to this.
DETECT_TIMEOUT = 4.0

_detect_caller = BoundedCaller('reader detection', DetectTimeout)


def reset_detection_state():
    """Forget a wedged scan. For tests, and for a caller that knows better."""
    _detect_caller.reset()
    _pkcs11_caller.reset()


def detect_reader(timeout=None):
    """Return (reader_name, card_present) via PC/SC.

    Instant, and works alongside CryptoTokenKit - unlike PKCS#11 enumeration,
    which is slow. Used as a cheap gate before the expensive part.

    Raises pcsc.PCSCError if the PC/SC service cannot be reached, and
    DetectTimeout if it accepts the call but never answers. A blocked C call
    cannot be cancelled from Python, so the thread running it is abandoned -
    and remembered, because the frontend re-polls every 15s and would
    otherwise pile up one abandoned thread per poll against a wedged service.
    """
    timeout = DETECT_TIMEOUT if timeout is None else timeout
    readers = _detect_caller.call(pcsc.list_readers, timeout)

    for name, card_present in readers or []:
        if card_present:
            return name, True
    return None, False


def enumerate_slots(lib, settle_timeout=SLOT_SETTLE_TIMEOUT, poll_interval=SLOT_POLL_INTERVAL,
                    now=time.monotonic, sleep=time.sleep):
    """List the card's real PKCS#11 slots with their token labels.

    Polls until the slot set stops growing (two consecutive reads add nothing)
    or settle_timeout expires, keeping the union of everything seen. The driver
    reveals slots progressively and has been observed dropping one between
    reads, so a single snapshot under-reports.
    """
    deadline = now() + settle_timeout
    found = {}
    stable = 0

    while True:
        try:
            slots = get_slots(lib)
        except DriverTimeout:
            # Not "no slots yet" - the driver is wedged and more polling will
            # not help. Anything else here is still treated as a slow card.
            raise
        except Exception:
            slots = []

        grew = False
        for slot in slots:
            if slot.slot_id in found:
                continue
            grew = True
            try:
                label = (slot.get_token().label or '').strip() or f'Slot {slot.slot_id}'
            except Exception:
                label = f'Slot {slot.slot_id}'
            found[slot.slot_id] = label

        if found and not grew:
            stable += 1
            if stable >= 2:
                break
        else:
            stable = 0

        if now() >= deadline:
            break
        sleep(poll_interval)

    return [{'id': sid, 'label': found[sid]} for sid in sorted(found)]


# Guard against /Parent cycles in malformed page trees.
MAX_PAGE_TREE_DEPTH = 64
DEFAULT_MEDIA_BOX = [0.0, 0.0, 612.0, 792.0]  # US Letter


def unlock_pdf(pdf_reader, password=None):
    """Authenticate an encrypted PDF so its strings and streams can be read.

    Nothing used to call this, so the first read of an encrypted object raised
    "PdfKeyNotAvailableError: No key available to decrypt, please authenticate
    first." The confusing part for users is that such a document opens in any
    viewer without a prompt: publishers routinely encrypt with an owner
    password only, to restrict printing or copying, leaving the user password
    empty. The file is still encrypted, and the key still has to be derived
    before any content can be read.

    So an empty password is tried first, which covers that whole class without
    bothering anybody. Raises ValueError with a message meant for the user when
    the document genuinely needs one.
    """
    if pdf_reader.security_handler is None:
        return

    from pyhanko.pdf_utils.crypt import AuthStatus

    result = pdf_reader.decrypt(password if password is not None else '')
    if result.status != AuthStatus.FAILED:
        return

    if password:
        raise ValueError('Parola introdusa nu este corecta pentru acest PDF.')
    raise ValueError(
        'Acest PDF este protejat cu parola si nu poate fi deschis pentru '
        'semnare. Deschideti-l cu parola si salvati o copie fara protectie, '
        'apoi incercati din nou.')


def get_page_media_box(pdf_reader, page_ix):
    """MediaBox of page `page_ix`, walking the page tree and honouring inheritance.

    Two traps this avoids:
      * /Pages/Kids is a TREE, not a flat page list. Intermediate /Pages nodes
        mean Kids[page_ix] is wrong - IndexError, or silently the wrong page.
      * /MediaBox is an inheritable attribute. A page may carry none and take
        its nearest ancestor's, so defaulting to Letter misplaces signatures
        on A4 by ~50pt.

    Raises pyhanko PdfError if page_ix is out of range; callers should range-check first.
    """
    page_ref, _resources = pdf_reader.find_page_for_modification(page_ix)
    node = page_ref.get_object()

    for _ in range(MAX_PAGE_TREE_DEPTH):
        if node is None:
            break
        # get_object() throughout: in an encrypted document a lookup returns a
        # proxy standing in for the decrypted value, and iterating that raised
        # "TypeError: 'DecryptedObjectProxy' object is not iterable". It is a
        # no-op on a direct object, so it costs nothing to always resolve.
        box = node.get('/MediaBox')
        if box is not None:
            return [float(v.get_object()) for v in box.get_object()]
        parent = node.get('/Parent')
        node = parent.get_object() if parent is not None else None

    # /MediaBox is required by the spec, so reaching here means a broken file.
    return list(DEFAULT_MEDIA_BOX)


# Points kept between a stamp and the page edge when a box has to be slid inside.
BOX_MARGIN = 4.0


def clamp_box(x, y, width, height, media_box, margin=BOX_MARGIN):
    """Slide a box (PDF coords, lower-left origin) inside the media box.

    Returns the (possibly unchanged) lower-left corner. A box drawn on an A4
    portrait page keeps its exact position on every other A4 portrait page; only
    a page that is too small or the wrong orientation moves it. A box larger
    than the page is pinned at the margin rather than pushed off the other side.
    """
    x0, y0, x1, y1 = (float(v) for v in media_box)
    min_x, min_y = x0 + margin, y0 + margin
    max_x, max_y = x1 - width - margin, y1 - height - margin

    cx = min_x if max_x < min_x else min(max(x, min_x), max_x)
    cy = min_y if max_y < min_y else min(max(y, min_y), max_y)
    return cx, cy


def resolve_box(pdf_reader, box, page_count):
    """Frontend box -> (page_ix, x, y, width, height) in PDF coordinates.

    The frontend measures from the top-left of the rendered page; PDF runs from
    the bottom-left of the MediaBox. Result is clamped inside the target page.
    """
    page_ix = int(box.get('page', 1)) - 1
    if page_ix < 0 or page_ix >= page_count:
        raise ValueError(f'Signature box is on page {page_ix + 1}, but this document '
                         f'has {page_count} page(s).')

    width = float(box.get('width', 200))
    height = float(box.get('height', 70))
    media_box = get_page_media_box(pdf_reader, page_ix)

    x = media_box[0] + float(box.get('x', 50))
    y = media_box[3] - float(box.get('y', 50)) - height
    x, y = clamp_box(x, y, width, height, media_box)
    return page_ix, x, y, width, height


def document_has_signature(pdf_reader):
    """True if the PDF already carries a filled-in signature field.

    Stamping writes to page content, which invalidates a pre-existing signature.
    Merely appending a new signature field does not, so this only gates stamping.
    """
    try:
        acro = pdf_reader.root.get('/AcroForm')
        if acro is None:
            return False
        # get_object() on every hop. In an encrypted document these come back
        # as proxies for the decrypted value, and the bare forms silently
        # failed: iterating the proxy raised, the except below turned that
        # into False, and the guard stopped guarding on exactly the documents
        # it was written for.
        fields = acro.get_object().get('/Fields')
        for ref in (fields.get_object() if fields is not None else []):
            field = ref.get_object()
            field_type = field.get('/FT')
            value = field.get('/V')
            if field_type is not None:
                field_type = field_type.get_object()
            if field_type == '/Sig' and value is not None:
                return True
    except Exception:
        return False
    return False


def get_page_count(pdf_reader):
    """Number of pages, resolved through any decryption proxy."""
    pages = pdf_reader.root['/Pages'].get_object()
    count = pages.get('/Count', 0)
    return int(count.get_object() if count is not None else 0)


# DejaVu Sans covers every Romanian letter, in both the correct comma-below
# forms (U+0218/U+021A/U+0219/U+021B) and the legacy cedilla ones certificates
# sometimes carry. Vendored because the font has to travel with the .app: a
# non-embedded font leaves rendering to whatever the viewer substitutes, which
# is not good enough on a document meant to stay valid for years.
SIGNATURE_FONT = os.path.join('assets', 'fonts', 'DejaVuSans-1000upem.ttf')

# Must be handed to the glyph accumulator *and* to TextBoxStyle. They carry
# separate font_size settings, and pyHanko uses the accumulator's to emit the
# advance after each line but the style's to return to the next line's start.
# Leave the accumulator at its default 10 while the style says 28 and every
# line begins further left than the last, walking the text out of its box.
STAMP_FONT_SIZE = 28

# pyHanko defaults leading to the font size, which leaves nothing below the
# baseline. Romanian puts a comma under s and t, and at zero extra leading
# those marks land on top of the line beneath. Enough room for them, and still
# three lines inside the smallest box the UI allows.
STAMP_LEADING = 34


def signature_font_path():
    """Locate the signature font, running from source or from the bundle.

    PyInstaller puts declared data files under sys._MEIPASS, which for a .app
    is Contents/Frameworks. Contents/Resources holds symlinks to the same tree
    and is where main.py chdirs, so both are tried.
    """
    candidates = []
    if getattr(sys, 'frozen', False):
        meipass = getattr(sys, '_MEIPASS', None)
        if meipass:
            candidates.append(os.path.join(meipass, SIGNATURE_FONT))
        bundle_dir = os.path.dirname(sys.executable)
        candidates.append(os.path.join(os.path.dirname(bundle_dir),
                                       'Resources', SIGNATURE_FONT))
    candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   SIGNATURE_FONT))
    for path in candidates:
        if os.path.isfile(path):
            return path
    # Nothing found: hand back the source-tree path so the error names a
    # location a developer can act on.
    return candidates[-1]


def build_font_factory():
    """Font engine for the stamp text, or None to accept pyHanko's default.

    None means Courier, which cannot render Romanian. That is only reached
    when the optional opentype extras or the font asset are missing, and both
    are covered by tests, so it should not happen in a shipped build.
    """
    if not EMBEDDED_FONT_AVAILABLE:
        return None
    path = signature_font_path()
    if not os.path.isfile(path):
        print(f"Warning: signature font missing at {path}; "
              "Romanian diacritics will not render")
        return None
    return GlyphAccumulatorFactory(font_file=path, font_size=STAMP_FONT_SIZE)


def build_stamp_style():
    """The signature appearance, shared by the real field and the page stamps.

    Both go through this so a stamped page looks identical to the signed one.
    """
    seal_graphic = RawContent(
        box=None,
        data=b'''
        q
        0.2 0.4 0.8 RG  % Blue stroke color
        0.9 0.95 1 rg  % Light blue fill
        2 w  % Line width
        50 35 40 30 re S  % Outer rectangle
        0.2 0.4 0.8 rg  % Blue fill for inner elements
        % Draw decorative lines
        15 60 m 135 60 l S  % Top line
        15 10 m 135 10 l S  % Bottom line
        Q
        '''
    )
    font_factory = build_font_factory()
    text_box_style = (
        TextBoxStyle(font=font_factory, font_size=STAMP_FONT_SIZE,
                     leading=STAMP_LEADING)
        if font_factory is not None
        else TextBoxStyle(font_size=STAMP_FONT_SIZE, leading=STAMP_LEADING))
    return TextStampStyle(
        stamp_text='DIGITALLY SIGNED\n%(signer)s\n%(ts)s',
        text_box_style=text_box_style,
        border_width=3,
        border_color=(0.2, 0.4, 0.8),
        background=seal_graphic,
        background_opacity=0.15,
    )


def get_signer_common_name(signer):
    """CN from the signing certificate, for the stamp text."""
    try:
        return signer.signing_cert.subject.native.get('common_name') or 'Unknown'
    except Exception:
        return 'Unknown'


def _ensure_page_contents(pdf_writer, page_ix):
    """Give a page an empty /Contents stream if it has none.

    A page with no /Contents is legal (an entirely blank page), but pyHanko's
    add_stream_to_page does a raw_get('/Contents') and raises KeyError on it.
    """
    from pyhanko.pdf_utils import generic

    page_ref, _ = pdf_writer.find_page_for_modification(page_ix)
    page = page_ref.get_object()
    if '/Contents' in page:
        return

    empty = pdf_writer.add_object(generic.StreamObject(stream_data=b''))
    page[generic.NameObject('/Contents')] = empty
    pdf_writer.update_container(page)


def apply_visual_stamps(pdf_writer, pdf_reader, boxes, page_count,
                        signer_name, timestamp=None):
    """Draw a signature-lookalike stamp for each box. Returns how many were applied.

    These are ordinary page content, not signature fields. Applied before
    sign_pdf(), so the single real signature's byte range covers them - they
    cannot be altered without invalidating it.
    """
    if not boxes:
        return 0

    from pyhanko.pdf_utils.layout import BoxConstraints
    from pyhanko.stamp import TextStamp

    style = build_stamp_style()
    applied = 0
    for box in boxes:
        page_ix, x, y, width, height = resolve_box(pdf_reader, box, page_count)
        _ensure_page_contents(pdf_writer, page_ix)
        params = {'signer': signer_name}
        if timestamp is not None:
            params['ts'] = timestamp
        TextStamp(
            pdf_writer, style,
            text_params=params,
            box=BoxConstraints(width=width, height=height),
        ).apply(page_ix, x, y)
        applied += 1
    return applied


@app.route('/')
def index():
    """Main page"""
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    """Check system status"""
    lib_path = get_pkcs11_lib_path()
    font_path = signature_font_path()
    return jsonify({
        'pkcs11_available': PKCS11_AVAILABLE,
        'pyhanko_available': PYHANKO_AVAILABLE,
        'pkcs11_lib_path': lib_path,
        'pkcs11_lib_exists': os.path.exists(lib_path),
        # Reported so a packaged build can be checked from outside. If the
        # font does not resolve inside the bundle the app still runs, but
        # silently falls back to Courier and mangles Romanian names, which
        # is precisely the failure this is meant to catch.
        'signature_font_path': font_path,
        'signature_font_embedded': EMBEDDED_FONT_AVAILABLE and os.path.isfile(font_path),
    })


@app.route('/api/slots')
def api_slots():
    """Report the card's real PKCS#11 slots.

    Cheap PC/SC presence check first, then actual slot enumeration. The result
    is cached per library path, because enumeration costs ~8-20s on a cold
    driver and the frontend polls this every 15s.
    """
    # Checked before touching the hardware: a build without the binding is
    # broken for everyone, and saying "no card" would blame the user's reader
    # for a packaging defect.
    if not PKCS11_AVAILABLE:
        return jsonify({'slots': [], 'code': 'pkcs11_missing',
                        'error': 'PKCS#11 support is missing from this build. '
                                 'Please reinstall the app.'})

    try:
        reader_name, card_present = detect_reader()
    except DetectTimeout as e:
        return jsonify({'slots': [], 'code': 'detect_timeout',
                        'error': f'The smart card service stopped responding ({e}). '
                                 'Unplug the reader and plug it back in.'})
    except pcsc.PCSCError as e:
        return jsonify({'slots': [], 'code': 'pcsc_unavailable',
                        'error': f'macOS smart card service unavailable: {e}'})
    except Exception as e:
        return jsonify({'slots': [], 'code': 'detect_failed',
                        'error': f'Reader detection failed: {e}'})

    if not card_present:
        # Card pulled - drop the cache so the next insert re-enumerates.
        clear_slot_cache()
        return jsonify({'slots': [], 'code': 'no_card',
                        'error': 'No smart card detected. Please insert your CEI card.'})

    lib_path = get_pkcs11_lib_path(request.args.get('pkcs11_path'))

    # Serialize enumeration: a second concurrent poll should wait and then hit
    # the cache rather than hammer the driver.
    with _slot_lock:
        cached = _slot_cache.get(lib_path)
        if cached:
            return jsonify({'slots': cached})

        try:
            slot_info = enumerate_slots(pkcs11.lib(lib_path))
        except DriverTimeout:
            # The driver took the call and never came back. More polling will
            # not help and neither will re-seating the card: in every observed
            # case only a reboot cleared it, so say that rather than leaving
            # the user retrying a spinner.
            return jsonify({'slots': [], 'code': 'driver_wedged',
                            'error': 'The card driver stopped responding. '
                                     'Please restart your Mac and try again.'})
        except Exception as e:
            # The one case that genuinely points at the PKCS#11 library: it
            # would not load, or the driver behind it refused to talk.
            return jsonify({'slots': [], 'code': 'pkcs11_error',
                            'error': f'Could not read card slots: {e}'})

        if not slot_info:
            return jsonify({'slots': [], 'code': 'no_slots',
                            'error': 'Card detected but no PKCS#11 slots available. '
                                     'Re-seat the card and retry.'})

        for slot in slot_info:
            slot['model'] = reader_name or ''
            slot['manufacturer'] = 'Idemia'

        _slot_cache[lib_path] = slot_info

    return jsonify({'slots': slot_info})


@app.route('/api/certificate', methods=['POST'])
def api_get_certificate():
    """Get certificate from smart card using python-pkcs11"""
    if not PKCS11_AVAILABLE:
        return jsonify({'error': 'python-pkcs11 not installed'}), 500

    data = request.json
    if not data:
        return jsonify({'error': 'Invalid request data'}), 400

    slot_id = int(data.get('slot', 2))
    pin = str(data.get('pin', '')).strip()

    if not pin:
        return jsonify({'error': 'PIN required'}), 400

    session = None
    try:
        custom_path = data.get('pkcs11_path')
        lib_path = get_pkcs11_lib_path(custom_path)
        lib = pkcs11.lib(lib_path)

        target_slot, available = find_slot(lib, slot_id)
        if not target_slot:
            return jsonify({'error': f'Slot {slot_id} not available. Available slots: {available}. Please click "Detect Smart Card" again.'}), 400

        token = target_slot.get_token()
        session = token.open(user_pin=pin)

        # Find certificates
        from pkcs11 import ObjectClass, Attribute
        cert_info = []
        for cert in session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}):
            cert_der = bytes(cert[Attribute.VALUE])
            label = cert.get(Attribute.LABEL, 'Unknown') or 'Unknown'
            if isinstance(label, bytes):
                label = label.decode('utf-8', errors='replace')

            try:
                from cryptography import x509
                from cryptography.hazmat.backends import default_backend
                cert_obj = x509.load_der_x509_certificate(cert_der, default_backend())
                subject = cert_obj.subject.rfc4514_string()
                issuer = cert_obj.issuer.rfc4514_string()
                not_after = cert_obj.not_valid_after_utc.isoformat()

                cert_info.append({
                    'label': label,
                    'subject': subject,
                    'issuer': issuer,
                    'valid_until': not_after,
                    'der_base64': base64.b64encode(cert_der).decode('ascii')
                })
            except Exception:
                cert_info.append({
                    'label': label,
                    'der_base64': base64.b64encode(cert_der).decode('ascii')
                })

        return jsonify({'certificates': cert_info})

    except pkcs11.exceptions.PinIncorrect:
        return jsonify({'error': 'Incorrect PIN. Please check your PIN and try again.'}), 401
    except pkcs11.exceptions.PinLocked:
        return jsonify({'error': 'PIN is locked. Too many incorrect attempts.'}), 401
    except pkcs11.exceptions.TokenNotPresent:
        return jsonify({'error': 'Smart card not detected. Please insert your CEI card.'}), 500
    except pkcs11.exceptions.SlotIDInvalid:
        return jsonify({'error': f'Invalid slot {slot_id}. Please click "Detect Smart Card" and select the correct slot.'}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': f'Error: {str(e)}'}), 500
    finally:
        if session:
            try:
                session.close()
            except Exception:
                pass


@app.route('/api/sign', methods=['POST'])
def api_sign_pdf():
    """Sign a PDF document using pyHanko with PKCS#11"""
    # Signing is the one path that needs both: pyHanko builds the PDF, and
    # python-pkcs11 drives the card that produces the signature.
    if not PYHANKO_AVAILABLE:
        return jsonify({'error': 'pyHanko not installed'}), 500
    if not PKCS11_AVAILABLE:
        return jsonify({'error': 'python-pkcs11 not installed'}), 500

    # Get form data
    if 'pdf' not in request.files:
        return jsonify({'error': 'No PDF file provided'}), 400

    pdf_file = request.files['pdf']
    slot_id = int(request.form.get('slot', 2))
    pin = str(request.form.get('pin', '')).strip()
    reason = request.form.get('reason', 'Document signed with Romanian CEI')
    location = request.form.get('location', 'Romania')
    contact = request.form.get('contact', '')

    # Parse signature boxes from JSON
    import json
    signature_boxes_json = request.form.get('signature_boxes', '[]')
    try:
        signature_boxes = json.loads(signature_boxes_json)
    except:
        signature_boxes = []

    # Use first signature box, or default if none provided
    if signature_boxes:
        box = signature_boxes[0]  # Use first box for the signature field
        sig_page = int(box.get('page', 1)) - 1  # Convert to 0-indexed
        sig_x = float(box.get('x', 50))
        sig_y = float(box.get('y', 50))
        sig_width = float(box.get('width', 200))
        sig_height = float(box.get('height', 70))
        visible = True
    else:
        visible = request.form.get('visible', 'true') == 'true'
        sig_page = int(request.form.get('page', 1)) - 1
        sig_x = float(request.form.get('x', 50))
        sig_y = float(request.form.get('y', 50))
        sig_width = float(request.form.get('width', 200))
        sig_height = float(request.form.get('height', 70))

    if not pin:
        return jsonify({'error': 'PIN required'}), 400

    session = None
    try:
        from io import BytesIO
        from pyhanko.sign.pkcs11 import PKCS11Signer
        from pyhanko.sign.fields import SigFieldSpec, append_signature_field
        from pyhanko.sign import PdfSignatureMetadata
        from pyhanko.sign.signers.pdf_signer import PdfSigner
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

        # Read PDF into BytesIO
        pdf_data = pdf_file.read()
        pdf_input = BytesIO(pdf_data)

        # Get custom PKCS11 path from form data
        custom_path = request.form.get('pkcs11_path')
        lib_path = get_pkcs11_lib_path(custom_path)

        # Load PKCS#11 library and find the right slot
        lib = pkcs11.lib(lib_path)

        target_slot, available = find_slot(lib, slot_id)
        if not target_slot:
            return jsonify({'error': f'Slot {slot_id} not found. Available slots: {available}. '
                                     f'Check that the card is fully seated in the reader.'}), 400

        # Open session with PIN
        token = target_slot.get_token()
        session = token.open(user_pin=pin)

        # Create PKCS#11 signer using pyHanko's built-in support
        # pyHanko handles ECDSA signatures correctly
        signer = PKCS11Signer(
            pkcs11_session=session,
            cert_label='Certificate ECC Advanced Signature',
            key_label='Private Key ECC Advanced Signature',
        )

        # Create signature metadata
        signature_meta = PdfSignatureMetadata(
            field_name='Signature1',
            reason=reason,
            location=location,
            contact_info=contact if contact else None,
        )

        # Prepare PDF writer (allow hybrid xref PDFs)
        from pyhanko.pdf_utils.reader import PdfFileReader
        pdf_reader = PdfFileReader(pdf_input, strict=False)

        # Before anything reads page content. Most encrypted documents carry
        # only an owner password and unlock silently; the rest need telling.
        try:
            unlock_pdf(pdf_reader, password=request.form.get('pdf_password') or None)
        except ValueError as e:
            return jsonify({'error': str(e), 'needs_password': True}), 400

        pdf_writer = IncrementalPdfFileWriter.from_reader(pdf_reader)

        page_count = get_page_count(pdf_reader)

        # Add signature field if visible
        if visible:
            try:
                sig_page, pdf_x, pdf_y, sig_width, sig_height = resolve_box(
                    pdf_reader,
                    {'page': sig_page + 1, 'x': sig_x, 'y': sig_y,
                     'width': sig_width, 'height': sig_height},
                    page_count,
                )
            except ValueError as e:
                return jsonify({'error': str(e)}), 400

            sig_field_spec = SigFieldSpec(
                sig_field_name='Signature1',
                on_page=sig_page,
                box=(pdf_x, pdf_y, pdf_x + sig_width, pdf_y + sig_height),
            )
            append_signature_field(pdf_writer, sig_field_spec)

        # Every box after the first becomes a visual stamp. Only box 0 is a real
        # signature field - one qualified signature covers the whole document,
        # and because stamping happens before sign_pdf() the stamps fall inside
        # its byte range and cannot be altered without invalidating it.
        extra_boxes = signature_boxes[1:] if signature_boxes else []
        if extra_boxes:
            if document_has_signature(pdf_reader):
                return jsonify({'error': 'This document already contains a signature. '
                                         'Stamping every page would modify page content and '
                                         'invalidate it. Keep a single signature box.'}), 400
            try:
                apply_visual_stamps(pdf_writer, pdf_reader, extra_boxes, page_count,
                                    get_signer_common_name(signer))
            except ValueError as e:
                return jsonify({'error': str(e)}), 400

        stamp_style = build_stamp_style()

        # Create PdfSigner with stamp style
        pdf_signer = PdfSigner(
            signature_meta=signature_meta,
            signer=signer,
            stamp_style=stamp_style,
        )

        # Sign the PDF
        pdf_output = BytesIO()
        pdf_signer.sign_pdf(
            pdf_writer,
            output=pdf_output,
        )

        output_data = pdf_output.getvalue()

        # Close the PKCS#11 session
        if session:
            session.close()
            session = None

        # Save to temp file and return
        output_filename = f"signed_{secure_filename(pdf_file.filename)}"
        output_path = os.path.join(app.config['UPLOAD_FOLDER'], output_filename)

        with open(output_path, 'wb') as f:
            f.write(output_data)

        # Return as base64 for download
        return jsonify({
            'success': True,
            'filename': output_filename,
            'data': base64.b64encode(output_data).decode('ascii'),
            'size': len(output_data)
        })

    except pkcs11.PKCS11Error as e:
        error_msg = str(e)
        error_type = type(e).__name__
        if 'PIN' in error_msg.upper():
            return jsonify({'error': 'Incorrect PIN or PIN locked'}), 401
        return jsonify({'error': f'Smart card error ({error_type}): {error_msg}'}), 500
    except Exception as e:
        import traceback
        traceback.print_exc()
        error_type = type(e).__name__
        error_msg = str(e) if str(e) else error_type
        return jsonify({'error': f'{error_type}: {error_msg}'}), 500
    finally:
        # Always close the session
        if session:
            try:
                session.close()
            except:
                pass


# Destinations offered by the About section. The frontend sends one of these
# KEYS, never a URL, so this endpoint cannot be turned into an arbitrary-URL
# (or arbitrary-argument) opener.
ABOUT_LINKS = {
    'website': 'https://plixco.ro',
    'github': 'https://github.com/sudondream',
    'linkedin': 'https://www.linkedin.com/in/sudondream/',
    'email': 'mailto:contact@plixco.ro',
}


@app.route('/api/open-external', methods=['POST'])
def api_open_external():
    """Open an About link in the user's default browser or mail client.

    Needed because pywebview would otherwise follow the link in the app window
    itself, replacing the signer UI with the target page.
    """
    data = request.json or {}
    url = ABOUT_LINKS.get(data.get('key'))
    if not url:
        return jsonify({'error': 'Unknown link'}), 400

    subprocess.run(['open', url], check=False)
    return jsonify({'success': True})


DOWNLOADS_FOLDER = os.path.expanduser('~/Downloads')


def reveal_in_finder(path):
    """Open Finder with the given path selected."""
    subprocess.run(['open', '-R', path], check=False)


@app.route('/api/save-files', methods=['POST'])
def api_save_files():
    """Save signed files to Downloads folder and open in Finder"""
    import zipfile

    data = request.json
    if not data or 'files' not in data:
        return jsonify({'error': 'No files provided'}), 400

    files_data = data['files']
    if not files_data:
        # Never write a zero-entry ZIP - that just looks like a successful save
        # of nothing. If we got here, every document failed to sign.
        return jsonify({'error': 'No signed documents to save - all documents failed to sign.'}), 400

    downloads_folder = DOWNLOADS_FOLDER

    try:
        saved_files = []

        if len(files_data) == 1:
            # Single file - save directly
            file_info = files_data[0]
            file_path = os.path.join(downloads_folder, file_info['name'])
            # Handle duplicate filenames
            base, ext = os.path.splitext(file_path)
            counter = 1
            while os.path.exists(file_path):
                file_path = f"{base}_{counter}{ext}"
                counter += 1

            file_bytes = base64.b64decode(file_info['data'])
            with open(file_path, 'wb') as f:
                f.write(file_bytes)
            saved_files.append(file_path)

            # Open Finder and select the file
            reveal_in_finder(file_path)

        else:
            # Multiple files - create ZIP
            zip_path = os.path.join(downloads_folder, 'signed_documents.zip')
            # Handle duplicate filenames
            base, ext = os.path.splitext(zip_path)
            counter = 1
            while os.path.exists(zip_path):
                zip_path = f"{base}_{counter}{ext}"
                counter += 1

            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for file_info in files_data:
                    file_bytes = base64.b64decode(file_info['data'])
                    zf.writestr(file_info['name'], file_bytes)

            saved_files.append(zip_path)

            # Open Finder and select the ZIP
            reveal_in_finder(zip_path)

        return jsonify({
            'success': True,
            'saved_files': saved_files
        })

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    print("\n" + "="*60)
    print("CEI Web PDF Signer")
    print("="*60)
    print(f"\nPKCS#11 library: {get_pkcs11_lib_path()}")
    print(f"python-pkcs11 available: {PKCS11_AVAILABLE}")
    print(f"pyHanko available: {PYHANKO_AVAILABLE}")
    print("\nOpen your browser and go to: http://localhost:5001")
    print("="*60 + "\n")

    # Use port 5001 to avoid conflict with macOS AirPlay Receiver on port 5000
    app.run(host='127.0.0.1', port=5001, debug=True)
