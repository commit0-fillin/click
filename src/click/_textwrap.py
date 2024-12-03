import textwrap
import typing as t
from contextlib import contextmanager

class TextWrapper(textwrap.TextWrapper):
    def __init__(self, *args: t.Any, **kwargs: t.Any) -> None:
        super().__init__(*args, **kwargs)
        self._char_escapes: t.Dict[str, str] = {}

    def wrap(self, text: str) -> t.List[str]:
        split_text = self._split_chunks(self._pre_process_text(text))
        lines = []

        for chunk in split_text:
            if len(chunk) <= self.width:
                lines.append(chunk)
            else:
                lines.extend(self._wrap_chunk(chunk))

        return lines

    def _pre_process_text(self, text: str) -> str:
        for char, escape in self._char_escapes.items():
            text = text.replace(char, escape)
        return text

    def _split_chunks(self, text: str) -> t.List[str]:
        return text.splitlines()

    def _wrap_chunk(self, chunk: str) -> t.List[str]:
        return textwrap.wrap(chunk, width=self.width, 
                             break_long_words=self.break_long_words, 
                             replace_whitespace=self.replace_whitespace)

    @contextmanager
    def extra_indent(self, indent: str) -> t.Generator[None, None, None]:
        original_subsequent_indent = self.subsequent_indent
        self.subsequent_indent = self.subsequent_indent + indent
        try:
            yield
        finally:
            self.subsequent_indent = original_subsequent_indent

    def escape_char(self, char: str, escape: str) -> None:
        self._char_escapes[char] = escape
