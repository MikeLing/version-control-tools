# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import logging
import re

from django.contrib.sites.models import Site
from django.db.models.signals import pre_delete

from djblets.siteconfig.models import SiteConfiguration
from djblets.util.decorators import simple_decorator

from reviewboard.extensions.base import Extension
from reviewboard.extensions.hooks import AuthBackendHook, SignalHook
from reviewboard.reviews.errors import PublishError
from reviewboard.reviews.models import (ReviewRequest,
                                        ReviewRequestDraft)
from reviewboard.reviews.signals import (reply_publishing,
                                         review_publishing,
                                         review_request_closed,
                                         review_request_publishing,
                                         review_request_reopened)
from reviewboard.site.urlresolvers import local_site_reverse

from mozreview.bugzilla.client import Bugzilla
from mozreview.bugzilla.errors import BugzillaError
from mozreview.errors import (CommitPublishProhibited,
                              ParentShipItError)
from mozreview.extra_data import (UNPUBLISHED_RRIDS_KEY,
                                  gen_child_rrs,
                                  gen_rrs_by_rids,
                                  gen_rrs_by_extra_data_key,
                                  is_parent)
from mozreview.models import (BugzillaUserMap,
                              get_bugzilla_api_key,
                              get_or_create_bugzilla_users,
                              get_profile)
from mozreview.signals import commit_request_publishing
from rbbz.auth import BugzillaBackend
from rbbz.diffs import build_plaintext_review
from rbbz.errors import (ConfidentialBugError,
                         InvalidBugIdError)
from rbbz.middleware import CorsHeaderMiddleware
from rbbz.resources import bugzilla_cookie_login_resource


REVIEWID_RE = re.compile('bz://(\d+)/[^/]+')

AUTO_CLOSE_DESCRIPTION = """
Discarded automatically because parent review request was discarded.
"""

AUTO_SUBMITTED_DESCRIPTION = """
Submitted because the parent review request was submitted.
"""

NEVER_USED_DESCRIPTION = """
Discarded because this review request ended up not being needed.
"""

OBSOLETE_DESCRIPTION = """
Discarded because this change is no longer required.
"""

# Extra data fields which should be automatically copied from
# the draft to the review request on publish.
DRAFTED_EXTRA_DATA_KEYS = (
    'p2rb.commit_id',
    'p2rb.first_public_ancestor',
    'p2rb.identifier',
)


class BugzillaExtension(Extension):
    middleware = [CorsHeaderMiddleware]

    resources = [
        bugzilla_cookie_login_resource,
    ]

    def initialize(self):
        AuthBackendHook(self, BugzillaBackend)

        # Any abortable signal hooks that talk to Bugzilla should have
        # sandbox_errors=False, since we don't want to complete the action if
        # updating Bugzilla failed for any reason.
        SignalHook(self, pre_delete, on_draft_pre_delete)
        SignalHook(self, review_request_publishing,
                   on_review_request_publishing,
                   sandbox_errors=False)
        SignalHook(self, review_publishing, on_review_publishing,
                   sandbox_errors=False)
        SignalHook(self, reply_publishing, on_reply_publishing,
                   sandbox_errors=False)
        SignalHook(self, review_request_closed,
                   on_review_request_closed_discarded)
        SignalHook(self, review_request_closed,
                   on_review_request_closed_submitted)
        SignalHook(self, review_request_reopened, on_review_request_reopened)


def get_obj_url(obj, site=None, siteconfig=None):
    if not site:
        site = Site.objects.get_current()

    if not siteconfig:
        siteconfig = SiteConfiguration.objects.get_current()

    return '%s://%s%s%s' % (
        siteconfig.get('site_domain_method'), site.domain,
        local_site_reverse('root').rstrip('/'),
        obj.get_absolute_url())


def get_reply_url(reply, site=None, siteconfig=None):
    """ Get the URL for a reply to a review.

    Since replies can have multiple comments, we can't link to a specific
    comment, so we link to the parent review which the reply is targeted at.
    """
    return get_obj_url(reply.base_reply_to, site=site, siteconfig=siteconfig)


def is_review_request_pushed(review_request):
    return str(review_request.extra_data.get('p2rb', False)) == "True"


def is_review_request_squashed(review_request):
    squashed = str(review_request.extra_data.get('p2rb.is_squashed', False))
    return squashed == "True"


def on_draft_pre_delete(sender, instance, using, **kwargs):
    """ Handle draft discards.

    There are no handy signals built into Review Board (yet) for us to detect
    when a squashed Review Request Draft is discarded. Instead, we monitor for
    deletions of models, and handle cases where the models being deleted are
    ReviewRequestDrafts. We then do some processing to ensure that the draft
    is indeed a draft of a squashed review request that we want to handle,
    and then propagate the discard down to the child review requests.
    """
    if not sender == ReviewRequestDraft:
        return

    # Drafts can get deleted for a number of reasons. They get deleted when
    # drafts are discarded, obviously, but also whenever review requests are
    # published, because the data gets copied over to the review request, and
    # then the draft is blown away. Unfortunately, on_pre_delete doesn't give
    # us too many clues about which scenario we're in, so we have to infer it
    # based on other things attached to the model. This is a temporary fix until
    # we get more comprehensive draft deletion signals built into Review Board.
    #
    # In the case where the review request is NOT public yet, the draft will
    # not have a change description. In this case, we do not need to
    # differentiate between publish and discard because discards of non-public
    # review request's drafts will always cause the review request to be closed
    # as discarded, and this case is handled by on_review_request_closed().
    #
    # In the case where the review request has a change description, but it's
    # set to public, we must have just published this draft before deleting it,
    # so there's nothing to do here.
    if (instance.changedesc is None or instance.changedesc.public):
        return

    review_request = instance.review_request

    if not review_request:
        return

    if not is_review_request_squashed(review_request):
        return

    # If the review request is marked as discarded, then we must be closing
    # it, and so the on_review_request_closed() handler will take care of it.
    if review_request.status == ReviewRequest.DISCARDED:
        return

    user = review_request.submitter

    for child in gen_child_rrs(review_request):
        draft = child.get_draft()
        if draft:
            draft.delete()

    for child in gen_rrs_by_extra_data_key(review_request,
                                           UNPUBLISHED_RRIDS_KEY):
        child.close(ReviewRequest.DISCARDED,
                    user=user,
                    description=NEVER_USED_DESCRIPTION)

    review_request.extra_data['p2rb.discard_on_publish_rids'] = '[]'
    review_request.extra_data['p2rb.unpublished_rids'] = '[]'
    review_request.save()


@simple_decorator
def bugzilla_to_publish_errors(func):
    def _transform_errors(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except BugzillaError as e:
            raise PublishError('Bugzilla error: %s' % e.msg)
    return _transform_errors


def post_bugzilla_attachment(bugzilla, bug_id, review_request_draft,
                             review_request):
    # We publish attachments for each commit/child to Bugzilla so that
    # reviewers can easily track their requests.

    # The review request exposes a list of usernames for reviewers. We need
    # to convert these to Bugzilla emails in order to make the request into
    # Bugzilla.
    #
    # It may seem like there is a data syncing problem here where usernames
    # may get out of sync with the reality from Bugzilla. Fortunately,
    # Review Board is smarter than that. Internally, the target_people list
    # is stored with foreign keys into the numeric primary key of the user
    # table. If the RB username changes, this won't impact target_people
    # nor the stored mapping to the numeric Bugzilla ID, which is
    # immutable.
    #
    # But we do have a potential data syncing problem with the stored email
    # address. Review Board's stored email address could be stale. So
    # instead of using it directly, we query Bugzilla and map the stored,
    # immutable numeric Bugzilla userid into an email address. This lookup
    # could be avoided if Bugzilla accepted a numeric userid in the
    # requestee parameter when modifying an attachment.
    reviewers = {}

    for u in review_request_draft.target_people.all():
        bum = BugzillaUserMap.objects.get(user=u)

        user_data = bugzilla.get_user_from_userid(bum.bugzilla_user_id)

        # Since we're making the API call, we might as well ensure the
        # local database is up to date.
        users = get_or_create_bugzilla_users(user_data)
        reviewers[users[0].email] = False

    last_user = None
    relevant_reviews = review_request.get_public_reviews().order_by(
        'user', '-timestamp')

    for review in relevant_reviews:
        if review.user == last_user:
            # We only care about the most recent review for each
            # particular user.
            continue

        last_user = review.user

        # The last review given by this reviewer had a ship-it, so we
        # will carry their r+ forward. If someone had manually changed
        # their flag on bugzilla, we may be setting it back to r+, but
        # we will consider the manual flag change on bugzilla user
        # error for now.
        if review.ship_it:
            reviewers[last_user.email] = True

    rr_url = get_obj_url(review_request)
    diff_url = '%sdiff/#index_header' % rr_url
    diffset_count = review_request.diffset_history.diffsets.count()

    if diffset_count < 1:
        # We don't need the first line, since it is also the attachment
        # summary, which is displayed in the comment.
        extended_commit_msg = review_request_draft.description.partition(
            '\n')[2].lstrip('\n')

        if extended_commit_msg:
            extended_commit_msg += '\n\n'

        comment = '%sReview commit: %s\nSee other reviews: %s' % (
            extended_commit_msg,
            diff_url,
            rr_url
        )
    else:
        comment = ('Review request updated; see interdiff: '
                   '%sdiff/%d-%d/\n' % (rr_url,
                                        diffset_count,
                                        diffset_count + 1))

    bugzilla.post_rb_url(bug_id,
                         review_request.id,
                         review_request_draft.summary,
                         comment,
                         diff_url,
                         reviewers)


@bugzilla_to_publish_errors
def on_review_request_publishing(user, review_request_draft, **kwargs):
    # There have been strange cases (all local, and during development), where
    # when attempting to publish a review request, this handler will fail
    # because the draft does not exist. This is a really strange case, and not
    # one we expect to happen in production. However, since we've seen it
    # locally, we handle it here, and log.
    if not review_request_draft:
        logging.error('Strangely, there was no review request draft on the '
                      'review request we were attempting to publish.')
        return

    review_request = review_request_draft.get_review_request()

    # skip review requests that were not pushed
    if not is_review_request_pushed(review_request):
        return

    if not is_parent(review_request):
        # Send a signal asking for approval to publish this review request.
        # We only want to publish this commit request if we are in the middle
        # of publishing the parent. If the parent is publishing it will be
        # listening for this signal to approve it.
        approvals = commit_request_publishing.send_robust(
            sender=review_request,
            user=user,
            review_request_draft=review_request_draft)

        for receiver, approved in approvals:
            if approved:
                break
        else:
            # This publish is not approved by the parent review request.
            raise CommitPublishProhibited()

    # The reviewid passed through p2rb is, for Mozilla's instance anyway,
    # bz://<bug id>/<irc nick>.
    reviewid = review_request_draft.extra_data.get('p2rb.identifier', None)
    m = REVIEWID_RE.match(reviewid)

    if not m:
        raise InvalidBugIdError('<unknown>')

    bug_id = m.group(1)
    using_bugzilla = we_are_using_bugzilla()
    try:
        bug_id = int(bug_id)
    except (TypeError, ValueError):
        raise InvalidBugIdError(bug_id)

    if using_bugzilla:
        b = Bugzilla(get_bugzilla_api_key(user))

        try:
            if b.is_bug_confidential(bug_id):
                raise ConfidentialBugError
        except BugzillaError as e:
            # Special cases:
            #   100: Invalid Bug Alias
            #   101: Bug does not exist
            if e.fault_code and (e.fault_code == 100 or e.fault_code == 101):
                raise InvalidBugIdError(bug_id)
            raise

    # Note that the bug ID has already been set when the review was created.

    # If this is a squashed/parent review request, automatically publish all
    # relevant children.
    if is_review_request_squashed(review_request):
        unpublished_rids = map(int, json.loads(
            review_request.extra_data['p2rb.unpublished_rids']))
        discard_on_publish_rids = map(int, json.loads(
            review_request.extra_data['p2rb.discard_on_publish_rids']))
        child_rrs = list(gen_child_rrs(review_request_draft))

        # Create or update Bugzilla attachments for each draft commit.  This
        # is done before the children are published to ensure that MozReview
        # doesn't get into a strange state if communication with Bugzilla is
        # broken or attachment creation otherwise fails.  The Bugzilla
        # attachments will then, of course, be in a weird state, but that
        # should be fixed by the next successful publish.
        if using_bugzilla:
            for child in child_rrs:
                child_draft = child.get_draft(user=user)

                if child_draft:
                    if child.id in discard_on_publish_rids:
                        b.obsolete_review_attachments(
                            bug_id, get_obj_url(child))
                    post_bugzilla_attachment(b, bug_id, child_draft, child)

        # Publish draft commits. This will already include items that are in
        # unpublished_rids, so we'll remove anything we publish out of
        # unpublished_rids.
        for child in child_rrs:
            if child.get_draft(user=user) or not child.public:
                def approve_publish(sender, user, review_request_draft,
                                    **kwargs):
                    return child is sender

                # Setup the parent signal handler to approve the publish
                # and then publish the child.
                commit_request_publishing.connect(approve_publish, sender=child,
                                                  weak=False)
                try:
                    child.publish(user=user)
                finally:
                    commit_request_publishing.disconnect(
                        receiver=approve_publish,
                        sender=child,
                        weak=False)

                if child.id in unpublished_rids:
                    unpublished_rids.remove(child.id)

        # The remaining unpubished_rids need to be closed as discarded because
        # they have never been published, and they will appear in the user's
        # dashboard unless closed.
        for child in gen_rrs_by_rids(unpublished_rids):
            child.close(ReviewRequest.DISCARDED,
                        user=user,
                        description=NEVER_USED_DESCRIPTION)

        # We also close the discard_on_publish review requests because, well,
        # we don't need them anymore. We use a slightly different message
        # though.
        for child in gen_rrs_by_rids(discard_on_publish_rids):
            child.close(ReviewRequest.DISCARDED,
                        user=user,
                        description=OBSOLETE_DESCRIPTION)

        review_request.extra_data['p2rb.unpublished_rids'] = '[]'
        review_request.extra_data['p2rb.discard_on_publish_rids'] = '[]'

    # Copy p2rb extra data from the draft, overwriting the current
    # values on the review request.
    draft_extra_data = review_request_draft.extra_data

    for key in DRAFTED_EXTRA_DATA_KEYS:
        if key in draft_extra_data:
            review_request.extra_data[key] = draft_extra_data[key]

    review_request.save()


@bugzilla_to_publish_errors
def on_review_publishing(user, review, **kwargs):
    """Comment in the bug and potentially r+ or clear a review flag.

    Note that a reviewer *must* have editbugs to set an attachment flag on
    someone else's attachment (i.e. the standard BMO review process).

    TODO: Report lack-of-editbugs properly; see bug 1119065.
    """
    review_request = review.review_request

    # skip review requests that were not pushed
    if not is_review_request_pushed(review_request):
        return

    site = Site.objects.get_current()
    siteconfig = SiteConfiguration.objects.get_current()
    comment = build_plaintext_review(review,
                                     get_obj_url(review, site,
                                                 siteconfig),
                                     {"user": user})
    b = Bugzilla(get_bugzilla_api_key(user))

    # TODO: Update all attachments in one call.  This is not possible right
    # now because we have to potentially mix changing and creating flags.

    if is_review_request_squashed(review_request):
        # Mirror the comment to the bug, unless it's a ship-it, in which
        # case throw an error.  Ship-its are allowed only on child commits.
        if review.ship_it:
            raise ParentShipItError

        [b.post_comment(int(bug_id), comment) for bug_id in
         review_request.get_bug_list()]
    else:
        diff_url = '%sdiff/#index_header' % get_obj_url(review_request)
        bug_id = int(review_request.get_bug_list()[0])

        if review.ship_it:
            commented = b.r_plus_attachment(bug_id, review.user.email,
                                            diff_url, comment)
        else:
            commented = b.cancel_review_request(bug_id, review.user.email,
                                                diff_url, comment)

        if comment and not commented:
            b.post_comment(bug_id, comment)


@bugzilla_to_publish_errors
def on_reply_publishing(user, reply, **kwargs):
    review_request = reply.review_request

    # skip review requests that were not pushed
    if not is_review_request_pushed(review_request):
        return

    bug_id = int(review_request.get_bug_list()[0])
    b = Bugzilla(get_bugzilla_api_key(user))

    url = get_reply_url(reply)
    comment = build_plaintext_review(reply, url, {"user": user})
    b.post_comment(bug_id, comment)


def on_review_request_closed_discarded(user, review_request, type, **kwargs):
    if type != ReviewRequest.DISCARDED:
        return

    if is_review_request_squashed(review_request):
        # close_child_review_requests will call save on this review request, so
        # we don't have to worry about it.
        review_request.commit = None

        close_child_review_requests(user, review_request,
                                    ReviewRequest.DISCARDED,
                                    AUTO_CLOSE_DESCRIPTION)
    else:
        # TODO: Remove this once we properly prevent users from closing
        # commit review requests.
        b = Bugzilla(get_bugzilla_api_key(user))
        bug = int(review_request.get_bug_list()[0])
        diff_url = '%sdiff/#index_header' % get_obj_url(review_request)
        b.obsolete_review_attachments(bug, diff_url)


def on_review_request_closed_submitted(user, review_request, type, **kwargs):
    if (not is_review_request_squashed(review_request) or
        type != ReviewRequest.SUBMITTED):
        return

    close_child_review_requests(user, review_request, ReviewRequest.SUBMITTED,
                                  AUTO_SUBMITTED_DESCRIPTION)


def close_child_review_requests(user, review_request, status,
                                  child_close_description):
    """Closes all child review requests for a squashed review request."""
    # At the point of closing, it's possible that if this review
    # request was never published, that most of the fields are empty
    # (See https://code.google.com/p/reviewboard/issues/detail?id=3465).
    # Luckily, the extra_data is still around, and more luckily, it's
    # not exposed in the UI for user-meddling. We can find all of the
    # child review requests via extra_data.p2rb.commits.
    for child in gen_child_rrs(review_request):
        child.close(status,
                    user=user,
                    description=child_close_description)

    # We want to discard any review requests that this squashed review
    # request never got to publish, so were never part of its "commits"
    # list.
    for child in gen_rrs_by_extra_data_key(review_request,
                                           UNPUBLISHED_RRIDS_KEY):
        child.close(ReviewRequest.DISCARDED,
                    user=user,
                    description=NEVER_USED_DESCRIPTION)

    review_request.extra_data['p2rb.unpublished_rids'] = '[]'
    review_request.extra_data['p2rb.discard_on_publish_rids'] = '[]'
    review_request.save()


def on_review_request_reopened(user, review_request, **kwargs):
    if not is_review_request_squashed(review_request):
        return

    identifier = review_request.extra_data['p2rb.identifier']

    # If we're reviving a squashed review request that was discarded, it means
    # we're going to want to restore the commit ID field back, since we remove
    # it on discarding. This might be a problem if there's already a review
    # request with the same commit ID somewhere on Review Board, since commit
    # IDs are unique.
    #
    # When this signal fires, the state of the review request has already
    # changed, so we query for a review request with the same commit ID that is
    # not equal to the revived review request.
    try:
        preexisting_review_request = ReviewRequest.objects.get(
            commit_id=identifier, repository=review_request.repository)
        if preexisting_review_request != review_request:
            logging.error("Could not revive review request with ID %s "
                          "because its commit id (%s) is already being used by "
                          "a review request with ID %s."
                          % (review_request.id, identifier,
                             preexisting_review_request.id))
            # TODO: We need Review Board to recognize exceptions in these signal
            # handlers so that the UI can print out a useful message.
            raise Exception("Revive failed because a review request with commit ID %s "
                            "already exists." % identifier)
    except ReviewRequest.DoesNotExist:
        # Great! This is a success case.
        pass

    for child in gen_child_rrs(review_request):
        child.reopen(user=user)

    # If the review request had been discarded, then the commit ID would
    # have been cleared out. If the review request had been submitted,
    # this is a no-op, since the commit ID would have been there already.
    review_request.commit = identifier
    review_request.save()

    # If the review request has a draft, we have to set the commit ID there as
    # well, otherwise it'll get overwritten on publish.
    draft = review_request.get_draft(user)
    if draft:
        draft.commit = identifier
        draft.save()


def we_are_using_bugzilla():
    siteconfig = SiteConfiguration.objects.get_current()
    return siteconfig.settings.get("auth_backend", "builtin") == "bugzilla"
