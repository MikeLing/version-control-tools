from __future__ import unicode_literals

from mozautomation.commitparser import (
    strip_commit_metadata,
)

from mozreview.bugzilla.client import (
    BugzillaAttachmentUpdates,
)
from mozreview.models import (
    BugzillaUserMap,
    get_or_create_bugzilla_users,
)
from mozreview.rb_utils import (
    get_obj_url,
)
from mozreview.review_helpers import (
    gen_latest_reviews
)


def update_bugzilla_attachments(bugzilla, bug_id, children_to_post,
                                children_to_obsolete):
    attachment_updates = BugzillaAttachmentUpdates(bugzilla, bug_id)

    for child in children_to_obsolete:
        attachment_updates.obsolete_review_attachments(get_obj_url(child))

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
    user_email_cache = {}

    for review_request_draft, review_request in children_to_post:
        reviewers = {}

        for u in review_request_draft.target_people.all():
            bum = BugzillaUserMap.objects.get(user=u)
            email = user_email_cache.get(bum.bugzilla_user_id)

            if email is None:
                user_data = bugzilla.get_user_from_userid(bum.bugzilla_user_id)

                # Since we're making the API call, we might as well ensure the
                # local database is up to date.
                users = get_or_create_bugzilla_users(user_data)
                email = users[0].email
                user_email_cache[bum.bugzilla_user_id] = email

            reviewers[email] = False

        for review in gen_latest_reviews(review_request):
            # The last review given by this reviewer had a ship-it, so we
            # will carry their r+ forward. If someone had manually changed
            # their flag on bugzilla, we may be setting it back to r+, but
            # we will consider the manual flag change on bugzilla user
            # error for now.
            if review.ship_it:
                reviewers[review.user.email] = True

        rr_url = get_obj_url(review_request)
        diff_url = '%sdiff/#index_header' % rr_url

        # Only post a comment if the diffset has actually changed
        comment = ''
        if review_request_draft.get_latest_diffset():
            diffset_count = review_request.diffset_history.diffsets.count()
            if diffset_count < 1:
                # We don't need the first line, since it is also the attachment
                # summary, which is displayed in the comment.
                full_commit_msg = review_request_draft.description.partition(
                    '\n')[2].strip()

                full_commit_msg = strip_commit_metadata(full_commit_msg)

                if full_commit_msg:
                    full_commit_msg += '\n\n'

                comment = '%sReview commit: %s\nSee other reviews: %s' % (
                    full_commit_msg,
                    diff_url,
                    rr_url
                )
            else:
                comment = ('Review request updated; see interdiff: '
                           '%sdiff/%d-%d/\n' % (rr_url,
                                                diffset_count,
                                                diffset_count + 1))

        attachment_updates.create_or_update_attachment(
            review_request.id,
            review_request_draft.summary,
            comment,
            diff_url,
            reviewers)

    attachment_updates.do_updates()