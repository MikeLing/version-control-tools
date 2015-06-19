#require docker
  $ . $TESTDIR/hgext/reviewboard/tests/helpers.sh
  $ commonenv

  $ bugzilla create-bug-range TestProduct TestComponent 2
  created bugs 1 to 2

Set up repo

  $ cd client
  $ echo foo > foo
  $ hg commit -A -m 'root commit'
  adding foo
  $ echo foo2 > foo
  $ hg commit -m 'second commit'

  $ hg phase --public -r 0

Create a user

  $ adminbugzilla create-user user1@example.com password1 'User One [:user1]'
  created user 6
  $ mozreview create-ldap-user user1@example.com user1 2001 'User One' --key-file ${MOZREVIEW_HOME}/keys/user1@example.com --scm-level 1
  $ exportbzauth user1@example.com password1

Dump the user so it gets mirrored over to Review Board

  $ rbmanage dump-user user1 > /dev/null

The user should not have an ldap username associated with them

  $ rbmanage dump-user-ldap user1
  no ldap username associated with user1

Perform a push with the user

  $ hg --config bugzilla.username=user1@example.com --config bugzilla.password=password1 push -r 1 --reviewid 1
  pushing to ssh://*:$HGPORT6/test-repo (glob)
  (adding commit id to 1 changesets)
  saved backup bundle to $TESTTMP/client/.hg/strip-backup/cd3395bd3f8a*-addcommitid.hg (glob)
  searching for changes
  remote: adding changesets
  remote: adding manifests
  remote: adding file changes
  remote: added 2 changesets with 2 changes to 1 files
  remote: Trying to insert into pushlog.
  remote: Inserted into the pushlog db successfully.
  submitting 1 changesets for review
  
  changeset:  1:60479d07173e
  summary:    second commit
  review:     http://*:$HGPORT1/r/2 (draft) (glob)
  
  review id:  bz://1/mynick
  review url: http://*:$HGPORT1/r/1 (draft) (glob)
  (visit review url to publish this review request so others can see it)

The user should now have an associated ldap_username

  $ rbmanage dump-user-ldap user1
  ldap username: user1@example.com

Cleanup

  $ mozreview stop
  stopped 8 containers