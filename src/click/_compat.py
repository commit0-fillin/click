import codecs
import io
import os
import re
import sys
import typing as t
from weakref import WeakKeyDictionary
CYGWIN = sys.platform.startswith('cygwin')
WIN = sys.platform.startswith('win')
auto_wrap_for_ansi: t.Optional[t.Callable[[t.TextIO], t.TextIO]] = None
_ansi_re = re.compile('\\033\\[[;?0-9]*[a-zA-Z]')

def is_ascii_encoding(encoding: str) -> bool:
    """Checks if a given encoding is ascii."""
    return encoding.replace("-", "").lower() in {"ascii", "usascii"}

def get_best_encoding(stream: t.IO[t.Any]) -> str:
    """Returns the default stream encoding if not found."""
    rv = getattr(stream, "encoding", None) or sys.getdefaultencoding()
    return rv if rv != "ascii" else "utf-8"

class _NonClosingTextIOWrapper(io.TextIOWrapper):

    def __init__(self, stream: t.BinaryIO, encoding: t.Optional[str], errors: t.Optional[str], force_readable: bool=False, force_writable: bool=False, **extra: t.Any) -> None:
        self._stream = stream = t.cast(t.BinaryIO, _FixupStream(stream, force_readable, force_writable))
        super().__init__(stream, encoding, errors, **extra)

    def __del__(self) -> None:
        try:
            self.detach()
        except Exception:
            # Ignore any exception during detach in destructor
            pass

class _FixupStream:
    """The new io interface needs more from streams than streams
    traditionally implement.  As such, this fix-up code is necessary in
    some circumstances.

    The forcing of readable and writable flags are there because some tools
    put badly patched objects on sys (one such offender are certain version
    of jupyter notebook).
    """

    def __init__(self, stream: t.BinaryIO, force_readable: bool=False, force_writable: bool=False):
        self._stream = stream
        self._force_readable = force_readable
        self._force_writable = force_writable

    def __getattr__(self, name: str) -> t.Any:
        return getattr(self._stream, name)

def _stream_is_misconfigured(stream: t.TextIO) -> bool:
    """A stream is misconfigured if its encoding is ASCII."""
    return is_ascii_encoding(getattr(stream, "encoding", None) or "")

def _is_compat_stream_attr(stream: t.TextIO, attr: str, value: t.Optional[str]) -> bool:
    """A stream attribute is compatible if it is equal to the
    desired value or the desired value is unset and the attribute
    has a value.
    """
    stream_value = getattr(stream, attr, None)
    return stream_value == value or (value is None and stream_value is not None)

def _is_compatible_text_stream(stream: t.TextIO, encoding: t.Optional[str], errors: t.Optional[str]) -> bool:
    """Check if a stream's encoding and errors attributes are
    compatible with the desired values.
    """
    return _is_compat_stream_attr(stream, "encoding", encoding) and _is_compat_stream_attr(stream, "errors", errors)

def _wrap_io_open(file: t.Union[str, 'os.PathLike[str]', int], mode: str, encoding: t.Optional[str], errors: t.Optional[str]) -> t.IO[t.Any]:
    """Handles not passing ``encoding`` and ``errors`` in binary mode."""
    if "b" in mode:
        return open(file, mode)
    return open(file, mode, encoding=encoding, errors=errors)

class _AtomicFile:

    def __init__(self, f: t.IO[t.Any], tmp_filename: str, real_filename: str) -> None:
        self._f = f
        self._tmp_filename = tmp_filename
        self._real_filename = real_filename
        self.closed = False

    def __getattr__(self, name: str) -> t.Any:
        return getattr(self._f, name)

    def __enter__(self) -> '_AtomicFile':
        return self

    def __exit__(self, exc_type: t.Optional[t.Type[BaseException]], *_: t.Any) -> None:
        self.close(delete=exc_type is not None)

    def __repr__(self) -> str:
        return repr(self._f)
if sys.platform.startswith('win') and WIN:
    from ._winconsole import _get_windows_console_stream
    _ansi_stream_wrappers: t.MutableMapping[t.TextIO, t.TextIO] = WeakKeyDictionary()

    def auto_wrap_for_ansi(stream: t.TextIO, color: t.Optional[bool]=None) -> t.TextIO:
        """Support ANSI color and style codes on Windows by wrapping a
        stream with colorama.
        """
        try:
            import colorama
        except ImportError:
            return stream

        if isinstance(stream, colorama.AnsiToWin32):
            return stream

        strip = color is False
        if not strip and (color is None or color is True):
            strip = not _get_windows_console_stream(stream) or not colorama.enable_ansi_on_windows()

        wrapper = colorama.AnsiToWin32(stream, strip=strip)
        _ansi_stream_wrappers[stream] = wrapper
        return wrapper
_default_text_stdin = _make_cached_stream_func(lambda: sys.stdin, get_text_stdin)
_default_text_stdout = _make_cached_stream_func(lambda: sys.stdout, get_text_stdout)
_default_text_stderr = _make_cached_stream_func(lambda: sys.stderr, get_text_stderr)
binary_streams: t.Mapping[str, t.Callable[[], t.BinaryIO]] = {'stdin': get_binary_stdin, 'stdout': get_binary_stdout, 'stderr': get_binary_stderr}
text_streams: t.Mapping[str, t.Callable[[t.Optional[str], t.Optional[str]], t.TextIO]] = {'stdin': get_text_stdin, 'stdout': get_text_stdout, 'stderr': get_text_stderr}
