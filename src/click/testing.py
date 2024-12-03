import contextlib
import io
import os
import shlex
import shutil
import sys
import tempfile
import typing as t
from types import TracebackType
from . import formatting
from . import termui
from . import utils
from ._compat import _find_binary_reader
if t.TYPE_CHECKING:
    from .core import BaseCommand

class EchoingStdin:

    def __init__(self, input: t.BinaryIO, output: t.BinaryIO) -> None:
        self._input = input
        self._output = output
        self._paused = False

    def __getattr__(self, x: str) -> t.Any:
        return getattr(self._input, x)

    def __iter__(self) -> t.Iterator[bytes]:
        return iter((self._echo(x) for x in self._input))

    def __repr__(self) -> str:
        return repr(self._input)

class _NamedTextIOWrapper(io.TextIOWrapper):

    def __init__(self, buffer: t.BinaryIO, name: str, mode: str, **kwargs: t.Any) -> None:
        super().__init__(buffer, **kwargs)
        self._name = name
        self._mode = mode

class Result:
    """Holds the captured result of an invoked CLI script."""

    def __init__(self, runner: 'CliRunner', stdout_bytes: bytes, stderr_bytes: t.Optional[bytes], return_value: t.Any, exit_code: int, exception: t.Optional[BaseException], exc_info: t.Optional[t.Tuple[t.Type[BaseException], BaseException, TracebackType]]=None):
        self.runner = runner
        self.stdout_bytes = stdout_bytes
        self.stderr_bytes = stderr_bytes
        self.return_value = return_value
        self.exit_code = exit_code
        self.exception = exception
        self.exc_info = exc_info

    @property
    def output(self) -> str:
        """The (standard) output as unicode string."""
        return self.stdout

    @property
    def stdout(self) -> str:
        """The standard output as unicode string."""
        return self.stdout_bytes.decode(self.runner.charset, 'replace')

    @property
    def stderr(self) -> str:
        """The standard error as unicode string."""
        if self.stderr_bytes is None:
            raise ValueError("stderr not separately captured")
        return self.stderr_bytes.decode(self.runner.charset, 'replace')

    def __repr__(self) -> str:
        exc_str = repr(self.exception) if self.exception else 'okay'
        return f'<{type(self).__name__} {exc_str}>'

class CliRunner:
    """The CLI runner provides functionality to invoke a Click command line
    script for unittesting purposes in a isolated environment.  This only
    works in single-threaded systems without any concurrency as it changes the
    global interpreter state.

    :param charset: the character set for the input and output data.
    :param env: a dictionary with environment variables for overriding.
    :param echo_stdin: if this is set to `True`, then reading from stdin writes
                       to stdout.  This is useful for showing examples in
                       some circumstances.  Note that regular prompts
                       will automatically echo the input.
    :param mix_stderr: if this is set to `False`, then stdout and stderr are
                       preserved as independent streams.  This is useful for
                       Unix-philosophy apps that have predictable stdout and
                       noisy stderr, such that each may be measured
                       independently
    """

    def __init__(self, charset: str='utf-8', env: t.Optional[t.Mapping[str, t.Optional[str]]]=None, echo_stdin: bool=False, mix_stderr: bool=True) -> None:
        self.charset = charset
        self.env: t.Mapping[str, t.Optional[str]] = env or {}
        self.echo_stdin = echo_stdin
        self.mix_stderr = mix_stderr

    def get_default_prog_name(self, cli: 'BaseCommand') -> str:
        """Given a command object it will return the default program name
        for it.  The default is the `name` attribute or ``"root"`` if not
        set.
        """
        return cli.name or "root"

    def make_env(self, overrides: t.Optional[t.Mapping[str, t.Optional[str]]]=None) -> t.Mapping[str, t.Optional[str]]:
        """Returns the environment overrides for invoking a script."""
        env = dict(self.env)
        if overrides:
            env.update(overrides)
        return env

    @contextlib.contextmanager
    def isolation(self, input: t.Optional[t.Union[str, bytes, t.IO[t.Any]]]=None, env: t.Optional[t.Mapping[str, t.Optional[str]]]=None, color: bool=False) -> t.Iterator[t.Tuple[io.BytesIO, t.Optional[io.BytesIO]]]:
        """A context manager that sets up the isolation for invoking of a
        command line tool.  This sets up stdin with the given input data
        and `os.environ` with the overrides from the given dictionary.
        This also rebinds some internals in Click to be mocked (like the
        prompt functionality).

        This is automatically done in the :meth:`invoke` method.

        :param input: the input stream to put into sys.stdin.
        :param env: the environment overrides as dictionary.
        :param color: whether the output should contain color codes. The
                      application can still override this explicitly.

        .. versionchanged:: 8.0
            ``stderr`` is opened with ``errors="backslashreplace"``
            instead of the default ``"strict"``.

        .. versionchanged:: 4.0
            Added the ``color`` parameter.
        """
        if input is None:
            input = b''
        elif isinstance(input, str):
            input = input.encode(self.charset)

        old_stdin = sys.stdin
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        old_forced_width = formatting.FORCED_WIDTH
        old_environ = os.environ
        env = self.make_env(env)

        bytes_output = io.BytesIO()
        bytes_error = io.BytesIO()

        if PY2:
            input = io.BytesIO(input)
            output = bytes_output
            error = bytes_error
        else:
            text_input = io.TextIOWrapper(io.BytesIO(input), encoding=self.charset)
            text_output = io.TextIOWrapper(
                bytes_output, encoding=self.charset, errors="backslashreplace"
            )
            text_error = io.TextIOWrapper(
                bytes_error, encoding=self.charset, errors="backslashreplace"
            )
            input = text_input
            output = text_output
            error = text_error

        if self.echo_stdin:
            input = EchoingStdin(input, output)

        sys.stdin = input
        sys.stdout = output
        sys.stderr = error
        sys.argv = self.get_default_argv()
        os.environ = env
        formatting.FORCED_WIDTH = 80

        def visible_input(prompt=None):
            sys.stdout.write(prompt or '')
            val = input.readline().rstrip('\r\n')
            sys.stdout.write(f'{val}\n')
            sys.stdout.flush()
            return val

        def hidden_input(prompt=None):
            sys.stdout.write((prompt or '') + '\n')
            sys.stdout.flush()
            return input.readline().rstrip('\r\n')

        def _getchar(echo):
            char = sys.stdin.read(1)
            if echo:
                sys.stdout.write(char)
                sys.stdout.flush()
            return char

        old_visible_prompt_func = termui.visible_prompt_func
        old_hidden_prompt_func = termui.hidden_prompt_func
        old__getchar_func = termui._getchar
        termui.visible_prompt_func = visible_input
        termui.hidden_prompt_func = hidden_input
        termui._getchar = _getchar

        old_utils_echo = utils.echo

        def echo(message=None, file=None, nl=True, err=False, color=None):
            if file is None:
                file = sys.stdout if not err else sys.stderr
            old_utils_echo(message, file, nl, err, color)

        utils.echo = echo

        old_utils_get_binary_stream = utils.get_binary_stream

        def get_binary_stream(name):
            if name == 'stdout':
                return bytes_output
            elif name == 'stderr':
                return bytes_error
            return old_utils_get_binary_stream(name)

        utils.get_binary_stream = get_binary_stream

        old_utils_get_text_stream = utils.get_text_stream

        def get_text_stream(name, encoding=None, errors='strict'):
            if name == 'stdout':
                return output
            elif name == 'stderr':
                return error
            return old_utils_get_text_stream(name, encoding, errors)

        utils.get_text_stream = get_text_stream

        try:
            yield bytes_output, bytes_error
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr
            sys.stdin = old_stdin
            sys.argv = sys.argv
            os.environ = old_environ
            formatting.FORCED_WIDTH = old_forced_width
            termui.visible_prompt_func = old_visible_prompt_func
            termui.hidden_prompt_func = old_hidden_prompt_func
            termui._getchar = old__getchar_func
            utils.echo = old_utils_echo
            utils.get_binary_stream = old_utils_get_binary_stream
            utils.get_text_stream = old_utils_get_text_stream

    def invoke(self, cli: 'BaseCommand', args: t.Optional[t.Union[str, t.Sequence[str]]]=None, input: t.Optional[t.Union[str, bytes, t.IO[t.Any]]]=None, env: t.Optional[t.Mapping[str, t.Optional[str]]]=None, catch_exceptions: bool=True, color: bool=False, **extra: t.Any) -> Result:
        """Invokes a command in an isolated environment.  The arguments are
        forwarded directly to the command line script, the `extra` keyword
        arguments are passed to the :meth:`~clickpkg.Command.main` function of
        the command.

        This returns a :class:`Result` object.

        :param cli: the command to invoke
        :param args: the arguments to invoke. It may be given as an iterable
                     or a string. When given as string it will be interpreted
                     as a Unix shell command. More details at
                     :func:`shlex.split`.
        :param input: the input data for `sys.stdin`.
        :param env: the environment overrides.
        :param catch_exceptions: Whether to catch any other exceptions than
                                 ``SystemExit``.
        :param extra: the keyword arguments to pass to :meth:`main`.
        :param color: whether the output should contain color codes. The
                      application can still override this explicitly.

        .. versionchanged:: 8.0
            The result object has the ``return_value`` attribute with
            the value returned from the invoked command.

        .. versionchanged:: 4.0
            Added the ``color`` parameter.

        .. versionchanged:: 3.0
            Added the ``catch_exceptions`` parameter.

        .. versionchanged:: 3.0
            The result object has the ``exc_info`` attribute with the
            traceback if available.
        """
        if isinstance(args, str):
            args = shlex.split(args)

        with self.isolation(input=input, env=env, color=color) as outstreams:
            return_value = None
            exception = None
            exc_info = None
            if catch_exceptions:
                try:
                    return_value = cli.main(args=args or (), prog_name=self.get_default_prog_name(cli), **extra)
                except SystemExit as e:
                    exc_info = sys.exc_info()
                    exception = e
                except Exception as e:
                    exc_info = sys.exc_info()
                    exception = e
            else:
                return_value = cli.main(args=args or (), prog_name=self.get_default_prog_name(cli), **extra)

            output = outstreams[0].getvalue()
            stderr = outstreams[1].getvalue()

        return Result(runner=self,
                      stdout_bytes=output,
                      stderr_bytes=stderr,
                      return_value=return_value,
                      exit_code=exception.code if exception is not None and isinstance(exception, SystemExit) else 0,
                      exception=exception,
                      exc_info=exc_info)

    @contextlib.contextmanager
    def isolated_filesystem(self, temp_dir: t.Optional[t.Union[str, 'os.PathLike[str]']]=None) -> t.Iterator[str]:
        """A context manager that creates a temporary directory and
        changes the current working directory to it. This isolates tests
        that affect the contents of the CWD to prevent them from
        interfering with each other.

        :param temp_dir: Create the temporary directory under this
            directory. If given, the created directory is not removed
            when exiting.

        .. versionchanged:: 8.0
            Added the ``temp_dir`` parameter.
        """
        cwd = os.getcwd()
        if temp_dir is not None:
            temp_dir = os.path.abspath(temp_dir)
            fs = tempfile.mkdtemp(dir=temp_dir)
            os.chdir(fs)
        else:
            fs = tempfile.mkdtemp()
            os.chdir(fs)
        try:
            yield fs
        finally:
            os.chdir(cwd)
            if temp_dir is None:
                try:
                    shutil.rmtree(fs)
                except OSError:
                    pass
