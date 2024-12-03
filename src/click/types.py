import os
import stat
import sys
import typing as t
from datetime import datetime
from gettext import gettext as _
from gettext import ngettext
from ._compat import _get_argv_encoding
from ._compat import open_stream
from .exceptions import BadParameter
from .utils import format_filename
from .utils import LazyFile
from .utils import safecall
if t.TYPE_CHECKING:
    import typing_extensions as te
    from .core import Context
    from .core import Parameter
    from .shell_completion import CompletionItem

class ParamType:
    """Represents the type of a parameter. Validates and converts values
    from the command line or Python into the correct type.

    To implement a custom type, subclass and implement at least the
    following:

    -   The :attr:`name` class attribute must be set.
    -   Calling an instance of the type with ``None`` must return
        ``None``. This is already implemented by default.
    -   :meth:`convert` must convert string values to the correct type.
    -   :meth:`convert` must accept values that are already the correct
        type.
    -   It must be able to convert a value if the ``ctx`` and ``param``
        arguments are ``None``. This can occur when converting prompt
        input.
    """
    is_composite: t.ClassVar[bool] = False
    arity: t.ClassVar[int] = 1
    name: str
    envvar_list_splitter: t.ClassVar[t.Optional[str]] = None

    def to_info_dict(self) -> t.Dict[str, t.Any]:
        """Gather information that could be useful for a tool generating
        user-facing documentation.

        Use :meth:`click.Context.to_info_dict` to traverse the entire
        CLI structure.

        .. versionadded:: 8.0
        """
        return {
            "name": self.name,
            "param_type_name": self.param_type_name,
            "opts": self.opts,
            "secondary_opts": self.secondary_opts,
            "type": str(self.type),
            "required": self.required,
            "nargs": self.nargs,
            "multiple": self.multiple,
            "default": self.default,
            "envvar": self.envvar,
            "help": self.help,
        }

    def __call__(self, value: t.Any, param: t.Optional['Parameter']=None, ctx: t.Optional['Context']=None) -> t.Any:
        if value is not None:
            return self.convert(value, param, ctx)

    def get_metavar(self, param: 'Parameter') -> t.Optional[str]:
        """Returns the metavar default for this param if it provides one."""
        if self.metavar is not None:
            return self.metavar
        elif self.name is not None:
            return self.name.upper()
        return None

    def get_missing_message(self, param: 'Parameter') -> t.Optional[str]:
        """Optionally might return extra information about a missing
        parameter.

        .. versionadded:: 2.0
        """
        if self.required:
            return f"Missing required parameter: {param.name}"
        return None

    def convert(self, value: t.Any, param: t.Optional['Parameter'], ctx: t.Optional['Context']) -> t.Any:
        """Convert the value to the correct type. This is not called if
        the value is ``None`` (the missing value).

        This must accept string values from the command line, as well as
        values that are already the correct type. It may also convert
        other compatible types.

        The ``param`` and ``ctx`` arguments may be ``None`` in certain
        situations, such as when converting prompt input.

        If the value cannot be converted, call :meth:`fail` with a
        descriptive message.

        :param value: The value to convert.
        :param param: The parameter that is using this type to convert
            its value. May be ``None``.
        :param ctx: The current context that arrived at this value. May
            be ``None``.
        """
        try:
            return self.type(value)
        except ValueError:
            self.fail(f"'{value}' is not a valid {self.name}", param, ctx)

    def split_envvar_value(self, rv: str) -> t.Sequence[str]:
        """Given a value from an environment variable this splits it up
        into small chunks depending on the defined envvar list splitter.

        If the splitter is set to `None`, which means that whitespace splits,
        then leading and trailing whitespace is ignored.  Otherwise, leading
        and trailing splitters usually lead to empty items being included.
        """
        if self.envvar_list_splitter is None:
            return [x.strip() for x in rv.split()]
        return rv.split(self.envvar_list_splitter)

    def fail(self, message: str, param: t.Optional['Parameter']=None, ctx: t.Optional['Context']=None) -> 't.NoReturn':
        """Helper method to fail with an invalid value message."""
        from .exceptions import BadParameter
        raise BadParameter(message, ctx=ctx, param=param)

    def shell_complete(self, ctx: 'Context', param: 'Parameter', incomplete: str) -> t.List['CompletionItem']:
        """Return a list of
        :class:`~click.shell_completion.CompletionItem` objects for the
        incomplete value. Most types do not provide completions, but
        some do, and this allows custom types to provide custom
        completions as well.

        :param ctx: Invocation context for this command.
        :param param: The parameter that is requesting completion.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        return []

class CompositeParamType(ParamType):
    is_composite = True

class FuncParamType(ParamType):

    def __init__(self, func: t.Callable[[t.Any], t.Any]) -> None:
        self.name: str = func.__name__
        self.func = func

class UnprocessedParamType(ParamType):
    name = 'text'

    def __repr__(self) -> str:
        return 'UNPROCESSED'

class StringParamType(ParamType):
    name = 'text'

    def __repr__(self) -> str:
        return 'STRING'

class Choice(ParamType):
    """The choice type allows a value to be checked against a fixed set
    of supported values. All of these values have to be strings.

    You should only pass a list or tuple of choices. Other iterables
    (like generators) may lead to surprising results.

    The resulting value will always be one of the originally passed choices
    regardless of ``case_sensitive`` or any ``ctx.token_normalize_func``
    being specified.

    See :ref:`choice-opts` for an example.

    :param case_sensitive: Set to false to make choices case
        insensitive. Defaults to true.
    """
    name = 'choice'

    def __init__(self, choices: t.Sequence[str], case_sensitive: bool=True) -> None:
        self.choices = choices
        self.case_sensitive = case_sensitive

    def __repr__(self) -> str:
        return f'Choice({list(self.choices)})'

    def shell_complete(self, ctx: 'Context', param: 'Parameter', incomplete: str) -> t.List['CompletionItem']:
        """Complete choices that start with the incomplete value.

        :param ctx: Invocation context for this command.
        :param param: The parameter that is requesting completion.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        from .shell_completion import CompletionItem

        str_choices = [str(choice) for choice in self.choices]

        if self.case_sensitive:
            matched = [choice for choice in str_choices if choice.startswith(incomplete)]
        else:
            incomplete = incomplete.lower()
            matched = [choice for choice in str_choices if choice.lower().startswith(incomplete)]

        return [CompletionItem(choice) for choice in matched]

class DateTime(ParamType):
    """The DateTime type converts date strings into `datetime` objects.

    The format strings which are checked are configurable, but default to some
    common (non-timezone aware) ISO 8601 formats.

    When specifying *DateTime* formats, you should only pass a list or a tuple.
    Other iterables, like generators, may lead to surprising results.

    The format strings are processed using ``datetime.strptime``, and this
    consequently defines the format strings which are allowed.

    Parsing is tried using each format, in order, and the first format which
    parses successfully is used.

    :param formats: A list or tuple of date format strings, in the order in
                    which they should be tried. Defaults to
                    ``'%Y-%m-%d'``, ``'%Y-%m-%dT%H:%M:%S'``,
                    ``'%Y-%m-%d %H:%M:%S'``.
    """
    name = 'datetime'

    def __init__(self, formats: t.Optional[t.Sequence[str]]=None):
        self.formats: t.Sequence[str] = formats or ['%Y-%m-%d', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M:%S']

    def __repr__(self) -> str:
        return 'DateTime'

class _NumberParamTypeBase(ParamType):
    _number_class: t.ClassVar[t.Type[t.Any]]

class _NumberRangeBase(_NumberParamTypeBase):

    def __init__(self, min: t.Optional[float]=None, max: t.Optional[float]=None, min_open: bool=False, max_open: bool=False, clamp: bool=False) -> None:
        self.min = min
        self.max = max
        self.min_open = min_open
        self.max_open = max_open
        self.clamp = clamp

    def _clamp(self, bound: float, dir: 'te.Literal[1, -1]', open: bool) -> float:
        """Find the valid value to clamp to bound in the given
        direction.

        :param bound: The boundary value.
        :param dir: 1 or -1 indicating the direction to move.
        :param open: If true, the range does not include the bound.
        """
        if not open:
            return bound
        
        # Move away from the bound by the smallest possible increment
        return bound + dir * sys.float_info.epsilon

    def _describe_range(self) -> str:
        """Describe the range for use in help text."""
        if self.min is None and self.max is None:
            return "any value"
        
        if self.min is not None and self.max is not None:
            return f"{self.min}{' (exclusive)' if self.min_open else ''} to {self.max}{' (exclusive)' if self.max_open else ''}"
        
        if self.min is not None:
            return f"{self.min}{' (exclusive)' if self.min_open else ''} or greater"
        
        return f"{self.max}{' (exclusive)' if self.max_open else ''} or less"

    def __repr__(self) -> str:
        clamp = ' clamped' if self.clamp else ''
        return f'<{type(self).__name__} {self._describe_range()}{clamp}>'

class IntParamType(_NumberParamTypeBase):
    name = 'integer'
    _number_class = int

    def __repr__(self) -> str:
        return 'INT'

class IntRange(_NumberRangeBase, IntParamType):
    """Restrict an :data:`click.INT` value to a range of accepted
    values. See :ref:`ranges`.

    If ``min`` or ``max`` are not passed, any value is accepted in that
    direction. If ``min_open`` or ``max_open`` are enabled, the
    corresponding boundary is not included in the range.

    If ``clamp`` is enabled, a value outside the range is clamped to the
    boundary instead of failing.

    .. versionchanged:: 8.0
        Added the ``min_open`` and ``max_open`` parameters.
    """
    name = 'integer range'

class FloatParamType(_NumberParamTypeBase):
    name = 'float'
    _number_class = float

    def __repr__(self) -> str:
        return 'FLOAT'

class FloatRange(_NumberRangeBase, FloatParamType):
    """Restrict a :data:`click.FLOAT` value to a range of accepted
    values. See :ref:`ranges`.

    If ``min`` or ``max`` are not passed, any value is accepted in that
    direction. If ``min_open`` or ``max_open`` are enabled, the
    corresponding boundary is not included in the range.

    If ``clamp`` is enabled, a value outside the range is clamped to the
    boundary instead of failing. This is not supported if either
    boundary is marked ``open``.

    .. versionchanged:: 8.0
        Added the ``min_open`` and ``max_open`` parameters.
    """
    name = 'float range'

    def __init__(self, min: t.Optional[float]=None, max: t.Optional[float]=None, min_open: bool=False, max_open: bool=False, clamp: bool=False) -> None:
        super().__init__(min=min, max=max, min_open=min_open, max_open=max_open, clamp=clamp)
        if (min_open or max_open) and clamp:
            raise TypeError('Clamping is not supported for open bounds.')

class BoolParamType(ParamType):
    name = 'boolean'

    def __repr__(self) -> str:
        return 'BOOL'

class UUIDParameterType(ParamType):
    name = 'uuid'

    def __repr__(self) -> str:
        return 'UUID'

class File(ParamType):
    """Declares a parameter to be a file for reading or writing.  The file
    is automatically closed once the context tears down (after the command
    finished working).

    Files can be opened for reading or writing.  The special value ``-``
    indicates stdin or stdout depending on the mode.

    By default, the file is opened for reading text data, but it can also be
    opened in binary mode or for writing.  The encoding parameter can be used
    to force a specific encoding.

    The `lazy` flag controls if the file should be opened immediately or upon
    first IO. The default is to be non-lazy for standard input and output
    streams as well as files opened for reading, `lazy` otherwise. When opening a
    file lazily for reading, it is still opened temporarily for validation, but
    will not be held open until first IO. lazy is mainly useful when opening
    for writing to avoid creating the file until it is needed.

    Starting with Click 2.0, files can also be opened atomically in which
    case all writes go into a separate file in the same folder and upon
    completion the file will be moved over to the original location.  This
    is useful if a file regularly read by other users is modified.

    See :ref:`file-args` for more information.
    """
    name = 'filename'
    envvar_list_splitter: t.ClassVar[str] = os.path.pathsep

    def __init__(self, mode: str='r', encoding: t.Optional[str]=None, errors: t.Optional[str]='strict', lazy: t.Optional[bool]=None, atomic: bool=False) -> None:
        self.mode = mode
        self.encoding = encoding
        self.errors = errors
        self.lazy = lazy
        self.atomic = atomic

    def shell_complete(self, ctx: 'Context', param: 'Parameter', incomplete: str) -> t.List['CompletionItem']:
        """Return a special completion marker that tells the completion
        system to use the shell to provide file path completions.

        :param ctx: Invocation context for this command.
        :param param: The parameter that is requesting completion.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        from .shell_completion import CompletionItem
        return [CompletionItem(incomplete, type='file')]

class Path(ParamType):
    """The ``Path`` type is similar to the :class:`File` type, but
    returns the filename instead of an open file. Various checks can be
    enabled to validate the type of file and permissions.

    :param exists: The file or directory needs to exist for the value to
        be valid. If this is not set to ``True``, and the file does not
        exist, then all further checks are silently skipped.
    :param file_okay: Allow a file as a value.
    :param dir_okay: Allow a directory as a value.
    :param readable: if true, a readable check is performed.
    :param writable: if true, a writable check is performed.
    :param executable: if true, an executable check is performed.
    :param resolve_path: Make the value absolute and resolve any
        symlinks. A ``~`` is not expanded, as this is supposed to be
        done by the shell only.
    :param allow_dash: Allow a single dash as a value, which indicates
        a standard stream (but does not open it). Use
        :func:`~click.open_file` to handle opening this value.
    :param path_type: Convert the incoming path value to this type. If
        ``None``, keep Python's default, which is ``str``. Useful to
        convert to :class:`pathlib.Path`.

    .. versionchanged:: 8.1
        Added the ``executable`` parameter.

    .. versionchanged:: 8.0
        Allow passing ``path_type=pathlib.Path``.

    .. versionchanged:: 6.0
        Added the ``allow_dash`` parameter.
    """
    envvar_list_splitter: t.ClassVar[str] = os.path.pathsep

    def __init__(self, exists: bool=False, file_okay: bool=True, dir_okay: bool=True, writable: bool=False, readable: bool=True, resolve_path: bool=False, allow_dash: bool=False, path_type: t.Optional[t.Type[t.Any]]=None, executable: bool=False):
        self.exists = exists
        self.file_okay = file_okay
        self.dir_okay = dir_okay
        self.readable = readable
        self.writable = writable
        self.executable = executable
        self.resolve_path = resolve_path
        self.allow_dash = allow_dash
        self.type = path_type
        if self.file_okay and (not self.dir_okay):
            self.name: str = _('file')
        elif self.dir_okay and (not self.file_okay):
            self.name = _('directory')
        else:
            self.name = _('path')

    def shell_complete(self, ctx: 'Context', param: 'Parameter', incomplete: str) -> t.List['CompletionItem']:
        """Return a special completion marker that tells the completion
        system to use the shell to provide path completions for only
        directories or any paths.

        :param ctx: Invocation context for this command.
        :param param: The parameter that is requesting completion.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        pass

class Tuple(CompositeParamType):
    """The default behavior of Click is to apply a type on a value directly.
    This works well in most cases, except for when `nargs` is set to a fixed
    count and different types should be used for different items.  In this
    case the :class:`Tuple` type can be used.  This type can only be used
    if `nargs` is set to a fixed number.

    For more information see :ref:`tuple-type`.

    This can be selected by using a Python tuple literal as a type.

    :param types: a list of types that should be used for the tuple items.
    """

    def __init__(self, types: t.Sequence[t.Union[t.Type[t.Any], ParamType]]) -> None:
        self.types: t.Sequence[ParamType] = [convert_type(ty) for ty in types]

def convert_type(ty: t.Optional[t.Any], default: t.Optional[t.Any]=None) -> ParamType:
    """Find the most appropriate :class:`ParamType` for the given Python
    type. If the type isn't provided, it can be inferred from a default
    value.
    """
    if ty is None and default is not None:
        ty = type(default)

    if isinstance(ty, ParamType):
        return ty
    if ty is str or ty is None:
        return STRING
    if ty is int:
        return INT
    if ty is float:
        return FLOAT
    if ty is bool:
        return BOOL
    if ty is uuid.UUID:
        return UUID
    if hasattr(ty, '__origin__') and ty.__origin__ is t.Union:
        for t in ty.__args__:
            if t is not type(None):
                return convert_type(t)
    if isinstance(ty, tuple):
        return Tuple(ty)

    return STRING
UNPROCESSED = UnprocessedParamType()
STRING = StringParamType()
INT = IntParamType()
FLOAT = FloatParamType()
BOOL = BoolParamType()
UUID = UUIDParameterType()
