import enum
import errno
import inspect
import os
import sys
import typing as t
from collections import abc
from contextlib import contextmanager
from contextlib import ExitStack
from functools import update_wrapper
from gettext import gettext as _
from gettext import ngettext
from itertools import repeat
from types import TracebackType
from . import types
from .exceptions import Abort
from .exceptions import BadParameter
from .exceptions import ClickException
from .exceptions import Exit
from .exceptions import MissingParameter
from .exceptions import UsageError
from .formatting import HelpFormatter
from .formatting import join_options
from .globals import pop_context
from .globals import push_context
from .parser import _flag_needs_value
from .parser import OptionParser
from .parser import split_opt
from .termui import confirm
from .termui import prompt
from .termui import style
from .utils import _detect_program_name
from .utils import _expand_args
from .utils import echo
from .utils import make_default_short_help
from .utils import make_str
from .utils import PacifyFlushWrapper
if t.TYPE_CHECKING:
    import typing_extensions as te
    from .shell_completion import CompletionItem
F = t.TypeVar('F', bound=t.Callable[..., t.Any])
V = t.TypeVar('V')

def _complete_visible_commands(ctx: 'Context', incomplete: str) -> t.Iterator[t.Tuple[str, 'Command']]:
    """List all the subcommands of a group that start with the
    incomplete value and aren't hidden.

    :param ctx: Invocation context for the group.
    :param incomplete: Value being completed. May be empty.
    """
    if isinstance(ctx.command, MultiCommand):
        for name in ctx.command.list_commands(ctx):
            cmd = ctx.command.get_command(ctx, name)
            if cmd is not None and not cmd.hidden and name.startswith(incomplete):
                yield (name, cmd)

@contextmanager
def augment_usage_errors(ctx: 'Context', param: t.Optional['Parameter']=None) -> t.Iterator[None]:
    """Context manager that attaches extra information to exceptions."""
    try:
        yield
    except UsageError as e:
        if e.ctx is None:
            e.ctx = ctx
        if param is not None and e.param is None:
            e.param = param
        raise

def iter_params_for_processing(invocation_order: t.Sequence['Parameter'], declaration_order: t.Sequence['Parameter']) -> t.List['Parameter']:
    """Given a sequence of parameters in the order as should be considered
    for processing and an iterable of parameters that exist, this returns
    a list in the correct order as they should be processed.
    """
    def sort_key(param):
        if param.is_eager:
            return (0, param.name)
        else:
            return (1, invocation_order.index(param))

    return sorted(declaration_order, key=sort_key)

class ParameterSource(enum.Enum):
    """This is an :class:`~enum.Enum` that indicates the source of a
    parameter's value.

    Use :meth:`click.Context.get_parameter_source` to get the
    source for a parameter by name.

    .. versionchanged:: 8.0
        Use :class:`~enum.Enum` and drop the ``validate`` method.

    .. versionchanged:: 8.0
        Added the ``PROMPT`` value.
    """
    COMMANDLINE = enum.auto()
    'The value was provided by the command line args.'
    ENVIRONMENT = enum.auto()
    'The value was provided with an environment variable.'
    DEFAULT = enum.auto()
    'Used the default specified by the parameter.'
    DEFAULT_MAP = enum.auto()
    'Used a default provided by :attr:`Context.default_map`.'
    PROMPT = enum.auto()
    'Used a prompt to confirm a default or provide a value.'

class Context:
    """The context is a special internal object that holds state relevant
    for the script execution at every single level.  It's normally invisible
    to commands unless they opt-in to getting access to it.

    The context is useful as it can pass internal objects around and can
    control special execution features such as reading data from
    environment variables.

    A context can be used as context manager in which case it will call
    :meth:`close` on teardown.

    :param command: the command class for this context.
    :param parent: the parent context.
    :param info_name: the info name for this invocation.  Generally this
                      is the most descriptive name for the script or
                      command.  For the toplevel script it is usually
                      the name of the script, for commands below it it's
                      the name of the script.
    :param obj: an arbitrary object of user data.
    :param auto_envvar_prefix: the prefix to use for automatic environment
                               variables.  If this is `None` then reading
                               from environment variables is disabled.  This
                               does not affect manually set environment
                               variables which are always read.
    :param default_map: a dictionary (like object) with default values
                        for parameters.
    :param terminal_width: the width of the terminal.  The default is
                           inherit from parent context.  If no context
                           defines the terminal width then auto
                           detection will be applied.
    :param max_content_width: the maximum width for content rendered by
                              Click (this currently only affects help
                              pages).  This defaults to 80 characters if
                              not overridden.  In other words: even if the
                              terminal is larger than that, Click will not
                              format things wider than 80 characters by
                              default.  In addition to that, formatters might
                              add some safety mapping on the right.
    :param resilient_parsing: if this flag is enabled then Click will
                              parse without any interactivity or callback
                              invocation.  Default values will also be
                              ignored.  This is useful for implementing
                              things such as completion support.
    :param allow_extra_args: if this is set to `True` then extra arguments
                             at the end will not raise an error and will be
                             kept on the context.  The default is to inherit
                             from the command.
    :param allow_interspersed_args: if this is set to `False` then options
                                    and arguments cannot be mixed.  The
                                    default is to inherit from the command.
    :param ignore_unknown_options: instructs click to ignore options it does
                                   not know and keeps them for later
                                   processing.
    :param help_option_names: optionally a list of strings that define how
                              the default help parameter is named.  The
                              default is ``['--help']``.
    :param token_normalize_func: an optional function that is used to
                                 normalize tokens (options, choices,
                                 etc.).  This for instance can be used to
                                 implement case insensitive behavior.
    :param color: controls if the terminal supports ANSI colors or not.  The
                  default is autodetection.  This is only needed if ANSI
                  codes are used in texts that Click prints which is by
                  default not the case.  This for instance would affect
                  help output.
    :param show_default: Show the default value for commands. If this
        value is not set, it defaults to the value from the parent
        context. ``Command.show_default`` overrides this default for the
        specific command.

    .. versionchanged:: 8.1
        The ``show_default`` parameter is overridden by
        ``Command.show_default``, instead of the other way around.

    .. versionchanged:: 8.0
        The ``show_default`` parameter defaults to the value from the
        parent context.

    .. versionchanged:: 7.1
       Added the ``show_default`` parameter.

    .. versionchanged:: 4.0
        Added the ``color``, ``ignore_unknown_options``, and
        ``max_content_width`` parameters.

    .. versionchanged:: 3.0
        Added the ``allow_extra_args`` and ``allow_interspersed_args``
        parameters.

    .. versionchanged:: 2.0
        Added the ``resilient_parsing``, ``help_option_names``, and
        ``token_normalize_func`` parameters.
    """
    formatter_class: t.Type['HelpFormatter'] = HelpFormatter

    def __init__(self, command: 'Command', parent: t.Optional['Context']=None, info_name: t.Optional[str]=None, obj: t.Optional[t.Any]=None, auto_envvar_prefix: t.Optional[str]=None, default_map: t.Optional[t.MutableMapping[str, t.Any]]=None, terminal_width: t.Optional[int]=None, max_content_width: t.Optional[int]=None, resilient_parsing: bool=False, allow_extra_args: t.Optional[bool]=None, allow_interspersed_args: t.Optional[bool]=None, ignore_unknown_options: t.Optional[bool]=None, help_option_names: t.Optional[t.List[str]]=None, token_normalize_func: t.Optional[t.Callable[[str], str]]=None, color: t.Optional[bool]=None, show_default: t.Optional[bool]=None) -> None:
        self.parent = parent
        self.command = command
        self.info_name = info_name
        self.params: t.Dict[str, t.Any] = {}
        self.args: t.List[str] = []
        self.protected_args: t.List[str] = []
        self._opt_prefixes: t.Set[str] = set(parent._opt_prefixes) if parent else set()
        if obj is None and parent is not None:
            obj = parent.obj
        self.obj: t.Any = obj
        self._meta: t.Dict[str, t.Any] = getattr(parent, 'meta', {})
        if default_map is None and info_name is not None and (parent is not None) and (parent.default_map is not None):
            default_map = parent.default_map.get(info_name)
        self.default_map: t.Optional[t.MutableMapping[str, t.Any]] = default_map
        self.invoked_subcommand: t.Optional[str] = None
        if terminal_width is None and parent is not None:
            terminal_width = parent.terminal_width
        self.terminal_width: t.Optional[int] = terminal_width
        if max_content_width is None and parent is not None:
            max_content_width = parent.max_content_width
        self.max_content_width: t.Optional[int] = max_content_width
        if allow_extra_args is None:
            allow_extra_args = command.allow_extra_args
        self.allow_extra_args = allow_extra_args
        if allow_interspersed_args is None:
            allow_interspersed_args = command.allow_interspersed_args
        self.allow_interspersed_args: bool = allow_interspersed_args
        if ignore_unknown_options is None:
            ignore_unknown_options = command.ignore_unknown_options
        self.ignore_unknown_options: bool = ignore_unknown_options
        if help_option_names is None:
            if parent is not None:
                help_option_names = parent.help_option_names
            else:
                help_option_names = ['--help']
        self.help_option_names: t.List[str] = help_option_names
        if token_normalize_func is None and parent is not None:
            token_normalize_func = parent.token_normalize_func
        self.token_normalize_func: t.Optional[t.Callable[[str], str]] = token_normalize_func
        self.resilient_parsing: bool = resilient_parsing
        if auto_envvar_prefix is None:
            if parent is not None and parent.auto_envvar_prefix is not None and (self.info_name is not None):
                auto_envvar_prefix = f'{parent.auto_envvar_prefix}_{self.info_name.upper()}'
        else:
            auto_envvar_prefix = auto_envvar_prefix.upper()
        if auto_envvar_prefix is not None:
            auto_envvar_prefix = auto_envvar_prefix.replace('-', '_')
        self.auto_envvar_prefix: t.Optional[str] = auto_envvar_prefix
        if color is None and parent is not None:
            color = parent.color
        self.color: t.Optional[bool] = color
        if show_default is None and parent is not None:
            show_default = parent.show_default
        self.show_default: t.Optional[bool] = show_default
        self._close_callbacks: t.List[t.Callable[[], t.Any]] = []
        self._depth = 0
        self._parameter_source: t.Dict[str, ParameterSource] = {}
        self._exit_stack = ExitStack()

    def to_info_dict(self) -> t.Dict[str, t.Any]:
        """Gather information that could be useful for a tool generating
        user-facing documentation. This traverses the entire CLI
        structure.

        .. code-block:: python

            with Context(cli) as ctx:
                info = ctx.to_info_dict()

        .. versionadded:: 8.0
        """
        pass

    def __enter__(self) -> 'Context':
        self._depth += 1
        push_context(self)
        return self

    def __exit__(self, exc_type: t.Optional[t.Type[BaseException]], exc_value: t.Optional[BaseException], tb: t.Optional[TracebackType]) -> None:
        self._depth -= 1
        if self._depth == 0:
            self.close()
        pop_context()

    @contextmanager
    def scope(self, cleanup: bool=True) -> t.Iterator['Context']:
        """This helper method can be used with the context object to promote
        it to the current thread local (see :func:`get_current_context`).
        The default behavior of this is to invoke the cleanup functions which
        can be disabled by setting `cleanup` to `False`.  The cleanup
        functions are typically used for things such as closing file handles.

        If the cleanup is intended the context object can also be directly
        used as a context manager.

        Example usage::

            with ctx.scope():
                assert get_current_context() is ctx

        This is equivalent::

            with ctx:
                assert get_current_context() is ctx

        .. versionadded:: 5.0

        :param cleanup: controls if the cleanup functions should be run or
                        not.  The default is to run these functions.  In
                        some situations the context only wants to be
                        temporarily pushed in which case this can be disabled.
                        Nested pushes automatically defer the cleanup.
        """
        if self._depth == 0:
            self._depth += 1
            try:
                push_context(self)
                yield self
            finally:
                self._depth -= 1
                if cleanup:
                    self.close()
                pop_context()
        else:
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1

    @property
    def meta(self) -> t.Dict[str, t.Any]:
        """This is a dictionary which is shared with all the contexts
        that are nested.  It exists so that click utilities can store some
        state here if they need to.  It is however the responsibility of
        that code to manage this dictionary well.

        The keys are supposed to be unique dotted strings.  For instance
        module paths are a good choice for it.  What is stored in there is
        irrelevant for the operation of click.  However what is important is
        that code that places data here adheres to the general semantics of
        the system.

        Example usage::

            LANG_KEY = f'{__name__}.lang'

            def set_language(value):
                ctx = get_current_context()
                ctx.meta[LANG_KEY] = value

            def get_language():
                return get_current_context().meta.get(LANG_KEY, 'en_US')

        .. versionadded:: 5.0
        """
        return self._meta

    def make_formatter(self) -> HelpFormatter:
        """Creates the :class:`~click.HelpFormatter` for the help and
        usage output.

        To quickly customize the formatter class used without overriding
        this method, set the :attr:`formatter_class` attribute.

        .. versionchanged:: 8.0
            Added the :attr:`formatter_class` attribute.
        """
        return self.formatter_class(
            width=self.terminal_width,
            max_width=self.max_content_width
        )

    def with_resource(self, context_manager: t.ContextManager[V]) -> V:
        """Register a resource as if it were used in a ``with``
        statement. The resource will be cleaned up when the context is
        popped.

        Uses :meth:`contextlib.ExitStack.enter_context`. It calls the
        resource's ``__enter__()`` method and returns the result. When
        the context is popped, it closes the stack, which calls the
        resource's ``__exit__()`` method.

        To register a cleanup function for something that isn't a
        context manager, use :meth:`call_on_close`. Or use something
        from :mod:`contextlib` to turn it into a context manager first.

        .. code-block:: python

            @click.group()
            @click.option("--name")
            @click.pass_context
            def cli(ctx):
                ctx.obj = ctx.with_resource(connect_db(name))

        :param context_manager: The context manager to enter.
        :return: Whatever ``context_manager.__enter__()`` returns.

        .. versionadded:: 8.0
        """
        return self._exit_stack.enter_context(context_manager)

    def call_on_close(self, f: t.Callable[..., t.Any]) -> t.Callable[..., t.Any]:
        """Register a function to be called when the context tears down.

        This can be used to close resources opened during the script
        execution. Resources that support Python's context manager
        protocol which would be used in a ``with`` statement should be
        registered with :meth:`with_resource` instead.

        :param f: The function to execute on teardown.
        """
        self._close_callbacks.append(f)
        return f

    def close(self) -> None:
        """Invoke all close callbacks registered with
        :meth:`call_on_close`, and exit all context managers entered
        with :meth:`with_resource`.
        """
        for cb in reversed(self._close_callbacks):
            cb()
        self._close_callbacks.clear()
        self._exit_stack.close()

    @property
    def command_path(self) -> str:
        """The computed command path.  This is used for the ``usage``
        information on the help page.  It's automatically created by
        combining the info names of the chain of contexts to the root.
        """
        rv = ''
        for ctx in reversed(self.parents):
            rv = f"{ctx.info_name} {rv}"
        return (rv + self.info_name).strip()

    def find_root(self) -> 'Context':
        """Finds the outermost context."""
        current = self
        while current.parent is not None:
            current = current.parent
        return current

    def find_object(self, object_type: t.Type[V]) -> t.Optional[V]:
        """Finds the closest object of a given type."""
        for ctx in chain((self,), reversed(self.parents)):
            if isinstance(ctx.obj, object_type):
                return ctx.obj
        return None

    def ensure_object(self, object_type: t.Type[V]) -> V:
        """Like :meth:`find_object` but sets the innermost object to a
        new instance of `object_type` if it does not exist.
        """
        rv = self.find_object(object_type)
        if rv is None:
            self.obj = rv = object_type()
        return rv

    def lookup_default(self, name: str, call: bool=True) -> t.Optional[t.Any]:
        """Get the default for a parameter from :attr:`default_map`.

        :param name: Name of the parameter.
        :param call: If the default is a callable, call it. Disable to
            return the callable instead.

        .. versionchanged:: 8.0
            Added the ``call`` parameter.
        """
        if self.default_map is None:
            return None
        value = self.default_map.get(name)
        if call and callable(value):
            return value()
        return value

    def fail(self, message: str) -> 'te.NoReturn':
        """Aborts the execution of the program with a specific error
        message.

        :param message: the error message to fail with.
        """
        raise UsageError(message, self)

    def abort(self) -> 'te.NoReturn':
        """Aborts the script."""
        raise Abort()

    def exit(self, code: int=0) -> 'te.NoReturn':
        """Exits the application with a given exit code."""
        raise Exit(code)

    def get_usage(self) -> str:
        """Helper method to get formatted usage string for the current
        context and command.
        """
        formatter = self.make_formatter()
        self.command.format_usage(self, formatter)
        return formatter.getvalue().rstrip("\n")

    def get_help(self) -> str:
        """Helper method to get formatted help page for the current
        context and command.
        """
        formatter = self.make_formatter()
        self.command.format_help(self, formatter)
        return formatter.getvalue().rstrip("\n")

    def _make_sub_context(self, command: 'Command') -> 'Context':
        """Create a new context of the same type as this context, but
        for a new command.

        :meta private:
        """
        return type(self)(
            command,
            info_name=command.name,
            parent=self,
            allow_extra_args=self.allow_extra_args,
            allow_interspersed_args=self.allow_interspersed_args,
            ignore_unknown_options=self.ignore_unknown_options,
            help_option_names=self.help_option_names,
            token_normalize_func=self.token_normalize_func,
            color=self.color,
            show_default=self.show_default,
        )

    def invoke(__self, __callback: t.Union['Command', 't.Callable[..., V]'], *args: t.Any, **kwargs: t.Any) -> t.Union[t.Any, V]:
        """Invokes a command callback in exactly the way it expects.  There
        are two ways to invoke this method:

        1.  the first argument can be a callback and all other arguments and
            keyword arguments are forwarded directly to the function.
        2.  the first argument is a click command object.  In that case all
            arguments are forwarded as well but proper click parameters
            (options and click arguments) must be keyword arguments and Click
            will fill in defaults.

        Note that before Click 3.2 keyword arguments were not properly filled
        in against the intention of this code and no context was created.  For
        more information about this change and why it was done in a bugfix
        release see :ref:`upgrade-to-3.2`.

        .. versionchanged:: 8.0
            All ``kwargs`` are tracked in :attr:`params` so they will be
            passed if :meth:`forward` is called at multiple levels.
        """
        if isinstance(__callback, Command):
            return __callback.invoke(__self)

        __self.params.update(kwargs)
        with augment_usage_errors(__self):
            with __self.scope(cleanup=False):
                return __callback(*args, **kwargs)

    def forward(__self, __cmd: 'Command', *args: t.Any, **kwargs: t.Any) -> t.Any:
        """Similar to :meth:`invoke` but fills in default keyword
        arguments from the current context if the other command expects
        it.  This cannot invoke callbacks directly, only other commands.

        .. versionchanged:: 8.0
            All ``kwargs`` are tracked in :attr:`params` so they will be
            passed if ``forward`` is called at multiple levels.
        """
        __self.params.update(kwargs)

        for param in __cmd.params:
            if param.name not in kwargs and param.name in __self.params:
                kwargs[param.name] = __self.params[param.name]

        return __self.invoke(__cmd, *args, **kwargs)

    def set_parameter_source(self, name: str, source: ParameterSource) -> None:
        """Set the source of a parameter. This indicates the location
        from which the value of the parameter was obtained.

        :param name: The name of the parameter.
        :param source: A member of :class:`~click.core.ParameterSource`.
        """
        self._parameter_source[name] = source

    def get_parameter_source(self, name: str) -> t.Optional[ParameterSource]:
        """Get the source of a parameter. This indicates the location
        from which the value of the parameter was obtained.

        This can be useful for determining when a user specified a value
        on the command line that is the same as the default value. It
        will be :attr:`~click.core.ParameterSource.DEFAULT` only if the
        value was actually taken from the default.

        :param name: The name of the parameter.
        :rtype: ParameterSource

        .. versionchanged:: 8.0
            Returns ``None`` if the parameter was not provided from any
            source.
        """
        return self._parameter_source.get(name)

class BaseCommand:
    """The base command implements the minimal API contract of commands.
    Most code will never use this as it does not implement a lot of useful
    functionality but it can act as the direct subclass of alternative
    parsing methods that do not depend on the Click parser.

    For instance, this can be used to bridge Click and other systems like
    argparse or docopt.

    Because base commands do not implement a lot of the API that other
    parts of Click take for granted, they are not supported for all
    operations.  For instance, they cannot be used with the decorators
    usually and they have no built-in callback system.

    .. versionchanged:: 2.0
       Added the `context_settings` parameter.

    :param name: the name of the command to use unless a group overrides it.
    :param context_settings: an optional dictionary with defaults that are
                             passed to the context object.
    """
    context_class: t.Type[Context] = Context
    allow_extra_args = False
    allow_interspersed_args = True
    ignore_unknown_options = False

    def __init__(self, name: t.Optional[str], context_settings: t.Optional[t.MutableMapping[str, t.Any]]=None) -> None:
        self.name = name
        if context_settings is None:
            context_settings = {}
        self.context_settings: t.MutableMapping[str, t.Any] = context_settings

    def to_info_dict(self, ctx: Context) -> t.Dict[str, t.Any]:
        """Gather information that could be useful for a tool generating
        user-facing documentation. This traverses the entire structure
        below this command.

        Use :meth:`click.Context.to_info_dict` to traverse the entire
        CLI structure.

        :param ctx: A :class:`Context` representing this command.

        .. versionadded:: 8.0
        """
        return {
            "name": self.name,
            "help": self.help,
            "usage": self.get_usage(ctx),
            "short_help": self.get_short_help_str(),
        }

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} {self.name}>'

    def make_context(self, info_name: t.Optional[str], args: t.List[str], parent: t.Optional[Context]=None, **extra: t.Any) -> Context:
        """This function when given an info name and arguments will kick
        off the parsing and create a new :class:`Context`.  It does not
        invoke the actual command callback though.

        To quickly customize the context class used without overriding
        this method, set the :attr:`context_class` attribute.

        :param info_name: the info name for this invocation.  Generally this
                          is the most descriptive name for the script or
                          command.  For the toplevel script it's usually
                          the name of the script, for commands below it's
                          the name of the command.
        :param args: the arguments to parse as list of strings.
        :param parent: the parent context if available.
        :param extra: extra keyword arguments forwarded to the context
                      constructor.

        .. versionchanged:: 8.0
            Added the :attr:`context_class` attribute.
        """
        for key, value in self.context_settings.items():
            if key not in extra:
                extra[key] = value

        ctx = self.context_class(self, info_name=info_name, parent=parent, **extra)
        with ctx.scope(cleanup=False):
            self.parse_args(ctx, args)
        return ctx

    def parse_args(self, ctx: Context, args: t.List[str]) -> t.List[str]:
        """Given a context and a list of arguments this creates the parser
        and parses the arguments, then modifies the context as necessary.
        This is automatically invoked by :meth:`make_context`.
        """
        return args

    def invoke(self, ctx: Context) -> t.Any:
        """Given a context, this invokes the command.  The default
        implementation is raising a not implemented error.
        """
        raise NotImplementedError("Command subclass did not implement invoke method")

    def shell_complete(self, ctx: Context, incomplete: str) -> t.List['CompletionItem']:
        """Return a list of completions for the incomplete value. Looks
        at the names of chained multi-commands.

        Any command could be part of a chained multi-command, so sibling
        commands are valid at any point during command completion. Other
        command classes will return more completions.

        :param ctx: Invocation context for this command.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        from .shell_completion import CompletionItem

        results = []
        if isinstance(ctx.parent, MultiCommand):
            results.extend(
                CompletionItem(name)
                for name, cmd in _complete_visible_commands(ctx.parent, incomplete)
                if cmd != self
            )

        return results

    def main(self, args: t.Optional[t.Sequence[str]]=None, prog_name: t.Optional[str]=None, complete_var: t.Optional[str]=None, standalone_mode: bool=True, windows_expand_args: bool=True, **extra: t.Any) -> t.Any:
        """This is the way to invoke a script with all the bells and
        whistles as a command line application.  This will always terminate
        the application after a call.  If this is not wanted, ``SystemExit``
        needs to be caught.

        This method is also available by directly calling the instance of
        a :class:`Command`.

        :param args: the arguments that should be used for parsing.  If not
                     provided, ``sys.argv[1:]`` is used.
        :param prog_name: the program name that should be used.  By default
                          the program name is constructed by taking the file
                          name from ``sys.argv[0]``.
        :param complete_var: the environment variable that controls the
                             bash completion support.  The default is
                             ``"_<prog_name>_COMPLETE"`` with prog_name in
                             uppercase.
        :param standalone_mode: the default behavior is to invoke the script
                                in standalone mode.  Click will then
                                handle exceptions and convert them into
                                error messages and the function will never
                                return but shut down the interpreter.  If
                                this is set to `False` they will be
                                propagated to the caller and the return
                                value of this function is the return value
                                of :meth:`invoke`.
        :param windows_expand_args: Expand glob patterns, user dir, and
            env vars in command line args on Windows.
        :param extra: extra keyword arguments are forwarded to the context
                      constructor.  See :class:`Context` for more information.

        .. versionchanged:: 8.0.1
            Added the ``windows_expand_args`` parameter to allow
            disabling command line arg expansion on Windows.

        .. versionchanged:: 8.0
            When taking arguments from ``sys.argv`` on Windows, glob
            patterns, user dir, and env vars are expanded.

        .. versionchanged:: 3.0
           Added the ``standalone_mode`` parameter.
        """
        if args is None:
            args = sys.argv[1:]

        if prog_name is None:
            prog_name = _detect_program_name()

        # Expand args on Windows if requested
        if windows_expand_args and sys.platform.startswith("win"):
            args = _expand_args(args)

        try:
            try:
                with self.make_context(prog_name, list(args), **extra) as ctx:
                    self._main_shell_completion(ctx, extra, prog_name, complete_var)
                    rv = self.invoke(ctx)
                    if not standalone_mode:
                        return rv
                    ctx.exit()
            except (EOFError, KeyboardInterrupt):
                echo(file=sys.stderr)
                raise Abort()
            except ClickException as e:
                if not standalone_mode:
                    raise
                e.show()
                sys.exit(e.exit_code)
            except IOError as e:
                if e.errno == errno.EPIPE:
                    sys.exit(1)
                raise
        except Abort:
            if not standalone_mode:
                raise
            echo("Aborted!", file=sys.stderr)
            sys.exit(1)

    def _main_shell_completion(self, ctx_args: t.MutableMapping[str, t.Any], prog_name: str, complete_var: t.Optional[str]=None) -> None:
        """Check if the shell is asking for tab completion, process
        that, then exit early. Called from :meth:`main` before the
        program is invoked.

        :param prog_name: Name of the executable in the shell.
        :param complete_var: Name of the environment variable that holds
            the completion instruction. Defaults to
            ``_{PROG_NAME}_COMPLETE``.

        .. versionchanged:: 8.2.0
            Dots (``.``) in ``prog_name`` are replaced with underscores (``_``).
        """
        if complete_var is None:
            complete_var = f"_{prog_name.replace('.', '_').upper()}_COMPLETE"

        instruction = os.environ.get(complete_var)
        if not instruction:
            return

        from .shell_completion import shell_complete

        rv = shell_complete(self, ctx_args, instruction)

        if rv is not None:
            echo(rv, nl=False)
            sys.exit(0)

    def __call__(self, *args: t.Any, **kwargs: t.Any) -> t.Any:
        """Alias for :meth:`main`."""
        return self.main(*args, **kwargs)

class Command(BaseCommand):
    """Commands are the basic building block of command line interfaces in
    Click.  A basic command handles command line parsing and might dispatch
    more parsing to commands nested below it.

    :param name: the name of the command to use unless a group overrides it.
    :param context_settings: an optional dictionary with defaults that are
                             passed to the context object.
    :param callback: the callback to invoke.  This is optional.
    :param params: the parameters to register with this command.  This can
                   be either :class:`Option` or :class:`Argument` objects.
    :param help: the help string to use for this command.
    :param epilog: like the help string but it's printed at the end of the
                   help page after everything else.
    :param short_help: the short help to use for this command.  This is
                       shown on the command listing of the parent command.
    :param add_help_option: by default each command registers a ``--help``
                            option.  This can be disabled by this parameter.
    :param no_args_is_help: this controls what happens if no arguments are
                            provided.  This option is disabled by default.
                            If enabled this will add ``--help`` as argument
                            if no arguments are passed
    :param hidden: hide this command from help outputs.

    :param deprecated: issues a message indicating that
                             the command is deprecated.

    .. versionchanged:: 8.1
        ``help``, ``epilog``, and ``short_help`` are stored unprocessed,
        all formatting is done when outputting help text, not at init,
        and is done even if not using the ``@command`` decorator.

    .. versionchanged:: 8.0
        Added a ``repr`` showing the command name.

    .. versionchanged:: 7.1
        Added the ``no_args_is_help`` parameter.

    .. versionchanged:: 2.0
        Added the ``context_settings`` parameter.
    """

    def __init__(self, name: t.Optional[str], context_settings: t.Optional[t.MutableMapping[str, t.Any]]=None, callback: t.Optional[t.Callable[..., t.Any]]=None, params: t.Optional[t.List['Parameter']]=None, help: t.Optional[str]=None, epilog: t.Optional[str]=None, short_help: t.Optional[str]=None, options_metavar: t.Optional[str]='[OPTIONS]', add_help_option: bool=True, no_args_is_help: bool=False, hidden: bool=False, deprecated: bool=False) -> None:
        super().__init__(name, context_settings)
        self.callback = callback
        self.params: t.List['Parameter'] = params or []
        self.help = help
        self.epilog = epilog
        self.options_metavar = options_metavar
        self.short_help = short_help
        self.add_help_option = add_help_option
        self.no_args_is_help = no_args_is_help
        self.hidden = hidden
        self.deprecated = deprecated

    def get_usage(self, ctx: Context) -> str:
        """Formats the usage line into a string and returns it.

        Calls :meth:`format_usage` internally.
        """
        formatter = ctx.make_formatter()
        self.format_usage(ctx, formatter)
        return formatter.getvalue().rstrip('\n')

    def format_usage(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Writes the usage line into the formatter.

        This is a low-level method called by :meth:`get_usage`.
        """
        pieces = self.collect_usage_pieces(ctx)
        formatter.write_usage(ctx.command_path, ' '.join(pieces))

    def collect_usage_pieces(self, ctx: Context) -> t.List[str]:
        """Returns all the pieces that go into the usage line and returns
        it as a list of strings.
        """
        rv = [self.options_metavar]
        for param in self.get_params(ctx):
            rv.extend(param.get_usage_pieces(ctx))
        if self.subcommand_metavar is not None:
            rv.append(self.subcommand_metavar)
        return rv

    def get_help_option_names(self, ctx: Context) -> t.List[str]:
        """Returns the names for the help option."""
        all_names = set(ctx.help_option_names)
        for param in self.params:
            all_names.difference_update(param.opts)
            all_names.difference_update(param.secondary_opts)
        return sorted(all_names)

    def get_help_option(self, ctx: Context) -> t.Optional['Option']:
        """Returns the help option object."""
        help_options = self.get_help_option_names(ctx)
        if not help_options or not self.add_help_option:
            return None

        def show_help(ctx: Context, param: t.Union['Option', 'Parameter'], value: t.Any) -> None:
            if value and not ctx.resilient_parsing:
                echo(ctx.get_help(), color=ctx.color)
                ctx.exit()

        return Option(
            help_options,
            is_flag=True,
            is_eager=True,
            expose_value=False,
            callback=show_help,
            help='Show this message and exit.',
        )

    def make_parser(self, ctx: Context) -> OptionParser:
        """Creates the underlying option parser for this command."""
        parser = OptionParser(ctx)
        for param in self.get_params(ctx):
            param.add_to_parser(parser, ctx)
        return parser

    def get_help(self, ctx: Context) -> str:
        """Formats the help into a string and returns it.

        Calls :meth:`format_help` internally.
        """
        formatter = ctx.make_formatter()
        self.format_help(ctx, formatter)
        return formatter.getvalue().rstrip('\n')

    def get_short_help_str(self, limit: int=45) -> str:
        """Gets short help for the command or makes it by shortening the
        long help string.
        """
        return (self.short_help or self.help or '')[:limit]

    def format_help(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Writes the help into the formatter if it exists.

        This is a low-level method called by :meth:`get_help`.

        This calls the following methods:

        -   :meth:`format_usage`
        -   :meth:`format_help_text`
        -   :meth:`format_options`
        -   :meth:`format_epilog`
        """
        self.format_usage(ctx, formatter)
        self.format_help_text(ctx, formatter)
        self.format_options(ctx, formatter)
        self.format_epilog(ctx, formatter)

    def format_help_text(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Writes the help text to the formatter if it exists."""
        if self.help:
            formatter.write_paragraph()
            with formatter.indentation():
                formatter.write_text(self.help)

    def format_options(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Writes all the options into the formatter if they exist."""
        opts = []
        for param in self.get_params(ctx):
            rv = param.get_help_record(ctx)
            if rv is not None:
                opts.append(rv)

        if opts:
            with formatter.section('Options'):
                formatter.write_dl(opts)

    def format_epilog(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Writes the epilog into the formatter if it exists."""
        if self.epilog:
            formatter.write_paragraph()
            with formatter.indentation():
                formatter.write_text(self.epilog)

    def invoke(self, ctx: Context) -> t.Any:
        """Given a context, this invokes the attached callback (if it exists)
        in the right way.
        """
        if self.callback is not None:
            return ctx.invoke(self.callback, **ctx.params)

    def shell_complete(self, ctx: Context, incomplete: str) -> t.List['CompletionItem']:
        """Return a list of completions for the incomplete value. Looks
        at the names of options and chained multi-commands.

        :param ctx: Invocation context for this command.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        from .shell_completion import CompletionItem

        results = []
        # Add option names
        for param in self.get_params(ctx):
            results.extend(param.shell_complete(ctx, incomplete))
        # Add any subcommands
        if isinstance(self, MultiCommand):
            results.extend(
                CompletionItem(name)
                for name, _ in _complete_visible_commands(self, incomplete)
            )
        # Filter based on incomplete
        return [c for c in results if c.start_with(incomplete)]

class MultiCommand(Command):
    """A multi command is the basic implementation of a command that
    dispatches to subcommands.  The most common version is the
    :class:`Group`.

    :param invoke_without_command: this controls how the multi command itself
                                   is invoked.  By default it's only invoked
                                   if a subcommand is provided.
    :param no_args_is_help: this controls what happens if no arguments are
                            provided.  This option is enabled by default if
                            `invoke_without_command` is disabled or disabled
                            if it's enabled.  If enabled this will add
                            ``--help`` as argument if no arguments are
                            passed.
    :param subcommand_metavar: the string that is used in the documentation
                               to indicate the subcommand place.
    :param chain: if this is set to `True` chaining of multiple subcommands
                  is enabled.  This restricts the form of commands in that
                  they cannot have optional arguments but it allows
                  multiple commands to be chained together.
    :param result_callback: The result callback to attach to this multi
        command. This can be set or changed later with the
        :meth:`result_callback` decorator.
    :param attrs: Other command arguments described in :class:`Command`.
    """
    allow_extra_args = True
    allow_interspersed_args = False

    def __init__(self, name: t.Optional[str]=None, invoke_without_command: bool=False, no_args_is_help: t.Optional[bool]=None, subcommand_metavar: t.Optional[str]=None, chain: bool=False, result_callback: t.Optional[t.Callable[..., t.Any]]=None, **attrs: t.Any) -> None:
        super().__init__(name, **attrs)
        if no_args_is_help is None:
            no_args_is_help = not invoke_without_command
        self.no_args_is_help = no_args_is_help
        self.invoke_without_command = invoke_without_command
        if subcommand_metavar is None:
            if chain:
                subcommand_metavar = 'COMMAND1 [ARGS]... [COMMAND2 [ARGS]...]...'
            else:
                subcommand_metavar = 'COMMAND [ARGS]...'
        self.subcommand_metavar = subcommand_metavar
        self.chain = chain
        self._result_callback = result_callback
        if self.chain:
            for param in self.params:
                if isinstance(param, Argument) and (not param.required):
                    raise RuntimeError('Multi commands in chain mode cannot have optional arguments.')

    def result_callback(self, replace: bool=False) -> t.Callable[[F], F]:
        """Adds a result callback to the command.  By default if a
        result callback is already registered this will chain them but
        this can be disabled with the `replace` parameter.  The result
        callback is invoked with the return value of the subcommand
        (or the list of return values from all subcommands if chaining
        is enabled) as well as the parameters as they would be passed
        to the main callback.

        Example::

            @click.group()
            @click.option('-i', '--input', default=23)
            def cli(input):
                return 42

            @cli.result_callback()
            def process_result(result, input):
                return result + input

        :param replace: if set to `True` an already existing result
                        callback will be removed.

        .. versionchanged:: 8.0
            Renamed from ``resultcallback``.

        .. versionadded:: 3.0
        """
        def decorator(f: F) -> F:
            if replace:
                self._result_callback = f
            else:
                old_callback = self._result_callback
                def new_callback(*args, **kwargs):
                    return f(old_callback(*args, **kwargs), *args, **kwargs)
                self._result_callback = new_callback
            return f
        return decorator

    def format_commands(self, ctx: Context, formatter: HelpFormatter) -> None:
        """Extra format methods for multi methods that adds all the commands
        after the options.
        """
        commands = []
        for subcommand in self.list_commands(ctx):
            cmd = self.get_command(ctx, subcommand)
            # What is this, the tool lied about a command.  Ignore it
            if cmd is None:
                continue
            if cmd.hidden:
                continue

            commands.append((subcommand, cmd))

        # allow for 3 times the default spacing
        if len(commands):
            limit = formatter.width - 6 - max(len(cmd[0]) for cmd in commands)

            rows = []
            for subcommand, cmd in commands:
                help = cmd.get_short_help_str(limit)
                rows.append((subcommand, help))

            if rows:
                with formatter.section('Commands'):
                    formatter.write_dl(rows)

    def get_command(self, ctx: Context, cmd_name: str) -> t.Optional[Command]:
        """Given a context and a command name, this returns a
        :class:`Command` object if it exists or returns `None`.
        """
        raise NotImplementedError()

    def list_commands(self, ctx: Context) -> t.List[str]:
        """Returns a list of subcommand names in the order they should
        appear.
        """
        return []

    def shell_complete(self, ctx: Context, incomplete: str) -> t.List['CompletionItem']:
        """Return a list of completions for the incomplete value. Looks
        at the names of options, subcommands, and chained
        multi-commands.

        :param ctx: Invocation context for this command.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        from .shell_completion import CompletionItem

        results = super().shell_complete(ctx, incomplete)
        results.extend(
            CompletionItem(name)
            for name in self.list_commands(ctx)
            if name.startswith(incomplete)
        )
        return results

class Group(MultiCommand):
    """A group allows a command to have subcommands attached. This is
    the most common way to implement nesting in Click.

    :param name: The name of the group command.
    :param commands: A dict mapping names to :class:`Command` objects.
        Can also be a list of :class:`Command`, which will use
        :attr:`Command.name` to create the dict.
    :param attrs: Other command arguments described in
        :class:`MultiCommand`, :class:`Command`, and
        :class:`BaseCommand`.

    .. versionchanged:: 8.0
        The ``commands`` argument can be a list of command objects.
    """
    command_class: t.Optional[t.Type[Command]] = None
    group_class: t.Optional[t.Union[t.Type['Group'], t.Type[type]]] = None

    def __init__(self, name: t.Optional[str]=None, commands: t.Optional[t.Union[t.MutableMapping[str, Command], t.Sequence[Command]]]=None, **attrs: t.Any) -> None:
        super().__init__(name, **attrs)
        if commands is None:
            commands = {}
        elif isinstance(commands, abc.Sequence):
            commands = {c.name: c for c in commands if c.name is not None}
        self.commands: t.MutableMapping[str, Command] = commands

    def add_command(self, cmd: Command, name: t.Optional[str]=None) -> None:
        """Registers another :class:`Command` with this group.  If the name
        is not provided, the name of the command is used.
        """
        name = name or cmd.name
        if name is None:
            raise TypeError("Command has no name.")
        _check_multicommand(self, name, cmd, register=True)
        self.commands[name] = cmd

    def command(self, *args: t.Any, **kwargs: t.Any) -> t.Union[t.Callable[[t.Callable[..., t.Any]], Command], Command]:
        """A shortcut decorator for declaring and attaching a command to
        the group. This takes the same arguments as :func:`command` and
        immediately registers the created command with this group by
        calling :meth:`add_command`.

        To customize the command class used, set the
        :attr:`command_class` attribute.

        .. versionchanged:: 8.1
            This decorator can be applied without parentheses.

        .. versionchanged:: 8.0
            Added the :attr:`command_class` attribute.
        """
        from .decorators import command

        def decorator(f):
            cmd = command(*args, cls=self.command_class, **kwargs)(f)
            self.add_command(cmd)
            return cmd

        if args and callable(args[0]):
            return decorator(args[0])
        return decorator

    def group(self, *args: t.Any, **kwargs: t.Any) -> t.Union[t.Callable[[t.Callable[..., t.Any]], 'Group'], 'Group']:
        """A shortcut decorator for declaring and attaching a group to
        the group. This takes the same arguments as :func:`group` and
        immediately registers the created group with this group by
        calling :meth:`add_command`.

        To customize the group class used, set the :attr:`group_class`
        attribute.

        .. versionchanged:: 8.1
            This decorator can be applied without parentheses.

        .. versionchanged:: 8.0
            Added the :attr:`group_class` attribute.
        """
        from .decorators import group

        def decorator(f):
            cmd = group(*args, cls=self.group_class, **kwargs)(f)
            self.add_command(cmd)
            return cmd

        if args and callable(args[0]):
            return decorator(args[0])
        return decorator

class CommandCollection(MultiCommand):
    """A command collection is a multi command that merges multiple multi
    commands together into one.  This is a straightforward implementation
    that accepts a list of different multi commands as sources and
    provides all the commands for each of them.

    See :class:`MultiCommand` and :class:`Command` for the description of
    ``name`` and ``attrs``.
    """

    def __init__(self, name: t.Optional[str]=None, sources: t.Optional[t.List[MultiCommand]]=None, **attrs: t.Any) -> None:
        super().__init__(name, **attrs)
        self.sources: t.List[MultiCommand] = sources or []

    def add_source(self, multi_cmd: MultiCommand) -> None:
        """Adds a new multi command to the chain dispatcher."""
        self.sources.append(multi_cmd)

def _check_iter(value: t.Any) -> t.Iterator[t.Any]:
    """Check if the value is iterable but not a string. Raises a type
    error, or return an iterator over the value.
    """
    if isinstance(value, str):
        raise TypeError("expected iterable, not string")
    try:
        return iter(value)
    except TypeError:
        raise TypeError("expected iterable")

class Parameter:
    """A parameter to a command comes in two versions: they are either
    :class:`Option`\\s or :class:`Argument`\\s.  Other subclasses are currently
    not supported by design as some of the internals for parsing are
    intentionally not finalized.

    Some settings are supported by both options and arguments.

    :param param_decls: the parameter declarations for this option or
                        argument.  This is a list of flags or argument
                        names.
    :param type: the type that should be used.  Either a :class:`ParamType`
                 or a Python type.  The latter is converted into the former
                 automatically if supported.
    :param required: controls if this is optional or not.
    :param default: the default value if omitted.  This can also be a callable,
                    in which case it's invoked when the default is needed
                    without any arguments.
    :param callback: A function to further process or validate the value
        after type conversion. It is called as ``f(ctx, param, value)``
        and must return the value. It is called for all sources,
        including prompts.
    :param nargs: the number of arguments to match.  If not ``1`` the return
                  value is a tuple instead of single value.  The default for
                  nargs is ``1`` (except if the type is a tuple, then it's
                  the arity of the tuple). If ``nargs=-1``, all remaining
                  parameters are collected.
    :param metavar: how the value is represented in the help page.
    :param expose_value: if this is `True` then the value is passed onwards
                         to the command callback and stored on the context,
                         otherwise it's skipped.
    :param is_eager: eager values are processed before non eager ones.  This
                     should not be set for arguments or it will inverse the
                     order of processing.
    :param envvar: a string or list of strings that are environment variables
                   that should be checked.
    :param shell_complete: A function that returns custom shell
        completions. Used instead of the param's type completion if
        given. Takes ``ctx, param, incomplete`` and must return a list
        of :class:`~click.shell_completion.CompletionItem` or a list of
        strings.

    .. versionchanged:: 8.0
        ``process_value`` validates required parameters and bounded
        ``nargs``, and invokes the parameter callback before returning
        the value. This allows the callback to validate prompts.
        ``full_process_value`` is removed.

    .. versionchanged:: 8.0
        ``autocompletion`` is renamed to ``shell_complete`` and has new
        semantics described above. The old name is deprecated and will
        be removed in 8.1, until then it will be wrapped to match the
        new requirements.

    .. versionchanged:: 8.0
        For ``multiple=True, nargs>1``, the default must be a list of
        tuples.

    .. versionchanged:: 8.0
        Setting a default is no longer required for ``nargs>1``, it will
        default to ``None``. ``multiple=True`` or ``nargs=-1`` will
        default to ``()``.

    .. versionchanged:: 7.1
        Empty environment variables are ignored rather than taking the
        empty string value. This makes it possible for scripts to clear
        variables if they can't unset them.

    .. versionchanged:: 2.0
        Changed signature for parameter callback to also be passed the
        parameter. The old callback format will still work, but it will
        raise a warning to give you a chance to migrate the code easier.
    """
    param_type_name = 'parameter'

    def __init__(self, param_decls: t.Optional[t.Sequence[str]]=None, type: t.Optional[t.Union[types.ParamType, t.Any]]=None, required: bool=False, default: t.Optional[t.Union[t.Any, t.Callable[[], t.Any]]]=None, callback: t.Optional[t.Callable[[Context, 'Parameter', t.Any], t.Any]]=None, nargs: t.Optional[int]=None, multiple: bool=False, metavar: t.Optional[str]=None, expose_value: bool=True, is_eager: bool=False, envvar: t.Optional[t.Union[str, t.Sequence[str]]]=None, shell_complete: t.Optional[t.Callable[[Context, 'Parameter', str], t.Union[t.List['CompletionItem'], t.List[str]]]]=None) -> None:
        self.name: t.Optional[str]
        self.opts: t.List[str]
        self.secondary_opts: t.List[str]
        self.name, self.opts, self.secondary_opts = self._parse_decls(param_decls or (), expose_value)
        self.type: types.ParamType = types.convert_type(type, default)
        if nargs is None:
            if self.type.is_composite:
                nargs = self.type.arity
            else:
                nargs = 1
        self.required = required
        self.callback = callback
        self.nargs = nargs
        self.multiple = multiple
        self.expose_value = expose_value
        self.default = default
        self.is_eager = is_eager
        self.metavar = metavar
        self.envvar = envvar
        self._custom_shell_complete = shell_complete
        if __debug__:
            if self.type.is_composite and nargs != self.type.arity:
                raise ValueError(f"'nargs' must be {self.type.arity} (or None) for type {self.type!r}, but it was {nargs}.")
            check_default = default if not callable(default) else None
            if check_default is not None:
                if multiple:
                    try:
                        check_default = next(_check_iter(check_default), None)
                    except TypeError:
                        raise ValueError("'default' must be a list when 'multiple' is true.") from None
                if nargs != 1 and check_default is not None:
                    try:
                        _check_iter(check_default)
                    except TypeError:
                        if multiple:
                            message = "'default' must be a list of lists when 'multiple' is true and 'nargs' != 1."
                        else:
                            message = "'default' must be a list when 'nargs' != 1."
                        raise ValueError(message) from None
                    if nargs > 1 and len(check_default) != nargs:
                        subject = 'item length' if multiple else 'length'
                        raise ValueError(f"'default' {subject} must match nargs={nargs}.")

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

    def __repr__(self) -> str:
        return f'<{self.__class__.__name__} {self.name}>'

    @property
    def human_readable_name(self) -> str:
        """Returns the human readable name of this parameter.  This is the
        same as the name for options, but the metavar for arguments.
        """
        return self.name if self.name is not None else self.opts[0] if self.opts else None

    def get_default(self, ctx: Context, call: bool=True) -> t.Optional[t.Union[t.Any, t.Callable[[], t.Any]]]:
        """Get the default for the parameter. Tries
        :meth:`Context.lookup_default` first, then the local default.

        :param ctx: Current context.
        :param call: If the default is a callable, call it. Disable to
            return the callable instead.

        .. versionchanged:: 8.0.2
            Type casting is no longer performed when getting a default.

        .. versionchanged:: 8.0.1
            Type casting can fail in resilient parsing mode. Invalid
            defaults will not prevent showing help text.

        .. versionchanged:: 8.0
            Looks at ``ctx.default_map`` first.

        .. versionchanged:: 8.0
            Added the ``call`` parameter.
        """
        value = ctx.lookup_default(self.name) if ctx is not None else None
        if value is None:
            value = self.default

        if call and callable(value):
            return value()

        return value

    def type_cast_value(self, ctx: Context, value: t.Any) -> t.Any:
        """Convert and validate a value against the option's
        :attr:`type`, :attr:`multiple`, and :attr:`nargs`.
        """
        if value is None:
            return value

        if self.multiple and not isinstance(value, tuple):
            value = (value,)

        if self.nargs != 1:
            return tuple(self.type(ctx, x, self) for x in value)

        return self.type(ctx, value, self)

    def get_error_hint(self, ctx: Context) -> str:
        """Get a stringified version of the param for use in error messages to
        indicate which param caused the error.
        """
        if self.param_type_name == 'option':
            return f"'{self.opts[0]}'"
        else:
            return f"'{self.name}'"

    def shell_complete(self, ctx: Context, incomplete: str) -> t.List['CompletionItem']:
        """Return a list of completions for the incomplete value. If a
        ``shell_complete`` function was given during init, it is used.
        Otherwise, the :attr:`type`
        :meth:`~click.types.ParamType.shell_complete` function is used.

        :param ctx: Invocation context for this command.
        :param incomplete: Value being completed. May be empty.

        .. versionadded:: 8.0
        """
        from .shell_completion import CompletionItem

        if self._custom_shell_complete is not None:
            results = self._custom_shell_complete(ctx, self, incomplete)

            if results and isinstance(results[0], str):
                results = [CompletionItem(c) for c in results]

            return results

        return self.type.shell_complete(ctx, self, incomplete)

class Option(Parameter):
    """Options are usually optional values on the command line and
    have some extra features that arguments don't have.

    All other parameters are passed onwards to the parameter constructor.

    :param show_default: Show the default value for this option in its
        help text. Values are not shown by default, unless
        :attr:`Context.show_default` is ``True``. If this value is a
        string, it shows that string in parentheses instead of the
        actual value. This is particularly useful for dynamic options.
        For single option boolean flags, the default remains hidden if
        its value is ``False``.
    :param show_envvar: Controls if an environment variable should be
        shown on the help page. Normally, environment variables are not
        shown.
    :param prompt: If set to ``True`` or a non empty string then the
        user will be prompted for input. If set to ``True`` the prompt
        will be the option name capitalized.
    :param confirmation_prompt: Prompt a second time to confirm the
        value if it was prompted for. Can be set to a string instead of
        ``True`` to customize the message.
    :param prompt_required: If set to ``False``, the user will be
        prompted for input only when the option was specified as a flag
        without a value.
    :param hide_input: If this is ``True`` then the input on the prompt
        will be hidden from the user. This is useful for password input.
    :param is_flag: forces this option to act as a flag.  The default is
                    auto detection.
    :param flag_value: which value should be used for this flag if it's
                       enabled.  This is set to a boolean automatically if
                       the option string contains a slash to mark two options.
    :param multiple: if this is set to `True` then the argument is accepted
                     multiple times and recorded.  This is similar to ``nargs``
                     in how it works but supports arbitrary number of
                     arguments.
    :param count: this flag makes an option increment an integer.
    :param allow_from_autoenv: if this is enabled then the value of this
                               parameter will be pulled from an environment
                               variable in case a prefix is defined on the
                               context.
    :param help: the help string.
    :param hidden: hide this option from help outputs.
    :param attrs: Other command arguments described in :class:`Parameter`.

    .. versionchanged:: 8.1.0
        Help text indentation is cleaned here instead of only in the
        ``@option`` decorator.

    .. versionchanged:: 8.1.0
        The ``show_default`` parameter overrides
        ``Context.show_default``.

    .. versionchanged:: 8.1.0
        The default of a single option boolean flag is not shown if the
        default value is ``False``.

    .. versionchanged:: 8.0.1
        ``type`` is detected from ``flag_value`` if given.
    """
    param_type_name = 'option'

    def __init__(self, param_decls: t.Optional[t.Sequence[str]]=None, show_default: t.Union[bool, str, None]=None, prompt: t.Union[bool, str]=False, confirmation_prompt: t.Union[bool, str]=False, prompt_required: bool=True, hide_input: bool=False, is_flag: t.Optional[bool]=None, flag_value: t.Optional[t.Any]=None, multiple: bool=False, count: bool=False, allow_from_autoenv: bool=True, type: t.Optional[t.Union[types.ParamType, t.Any]]=None, help: t.Optional[str]=None, hidden: bool=False, show_choices: bool=True, show_envvar: bool=False, **attrs: t.Any) -> None:
        if help:
            help = inspect.cleandoc(help)
        default_is_missing = 'default' not in attrs
        super().__init__(param_decls, type=type, multiple=multiple, **attrs)
        if prompt is True:
            if self.name is None:
                raise TypeError("'name' is required with 'prompt=True'.")
            prompt_text: t.Optional[str] = self.name.replace('_', ' ').capitalize()
        elif prompt is False:
            prompt_text = None
        else:
            prompt_text = prompt
        self.prompt = prompt_text
        self.confirmation_prompt = confirmation_prompt
        self.prompt_required = prompt_required
        self.hide_input = hide_input
        self.hidden = hidden
        self._flag_needs_value = self.prompt is not None and (not self.prompt_required)
        if is_flag is None:
            if flag_value is not None:
                is_flag = True
            elif self._flag_needs_value:
                is_flag = False
            else:
                is_flag = bool(self.secondary_opts)
        elif is_flag is False and (not self._flag_needs_value):
            self._flag_needs_value = flag_value is not None
        self.default: t.Union[t.Any, t.Callable[[], t.Any]]
        if is_flag and default_is_missing and (not self.required):
            if multiple:
                self.default = ()
            else:
                self.default = False
        if flag_value is None:
            flag_value = not self.default
        self.type: types.ParamType
        if is_flag and type is None:
            self.type = types.convert_type(None, flag_value)
        self.is_flag: bool = is_flag
        self.is_bool_flag: bool = is_flag and isinstance(self.type, types.BoolParamType)
        self.flag_value: t.Any = flag_value
        self.count = count
        if count:
            if type is None:
                self.type = types.IntRange(min=0)
            if default_is_missing:
                self.default = 0
        self.allow_from_autoenv = allow_from_autoenv
        self.help = help
        self.show_default = show_default
        self.show_choices = show_choices
        self.show_envvar = show_envvar
        if __debug__:
            if self.nargs == -1:
                raise TypeError('nargs=-1 is not supported for options.')
            if self.prompt and self.is_flag and (not self.is_bool_flag):
                raise TypeError("'prompt' is not valid for non-boolean flag.")
            if not self.is_bool_flag and self.secondary_opts:
                raise TypeError('Secondary flag is not valid for non-boolean flag.')
            if self.is_bool_flag and self.hide_input and (self.prompt is not None):
                raise TypeError("'prompt' with 'hide_input' is not valid for boolean flag.")
            if self.count:
                if self.multiple:
                    raise TypeError("'count' is not valid with 'multiple'.")
                if self.is_flag:
                    raise TypeError("'count' is not valid with 'is_flag'.")

    def prompt_for_value(self, ctx: Context) -> t.Any:
        """This is an alternative flow that can be activated in the full
        value processing if a value does not exist.  It will prompt the
        user until a valid value exists and then returns the processed
        value as result.
        """
        # Calculate the default before prompting anything to be stable.
        default = self.get_default(ctx)

        # If this is a prompt for a flag we need to handle this
        # differently.
        if self.is_bool_flag:
            return confirm(self.prompt, default)

        return prompt(
            self.prompt,
            default=default,
            type=self.type,
            hide_input=self.hide_input,
            show_choices=self.show_choices,
            confirmation_prompt=self.confirmation_prompt,
            value_proc=lambda x: self.process_value(ctx, x),
        )

class Argument(Parameter):
    """Arguments are positional parameters to a command.  They generally
    provide fewer features than options but can have infinite ``nargs``
    and are required by default.

    All parameters are passed onwards to the constructor of :class:`Parameter`.
    """
    param_type_name = 'argument'

    def __init__(self, param_decls: t.Sequence[str], required: t.Optional[bool]=None, **attrs: t.Any) -> None:
        if required is None:
            if attrs.get('default') is not None:
                required = False
            else:
                required = attrs.get('nargs', 1) > 0
        if 'multiple' in attrs:
            raise TypeError("__init__() got an unexpected keyword argument 'multiple'.")
        super().__init__(param_decls, required=required, **attrs)
        if __debug__:
            if self.default is not None and self.nargs == -1:
                raise TypeError("'default' is not supported for nargs=-1.")
