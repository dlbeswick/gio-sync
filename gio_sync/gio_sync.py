#!/usr/bin/env python3
"""Make use of the Gnome Gio library and GVFS to sync file paths.

This program is particularly intended to be useful in syncing across MTP
devices. It can be handy when all other MTP syncing methods, such as using
FUSE filesystems and rsync, don't work for the device.

Please note that only regular files will be considered for transfers. If 'src'
or 'dst' points to a symlink then those links will be followed, however, no
symlinks will be followed in the child hierarchy.


NOTE ON IGNORING FILES

Ignored files that are already synced are left alone and not deleted from
destination.

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

from .gio import decode_uri, file_at, files_and_dirs_get, FileEntry, GioSyncNotFound
from .diff import Diff
from .matcher import Matcher, MatcherGitignore
from .matcher_finder import MatcherFinder, MatcherFinderGitignore

import argparse
import logging
import sys
import time

from collections import deque
from typing import TypedDict

# Examples of using the GIO library can be seen here:
# https://github.com/GNOME/glib/blob/main/gio/gio-tool-copy.c
import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio # type: ignore
from gi.repository import GLib # type: ignore


class ProgressData(TypedDict):
  time_previous: float
  time_start: float
  progress_shown: bool

  
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

def sync_recurse(src: Gio.File, dst: Gio.File, dry_run: bool, size_only: bool, case_insensitive_protect: bool,
                 exclude_src: list[Matcher],
                 exclude_dst: list[Matcher],
                 exclude_matcher_finders: list[MatcherFinder]):
  
  """Syncronize 'src' and 'dst' so that the files at 'dst' match the files at 'src' in content.

  Files and directories present in 'src' but missing from 'dst' will be copied.
  Files and directories missing from 'src' but present in 'dst' will be deleted from 'dst'.

  :params src: May be a directory or file.
  :params dst: Must be a directory.
  :return: The number of files, but not directories, checked during the operation.
  """

  stack: deque[tuple[Gio.File, Gio.File, list[Matcher], list[Matcher]]] \
    = deque([(src, dst, exclude_src, exclude_dst)])
  
  done = 0
  progress_last = time.time()
  dst_original = dst

  attrs = set() if size_only else {Gio.FILE_ATTRIBUTE_TIME_MODIFIED, Gio.FILE_ATTRIBUTE_TIME_MODIFIED_USEC}
  
  while stack:
    src, dst, exclude_src, exclude_dst = stack.pop()

    # Note that the original destination path needs to be excluded from syncing operations, to avoid infinite
    # recursion in case the dest directory is nested in the source.
    src_files, src_dirs = files_and_dirs_get(src, [dst_original], attrs, case_insensitive_protect)

    try:
      dst_files, dst_dirs = files_and_dirs_get(dst, [], attrs, case_insensitive_protect)
    except GioSyncNotFound as e:
      dst_files = []
      dst_dirs = []

      # Any destination directory not found will be created.
      try:
        logging.info(f"+/ {decode_uri(dst.get_uri())}")
        if not dry_run:
          dst.make_directory()
      except GLib.GError as e:
        if not e.matches(Gio.io_error_quark(), Gio.IOErrorEnum.EXISTS):
          raise

    if time.time() - progress_last >= 1:
      progress_operation_show(len(stack), done)
      progress_last = time.time()
    
    # See if there are any more matchers that should be extracted from this set of files, such as looking for a
    # '.gitignore' file.
    #
    # Note that any discovered Matchers will also be applied to subdirectories.
    # This is the current behaviour but may change in future or per-Matcher.
    def get_exclude_matchers_from_finders(entries: list[FileEntry], path: Gio.File):
      result_with_none = [f.matcher_for_path(entries, path) for f in exclude_matcher_finders]
      return [m for m in result_with_none if m]

    new_src_matchers = get_exclude_matchers_from_finders(src_files, src)
    if new_src_matchers:
      logging.debug("Discovered a new set of exclusions: %s",
                    ','.join([m.description for m in new_src_matchers]))
      
    exclude_final_src = exclude_src + new_src_matchers

    # Note that 'src_files' is used as input here, as only .gitignore files (for example) on the source side are
    # checked. However, a set of "right-side"/destination matchers should also be generated (with 'dst' as the base
    # dir in that case) so that files can be properly excluded from the right according to these rules.
    #
    # The Matchers for both src and dst are basically created twice from the same set of files, but this is ok to keep
    # the code complexity down.
    exclude_final_dst = \
      exclude_dst + get_exclude_matchers_from_finders(src_files, src)

    src_diff_dirs = Diff(src_dirs, dst_dirs, True, size_only, exclude_final_src, exclude_final_dst)
    src_diff = Diff(src_files, dst_files, False, size_only, exclude_final_src, exclude_final_dst)

    if any(diff.dirty_is() for diff in [src_diff_dirs, src_diff]):
      logging.debug("Sync required at %s", decode_uri(dst.get_uri()))
      logging.debug("Active exclusions:\n  %s", '\n  '.join(m.description for m in exclude_final_src))

    if src_diff_dirs.dirty_is() or src_diff_dirs.excluded_is:
      logging.debug("Diff report for %s", decode_uri(dst.get_uri()))
      logging.debug(src_diff_dirs.describe() + "\n")

    if src_diff_dirs.dirty_is():
      for extra in src_diff_dirs.extra:
        # A directory exists in 'src' that's not found in 'dst'.
        #
        # Place it on the top of the stack so it's processed first, before descending further into child
        # directories.
        stack.append((extra.file, extra.file_at(dst), exclude_final_src, exclude_final_dst))
        
      for missing in src_diff_dirs.missing:
        delete_recurse(missing.entry, dry_run)

    if src_diff.dirty_is() or src_diff.excluded_is:
      logging.debug(src_diff.describe() + "\n")
      
    if src_diff.dirty_is():
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
    stack.extendleft((sd.file, dd.file, exclude_final_src, exclude_final_dst) for sd, dd in src_diff_dirs.same)

    done += len(src_files)
  
  return done

def list_recurse(path: Gio.File):
  stack = [path]

  while stack:
    path = stack.pop()
    
    try:
      files, dirs = files_and_dirs_get(path, [], set(), False)
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
                      help="GVfs URI of source of sync operation. May point to a file or a directory.")
  parser.add_argument('dst',
                      nargs='?',
                      help="GVfs URI of destination directory of sync operation. " +
                           "If not given, then list source files to stdout.")
  parser.add_argument('--size-only',
                      action='store_true',
                      help="Only consider file size when detecting changes. Useful when copying from an MTP " +
                           "source to a non-MTP destination.")
  parser.add_argument('--dry-run',
                      action='store_true',
                      help="Just describe what would be done, but don't make changes")
  parser.add_argument('--case-insensitive-protect',
                      action='store_true',
                      help="If one filesystem is case insensitive and the other is not, errors can occur during "
                           "file operations. This option will cause an error to be raised if a potential problem "
                           "is found. MTP is protected by default.")
  parser.add_argument('--no-case-insensitive-protect',
                      action='store_true',
                      help="Ignores errors that can occur when trying to create two files having different cases "
                           "on case insensitive file systems.")
  parser.add_argument('--ignore',
                      nargs='*',
                      default=[],
                      help="A '.gitignore' format ignore spec that's used to exclude files from syncing.")
  parser.add_argument('--ignore-file',
                      nargs='*',
                      default=[],
                      help="GVfs URI of a '.gitignore' file that's used to exclude files from syncing.")
  parser.add_argument('--gitignore',
                      action='store_true',
                      help="If given, then any '.gitignore' files found in the synced source directories will be "
                           "applied to files in that directory and its subdirectories."
                      )
  parser.add_argument('--verbose', action='store_true')

  args = parser.parse_args()

  logging.basicConfig(
    format='%(message)s',
    level=logging.DEBUG if args.verbose else logging.INFO
  )

  src_path = Gio.File.new_for_commandline_arg(args.src)

  case_insensitive_schemes = ['mtp']
  
  if args.dst:
    dst_path = Gio.File.new_for_commandline_arg(args.dst)

    case_insensitive_protect = \
      not args.no_case_insensitive_protect \
      and (
        (
          src_path.get_uri_scheme() not in case_insensitive_schemes
          and dst_path.get_uri_scheme() in case_insensitive_schemes
        )
        or args.case_insensitive_protect
      )

    logging.info("Case insensitivity protection is %s", "on" if case_insensitive_protect else "off")

    # Work out exclusion matchers
    exclude_src = []
    exclude_dst = []
    
    if args.ignore:
      full_ignore_list = '\n'.join(args.ignore)
      logging.info("Excluding files matching gitignore spec:\n  %s", '\n  '.join(full_ignore_list.split()))
      exclude_src.append(MatcherGitignore.from_text(full_ignore_list, src_path))
      exclude_dst.append(MatcherGitignore.from_text(full_ignore_list, dst_path))

    for f in args.ignore_file:
      logging.info("Excluding files matching gitignore spec in file '%s'", f)
      exclude_src.append(MatcherGitignore.from_file(Gio.File.new_for_commandline_arg(f), src_path))
      exclude_dst.append(MatcherGitignore.from_file(Gio.File.new_for_commandline_arg(f), dst_path))

    exclude_finders: list[MatcherFinder] = []
    
    if args.gitignore:
      logging.info("Excluding anything matched by any of the source paths' \".gitignore\" files")
      exclude_finders.append(MatcherFinderGitignore())

    # Run sync
    total = sync_recurse(src_path, dst_path, args.dry_run, args.size_only, case_insensitive_protect,
                         exclude_src, exclude_dst, exclude_finders)
    
    progress_operation_show(0, total)
    logging.info("Done")
  else:
    list_recurse(src_path)
  

if __name__ == '__main__':
  main()
