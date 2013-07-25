@echo off
set CR=%0
set CR_SUBJECT_HEADER="[Code review] "
set CR_DEFAULT_CC="developers@__MY_DOMAIN__"
set CR_MAX_PERL_COLS=89
set CR_MAX_PYTHON_COLS=79
set CR_MAX_JAVA_COLS=79
set CR_MAX_OTHERS_COLS=1000
set CR_ALLOW_TABS=0

set CR_SVN_REPOSITORY_URL='http://codesion.com/.../...'
REM export CR_GIT_REPO_REGEX="git@github.com:(.+).git"
REM export CR_GIT_HTTP_URL="https://github.com/%(repo)s/commit/%(hash)s"
REM export CR_GIT_BASE_URL="https://github.com/%(repo)s"

REM %* doesn't work with shift; we're going to lose any arguments after #10...
shift
python %CR%.py %0 %1 %2 %3 %4 %5 %6 %7 %8 %9
