#!/usr/bin/env python3
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

import os
import subprocess
import tempfile
import unittest
from pathlib import Path, PurePath

def run(*args):
  args_final = ["gio-sync", "--verbose"] + list(args)
  print("RUN ", args_final)
  result = subprocess.run(args_final, capture_output=True, universal_newlines=True)
  print("\n\nSTDOUT:\n" + result.stdout)
  print("\n\nSTDERR:\n" + result.stderr)
  return result

def make_test_files(src_dir, srcs, dst_dir, dsts):
  def make_dir_or_file(base, path):
    p = Path(base, f)
    if '.' in p.name:
      print("Make file", p)
      p.parent.mkdir(parents=True, exist_ok=True)
      p.touch()
    else:
      print("Make dir", p)
      p.mkdir(parents=True, exist_ok=True)
    
  for f in srcs:
    make_dir_or_file(src_dir, f)

  for f in dsts:
    make_dir_or_file(dst_dir, f)

  return srcs, dsts


class Test(unittest.TestCase):

  def check_files(self, src_dir, srcs, dst_dir):
    output_files = []
    for dir, dirs, files in os.walk(dst_dir):
      for file in files:
        relative = PurePath(dir, file).relative_to(dst_dir).as_posix()
        output_files.append((Path(src_dir, relative), Path(dst_dir, relative), relative))

    self.assertEqual(set(srcs), {r for src, dst, r in output_files})
    print("All expected files are present.")

    for src, dst, _ in output_files:
      with open(src) as src_io:
        with open(dst) as dst_io:
          self.assertEqual(src_io.read(), dst_io.read(), f"File {dst} has content mismatch")
    print("All file content matches.")
    
    for src, dst, _ in output_files:
      # Note that Gio wasn't writing mod times with nanosecond precision for me, so just check up to
      # microseconds.
      self.assertLessEqual(
        src.stat().st_mtime_ns//1000, dst.stat().st_mtime_ns//1000, f"File {dst} has unexpected mtime"
      )
    print("All file modification times are >= source.")
    
  def run_and_check_files(self, src_dir, srcs, dst_dir, dsts):
    make_test_files(src_dir, srcs, dst_dir, dsts)
    run(src_dir, dst_dir)
    self.check_files(src_dir, srcs, dst_dir)

    
  def test_recurse(self):
    with tempfile.TemporaryDirectory() as src:
      test_input_files = [
        "0.txt",
        "1/1.txt",
        "1/2/2.txt",
        "1/2/3/3.txt"
      ]
      
      self.run_and_check_files(src, test_input_files, os.path.join(src, "out"), [])

  def test_delete(self):
    with tempfile.TemporaryDirectory() as src:
      with tempfile.TemporaryDirectory() as dst:
        self.run_and_check_files(
          src,
          [
            "0.txt",
            "1/1.txt",
            "1/11.txt"
          ],
          dst,
          [
            "0.txt",
            "1/1.txt",
            "1/2/2.txt",
            "1/2/3/3.txt",
            "2",
            "3/4"
          ]
        )
        
  def test_change(self):
    with tempfile.TemporaryDirectory() as src:
      with tempfile.TemporaryDirectory() as dst:
        srcs, dsts = make_test_files(
          src,
          [
            "0.txt",
            "1/1.txt",
            "1/11.txt",
            "1/2/3/3change.txt"
          ],
          dst,
          [
            "0.txt",
            "1/1.txt",
            "1/2/2.txt",
            "1/2/3/3change.txt"
          ]
        )

        with open(Path(src, "1/2/3/3change.txt"), "w") as io:
          io.write("new data in the file")

        # Will also test:
        # * Infinite recursion due to output path being child of source path.
        # * Creation of output directory when it doesn't exist.
        run(src, dst)
          
        self.check_files(src, srcs, dst)

        # Test size change without timestamp change
        with open(Path(src, "1/2/3/3change.txt"), "w") as io:
          io.write("new data in the file 2")

        os.utime(Path(src, "1/2/3/3change.txt"), ns=(0, 0))
        os.utime(Path(dst, "1/2/3/3change.txt"), ns=(0, 0))
        run(src, dst)

        self.check_files(src, srcs, dst)
        
        # Test mtime change detection
        print("MTIME CHANGE")
        os.utime(Path(src, "1/2/3/3change.txt"), ns=(0, 1000))
        os.utime(Path(dst, "1/2/3/3change.txt"), ns=(0, 0))

        run(src, dst)
        
        self.check_files(src, srcs, dst)
        
        # Test --size-only
        print("SIZE ONLY")
        os.utime(Path(src, "1/2/3/3change.txt"), ns=(0, 1000))
        os.utime(Path(dst, "1/2/3/3change.txt"), ns=(0, 0))

        run('--size-only', src, dst)
        
        self.assertEqual(Path(dst, "1/2/3/3change.txt").stat().st_mtime_ns, 0,
                         "Dest file should not have been copied as timestamp changes without size")
        
  def test_single_file(self):
    with tempfile.TemporaryDirectory() as src:
      with tempfile.TemporaryDirectory() as dst:
        srcs, _ = make_test_files(src, ["0.txt"], dst, [])

        run(f"{src}/0.txt", dst)
          
        self.check_files(src, srcs, dst)

  def test_non_existent_single_file(self):
    with tempfile.TemporaryDirectory() as src:
      with tempfile.TemporaryDirectory() as dst:
        srcs, _ = make_test_files(src, [], dst, ["delete.txt"])

        result = run(f"{src}/delete.txt", dst)
        self.assertEqual(result.returncode, 1)
        self.assertIn(result.stdout, "No such file")
    
  def test_case_insensitive_protected(self):
    with tempfile.TemporaryDirectory() as src:
      with tempfile.TemporaryDirectory() as dst:
        srcs, _ = make_test_files(src, ["file0.txt", "File0.txt"], dst, ["file0.txt"])

        result = run("--case-insensitive-protect", src, dst)
        self.assertEqual(result.returncode, 1)
        self.assertIn(result.stdout, "differ only by case")
        
if __name__ == '__main__':
  unittest.main()
