import py_walk

import abc

import gi
gi.require_version("Gio", "2.0")
from gi.repository import Gio # type: ignore


class Matcher(abc.ABC):
  
  @abc.abstractmethod
  def match_is(self, info: Gio.File):
    return False

  @property
  @abc.abstractmethod
  def description(self) -> str:
    return ""

  
class MatcherGitignore(Matcher):

  @staticmethod
  def from_file(file: Gio.File, base_dir: Gio.File):
    data, error = file.load_bytes()
    return MatcherGitignore(
      py_walk.get_parser_from_text(data.get_data().decode('utf-8'), base_dir.get_path()),
      f".gitignore file at '{file.get_path()}'"
    )

  @staticmethod
  def from_text(text: str, base_dir: Gio.File):
    return MatcherGitignore(
      py_walk.get_parser_from_text(text, base_dir.get_path()),
      f"gitignore spec '{text}'"
    )
  
  def __init__(self, pywalk_parser, desc: str):
    self.pywalk_parser = pywalk_parser
    self.desc = desc
    
  def match_is(self, info: Gio.File):
    return self.pywalk_parser.match(info.get_path())

  @property
  def description(self):
    return self.desc
