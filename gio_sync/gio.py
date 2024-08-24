import gi
gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio # type: ignore
from gi.repository import GLib # type: ignore

import logging
import urllib.parse

# A combination of the Gio File object with its associated metadata.
FileEntry = tuple[Gio.File, Gio.FileInfo]

DEFAULT_FILE_ATTRIBUTES = {Gio.FILE_ATTRIBUTE_STANDARD_NAME,
                           Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
                           Gio.FILE_ATTRIBUTE_STANDARD_TYPE}


class GioSyncNotFound(RuntimeError):
  pass


def decode_uri(f: Gio.File):
  return urllib.parse.unquote(f)

def file_at(gfile: Gio.File, *paths: str):
  """Construct a new Gio.File having the base URI of 'gfile', and path segments given by 'paths'."""
  return Gio.File.new_for_uri(gfile.get_uri() + '/' + '/'.join([urllib.parse.quote(f) for f in paths]))

def files_and_dirs_get(gfile: Gio.File, exclude: list[Gio.File], attrs_extra: set, case_insensitive_protect: bool) -> (
    tuple[list[FileEntry], list[FileEntry]]
):
  """Get a list of metadata of directories and files at the given path.

  If a path to a file is given, rather than a path to a directory, then just that single file will be returned
  with an empty list of dirs.
  
  :params gfile: A path to a file or directory.
  :params exclude: A list of paths that should be excluded from the results. Checked with Gio.File.equal.
  :params case_insensitive_protect: Throws an error if two files having different cases are found.
  :return: A tuple of FileEntry objects with content [directories, files].
  """

  attrs = DEFAULT_FILE_ATTRIBUTES.union(attrs_extra)
  
  is_dir, gfile_info = test_dir(gfile, attrs)

  if not is_dir:
    return ([(gfile, gfile_info)], [])
    
  files: list[FileEntry] = []
  dirs: list[FileEntry] = []

  for f in gfile.enumerate_children(','.join(attrs), Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS):
    child_file = file_at(gfile, f.get_name())
    if any(child_file.equal(ex) for ex in exclude):
      logging.info("Skipping file: %s", decode_uri(child_file.get_uri()))
      continue

    if f.get_file_type() == Gio.FileType.DIRECTORY:
      dirs.append((child_file, f))
    elif f.get_file_type() == Gio.FileType.REGULAR:
      files.append((child_file, f))

  if case_insensitive_protect:
    fileset = {}
    
    for f, _ in files:
      lower = f.get_basename().lower()
      if lower in fileset:
        fileset[lower].append(f)
      else:
        fileset[lower] = [f]

    dupes = []
    
    for k, v in fileset.items():
      if len(v) > 1:
        dupes.extend(v)
        
    if dupes:
      raise RuntimeError(
        "Some filenames differ only by case. This will cause problems in the destination!\n\n" +
        "Problem files: \n\n" + '\n'.join(f.get_uri() for f in dupes)
      )
      
  return (files, dirs)

def test_dir(path: Gio.File, attrs: set = DEFAULT_FILE_ATTRIBUTES) -> tuple[bool, Gio.FileInfo]:
  """Return (is_dir, info) for the given path, where 'is_dir' is true if it's a directory.
  
  Raise GioSyncNotFound if the path is non-existent.
  """
  try:
    info = path.query_info(','.join(attrs), Gio.FileQueryInfoFlags.NONE)
  except GLib.GError as e:
    if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
      raise GioSyncNotFound("Path not found", path.get_uri())
    else:
      raise

  return info.get_file_type() == Gio.FileType.DIRECTORY, info
  
