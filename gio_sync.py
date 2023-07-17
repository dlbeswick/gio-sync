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

parser = argparse.ArgumentParser(
  formatter_class=argparse.RawDescriptionHelpFormatter,
  description=__doc__
)

parser.add_argument('src', type=str, help="Source of syncing operation")
parser.add_argument('dst', type=str, help="Destination of syncing operation")
parser.add_argument('--count', action='store_true')
parser.add_argument('--dry-run',
                    action='store_true',
                    help="Just describe what is to be done, but don't make changes")
parser.add_argument('--verbose', action='store_true')

ATTRS = [Gio.FILE_ATTRIBUTE_STANDARD_NAME,
         Gio.FILE_ATTRIBUTE_STANDARD_DISPLAY_NAME,
         Gio.FILE_ATTRIBUTE_STANDARD_SIZE,
         Gio.FILE_ATTRIBUTE_STANDARD_TYPE]


class FileInfoComparable:
  def __init__(self, info: Gio.FileInfo):
    self.info = info

  def file(self, parent: Gio.File) -> Gio.File:
    return file_at(parent, self.info.get_name())
  
  def __eq__(self, rhs) -> bool:
    return self.info.get_name() == rhs.info.get_name()
  
  def __hash__(self) -> int:
    return hash(self.info.get_name())

FileNameToFileInfoComparable = Dict[str, FileInfoComparable]


class Diff:
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
  return Gio.File.new_for_uri(gfile.get_uri() + '/' + '/'.join([urllib.parse.quote(f) for f in paths]))

def files_and_dirs_get(gfile: Gio.File) -> tuple[list[Gio.FileInfo], list[Gio.FileInfo]]:
  files: list[Gio.FileInfo] = []
  dirs: list[Gio.FileInfo] = []
  
  for f in gfile.enumerate_children(','.join(ATTRS), Gio.FileQueryInfoFlags.NONE):
    if f.get_file_type() == 2: # G_FILE_TYPE_DIRECTORY; where is this constant?
      dirs.append(f)
    else:
      files.append(f)

  return (files, dirs)

def file_name_map(files: list[Gio.File]) -> FileNameToFileInfoComparable:
  return { f.get_name(): FileInfoComparable(f) for f in files }

def progress_copy_show(current_num_bytes, total_num_bytes, user_data: ProgressData):
  tv = time.time()
  if tv - user_data.get('time_previous', tv) < 1: # type: ignore
    return

  rate = current_num_bytes / max((tv - user_data['time_start']) / 1000, 1)
  print("\r\033[K");
  print("Transferred %s out of %s (%s/s)" % (current_num_bytes, total_num_bytes, rate))

  user_data['time_previous'] = tv

def delete_recurse(file: Gio.File, info: Gio.FileInfo, dry_run: bool):
  
  def children_get(file: Gio.File, info: Gio.FileInfo):
    if info.get_file_type() == 2:
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
  
def copy_file(src_file: Gio.File, dst_path: Gio.File, overwrite: bool, dry_run: bool):
  dst_file = file_at(dst_path, src_file.get_basename())
  
  print(f"{src_file.get_uri()} {'+>' if overwrite else '->'} {dst_file.get_uri()}")
  
  if not dry_run:
    src_file.copy(
      dst_file,
      Gio.FileCopyFlags.OVERWRITE if overwrite else Gio.FileCopyFlags.NONE,
      None,
      progress_copy_show,
      { 'time_start': time.time() }
    )
    
def progress_show(num_dirs_remaining: int, num_files_done: int):
  print(f"Progress: {num_dirs_remaining} dirs remaining, synced {num_files_done} files")
  
def diff_recurse(src: Gio.File, dst: Gio.File, verbose: bool, dry_run: bool):
  stack = [(src, dst)]
  done = 0
  progress_last = time.time()

  while stack:
    src, dst = stack.pop()
    src_files, src_dirs = files_and_dirs_get(src)
    dst_files, dst_dirs = files_and_dirs_get(dst)

    if time.time() - progress_last > 1:
      progress_show(len(stack), done)
      progress_last = time.time()
    
    dst_diff_dirs = Diff(file_name_map(dst_dirs), file_name_map(src_dirs), True)
    if dst_diff_dirs.dirty_is():
      for missing in dst_diff_dirs.missing:
        def copy_recurse(src_dir):
          dst_dir = file_at(dst, src_dir.get_name())
          print(f"+/ {dst_dir.get_uri()}")
          dst_dir.make_directory()

          files, dirs = files_and_dirs_get(src_dir)
          for f in files:
            copy_file(file_at(src_dir, f.get_name()), dst_dir, False, dry_run)

          for d in dirs:
            copy_recurse(d)

        copy_recurse(missing.file(src))

      for extra in dst_diff_dirs.extra:
        delete_recurse(extra.file(dst), extra.info, dry_run)

    dst_diff = Diff(file_name_map(dst_files), file_name_map(src_files), False)
    if dst_diff.dirty_is():
      if verbose:
        print(f"Sync required at {dst.get_uri()}")
        dst_diff.describe()
        print()

      for missing in dst_diff.missing:
        copy_file(missing.file(src), dst, False, dry_run)

      for changed_dst, changed_src in dst_diff.changed:
        copy_file(changed_src.file(src), dst, True, dry_run)

      for extra in dst_diff.extra:
        dst_file = extra.file(dst)
        print(f"X {dst_file.get_uri()}")
        if not dry_run:
          dst_file.delete()

    for sd, dd in reversed(dst_diff_dirs.same):
      stack.append((sd.file(src), dd.file(dst)))

    done += len(src_files)
  
  return done

def main():
  args = parser.parse_args()

  src_path = Gio.File.new_for_commandline_arg(args.src)
  dst_path = Gio.File.new_for_commandline_arg(args.dst)

  src_info = src_path.query_info(','.join(ATTRS), Gio.FileQueryInfoFlags.NONE)
  src_is_dir = src_info.get_file_type() == 2
  
  if src_is_dir:
    try:
      dst_path.make_directory_with_parents(None)
    except GLib.GError as e:
      if e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS):
        pass
      else:
        raise RuntimeError("Error creating directory", e.message)
    
  file_at(dst_path)
  
  total = diff_recurse(src_path, dst_path, args.verbose, args.dry_run)

  progress_show(0, total)
  print("Done")
  

if __name__ == '__main__':
  main()
