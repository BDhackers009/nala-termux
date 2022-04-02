#                 __
#    ____ _____  |  | _____
#   /    \\__  \ |  | \__  \
#  |   |  \/ __ \|  |__/ __ \_
#  |___|  (____  /____(____  /
#       \/     \/          \/
#
# Copyright (C) 2021, 2022 Blake Lee
#
# This file is part of nala
#
# nala is program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# nala is program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with nala.  If not, see <https://www.gnu.org/licenses/>.
"""Where Utilities who don't have a special home come together."""
from __future__ import annotations

import json
import os
import signal
import sys
import termios
import tty
from datetime import datetime
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Any, Literal, cast

import jsbeautifier
from apt.package import Package, Version

from nala import _, color, console
from nala.constants import ERROR_PREFIX, HANDLER, JSON_OPTIONS, NALA_DEBUGLOG
from nala.options import arguments
from nala.rich import Group, Table, Tree, from_ansi

if TYPE_CHECKING:
	from nala.cache import Cache
	from nala.debfile import NalaDebPackage


class Terminal:
	"""Represent the user terminal."""

	# Term Constants
	STDIN = 0
	STDOUT = 1
	STDERR = 2

	# Control Codes
	CURSER_UP = b"\x1b[1A"
	CLEAR_LINE = b"\x1b[2k"
	CLEAR = b"\x1b[2J"
	CLEAR_FROM_CURRENT_TO_END = b"\x1b[K"
	BACKSPACE = b"\x08"
	HOME = b"\x1b[H"
	ENABLE_BRACKETED_PASTE = b"\x1b[?2004h"
	DISABLE_BRACKETED_PASTE = b"\x1b[?2004l"
	ENABLE_ALT_SCREEN = b"\x1b[?1049h"
	DISABLE_ALT_SCREEN = b"\x1b[?1049l"
	SHOW_CURSOR = b"\x1b[?25h"
	HIDE_CURSOR = b"\x1b[?25l"
	SET_CURSER = b"\x1b[?1l"
	SAVE_TERM = b"\x1b[22;0;0t"
	RESTORE_TERM = b"\x1b[23;0;0t"
	APPLICATION_KEYPAD = b"\x1b="
	NORMAL_KEYPAD = b"\x1b>"
	CR = b"\r"
	LF = b"\n"
	CRLF = b"\r\n"

	def __init__(self) -> None:
		"""Represent the user terminal."""
		self.console = console
		self.mode: list[int | list[bytes | int]] = []
		self.term_type: str = os.environ.get("TERM", "")
		self.locale: str = ""
		self.set_environment()

	def __repr__(self) -> str:
		"""Represent state of the user terminal as a string."""
		representation = {
			"object": "Terminal",
			"columns": self.columns,
			"lines": self.lines,
			"mode": str(self.mode),
			"term": self.console.is_terminal,
		}
		return str(jsbeautifier.beautify(json.dumps(representation), JSON_OPTIONS))

	def set_environment(self) -> None:
		"""Check and set various environment variables."""
		# Termios can't run if we aren't in a terminal
		# Just catch the exception and continue.
		try:
			self.mode = termios.tcgetattr(self.STDIN)
		except termios.error:
			pass

		if self.lines < 13 or self.columns < 31:
			print(
				_("Terminal can't support dialog, falling back to readline"),
				file=sys.stderr,
			)
			os.environ["DEBIAN_FRONTEND"] = "readline"
		# Readline is too hard to support with our fancy formatting
		if os.environ.get("DEBIAN_FRONTEND") == "readline":
			arguments.raw_dpkg = True
		os.environ["DPKG_COLORS"] = "never"
		self.locale = os.environ.get("LANG", "")
		# We have to set lang as C so we get predictable output from dpkg.
		os.environ["LANG"] = "C" if self.console.options.ascii_only else "C.UTF-8"

	@property
	def columns(self) -> int:
		"""Return termindal width."""
		return self.console.width

	@property
	def lines(self) -> int:
		"""Return terminal height."""
		return self.console.height

	def restore_mode(self) -> None:
		"""Restore the mode the Terminal was initialized with."""
		if not self.console.is_terminal:
			return
		try:
			termios.tcsetattr(self.STDIN, termios.TCSAFLUSH, self.mode)
		except termios.error:
			pass

	def restore_locale(self) -> None:
		"""Restore the locale to it's original value."""
		os.environ["LANG"] = self.locale

	def set_raw(self) -> None:
		"""Set terminal raw."""
		try:
			tty.setraw(self.STDIN)
		except termios.error:
			pass

	def write(self, data: bytes) -> None:
		"""Write bytes directly to stdout."""
		os.write(self.STDOUT, data)

	def is_xterm(self) -> bool:
		"""Return True if we're in an xterm, False otherwise."""
		return "xterm" in self.term_type

	@property
	def overflow(self) -> Literal["crop"]:
		"""Return overflow method for Rich."""
		# We only cast Literal['crop'] here to make mypy happy.
		return cast(
			Literal["crop"], "crop" if self.console.options.ascii_only else "ellipsis"
		)

	@staticmethod
	def is_su() -> bool:
		"""Return True if we're super user and False if we're not."""
		return os.geteuid() == 0


class DelayedKeyboardInterrupt:
	"""Context manager to delay KeyboardInterrupt.

	Keyboard Interrupts will be delayed until out of scope.
	"""

	def __init__(self) -> None:
		"""Context manager to delay KeyboardInterrupt."""
		self.signal_received: tuple[int, FrameType | None] | bool
		self.old_handler: HANDLER

	def __enter__(self) -> None:
		"""Enter context."""
		self.signal_received = False
		self.old_handler = signal.signal(signal.SIGINT, self.handler)

	def handler(self, sig: int, frame: FrameType | None) -> None:
		"""Handle sigint signals."""
		self.signal_received = (sig, frame)
		dprint("SIGINT received. Delaying KeyboardInterrupt.")

	def __exit__(self, _type: None, _value: None, _traceback: None) -> None:
		"""Exit context."""
		signal.signal(signal.SIGINT, self.old_handler)
		if isinstance(self.signal_received, tuple) and callable(self.old_handler):
			self.old_handler(*self.signal_received)


class PackageHandler:  # pylint: disable=too-many-instance-attributes
	"""Class for storing package lists."""

	def __init__(self) -> None:
		"""Class for storing package lists."""
		self.autoremoved: list[str] = []
		self.local_debs: list[NalaDebPackage] = []
		self.delete_pkgs: list[NalaPackage] = []
		self.install_pkgs: list[NalaPackage] = []
		self.reinstall_pkgs: list[NalaPackage] = []
		self.upgrade_pkgs: list[NalaPackage] = []
		self.autoremove_pkgs: list[NalaPackage] = []
		self.recommend_pkgs: list[NalaPackage | list[NalaPackage]] = []
		self.suggest_pkgs: list[NalaPackage | list[NalaPackage]] = []
		self.configure_pkgs: list[NalaPackage] = []
		self.downgrade_pkgs: list[NalaPackage] = []

	@property
	def extended_deleted(self) -> list[NalaPackage]:
		"""Return deleted_pkgs + autoremove_pkgs."""
		return self.delete_pkgs + self.autoremove_pkgs

	@property
	def delete_total(self) -> int:
		"""Return total deleted pkgs."""
		return len(self.delete_pkgs)

	@property
	def install_total(self) -> int:
		"""Return total installed pkgs."""
		return len(self.install_pkgs)

	@property
	def reinstall_total(self) -> int:
		"""Return total installed pkgs."""
		return len(self.reinstall_pkgs)

	@property
	def upgrade_total(self) -> int:
		"""Return total upgraded pkgs."""
		return len(self.upgrade_pkgs)

	@property
	def downgrade_total(self) -> int:
		"""Return total upgraded pkgs."""
		return len(self.downgrade_pkgs)

	@property
	def autoremove_total(self) -> int:
		"""Return total autoremoved pkgs."""
		return len(self.autoremove_pkgs)

	@property
	def configure_total(self) -> int:
		"""Return total autoremoved pkgs."""
		return len(self.configure_pkgs)

	@property
	def local_total(self) -> int:
		"""Return total local pkgs."""
		return len(self.local_debs)

	@property
	def dpkg_progress_total(self) -> int:
		"""Calculate our total operations for the dpkg progress bar."""
		return (
			self.delete_total * 2
			+ self.autoremove_total * 2
			# We add an extra for each install due to Unpacking: and Setting up:
			+ self.install_total * 2
			+ self.reinstall_total * 2
			+ self.downgrade_total * 2
			+ self.upgrade_total * 2
			# For local deb installs we add 1 more because of having to start
			# and stop InstallProgress an extra time for each package
			+ self.local_total
			# Configure needs an extra because it isn't unpacked
			+ self.configure_total * 2
			# This last +1 for the ending of dpkg itself
			+ 1
		)


class NalaPackage:
	"""Class that represents a Nala package."""

	def __init__(
		self, name: str, version: str, size: int, old_version: str | None = None
	) -> None:
		"""Class that represents a Nala package."""
		self.name = name
		self.version = version
		self.size = size
		self.old_version = old_version

	def __repr__(self) -> str:
		"""Return string representation of the object."""
		return f"NalaPackage: name:{self.name} version:{self.version}"

	@property
	def unit_size(self) -> str:
		"""Return the size as a readable unit. Example 12MB."""
		return unit_str(int(self.size))


term = Terminal()


def ask(question: str, default_no: bool = False) -> bool:
	"""Ask the user {question}.

	resp = input(f'{question}? [Y/n]

	Y returns True
	N returns False
	"""
	while True:
		resp = input(f"{question} [Y/n] ")
		if resp in ("y", "Y"):
			return True
		if resp in ("n", "N"):
			return False
		if resp == "":
			return not default_no
		print(_("Not a valid choice kiddo"))


def sudo_check(msg: str) -> None:
	"""Check for root and exits if not root."""
	if  term.is_su():
		sys.exit(f"{ERROR_PREFIX} {msg}")


def get_date() -> str:
	"""Return the formatted Date and Time."""
	timezone = str(datetime.utcnow().astimezone().tzinfo)
	return f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {timezone}"


def unit_str(val: int, just: int = 7) -> str:
	"""Check integer and figure out what format it should be."""
	if val > 1000**2:
		return f"{val/1000/1000 :.1f}".rjust(just) + " MB"
	if val > 1000:
		return f"{round(val/1000) :.0f}".rjust(just) + " kB"
	return f"{val :.0f}".rjust(just) + " B"


def iter_remove(path: Path) -> None:
	"""Iterate the directory supplied and remove all files."""
	vprint(_("Removing files in {dir}").format(dir=path))
	for file in path.iterdir():
		if file.is_file():
			dprint(_("Removed: {filename}").format(filename=file))
			dprint(f"Removed: {file}")
			file.unlink(missing_ok=True)


def get_version(pkg: Package) -> Version:
	"""Get the version, any version of a package."""
	if pkg.candidate:
		return pkg.candidate
	if pkg.installed:
		return pkg.installed
	for version in pkg.versions:
		return version
	# It would be really weird if we ever actually hit this error
	sys.exit(
		_("{error} can't find version for {pkg_name}").format(
			error=ERROR_PREFIX, pkg_name=pkg.name
		)
	)


def get_pkg_name(candidate: Version) -> str:
	"""Return the package name.

	Checks if we need and epoch in the path.
	"""
	if ":" in candidate.version:
		index = candidate.version.index(":")
		epoch = f"_{candidate.version[:index]}%3a"
		return Path(candidate.filename).name.replace("_", epoch, 1)
	return Path(candidate.filename).name


def pkg_candidate(pkg: Package) -> Version:
	"""Type enforce package candidate."""
	assert pkg.candidate
	return pkg.candidate


def pkg_installed(pkg: Package) -> Version:
	"""Type enforce package installed."""
	assert pkg.installed
	return pkg.installed


def get_installed_dep_names(installed_pkgs: tuple[Package, ...]) -> tuple[str, ...]:
	"""Iterate installed pkgs and return all of their deps in a list.

	This is so we can reduce iterations when checking reverse depends.
	"""
	total_deps = []
	for pkg in installed_pkgs:
		for deps in pkg_installed(pkg).dependencies:
			for dep in deps:
				if dep.name not in total_deps:
					total_deps.append(dep.name)
	return tuple(total_deps)


def print_rdeps(name: str, installed_pkgs: tuple[Package]) -> None:
	"""Print the installed reverse depends of a package."""
	msg = color(
		_("Installed Packages that Depend on {pkg_name}\n").format(
			pkg_name=color(name, "GREEN")
		),
		"YELLOW",
	)
	for pkg in installed_pkgs:
		for dep in pkg_installed(pkg).dependencies:
			if name in dep.rawstr:
				if pkg.essential:
					msg += _("  {pkg_name} is an {essential} package!\n").format(
						pkg_name=color(pkg.name, "GREEN"),
						essential=color("Essential", "RED"),
					)
					continue
				msg += f"  {color(pkg.name, 'GREEN')}\n"
				break
	print(msg.strip())


def arg_check() -> None:
	"""Check arguments and errors if no packages are specified.

	If args exists then duplicates will be removed.
	"""
	dprint(f"Raw Arguments: {sys.argv}")
	if arguments.no_update and arguments.update:
		sys.exit(
			_(
				"{error} {update} and {no_update} cannot be used at the same time"
			).format(
				error=ERROR_PREFIX,
				update=color("--update", "YELLOW"),
				no_update=color("--no-update", "YELLOW"),
			)
		)

	if arguments.command in ("install", "remove", "purge", "show"):
		if arguments.command == "install" and arguments.fix_broken:
			arguments.args = dedupe_list(arguments.args)
			return
		if not arguments.args:
			sys.exit(
				_("{error} You must specify a package to {command}").format(
					error=ERROR_PREFIX, command=arguments.command
				)
			)
		arguments.args = dedupe_list(arguments.args)


def dedupe_list(original: list[str]) -> list[str]:
	"""Deduplicate a list.

	Useful for when we want to maintain the list order and can't use set()
	"""
	dedupe = []
	for item in original:
		if item not in dedupe:
			dedupe.append(item)
	return dedupe


def get_summary_header(history: bool = False) -> tuple[str, str, str, str]:
	"""Return the correct headers for the summary."""
	if not arguments.command and arguments.fix_broken:
		return _("Remove"), _("Removing:"), _("Auto-Remove"), _("Auto-Removing:")
	if "purge" in arguments.command and history:
		return _("Purged"), _("Purged:"), _("Auto-Purged"), _("Auto-Purged:")
	if "purge" in arguments.command and not history:
		return _("Purge"), _("Purging:"), _("Auto-Purge"), _("Auto-Purging:")
	if not history:
		return _("Remove"), _("Removing:"), _("Auto-Remove"), _("Auto-Removing:")
	return _("Removed"), _("Removed:"), _("Auto-Removed"), _("Auto-Removed:")


def print_update_summary(nala_pkgs: PackageHandler, cache: Cache | None = None) -> None:
	"""Print our transaction summary."""
	dprint("Printing Update Summary")

	delete, deleting, auto_remove, auto_removing = get_summary_header(not cache)

	default_header = [_("Package:"), _("Version:"), _("Size:")]
	upgrade_header = [_("Package:"), _("Old Version:"), _("New Version:"), _("Size:")]

	print_packages(default_header, nala_pkgs.delete_pkgs, deleting, "bold red")

	print_packages(default_header, nala_pkgs.autoremove_pkgs, auto_removing, "bold red")

	print_packages(
		default_header, nala_pkgs.install_pkgs, _("Installing:"), "bold green"
	)

	print_packages(
		upgrade_header, nala_pkgs.reinstall_pkgs, _("Reinstalling:"), "bold green"
	)

	print_packages(upgrade_header, nala_pkgs.upgrade_pkgs, _("Upgrading:"), "bold blue")

	print_packages(
		upgrade_header, nala_pkgs.downgrade_pkgs, _("Downgrading:"), "bold orange_red1"
	)

	print_packages(
		default_header, nala_pkgs.configure_pkgs, _("Configuring:"), "bold magenta"
	)

	print_packages(
		default_header,
		nala_pkgs.recommend_pkgs,
		_("Recommended, Will Not Be Installed:"),
		"bold magenta",
	)

	print_packages(
		default_header,
		nala_pkgs.suggest_pkgs,
		_("Suggested, Will Not Be Installed:"),
		"bold magenta",
	)

	transaction_summary(delete, auto_remove, nala_pkgs, not cache)
	if cache:
		transaction_footer(cache)


def transaction_summary(
	delete_header: str,
	auto_header: str,
	nala_pkgs: PackageHandler,
	history: bool = False,
) -> None:
	"""Print a small transaction summary."""
	print("=" * term.columns)
	print(_("Summary"))
	print("=" * term.columns)
	table = Table.grid("", padding=(0, 1))
	table.add_column(justify="right", overflow=term.overflow)
	table.add_column(overflow=term.overflow)

	if nala_pkgs.install_pkgs:
		table.add_row(
			_("Install") if not history else _("Installed"),
			str(nala_pkgs.install_total),
			_("Packages"),
		)

	if nala_pkgs.reinstall_pkgs:
		table.add_row(_("Reinstall"), str(nala_pkgs.reinstall_total), _("Packages"))

	if nala_pkgs.upgrade_total:
		table.add_row(
			_("Upgrade") if not history else _("Upgraded"),
			str(nala_pkgs.upgrade_total),
			_("Packages"),
		)

	if nala_pkgs.downgrade_pkgs:
		table.add_row(_("Downgrade"), str(nala_pkgs.downgrade_total), _("Packages"))

	if nala_pkgs.configure_pkgs:
		table.add_row(_("Configure"), str(nala_pkgs.configure_total), _("Packages"))

	if nala_pkgs.delete_total:
		table.add_row(delete_header, str(nala_pkgs.delete_total), _("Packages"))

	if nala_pkgs.autoremove_total:
		table.add_row(auto_header, str(nala_pkgs.autoremove_total), _("Packages"))
	term.console.print(table)


def transaction_footer(cache: Cache) -> None:
	"""Print transaction footer."""
	print()
	if (download := cache.required_download) > 0:
		msg = _("Total download size:")
		print(f"{msg} {unit_str(download)}")
	if (space := cache.required_space) < 0:
		msg = _("Disk space to free:")
		print(f"{msg} {unit_str(-int(space))}")
	if space > 0:
		msg = _("Disk space required:")
		print(f"{msg} {unit_str(space)}")
	if arguments.download_only:
		print(_("Nala will only download the packages"))


def print_packages(
	headers: list[str],
	nala_packages: list[NalaPackage] | list[NalaPackage | list[NalaPackage]],
	title: str,
	style: str,
) -> None:
	"""Print package transactions in a pretty format."""
	if not nala_packages:
		return

	package_table = Table(padding=(0, 2), box=None)
	# Setup rich table and columns
	for header in headers:
		if header == _("Package:"):
			package_table.add_column(header, style=style, overflow=term.overflow)
		elif header == _("Size:"):
			package_table.add_column(header, justify="right", overflow=term.overflow)
		else:
			package_table.add_column(header, overflow="fold")

	# Add our packages
	for pkg in nala_packages:
		if isinstance(pkg, list):
			continue
		if pkg.old_version:
			package_table.add_row(pkg.name, pkg.old_version, pkg.version, pkg.unit_size)
			continue
		package_table.add_row(pkg.name, pkg.version, pkg.unit_size)

	# We iterate again so or_deps will be grouped at the bottom
	for pkg in nala_packages:
		if isinstance(pkg, list):
			package_table.add_row(*summary_or_depends(pkg))

	sep = "=" * term.columns
	term.console.print(sep, title, sep, package_table)


def summary_or_depends(pkg: list[NalaPackage]) -> tuple[Tree, Table, Table]:
	"""Format Recommend and Suggests or dependencies."""
	either = _("Either:")
	pkg_tree = Tree(f"{color(either)}", guide_style="white")
	for npkg in pkg:
		pkg_tree.add(npkg.name)
	ver_table = Table("", border_style="bold blue", box=None, pad_edge=False)
	ver_table.add_row(Group(*(npkg.version for npkg in pkg)))
	size_table = Table("", border_style="bold blue", box=None, pad_edge=False)
	size_table.add_row(Group(*(npkg.unit_size for npkg in pkg)))

	return pkg_tree, ver_table, size_table


def vprint(msg: object) -> None:
	"""Print message if verbose."""
	msg = str(msg)
	if arguments.verbose or arguments.debug:
		print(msg)
	if arguments.debug:
		dprint(from_ansi(msg).plain, from_verbose=True)
	sys.__stdout__.flush()


def dprint(msg: object, from_verbose: bool = False) -> None:
	"""Print message if debugging, write to log if root."""
	if not arguments.debug:
		return
	if not from_verbose:
		print(f"DEBUG: {msg}")
	if term.is_su():
		with open(NALA_DEBUGLOG, "a", encoding="utf-8") as logfile:
			logfile.write(f"[{get_date()}] DEBUG: {msg}\n")


def eprint(*args: Any, **kwargs: Any) -> None:
	"""Print message to stderr."""
	print(*args, file=sys.stderr, **kwargs)
