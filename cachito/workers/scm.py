# SPDX-License-Identifier: GPL-3.0-or-later
import glob
import logging
import os
from abc import ABC, abstractmethod
import urllib.parse
import tarfile
import tempfile

import git

from cachito.errors import CachitoError
from cachito.workers.config import get_worker_config


log = logging.getLogger(__name__)


class SCM(ABC):
    """The base class for interacting with source control."""

    def __init__(self, url, ref):
        """
        Initialize the SCM class.

        :param str url: the source control URL to the repository to fetch
        :param str ref: the source control reference
        """
        super().__init__()
        self.url = url
        self.ref = ref
        self._archives_dir = None
        self._archive_path = None
        self._repo_name = None
        self._package_dir = None

    @property
    def archive_name(self):
        """
        Get what the archive name should be for a particular SCM reference.

        :return: the archive name
        :rtype: str
        """
        return f'{self.ref}.tar.gz'

    @property
    def archive_path(self):
        """
        Get the path to where the archive for a particular SCM reference should be.

        :return: the path to the archive
        :rtype: str
        """
        if not self._archive_path:
            self._archive_path = os.path.join(self.package_dir, self.archive_name)

        return self._archive_path

    @property
    def archives_dir(self):
        """
        Get the absolute path of the archives directory from the Celery configuration.

        :returns: the absolute path of the archives directory
        :rtype: str
        """
        if not self._archives_dir:
            self._archives_dir = os.path.abspath(
                get_worker_config().cachito_sources_dir
            )
            log.debug('Using "%s" as the archives directory', self._archives_dir)

        return self._archives_dir

    @property
    def package_dir(self):
        """
        Get the directory for the source archive of this package.

        :return: the path to the directory of the source archive
        :rtype: str
        """
        if self._package_dir is None:
            self._package_dir = os.path.join(self.archives_dir, *self.repo_name.split('/'))
            # Create the directories if they don't exist
            os.makedirs(self._package_dir, exist_ok=True)
        return self._package_dir

    @abstractmethod
    def fetch_source(self):
        """
        Fetch the repo, create a compressed tar file, and put it in long-term storage.
        """

    @property
    @abstractmethod
    def repo_name(self):
        """
        Determine the repo name based on the URL
        """


class Git(SCM):
    """The git implementation of interacting with source control."""

    def _reset_git_head(self, repo):
        """
        Reset HEAD to a specific Git reference.

        :param git.Repo repo: the repository object.
        :raises CachitoError: if changing the HEAD of the repository fails.
        """
        try:
            repo.head.reference = repo.commit(self.ref)
            repo.head.reset(index=True, working_tree=True)
        except:  # noqa E722
            log.exception('Checking out the Git ref "%s" failed', self.ref)
            raise CachitoError(
                'Checking out the Git repository failed. Please verify the supplied reference '
                f'of "{self.ref}" is valid.'
            )

    def _create_archive(self, from_dir):
        """
        Create an archive from a specified directory.

        :param str from_dir: path to a directory from where to create the archive.
        """
        log.debug('Creating the archive at %s', self.archive_path)
        with tarfile.open(self.archive_path, mode='w:gz') as bundle_archive:
            bundle_archive.add(from_dir, 'app')

    def clone_and_archive(self):
        """
        Clone the git repository and create the compressed source archive.

        :raises CachitoError: if cloning the repository fails or if the archive can't be created
        """
        with tempfile.TemporaryDirectory(prefix='cachito-') as temp_dir:
            log.debug('Cloning the Git repository from %s', self.url)
            # Don't allow git to prompt for a username if we don't have access
            os.environ['GIT_TERMINAL_PROMPT'] = '0'
            clone_path = os.path.join(temp_dir, 'repo')
            try:
                repo = git.repo.Repo.clone_from(self.url, clone_path, no_checkout=True)
            except:  # noqa E722
                log.exception('Cloning the Git repository from %s failed', self.url)
                raise CachitoError('Cloning the Git repository failed')

            self._reset_git_head(repo)
            self._create_archive(repo.working_dir)

    def update_and_archive(self, previous_archive):
        """
        Pull the latest Git history inside an existing archive and create the
        compressed source archive.

        :param str previous_archive: path to an archive file created before.
        :raises CachitoError: if pulling the Git history from the remote repo or
            the checkout of the target Git ref fails.
        """
        with tempfile.TemporaryDirectory(prefix='cachito-') as temp_dir:
            with tarfile.open(previous_archive, mode='r:gz') as tar:
                tar.extractall(temp_dir)

            repo = git.Repo(os.path.join(temp_dir, 'app'))
            try:
                repo.remote().fetch()
            except:  # noqa E722
                log.exception('Failed to fetch from remote %s', self.url)
                raise CachitoError('Failed to fetch from the remote Git repository')

            self._reset_git_head(repo)
            self._create_archive(repo.working_dir)

    def fetch_source(self):
        """
        Fetch the repo, create a compressed tar file, and put it in long-term storage.
        """
        # If it already exists and isn't corrupt, don't download it again
        if os.path.exists(self.archive_path) and tarfile.is_tarfile(self.archive_path):
            log.debug('The archive already exists at "%s"', self.archive_path)
            return

        # Find a previous archive created by a previous request
        #
        # The previous archive does not mean the one just before the request that
        # schedules current task. The only reason for finding out such a file is
        # to access the git history. So, anyone is ok.
        previous_archive = max(
            glob.glob(os.path.join(self.package_dir, '*.tar.gz')),
            key=os.path.getctime,
            default=None,
        )

        if previous_archive:
            self.update_and_archive(previous_archive)
        else:
            self.clone_and_archive()

    @property
    def repo_name(self):
        """
        Determine the repo name based on the URL
        """
        if not self._repo_name:
            parsed_url = urllib.parse.urlparse(self.url)
            repo = parsed_url.path.strip('/')
            if repo.endswith('.git'):
                repo = repo[: -len('.git')]
            self._repo_name = repo
            log.debug('Parsed the repository name "%s" from %s', self._repo_name, self.url)

        return self._repo_name
