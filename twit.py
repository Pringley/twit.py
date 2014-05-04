#!/usr/bin/env python
"""Twit: an easier Git frontend.  """
import os
import re
import sys
import time
import subprocess
import contextlib
import collections

import click

try:
    import github3  # NOQA
    GITHUB3 = True
except ImportError:
    GITHUB3 = False

PY2 = sys.version_info[0] == 2
PY3 = sys.version_info[0] == 3

CommitInfo = collections.namedtuple('CommitInfo',
        ('message', 'time', 'parents'))
CommitInfo.__doc__ = """Details about a commit."""

class TwitError(Exception):
    """Generic error for Twit."""


class NotARepository(TwitError):
    """Raised when not in a Git repository."""


class DetachedHead(TwitError):
    """Raised when the repository is in detached HEAD mode."""


class DirtyWorkTree(TwitError):
    """Raised when the work tree is dirty."""


class InvalidRef(TwitError):
    """Raised when a bad reference is provided."""


class GitError(TwitError):
    """The git subprocess produced an error."""


class CannotFindGit(GitError):
    """Script could not locate the git executable."""


def _git(*args, strip=True):
    """Delegate to the Git executable."""
    try:
        proc = subprocess.Popen(('git',) + args, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT)
        stdout, _ = proc.communicate()
    except OSError as error:
        if error.errno == os.errno.ENOENT:
            raise CannotFindGit("git executable not found")
        else:
            raise
    if not PY2:
        stdout = stdout.decode()
    if 'fatal: Not a git repository' in stdout:
        raise NotARepository("current directory is not part of a repository")
    if strip:
        stdout = stdout.rstrip()
    return stdout


@contextlib.contextmanager
def _cd(path):
    """Context manager to temporarily change directory."""
    old_cwd = os.getcwd()
    os.chdir(path)
    yield
    os.chdir(old_cwd)


class GitExeRepo(object):
    """Git repository backed by Git plumbing shell commands."""

    def __init__(self, path, workdir=None):
        self.path = os.path.abspath(path)
        self.workdir = workdir or os.path.dirname(path)

    @classmethod
    def from_cwd(cls):
        """Get the Repository object implied by the current directory."""
        repo_path = _git('rev-parse', '--git-dir')
        workdir = _git('rev-parse', '--show-toplevel') or None
        return cls(repo_path, workdir)

    @property
    def current_branch(self):
        """Get the current branch."""
        with _cd(self.path):
            ref = _git('symbolic-ref', '-q', 'HEAD')
            if not ref:
                raise DetachedHead
            return re.sub('^refs/heads/', '', ref)

    @property
    def refs(self):
        """Get a list of all references."""
        with _cd(self.path):
            return _git('for-each-ref', '--format', '%(refname)').split('\n')

    @property
    def branches(self):
        """Get a list of all branches."""
        return [
            re.sub('^refs/heads/', '', ref)
            for ref in self.refs
            if ref.startswith('refs/heads/')
        ]

    @property
    def dirty(self):
        """Check for modified or untracked files."""
        with _cd(self.workdir):
            status = _git('status', '-z').rstrip('\0 ')
            if not status:
                return
            for line in status.split('\0'):
                wstat = line[1] # status of work tree
                if wstat not in (' ', '!'):
                    return True
            return False

    def stage_all(self):
        """Stage all changes in the working directory."""
        with _cd(self.workdir):
            _git('add', '--all', '.')

    def unstage_all(self):
        """Reset the index to the previous commit."""
        with _cd(self.workdir):
            head = _git('rev-parse', '--verify', '-q', 'HEAD')
            if head:
                _git('read-tree', head)
            else:
                _git('read-tree', '--empty')

    def discard_all(self):
        """Discard all changes."""
        with _cd(self.workdir):
            self.stage_all()
            head = _git('rev-parse', '--verify', '-q', 'HEAD')
            if not head:
                paths = _git('ls-files', '-z').rstrip('\0 ').split('\0')
                for path in paths:
                    os.remove(path)
            else:
                _git('reset', '--hard', head)

    def safe_checkout(self, ref):
        """Update a clean work tree to match a reference."""
        if self.dirty:
            raise DirtyWorkTree
        with _cd(self.workdir):
            if not _git('rev-parse', '--verify', '-q', ref):
                raise InvalidRef
            _git('checkout', '-q', ref)

    def commit(self, message, ref=None):
        """Create a commit."""
        with _cd(self.path):
            tree = _git('write-tree')
            prev_commit = _git('rev-parse', '--verify', '-q', 'HEAD')
            ref = ref or _git('symbolic-ref', '-q', 'HEAD')
            args = ['commit-tree', tree, '-m', message]
            if prev_commit:
                args += ['-p', prev_commit]
            commit = _git(*args)
            if ref:
                _git('update-ref', ref, commit)

    def rev_parse(self, ref):
        """Return the oid of the reference, or an empty string if error."""
        with _cd(self.path):
            return _git('rev-parse', '--verify', '-q', ref)

    def commit_info(self, ref):
        """Return info about a given commit."""
        oid = _git('rev-parse', '--verify', '-q', ref)
        if not oid:
            raise InvalidRef
        raw_commit = _git('cat-file', '-p', oid, strip=False)

        paragraphs = raw_commit.split('\n\n')
        lines = paragraphs[0].split('\n')
        message = '\n\n'.join(paragraphs[1:])
        tree = lines.pop(0).split(' ')[1]
        parents = []
        while lines[0].startswith('parent'):
            parent = lines.pop(0).split(' ')[1]
            parents.append(parent)
        raw_author = lines.pop(0)
        author_match = re.match('^.*? (.*) (\d+) .*$', raw_author)
        author, author_timestamp = author_match.groups()

        return CommitInfo(message=message,
                          time=author_timestamp,
                          parents=parents)


class TwitMixin(object):
    """Non-backend-specific Twit methods."""

    @property
    def snapshots(self):
        """Return a list of Twit snaphsots."""
        return [
            ref for ref in self.refs
            if ref.startswith('refs/hidden/tags/twit/')
        ]

    def save(self):
        """Save a snapshot of the working directory."""
        self.stage_all()
        now = int(time.time())
        ref = 'refs/hidden/tags/twit/{}'.format(now)
        self.commit('Snapshot taken via `twit save`.', ref=ref)
        self.unstage_all()



class GitExeTwitRepo(GitExeRepo, TwitMixin):
    """Twit repo backed by GitExe."""


TwitRepo = GitExeTwitRepo


@click.group()
def main():
    """Twit: an easier git frontend.

    For help on a subcommand, run:

        twit help SUBCOMMAND

    """


@main.command()
def save():
    """Take a snapshot of your current work."""
    repo = TwitRepo.from_cwd()
    repo.save()
    click.echo('Snapshot saved.')


@main.command('help')
@click.argument('subcommand', required=False)
@click.pass_context
def help_(context, subcommand):
    """Print help for a subcommand."""
    if subcommand is None:
        click.echo(main.get_help(context))
    else:
        if subcommand not in main.commands:
            click.echo("Command '{}' does not exist.\n".format(subcommand))
            click.echo(main.get_help(context))
            context.exit(1)
        command = main.commands[subcommand]
        click.echo(command.get_help(context))


if __name__ == '__main__':
    main()
