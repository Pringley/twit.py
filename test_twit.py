import os
import re
import sys
import shutil
import tempfile
import unittest

from twit import (GitExeTwitRepo, DetachedHead, DirtyWorkTree, InvalidRef, _cd,
        _git)

PY2 = sys.version_info[0] == 2
PY3 = sys.version_info[0] == 3

class SharedTestMixin(object):
    """Mixin to test both GitRepo backends."""

    if not PY2:
        # renamed in Python 3
        def assertItemsEqual(self, *args, **kwargs):
            return self.assertCountEqual(*args, **kwargs)

    def create_temp_repo(self):
        self.workdir = tempfile.mkdtemp()
        self.old_cwd = os.getcwd()
        os.chdir(self.workdir)
        _git('init')

    def cleanup_temp_repo(self):
        os.chdir(self.old_cwd)
        shutil.rmtree(self.workdir)

    def write_file(self, name='README', content='Read me.'):
        with open(name, 'w') as wfile:
            wfile.write(content)

    def commit_file(self, name='README', content='Read me.'):
        self.write_file(name, content)
        _git('add', name)
        _git('commit', '-m', 'Created {}'.format(name))

    def assert_empty_stage(self):
        status = _git('status', '-z').rstrip('\0 ')
        if not status:
            return
        for line in status.split('\0'):
            self.assertIn(line[0], (' ', '?'))

    def assert_clean_workdir(self):
        status = _git('status', '-z').rstrip('\0 ')
        if not status:
            return
        for line in status.split('\0'):
            self.assertEqual(line[1], ' ')

    def test_current_branch(self):
        self.assertEqual('master', self.repo.current_branch)
        _git('checkout', '-b', 'newbranch')
        self.assertEqual('newbranch', self.repo.current_branch)

        self.commit_file('README')
        head_commit = _git('rev-parse', 'HEAD')
        _git('checkout', head_commit)
        with self.assertRaises(DetachedHead):
            self.repo.current_branch

    def test_detached_head(self):
        self.assertFalse(self.repo.detached_head)
        self.commit_file('README')
        self.assertFalse(self.repo.detached_head)
        commit = _git('rev-parse', 'HEAD')
        _git('checkout', commit)
        self.assertTrue(self.repo.detached_head)

    def test_unborn(self):
        self.assertTrue(self.repo.unborn)
        _git('checkout', '-b', 'foo')
        self.assertTrue(self.repo.unborn)
        self.commit_file('README')
        self.assertFalse(self.repo.unborn)

    def test_refs(self):
        self.commit_file('README')
        self.assertItemsEqual(['refs/heads/master'], self.repo.refs)
        _git('branch', 'newbranch')
        self.assertItemsEqual(['refs/heads/master', 'refs/heads/newbranch'],
                self.repo.refs)
        _git('tag', 'v1.0')
        self.assertItemsEqual(['refs/heads/master', 'refs/heads/newbranch',
            'refs/tags/v1.0'], self.repo.refs)

    def test_branches(self):
        self.commit_file('README')
        self.assertItemsEqual(['master'], self.repo.branches)
        _git('branch', 'newbranch')
        self.assertItemsEqual(['master', 'newbranch'],
                self.repo.branches)
        _git('tag', 'v1.0')
        self.assertItemsEqual(['master', 'newbranch'],
                self.repo.branches)

    def test_dirty(self):
        self.write_file('README', 'original')
        self.assertTrue(self.repo.dirty)
        _git('add', 'README')
        _git('commit', '-m', 'added README')
        self.assertFalse(self.repo.dirty)
        self.write_file('README', 'changed')
        self.assertTrue(self.repo.dirty)
        _git('add', 'README')
        _git('commit', '-m', 'changed README')
        self.assertFalse(self.repo.dirty)
        self.write_file('new_file', 'new')
        self.assertTrue(self.repo.dirty)

    def test_stage_all(self):
        self.commit_file('README', 'original')
        self.commit_file('mistake', 'oops')
        self.write_file('README', 'changed')
        self.write_file('new_file', 'new')
        os.remove('mistake')
        os.mkdir('subdir')
        with _cd('subdir'):
            self.write_file('subfile')
            self.repo.stage_all()
        self.assert_clean_workdir()

    def test_unstage_all(self):
        self.write_file('file1')
        _git('add', 'file1')
        self.repo.unstage_all()
        self.assert_empty_stage()
        self.commit_file('file2')
        self.write_file('file3')
        _git('add', 'file3')
        self.repo.unstage_all()
        self.assert_empty_stage()
        self.write_file('file4')
        os.mkdir('subdir')
        with _cd('subdir'):
            self.write_file('file5')
            self.repo.unstage_all()
        self.assert_empty_stage()

    def test_discard_all(self):
        self.write_file('file1')
        self.repo.discard_all()
        self.assertFalse(os.path.exists('file1'))
        self.commit_file('file1', 'original')
        self.write_file('file1', 'changes')
        self.write_file('file2')
        self.repo.discard_all()
        self.assertFalse(os.path.exists('file2'))
        with open('file1') as rfile:
            contents = rfile.read()
        self.assertEqual(contents, 'original')

    def test_safe_checkout(self):
        self.commit_file('file1', 'foo')
        commit1 = _git('rev-parse', 'HEAD')
        self.repo.safe_checkout('master')
        self.assertEqual('refs/heads/master', _git('symbolic-ref', '-q', 'HEAD'))
        _git('branch', 'bar')
        self.repo.safe_checkout('bar')
        self.assertEqual('refs/heads/bar', _git('symbolic-ref', '-q', 'HEAD'))

    def test_commit(self):
        self.write_file('file1')
        _git('add', 'file1')
        self.repo.commit('initial commit')
        self.assert_clean_workdir()
        self.write_file('file2')
        _git('add', 'file2')
        self.repo.commit('another commit')
        self.assert_clean_workdir()

    def test_reset(self):
        self.commit_file('file1', 'original')
        commit1 = _git('rev-parse', 'HEAD')
        self.write_file('file1', 'changes')
        self.write_file('file2')
        self.repo.reset('HEAD', reset_type='hard')
        self.assertTrue(os.path.exists('file2'))
        with open('file1') as rfile:
            contents = rfile.read()
        self.assertEqual(contents, 'original')
        _git('add', 'file2')
        self.repo.reset('HEAD', reset_type='hard')
        self.assertFalse(os.path.exists('file2'))
        self.write_file('file1', 'changes')
        self.write_file('file2')
        _git('add', 'file2')
        self.repo.reset('HEAD')
        self.assertTrue(os.path.exists('file2'))
        with open('file1') as rfile:
            contents = rfile.read()

    def test_rev_parse(self):
        self.commit_file('file1', 'foo')
        commit1 = _git('rev-parse', 'HEAD')
        self.assertEqual(commit1, self.repo.rev_parse('HEAD'))
        self.assertEqual(commit1, self.repo.rev_parse('master'))
        self.assertEqual('', self.repo.rev_parse('foo'))
        self.assertEqual('', self.repo.rev_parse('HEAD^'))

    def test_commit_info(self):
        self.write_file('file1', 'foo')
        _git('add', 'file1')
        _git('commit', '-m', 'my message')
        commit1 = _git('rev-parse', 'HEAD')
        info = self.repo.commit_info('HEAD')
        self.assertEqual(info.message.rstrip(), 'my message')
        self.write_file('file2', 'foo')
        _git('add', 'file2')
        _git('commit', '-m', 'anothr message')
        info2 = self.repo.commit_info('HEAD')
        self.assertEqual(info2.message.rstrip(), 'anothr message')
        self.assertEqual([commit1], info2.parents)

    def test_save(self):
        self.write_file('file1')
        self.repo.save()
        self.assert_empty_stage()
        refs = _git('for-each-ref', '--format', '%(refname)').split('\n')
        ref_folders = [os.path.dirname(ref) for ref in refs]
        self.assertIn('refs/hidden/tags/twit', ref_folders)

    def test_set_head(self):
        self.commit_file('file1')
        self.repo.set_head('master')
        self.assertEqual('refs/heads/master', _git('symbolic-ref', '-q', 'HEAD'))
        _git('branch', 'foo')
        self.repo.set_head('foo')
        self.assertEqual('refs/heads/foo', _git('symbolic-ref', '-q', 'HEAD'))

    def test_open_snapshot(self):
        self.write_file('file1')
        snapshot = self.repo.save()
        os.remove('file1')
        self.repo.open_snapshot(snapshot)
        self.assertTrue(os.path.exists('file1'))
        self.commit_file('file2', 'original')
        self.write_file('file2', 'changes')
        snapshot2 = self.repo.save()
        _git('add', '--all', '.')
        _git('reset', '--hard', 'HEAD')
        self.repo.discard_all()
        self.repo.open_snapshot(snapshot2)
        self.assertTrue(os.path.exists('file1'))
        with open('file2') as rfile:
            contents = rfile.read()
        self.assertEqual('changes', contents)

    def test_open(self):
        self.write_file('file1')
        snapshot = self.repo.save()
        os.remove('file1')
        self.repo.open(snapshot)
        self.assertTrue(os.path.exists('file1'))
        self.commit_file('file2', 'original')
        self.write_file('file2', 'changes')
        snapshot2 = self.repo.save()
        _git('add', '--all', '.')
        _git('reset', '--hard', 'HEAD')
        self.repo.discard_all()
        self.repo.open(snapshot2)
        self.assertTrue(os.path.exists('file1'))
        with open('file2') as rfile:
            contents = rfile.read()
        self.assertEqual('changes', contents)
        _git('add', '--all', '.')
        _git('reset', '--hard', 'HEAD')
        self.repo.open('master')
        self.assertEqual('refs/heads/master', _git('symbolic-ref', '-q', 'HEAD'))

# Use the GitRepoTestMixin to test GitExeRepo
class GitExeRepoTestCase(unittest.TestCase, SharedTestMixin):
    def setUp(self):
        self.create_temp_repo()
        self.repo = GitExeTwitRepo.from_cwd()
    def tearDown(self):
        self.cleanup_temp_repo()
