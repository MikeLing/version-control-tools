#!/usr/bin/env python
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

# This is a script for performing common Bugzilla operations from the command
# line. It is meant to support testing.

import base64
import os
import sys

import bugsy
import yaml
import xmlrpclib

from rbbz.transports import bugzilla_transport

def main(args):
    url = os.environ['BUGZILLA_URL']
    username = os.environ['BUGZILLA_USERNAME']
    password = os.environ['BUGZILLA_PASSWORD']

    xmlrpcurl = os.environ['BUGZILLA_URL'] + '/xmlrpc.cgi'
    transport = bugzilla_transport(xmlrpcurl)
    proxy = xmlrpclib.ServerProxy(xmlrpcurl, transport)
    proxy.User.login({'login': username, 'password': password})

    client = bugsy.Bugsy(username=username, password=password,
            bugzilla_url=url)

    action = args[0]

    if action == 'create-bug':
        product, component, summary = args[1:]
        bug = bugsy.Bug(client, product=product, component=component, summary=summary)
        client.put(bug)
    if action == 'create-bug-range':
        product, component, upper = args[1:]

        existing = client.search_for.search()
        ids = [int(b['id']) for b in existing]
        ids.append(1)
        maxid = max(ids)

        count = 0
        for i in range(maxid, int(upper) + 1):
            count += 1
            bug = bugsy.Bug(client, product=product, component=component,
                    summary='Range %d' % i)
            foo = client.put(bug)

        print('created %d bugs' % count)

    if action == 'dump-bug':
        data = {}
        for bid in args[1:]:
            bug = client.get(bid)

            d = dict(
                summary=bug.summary,
                comments=[],
            )
            for comment in bug.get_comments():
                d['comments'].append(dict(
                    id=comment.id,
                    text=comment.text,
                ))

            r = client.request('bug/%s/attachment' % bid).json()
            for a in r['bugs'].get(bid, []):
                at = d.setdefault('attachments', [])
                at.append(dict(
                    id=a['id'],
                    attacher=a['attacher'],
                    content_type=a['content_type'],
                    description=a['description'],
                    summary=a['summary'],
                    data=base64.b64decode(a['data'])))

            key = 'Bug %s' % bid
            data[key] = d

        print(yaml.safe_dump(data, default_flow_style=False).rstrip())
    elif action == 'create-group':
        group, desc = args[1:]
        # Adding every user to every group is wrong. This is a quick hack to
        # work around bug 1079463.
        h = proxy.Group.create({
            'name': group,
            'description': desc,
            'user_regexp': '.*',
        })

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))