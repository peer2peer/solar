#    Copyright 2015 Mirantis, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
"""Solar CLI api

On create "golden" resource should be moved to special place
"""

import argparse
import os
import sys
import pprint

import textwrap
import yaml

from solar import extensions
from solar import utils
from solar.core import data
from solar.interfaces.db import get_db

# NOTE: these are extensions, they shouldn't be imported here
# Maybe each extension can also extend the CLI with parsers
from solar.extensions.modules.discovery import Discovery


class Cmd(object):

    def __init__(self):
        self.parser = argparse.ArgumentParser(
            description=textwrap.dedent(__doc__),
            formatter_class=argparse.RawDescriptionHelpFormatter)
        self.subparser = self.parser.add_subparsers(
            title='actions',
            description='Supported actions',
            help='Provide of one valid actions')
        self.register_actions()
        self.db = get_db()

    def parse(self, args):
        parsed = self.parser.parse_args(args)
        return parsed.func(parsed)

    def register_actions(self):

        parser = self.subparser.add_parser('discover')
        parser.set_defaults(func=getattr(self, 'discover'))

        # Perform configuration
        parser = self.subparser.add_parser('configure')
        parser.set_defaults(func=getattr(self, 'configure'))
        parser.add_argument(
            '-p',
            '--profile')
        parser.add_argument(
            '-a',
            '--actions',
            nargs='+')
        parser.add_argument(
            '-pa',
            '--profile_action')

        # Profile actions
        parser = self.subparser.add_parser('profile')
        parser.set_defaults(func=getattr(self, 'profile'))
        parser.add_argument('-l', '--list', dest='list', action='store_true')
        group = parser.add_argument_group('create')
        group.add_argument('-c', '--create', dest='create', action='store_true')
        group.add_argument('-t', '--tags', nargs='+', default=['env/test_env'])
        group.add_argument('-i', '--id', default=utils.generate_uuid())

        parser = self.subparser.add_parser('data')
        parser.set_defaults(func=getattr(self, 'data'))

    def profile(self, args):
        if args.create:
            params = {'tags': args.tags, 'id': args.id}
            profile_template_path = os.path.join(
                utils.read_config()['template-dir'], 'profile.yml'
            )
            data = yaml.load(utils.render_template(profile_template_path, params))
            self.db.store('profiles', data)
        else:
            pprint.pprint(self.db.get_list('profiles'))

    def configure(self, args):
        profile = self.db.get_record('profiles', args.profile)
        extensions.find_by_provider_from_profile(
            profile, 'configure').configure(
                actions=args.actions,
                profile_action=args.profile_action)

    def discover(self, args):
        Discovery({'id': 'discovery'}).discover()

    def data(self, args):

        resources = [
             {'id': 'mariadb',
              'tags': ['service/mariadb', 'entrypoint/mariadb'],
              'input': {
                  'ip_addr': '{{ self.node.ip }}' }},

            {'id': 'keystone',
             'tags': ['service/keystone'],
             'input': {
                 'admin_port': '35357',
                 'public_port': '5000',
                 'db_addr': '{{ first_with_tags("entrypoint/mariadb").node.ip }}'}},

             {'id': 'haproxy',
              'tags': ['service/haproxy'],
              'input': {
                  'services': [
                      {'service_name': 'keystone-admin',
                       'bind': '*:8080',
                       'some_params': '{{ with_tags("service/trololo", "avsd/vdf") }}',
                       'backends': {
                           'with_items': '{{ with_tags("service/keystone") }}',
                           'item': {'name': '{{ item.keystone.name }}', 'addr': '{{ item.node.ip }}:{{ item.node.admin_port }}'}}},

                      {'service_name': 'keystone-pub',
                       'bind': '*:8081',
                       'backends': {
                           'with_items': '{{ with_tags("service/keystone") }}',
                           'item': {'name': '{{ item.keystone.name }}', 'addr': '{{ item.node.ip }}:{{ item.node.public_port }}'}}}]}},

             {'id': 'n-1',
              'input': {'ip': '10.0.0.2'},
              'tags': ['node/1', 'service/keystone']},

             {'id': 'n-2',
              'input': {'ip': '10.0.0.3'},
              'tags': ['node/2', 'service/mariadb', 'service/haproxy', 'service/keystone']}]

        dg = data.DataGraph(resources)

        pprint.pprint(dg.resolve())



def main():
    api = Cmd()
    api.parse(sys.argv[1:])
