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
  args_final = ["gio-sync"] + list(args)
  print("RUN ", args_final)
  result = subprocess.run(args_final, capture_output=True, universal_newlines=True)
  print("\n\nSTDOUT:\n" + result.stdout)
  print("\n\nSTDERR:\n" + result.stderr)
  return result

def make_test_files(src_dir, srcs, dst_dir, dsts):
  for f in srcs:
    p = Path(src_dir, f)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()

  for f in dsts:
    p = Path(dst_dir, f)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.touch()

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
    
  def run_and_check_files(self, src_dir, srcs, dst_dir, dsts):
    make_test_files(src_dir, srcs, dst_dir, dsts)
    run(src_dir, dst_dir)
    self.check_files(src_dir, srcs, dst_dir)

    
  def test_recurse(self):
    with tempfile.TemporaryDirectory() as src:
      with tempfile.TemporaryDirectory() as dst:
        test_input_files = [
          "0.txt",
          "1/1.txt",
          "1/2/2.txt",
          "1/2/3/3.txt"
        ]

        self.run_and_check_files(src, test_input_files, dst, [])

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
            "1/2/3/3.txt"
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

        run(src, dst)
          
        self.check_files(src, srcs, dst)
        
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

        self.assertEqual(run(f"{src}/delete.txt", dst).returncode, 1)

        
if __name__ == '__main__':
  unittest.main()
