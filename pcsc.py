#!/usr/bin/env python3
"""Smart card reader detection through the macOS PC/SC stack.

Detection used to shell out to `opensc-tool`, which is not part of macOS. On a
machine without it the app reported "PKCS#11 not found" - blaming a library
path that was never consulted - and no amount of reconfiguring that path could
help. PCSC.framework ships with every macOS, so this needs nothing installed.

Constants and struct layout come from the SDK's pcsclite.h.
"""
import ctypes

PCSC_FRAMEWORK = "/System/Library/Frameworks/PCSC.framework/PCSC"

SCARD_S_SUCCESS = 0x00000000
SCARD_E_INVALID_HANDLE = 0x80100003
SCARD_E_INSUFFICIENT_BUFFER = 0x80100008
SCARD_E_NO_SERVICE = 0x8010001D
SCARD_E_SERVICE_STOPPED = 0x8010001E
SCARD_E_NO_READERS_AVAILABLE = 0x8010002E

# Failures of the service itself rather than of one reader. A reader that will
# not answer can be skipped; these mean nothing further can be trusted, and
# quietly reporting "no card" for them would send the user to re-seat a card
# that is already seated.
SERVICE_LEVEL_ERRORS = frozenset({
    SCARD_E_NO_SERVICE,
    SCARD_E_SERVICE_STOPPED,
    SCARD_E_INVALID_HANDLE,
})

SCARD_SCOPE_SYSTEM = 0x0002

SCARD_STATE_UNAWARE = 0x0000
SCARD_STATE_EMPTY = 0x0010
SCARD_STATE_PRESENT = 0x0020

MAX_ATR_SIZE = 33

# pcsclite.h: `typedef int32_t SCARDCONTEXT` on Apple - a handle, not a pointer.
SCARDCONTEXT = ctypes.c_int32


class PCSCError(Exception):
    """PC/SC could not be reached, or refused a call."""


class SCARD_READERSTATE(ctypes.Structure):
    # pcsclite.h wraps this struct in `#pragma pack(1)`.
    _pack_ = 1
    _fields_ = [
        ("szReader", ctypes.c_char_p),
        ("pvUserData", ctypes.c_void_p),
        ("dwCurrentState", ctypes.c_uint32),
        ("dwEventState", ctypes.c_uint32),
        ("cbAtr", ctypes.c_uint32),
        ("rgbAtr", ctypes.c_ubyte * MAX_ATR_SIZE),
    ]


def _rv(value):
    """Normalise a return code to unsigned.

    The C functions return a signed 32-bit LONG, so failures like
    SCARD_E_NO_SERVICE (0x8010001D) arrive negative and compare equal to
    nothing.
    """
    return value & 0xFFFFFFFF


def load_pcsc():
    """Open PCSC.framework and declare the signatures we use."""
    try:
        lib = ctypes.CDLL(PCSC_FRAMEWORK)
    except OSError as exc:
        raise PCSCError(f"cannot load PCSC.framework: {exc}") from exc

    lib.SCardEstablishContext.restype = ctypes.c_uint32
    lib.SCardEstablishContext.argtypes = [
        ctypes.c_uint32, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.POINTER(SCARDCONTEXT)]

    lib.SCardListReaders.restype = ctypes.c_uint32
    lib.SCardListReaders.argtypes = [
        SCARDCONTEXT, ctypes.c_char_p, ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_uint32)]

    lib.SCardGetStatusChange.restype = ctypes.c_uint32
    lib.SCardGetStatusChange.argtypes = [
        SCARDCONTEXT, ctypes.c_uint32,
        ctypes.POINTER(SCARD_READERSTATE), ctypes.c_uint32]

    lib.SCardReleaseContext.restype = ctypes.c_uint32
    lib.SCardReleaseContext.argtypes = [SCARDCONTEXT]

    return lib


def _reader_names(lib, ctx, _retries=1):
    """Raw names of every attached reader, via the size-then-data dance.

    Names stay as bytes: they are vendor strings with no encoding guarantee,
    and they have to go back to SCardGetStatusChange byte-for-byte.

    A reader plugged in between the two calls leaves the buffer too small.
    That is a stale measurement, not a fault, so the sizing is retried.
    """
    size = ctypes.c_uint32(0)

    rv = _rv(lib.SCardListReaders(ctx, None, None, ctypes.byref(size)))
    if rv == SCARD_E_NO_READERS_AVAILABLE:
        return []
    if rv != SCARD_S_SUCCESS:
        raise PCSCError(f"SCardListReaders failed (0x{rv:08X})")

    buf = ctypes.create_string_buffer(size.value)
    rv = _rv(lib.SCardListReaders(ctx, None, buf, ctypes.byref(size)))
    if rv == SCARD_E_INSUFFICIENT_BUFFER and _retries > 0:
        return _reader_names(lib, ctx, _retries - 1)
    if rv == SCARD_E_NO_READERS_AVAILABLE:
        return []
    if rv != SCARD_S_SUCCESS:
        raise PCSCError(f"SCardListReaders failed (0x{rv:08X})")

    # A NUL-separated list closed by a second NUL.
    return [name for name in buf.raw[:size.value].split(b"\x00") if name]


def _card_present(lib, ctx, raw_name):
    """Whether a card sits in this reader right now.

    A reader that will not answer is reported empty rather than fatal - one
    sulking reader must not hide the others - but a failure of the service
    itself is raised, so it does not get mistaken for an empty reader.
    """
    state = SCARD_READERSTATE()
    state.szReader = raw_name
    state.dwCurrentState = SCARD_STATE_UNAWARE

    rv = _rv(lib.SCardGetStatusChange(ctx, 0, ctypes.byref(state), 1))
    if rv in SERVICE_LEVEL_ERRORS:
        raise PCSCError(f"SCardGetStatusChange failed (0x{rv:08X})")
    if rv != SCARD_S_SUCCESS:
        return False
    return bool(state.dwEventState & SCARD_STATE_PRESENT)


def list_readers(lib=None):
    """Return [(reader_name, card_present)] for every attached reader.

    Empty when nothing is plugged in. Raises PCSCError when the PC/SC service
    itself is unreachable - a different problem, and worth telling apart.
    """
    lib = load_pcsc() if lib is None else lib

    context = SCARDCONTEXT()
    rv = _rv(lib.SCardEstablishContext(
        SCARD_SCOPE_SYSTEM, None, None, ctypes.byref(context)))
    if rv != SCARD_S_SUCCESS:
        raise PCSCError(f"SCardEstablishContext failed (0x{rv:08X})")

    ctx = context.value
    try:
        # Decoding is for display only; the raw bytes are what PC/SC answers to.
        return [(raw.decode(errors="replace"), _card_present(lib, ctx, raw))
                for raw in _reader_names(lib, ctx)]
    finally:
        lib.SCardReleaseContext(ctx)
