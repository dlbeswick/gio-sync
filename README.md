# gio-sync

This project uses the [Gio](https://docs.gtk.org/gio/) library and [GVfs](https://en.wikipedia.org/wiki/GVfs) to sync files between two locations.

File access for MTP devices such as Android phones are notoriously fickle on Linux, and I wrote this program because Gnome's file system turned out to be the only way that I could sync files to and from a particular MTP device. The various MTP Fuse filesystems weren't working; only Gnome Files was. So, I wrote the program as a way to make use of Gnome's IO libraries to sync files using the commandline.

You might also be interested in looking at this project for an example of how to use PyGObject to access Glib and GTK functions from Python.

# Things to know before you start

## No warranty!

Although I use this on my own files, I also have good backups. All care has of course been taken during development, but please be careful with your important files! Consider running with the `--dry-run` parameter first to be sure that this program will do what you expect it to do.

## Only file size is used to detect changed files

Any GVfs file path should work, but this project is geared towards supporting syncing involving MTP devices. MTP has a number of limitations that don't apply to Linux file systems, such as not having modification times and having only a limited set of file metadata. Because of this, the only way to detect file changes on MTP devices by metadata alone is to compare file sizes. If you think you'll have a lot of files that change their contents without changing their file sizes, then this program currently won't work well for you.

## You must mount everything yourself, first

This program won't mount any filesystems for you. Please make sure you can see your devices in the Gnome file explorer before running the program.

## Only Linux is supported

If you have Windows, you probably don't need this program anyway.

## If you need something, let me know

If you think this program could help you out in some way that it's not doing currently, then please raise a feature request in the [Bug Tracker](https://github.com/dlbeswick/gio-sync/issues) and let me know what your use case is. I'll see if I can help, or give you some pointers if you'd like to add the new functionality yourself.

# Installation

## From PIP

Run the following command:

    pip install gio-sync

## From source

1. Make sure the development package for `gobject-introspection` is installed on your system. On Ubuntu, type `sudo apt install libgirepository1.0-dev` in the Terminal. This is a requirement of the PyGObject PIP package.
1. Make sure you have Python 3, PIP and the PIP `build` package installed. You'll probably also need to install `python3-venv` as `build` seems to complain on Ubuntu without it, even if you have `virtualenv` installed separately via PIP.

        sudo apt install python3 python3-pip python3-venv
        pip install build

1. Build the package:

        python3 -m build
		 
1. Install the package with dependencies: 

        pip install dist/*.whl

# Running

1. Make sure the various devices and file locations you're syncing between are mounted in GVfs; in other words, make sure you can see them in Gnome Files.
1. Open the source and destination location windows. Press CTRL+L and look at the location bar at the top of the window to get the GVfs URI for each location. These URIs would start with `mtp://` or `sftp://`, for example. For regular file locations, just add `file://` in front of the path, i.e. `file:///home/my_username/Music`
1. Supply the source and destination URIs to `gio-sync`. Make sure to use quotes around the paths if they contain spaces. Note that the destination path must always point to a directory, and that directory will be created if needed.

## Usage example

    gio-sync file:///home/user/Music "mtp:///MY_PHONE_ID_123345/SD Card/music_synced"
	
# Further help

Please run `gio-sync --help` for further information about additional commandline parameters.

# FAQ

## Is this really necessary?

[Gvfs makes use of libmtp](https://gitlab.gnome.org/GNOME/gvfs/-/blob/master/daemon/gvfsbackendmtp.c) for its MTP backend, just the same as projects like `jmtpfs`. In theory it should work just as well, but in practice these two projects could make use of `libmtp` in such different ways that Gvfs could well perform much better than other projects for some devices. This was my experience. At least the user can know that if Gnome Files works for their device, then this sync tool will also work. This tool also worked for me even when using rsync across the Gvfs file system path in /var/run didn't work.

Because it uses simple MTP operations directly via Gio, it can be much quicker to run than other tools that are written assuming a regular POSIX filesystem.

# For developers

You can run tests as follows:

    python3 test/test.py
	
The tests are system-level tests and they call the `gio-sync` shell command entry point directly, so you may like to install the built package in a virtualenv to test any changes you're making during development.

# Issues

Please do log any issues you come across in the [Bug Tracker](https://github.com/dlbeswick/gio-sync/issues).
