#!/usr/bin/env python3
"""Make use of the Gnome Gio library and GVFS to sync file paths.

Note that only file size is used to determine if a file has changed. This is due
to the program's focus on MTP support and MTP's limitations around changing
timestamps.

This program is particularly intended to be useful in syncing across MTP
devices, which can be handy when all other MTP syncing methods fail. Samsung
Android phones are particularly problematic when it comes to MTP, but the GVFS
MTP implementation seems to handle it well.

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
import time
import urllib.parse

from typing import TypedDict, Dict

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio # type: ignore
from gi.repository import GLib # type: ignore

# tbd:  G_FILE_TYPE_DIRECTORY; where is this constant in the gi repositoryLib?
G_FILE_TYPE_DIRECTORY = 2

parser = argparse.ArgumentParser(
  formatter_class=argparse.RawDescriptionHelpFormatter,
  description=__doc__
)

parser.add_argument('src', type=str, help="GVFS URI of source of sync operation. May point to a file or a directory.")
parser.add_argument('dst', type=str, help="GVFS URI of destination directory of syncing operation.")
parser.add_argument('--dry-run',
                    action='store_true',
                    help="Just describe what would be done, but don't make changes")
parser.add_argument('--verbose', action='store_true')

ATTRS = [Gio.FILE_ATTRIBUTE_STANDARD_NAME,
         Gio.FILE_ATTRIBUTE_STANDARD_DISPLAY_NAME,
         Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
         Gio.FILE_ATTRIBUTE_STANDARD_TYPE]


class GioSyncNotFound(RuntimeError):
  pass


class FileInfoComparable:
  """Wrapper class around Gio.FileInfo that allows comparison by filename, so it can be easily used in sets,
  etc."""
  
  def __init__(self, info: Gio.FileInfo):
    self.info = info
    assert self.info.get_name(), "FileInfo not created with correct attribute request list"

  def file(self, parent: Gio.File) -> Gio.File:
    """Return a Gio.File representing the file location as it would be when having the given parent.

    I.e. parent.get_uri() + self.info.get_name()
    """
    return file_at(parent, self.info.get_name())
  
  def __eq__(self, rhs) -> bool:
    return self.info.get_name() == rhs.info.get_name()
  
  def __hash__(self) -> int:
    return hash(self.info.get_name())

FileNameToFileInfoComparable = Dict[str, FileInfoComparable]


class Diff:
  """Given two maps of file names to a list of Gio.FileInfo objects found at a particular path, creates a report
  detailing which files are missing, changed or added in the 'right' path as compared to 'left'."""
  
  changed: set[tuple[FileInfoComparable, FileInfoComparable]]
  
  def __init__(self, left: FileNameToFileInfoComparable, right: FileNameToFileInfoComparable, is_dir: bool):
    def sorted_list(x):
      return list(sorted(x, key=lambda i: i.info.get_name()))
    
    leftfiles = set([f for f in left.values()])
    rightfiles = set([f for f in right.values()])
    
    extra = leftfiles.difference(rightfiles)
    self.extra = sorted_list(extra)
    
    self.missing = sorted_list(rightfiles.difference(leftfiles))

    self.changed = \
      set() if is_dir else \
      {(l, right[l.info.get_name()]) for l in leftfiles
       if not is_dir and l.info.get_size() != right.get(l.info.get_name(), l).info.get_size()}

    changed_left = { l[0] for l in self.changed }
    same_files_left = sorted_list(leftfiles.difference(extra.union(changed_left)))
    self.same = [(l, right[l.info.get_name()]) for l in same_files_left]

  def dirty_is(self):
    return self.extra or self.missing or self.changed

  def describe(self):
    print("Extra:")
    print('\n'.join([f.info.get_name() for f in self.extra]))
    print("Missing:")
    print('\n'.join([f.info.get_name() for f in self.missing]))
    print("Changed:")
    print('\n'.join([f"{l.info.get_name()} (size {l.info.get_size()} vs. {r.info.get_size()})"
                     for l, r in self.changed]))
    print("Same:")
    print('\n'.join([f"{l.info.get_name()} (size {str(l.info.get_size())})" for l, r in self.same]))

  def identical(self):
    return not self.dirty_is

 
class ProgressData(TypedDict):
  time_previous: float|None
  time_start: float

  
def file_at(gfile: Gio.File, *paths: str):
  """Construct a new Gio.File having the base URI of 'gfile', and path segments given by 'paths'."""
  return Gio.File.new_for_uri(gfile.get_uri() + '/' + '/'.join([urllib.parse.quote(f) for f in paths]))

def files_and_dirs_get(gfile: Gio.File) -> tuple[list[Gio.FileInfo], list[Gio.FileInfo]]:
  """Get a list of metadata of directories and files at the given path.

  If a path to a is given, rather than a path to a directory, then just that single file will be returned with
  an empty list of dirs.
  
  :params gfile: A path to a file or directory.
  :return: A tuple of [directories, files]
  """

  is_dir, gfile_info = test_dir(gfile)

  if not is_dir:
    return ([gfile_info], [])
    
  files: list[Gio.FileInfo] = []
  dirs: list[Gio.FileInfo] = []
  
  for f in gfile.enumerate_children(','.join(ATTRS), Gio.FileQueryInfoFlags.NONE):
    if f.get_file_type() == G_FILE_TYPE_DIRECTORY:
      dirs.append(f)
    else:
      files.append(f)

  return (files, dirs)

def file_name_map(files: list[Gio.File]) -> FileNameToFileInfoComparable:
  """Given a list of Gio.Files, returns a map of the files' names to the Gio.File objects."""
  return { f.get_name(): FileInfoComparable(f) for f in files }

def progress_file_copy_show(current_num_bytes, total_num_bytes, user_data: ProgressData):
  """Show the progress of copying a single file."""
  tv = time.time()
  if tv - user_data.get('time_previous', tv) < 1: # type: ignore
    return

  rate = current_num_bytes / max((tv - user_data['time_start']) / 1000, 1)
  print("\r\033[K");
  print("Transferred %s out of %s (%s/s)" % (current_num_bytes, total_num_bytes, rate))

  user_data['time_previous'] = tv

def delete_recurse(file: Gio.File, info: Gio.FileInfo, dry_run: bool):
  """Delete the given file or directory, and all files and directories beneath it if applicable."""
  
  def children_get(file: Gio.File, info: Gio.FileInfo):
    if info.get_file_type() == G_FILE_TYPE_DIRECTORY:
      return list(
        file.enumerate_children(
          ','.join([Gio.FILE_ATTRIBUTE_STANDARD_TYPE, Gio.FILE_ATTRIBUTE_STANDARD_NAME]),
          Gio.FileQueryInfoFlags.NONE
        )
      )
    else:
      return []
    
  stack = [(file, info, children_get(file, info))]

  while stack:
    file, info, children = stack.pop()
    
    if children:
      child_info = children.pop()
      
      stack.append((file, info, children))

      child_file = file_at(file, child_info.get_name())
      stack.append((child_file, child_info, children_get(child_file, child_info)))
    else:
      print(f"{'X/' if info.get_file_type() == 2 else 'X'} {file.get_uri()}")
      if not dry_run:
        file.delete()
  
def copy_file_to_dir(src_file: Gio.File, dst_path: Gio.File, overwrite: bool, dry_run: bool):
  """Copy a single file while keeping its filename the same at the destination.

  :param src_file: The path to the file to be copied.
  :param dst_path: The path to the directory where the file should be copied to.
                   Note that the filename of src_file will be appended to this path.
  """
  dst_file = file_at(dst_path, src_file.get_basename())
  
  print(f"{src_file.get_uri()} {'+>' if overwrite else '->'} {dst_file.get_uri()}")
  
  if not dry_run:
    src_file.copy(
      dst_file,
      Gio.FileCopyFlags.OVERWRITE if overwrite else Gio.FileCopyFlags.NONE,
      None,
      progress_file_copy_show,
      { 'time_start': time.time() }
    )
    
def copy_dir_recurse(src_dir: Gio.File, dst: Gio.File, dry_run: bool):
  """Copies an entire directory and its subtree to the destination directory. The destination must not exist.

  Directories are created as required.
  
  :param src_dir: Must be a directory.
  :param dst_dir: Must be a path to a non-existent directory.
  """
  stack = [(src_dir, dst)]

  while stack:
    src_dir, dst = stack.pop()
    
    dst_dir = file_at(dst, src_dir.get_basename())
    print(f"+/ {dst_dir.get_uri()}")
    dst_dir.make_directory()

    files, dirs = files_and_dirs_get(src_dir)
    for f in files:
      copy_file_to_dir(file_at(src_dir, f.get_name()), dst_dir, False, dry_run)

    stack += [(file_at(src_dir, d.get_name()), dst_dir) for d in dirs]
            
def progress_operation_show(num_dirs_remaining: int, num_files_done: int):
  """Print the known progress of the entire copy operation so far."""
  print(f"Progress: {num_dirs_remaining} dirs remaining, synced {num_files_done} files")

def test_dir(path: Gio.File) -> tuple[bool, Gio.FileInfo]:
  """Return (is_dir, info) for the given path, where 'is_dir' is true if it's a directory.
  
  Raise GioSyncNotFound if the path is non-existent.
  """
  try:
    info = path.query_info(','.join(ATTRS), Gio.FileQueryInfoFlags.NONE)
  except GLib.GError as e:
    if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND):
      raise GioSyncNotFound("Path not found", path.get_uri())
    else:
      raise

  return info.get_file_type() == G_FILE_TYPE_DIRECTORY, info
  
def sync_recurse(src: Gio.File, dst: Gio.File, verbose: bool, dry_run: bool):
  """Syncronize 'src' and 'dst' so that the files at 'dst' match the files at 'src' in content.

  Files and directories present in 'src' but missing from 'dst' will be copied.
  Files and directories missing from 'src' but present in 'dst' will be deleted from 'dst'.

  :params src: May be a directory or file.
  :params dst: Must be a directory.
  :return: The number of files, but not directories, checked during the operation.
  """

  stack = [(src, dst)]
  done = 0
  progress_last = time.time()

  while stack:
    src, dst = stack.pop()
    src_files, src_dirs = files_and_dirs_get(src)

    # 'src' may point to either a directory or a file.
    # When it comes time to copy any files, 'src_root' will be used to properly form the source filename in
    # both of these cases. The filename of the source file to be copied will be appended to this root.
    src_is_dir, _ = test_dir(src)
    src_copy_root = src if src_is_dir else src.get_parent()
    
    try:
      dst_files, dst_dirs = files_and_dirs_get(dst)
    except GioSyncNotFound as e:
      dst_files = []
      dst_dirs = []

    if time.time() - progress_last > 1:
      progress_operation_show(len(stack), done)
      progress_last = time.time()
    
    dst_diff_dirs = Diff(file_name_map(dst_dirs), file_name_map(src_dirs), True)
    if dst_diff_dirs.dirty_is():
      for missing in dst_diff_dirs.missing:
        copy_dir_recurse(missing.file(src), dst, dry_run)

      for extra in dst_diff_dirs.extra:
        delete_recurse(extra.file(dst), extra.info, dry_run)

    dst_diff = Diff(file_name_map(dst_files), file_name_map(src_files), False)
    if dst_diff.dirty_is():
      if verbose:
        print(f"Sync required at {dst.get_uri()}")
        dst_diff.describe()
        print()

      for missing in dst_diff.missing:
        copy_file_to_dir(missing.file(src_copy_root), dst, False, dry_run)

      for changed_dst, changed_src in dst_diff.changed:
        copy_file_to_dir(changed_src.file(src_copy_root), dst, True, dry_run)

      for extra in dst_diff.extra:
        dst_file = extra.file(dst)
        delete_recurse(dst_file, extra.info, dry_run)

    for sd, dd in reversed(dst_diff_dirs.same):
      stack.append((sd.file(src), dd.file(dst)))

    done += len(src_files)
  
  return done

def main():
  args = parser.parse_args()

  src_path = Gio.File.new_for_commandline_arg(args.src)
  dst_path = Gio.File.new_for_commandline_arg(args.dst)

  total = sync_recurse(src_path, dst_path, args.verbose, args.dry_run)

  progress_operation_show(0, total)
  print("Done")
  

if __name__ == '__main__':
  main()
