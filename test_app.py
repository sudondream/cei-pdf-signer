#!/usr/bin/env python3
"""Tests for the signing flow's non-card logic.

Run: venv/bin/python test_app.py

These cover the two defects that produced "0 of 3 signed, empty zip":
  1. slot lookup took a single snapshot of a driver that enumerates slots
     progressively, so slot 2 was often missing
  2. /api/save-files turned an empty file list into an empty ZIP instead of
     an error

No smart card required.
"""
import ast
import ctypes
import os
import pathlib
import plistlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from unittest import mock

import app as app_module
import pcsc
import updater


class FakeSlot:
    def __init__(self, slot_id):
        self.slot_id = slot_id


class FakeLib:
    """Mimics the Idemia driver warming up: slot 2 only shows up after N calls."""

    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = 0

    def get_slots(self, token_present=True):
        ids = self.sequence[min(self.calls, len(self.sequence) - 1)]
        self.calls += 1
        return [FakeSlot(i) for i in ids]


class FindSlotTests(unittest.TestCase):
    """find_slot must outlast the driver's progressive slot enumeration."""

    def _clock(self):
        """Deterministic fake clock; sleep advances it."""
        state = {'t': 0.0}
        return state, (lambda: state['t']), (lambda d: state.__setitem__('t', state['t'] + d))

    def test_finds_slot_present_immediately(self):
        lib = FakeLib([[1, 2, 3]])
        state, now, sleep = self._clock()
        slot, seen = app_module.find_slot(lib, 2, now=now, sleep=sleep)
        self.assertIsNotNone(slot)
        self.assertEqual(slot.slot_id, 2)
        self.assertEqual(lib.calls, 1, "should not poll again once found")

    def test_finds_slot_that_appears_late(self):
        # Exactly what we measured: cold enumeration returns only slot 1.
        lib = FakeLib([[1], [1], [1, 2, 3]])
        state, now, sleep = self._clock()
        slot, seen = app_module.find_slot(lib, 2, now=now, sleep=sleep)
        self.assertIsNotNone(slot, "slot 2 appears on the 3rd enumeration; must keep polling")
        self.assertEqual(slot.slot_id, 2)
        self.assertEqual(seen, [1, 2, 3])

    def test_gives_up_after_timeout_and_reports_what_it_saw(self):
        lib = FakeLib([[1]])
        state, now, sleep = self._clock()
        slot, seen = app_module.find_slot(lib, 2, timeout=10, poll_interval=2,
                                          now=now, sleep=sleep)
        self.assertIsNone(slot)
        self.assertEqual(seen, [1], "caller needs the observed slot IDs for the error message")
        self.assertGreaterEqual(state['t'], 10, "must actually wait out the timeout")


class SaveFilesTests(unittest.TestCase):
    """/api/save-files must refuse an empty list instead of writing an empty ZIP."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        # Never touch the real ~/Downloads, and never pop Finder open.
        patcher = mock.patch.object(app_module, 'DOWNLOADS_FOLDER', self.tmp)
        patcher.start()
        self.addCleanup(patcher.stop)
        reveal = mock.patch.object(app_module, 'reveal_in_finder')
        reveal.start()
        self.addCleanup(reveal.stop)

    def test_empty_file_list_is_an_error_not_an_empty_zip(self):
        resp = self.client.post('/api/save-files', json={'files': []})
        self.assertEqual(resp.status_code, 400)
        self.assertIn('error', resp.get_json())
        self.assertEqual(os.listdir(self.tmp), [],
                         "nothing should be written to Downloads when there is nothing to save")

    def test_multiple_files_still_zip(self):
        import base64
        payload = {'files': [
            {'name': 'a.pdf', 'data': base64.b64encode(b'aaa').decode()},
            {'name': 'b.pdf', 'data': base64.b64encode(b'bbb').decode()},
        ]}
        resp = self.client.post('/api/save-files', json=payload)
        self.assertEqual(resp.status_code, 200)
        zips = [f for f in os.listdir(self.tmp) if f.endswith('.zip')]
        self.assertEqual(len(zips), 1)
        with zipfile.ZipFile(os.path.join(self.tmp, zips[0])) as zf:
            self.assertEqual(sorted(zf.namelist()), ['a.pdf', 'b.pdf'])

    def test_single_file_saved_directly(self):
        import base64
        payload = {'files': [{'name': 'solo.pdf', 'data': base64.b64encode(b'xyz').decode()}]}
        resp = self.client.post('/api/save-files', json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(os.listdir(self.tmp), ['solo.pdf'])


class FakeToken:
    def __init__(self, label):
        self.label = label


class LabelledSlot:
    def __init__(self, slot_id, label):
        self.slot_id = slot_id
        self._label = label

    def get_token(self):
        return FakeToken(self._label)


class LabelledLib:
    def __init__(self, sequence):
        self.sequence = list(sequence)
        self.calls = 0

    def get_slots(self, token_present=True):
        entry = self.sequence[min(self.calls, len(self.sequence) - 1)]
        self.calls += 1
        return [LabelledSlot(i, f'token-{i}') for i in entry]


class EnumerateSlotsTests(unittest.TestCase):
    """/api/slots must report the card's real slots, not a hardcoded 1/2/3."""

    def _clock(self):
        state = {'t': 0.0}
        return state, (lambda: state['t']), (lambda d: state.__setitem__('t', state['t'] + d))

    def test_waits_for_progressive_enumeration(self):
        # Driver reveals slots over time, then settles.
        lib = LabelledLib([[1], [1, 2], [1, 2, 3], [1, 2, 3], [1, 2, 3]])
        _, now, sleep = self._clock()
        slots = app_module.enumerate_slots(lib, now=now, sleep=sleep)
        self.assertEqual([s['id'] for s in slots], [1, 2, 3])
        self.assertEqual(slots[1]['label'], 'token-2')

    def test_keeps_union_when_driver_drops_a_slot(self):
        # Observed for real: [1,2,3] on one read, [1,2] on the next.
        lib = LabelledLib([[1, 2, 3], [1, 2], [1, 2]])
        _, now, sleep = self._clock()
        slots = app_module.enumerate_slots(lib, now=now, sleep=sleep)
        self.assertEqual([s['id'] for s in slots], [1, 2, 3],
                         "a slot vanishing between reads must not un-list it")

    def test_returns_empty_when_no_slots_ever_appear(self):
        lib = LabelledLib([[]])
        _, now, sleep = self._clock()
        slots = app_module.enumerate_slots(lib, settle_timeout=10, poll_interval=2,
                                           now=now, sleep=sleep)
        self.assertEqual(slots, [])

    def test_no_hardcoded_slot_labels_in_source(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'app.py')) as fh:
            src = fh.read()
        for needle in ("'PKI User PIN (Authentication)'",
                       "'ADVANCED SIGNATURE PIN (Signing)'",
                       "'QSCD PIN'"):
            self.assertFalse(needle in src,
                             f"slot list must come from the card, not the literal {needle}")


class SlotsEndpointTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        app_module.clear_slot_cache()
        self.addCleanup(app_module.clear_slot_cache)

    def test_no_card_reports_absence_and_clears_cache(self):
        app_module._slot_cache['x'] = [{'id': 1}]
        with mock.patch.object(app_module, 'detect_reader', return_value=(None, False)):
            resp = self.client.get('/api/slots')
        self.assertEqual(resp.get_json()['slots'], [])
        self.assertEqual(app_module._slot_cache, {}, "cache must drop when the card leaves")

    def test_card_present_returns_enumerated_slots_and_caches(self):
        enum = mock.Mock(return_value=[{'id': 2, 'label': 'ADVANCED SIGNATURE PIN'}])
        with mock.patch.object(app_module, 'detect_reader', return_value=('Reader X', True)), \
             mock.patch.object(app_module, 'enumerate_slots', enum), \
             mock.patch.object(app_module.pkcs11, 'lib', mock.Mock()):
            first = self.client.get('/api/slots').get_json()
            second = self.client.get('/api/slots').get_json()

        self.assertEqual([s['id'] for s in first['slots']], [2])
        self.assertEqual(first['slots'][0]['model'], 'Reader X')
        self.assertEqual(first, second)
        self.assertEqual(enum.call_count, 1, "second poll must be served from cache")


def build_nested_page_tree_pdf():
    """Minimal PDF whose /Pages/Kids is a TREE, not a flat page list.

    Root /Pages has a single intermediate /Pages kid holding the 2 real pages,
    and /MediaBox lives only on the root - so the pages inherit it. Both traits
    are present in the user's real documents and both broke the old code.
    """
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 2 /MediaBox [0 0 595.2 841.92] >>",
        b"<< /Type /Pages /Parent 2 0 R /Kids [4 0 R 5 0 R] /Count 2 >>",
        b"<< /Type /Page /Parent 3 0 R /Resources << >> >>",
        b"<< /Type /Page /Parent 3 0 R /Resources << >> >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objs) + 1)
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objs) + 1, xref_at)
    return bytes(out)


class PageTreeTests(unittest.TestCase):
    """/Pages/Kids is a tree; indexing it by page number is wrong."""

    def setUp(self):
        from io import BytesIO
        from pyhanko.pdf_utils.reader import PdfFileReader
        self.reader = PdfFileReader(BytesIO(build_nested_page_tree_pdf()), strict=False)

    def test_flat_kids_indexing_is_the_bug(self):
        # Guards the premise: 2 pages, but only 1 entry in the root's /Kids.
        kids = self.reader.root['/Pages']['/Kids']
        self.assertEqual(len(kids), 1)
        self.assertEqual(int(self.reader.root['/Pages']['/Count']), 2)
        with self.assertRaises(IndexError):
            kids[1]

    def test_media_box_found_for_last_page_via_tree_walk(self):
        box = app_module.get_page_media_box(self.reader, 1)
        self.assertEqual([round(v, 2) for v in box], [0.0, 0.0, 595.2, 841.92])

    def test_media_box_is_inherited_not_defaulted_to_letter(self):
        box = app_module.get_page_media_box(self.reader, 0)
        self.assertNotEqual([round(v) for v in box], [0, 0, 612, 792],
                            "must inherit A4 from the parent, not fall back to Letter")

    def test_out_of_range_page_raises(self):
        from pyhanko.pdf_utils.misc import PdfError
        with self.assertRaises(PdfError):
            app_module.get_page_media_box(self.reader, 5)


A4 = [0.0, 0.0, 595.2, 841.92]
A4_LANDSCAPE = [0.0, 0.0, 841.92, 595.2]


class ClampBoxTests(unittest.TestCase):
    """Same spot when it fits; slid inside the page when it doesn't."""

    def test_box_that_fits_is_untouched(self):
        x, y = app_module.clamp_box(400, 100, 150, 70, A4)
        self.assertEqual((x, y), (400, 100))

    def test_box_overflowing_top_slides_down(self):
        # y is the lower-left corner; 800 + 70 > 841.92
        x, y = app_module.clamp_box(400, 800, 150, 70, A4)
        self.assertEqual(x, 400)
        self.assertAlmostEqual(y, 841.92 - 70 - app_module.BOX_MARGIN, places=2)

    def test_box_overflowing_right_slides_left(self):
        x, y = app_module.clamp_box(700, 100, 150, 70, A4)
        self.assertAlmostEqual(x, 595.2 - 150 - app_module.BOX_MARGIN, places=2)
        self.assertEqual(y, 100)

    def test_negative_coords_pushed_to_margin(self):
        x, y = app_module.clamp_box(-50, -50, 150, 70, A4)
        self.assertEqual((x, y), (app_module.BOX_MARGIN, app_module.BOX_MARGIN))

    def test_landscape_page(self):
        # x=600 overflows A4 portrait (600+150 > 595.2) but fits landscape.
        x, _ = app_module.clamp_box(600, 100, 150, 70, A4)
        self.assertAlmostEqual(x, 595.2 - 150 - app_module.BOX_MARGIN, places=2)
        x, y = app_module.clamp_box(600, 100, 150, 70, A4_LANDSCAPE)
        self.assertEqual((x, y), (600, 100))

    def test_box_larger_than_page_pins_at_margin(self):
        x, y = app_module.clamp_box(10, 10, 5000, 5000, A4)
        self.assertEqual((x, y), (app_module.BOX_MARGIN, app_module.BOX_MARGIN),
                         "an oversized box must not be pushed to negative coords")

    def test_honours_nonzero_media_box_origin(self):
        offset = [100.0, 200.0, 695.2, 1041.92]
        x, y = app_module.clamp_box(0, 0, 150, 70, offset)
        self.assertEqual((x, y), (100 + app_module.BOX_MARGIN, 200 + app_module.BOX_MARGIN))


class ResolveBoxTests(unittest.TestCase):
    """Frontend box (top-left origin) -> PDF box (bottom-left origin), clamped."""

    def setUp(self):
        from io import BytesIO
        from pyhanko.pdf_utils.reader import PdfFileReader
        self.reader = PdfFileReader(BytesIO(build_nested_page_tree_pdf()), strict=False)

    def test_flips_y_axis(self):
        box = {'page': 1, 'x': 100, 'y': 50, 'width': 150, 'height': 70}
        page_ix, x, y, w, h = app_module.resolve_box(self.reader, box, 2)
        self.assertEqual(page_ix, 0)
        self.assertEqual(x, 100)
        # top-left y=50 with height 70 -> lower-left y = 841.92 - 50 - 70
        self.assertAlmostEqual(y, 841.92 - 50 - 70, places=2)
        self.assertEqual((w, h), (150, 70))

    def test_second_page_resolves_via_tree(self):
        box = {'page': 2, 'x': 10, 'y': 10, 'width': 50, 'height': 20}
        page_ix, x, y, w, h = app_module.resolve_box(self.reader, box, 2)
        self.assertEqual(page_ix, 1)

    def test_page_out_of_range_rejected(self):
        box = {'page': 9, 'x': 10, 'y': 10, 'width': 50, 'height': 20}
        with self.assertRaises(ValueError):
            app_module.resolve_box(self.reader, box, 2)


class ExistingSignatureTests(unittest.TestCase):
    def test_unsigned_document_reports_false(self):
        from io import BytesIO
        from pyhanko.pdf_utils.reader import PdfFileReader
        reader = PdfFileReader(BytesIO(build_nested_page_tree_pdf()), strict=False)
        self.assertFalse(app_module.document_has_signature(reader))


class StampAllPagesTests(unittest.TestCase):
    """Extra boxes must become real page content, not be silently dropped."""

    def test_stamps_are_applied_to_every_listed_page(self):
        from io import BytesIO
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

        reader = PdfFileReader(BytesIO(build_nested_page_tree_pdf()), strict=False)
        writer = IncrementalPdfFileWriter.from_reader(reader)

        boxes = [
            {'page': 1, 'x': 100, 'y': 50, 'width': 150, 'height': 70},
            {'page': 2, 'x': 100, 'y': 50, 'width': 150, 'height': 70},
        ]
        # box 0 is the real signature field; box 1 becomes a stamp
        applied = app_module.apply_visual_stamps(writer, reader, boxes[1:], 2,
                                                 'ADRIAN BANCU', '2026-08-06 12:00')
        self.assertEqual(applied, 1)

        out = BytesIO()
        writer.write(out)
        signed = PdfFileReader(BytesIO(out.getvalue()), strict=False)
        page_ref, _ = signed.find_page_for_modification(1)
        self.assertIn('/Contents', page_ref.get_object(),
                      "stamped page must carry a content stream")

    def test_no_extra_boxes_is_a_noop(self):
        from io import BytesIO
        from pyhanko.pdf_utils.reader import PdfFileReader
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
        reader = PdfFileReader(BytesIO(build_nested_page_tree_pdf()), strict=False)
        writer = IncrementalPdfFileWriter.from_reader(reader)
        self.assertEqual(
            app_module.apply_visual_stamps(writer, reader, [], 2, 'X', 'Y'), 0)


class OpenExternalTests(unittest.TestCase):
    """About links must open in the real browser, and only known destinations.

    pywebview navigates the app window itself on a normal link, which would
    strand the user on an external page with no way back to the app.
    """

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    def test_known_key_opens_the_right_url(self):
        with mock.patch.object(app_module.subprocess, 'run') as run:
            resp = self.client.post('/api/open-external', json={'key': 'github'})
        self.assertEqual(resp.status_code, 200)
        run.assert_called_once()
        self.assertEqual(run.call_args[0][0],
                         ['open', 'https://github.com/sudondream'])

    def test_linkedin_opens_the_right_url(self):
        with mock.patch.object(app_module.subprocess, 'run') as run:
            resp = self.client.post('/api/open-external', json={'key': 'linkedin'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(run.call_args[0][0],
                         ['open', 'https://www.linkedin.com/in/sudondream/'])

    def test_every_about_link_is_reachable(self):
        for key in ('website', 'github', 'linkedin', 'email'):
            with mock.patch.object(app_module.subprocess, 'run') as run:
                resp = self.client.post('/api/open-external', json={'key': key})
            self.assertEqual(resp.status_code, 200, key)
            run.assert_called_once()

    def test_unknown_key_is_rejected_and_opens_nothing(self):
        with mock.patch.object(app_module.subprocess, 'run') as run:
            resp = self.client.post('/api/open-external', json={'key': 'nope'})
        self.assertEqual(resp.status_code, 400)
        run.assert_not_called()

    def test_raw_url_cannot_be_injected(self):
        # The frontend sends a key, never a URL, so this must not be usable as
        # an arbitrary-URL or arbitrary-argument opener.
        for hostile in ('https://evil.example.com', 'file:///etc/passwd',
                        '/Applications/Calculator.app', '-a', ''):
            with mock.patch.object(app_module.subprocess, 'run') as run:
                resp = self.client.post('/api/open-external', json={'key': hostile})
            self.assertEqual(resp.status_code, 400, hostile)
            run.assert_not_called()

    def test_missing_body_is_rejected(self):
        with mock.patch.object(app_module.subprocess, 'run') as run:
            resp = self.client.post('/api/open-external', json={})
        self.assertEqual(resp.status_code, 400)
        run.assert_not_called()


class NoCtkKillTests(unittest.TestCase):
    """Killing CryptoTokenKit made the reader invisible to PC/SC until re-plug.

    PKCS#11 works fine alongside CTK, so the kill must be gone for good.
    """

    def test_kill_ctkd_no_longer_exists(self):
        self.assertFalse(hasattr(app_module, 'kill_ctkd'),
                         "kill_ctkd tears down ctkpcscd and blinds the reader; it must not come back")

    def test_source_has_no_ctk_pkill(self):
        # main.py is checked too: it is what launches the app, and a stale
        # "Kill CTK, start Flask" docstring survived there once already.
        here = os.path.dirname(os.path.abspath(__file__))
        for filename in ('app.py', 'main.py'):
            with open(os.path.join(here, filename)) as fh:
                src = fh.read()
            for needle in ('pkill', 'ctkd', 'ctkahp', 'Kill CTK',
                           'with administrator privileges'):
                self.assertFalse(needle in src,
                                 f"{filename} must not reference {needle!r}")


class NoPyKCS11Tests(unittest.TestCase):
    """PyKCS11 was replaced by python-pkcs11 and must not creep back.

    It hung on the Idemia driver, and a wildcard `from PyKCS11 import *`
    dumps ~200 CK* names into module scope where they can shadow anything.
    Shipping it also means bundling a dependency nothing calls.
    """

    SOURCES = ('app.py', 'main.py', 'setup.py', 'CEIPDFSigner.spec', 'requirements.txt')

    # Matches importing it or listing it as a dependency, not merely naming it -
    # the comment in app.py explaining why it was dropped has to stay legal.
    REINTRODUCED = re.compile(
        r'^\s*(?:import\s+PyKCS11|from\s+PyKCS11\b|["\']PyKCS11["\'],?|PyKCS11\s*[><=])',
        re.MULTILINE,
    )

    def test_no_source_imports_or_ships_pykcs11(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for filename in self.SOURCES:
            with open(os.path.join(here, filename)) as fh:
                src = fh.read()
            hit = self.REINTRODUCED.search(src)
            self.assertIsNone(hit,
                              f"{filename} imports or ships PyKCS11 ({hit.group(0).strip()!r} "
                              f"at char {hit.start()}); the card is driven by python-pkcs11"
                              if hit else '')

    def test_wildcard_import_did_not_leak_ck_constants(self):
        """`from PyKCS11 import *` dumped every CK* name into app's namespace.

        Checked separately from the source scan because this is the concrete
        harm: ~200 unqualified constants able to shadow module-level names.
        """
        for leaked in ('CKA_CLASS', 'CKO_CERTIFICATE', 'CKF_SERIAL_SESSION'):
            self.assertFalse(hasattr(app_module, leaked),
                             f"{leaked} leaked into app.py via a wildcard import")

    def test_status_reports_the_library_actually_used(self):
        """pkcs11_available must track python-pkcs11, not the dropped binding.

        The frontend gates its "dependencies missing" warning on this flag, so
        pointing it at an unused library warns about the wrong thing.
        """
        client = app_module.app.test_client()
        payload = client.get('/api/status').get_json()
        self.assertTrue(payload['pkcs11_available'],
                        "python-pkcs11 is importable, so the flag must be True")


def build_encrypted_pdf(owner_pass='owner-secret', user_pass=''):
    """A one page PDF with standard security. Empty user_pass is the common case.

    Insurers and banks routinely ship documents encrypted with an owner
    password only. They open in any viewer with no prompt, so users have no
    idea they are encrypted, but their strings and streams still have to be
    decrypted before anything can be read.
    """
    from io import BytesIO
    from pyhanko.pdf_utils.writer import PdfFileWriter
    from pyhanko.pdf_utils import generic

    writer = PdfFileWriter()
    contents = writer.add_object(generic.StreamObject(stream_data=b'BT ET'))
    writer.insert_page(generic.DictionaryObject({
        generic.NameObject('/Type'): generic.NameObject('/Page'),
        generic.NameObject('/MediaBox'): generic.ArrayObject(
            map(generic.NumberObject, [0, 0, 595, 842])),
        generic.NameObject('/Resources'): generic.DictionaryObject(),
        generic.NameObject('/Contents'): contents,
    }))
    writer.encrypt(owner_pass=owner_pass, user_pass=user_pass)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


class EncryptedPdfTests(unittest.TestCase):
    """Encrypted PDFs must be unlocked before anything reads their content.

    Reported as "PdfKeyNotAvailableError: No key available to decrypt, please
    authenticate first." on an RCA renewal document. Nothing ever called
    decrypt(), so the first read of an encrypted string or stream failed. The
    document opened fine in a viewer, because it carried only an owner
    password, which is why it looked like the app was at fault for no reason.
    """

    def _reader(self, data):
        from io import BytesIO
        from pyhanko.pdf_utils.reader import PdfFileReader
        return PdfFileReader(BytesIO(data), strict=False)

    def test_owner_password_only_is_unlocked_automatically(self):
        """The common case: no user password, so no reason to ask the user."""
        reader = self._reader(build_encrypted_pdf())
        app_module.unlock_pdf(reader)
        page = reader.find_page_for_modification(0)[0].get_object()
        self.assertEqual(page.raw_get('/Contents').get_object().data, b'BT ET',
                         "content stream must be readable once unlocked")

    def test_unencrypted_pdf_is_left_alone(self):
        reader = self._reader(build_nested_page_tree_pdf())
        app_module.unlock_pdf(reader)  # must not raise

    def test_real_user_password_reports_something_actionable(self):
        """A genuinely password protected file needs a message, not a traceback."""
        reader = self._reader(build_encrypted_pdf(user_pass='hunter2'))
        with self.assertRaises(ValueError) as caught:
            app_module.unlock_pdf(reader)
        message = str(caught.exception).lower()
        self.assertIn('parol', message,
                      "the message has to tell the user it wants a password")

    def test_supplied_password_unlocks_the_document(self):
        reader = self._reader(build_encrypted_pdf(user_pass='hunter2'))
        app_module.unlock_pdf(reader, password='hunter2')
        page = reader.find_page_for_modification(0)[0].get_object()
        self.assertEqual(page.raw_get('/Contents').get_object().data, b'BT ET')

    def test_media_box_survives_decryption(self):
        """Values read out of an encrypted document arrive wrapped.

        In an encrypted file a dictionary lookup hands back a proxy standing in
        for the decrypted object, so iterating it directly raised
        "TypeError: 'DecryptedObjectProxy' object is not iterable" and the
        signature never got placed. Separate from the missing decrypt() call,
        and only reachable once that one is fixed.
        """
        reader = self._reader(build_encrypted_pdf())
        app_module.unlock_pdf(reader)
        self.assertEqual(app_module.get_page_media_box(reader, 0),
                         [0.0, 0.0, 595.0, 842.0])

    def test_existing_signature_is_still_detected_when_encrypted(self):
        """The guard against stamping over a signature must not quietly lapse.

        document_has_signature swallows exceptions and reports False, so once
        an encrypted document made the field lookup raise, the guard silently
        stopped guarding: the app would stamp page content on an already
        signed file and invalidate the signature it was meant to protect.
        """
        from io import BytesIO
        from pyhanko.pdf_utils.writer import PdfFileWriter
        from pyhanko.pdf_utils import generic

        def build(encrypt):
            writer = PdfFileWriter()
            writer.insert_page(generic.DictionaryObject({
                generic.NameObject('/Type'): generic.NameObject('/Page'),
                generic.NameObject('/MediaBox'): generic.ArrayObject(
                    map(generic.NumberObject, [0, 0, 595, 842])),
                generic.NameObject('/Resources'): generic.DictionaryObject(),
            }))
            field = writer.add_object(generic.DictionaryObject({
                generic.NameObject('/FT'): generic.NameObject('/Sig'),
                generic.NameObject('/T'): generic.TextStringObject('Signature1'),
                generic.NameObject('/V'): generic.DictionaryObject({
                    generic.NameObject('/Type'): generic.NameObject('/Sig'),
                }),
            }))
            writer.root[generic.NameObject('/AcroForm')] = writer.add_object(
                generic.DictionaryObject({
                    generic.NameObject('/Fields'): generic.ArrayObject([field]),
                }))
            writer.update_root()
            if encrypt:
                writer.encrypt(owner_pass='owner-secret', user_pass='')
            buf = BytesIO()
            writer.write(buf)
            return buf.getvalue()

        plain = self._reader(build(encrypt=False))
        self.assertTrue(app_module.document_has_signature(plain),
                        "sanity: a signed document must be detected when unencrypted")

        encrypted = self._reader(build(encrypt=True))
        app_module.unlock_pdf(encrypted)
        self.assertTrue(app_module.document_has_signature(encrypted),
                        "encryption must not disable the existing-signature guard")

    def test_page_count_is_readable_when_encrypted(self):
        reader = self._reader(build_encrypted_pdf())
        app_module.unlock_pdf(reader)
        self.assertEqual(app_module.get_page_count(reader), 1)

    def test_stamping_works_on_an_encrypted_document(self):
        """End to end: the path that failed for the reporter."""
        from io import BytesIO
        from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter

        reader = self._reader(build_encrypted_pdf())
        app_module.unlock_pdf(reader)
        writer = IncrementalPdfFileWriter.from_reader(reader)
        applied = app_module.apply_visual_stamps(
            writer, reader,
            [{'page': 1, 'x': 40, 'y': 40, 'width': 300, 'height': 90}],
            1, 'ADRIAN BANCU', '2026-08-06 12:00')
        self.assertEqual(applied, 1)
        out = BytesIO()
        writer.write(out)
        self.assertTrue(out.getvalue().startswith(b'%PDF'))


class RomanianDiacriticsTests(unittest.TestCase):
    """The signature appearance has to render Romanian names correctly.

    The stamp used to draw its text with Courier, a PDF standard font declared
    /WinAnsiEncoding. WinAnsi has no s-comma, t-comma or a-breve, so pyHanko
    fell back to writing the whole string as UTF-16BE. A simple font reads
    those bytes one at a time, so a single diacritic destroyed the entire
    line: "Stefan Rusie" came out as thorn, y-diaeresis and NULs.

    Note the blast radius. 'a-circumflex' and 'i-circumflex' do exist in
    WinAnsi, but one s-comma anywhere in the name switches the encoding for
    every character, so the correct ones break too.

    Fixed by embedding DejaVu Sans, which covers Romanian and lets the text be
    drawn as glyph indices carrying a /ToUnicode map back to the original.
    """

    # Every Romanian diacritic, in both cases, plus a plain ASCII surname.
    SIGNER = 'ȘTEFAN RUSIE ăâîșț ĂÂÎȘȚ'

    @staticmethod
    def _render(signer):
        """Render the real signature appearance. Returns (content, font dict).

        The document is written out before the font is inspected: the glyph
        accumulator only emits the subset font program and the /ToUnicode CMap
        when the writer is finalised.
        """
        from io import BytesIO
        from pyhanko.pdf_utils.writer import PdfFileWriter
        from pyhanko.pdf_utils import generic
        from pyhanko.stamp import TextStamp

        writer = PdfFileWriter()
        writer.insert_page(generic.DictionaryObject({
            generic.NameObject('/Type'): generic.NameObject('/Page'),
            generic.NameObject('/MediaBox'): generic.ArrayObject(
                map(generic.NumberObject, [0, 0, 595, 842])),
            generic.NameObject('/Resources'): generic.DictionaryObject(),
        }))
        stamp = TextStamp(writer, app_module.build_stamp_style(),
                          text_params={'signer': signer, 'ts': '2026-08-06 12:00'})
        xobject = stamp.as_form_xobject()
        content = getattr(xobject, 'data', None) or xobject.encoded_data
        writer.write(BytesIO())
        fonts = xobject.get('/Resources')['/Font']
        return content, fonts.raw_get(list(fonts.keys())[0]).get_object()

    @staticmethod
    def _to_unicode(font_dict):
        """Glyph code -> character, parsed from the font's /ToUnicode CMap.

        This is the same table a viewer uses to render and to copy text out,
        so decoding through it tests what the user actually sees.
        """
        stream = font_dict.raw_get('/ToUnicode').get_object()
        cmap = (getattr(stream, 'data', None) or stream.encoded_data).decode('latin-1')
        table = {}
        for block in re.findall(r'beginbfchar(.*?)endbfchar', cmap, re.S):
            for src, dst in re.findall(r'<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>', block):
                table[int(src, 16)] = chr(int(dst, 16))
        listed = re.compile(r'<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*\[(.*?)\]', re.S)
        for block in re.findall(r'beginbfrange(.*?)endbfrange', cmap, re.S):
            # <lo> <hi> [<d1> <d2> ...]
            for lo, _hi, targets in listed.findall(block):
                for offset, dst in enumerate(re.findall(r'<([0-9a-fA-F]+)>', targets)):
                    table[int(lo, 16) + offset] = chr(int(dst, 16))
            # <lo> <hi> <dststart>, on what is left once the bracketed entries
            # are gone. Without stripping them first this pattern happily
            # matches three consecutive codes *inside* a [...] list.
            for lo, hi, start in re.findall(
                    r'<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>\s*<([0-9a-fA-F]+)>',
                    listed.sub('', block)):
                for offset in range(int(hi, 16) - int(lo, 16) + 1):
                    table[int(lo, 16) + offset] = chr(int(start, 16) + offset)
        return table

    @classmethod
    def _drawn_lines(cls, content, font_dict):
        """The text a viewer would show, decoded back out of the content stream."""
        table = cls._to_unicode(font_dict)
        lines = []
        # A TJ array interleaves hex glyph strings with kerning adjustments,
        # e.g. [<0027002c> 159 <0024002f>]. Only the hex chunks are text.
        for array in re.findall(rb'\[(.*?)\]\s*TJ', content, re.S):
            codes = b''.join(re.findall(rb'<([0-9a-fA-F]+)>', array)).decode('ascii')
            lines.append(''.join(
                table.get(int(codes[i:i + 4], 16), '�')
                for i in range(0, len(codes), 4)))
        return lines

    def test_diacritics_reach_the_page_intact(self):
        """The regression itself: the name must survive to the content stream."""
        content, font = self._render(self.SIGNER)
        self.assertIn('/ToUnicode', font,
                      "no /ToUnicode map, so the text cannot be rendered or copied")
        self.assertIn(self.SIGNER, self._drawn_lines(content, font),
                      "signer name did not survive into the appearance stream")

    def test_no_utf16_fallback(self):
        """UTF-16 reaching a text-showing operator means the old path is back.

        Checked separately from the round trip because this is the exact
        fingerprint users reported, and it can reappear on its own if the
        font ever fails to load and pyHanko falls back to a standard font.

        Only Tj operators are examined. A UTF-16BE string is also written into
        /Span << /ActualText (...) >>, but that is a PDF text string declaring
        what the glyphs mean, which is correct and wanted.
        """
        content, _ = self._render(self.SIGNER)
        shown = re.findall(rb'\((?:[^()\\]|\\.)*\)\s*Tj', content)
        for operand in shown:
            self.assertNotIn(rb'\376\377', operand,
                             f"text drawn as UTF-16BE through a simple font: {operand!r}")

    def test_font_is_embedded_not_substituted(self):
        """A signature appearance has to carry its own font.

        Referencing a non-embedded font leaves the rendering up to whatever
        the viewer happens to substitute, which is not acceptable on a
        document meant to stay valid for years.
        """
        _, font = self._render(self.SIGNER)
        self.assertEqual(font.get('/Subtype'), '/Type0',
                         "expected a composite font capable of Unicode")
        descendant = font.raw_get('/DescendantFonts').get_object()[0].get_object()
        descriptor = descendant.raw_get('/FontDescriptor').get_object()
        self.assertTrue(
            any(k in descriptor for k in ('/FontFile', '/FontFile2', '/FontFile3')),
            "font program is not embedded in the document")

    def test_lines_do_not_drift_sideways(self):
        """Every line has to start at the same x as the one above it.

        The glyph accumulator and TextBoxStyle each carry their own font_size.
        pyHanko uses the accumulator's to emit the advance after drawing a
        line, but the style's to travel back to the next line's start, so if
        they disagree the difference accumulates and walks the text out of its
        box. Nothing raises; it is only visible once rendered, which is how it
        got missed the first time.
        """
        content, _ = self._render(self.SIGNER)
        moves = re.findall(
            rb'TJ\s+(-?[\d.]+)\s+0\s+Td\s+EMC\s+(-?[\d.]+)\s+(-?[\d.]+)\s+Td', content)
        self.assertTrue(moves, "no line advances found in the appearance stream")
        for forward, back, _leading in moves:
            self.assertAlmostEqual(
                float(forward), -float(back), places=3,
                msg=f"line advance {float(forward)} is not undone by {float(back)}; "
                    "check that STAMP_FONT_SIZE reaches the glyph accumulator")

    def test_viewer_advances_match_the_layout(self):
        """The widths a viewer uses must agree with the widths pyHanko laid out.

        pyHanko copies a CID font's /W array straight out of the font's hmtx
        table, in raw design units, but PDF fixes CIDFontType2 glyph space at
        1/1000 of text space. The two only agree when the font has 1000 units
        per em. Stock DejaVu Sans has 2048, so a viewer advanced 2.048x too far
        on every glyph and the text ran out of its box, while pyHanko's own
        layout stayed correct because that path divides by unitsPerEm. The
        vendored font is rescaled to 1000 to keep the two in step; see
        scripts/prepare-signature-font.py.
        """
        content, font = self._render('ADRIAN BANCU')
        descendant = font.raw_get('/DescendantFonts').get_object()[0].get_object()

        widths = {}
        entries = list(descendant.get('/W'))
        i = 0
        while i < len(entries):
            first = int(entries[i])
            run = entries[i + 1]
            if isinstance(run, (list, tuple)) or hasattr(run, '__iter__'):
                for offset, width in enumerate(run):
                    widths[first + offset] = float(width)
                i += 2
            else:  # cFirst cLast width
                for cid in range(first, int(run) + 1):
                    widths[cid] = float(entries[i + 2])
                i += 3

        arrays = re.findall(rb'\[(.*?)\]\s*TJ\s+(-?[\d.]+)\s+0\s+Td', content, re.S)
        self.assertTrue(arrays, "no glyph runs found")
        for array, laid_out in arrays:
            codes = b''.join(re.findall(rb'<([0-9a-fA-F]+)>', array)).decode('ascii')
            kerning = sum(float(k) for k in re.findall(rb'>\s*(-?\d+)\s*<', array))
            total = sum(widths[int(codes[i:i + 4], 16)]
                        for i in range(0, len(codes), 4))
            # /W is per mille of text space; kerning in a TJ array is subtracted.
            rendered = (total - kerning) / 1000.0 * app_module.STAMP_FONT_SIZE
            self.assertAlmostEqual(
                rendered, float(laid_out), delta=0.5,
                msg=f"a viewer would advance {rendered:.1f}pt where pyHanko laid "
                    f"out {float(laid_out):.1f}pt; check the font's unitsPerEm")

    def test_leaves_room_under_the_baseline(self):
        """s-comma and t-comma sit below the baseline and need the space.

        With leading equal to the font size the marks under s and t collide
        with the line underneath, which is what made a correctly encoded
        Romanian name still look wrong.
        """
        self.assertGreater(app_module.STAMP_LEADING, app_module.STAMP_FONT_SIZE,
                           "no room below the baseline for s-comma and t-comma")

    def test_plain_ascii_names_still_render(self):
        """The common case must not regress while fixing the uncommon one."""
        content, font = self._render('ADRIAN BANCU')
        self.assertIn('ADRIAN BANCU', self._drawn_lines(content, font))

    def test_font_asset_ships_with_the_app(self):
        """The .ttf has to resolve both from source and from the bundle."""
        path = app_module.signature_font_path()
        self.assertTrue(os.path.isfile(path), f"font asset missing at {path}")

    def test_status_reports_the_font_is_active(self):
        """Lets the packaged build be checked from outside.

        A bundle that cannot find its font still starts and still signs; it
        just falls back to Courier and mangles Romanian names. Reporting the
        state over /api/status is what lets the release verifier catch that
        before the build is published.
        """
        payload = app_module.app.test_client().get('/api/status').get_json()
        self.assertTrue(payload['signature_font_embedded'],
                        f"font not active: {payload.get('signature_font_path')}")


class FakePCSC:
    """Stands in for PCSC.framework.

    Only the four entry points list_readers() calls. Scripted return codes let
    a test drive the failure branches without unplugging real hardware.
    """

    def __init__(self, readers=(), establish_rv=pcsc.SCARD_S_SUCCESS,
                 list_rv=pcsc.SCARD_S_SUCCESS, status_rv=pcsc.SCARD_S_SUCCESS):
        # Names are kept as raw bytes, the way PC/SC hands them over: they are
        # vendor strings with no encoding guarantee.
        self.readers = [(n if isinstance(n, bytes) else n.encode(), present)
                        for n, present in readers]
        self.establish_rv = establish_rv
        self.list_rv = list_rv
        self.status_rv = status_rv
        self.insufficient_buffer_once = False
        self.released = []

    def _blob(self):
        # PC/SC hands back NUL-separated names terminated by a second NUL.
        return b''.join(name + b'\x00' for name, _ in self.readers) + b'\x00'

    def SCardEstablishContext(self, scope, res1, res2, ctx_ref):
        if self.establish_rv == pcsc.SCARD_S_SUCCESS:
            ctx_ref._obj.value = 7
        return self.establish_rv

    def SCardListReaders(self, ctx, groups, buf, size_ref):
        if self.list_rv != pcsc.SCARD_S_SUCCESS:
            return self.list_rv
        blob = self._blob()
        # A reader appearing between the sizing call and the data call leaves
        # the caller holding a buffer that is now too small.
        if buf is not None and self.insufficient_buffer_once:
            self.insufficient_buffer_once = False
            return pcsc.SCARD_E_INSUFFICIENT_BUFFER
        size_ref._obj.value = len(blob)
        if buf is not None:
            ctypes.memmove(buf, blob, len(blob))
        return pcsc.SCARD_S_SUCCESS

    def SCardGetStatusChange(self, ctx, timeout, states_ref, count):
        state = states_ref._obj
        if self.status_rv != pcsc.SCARD_S_SUCCESS:
            return self.status_rv
        present = dict(self.readers)[state.szReader]
        state.dwEventState = (pcsc.SCARD_STATE_PRESENT if present
                              else pcsc.SCARD_STATE_EMPTY)
        return pcsc.SCARD_S_SUCCESS

    def SCardReleaseContext(self, ctx):
        self.released.append(ctx)
        return pcsc.SCARD_S_SUCCESS


class ListReadersTests(unittest.TestCase):
    """Reader detection talks to PCSC.framework directly.

    It used to shell out to `opensc-tool`, which is not installed on a stock
    Mac - the resulting error was rendered as "PKCS#11 not found", sending
    users to reconfigure a library path that was never the problem.
    """

    def test_reports_each_reader_and_whether_a_card_is_in_it(self):
        lib = FakePCSC(readers=[('Idemia Reader', True), ('Spare Reader', False)])
        self.assertEqual(pcsc.list_readers(lib),
                         [('Idemia Reader', True), ('Spare Reader', False)])

    def test_no_reader_attached_is_a_normal_empty_result(self):
        """SCARD_E_NO_READERS_AVAILABLE means "none plugged in", not a fault."""
        lib = FakePCSC(list_rv=pcsc.SCARD_E_NO_READERS_AVAILABLE)
        self.assertEqual(pcsc.list_readers(lib), [])

    def test_empty_reader_list_yields_nothing(self):
        self.assertEqual(pcsc.list_readers(FakePCSC(readers=[])), [])

    def test_unreachable_pcsc_service_raises(self):
        lib = FakePCSC(establish_rv=pcsc.SCARD_E_NO_SERVICE)
        with self.assertRaises(pcsc.PCSCError):
            pcsc.list_readers(lib)

    def test_context_is_released_when_listing_fails(self):
        """A leaked context holds a connection to the PC/SC daemon open."""
        lib = FakePCSC(list_rv=0x80100001)  # SCARD_F_INTERNAL_ERROR
        with self.assertRaises(pcsc.PCSCError):
            pcsc.list_readers(lib)
        self.assertEqual(lib.released, [7], "context must be released on the error path")

    def test_reader_whose_status_cannot_be_read_is_reported_cardless(self):
        """One sulking reader must not blind the app to the others."""
        lib = FakePCSC(readers=[('Idemia Reader', True)], status_rv=0x80100001)
        self.assertEqual(pcsc.list_readers(lib), [('Idemia Reader', False)])

    def test_service_dying_mid_scan_is_raised_not_swallowed(self):
        """A dead service must not masquerade as "no card in the reader".

        Reporting it as cardless sends the user to re-seat a card that is
        already seated - the misdirection this module exists to end.
        """
        lib = FakePCSC(readers=[('Idemia Reader', True)],
                       status_rv=pcsc.SCARD_E_NO_SERVICE)
        with self.assertRaises(pcsc.PCSCError):
            pcsc.list_readers(lib)

    def test_reader_appearing_mid_scan_is_retried_not_fatal(self):
        """Plugging a reader between the sizing and data calls invalidates the
        buffer. PC/SC says SCARD_E_INSUFFICIENT_BUFFER; the service is fine."""
        lib = FakePCSC(readers=[('Late Reader', True)])
        lib.insufficient_buffer_once = True
        self.assertEqual(pcsc.list_readers(lib), [('Late Reader', True)])

    def test_reader_name_that_is_not_utf8_does_not_explode(self):
        """Reader names are vendor strings, with no encoding guarantee.

        The name must still round-trip to SCardGetStatusChange as the exact
        bytes PC/SC gave us, or the card behind it goes unseen - so decoding
        is for display only.
        """
        lib = FakePCSC(readers=[(b'Idemia \xff\xfe Reader', True)])
        self.assertEqual([present for _, present in pcsc.list_readers(lib)], [True])
        name = pcsc.list_readers(lib)[0][0]
        self.assertIn('Idemia', name)

    def test_context_is_released_on_success(self):
        lib = FakePCSC(readers=[('Idemia Reader', True)])
        pcsc.list_readers(lib)
        self.assertEqual(lib.released, [7])


class DetectReaderTests(unittest.TestCase):
    """app.detect_reader keeps its (name, card_present) contract."""

    def test_returns_the_reader_holding_a_card(self):
        readers = [('Empty Reader', False), ('Idemia Reader', True)]
        with mock.patch.object(app_module.pcsc, 'list_readers', return_value=readers):
            self.assertEqual(app_module.detect_reader(), ('Idemia Reader', True))

    def test_reader_without_a_card_reports_absence(self):
        with mock.patch.object(app_module.pcsc, 'list_readers',
                               return_value=[('Idemia Reader', False)]):
            self.assertEqual(app_module.detect_reader(), (None, False))

    def test_no_readers_at_all_reports_absence(self):
        with mock.patch.object(app_module.pcsc, 'list_readers', return_value=[]):
            self.assertEqual(app_module.detect_reader(), (None, False))


class DetectTimeoutTests(unittest.TestCase):
    """Detection must stay bounded.

    The old subprocess carried timeout=10. The PC/SC calls that replaced it
    take no timeout argument at all, and this project has watched PC/SC block
    for 2048 seconds, released only by re-plugging the reader. Without a
    deadline the Flask worker blocks forever while the frontend re-polls every
    15s, stacking up threads that never come back.
    """

    def setUp(self):
        app_module.reset_detection_state()
        self.addCleanup(app_module.reset_detection_state)

    def test_detection_that_never_returns_gives_up(self):
        started = threading.Event()

        def wedged():
            started.set()
            time.sleep(30)

        with mock.patch.object(app_module.pcsc, 'list_readers', side_effect=wedged):
            with self.assertRaises(app_module.DetectTimeout):
                app_module.detect_reader(timeout=0.2)
        self.assertTrue(started.wait(1), "detection never actually ran")

    def test_a_wedged_scan_is_not_joined_by_a_second_one(self):
        """The frontend re-polls every 15s; one stuck scan must not become ten."""
        release = threading.Event()
        calls = []

        def wedged():
            calls.append(1)
            release.wait(30)
            return []

        with mock.patch.object(app_module.pcsc, 'list_readers', side_effect=wedged):
            for _ in range(3):
                with self.assertRaises(app_module.DetectTimeout):
                    app_module.detect_reader(timeout=0.2)
        release.set()
        self.assertEqual(len(calls), 1,
                         "each poll started another thread against a wedged service")

    def test_timeout_is_reported_under_its_own_code(self):
        def wedged():
            time.sleep(30)

        with mock.patch.object(app_module.pcsc, 'list_readers', side_effect=wedged), \
             mock.patch.object(app_module, 'DETECT_TIMEOUT', 0.2):
            payload = app_module.app.test_client().get('/api/slots').get_json()
        self.assertEqual(payload['code'], 'detect_timeout')

    def test_detection_recovers_once_the_service_answers_again(self):
        with mock.patch.object(app_module.pcsc, 'list_readers',
                               side_effect=lambda: time.sleep(30)):
            with self.assertRaises(app_module.DetectTimeout):
                app_module.detect_reader(timeout=0.2)

        app_module.reset_detection_state()
        with mock.patch.object(app_module.pcsc, 'list_readers',
                               return_value=[('Idemia Reader', True)]):
            self.assertEqual(app_module.detect_reader(timeout=5),
                             ('Idemia Reader', True))


class BoundedCallTests(unittest.TestCase):
    """Blocking driver calls run under a deadline, and a wedge is remembered.

    A C call that never returns cannot be cancelled from Python, so its thread
    is abandoned. Without remembering that, every 15s poll strands another one.
    """

    def setUp(self):
        self.caller = app_module.BoundedCaller('test')

    def test_returns_the_value_when_the_call_completes(self):
        self.assertEqual(self.caller.call(lambda: 'done', timeout=5), 'done')

    def test_propagates_the_error_when_the_call_raises(self):
        def boom():
            raise ValueError('driver said no')
        with self.assertRaises(ValueError):
            self.caller.call(boom, timeout=5)

    def test_raises_when_the_call_outlives_its_deadline(self):
        with self.assertRaises(app_module.CallTimeout):
            self.caller.call(lambda: time.sleep(30), timeout=0.2)

    def test_a_wedged_call_is_not_joined_by_the_next_one(self):
        release = threading.Event()
        calls = []

        def wedged():
            calls.append(1)
            release.wait(30)

        for _ in range(3):
            with self.assertRaises(app_module.CallTimeout):
                self.caller.call(wedged, timeout=0.2)
        release.set()
        self.assertEqual(len(calls), 1, "each attempt stranded another thread")

    def test_recovers_once_the_wedged_call_finishes(self):
        release = threading.Event()
        with self.assertRaises(app_module.CallTimeout):
            self.caller.call(lambda: release.wait(30), timeout=0.2)
        release.set()
        time.sleep(0.2)
        self.assertEqual(self.caller.call(lambda: 'ok', timeout=5), 'ok')


class ShutdownGraceTests(unittest.TestCase):
    """Quitting mid-call is what stranded the driver in the first place.

    The wedge is triggered by a process dying while inside a PKCS#11 call. The
    app is inside one for the ~12s of a cold enumeration and again while
    signing, so closing the window at the wrong moment leaves the next launch -
    and every other app on the machine - facing a driver that needs a reboot.
    """

    def setUp(self):
        app_module.reset_detection_state()
        self.addCleanup(app_module.reset_detection_state)

    def test_idle_driver_is_not_busy(self):
        self.assertFalse(app_module.driver_busy())

    def test_driver_is_busy_while_a_call_runs(self):
        started, release = threading.Event(), threading.Event()

        def slow():
            started.set()
            release.wait(10)

        t = threading.Thread(target=lambda: app_module._pkcs11_caller.call(slow, 0.2),
                             daemon=True)
        t.start()
        self.assertTrue(started.wait(2))
        self.assertTrue(app_module.driver_busy(), "a running call must count as busy")
        release.set()

    def test_wait_returns_true_once_the_call_finishes(self):
        release = threading.Event()
        threading.Thread(
            target=lambda: app_module._pkcs11_caller.call(lambda: release.wait(10), 0.2),
            daemon=True).start()
        time.sleep(0.3)
        release.set()
        self.assertTrue(app_module.wait_for_driver(timeout=5))

    def test_wait_gives_up_on_a_driver_that_never_returns(self):
        """A wedged call is never coming back; quitting must not hang forever."""
        release = threading.Event()
        self.addCleanup(release.set)
        threading.Thread(
            target=lambda: app_module._pkcs11_caller.call(lambda: release.wait(30), 0.2),
            daemon=True).start()
        time.sleep(0.3)
        t = time.monotonic()
        self.assertFalse(app_module.wait_for_driver(timeout=0.5))
        self.assertLess(time.monotonic() - t, 3, "must not wait past its own deadline")


class MainWiringTests(unittest.TestCase):
    """main.py is the GUI shell, so its mistakes surface only at runtime.

    `from app import app` binds the Flask object, not the module - writing
    `app.driver_busy()` there parses fine and raises only when the user quits,
    which no test exercises. These check the seams instead.
    """

    def _main_source(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'main.py')) as fh:
            return fh.read()

    def test_everything_main_imports_from_app_exists(self):
        # ast rather than a regex: the import list is long enough to be
        # wrapped in parentheses across lines, which a line-based pattern
        # reads as the name '(app'.
        tree = ast.parse(self._main_source())
        names = [alias.name
                 for node in ast.walk(tree)
                 if isinstance(node, ast.ImportFrom) and node.module == 'app'
                 for alias in node.names]
        self.assertTrue(names, "main.py no longer imports from app")
        for name in names:
            self.assertTrue(hasattr(app_module, name),
                            f"main.py imports {name!r} from app, which does not define it")

    def test_shutdown_hook_uses_the_module_functions_not_the_flask_object(self):
        src = self._main_source()
        self.assertNotIn('app.driver_busy', src,
                         "`app` here is the Flask object; this raises at quit time")
        self.assertNotIn('app.wait_for_driver', src,
                         "`app` here is the Flask object; this raises at quit time")

    def test_a_terminating_signal_drains_the_driver_before_exiting(self):
        """`kill` must not cut a PKCS#11 call short either.

        scripts/verify-release-archive.sh launches the bundle and then plainly
        kills it. With a card in the reader the app is mid-enumeration by then,
        so our own release check was manufacturing the wedge it exists to
        guard against.
        """
        src = self._main_source()
        for sig in ('SIGTERM', 'SIGINT'):
            self.assertIn(sig, src, f"{sig} is not handled; a kill still cuts calls short")

        handler = re.search(r'def _on_signal.*?(?=\ndef |\nif __name__)', src, re.DOTALL)
        self.assertIsNotNone(handler, "expected an _on_signal watcher in main.py")
        body = handler.group(0)

        self.assertIn('wait_for_driver', body,
                      "must drain the driver before exiting")
        # Measured, both of these the hard way:
        # - signal.signal handlers run on the main thread, which never leaves
        #   pywebview's Cocoa loop. Registering one replaces the default
        #   disposition without ever firing, and the app went immune to
        #   SIGTERM - still serving requests a minute later.
        # - sys.exit raises SystemExit, which cannot unwind that loop either.
        self.assertIn('sigwait', body,
                      "signal.signal never fires here; wait on a dedicated thread")
        self.assertIn('pthread_sigmask', src,
                      "sigwait needs the signals blocked in every thread")
        self.assertIn('os._exit', body,
                      "sys.exit cannot unwind the Cocoa loop; use os._exit")
        self.assertNotIn('sys.exit', body,
                         "sys.exit here swallows the signal instead of exiting")

    def test_the_js_hook_main_calls_is_defined_in_the_template(self):
        src = self._main_source()
        called = re.findall(r"evaluate_js\('(\w+)\(\)'\)", src)
        self.assertTrue(called, "no evaluate_js hook found in main.py")
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'templates', 'index.html')) as fh:
            page = fh.read()
        for fn in called:
            self.assertIn(f'function {fn}(', page,
                          f"main.py calls {fn}() but the page does not define it")


class WedgedDriverTests(unittest.TestCase):
    """The Idemia driver can deadlock inside a single get_slots() call.

    Observed 2026-08-14: two threads inside the driver deadlock, the call never
    returns, and only a reboot clears it. SLOT_SETTLE_TIMEOUT bounds the
    polling loop but is checked only between calls, so nothing bounded this.
    """

    def setUp(self):
        app_module.reset_detection_state()
        self.addCleanup(app_module.reset_detection_state)

    def _wedged_lib(self):
        lib = mock.Mock()
        lib.get_slots.side_effect = lambda **kw: time.sleep(30)
        return lib

    def test_enumerate_slots_gives_up_instead_of_hanging(self):
        with mock.patch.object(app_module, 'PKCS11_CALL_TIMEOUT', 0.2):
            with self.assertRaises(app_module.DriverTimeout):
                app_module.enumerate_slots(self._wedged_lib())

    def test_enumerate_slots_does_not_swallow_it_as_an_empty_card(self):
        """The loop treats call failures as "no slots yet" and keeps polling.
        A wedge must break out of that, not look like a slow card."""
        with mock.patch.object(app_module, 'PKCS11_CALL_TIMEOUT', 0.2):
            try:
                app_module.enumerate_slots(self._wedged_lib())
            except app_module.DriverTimeout:
                return
        self.fail("wedge was swallowed and reported as no slots")

    def test_find_slot_gives_up_instead_of_hanging(self):
        with mock.patch.object(app_module, 'PKCS11_CALL_TIMEOUT', 0.2):
            with self.assertRaises(app_module.DriverTimeout):
                app_module.find_slot(self._wedged_lib(), 2)

    def test_api_reports_a_wedged_driver_and_says_what_to_do(self):
        with mock.patch.object(app_module, 'detect_reader', return_value=('R', True)), \
             mock.patch.object(app_module.pkcs11, 'lib', mock.Mock()), \
             mock.patch.object(app_module, 'enumerate_slots',
                               side_effect=app_module.DriverTimeout('stuck')):
            payload = app_module.app.test_client().get('/api/slots').get_json()
        self.assertEqual(payload['code'], 'driver_wedged')
        self.assertIn('restart', payload['error'].lower(),
                      "only a reboot clears this; the message has to say so")


class TimeoutBudgetTests(unittest.TestCase):
    """The server must answer before the browser gives up on it.

    /api/slots spends its time in two places: reader detection, then PKCS#11
    slot enumeration. Their deadlines used to sum to exactly the frontend's
    abort, so a slow-but-successful poll was cut off client-side and the user
    got the generic "Reader timeout - Click to retry" instead of the specific
    message the server had prepared. The two numbers live in different files
    and drifted apart unnoticed; this reads both.
    """

    MARGIN = 5.0   # room for request overhead and a slow first paint

    def _client_abort_seconds(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'templates', 'index.html')) as fh:
            src = fh.read()
        match = re.search(r'controller\.abort\(\),\s*(\d+)\)', src)
        self.assertIsNotNone(match, "could not find the frontend's abort timeout")
        return int(match.group(1)) / 1000.0

    def test_worst_case_response_fits_inside_the_client_abort(self):
        budget = app_module.DETECT_TIMEOUT + app_module.SLOT_SETTLE_TIMEOUT
        self.assertLessEqual(
            budget, self._client_abort_seconds() - self.MARGIN,
            f"server can take up to {budget:g}s but the browser aborts at "
            f"{self._client_abort_seconds():g}s; the user would never see the real error")

    def test_a_single_driver_call_cannot_outlast_the_loop_that_polls_it(self):
        """Otherwise one wedged call blows the whole request budget on its own."""
        self.assertLessEqual(app_module.PKCS11_CALL_TIMEOUT,
                             app_module.SLOT_SETTLE_TIMEOUT)
        worst_case = app_module.DETECT_TIMEOUT + app_module.PKCS11_CALL_TIMEOUT
        self.assertLessEqual(worst_case, self._client_abort_seconds() - self.MARGIN)

    def test_detection_deadline_is_generous_against_a_healthy_service(self):
        """PC/SC answers in milliseconds; the deadline only catches a wedge."""
        self.assertGreaterEqual(app_module.DETECT_TIMEOUT, 2.0)


class SlotsErrorCodeTests(unittest.TestCase):
    """/api/slots reports a typed code; the badge must not parse prose.

    The frontend used to light "PKCS#11 not found" on any error containing the
    substring "not found", so an unrelated failure pointed users at the PKCS#11
    settings. Every error carries a machine-readable code instead.
    """

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        app_module.clear_slot_cache()
        self.addCleanup(app_module.clear_slot_cache)

    def test_pcsc_failure_is_not_reported_as_a_pkcs11_problem(self):
        with mock.patch.object(app_module, 'detect_reader',
                               side_effect=pcsc.PCSCError('service down')):
            payload = self.client.get('/api/slots').get_json()
        self.assertEqual(payload['code'], 'pcsc_unavailable')
        self.assertNotIn('PKCS', payload['error'],
                         "a PC/SC fault must not be blamed on PKCS#11")

    def test_absent_card_is_coded(self):
        with mock.patch.object(app_module, 'detect_reader', return_value=(None, False)):
            payload = self.client.get('/api/slots').get_json()
        self.assertEqual(payload['code'], 'no_card')

    def test_unreadable_pkcs11_library_is_coded_as_such(self):
        with mock.patch.object(app_module, 'detect_reader', return_value=('R', True)), \
             mock.patch.object(app_module.pkcs11, 'lib', side_effect=OSError('image not found')):
            payload = self.client.get('/api/slots').get_json()
        self.assertEqual(payload['code'], 'pkcs11_error')

    def test_missing_pkcs11_binding_is_reported_without_a_card(self):
        """A build shipped without the binding is broken for everyone.

        Reporting "no card" to someone who has not inserted one yet hides a
        packaging defect behind a hardware excuse.
        """
        with mock.patch.object(app_module, 'PKCS11_AVAILABLE', False), \
             mock.patch.object(app_module, 'detect_reader', return_value=(None, False)):
            payload = self.client.get('/api/slots').get_json()
        self.assertEqual(payload['code'], 'pkcs11_missing')

    def test_success_carries_no_error_code(self):
        with mock.patch.object(app_module, 'detect_reader', return_value=('R', True)), \
             mock.patch.object(app_module, 'enumerate_slots',
                               return_value=[{'id': 2, 'label': 'ADVANCED SIGNATURE PIN'}]), \
             mock.patch.object(app_module.pkcs11, 'lib', mock.Mock()):
            payload = self.client.get('/api/slots').get_json()
        self.assertNotIn('code', payload)


class NoOpenscToolTests(unittest.TestCase):
    """Reader detection must never shell out to opensc-tool again.

    It is absent from a stock macOS install, and the Homebrew path on Apple
    Silicon (/opt/homebrew/bin) was never on the bundled app's PATH anyway.
    PCSC.framework ships with the OS and needs nothing installed.
    """

    # Matches invoking it - a quoted string literal, as it would appear in a
    # subprocess argument list. Prose explaining why it was dropped stays legal,
    # the same allowance NoPyKCS11Tests makes.
    INVOKED = re.compile(r'''["']opensc-tool["']''')

    def test_no_source_invokes_opensc_tool(self):
        here = os.path.dirname(os.path.abspath(__file__))
        for filename in ('app.py', 'main.py', 'pcsc.py'):
            with open(os.path.join(here, filename)) as fh:
                src = fh.read()
            self.assertIsNone(self.INVOKED.search(src),
                              f"{filename} shells out to opensc-tool; "
                              f"detection must go through PCSC.framework")

    def test_badge_does_not_key_off_error_prose(self):
        """The bug: any error containing "not found" lit the PKCS#11 warning."""
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'templates', 'index.html')) as fh:
            src = fh.read()
        self.assertNotIn("includes('not found')", src,
                         "the badge must switch on the error code, not on prose")

    def test_server_error_messages_are_not_gated_behind_a_dead_branch(self):
        """`!data.slots` is always false: every error response carries slots: [],
        and [] is truthy in JS. That swallowed every typed error into the
        generic "No card" state - the misattribution this all exists to end.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'templates', 'index.html')) as fh:
            src = fh.read()
        # Matches it as a condition, so the comment recording the bug stays legal.
        dead_guard = re.compile(r'if\s*\([^)]*!data\.slots')
        self.assertIsNone(dead_guard.search(src),
                          "guard is always false; test data.slots.length instead")

    def test_unfixable_states_do_not_invite_the_user_into_settings(self):
        """Only a library the user can repoint belongs on the settings badge.

        A missing python-pkcs11 binding is a packaging defect; offering the
        library-path dialog for it repeats the original bug in miniature.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'templates', 'index.html')) as fh:
            src = fh.read()
        warning_map = re.search(r'PKCS11_WARNING_TEXT\s*=\s*\{(.*?)\}', src, re.DOTALL)
        self.assertIsNotNone(warning_map, "PKCS11_WARNING_TEXT not found")
        self.assertNotIn('pkcs11_missing', warning_map.group(1),
                         "a build defect must not be presented as a setting to change")


class VersionParsingTests(unittest.TestCase):
    """Tags are compared as numbers. As strings, v0.9 outranks v0.10."""

    def test_beta_suffix_is_ignored(self):
        self.assertEqual(updater.parse_version('v0.13-beta'), (0, 13, 0))

    def test_leading_v_is_optional(self):
        self.assertEqual(updater.parse_version('0.13.2'), (0, 13, 2))

    def test_garbage_does_not_parse(self):
        for tag in ('dev', '', 'beta', 'v', None):
            self.assertIsNone(updater.parse_version(tag), tag)

    def test_ten_is_newer_than_nine(self):
        self.assertTrue(updater.is_newer('v0.10-beta', 'v0.9-beta'))
        self.assertFalse(updater.is_newer('v0.9-beta', 'v0.10-beta'))

    def test_same_version_is_not_newer(self):
        self.assertFalse(updater.is_newer('v0.13-beta', 'v0.13-beta'))

    def test_unparseable_never_counts_as_an_update(self):
        # A tag we cannot read must not trigger a download. Both directions.
        self.assertFalse(updater.is_newer('banana', 'v0.13-beta'))
        self.assertFalse(updater.is_newer('v0.99-beta', 'dev'))

    def test_numeric_version_is_dotted_integers_for_apple(self):
        # CFBundleVersion must be dotted integers or notarization complains.
        self.assertEqual(updater.numeric_version('v0.13-beta'), '0.13.0')
        self.assertEqual(updater.numeric_version('dev'), '0.0.0')


class CurrentVersionTests(unittest.TestCase):

    def test_unfrozen_reports_dev(self):
        # Run from source there is no Info.plist, so the updater switches off.
        with mock.patch.object(updater.sys, 'frozen', False, create=True):
            self.assertIsNone(updater.bundle_path())
            self.assertEqual(updater.current_release_tag(), 'dev')

    def test_tag_is_read_from_the_bundle_plist(self):
        with tempfile.TemporaryDirectory() as tmp:
            contents = os.path.join(tmp, 'X.app', 'Contents')
            os.makedirs(os.path.join(contents, 'MacOS'))
            with open(os.path.join(contents, 'Info.plist'), 'wb') as fh:
                plistlib.dump({'CEIReleaseTag': 'v0.13-beta'}, fh)
            exe = os.path.join(contents, 'MacOS', 'X')
            with mock.patch.object(updater.sys, 'frozen', True, create=True), \
                 mock.patch.object(updater.sys, 'executable', exe):
                self.assertEqual(updater.current_release_tag(), 'v0.13-beta')

    def test_missing_key_reports_dev(self):
        with tempfile.TemporaryDirectory() as tmp:
            contents = os.path.join(tmp, 'X.app', 'Contents')
            os.makedirs(os.path.join(contents, 'MacOS'))
            with open(os.path.join(contents, 'Info.plist'), 'wb') as fh:
                plistlib.dump({'CFBundleName': 'X'}, fh)
            exe = os.path.join(contents, 'MacOS', 'X')
            with mock.patch.object(updater.sys, 'frozen', True, create=True), \
                 mock.patch.object(updater.sys, 'executable', exe):
                self.assertEqual(updater.current_release_tag(), 'dev')


def _release(tag, assets=('CEI-PDF-Signer-v0.14-beta-macOS.zip', 'SHA256SUMS.txt'),
             draft=False):
    return {
        'tag_name': tag,
        'draft': draft,
        'html_url': 'https://github.com/sudondream/cei-pdf-signer/releases/' + tag,
        'assets': [{'name': name, 'size': 29360128,
                    'browser_download_url': 'https://example.invalid/' + name}
                   for name in assets],
    }


class LatestReleaseTests(unittest.TestCase):
    """GitHub's /releases/latest hides prereleases. Every tag here is -beta."""

    def test_uses_the_latest_endpoint_when_it_answers(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return _release('v0.14-beta')

        self.assertEqual(updater.latest_release(fetch)['tag_name'], 'v0.14-beta')
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].endswith('/releases/latest'))

    def test_falls_back_to_the_list_when_latest_is_absent(self):
        # The day someone ticks "prerelease", /latest 404s and every installed
        # app would silently stop seeing updates without this fallback.
        def fetch(url):
            if url.endswith('/latest'):
                raise updater.NotFound()
            return [_release('v0.14-beta'), _release('v0.13-beta')]

        self.assertEqual(updater.latest_release(fetch)['tag_name'], 'v0.14-beta')

    def test_drafts_are_skipped_in_the_fallback(self):
        def fetch(url):
            if url.endswith('/latest'):
                raise updater.NotFound()
            return [_release('v0.15-beta', draft=True), _release('v0.14-beta')]

        self.assertEqual(updater.latest_release(fetch)['tag_name'], 'v0.14-beta')

    def test_no_releases_at_all(self):
        def fetch(url):
            if url.endswith('/latest'):
                raise updater.NotFound()
            return []

        self.assertIsNone(updater.latest_release(fetch))


class CheckTests(unittest.TestCase):

    def test_newer_release_is_offered(self):
        found = updater.check('v0.13-beta', lambda url: _release('v0.14-beta'))
        self.assertEqual(found.tag, 'v0.14-beta')
        self.assertEqual(found.zip_url,
                         'https://example.invalid/CEI-PDF-Signer-v0.14-beta-macOS.zip')
        self.assertEqual(found.sums_url, 'https://example.invalid/SHA256SUMS.txt')
        self.assertEqual(found.zip_size, 29360128)

    def test_same_version_offers_nothing(self):
        self.assertIsNone(
            updater.check('v0.14-beta', lambda url: _release('v0.14-beta')))

    def test_release_without_our_archive_is_ignored(self):
        # A release carrying only source tarballs is not something to install.
        release = _release('v0.14-beta', assets=('Source code.zip', 'SHA256SUMS.txt'))
        self.assertIsNone(updater.check('v0.13-beta', lambda url: release))

    def test_release_without_checksums_is_ignored(self):
        release = _release('v0.14-beta', assets=('CEI-PDF-Signer-v0.14-beta-macOS.zip',))
        self.assertIsNone(updater.check('v0.13-beta', lambda url: release))

    def test_network_failure_is_silent(self):
        # A signer that cannot reach GitHub is still a working signer.
        def fetch(url):
            raise OSError('no route to host')

        self.assertIsNone(updater.check('v0.13-beta', fetch))

    def test_dev_build_never_updates(self):
        self.assertIsNone(updater.check('dev', lambda url: _release('v0.14-beta')))


class BundleLocationTests(unittest.TestCase):

    def test_translocation_is_detected(self):
        # An app opened straight from Downloads runs read-only from a random
        # path under here, and cannot replace itself.
        path = pathlib.Path('/private/var/folders/ab/xy/T/AppTranslocation/'
                            '1234-5678/d/CEI PDF Signer.app')
        self.assertTrue(updater.is_translocated(path))
        self.assertFalse(updater.is_installable(path))

    def test_normal_path_is_not_translocated(self):
        self.assertFalse(
            updater.is_translocated(pathlib.Path('/Applications/CEI PDF Signer.app')))

    def test_writable_location_is_installable(self):
        with tempfile.TemporaryDirectory() as tmp:
            bundle = pathlib.Path(tmp) / 'CEI PDF Signer.app'
            bundle.mkdir()
            self.assertTrue(updater.is_installable(bundle))

    def test_read_only_parent_is_not_installable(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = pathlib.Path(tmp) / 'ro'
            parent.mkdir()
            bundle = parent / 'CEI PDF Signer.app'
            bundle.mkdir()
            os.chmod(parent, 0o500)
            try:
                self.assertFalse(updater.is_installable(bundle))
            finally:
                os.chmod(parent, 0o700)   # or TemporaryDirectory cannot clean up


class MoveDestinationTests(unittest.TestCase):
    """Each of these must suppress the move prompt on its own."""

    def test_offers_applications_for_a_bundle_in_downloads(self):
        with tempfile.TemporaryDirectory() as home:
            bundle = pathlib.Path(home) / 'Downloads' / 'CEI PDF Signer.app'
            with mock.patch.object(updater, 'APPLICATIONS',
                                   pathlib.Path(home) / 'Applications'):
                (pathlib.Path(home) / 'Applications').mkdir()
                self.assertEqual(
                    updater.move_destination(bundle, home=home),
                    pathlib.Path(home) / 'Applications' / 'CEI PDF Signer.app')

    def test_already_in_applications_offers_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            apps = pathlib.Path(home) / 'Applications'
            apps.mkdir()
            with mock.patch.object(updater, 'APPLICATIONS', apps):
                self.assertIsNone(
                    updater.move_destination(apps / 'CEI PDF Signer.app', home=home))

    def test_already_in_home_applications_offers_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            user_apps = pathlib.Path(home) / 'Applications'
            user_apps.mkdir()
            with mock.patch.object(updater, 'APPLICATIONS', pathlib.Path('/Applications')):
                self.assertIsNone(
                    updater.move_destination(user_apps / 'CEI PDF Signer.app',
                                             home=home))

    def test_unwritable_applications_offers_nothing(self):
        with tempfile.TemporaryDirectory() as home:
            apps = pathlib.Path(home) / 'Applications'
            apps.mkdir()
            os.chmod(apps, 0o500)
            try:
                with mock.patch.object(updater, 'APPLICATIONS', apps):
                    self.assertIsNone(updater.move_destination(
                        pathlib.Path(home) / 'Downloads' / 'CEI PDF Signer.app',
                        home=home))
            finally:
                os.chmod(apps, 0o700)

    def test_unfrozen_offers_nothing(self):
        self.assertIsNone(updater.move_destination(None))


class RelaunchHelperTests(unittest.TestCase):
    """The helper runs after the app is dead, so it is run for real here.

    'open' is shadowed by a stub on PATH: the tests must not launch anything.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, 'bin')
        os.makedirs(self.bin)
        self.opened = os.path.join(self.tmp, 'opened.txt')
        with open(os.path.join(self.bin, 'open'), 'w') as fh:
            fh.write('#!/bin/sh\necho "$1" >> %s\n' % self.opened)
        os.chmod(os.path.join(self.bin, 'open'), 0o755)

    def _bundle(self, name, marker, subdir=''):
        path = os.path.join(self.tmp, subdir, name)
        os.makedirs(os.path.join(path, 'Contents'))
        with open(os.path.join(path, 'Contents', 'marker'), 'w') as fh:
            fh.write(marker)
        return path

    def _reaped(self, argv):
        """Run argv, reaping it the moment it exits.

        An unreaped child stays a zombie, and `kill -0` succeeds on a zombie -
        so the helper would wait out its full timeout instead of noticing the
        exit. Production does not have this problem: the app is launched by
        launchd, which reaps it promptly.
        """
        live = subprocess.Popen(argv)
        threading.Thread(target=live.wait, daemon=True).start()
        return live

    def _marker(self, bundle):
        with open(os.path.join(bundle, 'Contents', 'marker')) as fh:
            return fh.read()

    def _run(self, pid, src, dest, cleanup=''):
        env = dict(os.environ, PATH=self.bin + os.pathsep + os.environ['PATH'])
        return subprocess.run(
            updater.relaunch_command(pid, src, dest, cleanup),
            env=env, capture_output=True, text=True, timeout=60)

    def _dead_pid(self):
        """A pid that has already exited and been reaped."""
        done = subprocess.Popen(['/bin/sh', '-c', 'exit 0'])
        done.wait()
        return done.pid

    def test_moves_a_bundle_into_place_and_opens_it(self):
        src = self._bundle('New.app', 'new')
        dest = os.path.join(self.tmp, 'Dest.app')
        self._run(self._dead_pid(), src, dest)
        self.assertEqual(self._marker(dest), 'new')
        with open(self.opened) as fh:
            self.assertEqual(fh.read().strip(), dest)

    def test_replaces_an_existing_bundle(self):
        # The update case: the staged bundle sits in its own temp dir, and
        # that whole dir is the cleanup path.
        src = self._bundle('New.app', 'new', subdir='staging')
        dest = self._bundle('Dest.app', 'old')
        self._run(self._dead_pid(), src, dest, cleanup=os.path.dirname(src))
        self.assertEqual(self._marker(dest), 'new')
        self.assertFalse(os.path.exists(os.path.dirname(src)))

    def test_cleanup_path_is_removed(self):
        # The update passes its temp dir here; a normal move passes the
        # original bundle, so the app does not end up installed twice.
        src = self._bundle('New.app', 'new')
        dest = os.path.join(self.tmp, 'Dest.app')
        self._run(self._dead_pid(), src, dest, cleanup=src)
        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(dest))

    def test_empty_cleanup_leaves_the_source_alone(self):
        # The translocated move: the original is unreachable, so nothing is
        # deleted. An empty argument must not turn into `rm -rf ''`.
        src = self._bundle('New.app', 'new')
        dest = os.path.join(self.tmp, 'Dest.app')
        self._run(self._dead_pid(), src, dest, cleanup='')
        self.assertTrue(os.path.exists(src))
        self.assertTrue(os.path.exists(dest))

    def test_failed_copy_restores_the_original(self):
        # The rollback path, exercised rather than reasoned about. A missing
        # source makes ditto fail after the destination has been moved aside.
        dest = self._bundle('Dest.app', 'old')
        missing = os.path.join(self.tmp, 'Gone.app')
        self._run(self._dead_pid(), missing, dest)
        self.assertEqual(self._marker(dest), 'old')
        with open(self.opened) as fh:
            self.assertEqual(fh.read().strip(), dest)

    def test_no_leftover_aside_copy_on_success(self):
        src = self._bundle('New.app', 'new')
        dest = self._bundle('Dest.app', 'old')
        self._run(self._dead_pid(), src, dest)
        leftovers = [n for n in os.listdir(self.tmp) if '.old-' in n]
        self.assertEqual(leftovers, [])

    def test_it_waits_for_the_process_to_die(self):
        live = self._reaped(['/bin/sh', '-c', 'sleep 1'])
        src = self._bundle('New.app', 'new')
        dest = os.path.join(self.tmp, 'Dest.app')
        started = time.time()
        self._run(live.pid, src, dest)
        self.assertGreaterEqual(time.time() - started, 0.9)
        self.assertEqual(self._marker(dest), 'new')

    def test_a_process_that_never_dies_touches_nothing(self):
        # Swapping a bundle under a live process is worse than not updating.
        live = self._reaped(['/bin/sh', '-c', 'sleep 60'])
        self.addCleanup(live.kill)
        src = self._bundle('New.app', 'new')
        dest = self._bundle('Dest.app', 'old')
        with mock.patch.object(updater, 'WAIT_TICKS', 3):
            self._run(live.pid, src, dest)
        self.assertEqual(self._marker(dest), 'old')
        self.assertFalse(os.path.exists(self.opened))


class ChecksumTests(unittest.TestCase):

    def test_sha256_of_a_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as fh:
            fh.write(b'hello')
        self.addCleanup(os.unlink, fh.name)
        self.assertEqual(
            updater.sha256(fh.name),
            '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824')

    def test_expected_sha_picks_the_right_line(self):
        sums = ('aaaa  OTHER.zip\n'
                'bbbb  CEI-PDF-Signer-v0.14-beta-macOS.zip\n')
        self.assertEqual(
            updater.expected_sha(sums, 'CEI-PDF-Signer-v0.14-beta-macOS.zip'), 'bbbb')

    def test_expected_sha_missing_entry(self):
        self.assertIsNone(updater.expected_sha('aaaa  OTHER.zip\n', 'ours.zip'))


class VerifyTests(unittest.TestCase):
    """Every check must be able to fail the whole verification on its own."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.zip = os.path.join(self.tmp, 'a.zip')
        with open(self.zip, 'wb') as fh:
            fh.write(b'hello')
        self.sha = ('2cf24dba5fb0a30e26e83b2ac5b9e29e'
                    '1b161e5c1fa7425e73043362938b9824')
        self.bundle = os.path.join(self.tmp, 'X.app')
        os.makedirs(self.bundle)

    def _run(self, ok=True, team='ABCD1234', quarantined=False):
        def fake(argv, **kwargs):
            if argv[0] == 'codesign' and '-dv' in argv:
                return subprocess.CompletedProcess(
                    argv, 0, '', 'TeamIdentifier=%s\n' % team)
            if argv[0] == 'xattr':
                out = 'com.apple.quarantine\n' if quarantined else ''
                return subprocess.CompletedProcess(argv, 0, out, '')
            return subprocess.CompletedProcess(argv, 0 if ok else 1, '', 'nope')
        return fake

    def test_a_good_bundle_verifies(self):
        updater.verify(self.zip, self.bundle, self.sha, 'ABCD1234',
                       run=self._run())

    def test_wrong_checksum_is_refused(self):
        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, 'deadbeef', 'ABCD1234',
                           run=self._run())

    def test_a_different_team_is_refused(self):
        # The check that actually matters: a bundle signed by someone else is
        # not ours, whatever the release page says.
        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, self.sha, 'ABCD1234',
                           run=self._run(team='EVIL0000'))

    def test_a_broken_signature_is_refused(self):
        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, self.sha, 'ABCD1234',
                           run=self._run(ok=False))

    def test_quarantined_download_is_refused(self):
        # We fetch with Python, so LaunchServices never stamps this. Finding it
        # means something happened that we do not understand.
        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, self.sha, 'ABCD1234',
                           run=self._run(quarantined=True))

    def test_an_adhoc_bundle_has_no_team(self):
        # codesign says "TeamIdentifier=not set" for ad-hoc. Read naively that
        # is the team "not", and two unsigned bundles would compare equal.
        def fake(argv, **kwargs):
            return subprocess.CompletedProcess(
                argv, 0, '', 'TeamIdentifier=not set\n')

        self.assertIsNone(updater.team_identifier(self.bundle, run=fake))

    def test_an_unsigned_download_is_refused(self):
        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, self.sha, None,
                           run=self._run(team='not set'))

    def test_checksum_is_checked_before_anything_expensive(self):
        seen = []

        def fake(argv, **kwargs):
            seen.append(argv[0])
            return subprocess.CompletedProcess(argv, 0, '', 'TeamIdentifier=X\n')

        with self.assertRaises(updater.VerificationError):
            updater.verify(self.zip, self.bundle, 'deadbeef', 'X', run=fake)
        self.assertEqual(seen, [])


class UpdateStatusTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        app_module.reset_update_state()

    def test_idle_by_default(self):
        body = self.client.get('/api/update/status').get_json()
        self.assertEqual(body['stage'], 'idle')
        self.assertIsNone(body['tag'])

    def test_available_after_a_check_finds_one(self):
        found = updater.Update('v0.14-beta', 'https://example.invalid/a.zip',
                               10, 'https://example.invalid/SHA256SUMS.txt',
                               'https://example.invalid/page')
        with mock.patch.object(app_module.updater, 'check', return_value=found), \
             mock.patch.object(app_module.updater, 'is_installable', return_value=True):
            app_module.start_update_check().join(5)
        body = self.client.get('/api/update/status').get_json()
        self.assertEqual(body['stage'], 'available')
        self.assertEqual(body['tag'], 'v0.14-beta')
        self.assertTrue(body['installable'])

    def test_a_failed_check_stays_idle(self):
        # A signer that cannot reach GitHub is still a working signer.
        with mock.patch.object(app_module.updater, 'check', return_value=None):
            app_module.start_update_check().join(5)
        self.assertEqual(
            self.client.get('/api/update/status').get_json()['stage'], 'idle')


class UpdateStartTests(unittest.TestCase):

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        app_module.reset_update_state()
        self.found = updater.Update('v0.14-beta', 'https://example.invalid/a.zip',
                                    10, 'https://example.invalid/SHA256SUMS.txt',
                                    'https://example.invalid/page')

    def _make_available(self):
        with mock.patch.object(app_module.updater, 'check', return_value=self.found), \
             mock.patch.object(app_module.updater, 'is_installable', return_value=True):
            app_module.start_update_check().join(5)

    def test_start_is_rejected_when_nothing_is_available(self):
        self.assertEqual(self.client.post('/api/update/start').status_code, 409)

    def test_start_is_rejected_while_the_card_driver_is_busy(self):
        # Restarting mid-PKCS#11 is what wedges the driver until reboot.
        self._make_available()
        with mock.patch.object(app_module, 'driver_busy', return_value=True):
            self.assertEqual(self.client.post('/api/update/start').status_code, 409)

    def test_start_is_rejected_when_the_bundle_cannot_be_replaced(self):
        with mock.patch.object(app_module.updater, 'check', return_value=self.found), \
             mock.patch.object(app_module.updater, 'is_installable', return_value=False):
            app_module.start_update_check().join(5)
        self.assertEqual(self.client.post('/api/update/start').status_code, 409)

    def test_start_is_rejected_twice(self):
        self._make_available()
        with mock.patch.object(app_module, '_run_update', lambda: None):
            self.assertEqual(self.client.post('/api/update/start').status_code, 200)
            self.assertEqual(self.client.post('/api/update/start').status_code, 409)


class UpdateDownloadLinkTests(unittest.TestCase):
    """The fallback opener must not become an arbitrary-URL opener.

    /api/open-external is guarded by test_raw_url_cannot_be_injected; this
    route takes no parameters at all, so the same property holds by shape.
    """

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        app_module.reset_update_state()

    def _found(self, page_url):
        found = updater.Update('v0.14-beta', 'z', 10, 's', page_url)
        with mock.patch.object(app_module.updater, 'check', return_value=found), \
             mock.patch.object(app_module.updater, 'is_installable', return_value=False):
            app_module.start_update_check().join(5)

    def test_opens_the_stored_release_page(self):
        url = 'https://github.com/sudondream/cei-pdf-signer/releases/v0.14-beta'
        self._found(url)
        with mock.patch.object(app_module.subprocess, 'run') as run:
            resp = self.client.post('/api/update/open-download')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(run.call_args[0][0], ['open', url])

    def test_a_url_in_the_body_is_ignored(self):
        self._found('https://real.example')
        with mock.patch.object(app_module.subprocess, 'run') as run:
            self.client.post('/api/update/open-download',
                             json={'url': 'https://evil.example.com'})
        self.assertEqual(run.call_args[0][0], ['open', 'https://real.example'])

    def test_nothing_to_open_is_rejected(self):
        with mock.patch.object(app_module.subprocess, 'run') as run:
            resp = self.client.post('/api/update/open-download')
        self.assertEqual(resp.status_code, 400)
        run.assert_not_called()


class RelaunchWiringTests(unittest.TestCase):
    """The quit path must drain the card driver before spawning anything."""

    def test_helper_is_detached_and_the_driver_is_drained_first(self):
        import main as main_module
        order = []
        window = mock.Mock()
        window.destroy.side_effect = lambda: order.append('destroy')

        with mock.patch.object(main_module, 'wait_for_driver',
                               side_effect=lambda: order.append('drain')), \
             mock.patch.object(main_module.subprocess, 'Popen',
                               side_effect=lambda *a, **k: order.append('spawn')) as popen, \
             mock.patch.object(main_module.os, '_exit',
                               side_effect=lambda code: order.append('exit')):
            main_module._quit_and_relaunch(['/bin/sh', '-c', 'true'], window=window)

        self.assertEqual(order[0], 'drain',
                         'spawning before the driver drains risks the wedge')
        self.assertIn('spawn', order)
        self.assertTrue(popen.call_args[1]['start_new_session'],
                        'the helper must outlive this process')


class UpdateUiTests(unittest.TestCase):

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, 'templates', 'index.html')) as fh:
            self.src = fh.read()

    def test_banner_and_pill_both_exist(self):
        self.assertIn('id="update-banner"', self.src)
        self.assertIn('id="update-pill"', self.src)

    def test_dismissing_the_banner_reveals_the_pill(self):
        # The offer must survive a dismissal, or it is gone forever.
        start = self.src.find('function dismissUpdateBanner()')
        self.assertNotEqual(start, -1, 'dismissUpdateBanner not found')
        body = self.src[start:self.src.find('function ', start + 10)]
        self.assertIn("getElementById('update-pill')", body)
        self.assertIn("classList.add('visible')", body)

    def test_the_frontend_never_supplies_a_download_url(self):
        # The route takes no parameters; the page must not pretend otherwise.
        call = re.search(r"fetch\('/api/update/open-download'[^)]*\)", self.src)
        self.assertIsNotNone(call, 'open-download is never called')
        self.assertNotIn('body', call.group(0))

    def test_unusable_locations_offer_a_download_instead(self):
        self.assertIn('installable', self.src)
        self.assertIn('Descarca', self.src)

    def test_the_file_list_selector_matches_the_real_markup(self):
        # startUpdate() warns before dropping a queue; a selector that silently
        # matches nothing would skip the warning and lose the user's files.
        self.assertIn('id="file-list"', self.src)
        self.assertIn("className = 'file-item'", self.src)
        self.assertIn("'#file-list .file-item'", self.src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
