#!/usr/bin/env python3
"""Make use of the Gnome Gio library and GVFS to sync file paths.

This program is particularly intended to be useful in syncing across MTP
devices. It can be handy when all other MTP syncing methods, such as using
FUSE filesystems and rsync, don't work for the device.

Please note that only regular files will be considered for transfers. If 'src'
or 'dst' points to a symlink then those links will be followed, however, no
symlinks will be followed in the child hierarchy.

"""

"""
gio-sync: Make use of the Gnome Gio library and GVFS to sync file paths.
Copyright (C) 2023 David Beswick

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""

# Examples of using the GIO library can be seen here:
# https://github.com/GNOME/glib/blob/main/gio/gio-tool-copy.c

import argparse
import logging
import sys
import time
import urllib.parse

from collections import deque
from typing import TypedDict, Dict

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio # type: ignore
from gi.repository import GLib # type: ignore

ATTRS = {Gio.FILE_ATTRIBUTE_STANDARD_NAME,
         Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
         Gio.FILE_ATTRIBUTE_STANDARD_TYPE}

# A combination of the Gio File object with its associated metadata.
FileEntry = tuple[Gio.File, Gio.FileInfo]


class GioSyncNotFound(RuntimeError):
  pass


class FileEntryComparable:
  """Wrapper class around Gio.FileInfo that allows comparison by filename, so it can be easily used in sets,
  etc."""
  
  def __init__(self, entry: FileEntry):
    self.entry = entry

  @property
  def info(self):
    return self.entry[1]
    
  @property
  def file(self):
    return self.entry[0]
  
  def file_at(self, parent: Gio.File) -> Gio.File:
    """Return a Gio.File representing the file location as it would be when having the given parent.
    """
    return file_at(parent, self.entry[0].get_basename())
  
  def __eq__(self, rhs) -> bool:
    return self.info.get_name() == rhs.info.get_name()
  
  def __hash__(self) -> int:
    return hash(self.info.get_name())

FileNameToFileEntryComparable = Dict[str, FileEntryComparable]


class Diff:
  """Given two maps of file names to a list of Gio.FileInfo objects found at a particular path, creates a report
  detailing which files are missing, changed or added in the 'right' path as compared to 'left'.

  :param is_dir: True if the lists consist of only directories, or False if they consist of only files.
  """
  
  changed: set[tuple[FileEntryComparable, FileEntryComparable]]
  
  def __init__(self, left: FileNameToFileEntryComparable, right: FileNameToFileEntryComparable, is_dir: bool,
               size_only: bool):
    
    def sorted_list(x):
      return list(sorted(x, key=lambda i: i.info.get_name()))

    def is_changed(l: FileEntryComparable, r: FileEntryComparable):
      # Most filesystems don't record useful information in directory metadata about whether a change has
      # happened to a file inside the directory.
      if is_dir:
        return False
      
      size_diff = l.info.get_size() != r.info.get_size()

      if size_only:
        time_diff = False
      else:
        time_l = l.info.get_modification_date_time()
        time_r = r.info.get_modification_date_time()
        assert(time_l)
        assert(time_r)

        time_diff = GLib.DateTime.compare(time_l, time_r) > 0
        
      return size_diff or time_diff
    
    leftfiles = set([f for f in left.values()])
    rightfiles = set([f for f in right.values()])
    
    extra = leftfiles.difference(rightfiles)
    self.extra = sorted_list(extra)
    
    self.missing = sorted_list(rightfiles.difference(leftfiles))

    self.changed = set()
    
    # Most filesystems don't record useful information in directory metadata about whether a change has
    # happened to a file inside the directory.
    if not is_dir:
      for l in leftfiles:
        r = right.get(l.info.get_name())
        if r and is_changed(l, r):
          self.changed.add((l, r))

    changed_left = { l[0] for l in self.changed }
    same_files_left = sorted_list(leftfiles.difference(extra.union(changed_left)))
    self.same = [(l, right[l.info.get_name()]) for l in same_files_left]

  def dirty_is(self):
    return self.extra or self.missing or self.changed

  def describe(self):
    lf = '\n'

    def change_info(l, r):
      infos = [f"size {l.info.get_size()} vs. {r.info.get_size()}"]

      if l.info.has_attribute(Gio.FILE_ATTRIBUTE_TIME_MODIFIED):
        infos.append(f"time {GLib.DateTime.format_iso8601(l.info.get_modification_date_time())} vs {GLib.DateTime.format_iso8601(r.info.get_modification_date_time())}")

      return ", ".join(infos)
    
    return f"""Extra:
{lf.join([f.info.get_name() for f in self.extra])}
Missing:
{lf.join([f.info.get_name() for f in self.missing])}
Changed:
{lf.join([f"{l.info.get_name()} ({change_info(l, r)})" for l, r in self.changed])}
Same:
{lf.join([f"{l.info.get_name()} (size {str(l.info.get_size())})" for l, r in self.same])}
    """

 
class ProgressData(TypedDict):
  time_previous: float
  time_start: float
  progress_shown: bool

  
def decode_uri(f: Gio.File):
  return urllib.parse.unquote(f)
  
def file_at(gfile: Gio.File, *paths: str):
  """Construct a new Gio.File having the base URI of 'gfile', and path segments given by 'paths'."""
  return Gio.File.new_for_uri(gfile.get_uri() + '/' + '/'.join([urllib.parse.quote(f) for f in paths]))

def files_and_dirs_get(gfile: Gio.File, exclude: list[Gio.File], attrs_extra: set) -> (
    tuple[list[FileEntry], list[FileEntry]]
):
  """Get a list of metadata of directories and files at the given path.

  If a path to a file is given, rather than a path to a directory, then just that single file will be returned
  with an empty list of dirs.
  
  :params gfile: A path to a file or directory.
  :params exclude: A list of paths that should be excluded from the results. Checked with Gio.File.equal.
  :return: A tuple of FileEntry objects with content [directories, files].
  """

  attrs = ATTRS.union(attrs_extra)
  
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

  return (files, dirs)

def file_name_map(files: list[FileEntry]) -> FileNameToFileEntryComparable:
  """Given a list of Gio.Files, returns a map of the files' names to the FileEntry objects."""
  return { f[1].get_name(): FileEntryComparable(f) for f in files }

def progress_file_copy_show(current_num_bytes, total_num_bytes, user_data: ProgressData):
  """Show the progress of copying a single file."""
  tv = time.time()

  is_final_display = user_data['progress_shown'] and current_num_bytes == total_num_bytes
  
  # Print a progress message each second.
  #
  # If a progress message has ever been shown, then print a final progress message at the final iteration
  # even if time hasn't passed.
  if not is_final_display and tv - user_data['time_previous'] < 1:
    return

  user_data['progress_shown'] = True

  rate = current_num_bytes / max(tv - user_data['time_start'], 1)

  # VT100 control characters.
  # https://vt100.net/docs/vt510-rm/chapter4.html
  #
  # \r = carriage return. Without linefeed, it moves the cursor to beginning of line.
  # \033 = ESC, control code sequence indicator.
  # [K = erase line (search Erase in Line in above docs.)
  sys.stderr.write("\r\033[K")
  sys.stderr.write(
    "Transferred %dM out of %dM (%.2fM/s)" % (
      current_num_bytes/1024/1024, total_num_bytes/1024/1024, rate/1024/1024
    )
  )
  sys.stderr.flush()

  user_data['time_previous'] = tv

def delete_recurse(entry: FileEntry, dry_run: bool):
  """Delete the given file or directory, and all files and directories beneath it if applicable."""
  
  def children_get(file: Gio.File, info: Gio.FileInfo):
    if info.get_file_type() == Gio.FileType.DIRECTORY:
      return list(
        file.enumerate_children(
          ','.join([Gio.FILE_ATTRIBUTE_STANDARD_TYPE, Gio.FILE_ATTRIBUTE_STANDARD_NAME]),
          Gio.FileQueryInfoFlags.NONE
        )
      )
    else:
      return []

  file, info = entry
  
  stack = [(file, info, children_get(file, info))]

  while stack:
    file, info, children = stack.pop()
    
    if children:
      child_info = children.pop()
      
      stack.append((file, info, children))

      child_file = file_at(file, child_info.get_name())
      stack.append((child_file, child_info, children_get(child_file, child_info)))
    else:
      logging.info(
        f"{'X/' if info.get_file_type() == Gio.FileType.DIRECTORY else 'X'} {decode_uri(file.get_uri())}"
      )
      
      if not dry_run:
        file.delete()
  
def copy_file(src_file: Gio.File, dst_file: Gio.File, overwrite: bool, dry_run: bool):
  logging.info(
    f"{decode_uri(src_file.get_uri())} {'+>' if overwrite else '->'} {decode_uri(dst_file.get_uri())}"
  )
  
  if not dry_run:
    data = {
      'time_previous': time.time(),
      'time_start': time.time(),
      'progress_shown': False
    }
    
    src_file.copy(
      dst_file,
      Gio.FileCopyFlags.OVERWRITE if overwrite else Gio.FileCopyFlags.NONE,
      None,
      progress_file_copy_show,
      data
    )

    # After one second, the program will begin to show file copy progress information.
    # VT100 control codes are being used to repeatedly update the progress display on a single line.
    # If this line has been shown, then a final linefeed must be output so that the rest of the text continues
    # on a new line.
    if data['progress_shown']:
      sys.stderr.write('\n')
  
def copy_file_to_dir(src_file: Gio.File, dst_path: Gio.File, overwrite: bool, dry_run: bool):
  """Copy a single file while keeping its filename the same at the destination.

  :param src_file: The path to the file to be copied.
  :param dst_path: The path to the directory where the file should be copied to.
                   Note that the filename of src_file will be appended to this path.
  """
  copy_file(src_file, file_at(dst_path, src_file.get_basename()), overwrite, dry_run)
    
def progress_operation_show(num_dirs_remaining: int, num_files_done: int):
  """Print the known progress of the entire copy operation so far."""
  logging.info(f"Progress: {num_dirs_remaining} dirs remaining, synced {num_files_done} files")

def test_dir(path: Gio.File, attrs: set = ATTRS) -> tuple[bool, Gio.FileInfo]:
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
  
def sync_recurse(src: Gio.File, dst: Gio.File, dry_run: bool, size_only: bool):
  """Syncronize 'src' and 'dst' so that the files at 'dst' match the files at 'src' in content.

  Files and directories present in 'src' but missing from 'dst' will be copied.
  Files and directories missing from 'src' but present in 'dst' will be deleted from 'dst'.

  :params src: May be a directory or file.
  :params dst: Must be a directory.
  :return: The number of files, but not directories, checked during the operation.
  """

  stack = deque([(src, dst)])
  done = 0
  progress_last = time.time()
  dst_original = dst

  attrs = set() if size_only else {Gio.FILE_ATTRIBUTE_TIME_MODIFIED, Gio.FILE_ATTRIBUTE_TIME_MODIFIED_USEC}
  
  while stack:
    src, dst = stack.pop()

    # Note that the original destination path needs to be excluded from syncing operations, to avoid infinite
    # recursion in case the dest directory is nested in the source.
    src_files, src_dirs = files_and_dirs_get(src, [dst_original], attrs)

    try:
      dst_files, dst_dirs = files_and_dirs_get(dst, [], attrs)
      dst_exists = True
    except GioSyncNotFound as e:
      dst_files = []
      dst_dirs = []

      # Any destination directory not found will be created.
      try:
        logging.info(f"+/ {decode_uri(dst.get_uri())}")
        dst.make_directory()
      except GLib.GError as e:
        if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS):
          raise

    if time.time() - progress_last >= 1:
      progress_operation_show(len(stack), done)
      progress_last = time.time()
    
    src_diff_dirs = Diff(file_name_map(src_dirs), file_name_map(dst_dirs), True, size_only)
    src_diff = Diff(file_name_map(src_files), file_name_map(dst_files), False, size_only)

    if any(diff.dirty_is() for diff in [src_diff_dirs, src_diff]):
      logging.debug(f"Sync required at {decode_uri(dst.get_uri())}")
      
    if src_diff_dirs.dirty_is():
      logging.debug(src_diff_dirs.describe() + "\n")
      for extra in src_diff_dirs.extra:
        # A directory exists in 'src' that's not found in 'dst'.
        #
        # Place it on the top of the stack so it's processed first, before descending further into child
        # directories.
        stack.append((extra.file, extra.file_at(dst)))
        
      for missing in src_diff_dirs.missing:
        delete_recurse(missing.entry, dry_run)

    if src_diff.dirty_is():
      logging.debug(src_diff.describe() + "\n")
      for extra in src_diff.extra:
        copy_file_to_dir(extra.file, dst, False, dry_run)

      for changed_src, changed_dst in src_diff.changed:
        copy_file(changed_src.file, changed_dst.file, True, dry_run)

      for missing in src_diff.missing:
        delete_recurse(missing.entry, dry_run)

    # For the directory being examined, any directories missing from 'dst' will be copied in the next iteration,
    # and any extra directories not found in 'src' will have been deleted already.
    #
    # Now, add all the directories in common to the bottom of the stack, so that the above next iteration will
    # be processing the missing directories only. Finally, these additional directories will be processed in
    # alphabetical order because stack entries are popped off the top.
    stack.extendleft((sd.file, dd.file) for sd, dd in src_diff_dirs.same)

    done += len(src_files)
  
  return done

def list_recurse(path: Gio.File):
  stack = [path]

  while stack:
    path = stack.pop()
    
    try:
      files, dirs = files_and_dirs_get(path, [], set())
    except GioSyncNotFound:
      logging.warning("File disappeared: '%s'", decode_uri(path.get_uri()))
      continue
    except GLib.GError as e:
      logging.warning("%s", e)
      continue
      
    for file, info in files:
      print(decode_uri(file.get_uri()))

    stack.extend(reversed([d for d, _ in dirs]))

def main():
  parser = argparse.ArgumentParser(
    formatter_class=argparse.RawDescriptionHelpFormatter,
    description=__doc__
  )

  parser.add_argument('src',
                      type=str,
                      help="GVfs URI of source of sync operation. May point to a file or a directory.")
  parser.add_argument('dst',
                      type=str,
                      nargs='?',
                      help="GVfs URI of destination directory of sync operation. " +
                           "If not given, then list source files to stdout.")
  parser.add_argument('--size-only',
                      action='store_true',
                      help="Only consider file size when detecting changes. Useful when copying from an MTP "+
                           "source to a non-MTP destination.")
  parser.add_argument('--dry-run',
                      action='store_true',
                      help="Just describe what would be done, but don't make changes")
  parser.add_argument('--verbose', action='store_true')

  args = parser.parse_args()

  logging.basicConfig(
    format='%(message)s',
    level=logging.DEBUG if args.verbose else logging.INFO
  )
  
  src_path = Gio.File.new_for_commandline_arg(args.src)

  if args.dst:
    dst_path = Gio.File.new_for_commandline_arg(args.dst)

    total = sync_recurse(src_path, dst_path, args.dry_run, args.size_only)
    
    progress_operation_show(0, total)
    logging.info("Done")
  else:
    list_recurse(src_path)
  

if __name__ == '__main__':
  main()
