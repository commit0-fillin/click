import typing as t
from contextlib import contextmanager
from gettext import gettext as _
from ._compat import term_len
from .parser import split_opt
FORCED_WIDTH: t.Optional[int] = None

def wrap_text(text: str, width: int=78, initial_indent: str='', subsequent_indent: str='', preserve_paragraphs: bool=False) -> str:
    """A helper function that intelligently wraps text.  By default, it
    assumes that it operates on a single paragraph of text but if the
    `preserve_paragraphs` parameter is provided it will intelligently
    handle paragraphs (defined by two empty lines).

    If paragraphs are handled, a paragraph can be prefixed with an empty
    line containing the ``\\b`` character (``\\x08``) to indicate that
    no rewrapping should happen in that block.

    :param text: the text that should be rewrapped.
    :param width: the maximum width for the text.
    :param initial_indent: the initial indent that should be placed on the
                           first line as a string.
    :param subsequent_indent: the indent string that should be placed on
                              each consecutive line.
    :param preserve_paragraphs: if this flag is set then the wrapping will
                                intelligently handle paragraphs.
    """
    from ._textwrap import TextWrapper
    wrapper = TextWrapper(width=width, initial_indent=initial_indent,
                          subsequent_indent=subsequent_indent,
                          replace_whitespace=False)
    
    if not preserve_paragraphs:
        return wrapper.fill(text)
    
    p = []
    buf = []
    indent = None

    def _flush_par():
        if not buf:
            return
        if buf[0].strip() == '\b':
            p.append('\n'.join(buf[1:]))
        else:
            p.append(wrapper.fill('\n'.join(buf)))

    for line in text.splitlines():
        if not line.strip():
            if indent is None:
                _flush_par()
                indent = len(line) - len(line.lstrip())
            elif len(line) - len(line.lstrip()) <= indent:
                _flush_par()
            buf.append(line)
        else:
            buf.append(line)
    _flush_par()
    return '\n\n'.join(p)

class HelpFormatter:
    """This class helps with formatting text-based help pages.  It's
    usually just needed for very special internal cases, but it's also
    exposed so that developers can write their own fancy outputs.

    At present, it always writes into memory.

    :param indent_increment: the additional increment for each level.
    :param width: the width for the text.  This defaults to the terminal
                  width clamped to a maximum of 78.
    """

    def __init__(self, indent_increment: int=2, width: t.Optional[int]=None, max_width: t.Optional[int]=None) -> None:
        import shutil
        self.indent_increment = indent_increment
        if max_width is None:
            max_width = 80
        if width is None:
            width = FORCED_WIDTH
            if width is None:
                width = max(min(shutil.get_terminal_size().columns, max_width) - 2, 50)
        self.width = width
        self.current_indent = 0
        self.buffer: t.List[str] = []

    def write(self, string: str) -> None:
        """Writes a unicode string into the internal buffer."""
        self.buffer.append(string)

    def indent(self) -> None:
        """Increases the indentation."""
        self.current_indent += self.indent_increment

    def dedent(self) -> None:
        """Decreases the indentation."""
        self.current_indent = max(self.current_indent - self.indent_increment, 0)

    def write_usage(self, prog: str, args: str='', prefix: t.Optional[str]=None) -> None:
        """Writes a usage line into the buffer.

        :param prog: the program name.
        :param args: whitespace separated list of arguments.
        :param prefix: The prefix for the first line. Defaults to
            ``"Usage: "``.
        """
        if prefix is None:
            prefix = _("Usage: ")

        usage = f"{prog} {args}".rstrip()
        self.write(f"{prefix}{usage}\n")

    def write_heading(self, heading: str) -> None:
        """Writes a heading into the buffer."""
        self.write(f"\n{heading}:\n")

    def write_paragraph(self) -> None:
        """Writes a paragraph into the buffer."""
        if self.buffer and self.buffer[-1] != '\n':
            self.write('\n')

    def write_text(self, text: str) -> None:
        """Writes re-indented text into the buffer.  This rewraps and
        preserves paragraphs.
        """
        indent = ' ' * self.current_indent
        text_width = self.width - self.current_indent

        wrapped_text = wrap_text(text, text_width, initial_indent=indent,
                                 subsequent_indent=indent, preserve_paragraphs=True)
        self.write(wrapped_text)
        self.write('\n')

    def write_dl(self, rows: t.Sequence[t.Tuple[str, str]], col_max: int=30, col_spacing: int=2) -> None:
        """Writes a definition list into the buffer.  This is how options
        and commands are usually formatted.

        :param rows: a list of two item tuples for the terms and values.
        :param col_max: the maximum width of the first column.
        :param col_spacing: the number of spaces between the first and
                            second column.
        """
        rows = list(rows)
        if not rows:
            return

        first_col = [term for term, value in rows]
        if not first_col:
            return

        second_col = [value for term, value in rows]

        # Compute maximum width for first column
        first_col_width = min(max(len(term) for term in first_col), col_max)

        # Compute maximum width for second column
        second_col_width = self.width - first_col_width - col_spacing

        for first, second in zip(first_col, second_col):
            self.write('  ')
            self.write(f"{first:<{first_col_width}}")
            if not second:
                self.write('\n')
                continue
            self.write(' ' * col_spacing)
            
            wrapped_second = wrap_text(second, width=second_col_width)
            lines = wrapped_second.splitlines()
            
            if lines:
                self.write(lines[0] + '\n')
                for line in lines[1:]:
                    self.write(' ' * (first_col_width + col_spacing + 2))
                    self.write(line + '\n')
            else:
                self.write('\n')

    @contextmanager
    def section(self, name: str) -> t.Iterator[None]:
        """Helpful context manager that writes a paragraph, a heading,
        and the indents.

        :param name: the section name that is written as heading.
        """
        self.write_paragraph()
        self.write_heading(name)
        self.indent()
        try:
            yield
        finally:
            self.dedent()

    @contextmanager
    def indentation(self) -> t.Iterator[None]:
        """A context manager that increases the indentation."""
        self.indent()
        try:
            yield
        finally:
            self.dedent()

    def getvalue(self) -> str:
        """Returns the buffer contents."""
        return "".join(self.buffer)

def join_options(options: t.Sequence[str]) -> t.Tuple[str, bool]:
    """Given a list of option strings this joins them in the most appropriate
    way and returns them in the form ``(formatted_string,
    any_prefix_is_slash)`` where the second item in the tuple is a flag that
    indicates if any of the option prefixes was a slash.
    """
    from .parser import split_opt
    
    rv = []
    any_prefix_is_slash = False
    for opt in options:
        prefix, _ = split_opt(opt)
        if prefix == '/':
            any_prefix_is_slash = True
        rv.append((prefix and '/' in prefix) and opt or opt.replace('/', '-'))

    return ', '.join(rv), any_prefix_is_slash
