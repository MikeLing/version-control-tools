#!/usr/bin/python -u
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import os
import sys


if 'BMOWEB_PORT_80_TCP_ADDR' not in os.environ:
    print('error: container invoked without link to a bmoweb container')
    sys.exit(1)

if 'PULSE_PORT_5672_TCP_ADDR' not in os.environ:
    print('error: container invoked without link to a pulse container')
    sys.exit(1)

if 'AUTOLAND_PORT_80_TCP_ADDR' not in os.environ:
    print('error: container invoked without link to an autoland container')
    sys.exit(1)

execfile('/venv/bin/activate_this.py', dict(__file__='/venv/bin/activate_this.py'))
sys.path.insert(0, '/reviewboard/conf')
os.environ['DJANGO_SETTINGS_MODULE'] = 'reviewboard.settings'

bugzilla_url = 'http://%s:%s' % (os.environ['BMOWEB_PORT_80_TCP_ADDR'],
                                 os.environ['BMOWEB_PORT_80_TCP_PORT'])

autoland_url = 'http://%s:%s' % (os.environ['AUTOLAND_PORT_80_TCP_ADDR'],
                                 os.environ['AUTOLAND_PORT_80_TCP_PORT'])

ldap_url = 'ldap://%s:%s' % (os.environ['LDAP_PORT_389_TCP_ADDR'],
                             os.environ['LDAP_PORT_389_TCP_PORT'])

# siteconfig takes priority over settings_local.py. Ensure siteconfig
# is up to date.
#
# This code mimics what is done in
# reviewboard.admin.management.sites.migrate_settings(). Its existence is
# unfortunate. If we could guarantee that settings_local.py never changes,
# we wouldn't need this.

from djblets.siteconfig.models import SiteConfiguration
from django.conf import settings
sc = SiteConfiguration.objects.get_current()
caches = getattr(settings, 'CACHES', {})
sc.set('cache_backend', caches)

# Set default logging location
sc.set('logging_enabled', True)
sc.set('logging_directory', '/reviewboard/logs')

# Define Bugzilla URL. We use the internal IP here to do initial setup.
# However, for Bugzilla's authentication-delegation system, we later need to
# reset this to Bugzilla's public IP, which is not available at this point.
# It is later set via set-site-url by vcttesting.mozreview.MozReview.
sc.set('auth_bz_xmlrpc_url', '%s/xmlrpc.cgi' % bugzilla_url)
sc.save()

# Define MozReview settings.
from djblets.extensions.models import RegisteredExtension
mre = RegisteredExtension.objects.get(class_name='mozreview.extension.MozReviewExtension')
mre.settings['enabled'] = True
mre.settings['pulse_host'] = os.environ['PULSE_PORT_5672_TCP_ADDR']
mre.settings['pulse_port'] = int(os.environ['PULSE_PORT_5672_TCP_PORT'])
mre.settings['pulse_user'] = 'guest'
mre.settings['pulse_password'] = 'guest'
mre.settings['pulse_ssl'] = False
mre.settings['autoland_try_ui_enabled'] = True
mre.settings['autoland_url'] = autoland_url
mre.settings['autoland_user'] = 'autoland'
mre.settings['autoland_password'] = 'autoland'
mre.settings['autoland_testing'] = True
mre.settings['autoland_import_pullrequest_ui_enabled'] = True
mre.settings['ldap_url'] = ldap_url
mre.settings['ldap_user'] = 'uid=bind-mozreview,ou=logins,dc=mozilla'
mre.settings['ldap_password'] = 'password'
mre.save()

os.execl(sys.argv[1], *sys.argv[1:])
