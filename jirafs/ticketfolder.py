import datetime
import fnmatch
import json
import logging
import os
import re
import subprocess

from jira.resources import Issue
import six

from . import constants
from . import migrations
from .exceptions import (
    CannotInferTicketNumberFromFolderName,
    NotTicketFolderException
)
from .rstfieldmanager import RSTFieldManager


logger = logging.getLogger(__name__)


class TicketFolder(object):
    def __init__(self, path, jira, migrate=True):
        self.path = os.path.realpath(
            os.path.expanduser(path)
        )
        self.get_jira = jira

        if not os.path.isdir(self.metadata_dir):
            raise NotTicketFolderException(
                "%s is not a synchronizable ticket folder" % (
                    path
                )
            )

        self.ticket_number = self.infer_ticket_number()
        if migrate:
            self.run_migrations()

        # If no `new_comment.jira.txt` file exists, let's create one
        comment_path = self.get_local_path(constants.TICKET_NEW_COMMENT)
        if not os.path.exists(comment_path):
            with open(comment_path, 'w') as out:
                out.write('')

    @property
    def jira(self):
        if not hasattr(self, '_jira'):
            self._jira = self.get_jira()
        return self._jira

    @property
    def issue(self):
        if not hasattr(self, '_issue'):
            self._issue = self.jira.issue(self.ticket_number)
        return self._issue

    def store_cached_issue(self):
        storable = {
            'options': self.issue._options,
            'raw': self.issue.raw
        }
        with open(self.get_metadata_path('issue.json'), 'w') as out:
            out.write(json.dumps(storable))

    @property
    def cached_issue(self):
        if not hasattr(self, '_cached_issue'):
            try:
                issue_path = self.get_metadata_path('issue.json')
                with open(issue_path, 'r') as _in:
                    storable = json.loads(_in.read())
                    self._cached_issue = Issue(
                        storable['options'],
                        None,
                        storable['raw'],
                    )
            except IOError:
                self.log(
                    'Error encountered while loading cached issue!',
                    level=logging.ERROR,
                )
                self._cached_issue = self.issue
        return self._cached_issue

    @property
    def metadata_dir(self):
        return os.path.join(
            self.path,
            constants.METADATA_DIR,
        )

    @property
    def git_merge_base(self):
        return self.run_git_command(
            'merge-base', 'master', 'jira',
        )

    def infer_ticket_number(self):
        raw_number = self.path.split('/')[-1:][0].upper()
        if not re.match('^\w+-\d+$', raw_number):
            raise CannotInferTicketNumberFromFolderName(
                "Cannot infer ticket number from folder %s. Please name "
                "ticket folders after the ticket they represent." % (
                    self.path,
                )
            )
        return raw_number

    def get_metadata_path(self, filename):
        return os.path.join(
            self.metadata_dir,
            filename
        )

    def get_remote_file_metadata(self):
        remote_files = self.get_shadow_path('.jirafs/remote_files.json')
        try:
            with open(remote_files, 'r') as _in:
                return json.loads(_in.read())
        except IOError:
            return {}

    def set_remote_file_metadata(self, data):
        remote_files = self.get_shadow_path('.jirafs/remote_files.json')
        with open(remote_files, 'w') as out:
            out.write(
                json.dumps(data)
            )

    def get_local_path(self, filename):
        return os.path.join(
            self.path,
            filename
        )

    def get_shadow_path(self, filename):
        return os.path.join(
            self.get_metadata_path('shadow'),
            filename,
        )

    @property
    def version(self):
        try:
            with open(self.get_metadata_path('version'), 'r') as _in:
                return int(_in.read().strip())
        except IOError:
            return 1

    @property
    def log_path(self):
        return self.get_metadata_path(constants.TICKET_OPERATION_LOG)

    @classmethod
    def initialize_ticket_folder(cls, path, jira):
        path = os.path.realpath(path)

        metadata_path = os.path.join(
            path,
            constants.METADATA_DIR,
        )
        os.mkdir(metadata_path)

        # Create bare git repository so we can easily detect changes.
        excludes_path = os.path.join(metadata_path, 'gitignore')
        with open(excludes_path, 'w') as gitignore:
            gitignore.write(
                '%s\n' % constants.METADATA_DIR,
            )

        subprocess.check_call(
            (
                'git',
                '--bare',
                'init',
                os.path.join(
                    metadata_path,
                    'git',
                )
            ),
            stdout=subprocess.PIPE
        )
        subprocess.check_call((
            'git',
            'config',
            '--file=%s' % os.path.join(
                metadata_path,
                'git',
                'config'
            ),
            'core.excludesfile',
            excludes_path
        ))

        instance = cls(path, jira, migrate=False)
        instance.log(
            'Ticket folder for issue %s created at %s',
            (instance.ticket_number, instance.path, )
        )
        instance.run_git_command(
            'commit', '--allow-empty', '-m', 'Initialized'
        )
        instance.run_migrations(silent=True)

        comment_path = instance.get_local_path(constants.TICKET_NEW_COMMENT)
        with open(comment_path, 'w') as out:
            out.write('')

        return instance

    @classmethod
    def clone(cls, path, jira):
        path = os.path.realpath(path)
        os.mkdir(path)
        folder = cls.initialize_ticket_folder(path, jira)
        folder.sync()
        return folder

    def run_git_command(self, *args, **kwargs):
        failure_ok = kwargs.get('failure_ok', False)
        shadow = kwargs.get('shadow', False)

        if not shadow:
            work_tree = self.path,
            git_dir = self.get_metadata_path('git')
        else:
            work_tree = self.get_metadata_path('shadow')
            git_dir = self.get_metadata_path('shadow/.git')

        cmd = [
            'git',
            '--work-tree=%s' % work_tree,
            '--git-dir=%s' % git_dir,
        ]
        cmd.extend(args)

        self.log('Executing git command %s', (cmd, ), logging.DEBUG)
        try:
            return subprocess.check_output(
                cmd,
                stderr=subprocess.PIPE
            ).decode('utf-8').strip()
        except subprocess.CalledProcessError:
            if not failure_ok:
                raise

    def get_local_file_at_revision(self, path, revision, failure_ok=True):
        return self.run_git_command(
            'show', '%s:%s' % (
                revision,
                path,
            ),
            failure_ok=failure_ok
        )

    def get_ignore_globs(self, which=constants.IGNORE_FILE):
        all_globs = [
            constants.TICKET_DETAILS,
            constants.TICKET_COMMENTS,
            constants.TICKET_NEW_COMMENT,
        ]
        for field in constants.FILE_FIELDS:
            all_globs.append(
                constants.TICKET_FILE_FIELD_TEMPLATE.format(field_name=field)
            )

        def get_globs_from_file(input_file):
            globs = []
            for line in input_file.readlines():
                if line.startswith('#') or not line.strip():
                    continue
                globs.append(line.strip())
            return globs

        try:
            with open(self.get_local_path(which)) as local_ign:
                all_globs.extend(
                    get_globs_from_file(local_ign)
                )
        except IOError:
            pass

        try:
            with open(os.path.expanduser('~/%s' % which)) as global_ignores:
                all_globs.extend(
                    get_globs_from_file(global_ignores)
                )
        except IOError:
            pass

        return all_globs

    def file_matches_globs(self, filename, ignore_globs):
        for glob in ignore_globs:
            if fnmatch.fnmatch(filename, glob):
                return True
        return False

    def get_locally_changed(self):
        ignore_globs = self.get_ignore_globs()

        new_files = self.run_git_command(
            'ls-files', '-o', failure_ok=True
        ).split('\n')
        modified_files = self.run_git_command(
            'ls-files', '-m', failure_ok=True
        ).split('\n')

        all_possible = [
            filename for filename in new_files + modified_files if filename
        ]

        assets = []
        for filename in all_possible:
            if self.file_matches_globs(filename, ignore_globs):
                continue
            if not os.path.isfile(os.path.join(self.path, filename)):
                continue
            if filename.startswith('.'):
                continue
            assets.append(filename)

        return assets

    def get_remotely_changed(self):
        ignore_globs = self.get_ignore_globs(constants.REMOTE_IGNORE_FILE)
        metadata = self.get_remote_file_metadata()

        assets = []
        for attachment in self.issue.fields.attachment:
            matches_globs = (
                self.file_matches_globs(attachment.filename, ignore_globs)
            )
            changed = metadata.get(attachment.filename) != attachment.created
            if not matches_globs and changed:
                assets.append(attachment.filename)
        return assets

    def get_local_fields(self):
        return RSTFieldManager.create(
            self,
            path=self.path,
        )

    def get_original_values(self):
        return RSTFieldManager.create(
            self,
            revision=self.git_merge_base,
        )

    def get_local_differing_fields(self):
        """ Get fields that differ between local and the last sync

        .. warning::

           Does not support setting fields that were not set originally
           in a sync operation!

        """
        local_fields = self.get_local_fields()
        original_values = self.get_original_values()

        differing = {}
        for k, v in original_values.items():
            if local_fields.get(k) != v:
                differing[k] = (v, local_fields.get(k), )

        return differing

    def get_new_comment(self, clear=False):
        try:
            with open(
                self.get_local_path(constants.TICKET_NEW_COMMENT), 'r+'
            ) as c:
                comment = c.read().strip()
                if clear:
                    c.seek(0)
                    c.truncate()

            return comment
        except IOError:
            return ''

    def fetch(self):
        file_meta = self.get_remote_file_metadata()

        for filename in self.get_remotely_changed():
            for attachment in self.issue.fields.attachment:
                if attachment.filename == filename:
                    shadow_filename = self.get_shadow_path(filename)
                    with open(shadow_filename, 'wb') as download:
                        self.log(
                            'Download file "%s"',
                            (attachment.filename, ),
                        )
                        file_meta[filename] = attachment.created
                        download.write(attachment.get())

        self.set_remote_file_metadata(file_meta)

        with open(self.get_shadow_path(constants.TICKET_DETAILS), 'w') as dets:
            for field in sorted(self.issue.raw['fields'].keys()):
                value = getattr(self.issue.fields, field)
                if isinstance(value, six.string_types):
                    value = value.replace('\r\n', '\n').strip()
                elif value is None:
                    value = ''
                elif field in constants.NO_DETAIL_FIELDS:
                    continue

                if not isinstance(value, six.string_types):
                    value = six.text_type(value)

                if field in constants.FILE_FIELDS:
                    # Write specific fields to their own files without
                    # significant alteration

                    file_field_path = self.get_shadow_path(
                        constants.TICKET_FILE_FIELD_TEMPLATE
                    ).format(field_name=field)
                    with open(file_field_path, 'w') as file_field_file:
                        file_field_file.write(value)
                        file_field_file.write('\n')  # For unix' sake
                else:
                    # Normal fields, though, just go into the standard
                    # fields file.
                    if value is None:
                        continue
                    elif field in constants.NO_DETAIL_FIELDS:
                        continue

                    dets.write('%s::\n\n' % field)
                    for line in value.replace('\r\n', '\n').split('\n'):
                        dets.write('    %s\n' % line)
                    dets.write('\n')

        comments_filename = self.get_shadow_path(constants.TICKET_COMMENTS)
        with open(comments_filename, 'w') as comm:
            for comment in self.issue.fields.comment.comments:
                comm.write('%s: %s::\n\n' % (comment.created, comment.author))
                lines = comment.body.replace('\r\n', '\n').split('\n')
                for line in lines:
                    comm.write('    %s\n' % line)
                comm.write('\n')

        self.store_cached_issue()

        self.run_git_command('add', '-A', shadow=True)
        self.run_git_command(
            'commit', '-m', 'Pulled remote changes',
            failure_ok=True, shadow=True
        )
        self.run_git_command('push', 'origin', 'jira', shadow=True)

    def merge(self):
        self.run_git_command('stash', '--include-untracked', failure_ok=True)
        self.run_git_command('merge', 'jira')
        self.run_git_command('stash', 'pop', failure_ok=True)

    def pull(self):
        self.fetch()
        self.merge()

    def push(self):
        status = self.status()

        file_meta = self.get_remote_file_metadata()

        for filename in status['to_upload']:
            with open(self.get_local_path(filename), 'rb') as upload:
                self.log(
                    'Uploading file "%s"',
                    (filename, ),
                )
                # Delete the existing issue if there is one
                for attachment in self.issue.fields.attachment:
                    if attachment.filename == filename:
                        attachment.delete()
                attachment = self.jira.add_attachment(
                    self.ticket_number,
                    upload
                )
                file_meta[filename] = attachment.created

        comment = self.get_new_comment(clear=True)
        if comment:
            self.log('Adding comment "%s"' % comment)
            self.jira.add_comment(self.ticket_number, comment)

        collected_updates = {}
        for field, diff_values in status['local_differs'].items():
            collected_updates[field] = diff_values[1]

        if collected_updates:
            self.log(
                'Updating fields "%s"',
                (collected_updates, )
            )
            self.issue.update(**collected_updates)

        # Commit local copy
        self.run_git_command('add', '-A', failure_ok=True)
        self.run_git_command(
            'commit', '-m', 'Pushed local changes', failure_ok=True
        )

        # Commit changes to remote copy, too, so we record remote
        # file metadata.
        self.run_git_command('fetch', shadow=True)
        self.run_git_command('merge', 'master')
        self.set_remote_file_metadata(file_meta)
        self.run_git_command('add', '-A', shadow=True)
        self.run_git_command(
            'commit', '-m', 'Pulled remote changes',
            failure_ok=True, shadow=True
        )
        self.run_git_command('push', 'origin', 'jira', shadow=True)

    def sync(self):
        self.pull()
        self.push()

    def status(self):
        locally_changed = self.get_locally_changed()

        status = {
            'to_upload': locally_changed,
            'local_differs': self.get_local_differing_fields(),
            'new_comment': self.get_new_comment()
        }

        return status

    def run_migrations(self, silent=False):
        loglevel = logging.INFO
        if silent:
            loglevel = logging.DEBUG
        while self.version < constants.CURRENT_REPO_VERSION:
            migrator = getattr(
                migrations,
                'migration_%s' % str(self.version + 1).zfill(4)
            )
            self.migrate(migrator, loglevel=loglevel)

    def migrate(self, migrator, loglevel=logging.INFO):
        self.log('%s: Migration started', (migrator.__name__, ), loglevel)
        migrator(self)
        self.log('%s: Migration finished', (migrator.__name__, ), loglevel)

    def log(self, message, args=None, level=logging.INFO):
        if args is None:
            args = []
        logger.log(level, message, *args)
        with open(self.log_path, 'a') as log_file:
            log_file.write(
                "%s\t%s\t%s\n" % (
                    datetime.datetime.utcnow().isoformat(),
                    logging.getLevelName(level),
                    (message % args).replace('\n', '\\n')
                )
            )
        if level >= logging.INFO:
            print(
                "[%s %s] %s" % (
                    logging.getLevelName(level),
                    self.issue,
                    message % args
                )
            )

    def get_log(self):
        with open(self.log_path, 'r') as log_file:
            return log_file.read()
