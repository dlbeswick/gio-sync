import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")
from gi.repository import Gio # type: ignore
from gi.repository import GLib # type: ignore

from .gio import file_at, FileEntry
from .matcher import Matcher


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

  
type FileNameToFileEntryComparable = dict[str, FileEntryComparable]

def comparable_file_name_map(files: list[FileEntry]) -> FileNameToFileEntryComparable:
  """Given a list of Gio.Files, returns a map of the files' names to the FileEntry objects."""
  return { f[1].get_name(): FileEntryComparable(f) for f in files }


class Diff:
  """Given two maps of file names to a list of FileEntryComparable (wrapped Gio.FileInfo) objects found at a
  particular path, creates a report detailing which files are missing, changed or added in the 'right' path as
  compared to 'left'.

  :param is_dir: True if the lists consist of only directories, or False if they consist of only files.
  """
  
  changed: set[tuple[FileEntryComparable, FileEntryComparable]]
  excluded_with_matchers: list[tuple[FileEntry, list[Matcher]]]

  
  def __init__(self,
               entries_left: list[FileEntry], entries_right: list[FileEntry], is_dir: bool,
               size_only: bool, exclude_matchers_left: list[Matcher],
               exclude_matchers_right: list[Matcher]):
    
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

    # Handle file exclusion
    def exclude_entries_and_report(entries: list[FileEntry], exclude_matchers: list[Matcher]):
      excluded_with_matchers: list[tuple[FileEntry, list[Matcher]]] = []
      entries_not_excluded: list[FileEntry] = []
      
      for entry in entries:
        matched = [m for m in exclude_matchers if m.match_is(entry[0])]
        if matched:
          excluded_with_matchers.append((entry, matched))
        else:
          entries_not_excluded.append(entry)

      return entries_not_excluded, excluded_with_matchers

    entries_not_excluded_left, self.excluded_with_matchers = \
      exclude_entries_and_report(entries_left, exclude_matchers_left)

    # Important to also remove excluded entries from the right side, as if this isn't done then already-synced files
    # that are excluded will be deleted. This is possibly unsafe. A commandline option may be added to allow this
    # behaviour in future.
    entries_not_excluded_right, _ = \
      exclude_entries_and_report(entries_right, exclude_matchers_right)
    
    left = comparable_file_name_map(entries_not_excluded_left)
    right = comparable_file_name_map(entries_not_excluded_right)
    
    leftfiles = set(left.values())
    rightfiles = set(right.values())
    
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
        infos.append(f"time {GLib.DateTime.format_iso8601(l.info.get_modification_date_time())}"
                     f"vs {GLib.DateTime.format_iso8601(r.info.get_modification_date_time())}")

      return ", ".join(infos)
    
    return f"""Extra:
{lf.join([f.info.get_name() for f in self.extra])}
Missing:
{lf.join([f.info.get_name() for f in self.missing])}
Changed:
{lf.join([f"{l.info.get_name()} ({change_info(l, r)})" for l, r in self.changed])}
Same:
{lf.join([f"{l.info.get_name()} (size {str(l.info.get_size())})" for l, r in self.same])}
Excluded:
{lf.join([f"{e[1].get_name()} because of {','.join([m.description for m in ms])}" for e, ms in self.excluded_with_matchers])}
    """

  @property
  def excluded_is(self):
    return len(self.excluded_with_matchers) > 0
