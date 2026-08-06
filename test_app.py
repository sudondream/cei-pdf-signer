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
import os
import tempfile
import unittest
import zipfile
from unittest import mock

import app as app_module


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


if __name__ == '__main__':
    unittest.main(verbosity=2)
