#!/usr/bin/env python
"""Twit: an easier Git frontend.  """
import os
import re
import sys
import time
import json
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

CommitInfo = collections.namedtuple('CommitInfo',
        ('message', 'time', 'parents', 'tree'))

class TwitError(Exception):
    """Generic error for Twit."""


class NotARepository(TwitError):
    """Raised when not in a Git repository."""


class DetachedHead(TwitError):
    """Raised when the repository is in detached HEAD mode."""


class UnbornBranch(TwitError):
    """Raised when the current branch is unborn."""


class DirtyWorkTree(TwitError):
    """Raised when the work tree is dirty."""


class InvalidRef(TwitError):
    """Raised when a bad reference is provided."""


class InvalidSnapshot(TwitError):
    """Raised when an invalid snapshot is found."""


class GitError(TwitError):
    """The git subprocess produced an error."""


class CannotFindGit(GitError):
    """Script could not locate the git executable."""


def _git_nostrip(*args):
    """Delegate to the Git executable, returning unstripped output."""
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
    return stdout

def _git(*args):
    """Delegate to the Git executable."""
    return _git_nostrip(*args).rstrip()

class _cd(object):
    """Context manager to temporarily change directory."""
    def __init__(self, path):
        self.path = path
    def __enter__(self):
        self.old_cwd = os.getcwd()
        os.chdir(self.path)
    def __exit__(self, type_, value, traceback):
        os.chdir(self.old_cwd)


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
    def detached_head(self):
        """Return True if in detached HEAD mode.."""
        with _cd(self.path):
            ref = _git('symbolic-ref', '-q', 'HEAD')
            return (not ref)

    @property
    def unborn(self):
        """Return True if the current branch is unborn."""
        return (not self.rev_parse('HEAD'))

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
            return commit

    def reset(self, ref, reset_type='mixed'):
        """Reset to a previous commit (as `git reset`)."""
        if reset_type not in ('soft', 'hard', 'mixed'):
            raise ValueError('invalid reset type')
        type_arg = '--' + reset_type
        with _cd(self.workdir):
            if not self.rev_parse(ref):
                raise InvalidRef
            if self.unborn:
                raise UnbornBranch
            _git('reset', type_arg, ref)

    def set_head(self, branch, force=False):
        """Set HEAD to a given branch."""
        with _cd(self.path):
            ref = 'refs/heads/' + branch
            if not force and not self.rev_parse(ref):
                raise InvalidRef
            _git('symbolic-ref', 'HEAD', ref)

    def rev_parse(self, ref):
        """Return the oid of the reference, or an empty string if error."""
        with _cd(self.path):
            return _git('rev-parse', '--verify', '-q', ref)

    def commit_info(self, ref):
        """Return info about a given commit."""
        oid = _git('rev-parse', '--verify', '-q', ref)
        if not oid:
            raise InvalidRef
        raw_commit = _git_nostrip('cat-file', '-p', oid)

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
                          tree=tree,
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

    @property
    def snapshot_commits(self):
        """Return a list of commit hashes referring to Twit snapshots."""
        return [
            self.rev_parse(snapshot)
            for snapshot in self.snapshots
        ]

    def save(self):
        """Save a snapshot of the working directory."""
        self.stage_all()
        index = 1
        while 'refs/hidden/tags/twit/{}'.format(index) in self.refs:
            index += 1
        ref = 'refs/hidden/tags/twit/{}'.format(index)
        try:
            branch = self.current_branch
        except DetachedHead:
            branch = None
        message = json.dumps({
            'branch': branch,
            'note': 'Tag auto-generated by Twit.',
        })
        commit = self.commit(message, ref=ref)
        self.unstage_all()
        return ref

    def open_snapshot(self, ref):
        """Open a Twit snapshot."""
        if self.dirty:
            raise DirtyWorkTree
        oid = self.rev_parse(ref)
        if not oid or oid not in self.snapshot_commits:
            raise InvalidRef("not a Twit snapshot")
        cinfo = self.commit_info(oid)
        if len(cinfo.parents) > 1:
            raise InvalidSnapshot('multiple parent commits')
        elif len(cinfo.parents) == 1:
            parent = cinfo.parents[0]
        else:
            parent = None
        try:
            sinfo = json.loads(cinfo.message)
        except ValueError:
            raise InvalidSnapshot('message is invalid json')
        branch = sinfo.get('branch', None)

        # First, checkout the snapshot.
        self.safe_checkout(oid)

        if parent is None:
            # If the snapshot was taken on an unborn branch, set HEAD to a
            # temporary branch and clear the index.
            self.set_head('twit/snapshot/unborn' + cinfo.time, force=True)
            self.unstage_all()
        else:
            # Otherwise, simply reset HEAD and the index to the commit that the
            # snapshot was taken from.
            self.reset(parent)

        if branch is not None:
            branch_commit = self.rev_parse(branch)
            if parent is None and not branch_commit:
                # If the snapshot was taken on an unborn branch and the branch
                # is still unborn, then set the HEAD to point to that branch.
                self.set_head(branch, force=True)
            elif parent is not None and branch_commit == parent:
                # If the snapshot was taken on a branch that has not been
                # updated, set the HEAD to point to that branch.
                self.set_head(branch)
            else:
                # If the snapshot's original branch points to a different
                # commit, enter detached HEAD mode.
                pass

    def open(self, ref):
        """Open a branch, commit, or snapshot."""
        if self.dirty:
            raise DirtyWorkTree
        oid = self.rev_parse(ref)
        if not oid:
            ref = 'refs/heads/' + ref
            oid = self.rev_parse(ref)
            if not oid:
                raise InvalidRef
        if oid in self.snapshot_commits:
            self.open_snapshot(oid)
        else:
            self.safe_checkout(ref)

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


@main.command()
@click.argument('revision')
def open(revision):
    """Open a snapshot or branch."""
    repo = TwitRepo.from_cwd()
    if repo.dirty:
        snapshot = repo.save()
        repo.discard_all()
    try:
        repo.open(revision)
    except InvalidRef:
        click.echo("Invalid revision specified.""")
        repo.open(snapshot)

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
