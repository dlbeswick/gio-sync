import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio # type: ignore

from .gio import FileEntry
from .matcher import Matcher, MatcherGitignore

import abc


class MatcherFinder(abc.ABC):
  """Given a path and the list of files at that path, return a Matcher that applies to the files, if any.

  If no Matcher applies, then MatcherNever should be returned.

  Some matchers are sensitive to the 'base dir' as an input, such as the .gitignore matcher. In that case, it affects
  how ignore specs starting with a slash are interpreted.
  """

  @abc.abstractmethod
  def matcher_for_path(self, entries: list[FileEntry], base_dir: Gio.File) -> Matcher | None:
    return None


class MatcherFinderGitignore(MatcherFinder):

  def matcher_for_path(self, entries: list[FileEntry], base_dir: Gio.File):
    for file, info in entries:
      if info.get_name() == ".gitignore":
        return MatcherGitignore.from_file(file, base_dir)

    return None
