# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import json
import logging

from reviewboard.changedescs.models import ChangeDescription
from reviewboard.reviews.models import ReviewRequest

MOZREVIEW_KEY = 'p2rb'

COMMITS_KEY = MOZREVIEW_KEY + '.commits'
COMMIT_ID_KEY = MOZREVIEW_KEY + '.commit_id'
IDENTIFIER_KEY = MOZREVIEW_KEY + '.identifier'
REVIEWER_MAP_KEY = MOZREVIEW_KEY + '.reviewer_map'
UNPUBLISHED_RRIDS_KEY = MOZREVIEW_KEY + '.unpublished_rids'


def is_pushed(review_request):
    """Is this a review request that was pushed to MozReview."""
    return IDENTIFIER_KEY in review_request.extra_data


def is_parent(review_request):
    """Is this a MozReview 'parent' review request.

    If this review request represents the folded diff parent of each child
    review request we will return True. This will return false on each of the
    child review requests (or a request which was not pushed).
    """
    return str(review_request.extra_data.get(
        'p2rb.is_squashed', False)).lower() == 'true'


def get_parent_rr(review_request):
    """Retrieve the `review_request` parent.

    If `review_request` is a parent, return it directly.
    Otherwise return its parent based on the identifier in extra_data.
    """
    if not is_pushed(review_request):
        return None

    if is_parent(review_request):
        return review_request

    return ReviewRequest.objects.get(
        commit_id=review_request.extra_data[IDENTIFIER_KEY],
        repository=review_request.repository)


def gen_child_rrs(review_request, user=None):
    """ Generate child review requests.

    For some review request (draft or normal) that has a p2rb.commits
    extra_data field, we yield the child review requests belonging to
    the review-request IDs in that field.

    If a user instance is given, a check is performed to guarantee that
    the user has access to each child. If the user has access to the draft
    version of a child, the draft will be returned.

    If a review request is not found for the listed ID, get_rr_for_id will
    log this, and we'll skip that ID.
    """
    if COMMITS_KEY not in review_request.extra_data:
        return

    commit_tuples = json.loads(review_request.extra_data[COMMITS_KEY])
    for commit_tuple in commit_tuples:
        child = get_rr_for_id(commit_tuple[1])

        # TODO: We should fail if we can't find a child; it indicates
        # something very bad has happened.  Unfortunately this call is
        # used in several different contexts, so we need to make changes
        # there as well.
        if not child:
            continue
        elif user is None:
            yield child
        elif child.is_accessible_by(user):
            yield child.get_draft(user) or child


def gen_rrs_by_extra_data_key(review_request, key):
    if key not in review_request.extra_data:
        return

    return gen_rrs_by_rids(json.loads(review_request.extra_data[key]))


def gen_rrs_by_rids(rrids):
    for rrid in rrids:
        review_request = get_rr_for_id(rrid)
        if review_request:
            yield review_request


def get_rr_for_id(id):
    try:
        return ReviewRequest.objects.get(pk=id)
    except ReviewRequest.DoesNotExist:
        logging.error('Could not retrieve child review request with '
                      'id %s because it does not appear to exist.'
                      % id)


def update_parent_rr_reviewers(parent_rr_draft):
    """Update the list of reviewers on a parent draft.

    We first retrieve a list of the (possibly draft) children and then we
    build a list of unique reviewers related to them.
    We also store a map of the reviewers grouped by children in the parent's
    extra_data. In this way we can publish a parent draft even if the only
    change is on the reviewer list of one of the children.
    """
    # TODO: Add an optional child_rr_draft parameter to speed things up when we know
    # which child changed.
    child_rr_list = gen_child_rrs(parent_rr_draft)
    reviewers_map_before = parent_rr_draft.extra_data.get(REVIEWER_MAP_KEY, None)

    reviewers_map_after = {}

    for child in child_rr_list:
        actual_child = child.get_draft() or child
        reviewers = actual_child.target_people.values_list(
            'id', flat=True).order_by('id')
        reviewers_map_after[str(child.id)] = list(reviewers)

    if reviewers_map_after != reviewers_map_before:
        total_reviewers = set(sum(reviewers_map_after.values(), []))
        parent_rr_draft.target_people = total_reviewers
        parent_rr_draft.extra_data[REVIEWER_MAP_KEY] = json.dumps(reviewers_map_after)

        parent_rr = parent_rr_draft.get_review_request()
        if parent_rr.public and parent_rr_draft.changedesc is None:
            parent_rr_draft.changedesc = ChangeDescription.objects.create()

        parent_rr_draft.save()
