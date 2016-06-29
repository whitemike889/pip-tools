# coding: utf-8
from __future__ import (absolute_import, division, print_function,
                        unicode_literals)

import os
from shutil import rmtree

from pip.index import PackageFinder
from pip.req.req_set import RequirementSet

from ..cache import CACHE_DIR
from ..exceptions import NoCandidateFound
from ..utils import (lookup_table,
                     make_install_requirement, pip_version_info)
from .base import BaseRepository

try:
    from tempfile import TemporaryDirectory  # added in 3.2
except ImportError:
    from .._compat import TemporaryDirectory


class PyPIRepository(BaseRepository):
    DEFAULT_INDEX_URL = 'https://pypi.python.org/simple'

    """
    The PyPIRepository will use the provided Finder instance to lookup
    packages.  Typically, it looks up packages on PyPI (the default implicit
    config), but any other PyPI mirror can be used if index_urls is
    changed/configured on the Finder.
    """
    def __init__(self, pip_options, session):
        self.session = session

        index_urls = [pip_options.index_url] + pip_options.extra_index_urls
        if pip_options.no_index:
            index_urls = []

        self.finder = PackageFinder(
            find_links=pip_options.find_links,
            index_urls=index_urls,
            trusted_hosts=pip_options.trusted_hosts,
            allow_all_prereleases=pip_options.pre,
            process_dependency_links=pip_options.process_dependency_links,
            session=self.session,
        )

        # Caches
        # stores project_name => InstallationCandidate mappings for all
        # versions reported by PyPI, so we only have to ask once for each
        # project
        self._available_candidates_cache = {}

        # Cache for get_dependencies.
        self._get_dependencies_cache = {}

        # Setup file paths
        self.freshen_build_caches()
        self._download_dir = os.path.join(CACHE_DIR, 'pkgs')
        self._wheel_download_dir = os.path.join(CACHE_DIR, 'wheels')

        # References to old build dirs (to prevent too early pruning).
        self._old_build_dirs = []

    def freshen_build_caches(self):
        """
        Start with fresh build/source caches.  Will remove any old build
        caches from disk automatically.
        """
        # Keep old build dirs around: they are required by
        # format_requirement/get_src_requirement.
        if hasattr(self, '_build_dir'):
            self._old_build_dirs.append(self._build_dir)
        if hasattr(self, '_source_dir'):
            self._old_build_dirs.append(self._source_dir)

        self._build_dir = TemporaryDirectory('build')
        self._source_dir = TemporaryDirectory('source')

    @property
    def build_dir(self):
        return self._build_dir.name

    @property
    def source_dir(self):
        return self._source_dir.name

    def clear_caches(self):
        rmtree(self._download_dir, ignore_errors=True)
        rmtree(self._wheel_download_dir, ignore_errors=True)

    def find_all_candidates(self, req_name):
        if req_name not in self._available_candidates_cache:
            # pip 8 changed the internal API, making this a public method
            if pip_version_info >= (8, 0):
                candidates = self.finder.find_all_candidates(req_name)
            else:
                candidates = self.finder._find_all_versions(req_name)
            self._available_candidates_cache[req_name] = candidates
        return self._available_candidates_cache[req_name]

    def find_best_match(self, ireq, prereleases=None):
        """
        Returns a Version object that indicates the best match for the given
        InstallRequirement according to the external repository.
        """
        if ireq.editable or ireq.link:
            return ireq  # return itself as the best match

        all_candidates = self.find_all_candidates(ireq.name)
        candidates_by_version = lookup_table(all_candidates, key=lambda c: c.version, unique=True)
        matching_versions = ireq.specifier.filter((candidate.version for candidate in all_candidates),
                                                  prereleases=prereleases)

        # Reuses pip's internal candidate sort key to sort
        matching_candidates = [candidates_by_version[ver] for ver in matching_versions]
        if not matching_candidates:
            raise NoCandidateFound(ireq, all_candidates)
        best_candidate = max(matching_candidates, key=self.finder._candidate_sort_key)

        # Turn the candidate into a pinned InstallRequirement
        return make_install_requirement(
            best_candidate.project, best_candidate.version, ireq.extras
        )

    def get_dependencies(self, ireq):
        """
        Given a pinned or an editable InstallRequirement, returns a set of
        dependencies (also InstallRequirements, but not necessarily pinned).
        They indicate the secondary dependencies for the given requirement.
        """

        if ireq in self._get_dependencies_cache:
            return self._get_dependencies_cache[ireq]

        if not os.path.isdir(self._download_dir):
            os.makedirs(self._download_dir)
        if not os.path.isdir(self._wheel_download_dir):
            os.makedirs(self._wheel_download_dir)

        reqset = RequirementSet(self.build_dir,
                                self.source_dir,
                                # Pass no download_dir for editables, otherwise
                                # pip uses "export" instead of "obtain"
                                # (dropping the VCS revision info).
                                download_dir=None if ireq.editable else self._download_dir,
                                wheel_download_dir=self._wheel_download_dir,
                                # Ignore installed packages: This is required
                                # for the source_dir of VCS URLs to be created.
                                ignore_installed=True,
                                session=self.session)
        dependencies = reqset._prepare_file(self.finder, ireq)
        self._get_dependencies_cache[ireq] = set(dependencies)
        return self._get_dependencies_cache[ireq]
