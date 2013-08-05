#!python

# This program is free software: you can redistribute it and/or modify it
# under the terms of the FOO licence. The full text of the licence can be
# found at http://osi.org/foo
#
# Kevin "X" Chang
#

"""
A command line wrapper for svn|git and upload.py (Mondrian/Rietveld). It
checks that code is LGTM'ed before allowing commit to svn. Also, it
indicates the size of the CL in the message.

Sample usage:
cr st
cr st --all

Use case 1 (local commit then diff against remote-branch):
vi file1.cc file2.java ...
cr commit -m "I changed file1.cc and file2.java to fix z problem." -a
cr diff origin/master  # just make sure this is what you want to review
cr mail -r joe origin/master
# wait for LGTM...
cr finish

Use case 2 (like Subversion and Perforce):
cr mail -r joe -m "Yo joe, I fixed z" [optional_file1 optional_file2 ...]
cr upload -r viral -m "Update fix, no email sent." --changelist issue123456
# wait for LGTM...
cr commit [--changelist issue123456]

Use case 3 (use hashes):
cr mail -r joe --rev b9319f:c82f04
cr upload -r joe --rev b9319f:ffff34 --changelist issue123456
# wait for LGTM
cr finish --rev b9319f:c82f04 --changelist issue123456
"""

import json
import logging
import optparse
import os
import re
import sys
import time
import urllib, urllib2

# import a third party HTML/XML in-memory parser
try:
    import BeautifulSoup
    if BeautifulSoup.__version__ < "3.2.0":
        raise ImportError("cr.py needs BeautifulSoup version 3.2.0 or higher")
except ImportError, err:
    print >> sys.stderr, err
    print "Maybe run: sudo apt-get install python-beautifulsoup"
    sys.exit(1)

try:
    import upload
except ImportError, err:
    print >> sys.stderr, ("%s. Please make sure Rietveld's upload.py "
                          "exists and is in the PYTHONPATH" % err)
from upload import (ErrorExit, RunShell, RunShellWithReturnCode, StatusUpdate)

# global configurations
SVN = "svn"
GIT = "git"
UPLOAD_CONTENT_TYPE = "application/x-www-form-urlencoded"
SERVER = os.environ.get('CR_SERVER', upload.DEFAULT_REVIEW_SERVER)
SUBJECT_HEADER = os.environ.get('CR_SUBJECT_HEADER', "")
SVN_REPOSITORY_URL = os.environ.get('CR_SVN_REPOSITORY_URL', None)
GIT_REPO_REGEX = os.environ.get('CR_GIT_REPO_REGEX', "__invalid_regex__")
GIT_HTTP_URL = os.environ.get('CR_GIT_HTTP_URL', "")
GIT_BASE_URL = os.environ.get('CR_GIT_BASE_URL', "")
logging.basicConfig(level=(logging.DEBUG if os.environ.get('DEBUG', None)
                           else logging.ERROR),
                    format=('%(asctime)s:%(levelname)s:'
                            '%(lineno)s: %(message)s'))
logger = logging.getLogger(__name__)


def RunShellWithLineCommand(cmds):
    if isinstance(cmds, basestring):
        cmds = [cmds]
    for cmd in cmds:
        print cmd
        cmds = cmd.split(' ')
        output, ret_code = RunShellWithReturnCode(cmds, print_output=True)
        if ret_code != 0:
            ErrorExit("Unable to execute '%s': %s" % (cmd, output))


class CrOptionParser(object):
    """ CR specific option parser """
    parser = optparse.OptionParser(usage="%prog [normal options]")
    parser.add_option("-v", "--verbose", action="store_true",
                      dest="verbose", default=False,
                      help="Print info level logs.")
    parser.add_option("--cl", "--changelist", action="store",
                      dest="changelist",
                      help="operate only on members of changelist ARG")
    parser.add_option("-m", "--message", action="store",
                      dest="message",
                      help="specify log message ARG")

    option_group = parser.add_option_group("Subversion options")
    option_group.add_option("--username", action="store", dest="username",
                            help="specify a username ARG")
    option_group.add_option("--password", action="store", dest="password",
                            help="specify a password ARG")

    option_group = parser.add_option_group("Mondrian options "
                                           "(from upload.py)")
    option_group.add_option("-e", "--email", action="store", dest="email",
                            metavar="EMAIL", default=None,
                            help="The username to use. "
                                 " Will prompt if omitted.")
    option_group.add_option("-R", "--rev", "--revision",
                            action="store", dest="revision",
                            metavar="REV", default=None,
                            help="Base SVN revision/branch/tree to diff "
                                 "against. Use rev1:rev2 range to review "
                                 "already committed change.")
    option_group.add_option("-r", "--reviewers", action="store",
                            dest="reviewers",
                            metavar="REVIEWERS", default=None,
                            help="Add reviewers (comma separated email "
                                 "addresses).")
    option_group.add_option("-c", "--cc", action="store", dest="cc",
                            metavar="CC", default=None,
                            help="Add CC (comma separated email addresses).")
    option_group.add_option("-i", "--issue", type="int", action="store",
                            metavar="ISSUE", default=None,
                            help="Issue number to which to add. "
                                 "Defaults to new issue.")
    parser.add_option("--force", action="store_true",
                      dest="force", default=False,
                      help="Force commit without LGTM. Use with care!")
    parser.add_option("--difffile", action="store",
                      dest="difffile", default=None,
                      help="Use user's own diff file for debugging purpose.")

class FileInfo(object):
    """ A simple container for file information. """
    def __init__(self, name, type, changelist):
        self.name = name
        # either '?', 'M', 'A', or some other vcs specific character
        self.type = type
        self.changelist = changelist
        self.branch_info = {}

    def setStatus(self, status):
        self.status = status

    def setChangelist(self, changelist):
        self.changelist = changelist

    def setBranchInfo(self, branch, value):
        """ Set the remote branch name. Branch is a concept in git. """
        self.branch_info[branch] = value

    def getBranchInfo(self, branch):
        """ Get the remote branch name. Branch is a concept in git. """
        return self.branch_info.get(branch, None)

    def __repr__(self):
        return ("{filename:%s, type:%s, cl:%s, br:%s}" %
                (self.name,
                 self.type,
                 self.changelist,
                 self.branch_info))


class FileGroupInfo(object):
    """ A container for files """
    TYPE_FILES = 'f'
    TYPE_BRANCH = 'b'
    def __init__(self, name,
                 type,
                 fileinfo_list=None,
                 remote_branch=None,
                 local_branch=None):
        self.name = name
        self.type = type
        self.fileinfo_list = fileinfo_list
        self.remote_branch = remote_branch
        self.local_branch = local_branch

    def appendFileInfo(self, file):
        self.fileinfo_list.append(file)

    def __repr__(self):
        if self.fileinfo_list:
            meta = self.fileinfo_list
        else:
            meta = getBranchPrintout(self.remote_branch, self.local_branch)
        return ("{filegroupname:%s, type:%s, info:%s}" %
                (self.name, self.type, meta))


class CrBaseVCS(object):
    """
    A base class that inherits GenerateDiff from upload.py. Also
    it include a few specific classes.
    """
    CMD = None

    @staticmethod
    def GetGoofySubjectHeader(diff):
        """ Given a diff, return a subject liner pertaining to the diff """
        add = 0
        sub = 0
        for l in diff.splitlines():
            if re.match(r'^\+[^\+]', l):
                add += 1
            if re.match(r'^\-[^\-]', l):
                sub += 1

        # "change_velocity" is an indicator as to how much the code changed.
        # One cannot simply use "lines_added" because there is a case such that
        # if one rearranges the entire code base without making a single
        # line modification, then lines_added would be 0!
        change_velocity = max(add, sub)

        # Real default goofy g4/gvn messages that were removed from Rietveld.
        # Adding it back with a twist for good humor.
        if change_velocity < 12:
            message = "A wee bit code review"
        elif change_velocity < 75:
            message = "A small code review"
        elif change_velocity < 250:
            message = "A medium code review"
        elif change_velocity < 750:
            message = "A large code review"
        elif change_velocity < 3000:
            message = "A titanic code review"
        elif change_velocity < 8000:
            message = "Yomama's huge ars code review"
        else:
            message = "Excessively fatty code review."
        return "%s%s +%d -%d." % (SUBJECT_HEADER, message, add, sub)

    @staticmethod
    def GetTriggerWarnings(diff):
        """See if there are any rules broken in the new check in"""
        # TODO(kevinx): deprecate this by mid Q2 2011 -- use lint programs.
        max_cols = {}
        known_languages = ('perl', 'python', 'java', 'others')

        # import default constraints from environment variables
        for language in known_languages:
            env_key = "CR_MAX_%s_COLS" % language.upper()
            if env_key in os.environ:
                max_cols[language] = int(os.environ[env_key])
            else:
                max_cols[language] = 80
        allow_tabs = True if ("CR_ALLOW_TABS" in os.environ and
                              os.environ["CR_ALLOW_TABS"] == "1") else False

        file_name = ''
        language = 'others'
        file_suffix_mapping = {'.py': 'python',
                               '.pl': 'perl',
                               '.pm': 'perl',
                               '.java': 'java'}
        warn_msg = []
        err_msg = []
        for line in diff.splitlines():
            # TODO(kevinx): This is for svn. Take care of git.
            m = re.match(r'^Index: (.+(\.\w+))$', line, re.IGNORECASE)
            if m:
                file_name = m.group(1)
                file_suffix = m.group(2).lower()
                if file_suffix in file_suffix_mapping:
                    language = file_suffix_mapping[file_suffix]
                else:
                    language = 'others'
                # only check constraints for new lines
            if not re.match(r"\+[^\+]", line):
                continue
            if (not allow_tabs
                    and re.search(r"\t", line)
                    and re.match(r'Makefile$', file_name, re.IGNORECASE)):
                line = line.replace("\t", "[BADTAB]")
                err_msg.append("Tab detected(%s):%s" % (file_name, line))
            if len(line) - 1 > max_cols[language]:
                warn_msg.append("Exceed %d cols(%s):%s" %
                                (max_cols[language], file_name, line))
        return err_msg, warn_msg

    def getBaseUrl(self, branch=None):
        """ For certain vcs like git, we need to get the base url manually """
        return None

    def executeAllCmd(self, argv):
        """ Pass all the commands to the vcs """
        cmd = [self.CMD]
        cmd.extend(argv)
        vcs_st, ret_code = RunShellWithReturnCode(cmd, print_output=True)
        sys.exit(ret_code)

    def executeStatus(self, prog, argv):
        """ Simple 'svn st' or 'git status' command """
        raise NotImplementedError("Please override executeStatus")

    def commitAndGetMessage(self,
                            changelist_name,
                            approval_message,
                            mondrian_description,
                            force=False):
        """ Commit files to repository """
        raise NotImplementedError("Please override commit")

    def removeChangelist(self, changelist):
        raise NotImplementedError("Please override removeChangelist")

    def getFileGroupInfo(self, changelist=None, opt_files=[]):
        """
        Return an assocative array of changelist -> FileGroupInfo
        """
        raise NotImplementedError("Please override getFileGroupInfo")

    def moveFilesToChangelist(self, file_list, changelist_name):
        raise NotImplementedError("Please override moveFilesToChangelist")


class SubversionVCS(CrBaseVCS, upload.SubversionVCS):
    """ Inherit both CrBaseVCS and the base SubversionVCS """
    CMD = SVN

    def GenerateDiff(self, args, options=None):
        """ Override the original version to check for more errors """
        diff_output = super(SubversionVCS, self).GenerateDiff(args)

        # "svn diff" version 1.6 or below has a bug where moved files 
        # (the ones with a "+" symbol) do not show up on diff. The only
        # way for them to show up is if the content also changed.
        files_in_diff = {}
        for line in diff_output.splitlines():
            m = re.match(r"Index: (.+)", line)
            if m:
                filename = m.group(1)
                files_in_diff[filename] = True
        err_msgs = []
        for filename in files_in_diff.keys():
            if not os.path.islink(filename) and not filename in files_in_diff:
                err_msgs.append("Error: 'svn diff' failed to diff '%s'" %
                                filename)
        if err_msgs:
            ErrorExit("""
%s

Note that svn 1.6.x and below has a bug where moved files (the ones with
'+' symbol) do not show up on diff. Until svn 1.7.x is released, you can
get around the problem by adding a bogus empty line to these files.""" %
                      "\n".join(err_msgs))
        return diff_output

    def executeStatus(self, prog, argv):
        """ svn status """
        cmd = [SVN, "st"]
        if len(argv) > 0:
            cmd.extend(argv)
        _vcs_st, _ret_code = RunShellWithReturnCode(cmd, print_output=True)

    def commitAndGetMessage(self,
                            changelist_name,
                            approval_message,
                            mondrian_description,
                            force=False):
        message = mondrian_description + " " + approval_message
        cmd = [SVN, "commit", "--changelist", changelist_name,
               "--message", message]
        logger.debug("CMD: " + str(cmd))
        vcs_st, ret_code = RunShellWithReturnCode(cmd, print_output=True)
        if ret_code != 0:
            ErrorExit("Unable to commit changelist '%s'..." % changelist_name)
        m = re.search(r'Committed revision (\d+)', vcs_st)
        if not m:
            ErrorExit("Unable to find revision number in svn commit!")
        revision = m.group(1)

        commit_message = "Committed revision %s." % revision
        if SVN_REPOSITORY_URL:
            commit_message += " " + re.sub(r'%d', revision, SVN_REPOSITORY_URL)
        return commit_message + "\n" + message

    def removeChangelist(self, changelist):
        # TODO(kevinx): add removeChangelist for svn
        pass

    def getFileGroupInfo(self, changelist=None, opt_files=None):
        """
        Return a list of changelist groups and FileInfo objects
        from 'svn st'. Note that the first group may not have
        associated changelist name (None).
        """
        if opt_files is None:
            opt_files = []
        if changelist:
            opt_files.extend(["--changelist", changelist])
        cmd = [SVN, "status"]
        if len(opt_files) > 0:
            cmd.extend(opt_files)
        vcs_st, ret_code = RunShellWithReturnCode(cmd)
        if ret_code > 0:
            ErrorExit("Unable to execute svn")

        changelist_to_filegroupinfo = {}
        changelist = None
        file_info_list = []
        for line in vcs_st.splitlines():
            m = re.match(r"^\-\-\- Changelist '([\w\-]+)':", line)
            if m:
                if len(file_info_list) > 0:
                    changelist_to_filegroupinfo[changelist] = (
                        FileGroupInfo(name=changelist,
                                      type=FileGroupInfo.TYPE_FILES,
                                      fileinfo_list=file_info_list))
                changelist = m.group(1)
                file_info_list = []
                continue

            if line and len(line) > 8 and line[0] != " " and line[0] != "":
                file_name = line[8:]
                file_type = line[0]
                file_info_list.append(
                    FileInfo(file_name, file_type, changelist))

        if len(file_info_list) > 0:
            changelist_to_filegroupinfo[changelist] = (
                FileGroupInfo(name=changelist,
                              type=FileGroupInfo.TYPE_FILES,
                              fileinfo_list=file_info_list))

        return changelist_to_filegroupinfo

    def moveFilesToChangelist(self, file_list, changelist_name):
        """ Move a bunch of files to a changelist using svn native command """
        if len(file_list) and self.options.changelist is None:
            cmd = [SVN, "changelist", changelist_name]
            cmd.extend(file_list)
            print("Moving files [%s] into changelist '%s'..." %
                  (", ".join(file_list), changelist_name))
            svn_cl, ret_code = RunShellWithReturnCode(cmd, print_output=True)
            if ret_code != 0:
                cmd = [SVN, "changelist", "--remove"]
                cmd.extend(file_list)
                RunShell(cmd)
                ErrorExit("Unable to move files %s into changelist '%s'" %
                          (", ".join(file_list), changelist_name))


class GitVCS(CrBaseVCS, upload.GitVCS):
    """ Inherit both CrBaseVCS and the base GitVCS """

    # constants
    CMD = GIT
    STAGED = '__staged__'
    WORKING = '__working__'

    def __init__(self, options):
        pwd, git_dir = self._getGitDir()
        self.pwd = pwd
        super(GitVCS, self).__init__(options)

    def _generateDiff(self, extra_args):
        """ This is from upload.py except that '--cached' is taken out """
        #extra_args = extra_args[:]
        if self.options.revision:
            if ":" in self.options.revision:
                extra_args = self.options.revision.split(":", 1) + extra_args
            else:
                extra_args = [self.options.revision] + extra_args
        env = os.environ.copy()
        if 'GIT_EXTERNAL_DIFF' in env:
            del env['GIT_EXTERNAL_DIFF']
        # Changed(open42): removed --cached to allow branches
        return RunShell([GIT, "diff", "--no-ext-diff", "--no-color",
                         "--full-index", "-M"]
                        + extra_args, env=env)

    def GenerateDiff(self, args, options=None):
        """
        Override the default git GenerateDiff and add HEAD to ensure that
        both staging and working files are included.
        """
        logger.debug("Generating diff for %s" % args)

        # get a diff of staging and working files
        full_branch, _, _, remote_branch, _, _ = self._getCurrentGitInfo()
        return self._generateDiff([remote_branch, full_branch, '--'])
        #return self._generateDiff(['HEAD', '--'] + args)

    def getBaseUrl(self, branch=None):
        base_url, _ = self._getGitHttpUrlInfo(branch=branch)
        return base_url

    def executeStatus(self, prog, argv):
        """ Execute status """
        cmd = [GIT, 'status']
        cmd.extend(argv)
        logger.debug("%% %s" % cmd)
        _vcs_st, _ret_code = RunShellWithReturnCode(cmd, print_output=True)

    def commitAndGetMessage(self,
                            changelist,
                            approval_message,
                            mondrian_description,
                            force=False):
        """ Commit from HEAD to origin/master """
        full_branch, issue, current_branch, remote_branch, branches, _ = (
            self._getCurrentGitInfo())

        commit_log = self._getGitCommitLogList(rev_from=remote_branch,
                                               rev_to=full_branch)
        if len(commit_log) == 0:
            ErrorExit("The commit log from %s..%s is empty!" %
                      (remote_branch, 'HEAD'))

        # Amend the last git message to contain approver name.
        _git_subject = commit_log[0][1].decode('utf-8')
        _git_subject = re.sub(u'^[\u2713|\u2717]+', '', _git_subject)
        _git_subject = (u'\u2713' if not force else u'\u2717') + _git_subject
        git_body = commit_log[0][2]
        git_body = re.sub("\s*\(Code-reviewer.+\)\s*", '', git_body)
        git_body = re.sub(r'[\n\s\r]+$', '', git_body)
        git_body = approval_message + '\n\n' + git_body

        new_msg = _git_subject + '\n\n' + git_body
        cmd = [GIT, "commit", "--amend", "-m", new_msg]
        mondrian_commit_msg, ret_code = (
            RunShellWithReturnCode(cmd, print_output=False))
        if ret_code != 0:
            ErrorExit("Unable to execute '%s' to amend message..." % cmd)

        remote_name = remote_branch.split("/")[-1]
        cmd_args = {'prog': GIT,
                    'current_branch': full_branch,
                    'remote_branch': remote_branch,
                    'remote_name': remote_name}

        if current_branch == 'master':
            print("You are on master. "
                  "It's safer/cleaner if you branch next time.")
        else:
            pass
            #"%(prog)s merge --no-ff --log %(current_branch)s"])

        RunShellWithLineCommand(
            ["%(prog)s fetch" % cmd_args,
             "%(prog)s rebase %(remote_branch)s" % cmd_args])

        # refetch git commit log because the hash IDs may have changed
        commit_log = self._getGitCommitLogList(rev_from=remote_branch,
                                               rev_to=full_branch)
        if len(commit_log) == 0:
            ErrorExit('Commit log from %s..%s is empty!' %
                      (full_branch, remote_branch))

        # pull from remotes/origin/master, and then merge (may change hash IDs)
        if current_branch == 'master':
            RunShellWithLineCommand(
                ['%(prog)s push origin %(remote_name)s' % cmd_args])
        else:
            RunShellWithLineCommand(
                ['%(prog)s push origin'
                 ' %(current_branch)s:%(remote_name)s' % cmd_args])
            print """Suggested commands to sync and clean up branch manually:
%(prog)s checkout master; %(prog)s rebase origin/master;
%(prog)s branch -d %(current_branch)s""" % cmd_args

        # Retrieve committed messages from git (git_hash & desc), and then
        # generate the final message to be posted on Mondrian.
        mondrian_msgs = []
        for git_hash, desc, git_body in commit_log:
            _, url = self._getGitHttpUrlInfo(githash=git_hash,
                                             branch=remote_branch)
            mondrian_msgs.append((url if url else git_hash) + '\n' +
                                 desc + git_body)
        return "\n".join(mondrian_msgs)

    def removeChangelist(self, changelist):
        print "You may optionally remove your changelist"

    def getFileGroupInfo(self, changelist=None, opt_files=None):
        """ Implemention of changelist on top of git """
        # TODO(kevinx): deprecate this if possible!
        if opt_files is None:
            opt_files = []
        # populate working-and-staged files in the changelist
        changelist_to_filegroupinfo = {}
        (full_branch, issue, current_branch, remote_branch,
         branches, git_fileinfo) = self._getCurrentGitInfo()
        logger.debug("git_fileinfo:%s" % git_fileinfo)

        cmd = [GIT, "diff", "--name-only", "--no-color",
               remote_branch, full_branch]
        for fname in RunShell(cmd, silent_ok=True).splitlines():
            if fname not in git_fileinfo:
                git_fileinfo[fname] = (
                    FileInfo(fname, type=None, changelist=full_branch))
                git_fileinfo[fname].setBranchInfo(remote_branch, '*')

        changelist_to_filegroupinfo[full_branch] = (
            FileGroupInfo(name=full_branch,
                          type=FileGroupInfo.TYPE_BRANCH,
                          remote_branch=remote_branch,
                          local_branch=full_branch))

        return changelist_to_filegroupinfo

    # branch specific functions:

    def renameGitBranchWithIssueNum(self,
                                    issue_str,
                                    local_branch,
                                    remote_branch):
        new_branch_name = ('{issue_str}#'
                           '{remote_branch}#{local_branch}'.format(
                               issue_str=issue_str,
                               local_branch=local_branch,
                               remote_branch=remote_branch))
        RunShellWithReturnCode([GIT, 'br', '-m', local_branch,
                                new_branch_name])

    # private functions:

    def _getGitDir(self):
        git_dir = pwd = os.getcwd()
        while git_dir:
            if os.path.exists("%s/.git" % git_dir):
                break
            git_dir = os.path.dirname(git_dir)
        if not git_dir:
            ErrorExit("Path %s is not a valid git directory" % pwd)
        git_dir += "/.git"
        return pwd, git_dir

    def _getCurrentGitInfo(self):
        """
        Get current path, git path, branch name. This is done via
        'git status --porcelain', 'git diff --name-only --no-color',
        and 'git branch -a --no-color'
        Note that git_fileinfo is keyed by the file name, then the
        branch name.
        """
        branches = []
        full_branch, issue, current_branch, remote_branch = (
            None, None, None, 'remotes/origin/master')
        cmd = [GIT, "branch", "-a", "--no-color"]
        for branch in RunShell(cmd).splitlines():
            # Take care of "remotes/origin/HEAD -> origin/master"
            branch = re.sub(r' \-\> .+$', '', branch)
            m = re.match(r'^(\*)?\s+(.+)', branch)
            if not m:
                ErrorExit("Unable to parse branch output: '%s'" % branch)
            branches.append(m.group(2))
            if m.group(1) == '*':
                full_branch = current_branch = m.group(2)
        if not full_branch:
            ErrorExit("Unable to determine current branch (git branch)")

        if len(full_branch.split('#')) >= 3:
            issue, remote_branch, current_branch = full_branch.split('#', 2)

        git_fileinfo = {}
        cmd = [GIT, "status", "--porcelain"]
        for line in RunShell(cmd, silent_ok=True).splitlines():
            m = re.match('^(.)(.) (.+)', line)
            if not m:
                ErrorExit("Unable to parse 'status --porcelain' line: %s" %
                          line)
            stage, working, fname = m.group(1, 2, 3)
            if fname not in git_fileinfo:
                filetype = '?' if (stage != ' ' or working != ' ') else None
                git_fileinfo[fname] = (
                    FileInfo(fname, type=filetype, changelist=None))
            if stage != ' ':
                git_fileinfo[fname].setBranchInfo(GitVCS.STAGED, stage)
            if working != ' ':
                git_fileinfo[fname].setBranchInfo(GitVCS.WORKING, working)

        return (full_branch, issue, current_branch, remote_branch,
                branches, git_fileinfo)

    def _getGitHttpUrlInfo(self, githash=None, branch=""):
        """
        Return base URL and the hashed URL (for the current path).
        The base URL is used for display purpose, so that the reviewer
        knows whether the code review is for master, production, or
        some other branch.
        """
        cmd = [GIT, 'remote', '-vv']
        output, ret_code = RunShellWithReturnCode(cmd, print_output=False)
        if ret_code != 0:
            ErrorExit("Unable to execute '%s'" % cmd)

        m = re.search(GIT_REPO_REGEX, output)
        if not m:
            return None, None
        repo = m.group(1)

        if not githash:
            cmd = [GIT, 'log', '-1', '--pretty=%H']
            githash, ret_code = RunShellWithReturnCode(cmd, print_output=False)
            if ret_code != 0:
                ErrorExit("Unable to execute '%s'" % cmd)

        _vars = {'repo': repo, 'hash': githash.strip()}
        git_base_url = GIT_BASE_URL % _vars

        if branch and branch.startswith('remotes/origin'):
            branch = re.sub('^remotes/origin', '', branch)
            # TODO(kevinx): the notion of /tree is GitHub specific.
            #               Make it generic
            git_base_url += "/tree" + branch

        return git_base_url, GIT_HTTP_URL % _vars

    def _getGitCommitLogList(self, rev_from=None, rev_to=None):
        """
        Return commit messages in the [[hash,desc,body], ...] format.
        """
        ID, DELIM = '__#id#__:', '__#delim#__'

        def get_commit_comments(git_log, include_body=True):
            comments = []
            for line in git_log.splitlines():
                idx = len(comments) - 1
                if re.match('^' + ID, line):
                    line = re.sub(r'^' + ID, '', line)
                    hashtag, subject, body = line.split(DELIM)
                    comments.append([hashtag, subject, ''])
                    if include_body:

                        if comments[idx][2] != '':
                            comments[idx][2] += '\n'
                        comments[idx][2] += body
                elif len(comments) > 0 and include_body:
                    comments[idx][2] += '\n' + line
            return comments

        logger.debug("FROM:%s TO:%s" % (rev_from, rev_to))
        if rev_from and rev_to:
            cmd = [GIT, "log", "%s..%s" % (rev_from, rev_to),
                   "--pretty={ID}%H{DELIM}%s{DELIM}%b".format(
                       ID=ID, DELIM=DELIM)]
            git_log, ret_code = RunShellWithReturnCode(cmd, print_output=False)
            if ret_code != 0:
                ErrorExit("Unable to execute '%s' to get hash + subj..." % cmd)
            return get_commit_comments(git_log)

        cmd = [GIT, "status"]
        git_status, ret_code = RunShellWithReturnCode(cmd, print_output=False)
        if ret_code != 0:
            ErrorExit("Unable to execute '%s' to get branch status" % cmd)

        commit_match = re.search('Your branch is ahead .+ by (\d+) commit',
                                 git_status)
        if commit_match:
            branch_ahead = int(commit_match.group(1))
            cmd = [GIT, "log", "-%d" % branch_ahead,
                   "--pretty={ID}%H{DELIM}%s{DELIM}%b".format(
                       ID=ID, DELIM=DELIM)]
            git_log, ret_code = RunShellWithReturnCode(cmd, print_output=False)
            if ret_code != 0:
                ErrorExit("Unable to execute '%s' to get hash + subj..." % cmd)
            return get_commit_comments(git_log)

        return []


def getBranchPrintout(remote_branch, local_branch):
    return "'%s' (in the context of '%s')" % (local_branch, remote_branch)


def ParseUserArguments(vcs, opt_svn_revision, opt_changelist, opt_files):
    """
    Given an optional changelist and optional files, return:
    1) changelist and 2) fileinfo_list
    """

    if opt_svn_revision:
        return (opt_changelist if opt_changelist else "issue",
                [])  # fileinfo_list,

    changelist_to_filegroupinfo = (
        vcs.getFileGroupInfo(changelist=opt_changelist))
    logger.debug("changelist_to_filegroupinfo:%s" % (
        changelist_to_filegroupinfo))

    # The user will either want to commit files (defined by fileinfo_list),
    # or upload to branch (defined by remote_branch & local_branch).
    cl = None

    if opt_changelist:
        # user defined changelist, return the right group
        cl = opt_changelist
        filegroup_info = changelist_to_filegroupinfo.get(cl, None)
        if not filegroup_info:
            ErrorExit("Unable to find changelist '%s'" % cl)
    elif len(opt_files) == 0:
        # if no file and no branch specified by the user, pick a file group
        cl_len = len(changelist_to_filegroupinfo)
        if None in changelist_to_filegroupinfo:
            cl_len -= 1
        logger.debug("changelist length:%d" % cl_len)
        if cl_len == 1:
            # there is exactly one active changelist
            if None in changelist_to_filegroupinfo:
                changelist_to_filegroupinfo.pop(None)
            cl = changelist_to_filegroupinfo.keys()[0]
            filegroup_info = changelist_to_filegroupinfo[cl]
        elif cl_len == 0:
            if None in changelist_to_filegroupinfo:
                # There is 1 group without a name, so assign a temporary issue
                # (and later on move all these files into
                # changelist 'issue<Issue>')
                cl = "issue"
                filegroup_info = changelist_to_filegroupinfo[None]
            else:
                ErrorExit("Unable to find a suitable changelist to use.")
        else:
            # there are more than 1 changelists, the user needs to be explicit
            ErrorExit("There is more than one changelist or branch. Please\n"
                      "specify one of the followings as --changelist"
                      " [short: --cl]:\n%s" %
                      [cl.encode('utf-8')
                       for cl in changelist_to_filegroupinfo.keys()
                       if cl])
    else:
        # the user specified one or more files
        # user has provided files
        cl = "issue"
        filegroup_info = (
            FileGroupInfo(name=cl,
                          type=FileGroupInfo.TYPE_FILES,
                          fileinfo_list=[FileInfo(fn, type='*',
                                                  changelist=cl)
                                         for fn in opt_files]))

    return (cl, filegroup_info.fileinfo_list)


def fetchContentFromUrl(rpc_server, url):
    """ Generic url fetch function """
    tries = 3
    while tries > 0:
        try:
            return rpc_server.Send(url)
        except urllib2.HTTPError, e:
            err_html = e.read()
            if (e.code == 404 or e.msg == 'Not found' or
                    re.match(r'No issue exists', err_html, re.IGNORECASE)):
                ErrorExit("Error in http://%s%s, maybe it is closed?\n%s" %
                          (SERVER, url, err_html))
            print("Error %d in 'http://%s%s', retrying..." %
                  (e.code, SERVER, url))
            time.sleep(1)
            tries -= 1
        except urllib2.URLError, e:
            ErrorExit("Unable to find server '%s' (%s)" % (SERVER, str(e)))
        except Exception, e:
            ErrorExit("Fatal error in http://%s%s:\n%s" %
                      (SERVER, url, str(e)))
    if tries == 0:
        ErrorExit("Failed fetching from 'http://%s%s'" % (SERVER, url))


def getRawHTMLMessagesFromMondrian(html, api_data):
    """
    Extract messages from the Mondrian server. To get xsrf token,
    perform an ugly HTML extraction which depends on the structure of
    the template file located in .../codereview/templates/issue.html
    """
    # JS contains: var xsrfToken = '477a06484acae6831a9dba8a17771eed';
    m = re.search(r"xsrfToken\s+=\s+['\"](\w+)['\"]", html, re.IGNORECASE)
    xsrfToken = m.group(1) if m else ""

    #{"description": "Msg is here", "created": "2011-03-30 01:46:45.196263",
    # "cc": [], "reviewers": ["kevinx@open42.com"],
    # "owner_email": "kevinx@open42.com", "patchsets": [1],
    # "modified": "2011-03-30 01:46:45.352817", "private": false,
    # "base_url": "", "closed": false, "owner": "kevinx", "issue": 6165030,
    # "subject": "[Code review] A wee bit code review +1 -0. Msg is here"}

    #{"files": {"aa": {"status": "M", "no_base_file": false,
    # "is_binary": false, "num_chunks": 1, "num_added": 1, "num_removed": 0,
    # "id": 2}}, "created": "2011-03-30 01:46:45.214304", "url": null,
    # "num_comments": 0, "owner_email": "kevinx@open42.com",
    # "modified": "2011-03-30 01:46:45.214671", "owner": "kevinx",
    # "patchset": 1, "issue": 6165030, "message": null}
    api_data = json.loads(api_data)

    # passing convertEntities converts &amp;&quote --> &" ascii
    souped_body = BeautifulSoup.BeautifulSoup(
        html, convertEntities=BeautifulSoup.BeautifulSoup.HTML_ENTITIES)

    raw_msgs = []
    messages = souped_body.findAll('div',
                                   attrs={'id': re.compile('msg\d+'),
                                          'name': re.compile('\d+')})
    for message in messages:
        commenter = None
        ago = None
        info_td = (message.div.table.tr.findAll('td'))
        if info_td and len(info_td) >= 3:
            ago = info_td[3].getText(separator=" ")
            commenter = info_td[0].getText(separator=" ")

        msg_text_body = message.find('div', {'class': 'message-body'})
        if msg_text_body:
            msg_text = msg_text_body.getText(separator=" ")
        else:
            msg_text = ""
        msg_text = msg_text.encode('utf-8')
        raw_msgs.append({'commenter': str(commenter.strip()),
                         'ago': str(ago.strip()),
                         'msg': str(msg_text.strip())})

    return {'title': api_data['subject'].encode('utf-8'),
            'description': api_data['description'].encode('utf-8'),
            'xsrfToken': xsrfToken,
            'messages': raw_msgs}


def printCrHelp(prog, vcs_cmd):
    global SVN, GIT
    help_params = {'prog': prog, 'cl': 'issue6415002',
                   'files': "file1.py file2.pl ..."}
    print("""Sample %(prog)s specific commands:
# Mail -> Receive LGTM -> Commit:
%(prog)s mail -r guido     -m "My new feature"
%(prog)s mail -r larry     -m "My new feature" .
%(prog)s mail -r kernighan -m "My new feature" file1.py file2.c ...
%(prog)s mail -r billjoy   -m "My new feature" --changelist %(cl)s
%(prog)s mail -m "Change 1 ..." --changelist %(cl)s   # re-upload, mail
...
%(prog)s upload -m "Change 2 ..." --changelist %(cl)s # re-upload, no mail
...
%(prog)s finish
%(prog)s finish --changelist %(cl)s

# Normal UNIX diff:
%(prog)s diff
%(prog)s diff %(files)s
%(prog)s diff --changelist %(cl)s""" % help_params)

    if vcs_cmd == GIT:
        print("""%(prog)s diff origin/master master
%(prog)s diff remotes/origin/master master""" % help_params)

    if vcs_cmd == SVN:
        print("\n# Add file(s) into the %(prog)s repository:" % help_params)
    elif vcs_cmd == GIT:
        print("\n# Add working file(s) into %(prog)s staging area:" %
              help_params)
    print("%(prog)s add %(files)s" % help_params)

    printChangelistHelp(prog, vcs_cmd)


def printChangelistHelp(prog, vcs_cmd):
    help_params = {'prog': prog, 'cl': 'issue6172002',
                   'files': "file1.py file2.pl ..."}
    print("""# Add file(s) into changelist '%(cl)s':
%(prog)s changelist %(cl)s %(files)s
# Remove file(s) from changelist:
%(prog)s changelist --remove %(files)s""" % help_params)
    if vcs_cmd == SVN:
        print("%(prog)s changelist --remove "
              "--changelist %(cl)s --depth infinity .""" % help_params)
    elif vcs_cmd == GIT:
        print("%(prog)s changelist --remove "
              "--changelist %(cl)s""" % help_params)


def executeUploadPy(vcs,
                    options,
                    files,
                    base_url=None,
                    first_upload=False):
    """ Function that calls the upload.py code. """
    # default arguments to use on upload.py
    upload_argv = ['upload.py', '--assume_yes']

    if base_url:
        upload_argv.extend(["--base_url", base_url])

    logger.debug("executeUploadPy files:%s" % files)
    diff_data = vcs.GenerateDiff(files, options=options)
    err_msg, warn_msg = vcs.GetTriggerWarnings(diff_data)
    if err_msg:
        print "Error: you must fix problematic code below:"
        print "\n".join(err_msg)
    if warn_msg:
        print "Warnings: I highly suggest you clean up the code below:"
        print "\n".join(warn_msg)
    if err_msg:
        sys.exit(1)

    if options.changelist:
        m = re.match(r'issue(\d+)', options.changelist)
        if m:
            issue_num = int(m.group(1))
            upload_argv.extend(["-i", issue_num])

    if options.message:
        if len(options.message) < 5:
            ErrorExit("Message too short. Please add a more informative "
                      "--message")
        upload_argv.extend(["-d", options.message])
        message = options.message
    else:
        message = ""
    if first_upload:
        goofy_subject = CrBaseVCS.GetGoofySubjectHeader(diff_data) + " "
    else:
        goofy_subject = ""

    upload_subject = (goofy_subject + message).replace('\n', ' ')[0:100]
    upload_argv.extend(["-m", upload_subject])

    # setup arguments for upload.py
    if options.verbose:
        upload_argv.append("--verbose")
    if options.email:
        upload_argv.extend(["-e", options.email])
    else:
        last_email_file_name = os.path.expanduser(
            "~/.last_codereview_email_address")
        if os.path.exists(last_email_file_name):
            try:
                last_email_file = open(last_email_file_name, "r")
                last_email = last_email_file.readline().strip("\n")
                last_email_file.close()
                upload_argv.extend(["-e", last_email])
            except IOError, e:
                ErrorExit("Error reading ~/.last_codereview_email_address %s" %
                          e)

    if options.send_mail:
        upload_argv.extend(["--send_mail"])
    if options.reviewers:
        upload_argv.extend(["-r", options.reviewers])
    cc = []
    if options.cc:
        cc.append(options.cc)
    if 'CR_DEFAULT_CC' in os.environ:
        cc.append(os.environ['CR_DEFAULT_CC'])
    if cc:
        upload_argv.append("--cc=" + ",".join(cc))

    upload_argv = [str(a) for a in upload_argv]
    logger.debug("Executing: %s" % str(upload_argv))
    return upload.RealMain(upload_argv, diff_data)


def executeIssueNumberAndUpload(vcs, prog, argv,
                                send_mail=False,
                                quiet=False):
    """
    Take care of various commit cases:
    cr mail [-m 'comment here...'] -r kevinx origin/master
    cr mail [-m 'comment here...'] -r kevinx <file1, ...>
    cr mail [-m 'comment here...'] --changelist <CL>
    cr mail --changelist <CL>
    """

    options, user_files = CrOptionParser.parser.parse_args(argv)

    # One of the most common problems is that people confuse
    # --cc with -cc (invalid):
    if options.cc:
        if 'c' in options.cc.split(","):
            ErrorExit("Do you mean to use --cc instead of -cc?"
                      " (-cc really means --cc=c)")

    options.send_mail = send_mail    # pass on --send_mail option to upload.py

    # If using Git but no message provided, then check for already
    # committed messages (thus passing --message is not necessary).
    remote_branch = current_branch = cl = None
    fileinfo_list = []
    if vcs.CMD == SVN:
        cl, fileinfo_list = ParseUserArguments(
                vcs, options.revision, options.changelist, user_files)

    elif vcs.CMD == GIT:
        RunShellWithLineCommand('git fetch')
        cl, issue, current_branch, remote_branch, _branches, _git_fileinfo = (
            vcs._getCurrentGitInfo())
        RunShellWithLineCommand('git rebase %s' % remote_branch)

        rev_from, rev_to = remote_branch, cl
        gitCommitLogList = vcs._getGitCommitLogList(rev_from=rev_from,
                                                    rev_to=rev_to)
        commit_length = len(gitCommitLogList)
        if commit_length == 0:
            ErrorExit("Please commit your code (in branch '%s') first." % cl)

        message = options.message
        if not message:
            message = ''
        else:
            message = re.sub(r'[\.\s]+$', '', message) + '.'
            message += '\n'
        for i, commit_log in enumerate(reversed(gitCommitLogList)):
            _githash, desc, body = commit_log
            message += ('%d) ' % (i + 1)) if commit_length > 1 else ''
            message += desc
            message += ('\n' + body) if body else ''
            message = re.sub(r'[\.\s]+$', '', message) + '.'
            if i + 1 != commit_length:
                message += '\n'
        options.message = message

    if not options.message:
        CrOptionParser.parser.error("Please specify --message [short: -m]")

    logger.debug("executeIssueNumberAndUpload options:%s, user_files:%s" %
                 (options, user_files))

    if options.revision:
        file_list = []
        base_url = "hash:unknown"
    elif remote_branch or current_branch:
        # this is only for git
        file_list = [remote_branch, current_branch]
        base_url = vcs.getBaseUrl(branch=remote_branch)
    elif fileinfo_list:
        # svn (not for git)
        file_list = [f.name for f in fileinfo_list if f.type != '?']
        logger.debug("cl:%s, file_list:%s" % (cl, file_list))
        if len(file_list) > 0:
            print "Moving %s to changelist '%s'" % (file_list, cl)
            vcs.moveFilesToChangelist(file_list, cl)
        base_url = None
    else:
        # FIXME(kevinx): unknown problem, fix
        ErrorExit("Fatal error, nothing changed.")

    m = re.match(r'issue(\d+)', cl)
    if m:
        issue_num = int(m.group(1))
        print("Re-uploading to issue %d..." % issue_num),
        options.changelist = cl
        issue_str, patchset = executeUploadPy(vcs,
                                              options,
                                              files=file_list,
                                              base_url=base_url,
                                              first_upload=False)
        assert(int(issue_str) == issue_num)
        if not quiet:
            finish_options = (
                '[--changelist {cl}] [-m \"Comments\"]'.format(cl=cl)
                if vcs.CMD != GIT else '')
            print("""Patched '{patch}' to {issue_num}.
After your reviewer provides a final LGTM at the URL
(http://{server}/{issue_num}) then you can finish submitting your code:
% {prog} finish {finish_options}

If you need to re-update more patches to {issue_num}, execute:
% {prog} mail {reviewer_options}[-m \"my patch message\"]""".format(
                prog=prog,
                patch=patchset,
                server=SERVER,
                issue_num=issue_num,
                reviewer_options=(
                    '-r %s ' % options.reviewers if options.reviewers
                    else '[-r reviewer1,reviewer2,...] '),
                finish_options=finish_options))
        return None, None

    if not options.reviewers:
        ErrorExit("Please specify at least one --reviewer [short: -r]")

    print "Uploading diff and creating a new issue number on %s..." % SERVER
    issue_str, patchset = executeUploadPy(vcs,
                                          options,
                                          files=file_list,
                                          base_url=base_url,
                                          first_upload=True)

    new_cl = "issue" + issue_str
    if remote_branch and current_branch:
        # for git, rename the branch name with issue number
        if not re.match(r'^issue\d+#[\w\-]+#[\w\-]+#', cl):
            print("Moving git branch %s to issue%s" %
                  (getBranchPrintout(remote_branch, current_branch),
                   issue_str))
            vcs.renameGitBranchWithIssueNum('issue' + issue_str,
                                            current_branch, remote_branch)
    elif file_list:
        # subversion
        if cl != 'issue':
            new_cl = "issue%s-%s" % (issue_str, cl)
        print "Moving %s to new changelist '%s'" % (file_list, new_cl)
        vcs.moveFilesToChangelist(file_list, new_cl)

    if options.force:
        print('Skipping code review process [--force]')
    else:
        print ""
        if not send_mail:
            print("Note: %s did not automatically send email to '%s'." %
                  (prog, options.reviewers))

        finish_options = (('--changelist %s [-m "Comments"]' % new_cl)
                          if vcs.CMD != GIT else '')
        print("""To re-submit your updates, execute:
% {prog} mail -r {reviewers} [-m \"my patch message\"]"

When %s reviews your code and provides an LGTM, submit via:
% {prog} finish {finish_options}""".format(prog=prog,
                                           reviewers=options.reviewers,
                                           finish_options=finish_options))

    return issue_str, patchset


def executeCheckIn(vcs, prog, argv):
    """
    Take care of various commit cases. Make sure to check for LGTM:
    cr finish [-m "comment here..."]
    cr finish [-m "comment here..."] .
    cr finish [-m "comment here..."] --changelist <CL>
    cr finish --changelist <CL>
    """
    options, user_files = CrOptionParser.parser.parse_args(argv)

    if len(user_files) == 1 and user_files[0] == ".":
        user_files = []
    elif len(user_files) > 0:
        pass

    logger.debug("Files/branch to commit: %s" % user_files)

    if options.revision:
        changelist_to_filegroupinfo = {}
    else:
        # below verifies that the changelist exists (value not used)
        changelist_to_filegroupinfo = (
            vcs.getFileGroupInfo(changelist=options.changelist,
                                 opt_files=user_files))

    cl, fileinfo_list = (
        ParseUserArguments(
            vcs, options.revision, options.changelist, user_files))

    if cl == "issue" and not options.force:
        ErrorExit("""You should not commit blindly without a code review. Try:
%(prog)s mail -r johnsmith -m "My new feature X" .
%(prog)s mail -r johnsmith -m "My new feature Y" lib/AdBlender 
%(prog)s mail -r johnsmith --changelist <changelist_name>
...
%(prog)s changelist <changelist_name> file1 file2 ...
...
# Emergency only:
%(prog)s finish -r johnsmith -m "My urgent change" [file1 ...] --force """ %
                  {'prog': prog})

    m = re.match(r'issue(\d+)', cl) if cl else None
    if m:
        issue_num = int(m.group(1))
    else:
        # If the changelist does not begin with "issue<num>", then it is
        # not yet uploaded to Mondrian.
        if options.force:
            # If user forces a commit, then call executeIssueNumberAndUpload
            # which renames the changelist to be "issue<num>", uploads code to
            # Mondrian, and return a issue number. This is done for the
            # convenience of the --force.
            issue_str, patchset = executeIssueNumberAndUpload(vcs, prog, argv)
            issue_num = int(issue_str)
            if vcs.CMD == SVN:
                cl = "issue" + issue_str
            elif vcs.CMD == GIT:
                (cl, issue,
                 _current_branch, _remote_branch, _branches,
                 _gitfileinfo) = vcs._getCurrentGitInfo()
        else:
            ErrorExit("The changelist '%s' is not approved for check in.\n" %
                      cl +
                      "Make sure to run "
                      "\"%s mail -r <reviewer> -m 'comment'\"\n" % prog +
                      "If this is an emergency, use the --force.")

    rpc_server = upload.GetRpcServer(SERVER,
                                     host_override=None,
                                     save_cookies=True,
                                     account_type=upload.AUTH_ACCOUNT_TYPE)

    # Step 1: fetch the status of the issue on Mondrian
    if not options.force:
        print("Checking for LGTM status from %s for changelist '%s'... " %
              (SERVER, cl))

    html = fetchContentFromUrl(rpc_server, "/%d" % issue_num)
    api_data = fetchContentFromUrl(rpc_server, "/api/%d" % issue_num)
    mondrian_page_info = getRawHTMLMessagesFromMondrian(html, api_data)

    if options.message:
        mondrian_description = options.message
    else:
        if mondrian_page_info['description']:
            mondrian_description = mondrian_page_info['description']
        else:
            mondrian_description = mondrian_page_info['title']

    logger.debug("Original mondrian info:" + str(mondrian_page_info))

    approval_message = ""
    if options.force:
        approvers = ["UNAPPROVED"]
        lgtm_ago = "now"
        approval_message = "FORCE CHECK IN"
    else:
        approvers = []
        lgtm_ago = None
        mondrian_msgs = mondrian_page_info['messages']
        for msg_pack in mondrian_msgs:
            msg = msg_pack['msg']
            logger.debug("...msg:'%s'" % msg)
            if re.search(r"LGTM|LTGM|looks good to me", msg, re.IGNORECASE):
                _approver = msg_pack['commenter']
                if _approver == "me" and os.environ.get('LGTM', None) is None:
                    print("You should not LGTM or solicit LGTM in "
                          "your own comment!")
                else:
                    if _approver not in approvers:
                        approvers.append(_approver)
                    lgtm_ago = msg_pack['ago']
                    approval_message = "LGTM'ed"

    if not lgtm_ago:
        ErrorExit("Sorry, changelist '%s' does not yet have an LGTM.\n" % cl +
                  "Please ask your reviewer to review: http://%s/%d\n" %
                  (SERVER, issue_num) +
                  "If this is an emergency, use the --force to commit.")

    # Step 2: upload the last version to Mondrian
    if not options.force:
        if '-m' not in argv and '--message' not in argv:
            argv.extend(['--message', "Final version."])
        if '--cl' not in argv and '--changelist' not in argv:
            argv.extend(['--changelist', cl])
        _issue_str, _patchset = executeIssueNumberAndUpload(vcs, prog, argv,
                                                            send_mail=False,
                                                            quiet=True)

    # Step 3: if LGTM'ed then commit to vcs. Note that in
    # Subversion the '[' and ']' characters are reserved to enclose
    # notifications for issue tracking systems 
    approval_message = ("(Code-reviewer:%s %s %s. http://%s/%d)" %
                        (",".join(approvers),
                         approval_message,
                         lgtm_ago,
                         SERVER,
                         issue_num))
    print("Committing changelist '%s' approved by %s (%s)..." %
          (cl, ",".join(approvers), lgtm_ago))
    # normal changelist that exists
    vcs_message = vcs.commitAndGetMessage(cl,
                                          approval_message,
                                          mondrian_description,
                                          force=options.force)

    # Step 4: auto close the issue on Mondrian if it's been LGTM'ed
    if options.force:
        print "Warning: issue not automatically closed because of --force."
    else:
        url = "/%d/close" % issue_num
        pload = "xsrf_token=" + mondrian_page_info['xsrfToken']
        close_html = rpc_server.Send(url,
                                     content_type=UPLOAD_CONTENT_TYPE,
                                     payload=pload)
        # make sure the string is in sync with
        # .../codereview/views.py (def close(...))
        if re.search(r'Closed', html, re.IGNORECASE):
            print("Closed issue '%d' on http://%s/%d" %
                  (issue_num, SERVER, issue_num))
        else:
            ErrorExit("Unable to close issue '%d' on http://%s%s:\n%s" %
                      (issue_num, SERVER, url, close_html))

    # Step 5: post a message to Mondrian that it is committed
    url = "/%d/publish" % issue_num
    pload_arr = {'xsrf_token': mondrian_page_info['xsrfToken'],
                 'subject': mondrian_page_info['title'],
                 'message': vcs_message,
                 'send_mail': 0,
                 'message_only': 1}
    pload = urllib.urlencode(pload_arr)
    update_html = "Fatal error: Mondrian did not return a 302"

    tries = 3
    while tries > 0:
        try:
            logger.debug("Uploading to %s ..." % url)
            update_html = rpc_server.Send(url,
                                          content_type=UPLOAD_CONTENT_TYPE,
                                          payload=pload,
                                          request_password_if_302=False)
        except urllib2.HTTPError, e:
            # This is the actual, expected behavior because on Mondrian,
            # a succ post to message returns a 302 back to the site.
            update_html = e.msg
            if e.code == 302 or re.search(r'Found', update_html,
                                          re.IGNORECASE):
                print("Published commit information to http://%s/%d" %
                      (SERVER, issue_num))
                vcs.removeChangelist(cl)
                return
        print("Error posting a commit message to "
              "http://%s%s, trying again..." % (SERVER, url))
        time.sleep(2)
        tries -= 1

    ErrorExit("Unable to publish '%s' to http://%s%s:\n%s" %
              (vcs_message, SERVER, url, update_html))


def getVcsArgsAndRemnantArgs(argv):
    """
    Get two lists of arguments:
    1) VCS args as defined in upload.parser
    2) Remnant args
    """
    # expand the form "--key=value" into "--key", "value"
    _argv = []
    for arg in argv:
        m = re.search("(\-+\d+)=(.+)", arg)
        if m:
            _argv.extend(m.group(1, 2))
        else:
            _argv.append(arg)

    # generate two arg lists, one to pass to vcs, the other one is remnant
    vcs_args = []
    remnant_args = []
    i = 0
    while i < len(_argv):
        arg = _argv[i]
        if upload.parser.has_option(arg):
            args = vcs_args
        else:
            args = remnant_args
        args.append(arg)

        i += 1
        next_arg = argv[i] if i < len(argv) else None
        if next_arg and not re.match('^\-', next_arg):
            args.append(next_arg)
            i += 1

    return vcs_args, remnant_args


def Main(argv):
    if '--verbose' in argv:
        logger.getLogger().setLevel(logger.DEBUG)
        argv.remove('--verbose')
        argv.append('--verbose')

    if 'CR' in os.environ:
        prog = os.environ['CR']    # another script calls this script
    else:
        prog = os.path.basename(argv[0])

    #vcs_args, remnant_args = getVcsArgsAndRemnantArgs(argv[1:])
    #vcs_options, args = upload.parser.parse_args(vcs_args)
    vcs_options, args = upload.parser.parse_args([])

    def GetVCS(options):
        """ Factory: return a VCS object (GitVCS, SubversionVCS, etc...) """
        (vcs, extra_output) = upload.GuessVCSName(options)
        if vcs == upload.VCS_SUBVERSION:
            return SubversionVCS(options)
        elif vcs == upload.VCS_GIT:
            return GitVCS(options)
        raise NotImplementedError("Unimplemented VCS %s" % vcs)
    vcs = GetVCS(vcs_options)

    if len(argv) == 1:
        printCrHelp(prog, vcs.CMD)
        sys.exit(1)

    cmd = argv[1]
    if cmd in ['st', 'stat', 'status'] or cmd in ['op', 'opened']:
        # 'opened' is for people who are used to Perforce
        vcs.executeStatus(prog, argv[2:])
    elif cmd == 'mail' or cmd == 'ma':
        executeIssueNumberAndUpload(vcs, prog, argv[2:], send_mail=True)
    elif cmd == 'upload':
        executeIssueNumberAndUpload(vcs, prog, argv[2:], send_mail=False)
    elif 'difffile' in cmd:
        # debugging the upload capability of upload.py
        options, _ = CrOptionParser.parser.parse_args(argv[3:])
        options.difffile = argv[2]
        options.send_mail = True
        executeUploadPy(vcs, options, ['__debug__'], first_upload=True)
        print("Debug: uploaded your own diff file '%s'."
              " Now go check the upload." %
              options.difffile)
    elif cmd == 'help':
        vcs_cmd = [vcs.CMD, "help"]
        vcs_cmd.extend(argv[2:])
        output, ret_code = RunShellWithReturnCode(vcs_cmd)
        output = re.sub(r'Subversion is a tool.+\n.+', "", output,
                        re.MULTILINE)
        output = output.replace("svn", prog).replace("Subversion", prog)
        output = output.replace("git", prog).replace("Git", prog)
        output = output.rstrip()
        print output
        if len(argv[2:]) == 0:
            print "\n"
            printCrHelp(prog, vcs.CMD)
            sys.exit(1)
    elif vcs.CMD == GIT and cmd in ['commit', 'ci']:
        # for git, commit is a passthrough command
        vcs.executeAllCmd(['commit'] + argv[2:])
    elif cmd in ['commit', 'ci', 'finish']:
        executeCheckIn(vcs, prog, argv[2:])
    elif vcs.CMD == GIT and cmd in ['br', 'branch']:
        vcs.parseGitBranchOptions(argv[2:])
    elif vcs.CMD == GIT and cmd in ['cl', 'changelist']:
        ErrorExit("Sorry, git does not have a changelist option like svn.")
    elif vcs.CMD == GIT and cmd == 'co':
        vcs.executeAllCmd(['checkout'] + argv[2:])
    else:
        vcs.executeAllCmd(argv[1:])


if __name__ == "__main__":
    try:
        Main(sys.argv)
    except KeyboardInterrupt:
        print
        StatusUpdate("Interrupted.")
        sys.exit(1)
