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

from collections import namedtuple

import dictdiffer
import networkx as nx

from solar.core.log import log
from solar.core import resource
from solar.core.resource.resource import RESOURCE_STATE
from solar.core import signals
from solar.dblayer.solar_models import CommitedResource
from solar.dblayer.solar_models import HistoryItem
from solar.dblayer.solar_models import LogItem
from solar.events import api as evapi
from solar.events.controls import StateChange
from solar.orchestration import graph
from solar.system_log import data
from solar import utils

from solar.system_log.consts import CHANGES


def guess_action(from_, to):
    # NOTE(dshulyak) imo the way to solve this - is dsl for orchestration,
    # something where this action will be excplicitly specified
    if not from_:
        return CHANGES.run.name
    elif not to:
        return CHANGES.remove.name
    else:
        return CHANGES.update.name


def create_diff(staged, commited):

    def listify(t):
        # we need all values as lists, because we need the same behaviour
        # in pre and post save situations
        return list(map(listify, t)) if isinstance(t, (list, tuple)) else t

    res = tuple(dictdiffer.diff(commited, staged))
    return listify(res)


class Diff(namedtuple('Diff', ['resource', 'diff', 'connections', 'path'])):

    def to_log_item(self, action):
        return LogItem.new(
            {'resource': self.resource,
             'diff': self.diff,
             'connections_diff': self.connections,
             'base_path': self.path,
             'action': action,
             'log': 'staged'})

    def to_update(self):
        return self.to_log_item('update')

    def to_run(self):
        return self.to_log_item('run')

    def to_remove(self):
        return self.to_log_item('remove')

    @classmethod
    def create_from_resource(cls, resource_obj):
        commited = resource_obj.load_commited()
        if resource_obj.to_be_removed():
            resource_args = {}
            resource_connections = []
        else:
            resource_args = resource_obj.args
            resource_connections = resource_obj.connections

        if commited.state == RESOURCE_STATE.removed.name:
            commited_args = {}
            commited_connections = []
        else:
            commited_args = commited.inputs
            commited_connections = commited.connections

        return cls(resource_obj.name,
                   create_diff(resource_args, commited_args),
                   create_sorted_diff(
                       resource_connections, commited_connections),
                   resource_obj.base_path)


def create_run(resource_obj):
    return Diff.create_from_resource(resource_obj).to_run()


def create_remove(resource_obj):
    return Diff.create_from_resource(resource_obj).to_remove()


def create_update(resource_obj):
    return Diff.create_from_resource(resource_obj).to_update()


def populate_log_item(log_item, diff=None):
    if diff is None:
        diff = Diff.create_from_resource(resource.load(log_item.resource))
    log_item.diff = diff.diff
    log_item.connections_diff = diff.connections
    log_item.base_path = diff.path
    return log_item


def create_sorted_diff(staged, commited):
    staged.sort()
    commited.sort()
    return create_diff(staged, commited)


def staged_log():
    """Staging procedure takes manually created log items, populate them
    with diff and connections diff

    Current implementation prevents from several things to occur:
    - same log_action (resource.action pair) cannot not be staged multiple
      times
    - child will be staged only if diff or connections_diff is changed,
      and we can execute *run* action to apply that diff - in all other cases
      child should be staged explicitly
    """
    log_actions = set()
    resources_names = set()
    staged_log = data.SL()
    without_duplicates = []
    for log_item in staged_log:
        if log_item.log_action in log_actions:
            log_item.delete()
            continue
        resources_names.add(log_item.resource)
        log_actions.add(log_item.log_action)
        without_duplicates.append(log_item)
    utils.solar_map(lambda li: populate_log_item(li),
                    without_duplicates, concurrency=10)
    # this is backward compatible change, there might better way
    # to "guess" child actions
    childs = filter(lambda child: child.name not in resources_names,
                    resource.load_childs(list(resources_names)))
    diffs = filter(
        lambda d: d.diff or d.connections,
        utils.solar_map(Diff.create_from_resource, childs, concurrency=10))
    update_items = utils.solar_map(
        lambda d: d.to_update(), diffs, concurrency=10)
    for log_item in update_items + without_duplicates:
        log_item.save_lazy()
    return without_duplicates + update_items


def send_to_orchestration(tags=None):
    dg = nx.MultiDiGraph()
    events = {}
    changed_nodes = []

    if tags:
        staged_log = LogItem.log_items_by_tags(tags)
    else:
        staged_log = data.SL()
    for logitem in staged_log:
        events[logitem.resource] = evapi.all_events(logitem.resource)
        changed_nodes.append(logitem.resource)

        state_change = StateChange(logitem.resource, logitem.action)
        state_change.insert(changed_nodes, dg)

    evapi.build_edges(dg, events)
    # what `name` should be?
    dg.graph['name'] = 'system_log'
    built_graph = graph.create_plan_from_graph(dg)
    graph.assign_weights_nested(built_graph)
    return built_graph


def parameters(res, action, data):
    return {'args': [res, action],
            'type': 'solar_resource'}


def _get_args_to_update(args, connections):
    """Returns args to update

    For each resource we can update only args that are not provided
    by connections
    """
    inherited = [i[3].split(':')[0] for i in connections]
    return {
        key: args[key] for key in args
        if key not in inherited
    }


def is_create(logitem):
    return logitem.action == 'run'


def is_update(logitem):
    return logitem.action == 'update'


def revert_uids(uids):
    """Reverts uids

    :param uids: iterable not generator
    """
    items = HistoryItem.multi_get(uids)

    for item in items:
        if is_update(item):
            _revert_update(item)
        elif item.action == CHANGES.remove.name:
            _revert_remove(item)
        elif is_create(item):
            _revert_run(item)
        else:
            log.debug('Action %s for resource %s is a side'
                      ' effect of another action', item.action, item.res)


def _revert_remove(logitem):
    """Resource should be created with all previous connections"""
    commited = CommitedResource.get(logitem.resource)
    args = dictdiffer.revert(logitem.diff, commited.inputs)
    connections = dictdiffer.revert(
        logitem.connections_diff, sorted(commited.connections))

    resource.Resource(logitem.resource, logitem.base_path,
                      args=_get_args_to_update(args, connections),
                      tags=commited.tags)
    for emitter, emitter_input, receiver, receiver_input in connections:
        emmiter_obj = resource.load(emitter)
        receiver_obj = resource.load(receiver)
        signals.connect(emmiter_obj, receiver_obj, {
                        emitter_input: receiver_input})


def _update_inputs_connections(res_obj, args, old_connections, new_connections):  # NOQA

    removed = []
    for item in old_connections:
        if item not in new_connections:
            removed.append(item)

    added = []
    for item in new_connections:
        if item not in old_connections:
            added.append(item)

    for emitter, _, receiver, _ in removed:
        emmiter_obj = resource.load(emitter)
        receiver_obj = resource.load(receiver)
        emmiter_obj.disconnect(receiver_obj)

    for emitter, emitter_input, receiver, receiver_input in added:
        emmiter_obj = resource.load(emitter)
        receiver_obj = resource.load(receiver)
        emmiter_obj.connect(receiver_obj, {emitter_input: receiver_input})

    if removed or added:
        # TODO without save we will get error
        # that some values can not be updated
        # even if connection was removed
        receiver_obj.db_obj.save()
    if args:
        res_obj.update(args)


def _revert_update(logitem):
    """Revert of update should update inputs and connections"""
    res_obj = resource.load(logitem.resource)
    commited = res_obj.load_commited()

    connections = dictdiffer.revert(
        logitem.connections_diff, sorted(commited.connections))
    args = dictdiffer.revert(logitem.diff, commited.inputs)

    _update_inputs_connections(
        res_obj, _get_args_to_update(args, connections),
        commited.connections, connections)


def _revert_run(logitem):
    res_obj = resource.load(logitem.resource)
    res_obj.remove()


def revert(uid):
    return revert_uids([uid])


def _discard_remove(item):
    resource_obj = resource.load(item.resource)
    resource_obj.set_created()


def _discard_update(item):
    resource_obj = resource.load(item.resource)
    old_connections = resource_obj.connections
    new_connections = dictdiffer.revert(
        item.connections_diff, sorted(old_connections))
    inputs = dictdiffer.revert(item.diff, resource_obj.args)
    _update_inputs_connections(
        resource_obj, _get_args_to_update(inputs, old_connections),
        old_connections, new_connections)


def _discard_run(item):
    resource.load(item.resource).remove(force=True)


def discard_single(item):
    if is_update(item):
        _discard_update(item)
    elif item.action == CHANGES.remove.name:
        _discard_remove(item)
    elif is_create(item):
        _discard_run(item)
    else:
        log.debug('Action %s for resource %s is a side'
                  ' effect of another action', item.action, item.res)
    item.delete()


def discard_uids(uids):
    items = filter(bool, LogItem.multi_get(uids))
    for item in items:
        discard_single(item)


def discard_uid(uid):
    return discard_uids([uid])


def discard_all():
    staged_log = data.SL()
    return discard_uids([l.key for l in staged_log])


def commit_all():
    """Helper mainly for ease of testing"""
    from solar.system_log.operations import move_to_commited
    for item in data.SL():
        move_to_commited(item.log_action)


def clear_history():
    LogItem.delete_all()
    CommitedResource.delete_all()
