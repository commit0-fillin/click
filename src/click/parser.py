"""
This module started out as largely a copy paste from the stdlib's
optparse module with the features removed that we do not need from
optparse because we implement them in Click on a higher level (for
instance type handling, help formatting and a lot more).

The plan is to remove more and more from here over time.

The reason this is a different module and not optparse from the stdlib
is that there are differences in 2.x and 3.x about the error messages
generated and optparse in the stdlib uses gettext for no good reason
and might cause us issues.

Click uses parts of optparse written by Gregory P. Ward and maintained
by the Python Software Foundation. This is limited to code in parser.py.

Copyright 2001-2006 Gregory P. Ward. All rights reserved.
Copyright 2002-2006 Python Software Foundation. All rights reserved.
"""
import typing as t
from collections import deque
from gettext import gettext as _
from gettext import ngettext
from .exceptions import BadArgumentUsage
from .exceptions import BadOptionUsage
from .exceptions import NoSuchOption
from .exceptions import UsageError
if t.TYPE_CHECKING:
    import typing_extensions as te
    from .core import Argument as CoreArgument
    from .core import Context
    from .core import Option as CoreOption
    from .core import Parameter as CoreParameter
V = t.TypeVar('V')
_flag_needs_value = object()

def _unpack_args(args: t.Sequence[str], nargs_spec: t.Sequence[int]) -> t.Tuple[t.Sequence[t.Union[str, t.Sequence[t.Optional[str]], None]], t.List[str]]:
    """Given an iterable of arguments and an iterable of nargs specifications,
    it returns a tuple with all the unpacked arguments at the first index
    and all remaining arguments as the second.

    The nargs specification is the number of arguments that should be consumed
    or `-1` to indicate that this position should eat up all the remainders.

    Missing items are filled with `None`.
    """
    args = list(args)
    nargs_spec = list(nargs_spec)
    rv = []
    spos = 0

    for nargs in nargs_spec:
        if nargs == -1:
            rv.append(tuple(args[spos:]))
            spos = len(args)
        else:
            rv.append(tuple(args[spos:spos + nargs] + [None] * (nargs - len(args[spos:spos + nargs]))))
            spos += nargs

    return (rv, args[spos:])

def split_arg_string(string: str) -> t.List[str]:
    """Split an argument string as with :func:`shlex.split`, but don't
    fail if the string is incomplete. Ignores a missing closing quote or
    incomplete escape sequence and uses the partial token as-is.

    .. code-block:: python

        split_arg_string("example 'my file")
        ["example", "my file"]

        split_arg_string("example my\\")
        ["example", "my"]

    :param string: String to split.
    """
    import shlex
    lex = shlex.shlex(string, posix=True)
    lex.whitespace_split = True
    lex.commenters = ''
    try:
        return list(lex)
    except ValueError:
        return string.split()

class Option:

    def __init__(self, obj: 'CoreOption', opts: t.Sequence[str], dest: t.Optional[str], action: t.Optional[str]=None, nargs: int=1, const: t.Optional[t.Any]=None):
        self._short_opts = []
        self._long_opts = []
        self.prefixes: t.Set[str] = set()
        for opt in opts:
            prefix, value = split_opt(opt)
            if not prefix:
                raise ValueError(f'Invalid start character for option ({opt})')
            self.prefixes.add(prefix[0])
            if len(prefix) == 1 and len(value) == 1:
                self._short_opts.append(opt)
            else:
                self._long_opts.append(opt)
                self.prefixes.add(prefix)
        if action is None:
            action = 'store'
        self.dest = dest
        self.action = action
        self.nargs = nargs
        self.const = const
        self.obj = obj

class Argument:

    def __init__(self, obj: 'CoreArgument', dest: t.Optional[str], nargs: int=1):
        self.dest = dest
        self.nargs = nargs
        self.obj = obj

class ParsingState:

    def __init__(self, rargs: t.List[str]) -> None:
        self.opts: t.Dict[str, t.Any] = {}
        self.largs: t.List[str] = []
        self.rargs = rargs
        self.order: t.List['CoreParameter'] = []

class OptionParser:
    """The option parser is an internal class that is ultimately used to
    parse options and arguments.  It's modelled after optparse and brings
    a similar but vastly simplified API.  It should generally not be used
    directly as the high level Click classes wrap it for you.

    It's not nearly as extensible as optparse or argparse as it does not
    implement features that are implemented on a higher level (such as
    types or defaults).

    :param ctx: optionally the :class:`~click.Context` where this parser
                should go with.
    """

    def __init__(self, ctx: t.Optional['Context']=None) -> None:
        self.ctx = ctx
        self.allow_interspersed_args: bool = True
        self.ignore_unknown_options: bool = False
        if ctx is not None:
            self.allow_interspersed_args = ctx.allow_interspersed_args
            self.ignore_unknown_options = ctx.ignore_unknown_options
        self._short_opt: t.Dict[str, Option] = {}
        self._long_opt: t.Dict[str, Option] = {}
        self._opt_prefixes = {'-', '--'}
        self._args: t.List[Argument] = []

    def add_option(self, obj: 'CoreOption', opts: t.Sequence[str], dest: t.Optional[str], action: t.Optional[str]=None, nargs: int=1, const: t.Optional[t.Any]=None) -> None:
        """Adds a new option named `dest` to the parser.  The destination
        is not inferred (unlike with optparse) and needs to be explicitly
        provided.  Action can be any of ``store``, ``store_const``,
        ``append``, ``append_const`` or ``count``.

        The `obj` can be used to identify the option in the order list
        that is returned from the parser.
        """
        option = Option(obj, opts, dest, action=action, nargs=nargs, const=const)
        self._opt_prefixes.update(option.prefixes)
        for opt in option._short_opts:
            self._short_opt[opt] = option
        for opt in option._long_opts:
            self._long_opt[opt] = option

    def add_argument(self, obj: 'CoreArgument', dest: t.Optional[str], nargs: int=1) -> None:
        """Adds a positional argument named `dest` to the parser.

        The `obj` can be used to identify the option in the order list
        that is returned from the parser.
        """
        self._args.append(Argument(obj, dest, nargs))

    def parse_args(self, args: t.List[str]) -> t.Tuple[t.Dict[str, t.Any], t.List[str], t.List['CoreParameter']]:
        """Parses positional arguments and returns ``(values, args, order)``
        for the parsed options and arguments as well as the leftover
        arguments if there are any.  The order is a list of objects as they
        appear on the command line.  If arguments appear multiple times they
        will be memorized multiple times as well.
        """
        state = ParsingState(args)
        try:
            self._process_args_for_options(state)
            self._process_args_for_args(state)
        except UsageError:
            if self.ctx is None or not self.ctx.resilient_parsing:
                raise
        return state.opts, state.largs, state.order
