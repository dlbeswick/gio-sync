#!/usr/bin/env python3
import time
import urllib.parse

from typing import TypedDict, Dict

import gi

gi.require_version("Gio", "2.0")
from gi.repository import Gio # type: ignore

src_file = Gio.File.new_for_commandline_arg("/home/david/Music/music/")
dst_file = Gio.File.new_for_commandline_arg("mtp://SAMSUNG_SAMSUNG_Android_R58W40VQMAM/SD%20%E2%80%8B%E0%B8%81%E0%B8%B2%E0%B8%A3%E0%B9%8C%E0%B8%94/clone/music/")

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
  changed: set[FileInfoComparable]
  
  def __init__(self, left: FileNameToFileInfoComparable, right: FileNameToFileInfoComparable, is_dir: bool):
    leftfiles = set([f for f in left.values()])
    rightfiles = set([f for f in right.values()])
    self.extra = leftfiles.difference(rightfiles)
    self.missing = rightfiles.difference(leftfiles)

    self.changed = \
      set() if is_dir else \
      {l for l in leftfiles
       if not is_dir and l.info.get_size() != right.get(l.info.get_name(), l).info.get_size()}

    self.same = [(l, right[l.info.get_name()]) for l in leftfiles.difference(self.extra.union(self.changed))]

  def dirty_is(self):
    return self.extra or self.missing or self.changed

  def identical(self):
    return not self.dirty_is
    
  def describe(self):
    print("Extra:")
    print('\n'.join([f.info.get_name() for f in self.extra]))
    print("Missing:")
    print('\n'.join([f.info.get_name() for f in self.missing]))
    print("Changed:")
    print('\n'.join([f.info.get_name() + " " + str(f.info.get_size()) for f in self.changed]))
    print("Same:")
    print('\n'.join([f"{l.info.get_name()} {l.info.get_size()}/{r.info.get_size()}" for l, r in self.same]))

    
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

def show_progress(current_num_bytes, total_num_bytes, user_data: ProgressData):
  tv = time.time()
  if tv - user_data.get('time_previous', tv) < 500: # type: ignore
    return

  rate = current_num_bytes / max((tv - user_data['time_start']) / 1000, 1)
  print("\r\033[K");
  print("Transferred %s out of %s (%s/s)" % (current_num_bytes, total_num_bytes, rate))

  user_data['time_previous'] = tv

def delete_recurse(file: Gio.File, info: Gio.FileInfo):
  if info.get_file_type() == 2:
    for i in file.enumerate_children(
        ','.join([Gio.FILE_ATTRIBUTE_STANDARD_TYPE, Gio.FILE_ATTRIBUTE_STANDARD_NAME]),
        Gio.FileQueryInfoFlags.NONE
    ):
      delete_recurse(file_at(file, i.get_name()), i)

  print(f"{'X/' if info.get_file_type() == 2 else 'X'} {file.get_uri()}")
  file.delete()
  
def copy_file(src_file: Gio.File, dst_path: Gio.File, overwrite: bool):
  dst_file = file_at(dst_path, src_file.get_basename())
  
  print(f"{src_file.get_uri()} {'+>' if overwrite else '->'} {dst_file.get_uri()}")
  
  src_file.copy(
    dst_file,
    Gio.FileCopyFlags.OVERWRITE if overwrite else Gio.FileCopyFlags.NONE,
    None,
    show_progress,
    { 'time_start': time.time() }
  )
    
def diff_recurse(src: Gio.File, dst: Gio.File):
  try:
    info = dst.query_info(','.join(ATTRS), Gio.FileQueryInfoFlags.NONE)
  except:
    print(f"Error at {src.get_uri()} / {dst.get_uri()}")
    raise

  src_files, src_dirs = files_and_dirs_get(src)
  dst_files, dst_dirs = files_and_dirs_get(dst)

  dst_diff_dirs = Diff(file_name_map(dst_dirs), file_name_map(src_dirs), True)
  if dst_diff_dirs.dirty_is():
    for missing in dst_diff_dirs.missing:
      def copy_recurse(src_dir):
        dst_dir = file_at(dst, missing.info.get_name())
        print(f"+/ {dst_dir.get_uri()}")
        dst_dir.make_directory()
        
        files, dirs = files_and_dirs_get(src_dir)
        for f in files:
          copy_file(src_dir, dst_dir, False)

        for d in dirs:
          copy_recurse(d)

      copy_recurse(file_at(src, missing.info.get_name()))
  
    for extra in dst_diff_dirs.extra:
      delete_recurse(extra.file(dst), extra.info)
  
  dst_diff = Diff(file_name_map(dst_files), file_name_map(src_files), False)
  if dst_diff.dirty_is():
    print(src.get_uri())
    print(dst.get_uri())
    dst_diff.describe()

    for missing in dst_diff.missing:
      copy_file(missing.file(src), dst, False)
    
    for changed in dst_diff.changed:
      copy_file(changed.file(src), dst, True)
      
    for extra in dst_diff.extra:
      dst_file = extra.file(dst)
      print(f"X {dst_file.get_uri()}")
      dst_file.delete()
      
  for sd, dd in dst_diff_dirs.same:
    diff_recurse(sd.file(src), dd.file(dst))

  
diff_recurse(src_file, dst_file)

print("Done!")
