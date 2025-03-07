# -*- coding: utf-8 -*- {{{
# vim: set fenc=utf-8 ft=python sw=4 ts=4 sts=4 et:
#
# Copyright 2020, Battelle Memorial Institute.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This material was prepared as an account of work sponsored by an agency of
# the United States Government. Neither the United States Government nor the
# United States Department of Energy, nor Battelle, nor any of their
# employees, nor any jurisdiction or organization that has cooperated in the
# development of these materials, makes any warranty, express or
# implied, or assumes any legal liability or responsibility for the accuracy,
# completeness, or usefulness or any information, apparatus, product,
# software, or process disclosed, or represents that its use would not infringe
# privately owned rights. Reference herein to any specific commercial product,
# process, or service by trade name, trademark, manufacturer, or otherwise
# does not necessarily constitute or imply its endorsement, recommendation, or
# favoring by the United States Government or any agency thereof, or
# Battelle Memorial Institute. The views and opinions of authors expressed
# herein do not necessarily state or reflect those of the
# United States Government or any agency thereof.
#
# PACIFIC NORTHWEST NATIONAL LABORATORY operated by
# BATTELLE for the UNITED STATES DEPARTMENT OF ENERGY
# under Contract DE-AC05-76RL01830
# }}}

import argparse
import base64
import collections
import hashlib
import logging
import logging.handlers
import logging.config
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from typing import Optional
from datetime import timedelta, datetime

import gevent
import gevent.event

# noinspection PyUnresolvedReferences
import grequests
import requests
from requests.exceptions import ConnectionError

from volttron.platform import aip as aipmod
from volttron.platform import config
from volttron.platform import get_home, get_address
from volttron.platform import jsonapi
from volttron.platform.jsonrpc import MethodNotFound
from volttron.platform.agent import utils
from volttron.platform.agent.known_identities import (
    CONTROL_CONNECTION,
    CONFIGURATION_STORE,
    PLATFORM_HEALTH,
    AUTH,
)
from volttron.platform.auth import AuthEntry, AuthFile, AuthException
from volttron.platform.certs import Certs
from volttron.platform.jsonrpc import RemoteError
from volttron.platform.keystore import KeyStore, KnownHostsStore
from volttron.platform.messaging.health import Status, STATUS_BAD
from volttron.platform.scheduling import periodic
from volttron.platform.vip.agent import Agent as BaseAgent, Core, RPC
from volttron.platform.vip.agent.errors import VIPError, Unreachable
from volttron.platform.vip.agent.subsystems.query import Query
from volttron.utils.rmq_config_params import RMQConfig
from volttron.utils.rmq_mgmt import RabbitMQMgmt
from volttron.utils.rmq_setup import check_rabbit_status
from volttron.platform.agent.utils import is_secure_mode, \
    wait_for_volttron_shutdown
from volttron.platform.install_agents import add_install_agent_parser, InstallRuntimeError

try:
    import volttron.restricted
except ImportError:
    HAVE_RESTRICTED = False
else:
    from volttron.restricted import cgroups

    HAVE_RESTRICTED = True

_stdout = sys.stdout
_stderr = sys.stderr

# will be volttron.platform.main or main.py instead of __main__
_log = logging.getLogger(
    os.path.basename(sys.argv[0]) if __name__ == "__main__" else __name__
)
# Allows server side logging.
# _log.setLevel(logging.DEBUG)

message_bus = utils.get_messagebus()
rmq_mgmt = None

CHUNK_SIZE = 4096


class ControlService(BaseAgent):
    def __init__(
        self, aip: aipmod.AIPplatform, agent_monitor_frequency, *args, **kwargs
    ):

        tracker = kwargs.pop("tracker", None)
        # Control config store not necessary right now
        kwargs["enable_store"] = False
        kwargs["enable_channel"] = True
        super(ControlService, self).__init__(*args, **kwargs)
        self._aip = aip
        self._tracker = tracker
        self.crashed_agents = {}
        self.agent_monitor_frequency = int(agent_monitor_frequency)

        if self.core.publickey is None or self.core.secretkey is None:
            (
                self.core.publickey,
                self.core.secretkey,
                _,
            ) = self.core._get_keys_from_addr()
        if self.core.publickey is None or self.core.secretkey is None:
            (
                self.core.publickey,
                self.core.secretkey,
            ) = self.core._get_keys_from_keystore()

    @Core.receiver("onsetup")
    def _setup(self, sender, **kwargs):
        if not self._tracker:
            return
        self.vip.rpc.export(lambda: self._tracker.enabled, "stats.enabled")
        self.vip.rpc.export(self._tracker.enable, "stats.enable")
        self.vip.rpc.export(self._tracker.disable, "stats.disable")
        self.vip.rpc.export(lambda: self._tracker.stats, "stats.get")

    @Core.receiver("onstart")
    def onstart(self, sender, **kwargs):
        _log.debug(
            " agent monitor frequency is... {}".format(
                self.agent_monitor_frequency)
        )
        self.core.schedule(periodic(self.agent_monitor_frequency),
                           self._monitor_agents)

    def _monitor_agents(self):
        """
        Periodically look for agents that crashed and schedule a restart
        attempt. Attempts at most 5 times with increasing interval
        between attempts. Sends alert if attempts fail.
        """
        # Get status for agents that have been started at least once.
        stats = self._aip.status_agents()
        for (uid, name, (pid, stat), identity) in stats:
            if stat:
                # stat=0 means stopped and stat=None means running
                # will always have pid(current/crashed/stopped)
                attempt = self.crashed_agents.get(uid, -1) + 1
                if attempt < 5:
                    self.crashed_agents[uid] = attempt
                    next_restart = utils.get_aware_utc_now() + timedelta(
                        minutes=attempt * 5
                    )
                    _log.debug(
                        "{} stopped unexpectedly. Will attempt to "
                        "restart at {}".format(name, next_restart)
                    )
                    self.core.schedule(next_restart, self._restart_agent, uid,
                                       name)
                else:
                    self.send_alert(uid, name)
                    self.crashed_agents.pop(uid)

    def _restart_agent(self, agent_id, agent_name):
        """
        Checks if a given agent has crashed. If so attempts to restart it.
        If successful removes the agent id from list of crashed agents
        :param agent_id:
        :param agent_name:
        :return:
        """
        (id, stat) = self._aip.agent_status(agent_id)
        if stat:
            # if there is still some error status... attempt restart
            # call self.stop to inform router but call aip start to get
            # status back
            self.stop_agent(agent_id)
            (id, stat) = self._aip.start_agent(agent_id)
            if stat is None:
                # start successful
                self.crashed_agents.pop(agent_id)
                _log.info("Successfully restarted agent {}".format(agent_name))
            else:
                _log.info("Restart of {} failed".format(agent_name))

    def send_alert(self, agent_id, agent_name):
        """Send an alert for the group, summarizing missing topics."""
        alert_key = "Agent {}({}) stopped unexpectedly".format(agent_name,
                                                               agent_id)
        context = (
            "Agent {}({}) stopped unexpectedly. Attempts to "
            "restart failed".format(agent_name, agent_id)
        )
        status = Status.build(STATUS_BAD, context=context)
        self.vip.health.send_alert(alert_key, status)

    @RPC.export
    def peerlist(self):
        # We want to keep the same interface so we convert the byte array to
        # string array when returning.
        peer_list = self.vip.peerlist().get(timeout=5)
        return peer_list

    @RPC.export
    def serverkey(self):
        q = Query(self.core)
        pk = q.query("serverkey").get(timeout=1)
        del q
        return pk

    @RPC.export
    def clear_status(self, clear_all=False):
        self._aip.clear_status(clear_all)

    @RPC.export
    def agent_status(self, uuid):
        if not isinstance(uuid, str):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string for 'uuid';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )
        return self._aip.agent_status(uuid)

    @RPC.export
    def agent_name(self, uuid):
        if not isinstance(uuid, str):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string for 'uuid';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )
        return self._aip.agent_name(uuid)

    @RPC.export
    def agent_version(self, uuid):
        if not isinstance(uuid, str):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string for 'uuid';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )
        return self._aip.agent_version(uuid)

    @RPC.export
    def agent_versions(self):
        return self._aip.agent_versions()

    @RPC.export
    def status_agents(self, get_agent_user=False):
        return self._aip.status_agents(get_agent_user)

    @RPC.export
    def start_agent(self, uuid):
        if not isinstance(uuid, str):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string for 'uuid';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )
        self._aip.start_agent(uuid)

    @RPC.export
    def stop_agent(self, uuid):
        if not isinstance(uuid, str):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string for 'uuid';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )

        identity = self.agent_vip_identity(uuid)
        self._aip.stop_agent(uuid)
        # Send message to router that agent is shutting down
        frames = [identity]

        # Was self.core.socket.send_vip(b'', b'agentstop', frames, copy=False)
        self.core.connection.send_vip("", "agentstop", args=frames, copy=False)

    @RPC.export
    def restart_agent(self, uuid):
        self.stop_agent(uuid)
        self.start_agent(uuid)

    @RPC.export
    def shutdown(self):
        self._aip.shutdown()

    @RPC.export
    def stop_platform(self):
        # XXX: Restrict call as it kills the process
        self.core.connection.send_vip("", "quit")

    @RPC.export
    def list_agents(self):
        _log.info("CONTROL RPC list_agents")
        tag = self._aip.agent_tag
        priority = self._aip.agent_priority
        return [
            {
                "name": name,
                "uuid": uuid,
                "tag": tag(uuid),
                "priority": priority(uuid),
                "identity": self.agent_vip_identity(uuid),
            }
            for uuid, name in self._aip.list_agents().items()
        ]

    @RPC.export
    def tag_agent(self, uuid, tag):
        if not isinstance(uuid, str):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string for 'uuid';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )
        if not isinstance(tag, (type(None), str)):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string for 'tag';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )
        self._aip.tag_agent(uuid, tag)

    @RPC.export
    def remove_agent(self, uuid, remove_auth=True):
        if not isinstance(uuid, str):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string for 'uuid';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )

        identity = self.agent_vip_identity(uuid)
        # Because we are using send_vip we should pass frames that have
        # bytes rather than
        # strings.
        frames = [identity]

        # Send message to router that agent is shutting down
        self.core.connection.send_vip("", "agentstop", args=frames)
        self._aip.remove_agent(uuid, remove_auth=remove_auth)

    @RPC.export
    def prioritize_agent(self, uuid, priority="50"):
        if not isinstance(uuid, str):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string for 'uuid';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )
        if not isinstance(priority, (type(None), str)):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string or null for 'priority';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )
        self._aip.prioritize_agent(uuid, priority)

    @RPC.export
    def agent_vip_identity(self, uuid):
        """Lookup the agent's vip identity based upon it's uuid.

        @param uuid:
        @return:
        """
        if not isinstance(uuid, str):
            identity = bytes(self.vip.rpc.context.vip_message.peer).decode(
                "utf-8")
            raise TypeError(
                "expected a string for 'uuid';"
                "got {!r} from identity: {}".format(type(uuid).__name__,
                                                    identity)
            )
        return self._aip.agent_identity(uuid)

    @RPC.export
    def get_all_agent_publickeys(self):
        """
        RPC method to retrieve the public keys of all of the agents installed
        on the VOLTTRON instance.

        This method does not differentiate between running and not running
        agents.

        .. note::

            This method will only retrieve a publickey for an installed agents.
            It is recommended that dynamic agents use the context of the
            containing agent's publickey for connections to external instances.

        :return: mapping of identity to agent publickey
        :rtype: dict
        """
        id_map = self._aip.get_agent_identity_to_uuid_mapping()
        retmap = {}
        for id, uuid in id_map.items():
            retmap[id] = self._aip.get_agent_keystore(uuid).public
        return retmap

    @RPC.export
    def identity_exists(self, identity):
        if not identity:
            raise ValueError("Attribute identity cannot be None or empty")

        return self._identity_exists(identity)

    @RPC.export
    def install_agent_rmq(self, vip_identity, filename, topic, force,
                          response_topic):
        """
        Install the agent through the rmq message bus.
        """
        peer = self.vip.rpc.context.vip_message.peer

        protocol_message = None
        protocol_headers = None
        response_received = False

        def protocol_subscription(peer, sender, bus, topic, headers, message):
            nonlocal protocol_message, protocol_headers, response_received
            _log.debug(f"Received topic, message topic {topic}, {message}")
            protocol_message = message
            protocol_message = base64.b64decode(
                protocol_message.encode("utf-8"))
            protocol_headers = headers
            response_received = True

        agent_uuid, agent_existed_before = self._identity_exists_but_no_force(
            vip_identity, force
        )
        try:
            tmpdir = tempfile.mkdtemp()
            path = os.path.join(tmpdir, os.path.basename(filename))
            store = open(path, "wb")
            sha512 = hashlib.sha512()

            try:
                request_checksum = base64.b64encode(
                    jsonapi.dumps(["checksum"]).encode("utf-8")
                ).decode("utf-8")
                request_fetch = base64.b64encode(
                    jsonapi.dumps(["fetch", 1024]).encode("utf-8")
                ).decode("utf-8")

                _log.debug(f"Server subscribing to {topic}")
                self.vip.pubsub.subscribe(
                    peer="pubsub", prefix=topic, callback=protocol_subscription
                ).get(timeout=5)
                gevent.sleep(5)
                _log.debug("AFTER SUBSCRIPTION")
                while True:

                    _log.debug(
                        f"Requesting data {request_fetch} sending to "
                        f"{response_topic}"
                    )
                    response_received = False

                    # request a chunk of the filecl
                    self.vip.pubsub.publish(
                        "pubsub", topic=response_topic, message=request_fetch
                    ).get(timeout=5)
                    # chunk binary representation of the bytes read from
                    # the other side of the connectoin
                    with gevent.Timeout(30):
                        _log.debug("Waiting for chunk")
                        while not response_received:
                            gevent.sleep(0.1)

                    # Chunk will be bytes
                    chunk = protocol_message
                    _log.debug(f"chunk is {chunk}")
                    if chunk == b"complete":
                        _log.debug(f"File transfer complete!")
                        break

                    sha512.update(chunk)
                    store.write(chunk)

                    with gevent.Timeout(30):
                        _log.debug("Requesting checksum")
                        response_received = False
                        self.vip.pubsub.publish(
                            "pubsub", topic=response_topic,
                            message=request_checksum
                        ).get(timeout=5)

                        while not response_received:
                            gevent.sleep(0.1)

                        checksum = protocol_message
                        assert checksum == sha512.digest()

                _log.debug("Outside of while loop in install agent service.")

            except AssertionError:
                _log.warning("Checksum mismatch on received file")
                raise
            except gevent.Timeout:
                _log.warning("Gevent timeout trying to receive data")
                raise
            finally:
                store.close()
                self.vip.pubsub.unsubscribe(
                    "pubsub", response_topic, protocol_subscription
                )
                _log.debug("Unsubscribing on server")

                _log.debug("After transfering wheel to us now to do stuff.")
                agent_data_dir = None
                backup_agent_file = None

            agent_uuid = self._install_wheel_to_platform(
                agent_uuid, vip_identity, path, agent_existed_before
            )
            return agent_uuid
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _install_wheel_to_platform(
        self, agent_uuid, vip_identity, path, agent_existed_before
    ):
        agent_data_dir = None
        backup_agent_file = None
        # Fix unbound variable.  Only gets set if there is an existing agent
        # already.
        publickey = None
        secretkey = None
        # Note if this is anything then we know we have already got an agent
        # mapped to the identity.
        if agent_uuid:
            _log.debug(f"There is an existing agent {agent_uuid}")
            backup_agent_file = "/tmp/{}.tar.gz".format(agent_uuid)
            if agent_uuid:
                agent_data_dir = self._aip.create_agent_data_dir_if_missing(
                    agent_uuid)

                if agent_data_dir:
                    backup_agent_data(backup_agent_file, agent_data_dir)

                keystore = self._aip.get_agent_keystore(agent_uuid)
                publickey = keystore.public
                secretkey = keystore.secret
                _log.info(
                    'Removing previous version of agent "{}"\n'.format(
                        vip_identity)
                )
                self.remove_agent(agent_uuid)
        _log.debug("Calling aip install_agent.")
        agent_uuid = self._aip.install_agent(
            path, vip_identity=vip_identity, publickey=publickey,
            secretkey=secretkey
        )

        if agent_existed_before and backup_agent_file is not None:
            restore_agent_data_from_tgz(
                backup_agent_file,
                self._aip.create_agent_data_dir_if_missing(agent_uuid),
            )
        _log.debug(f"Returning {agent_uuid}")
        return agent_uuid

    @RPC.export
    def install_agent(
        self,
        filename,
        channel_name,
        vip_identity=None,
        publickey=None,
        secretkey=None,
        force=False,
    ):
        """
        Installs an agent on the instance instance.

        The installation of an agent through this method involves sending
        the binary data of the agent file through a channel.  The following
        example is the protocol for sending the agent across the wire:

        Example Protocol:

        .. code-block:: python

            # client creates channel to this agent (control)
            channel = agent.vip.channel('control', 'channel_name')

            # Begin sending data
            sha512 = hashlib.sha512()
            while True:
                request, file_offset, chunk_size = channel.recv_multipart()

                # Control has all of the file. Send hash for for it to verify.
                if request == b'checksum':
                    channel.send(hash)
                assert request == b'fetch'

                # send a chunk of the file
                file_offset = int(file_offset)
                chunk_size = int(chunk_size)
                file.seek(file_offset)
                data = file.read(chunk_size)
                sha512.update(data)
                channel.send(data)

            agent_uuid = agent_uuid.get(timeout=10)
            # close and delete the channel
            channel.close(linger=0)
            del channel

        :param:string:filename:
            The name of the agent packaged file that is being written.
        :param:string:channel_name:
            The name of the channel that the agent file will be sent on.
        :param:string:publickey:
            Encoded public key the installed agent will use
        :param:string:secretkey:
            Encoded secret key the installed agent will use
        :param:string:force:
            Boolean value specifying whether the existence of an identity
            should
            reinstall or cause an error.
        """

        # at this point if agent_uuid is populated then there is an
        # identity of that already available.
        agent_uuid, agent_existed_before = self._identity_exists_but_no_force(
            vip_identity, force
        )
        _log.debug(f"rpc: install_agent {agent_uuid}")
        # Prepare to install agent that is passed over to us.
        peer = self.vip.rpc.context.vip_message.peer
        channel = self.vip.channel(peer, channel_name)
        try:
            tmpdir = tempfile.mkdtemp()
            path = os.path.join(tmpdir, os.path.basename(filename))
            store = open(path, "wb")
            sha512 = hashlib.sha512()

            try:
                request_checksum = jsonapi.dumpb(["checksum"])
                request_fetch = jsonapi.dumpb(["fetch", 1024])
                while True:

                    # request a chunk of the file
                    channel.send(request_fetch)

                    # chunk binary representation of the bytes read from
                    # the other side of the connectoin
                    with gevent.Timeout(30):
                        _log.debug("Waiting for chunk")
                        chunk = channel.recv()
                        _log.debug(f"chunk is {chunk}")
                        if chunk == b"complete":
                            _log.debug(f"File transfer complete!")
                            break

                    sha512.update(chunk)
                    store.write(chunk)

                    with gevent.Timeout(30):
                        channel.send(request_checksum)
                        checksum = channel.recv()

                        assert checksum == sha512.digest()
                _log.debug("Outside of while loop in install agent service.")

            except AssertionError:
                _log.warning("Checksum mismatch on received file")
                raise
            except gevent.Timeout:
                _log.warning("Gevent timeout trying to receive data")
                raise
            finally:
                store.close()
                _log.debug("Closing channel on server")
                channel.close(linger=0)
                del channel

            _log.debug("After transfering wheel to us now to do stuff.")
            agent_uuid = self._install_wheel_to_platform(
                agent_uuid, vip_identity, path, agent_existed_before
            )
            return agent_uuid
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def _identity_exists_but_no_force(self, vip_identity: str, force: bool):
        """
        This will raise a ValueError if the identity passed exists but
        force was not True when this function is called.

        This function should be called before any agent is installed through
        the respective message buses.
        """
        # at this point if agent_uuid is populated then there is an
        # identity of that already available.
        agent_uuid = None
        if vip_identity:
            agent_uuid = self._identity_exists(vip_identity)
        agent_existed_before = False
        if agent_uuid:
            agent_existed_before = True
            if not force:
                raise ValueError("Identity already exists, but not forced!")
        return agent_uuid, agent_existed_before

    def _identity_exists(self, identity: str) -> Optional[str]:
        """
        Determines if an agent identity is already installed.  This
        function returns the agent uuid of the agent with the passed
        identity.  If the identity
        doesn't exist then returns None.
        """
        results = self.list_agents()
        if not results:
            return None

        for x in results:
            if x["identity"] == identity:
                return x["uuid"]
        return None

        # dict_results = dict((k, v) for k, v in results)
        # #json_results = jsonapi.loads(results)
        # agent_ctx = dict_results.get(identity)
        # if agent_ctx:
        #     return agent_ctx['agent_uuid']


def log_to_file(file, level=logging.WARNING,
                handler_class=logging.StreamHandler):
    """Direct log output to a file (or something like one)."""
    handler = handler_class(file)
    handler.setLevel(level)
    handler.setFormatter(
        utils.AgentFormatter(
            "%(asctime)s %(composite_name)s %(levelname)s: %(message)s"
        )
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)


Agent = collections.namedtuple("Agent",
                               "name tag uuid vip_identity agent_user")


def _list_agents(aip):
    return [
        Agent(name, aip.agent_tag(uuid), uuid, aip.agent_identity(uuid), "")
        for uuid, name in aip.list_agents().items()
    ]


def escape(pattern):
    strings = re.split(r"([*?])", pattern)
    if len(strings) == 1:
        return re.escape(pattern), False
    return (
        "".join(
            ".*"
            if s == "*"
            else "."
            if s == "?"
            else s
            if s in [r"\?", r"\*"]
            else re.escape(s)
            for s in strings
        ),
        True,
    )


def filter_agents(agents, patterns, opts):
    by_name, by_tag, by_uuid = opts.by_name, opts.by_tag, opts.by_uuid
    for pattern in patterns:
        regex, _ = escape(pattern)
        result = set()

        # if no option is selected, try matching based on uuid
        if not (by_uuid or by_name or by_tag):
            reobj = re.compile(regex)
            matches = [agent for agent in agents if reobj.match(agent.uuid)]
            if len(matches) == 1:
                result.update(matches)
            # if no match is found based on uuid, try matching on agent name
            elif len(matches) == 0:
                matches = [agent for agent in agents if
                           reobj.match(agent.name)]
                if len(matches) >= 1:
                    result.update(matches)
        else:
            reobj = re.compile(regex + "$")
            if by_uuid:
                result.update(
                    agent for agent in agents if reobj.match(agent.uuid))
            if by_name:
                result.update(
                    agent for agent in agents if reobj.match(agent.name))
            if by_tag:
                result.update(
                    agent for agent in agents if reobj.match(agent.tag or ""))
        yield pattern, result


def filter_agent(agents, pattern, opts):
    return next(filter_agents(agents, [pattern], opts))[1]


def backup_agent_data(output_filename, source_dir):
    with tarfile.open(output_filename, "w:gz") as tar:
        tar.add(source_dir,
                arcname=os.path.sep)  # os.path.basename(source_dir))


def restore_agent_data_from_tgz(source_file, output_dir):
    # Open tarfile
    with tarfile.open(source_file, mode="r:gz") as tar:
        tar.extractall(output_dir)


def find_agent_data_dir(opts, agent_uuid):
    # Find agent-data directory path, create if missing
    agent_data_dir = opts.aip.create_agent_data_dir_if_missing(agent_uuid)
    return agent_data_dir


def upgrade_agent(opts):
    publickey = None
    secretkey = None

    identity = opts.vip_identity
    if not identity:
        raise ValueError("Missing required VIP IDENTITY option")

    identity_to_uuid = opts.aip.get_agent_identity_to_uuid_mapping()
    agent_uuid = identity_to_uuid.get(identity, None)
    backup_agent_file = "/tmp/{}.tar.gz".format(agent_uuid)
    if agent_uuid:
        agent_data_dir = find_agent_data_dir(opts, agent_uuid)

        if agent_data_dir:
            backup_agent_data(backup_agent_file, agent_data_dir)

        keystore = opts.aip.get_agent_keystore(agent_uuid)
        publickey = keystore.public
        secretkey = keystore.secret
        _stdout.write(
            'Removing previous version of agent "{}"\n'.format(identity))
        opts.connection.call("remove_agent", agent_uuid, remove_auth=False)
    else:
        _stdout.write(
            (
                'Could not find agent with VIP IDENTITY "{}". '
                "Installing as new agent\n"
            ).format(identity)
        )

    if secretkey is None or publickey is None:
        publickey = None
        secretkey = None

    def restore_agents_data(agent_uuid):
        # if we are  upgrading transfer the old data on.
        if os.path.exists(backup_agent_file):
            new_agent_data_dir = find_agent_data_dir(opts, agent_uuid)
            restore_agent_data_from_tgz(backup_agent_file, new_agent_data_dir)
            os.remove(backup_agent_file)

    install_agent(
        opts, publickey=publickey, secretkey=secretkey,
        callback=restore_agents_data
    )


def tag_agent(opts):
    agents = filter_agent(_list_agents(opts.aip), opts.agent, opts)
    if len(agents) != 1:
        if agents:
            msg = "multiple agents selected"
        else:
            msg = "agent not found"
        _stderr.write(
            "{}: error: {}: {}\n".format(opts.command, msg, opts.agent))
        return 10
    (agent,) = agents
    if opts.tag:
        _stdout.write("Tagging {} {}\n".format(agent.uuid, agent.name))
        opts.aip.tag_agent(agent.uuid, opts.tag)
    elif opts.remove:
        if agent.tag is not None:
            _stdout.write(
                "Removing tag for {} {}\n".format(agent.uuid, agent.name))
            opts.aip.tag_agent(agent.uuid, None)
    else:
        if agent.tag is not None:
            _stdout.writelines([agent.tag, "\n"])


def remove_agent(opts, remove_auth=True):
    agents = _list_agents(opts.aip)
    for pattern, match in filter_agents(agents, opts.pattern, opts):
        if not match:
            _stderr.write(
                "{}: error: agent not found: {}\n".format(opts.command,
                                                          pattern)
            )
        elif len(match) > 1 and not opts.force:
            _stderr.write(
                "{}: error: pattern returned multiple agents: {}\n".format(
                    opts.command, pattern
                )
            )
            _stderr.write(
                "Use -f or --force to force removal of multiple agents.\n")
            return 10
        for agent in match:
            _stdout.write("Removing {} {}\n".format(agent.uuid, agent.name))
            opts.connection.call("remove_agent", agent.uuid,
                                 remove_auth=remove_auth)


def _calc_min_uuid_length(agents):
    n = 0
    for agent1 in agents:
        for agent2 in agents:
            if agent1 is agent2:
                continue
            common_len = len(os.path.commonprefix([agent1.uuid, agent2.uuid]))
            if common_len > n:
                n = common_len
    return n + 1


def list_agents(opts):
    def get_priority(agent):
        return opts.aip.agent_priority(agent.uuid) or ""

    _show_filtered_agents(opts, "PRI", get_priority)


def list_peers(opts):
    conn = opts.connection
    peers = sorted(conn.call("peerlist"))
    for peer in peers:
        sys.stdout.write("{}\n".format(peer))


def print_rpc_list(peers, code=False):
    for peer in peers:
        print(f"{peer}")
        for method in peers[peer]:
            if code:
                print(f"\tself.vip.rpc.call({peer}, {method}).get()")
            else:
                print(f"\t{method}")


def print_rpc_methods(opts, peer_method_metadata, code=False):
    for peer in peer_method_metadata:
        if code is True:
            pass
        else:
            print(f"{peer}")
        for method in peer_method_metadata[peer]:
            params = peer_method_metadata[peer][method].get(
                "params", "No parameters for this method."
            )
            if code is True:
                if len(params) == 0:
                    print(f"self.vip.rpc.call({peer}, {method}).get()")
                else:
                    print(
                        f"self.vip.rpc.call({peer}, {method}, "
                        f"{[param for param in params]}).get()"
                    )
                continue
            else:
                print(f"\t{method}")
                if opts.verbose == True:
                    print("\tDocumentation:")
                    doc = (
                        peer_method_metadata[peer][method]
                            .get("doc", "No documentation for this method.")
                            .replace("\n", "\n\t\t")
                    )
                    print(f"\t\t{doc}\n")
            print("\tParameters:")
            if type(params) is str:
                print(f"\t\t{params}")
            else:
                for param in params:
                    print(f"\t\t{param}:\n\t\t\t{params[param]}")


def list_agents_rpc(opts):
    conn = opts.connection
    try:
        peers = sorted(conn.call("peerlist"))
    except Exception as e:
        print(e)
    if opts.by_vip == True or len(opts.pattern) == 1:
        peers = [peer for peer in peers if peer in opts.pattern]
    elif len(opts.pattern) > 1:
        peer = opts.pattern[0]
        methods = opts.pattern[1:]
        peer_method_metadata = {peer: {}}
        for method in methods:
            try:
                peer_method_metadata[peer][method] = conn.server.vip.rpc.call(
                    peer, f"{method}.inspect"
                ).get(timeout=4)
                authorized_capabilities = conn.server.vip.rpc.call(
                    peer, "auth.get_rpc_authorizations", method
                ).get(timeout=4)
                peer_method_metadata[peer][method][
                    "authorized_capabilities"
                ] = f"Authorized capabilities: {authorized_capabilities}"
            except gevent.Timeout:
                print(f"{peer} has timed out.")
            except Unreachable:
                print(f"{peer} is unreachable")
            except MethodNotFound as e:
                print(e)

        # _stdout.write(f"{peer_method_metadata}\n")
        print_rpc_methods(opts, peer_method_metadata)
        return
    peer_methods = {}
    for peer in peers:
        try:
            peer_methods[peer] = conn.server.vip.rpc.call(peer, "inspect").get(
                timeout=4
            )["methods"]
        except gevent.Timeout:
            print(f"{peer} has timed out")
        except Unreachable:
            print(f"{peer} is unreachable")
        except MethodNotFound as e:
            print(e)

    if opts.verbose is True:
        print_rpc_list(peer_methods)
        # for peer in peer_methods:
        #     _stdout.write(f"{peer}:{peer_methods[peer]}\n")
    else:
        for peer in peer_methods:
            peer_methods[peer] = [
                method for method in peer_methods[peer] if "." not in method
            ]
            # _stdout.write(f"{peer}:{peer_methods[peer]}\n")
        print_rpc_list(peer_methods)


def list_agent_rpc_code(opts):
    conn = opts.connection
    try:
        peers = sorted(conn.call("peerlist"))
    except Exception as e:
        print(e)
    if len(opts.pattern) == 1:
        peers = [peer for peer in peers if peer in opts.pattern]
    elif len(opts.pattern) > 1:
        peer = opts.pattern[0]
        methods = opts.pattern[1:]
        peer_method_metadata = {peer: {}}
        for method in methods:
            try:
                peer_method_metadata[peer][method] = conn.server.vip.rpc.call(
                    peer, f"{method}.inspect"
                ).get(timeout=4)
            except gevent.Timeout:
                print(f"{peer} has timed out.")
            except Unreachable:
                print(f"{peer} is unreachable")
            except MethodNotFound as e:
                print(e)

        # _stdout.write(f"{peer_method_metadata}\n")
        print_rpc_methods(opts, peer_method_metadata, code=True)
        return

    peer_methods = {}
    for peer in peers:
        try:
            peer_methods[peer] = conn.server.vip.rpc.call(peer, "inspect").get(
                timeout=4
            )["methods"]
        except gevent.Timeout:
            print(f"{peer} has timed out.")
        except Unreachable:
            print(f"{peer} is unreachable")
        except MethodNotFound as e:
            print(e)

    if opts.verbose is True:
        pass
    else:
        for peer in peer_methods:
            peer_methods[peer] = [
                method for method in peer_methods[peer] if "." not in method
            ]

    peer_method_metadata = {}
    for peer in peer_methods:
        peer_method_metadata[peer] = {}
        for method in peer_methods[peer]:
            try:
                peer_method_metadata[peer][method] = conn.server.vip.rpc.call(
                    peer, f"{method}.inspect"
                ).get(timeout=4)
            except gevent.Timeout:
                print(f"{peer} has timed out")
            except Unreachable:
                print(f"{peer} is unreachable")
            except MethodNotFound as e:
                print(e)
    print_rpc_methods(opts, peer_method_metadata, code=True)


def list_remotes(opts):
    """Lists remote certs and credentials.
    Can be filters using the '--status' option, specifying
    pending, approved, or denied.
    The output printed includes:
        user id of a ZMQ credential, or the common name of a CSR
        remote address of the credential or csr
        status of the credential or cert (either APPROVED, DENIED, or PENDING)

    """
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return

    output_view = []
    try:
        pending_csrs = conn.server.vip.rpc.call(AUTH, "get_pending_csrs").get(
            timeout=4)
        for csr in pending_csrs:
            output_view.append(
                {
                    "entry": {
                        "user_id": csr["identity"],
                        "address": csr["remote_ip_address"],
                    },
                    "status": csr["status"],
                }
            )
    except TimeoutError:
        print("Certs timed out")
    try:
        approved_certs = conn.server.vip.rpc.call(
            AUTH, "get_authorization_approved"
        ).get(timeout=4)
        for value in approved_certs:
            output_view.append({"entry": value, "status": "APPROVED"})
    except TimeoutError:
        print("Approved credentials timed out")
    try:
        denied_certs = conn.server.vip.rpc.call(AUTH,
                                                "get_authorization_denied").get(
            timeout=4
        )
        for value in denied_certs:
            output_view.append({"entry": value, "status": "DENIED"})
    except TimeoutError:
        print("Denied credentials timed out")
    try:
        pending_certs = conn.server.vip.rpc.call(AUTH,
                                                 "get_authorization_pending").get(
            timeout=4
        )
        for value in pending_certs:
            output_view.append({"entry": value, "status": "PENDING"})
    except TimeoutError:
        print("Pending credentials timed out")

    if not output_view:
        print("No remote certificates or credentials")
        return

    if opts.status == "approved":
        output_view = [
            output for output in output_view if output["status"] == "APPROVED"
        ]

    elif opts.status == "denied":
        output_view = [output for output in output_view if
                       output["status"] == "DENIED"]

    elif opts.status == "pending":
        output_view = [
            output for output in output_view if output["status"] == "PENDING"
        ]

    elif opts.status is not None:
        _stdout.write(
            "Invalid parameter. Please use 'approved', 'denied', 'pending', "
            "or leave blank to list all.\n"
        )
        return

    if len(output_view) == 0:
        print(f"No {opts.status} remote certificates or credentials")
        return

    for output in output_view:
        for value in output["entry"]:
            if not output["entry"][value]:
                output["entry"][value] = "-"

    userid_width = max(
        5, max(len(str(output["entry"]["user_id"])) for output in output_view)
    )
    address_width = max(
        5, max(len(str(output["entry"]["address"])) for output in output_view)
    )
    status_width = max(5, max(
        len(str(output["status"])) for output in output_view))
    fmt = "{:{}} {:{}} {:{}}\n"
    _stderr.write(
        fmt.format(
            "USER_ID", userid_width, "ADDRESS", address_width, "STATUS",
            status_width
        )
    )
    fmt = "{:{}} {:{}} {:{}}\n"
    for output in output_view:
        _stdout.write(
            fmt.format(
                output["entry"]["user_id"],
                userid_width,
                output["entry"]["address"],
                address_width,
                output["status"],
                status_width,
            )
        )


def approve_remote(opts):
    """Approves either a pending CSR or ZMQ credential.
    The platform must be running for this command to succeed.
    :param opts.user_id: The ZMQ credential user_id or pending CSR common name
    :type opts.user_id: str
    """
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    conn.server.vip.rpc.call(AUTH, "approve_authorization_failure",
                             opts.user_id).get(
        timeout=4
    )


def deny_remote(opts):
    """Denies either a pending CSR or ZMQ credential.
    The platform must be running for this command to succeed.
    :param opts.user_id: The ZMQ credential user_id or pending CSR common name
    :type opts.user_id: str
    """
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    conn.server.vip.rpc.call(AUTH, "deny_authorization_failure",
                             opts.user_id).get(
        timeout=4
    )


def delete_remote(opts):
    """Deletes either a pending CSR or ZMQ credential.
    The platform must be running for this command to succeed.
    :param opts.user_id: The ZMQ credential user_id or pending CSR common name
    :type opts.user_id: str
    """
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    conn.server.vip.rpc.call(AUTH, "delete_authorization_failure",
                             opts.user_id).get(
        timeout=4
    )


# the following global variables are used to update the cache so
# that we don't ask the platform too many times for the data
# associated with health.
health_cache_timeout_date = None
health_cache_timeout = 5
health_cache = {}


def update_health_cache(opts):
    global health_cache_timeout_date

    t_now = datetime.now()
    do_update = True
    # Make sure we update if we don't have any health dicts, or if the cache
    # has timed out.
    if (
        health_cache_timeout_date is not None
        and t_now < health_cache_timeout_date
        and health_cache
    ):
        do_update = False

    if do_update:
        health_cache.clear()
        if opts.connection.server:
            health_cache.update(
                opts.connection.server.vip.rpc.call(
                    PLATFORM_HEALTH, "get_platform_health"
                ).get(timeout=4)
            )
            health_cache_timeout_date = datetime.now() + timedelta(
                seconds=health_cache_timeout
            )


def add_agent_rpc_authorizations(opts):
    """
    Adds authorizations to method in auth entry in auth file.

    :param opts: Contains command line pattern and connection
    :return: None
    """
    conn = opts.connection
    agent_id = ".".join(opts.pattern[0].split(".")[:-1])
    agent_method = opts.pattern[0].split(".")[-1]
    if len(opts.pattern) < 2:
        _log.error(
            "Missing authorizations for method. "
            "Should be in the format agent_id.method "
            "authorized_capability1 authorized_capability2 ..."
        )
        return
    added_auths = [x for x in opts.pattern[1:]]
    try:
        conn.server.vip.rpc.call(
            AUTH, "add_rpc_authorizations", agent_id, agent_method, added_auths
        ).get(timeout=4)
    except TimeoutError:
        _log.error(
            f"Adding RPC authorizations {added_auths} for {agent_id}'s "
            f"method {agent_method} timed out"
        )
    except Exception as e:
        _log.error(
            f"{e}) \nCommand format should be agent_id.method "
            f"authorized_capability1 authorized_capability2 ..."
        )
    return


def remove_agent_rpc_authorizations(opts):
    """
    Removes authorizations to method in auth entry in auth file.

    :param opts: Contains command line pattern and connection
    :return: None
    """
    conn = opts.connection
    agent_id = ".".join(opts.pattern[0].split(".")[:-1])
    agent_method = opts.pattern[0].split(".")[-1]
    if len(opts.pattern) < 2:
        _log.error(
            "Missing authorizations for method. "
            "Should be in the format agent_id.method "
            "authorized_capability1 authorized_capability2 ..."
        )
        return
    removed_auths = [x for x in opts.pattern[1:]]
    try:
        conn.server.vip.rpc.call(
            AUTH,
            "delete_rpc_authorizations",
            agent_id,
            agent_method,
            removed_auths,
        ).get(timeout=4)
    except TimeoutError:
        _log.error(
            f"Adding RPC authorizations {removed_auths} for {agent_id}'s "
            f"method {agent_method} timed out"
        )
    except Exception as e:
        _log.error(
            f"{e}) \nCommand format should be agent_id.method "
            f"authorized_capability1 authorized_capability2 ..."
        )
    return


def status_agents(opts):
    agents = {agent.uuid: agent for agent in _list_agents(opts.aip)}
    status = {}
    for details in opts.connection.call("status_agents", get_agent_user=True):
        if is_secure_mode():
            (uuid, name, agent_user, stat, identity) = details
        else:
            (uuid, name, stat, identity) = details
            agent_user = ""
        try:
            agent = agents[uuid]
            agents[uuid] = agent._replace(agent_user=agent_user)
        except KeyError:
            agents[uuid] = agent = Agent(
                name, None, uuid, vip_identity=identity, agent_user=agent_user
            )
        status[uuid] = stat
    agents = list(agents.values())

    def get_status(agent):
        try:
            pid, stat = status[agent.uuid]
        except KeyError:
            pid = stat = None

        if stat is not None:
            return str(stat)
        if pid:
            return "running [{}]".format(pid)
        return ""

    def get_health(agent):
        update_health_cache(opts)

        try:
            health_dict = health_cache.get(agent.vip_identity)

            if health_dict:
                if opts.json:
                    return health_dict
                else:
                    return health_dict.get("message", "")
            else:
                return ""
        except (VIPError, gevent.Timeout):
            return ""

    _show_filtered_agents_status(opts, get_status, get_health, agents)


def agent_health(opts):
    agents = {agent.uuid: agent for agent in _list_agents(opts.aip)}.values()
    agents = get_filtered_agents(opts, agents)
    if not agents:
        if not opts.json:
            _stderr.write("No installed Agents found\n")
        else:
            _stdout.write(f"{jsonapi.dumps({}, indent=2)}\n")
        return
    agent = agents.pop()
    update_health_cache(opts)

    data = health_cache.get(agent.vip_identity)

    if not data:
        if not opts.json:
            _stdout.write(f"No health associated with {agent.vip_identity}\n")
        else:
            _stdout.write(f"{jsonapi.dumps({}, indent=2)}\n")
    else:
        _stdout.write(f"{jsonapi.dumps(data, indent=4)}\n")


def clear_status(opts):
    opts.connection.call("clear_status", opts.clear_all)


def enable_agent(opts):
    agents = _list_agents(opts.aip)
    for pattern, match in filter_agents(agents, opts.pattern, opts):
        if not match:
            _stderr.write(
                "{}: error: agent not found: {}\n".format(opts.command,
                                                          pattern)
            )
        for agent in match:
            _stdout.write(
                "Enabling {} {} with priority {}\n".format(
                    agent.uuid, agent.name, opts.priority
                )
            )
            opts.aip.prioritize_agent(agent.uuid, opts.priority)


def disable_agent(opts):
    agents = _list_agents(opts.aip)
    for pattern, match in filter_agents(agents, opts.pattern, opts):
        if not match:
            _stderr.write(
                "{}: error: agent not found: {}\n".format(opts.command,
                                                          pattern)
            )
        for agent in match:
            priority = opts.aip.agent_priority(agent.uuid)
            if priority is not None:
                _stdout.write(
                    "Disabling {} {}\n".format(agent.uuid, agent.name))
                opts.aip.prioritize_agent(agent.uuid, None)


def start_agent(opts):
    call = opts.connection.call
    agents = _list_agents(opts.aip)
    for pattern, match in filter_agents(agents, opts.pattern, opts):
        if not match:
            _stderr.write(
                "{}: error: agent not found: {}\n".format(opts.command,
                                                          pattern)
            )
        for agent in match:
            pid, status = call("agent_status", agent.uuid)
            if pid is None or status is not None:
                _stdout.write(
                    "Starting {} {}\n".format(agent.uuid, agent.name))
                call("start_agent", agent.uuid)


def stop_agent(opts):
    call = opts.connection.call
    agents = _list_agents(opts.aip)
    for pattern, match in filter_agents(agents, opts.pattern, opts):
        if not match:
            _stderr.write(
                "{}: error: agent not found: {}\n".format(opts.command,
                                                          pattern)
            )
        for agent in match:
            pid, status = call("agent_status", agent.uuid)
            if pid and status is None:
                _stdout.write(
                    "Stopping {} {}\n".format(agent.uuid, agent.name))
                call("stop_agent", agent.uuid)


def restart_agent(opts):
    stop_agent(opts)
    start_agent(opts)


def run_agent(opts):
    call = opts.connection.call
    for directory in opts.directory:
        call("run_agent", directory)


def shutdown_agents(opts):
    if "rmq" == utils.get_messagebus():
        if not check_rabbit_status():
            rmq_cfg = RMQConfig()
            wait_period = (
                rmq_cfg.reconnect_delay() if rmq_cfg.reconnect_delay() < 60
                else 60
            )
            _stderr.write(
                "RabbitMQ server is not running.\n"
                "Waiting for {} seconds for possible reconnection and to "
                "perform normal shutdown\n".format(
                    wait_period
                )
            )
            gevent.sleep(wait_period)
            if not check_rabbit_status():
                _stderr.write(
                    "RabbitMQ server is still not running.\nShutting down "
                    "the platform forcefully\n"
                )
                opts.aip.brute_force_platform_shutdown()
                return
    opts.connection.call("shutdown")
    _log.debug("Calling stop_platform")
    if opts.platform:
        opts.connection.notify("stop_platform")
        wait_for_volttron_shutdown(get_home(), 60)


def create_cgroups(opts):
    try:
        cgroups.setup(user=opts.user, group=opts.group)
    except ValueError as exc:
        _stderr.write("{}: error: {}\n".format(opts.command, exc))
        return os.EX_NOUSER


def _send_agent(connection, peer, path):
    wheel = open(path, "rb")
    channel = connection.vip.channel(peer)

    def send():
        try:
            # Wait for peer to open compliment channel
            channel.recv()
            while True:
                data = wheel.read(8192)
                channel.send(data)
                if not data:
                    break
            # Wait for peer to signal all data received
            channel.recv()
        finally:
            wheel.close()
            channel.close(linger=0)

    result = connection.vip.rpc.call(
        peer, "install_agent", os.path.basename(path), channel.name
    )
    task = gevent.spawn(send)
    result.rawlink(lambda glt: task.kill(block=False))
    _log.debug(f"Result is {result}")
    return result


def send_agent(opts):
    connection = opts.connection
    for wheel in opts.wheel:
        uuid = _send_agent(connection.server, connection.peer, wheel).get()
        return uuid


def gen_keypair(opts):
    keypair = KeyStore.generate_keypair_dict()
    _stdout.write("{}\n".format(jsonapi.dumps(keypair, indent=2)))


def add_server_key(opts):
    store = KnownHostsStore()
    store.add(opts.host, opts.serverkey)
    _stdout.write("server key written to {}\n".format(store.filename))


def list_known_hosts(opts):
    store = KnownHostsStore()
    entries = store.load()
    if entries:
        _print_two_columns(entries, "HOST", "CURVE KEY")
    else:
        _stdout.write("No entries in {}\n".format(store.filename))


def remove_known_host(opts):
    store = KnownHostsStore()
    store.remove(opts.host)
    _stdout.write(
        'host "{}" removed from {}\n'.format(opts.host, store.filename))


def do_stats(opts):
    call = opts.connection.call
    if opts.op == "status":
        _stdout.write("%sabled\n" % ("en" if call("stats.enabled") else "dis"))
    elif opts.op in ["dump", "pprint"]:
        stats = call("stats.get")
        if opts.op == "pprint":
            import pprint

            pprint.pprint(stats, _stdout)
        else:
            _stdout.writelines([str(stats), "\n"])
    else:
        call("stats." + opts.op)
        _stdout.write("%sabled\n" % ("en" if call("stats.enabled") else "dis"))


def show_serverkey(opts):
    """
    write serverkey to standard out.

    return 0 if success, 1 if false
    """
    q = Query(opts.connection.server.core)
    pk = q.query("serverkey").get(timeout=2)
    del q
    if pk is not None:
        _stdout.write("%s\n" % pk)
        return 0

    return 1


def _print_two_columns(dict_, key_name, value_name):
    padding = 2
    key_lengths = [len(key) for key in dict_] + [len(key_name)]
    max_key_len = max(key_lengths) + padding
    _stdout.write(
        "{}{}{}\n".format(key_name, " " * (max_key_len - len(key_name)),
                          value_name)
    )
    _stdout.write(
        "{}{}{}\n".format(
            "-" * len(key_name),
            " " * (max_key_len - len(key_name)),
            "-" * len(value_name),
        )
    )
    for key in sorted(dict_):
        value = dict_[key]
        if isinstance(value, list):
            value = sorted(value)
        _stdout.write(
            "{}{}{}\n".format(key, " " * (max_key_len - len(key)), value))


def list_auth(opts, indices=None):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return

    entries = conn.server.vip.rpc.call(AUTH, "auth_file.read").get()[
        "allow_list"]
    print_out = []
    if entries:
        for index, entry in enumerate(entries):
            if indices is None or index in indices:
                _stdout.write("\nINDEX: {}\n".format(index))
                _stdout.write("{}\n".format(jsonapi.dumps(entry, indent=2)))
    else:
        _stdout.write(
            "No entries in {}\n".format(os.path.join(get_home(), "auth.json"))
        )


def _ask_for_auth_fields(
    domain=None,
    address=None,
    user_id=None,
    identity=None,
    capabilities=None,
    roles=None,
    groups=None,
    mechanism="CURVE",
    credentials=None,
    comments=None,
    enabled=True,
    **kwargs,
):
    """Prompts user for Auth Entry fields."""

    class Asker(object):
        def __init__(self):
            self._fields = collections.OrderedDict()

        def add(
            self,
            name,
            default=None,
            note=None,
            callback=lambda x: x,
            validate=lambda x, y: (True, ""),
        ):
            self._fields[name] = {
                "note": note,
                "default": default,
                "callback": callback,
                "validate": validate,
            }

        def ask(self):
            for name in self._fields:
                note = self._fields[name]["note"]
                default = self._fields[name]["default"]
                callback = self._fields[name]["callback"]
                validate = self._fields[name]["validate"]
                if isinstance(default, list):
                    default_str = "{}".format(",".join(default))
                elif default is None:
                    default_str = ""
                else:
                    default_str = default
                note = "({}) ".format(note) if note else ""
                question = "{} {}[{}]: ".format(name, note, default_str)
                valid = False
                while not valid:
                    response = input(question).strip()
                    if response == "":
                        response = default
                    if response == "clear":
                        if _ask_yes_no("Do you want to clear this field?"):
                            response = None
                    valid, msg = validate(response, self._fields)
                    if not valid:
                        _stderr.write("{}\n".format(msg))

                self._fields[name]["response"] = callback(response)
            return {k: self._fields[k]["response"] for k in self._fields}

    def to_true_or_false(response):
        if isinstance(response, str):
            return {"true": True, "false": False}[response.lower()]
        return response

    def is_true_or_false(x, fields):
        if x is not None:
            if isinstance(x, bool) or x.lower() in ["true", "false"]:
                return True, None
        return False, "Please enter True or False"

    def valid_creds(creds, fields):
        try:
            mechanism = fields["mechanism"]["response"]
            AuthEntry.valid_credentials(creds, mechanism=mechanism)
        except AuthException as e:
            return False, str(e)
        return True, None

    def valid_mech(mech, fields):
        try:
            AuthEntry.valid_mechanism(mech)
        except AuthException as e:
            return False, str(e)
        return True, None

    asker = Asker()
    asker.add("domain", domain)
    asker.add("address", address)
    asker.add("user_id", user_id)
    asker.add("identity", identity)
    asker.add(
        "capabilities",
        capabilities,
        "delimit multiple entries with comma",
        _parse_capabilities,
    )
    asker.add("roles", roles, "delimit multiple entries with comma",
              _comma_split)
    asker.add("groups", groups, "delimit multiple entries with comma",
              _comma_split)
    asker.add("mechanism", mechanism, validate=valid_mech)
    asker.add("credentials", credentials, validate=valid_creds)
    asker.add("comments", comments)
    asker.add("enabled", enabled, callback=to_true_or_false,
              validate=is_true_or_false)

    return asker.ask()


def _comma_split(line):
    if not isinstance(line, str):
        return line
    line = line.strip()
    if not line:
        return []
    return [word.strip() for word in line.split(",")]


def _parse_capabilities(line):
    if not isinstance(line, str):
        return line
    line = line.strip()
    try:
        result = jsonapi.loads(line.replace("'", '"'))
    except Exception as e:
        result = _comma_split(line)
    return result


def add_auth(opts):
    """Add authorization entry.

    If all options are None, then use interactive 'wizard.'
    """
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return

    fields = {
        "domain": opts.domain,
        "address": opts.address,
        "mechanism": opts.mechanism,
        "credentials": opts.credentials,
        "user_id": opts.user_id,
        "identity": opts.user_id,
        "groups": _comma_split(opts.groups),
        "roles": _comma_split(opts.roles),
        "capabilities": _parse_capabilities(opts.capabilities),
        "rpc_method_authorizations": None,
        "comments": opts.comments,
    }

    if any(fields.values()):
        # Remove unspecified options so the default parameters are used
        fields = {k: v for k, v in fields.items() if v}
        fields["enabled"] = not opts.disabled
        entry = fields
    else:
        # No options were specified, use interactive wizard
        responses = _ask_for_auth_fields()
        responses["rpc_method_authorizations"] = None
        entry = responses

    if opts.add_known_host:
        if entry["address"] is None:
            raise ValueError(
                "host (--address) is required when " "--add-known-host is "
                "specified"
            )
        if entry["credentials"] is None:
            raise ValueError(
                "serverkey (--credentials) is required when "
                "--add-known-host is specified"
            )
        opts.host = entry["address"]
        opts.serverkey = entry["credentials"]
        add_server_key(opts)

    try:
        conn.server.vip.rpc.call(AUTH, "auth_file.add", entry).get(timeout=4)
        _stdout.write("added entry {}\n".format(entry))
    except AuthException as err:
        _stderr.write("ERROR: %s\n" % str(err))


def _ask_yes_no(question, default="yes"):
    yes = set(["yes", "ye", "y"])
    no = set(["no", "n"])
    y = "y"
    n = "n"
    if default in yes:
        y = "Y"
    elif default in no:
        n = "N"
    else:
        raise ValueError("invalid default answer: '%s'" % default)
    while True:
        choice = input("{} [{}/{}] ".format(question, y, n)).lower()
        if choice == "":
            choice = default
        if choice in yes:
            return True
        if choice in no:
            return False
        _stderr.write("Please respond with 'yes' or 'no'\n")


def remove_auth(opts):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    entry_count = len(
        conn.server.vip.rpc.call(AUTH, "auth_file.read").get()["allow_list"]
    )

    for i in opts.indices:
        if i < 0 or i >= entry_count:
            _stderr.write("ERROR: invalid index {}\n".format(i))
            return

    _stdout.write("This action will delete the following:\n")
    list_auth(opts, opts.indices)
    if not _ask_yes_no("Do you wish to delete?"):
        return
    try:
        conn.server.vip.rpc.call(AUTH, "auth_file.remove_by_indices",
                                 opts.indices)
        if len(opts.indices) > 1:
            msg = "removed entries at indices {}".format(opts.indices)
        else:
            msg = msg = "removed entry at index {}".format(opts.indices)
        _stdout.write(msg + "\n")
    except AuthException as err:
        _stderr.write("ERROR: %s\n" % str(err))


def update_auth(opts):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return

    entries = conn.server.vip.rpc.call(AUTH, "auth_file.read").get()[
        "allow_list"]
    try:
        if opts.index < 0:
            raise IndexError
        entry = entries[opts.index]
        _stdout.write('(For any field type "clear" to clear the value.)\n')
        response = _ask_for_auth_fields(**entry)
        response["rpc_method_authorizations"] = None
        updated_entry = response
        conn.server.vip.rpc.call(
            AUTH, "auth_file.update_by_index", updated_entry, opts.index
        )
        _stdout.write("updated entry at index {}\n".format(opts.index))
    except IndexError:
        _stderr.write("ERROR: invalid index %s\n" % opts.index)
    except AuthException as err:
        _stderr.write("ERROR: %s\n" % str(err))


def add_role(opts):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return

    roles = conn.server.vip.rpc.call(AUTH, "auth_file.read").get()["roles"]
    if opts.role in roles:
        _stderr.write('role "{}" already exists\n'.format(opts.role))
        return
    roles[opts.role] = list(set(opts.capabilities))
    conn.server.vip.rpc.call(AUTH, "auth_file.set_roles", roles)
    _stdout.write('added role "{}"\n'.format(opts.role))


def list_roles(opts):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    roles = conn.server.vip.rpc.call(AUTH, "auth_file.read").get()["roles"]
    _print_two_columns(roles, "ROLE", "CAPABILITIES")


def update_role(opts):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    roles = conn.server.vip.rpc.call(AUTH, "auth_file.read").get()["roles"]
    if opts.role not in roles:
        _stderr.write('role "{}" does not exist\n'.format(opts.role))
        return
    caps = roles[opts.role]
    if opts.remove:
        roles[opts.role] = list(set(caps) - set(opts.capabilities))
    else:
        roles[opts.role] = list(set(caps) | set(opts.capabilities))
    conn.server.vip.rpc.call(AUTH, "auth_file.set_roles", roles)
    _stdout.write('updated role "{}"\n'.format(opts.role))


def remove_role(opts):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    roles = conn.server.vip.rpc.call(AUTH, "auth_file.read").get()["roles"]
    if opts.role not in roles:
        _stderr.write('role "{}" does not exist\n'.format(opts.role))
        return
    del roles[opts.role]
    conn.server.vip.rpc.call(AUTH, "auth_file.set_roles", roles)
    _stdout.write('removed role "{}"\n'.format(opts.role))


def add_group(opts):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    groups = conn.server.vip.rpc.call(AUTH, "auth_file.read").get()["groups"]
    if opts.group in groups:
        _stderr.write('group "{}" already exists\n'.format(opts.group))
        return
    groups[opts.group] = list(set(opts.roles))
    conn.server.vip.rpc.call(AUTH, "auth_file.set_groups", groups)
    _stdout.write('added group "{}"\n'.format(opts.group))


def list_groups(opts):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    groups = conn.server.vip.rpc.call(AUTH, "auth_file.read").get()["groups"]
    _print_two_columns(groups, "GROUPS", "ROLES")


def update_group(opts):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    groups = conn.server.vip.rpc.call(AUTH, "auth_file.read").get()["groups"]
    if opts.group not in groups:
        _stderr.write('group "{}" does not exist\n'.format(opts.group))
        return
    roles = groups[opts.group]
    if opts.remove:
        groups[opts.group] = list(set(roles) - set(opts.roles))
    else:
        groups[opts.group] = list(set(roles) | set(opts.roles))
    conn.server.vip.rpc.call(AUTH, "auth_file.set_groups", groups)
    _stdout.write('updated group "{}"\n'.format(opts.group))


def remove_group(opts):
    conn = opts.connection
    if not conn:
        _stderr.write(
            "VOLTTRON is not running. This command "
            "requires VOLTTRON platform to be running\n"
        )
        return
    groups = conn.server.vip.rpc.call(AUTH, "auth_file.read").get()["groups"]
    if opts.group not in groups:
        _stderr.write('group "{}" does not exist\n'.format(opts.group))
        return
    del groups[opts.group]
    conn.server.vip.rpc.call(AUTH, "auth_file.set_groups", groups)
    _stdout.write('removed group "{}"\n'.format(opts.group))


def get_filtered_agents(opts, agents=None):
    if opts.pattern:
        filtered = set()
        for pattern, match in filter_agents(agents, opts.pattern, opts):
            if not match:
                _stderr.write(
                    "{}: error: agent not found: {}\n".format(opts.command,
                                                              pattern)
                )
            filtered |= match
        agents = list(filtered)
    return agents


def _show_filtered_agents(opts, field_name, field_callback, agents=None):
    """Provides generic way to filter and display agent information.
    The agents will be filtered by the provided opts.pattern and the
    following fields will be displayed:
      * UUID (or part of the UUID)
      * agent name
      * VIP identiy
      * tag
      * field_name
    @param:Namespace:opts:
        Options from argparse
    @param:string:field_name:
        Name of field to display about agents
    @param:function:field_callback:
        Function that takes an Agent as an argument and returns data
        to display
    @param:list:agents:
        List of agents to filter and display
    """
    if not agents:
        agents = _list_agents(opts.aip)

    agents = get_filtered_agents(opts, agents)

    if not agents:
        if not opts.json:
            _stderr.write("No installed Agents found\n")
        else:
            _stdout.write(f"{jsonapi.dumps({}, indent=2)}\n")
        return
    agents = sorted(agents, key=lambda x: x.name)
    if not opts.min_uuid_len:
        n = 36
    else:
        n = max(_calc_min_uuid_length(agents), opts.min_uuid_len)
    name_width = max(5, max(len(agent.name) for agent in agents))
    tag_width = max(3, max(len(agent.tag or "") for agent in agents))
    identity_width = max(3, max(
        len(agent.vip_identity or "") for agent in agents))
    fmt = "{} {:{}} {:{}} {:{}} {:>6}\n"

    if not opts.json:
        _stderr.write(
            fmt.format(
                " " * n,
                "AGENT",
                name_width,
                "IDENTITY",
                identity_width,
                "TAG",
                tag_width,
                field_name,
            )
        )
        for agent in agents:
            _stdout.write(
                fmt.format(
                    agent.uuid[:n],
                    agent.name,
                    name_width,
                    agent.vip_identity,
                    identity_width,
                    agent.tag or "",
                    tag_width,
                    field_callback(agent),
                )
            )
    else:
        json_obj = {}
        for agent in agents:
            json_obj[agent.vip_identity] = {
                "agent_uuid": agent.uuid,
                "name": agent.name,
                "identity": agent.vip_identity,
                "agent_tag": agent.tag or "",
                field_name: field_callback(agent),
            }
        _stdout.write(f"{jsonapi.dumps(json_obj, indent=2)}\n")


def _show_filtered_agents_status(opts, status_callback, health_callback,
                                 agents=None):
    """Provides generic way to filter and display agent information.

    The agents will be filtered by the provided opts.pattern and the
    following fields will be displayed:
      * UUID (or part of the UUID)
      * agent name
      * VIP identiy
      * tag
      * field_name

    @param:Namespace:opts:
        Options from argparse
    @param:string:field_name:
        Name of field to display about agents
    @param:function:field_callback:
        Function that takes an Agent as an argument and returns data
        to display
    @param:list:agents:
        List of agents to filter and display
    """
    if not agents:
        agents = _list_agents(opts.aip)

    # Find max before so the uuid of the agent is available
    # when a usre has filtered the list.
    if not opts.min_uuid_len:
        n = 36
    else:
        n = max(_calc_min_uuid_length(agents), opts.min_uuid_len)

    agents = get_filtered_agents(opts, agents)

    if not agents:
        if not opts.json:
            _stderr.write("No installed Agents found\n")
        else:
            _stdout.write(f"{jsonapi.dumps({}, indent=2)}\n")
        return

    agents = sorted(agents, key=lambda x: x.name)
    if not opts.json:
        name_width = max(5, max(len(agent.name) for agent in agents))
        tag_width = max(3, max(len(agent.tag or "") for agent in agents))
        identity_width = max(3, max(
            len(agent.vip_identity or "") for agent in agents))
        if is_secure_mode():
            user_width = max(3, max(
                len(agent.agent_user or "") for agent in agents))
            fmt = "{} {:{}} {:{}} {:{}} {:{}} {:>6} {:>15}\n"
            _stderr.write(
                fmt.format(
                    "UUID",
                    "AGENT",
                    name_width,
                    "IDENTITY",
                    identity_width,
                    "TAG",
                    tag_width,
                    "AGENT_USER",
                    user_width,
                    "STATUS",
                    "HEALTH",
                )
            )
            fmt = "{} {:{}} {:{}} {:{}} {:{}} {:<15} {:<}\n"
            for agent in agents:
                status_str = status_callback(agent)
                agent_health_dict = health_callback(agent)
                _stdout.write(
                    fmt.format(
                        agent.uuid[:n],
                        agent.name,
                        name_width,
                        agent.vip_identity,
                        identity_width,
                        agent.tag or "",
                        tag_width,
                        agent.agent_user if status_str.startswith(
                            "running") else "",
                        user_width,
                        status_str,
                        health_callback(agent),
                    )
                )
        else:
            fmt = "{} {:{}} {:{}} {:{}} {:>6} {:>15}\n"
            _stderr.write(
                fmt.format(
                    "UUID",
                    "AGENT",
                    name_width,
                    "IDENTITY",
                    identity_width,
                    "TAG",
                    tag_width,
                    "STATUS",
                    "HEALTH",
                )
            )
            fmt = "{} {:{}} {:{}} {:{}} {:<15} {:<}\n"
            for agent in agents:
                _stdout.write(
                    fmt.format(
                        agent.uuid[:n],
                        agent.name,
                        name_width,
                        agent.vip_identity,
                        identity_width,
                        agent.tag or "",
                        tag_width,
                        status_callback(agent),
                        health_callback(agent),
                    )
                )
    else:
        json_obj = {}
        for agent in agents:
            json_obj[agent.vip_identity] = {
                "agent_uuid": agent.uuid,
                "name": agent.name,
                "identity": agent.vip_identity,
                "agent_tag": agent.tag or "",
                "status": status_callback(agent),
                "health": health_callback(agent),
            }
            if is_secure_mode():
                json_obj[agent.vip_identity]["agent_user"] = (
                    agent.agent_user
                    if json_obj[agent.vip_identity]["status"].startswith(
                        "running")
                    else ""
                )
        _stdout.write(f"{jsonapi.dumps(json_obj, indent=2)}\n")


def get_agent_publickey(opts):
    def get_key(agent):
        return opts.aip.get_agent_keystore(agent.uuid).public

    _show_filtered_agents(opts, "PUBLICKEY", get_key)


def add_config_to_store(opts):
    opts.connection.peer = CONFIGURATION_STORE
    call = opts.connection.call

    file_contents = opts.infile.read()

    call(
        "manage_store",
        opts.identity,
        opts.name,
        file_contents,
        config_type=opts.config_type,
    )


def delete_config_from_store(opts):
    opts.connection.peer = CONFIGURATION_STORE
    call = opts.connection.call
    if opts.delete_store:
        call("manage_delete_store", opts.identity)
        return

    if opts.name is None:
        _stderr.write(
            "ERROR: must specify a configuration when not deleting entire "
            "store\n"
        )
        return

    call("manage_delete_config", opts.identity, opts.name)


def list_store(opts):
    opts.connection.peer = CONFIGURATION_STORE
    call = opts.connection.call
    results = []
    if opts.identity is None:
        results = call("manage_list_stores")
    else:
        results = call("manage_list_configs", opts.identity)

    for item in results:
        _stdout.write(item + "\n")


def get_config(opts):
    opts.connection.peer = CONFIGURATION_STORE
    call = opts.connection.call
    results = call("manage_get", opts.identity, opts.name, raw=opts.raw)

    if opts.raw:
        _stdout.write(results)
    else:
        if isinstance(results, str):
            _stdout.write(results)
        else:
            _stdout.write(jsonapi.dumps(results, indent=2))
            _stdout.write("\n")


def edit_config(opts):
    opts.connection.peer = CONFIGURATION_STORE
    call = opts.connection.call

    if opts.new_config:
        config_type = opts.config_type
        raw_data = ""
    else:
        try:
            results = call("manage_get_metadata", opts.identity, opts.name)
            config_type = results["type"]
            raw_data = results["data"]
        except RemoteError as e:
            if "No configuration file" not in e.message:
                raise
            config_type = opts.config_type
            raw_data = ""

    # Write raw data to temp file
    # This will not work on Windows, FYI
    with tempfile.NamedTemporaryFile(suffix=".txt", mode="r+") as f:
        f.write(raw_data)
        f.flush()

        success = True
        try:
            # do not use utils.execute_command as we don't want set stdout to
            #  subprocess.PIPE
            subprocess.check_call([opts.editor, f.name])
        except subprocess.CalledProcessError as e:
            _stderr.write(
                "Editor returned with code {}. Changes not "
                "committed.\n".format(
                    e.returncode
                )
            )
            success = False

        if not success:
            return

        f.seek(0)
        new_raw_data = f.read()

        if new_raw_data == raw_data:
            _stderr.write("No changes detected.\n")
            return

        call(
            "manage_store",
            opts.identity,
            opts.name,
            new_raw_data,
            config_type=config_type,
        )


class ControlConnection(object):
    def __init__(self, address, peer="control"):
        self.address = address
        self.peer = peer
        message_bus = utils.get_messagebus()
        self._server = BaseAgent(
            address=self.address,
            enable_store=False,
            identity=CONTROL_CONNECTION,
            message_bus=message_bus,
            enable_channel=True,
        )
        self._greenlet = None

    @property
    def server(self):
        if self._greenlet is None:
            event = gevent.event.Event()
            self._greenlet = gevent.spawn(self._server.core.run, event)
            event.wait()
        return self._server

    def call(self, method, *args, **kwargs):
        return self.server.vip.rpc.call(self.peer, method, *args,
                                        **kwargs).get()

    def call_no_get(self, method, *args, **kwargs):
        return self.server.vip.rpc.call(self.peer, method, *args, **kwargs)

    def notify(self, method, *args, **kwargs):
        return self.server.vip.rpc.notify(self.peer, method, *args, **kwargs)

    def kill(self, *args, **kwargs):
        """
        Resets a running greenlet and cleans up the internal stopped agent.
        """
        if self._greenlet is not None:
            try:
                self._server.core.stop()
            finally:
                self._server = None
            try:
                self._greenlet.kill(*args, **kwargs)
            finally:
                self._greenlet = None


def priority(value):
    n = int(value)
    if not 0 <= n < 100:
        raise ValueError("invalid priority (0 <= n < 100): {}".format(n))
    return "{:02}".format(n)


def get_keys(opts):
    """Gets keys from keystore and known-hosts store"""
    hosts = KnownHostsStore()
    serverkey = hosts.serverkey(opts.vip_address)
    key_store = KeyStore()
    publickey = key_store.public
    secretkey = key_store.secret
    return {"publickey": publickey, "secretkey": secretkey,
            "serverkey": serverkey}


# RabbitMQ management methods
def add_vhost(opts):
    try:
        rmq_mgmt.create_vhost(opts.vhost)
    except requests.exceptions.HTTPError as e:
        _stdout.write("Error adding a Virtual Host: {} \n".format(opts.vhost))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def add_user(opts):
    rmq_mgmt.create_user(opts.user, opts.pwd)
    permissions = dict(configure="", read="", write="")
    read = _ask_yes_no("Do you want to set READ permission ")
    write = _ask_yes_no("Do you want to set WRITE permission ")
    configure = _ask_yes_no("Do you want to set CONFIGURE permission ")

    if read:
        permissions["read"] = ".*"
    if write:
        permissions["write"] = ".*"
    if configure:
        permissions["configure"] = ".*"
    try:
        rmq_mgmt.set_user_permissions(permissions, opts.user)
    except requests.exceptions.HTTPError as e:
        _stdout.write(
            "Error Setting User permissions : {} \n".format(opts.user))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def add_exchange(opts):
    if opts.type not in ["topic", "fanout", "direct"]:
        print(
            "Unknown exchange type. Valid exchange types are topic or fanout "
            "or direct"
        )
        return
    durable = _ask_yes_no("Do you want exchange to be durable ")
    auto_delete = _ask_yes_no("Do you want exchange to be auto deleted ")
    alternate = _ask_yes_no("Do you want alternate exchange ")

    properties = dict(durable=durable, type=opts.type, auto_delete=auto_delete)
    try:
        if alternate:
            alternate_exch = opts.name + "alternate"
            properties["alternate-exchange"] = alternate_exch
            # create alternate exchange
            new_props = dict(durable=durable, type="fanout",
                             auto_delete=auto_delete)
            rmq_mgmt.create_exchange(alternate_exch, new_props)
        rmq_mgmt.create_exchange(opts.name, properties)
    except requests.exceptions.HTTPError as e:
        _stdout.write("Error Adding Exchange : {} \n".format(opts.name))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def add_queue(opts):
    durable = _ask_yes_no("Do you want queue to be durable ")
    auto_delete = _ask_yes_no("Do you want queue to be auto deleted ")

    properties = dict(durable=durable, auto_delete=auto_delete)
    try:
        rmq_mgmt.create_queue(opts.name, properties)
    except requests.exceptions.HTTPError as e:
        _stdout.write("Error Adding Queue : {} \n".format(opts.name))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def list_vhosts(opts):
    try:
        vhosts = rmq_mgmt.get_virtualhosts()
        for item in vhosts:
            _stdout.write(item + "\n")
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Virtual Hosts Found: {} \n")
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def list_users(opts):
    try:
        users = rmq_mgmt.get_users()
        for item in users:
            _stdout.write(item + "\n")
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Users Found: {} \n")
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def list_user_properties(opts):
    try:
        props = rmq_mgmt.get_user_props(opts.user)
        for key, value in props.items():
            _stdout.write("{0}: {1} \n".format(key, value))
    except requests.exceptions.HTTPError as e:
        _stdout.write("No User Found: {} \n".format(opts.user))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def list_exchanges(opts):
    try:
        exchanges = rmq_mgmt.get_exchanges()
        for exch in exchanges:
            _stdout.write(exch + "\n")
    except requests.exceptions.HTTPError as e:
        _stdout.write("No exchanges found \n")
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def list_exchanges_with_properties(opts):
    exchanges = None
    try:
        exchanges = rmq_mgmt.get_exchanges_with_props()
    except requests.exceptions.HTTPError as e:
        _stdout.write("No exchanges found \n")
        return
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )
        return
    try:
        name_width = max(8, max(len(e["name"]) for e in exchanges))
        dur_width = len("DURABLE")
        auto_width = len("AUTO-DELETE")
        type_width = max(6, max(len(e["type"]) for e in exchanges))
        # args_width = max(6, max(len(e['type']) for e in exchanges))
        fmt = "{:{}} {:{}} {:{}} {:{}}\n"
        _stderr.write(
            fmt.format(
                "EXCHANGE",
                name_width,
                "TYPE",
                type_width,
                "DURABLE",
                dur_width,
                "AUTO-DELETE",
                auto_width,
            )
        )
        for exch in exchanges:
            _stdout.write(
                fmt.format(
                    exch["name"],
                    name_width,
                    exch["type"],
                    type_width,
                    str(exch["durable"]),
                    dur_width,
                    str(exch["auto_delete"]),
                    auto_width,
                )
            )
            # exch['messages'], args_width))
    except (AttributeError, KeyError) as ex:
        _stdout.write("Error in getting queue properties")


def list_queues(opts):
    queues = None
    try:
        queues = rmq_mgmt.get_queues()
    except requests.exceptions.HTTPError as e:
        _stdout.write("No queues found \n")
        return
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )
        return
    if queues:
        for q in queues:
            _stdout.write(q + "\n")


def list_queues_with_properties(opts):
    queues = None
    try:
        queues = rmq_mgmt.get_queues_with_props()
    except requests.exceptions.HTTPError as e:
        _stdout.write("No queues found \n")
        return
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )
        return
    try:
        name_width = max(5, max(len(q["name"]) for q in queues))
        dur_width = len("DURABLE")
        excl_width = len("EXCLUSIVE")
        auto_width = len("auto-delete")
        state_width = len("running")
        unack_width = len("MESSAGES")
        fmt = "{:{}} {:{}} {:{}} {:{}} {:{}} {:{}}\n"
        _stderr.write(
            fmt.format(
                "QUEUE",
                name_width,
                "STATE",
                state_width,
                "DURABLE",
                dur_width,
                "EXCLUSIVE",
                excl_width,
                "AUTO-DELETE",
                auto_width,
                "MESSAGES",
                unack_width,
            )
        )
        for q in queues:
            _stdout.write(
                fmt.format(
                    q["name"],
                    name_width,
                    str(q["state"]),
                    state_width,
                    str(q["durable"]),
                    dur_width,
                    str(q["exclusive"]),
                    excl_width,
                    str(q["auto_delete"]),
                    auto_width,
                    q["messages"],
                    unack_width,
                )
            )
    except (AttributeError, KeyError) as ex:
        _stdout.write("Error in getting queue properties")


def list_connections(opts):
    try:
        conn = rmq_mgmt.get_connection()
    except requests.exceptions.HTTPError as e:
        _stdout.write("No connections found \n")
        return
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )
        return


def list_fed_parameters(opts):
    parameters = None
    try:
        parameters = rmq_mgmt.get_parameter("federation-upstream")
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Federation Parameters Found \n")
        return
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )
        return
    try:
        if parameters:
            name_width = max(5, max(len(p["name"]) for p in parameters))
            uri_width = max(3, max(len(p["value"]["uri"]) for p in parameters))
            fmt = "{:{}} {:{}}\n"
            _stderr.write(fmt.format("NAME", name_width, "URI", uri_width))
            for param in parameters:
                _stdout.write(
                    fmt.format(
                        param["name"], name_width, param["value"]["uri"],
                        uri_width
                    )
                )
    except (AttributeError, KeyError) as ex:
        _stdout.write("Error in federation parameters")


def list_shovel_parameters(opts):
    parameters = None
    try:
        parameters = rmq_mgmt.get_parameter("shovel")
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Shovel Parameters Found \n")
        return
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )
        return
    try:
        if parameters:
            name_width = max(5, max(len(p["name"]) for p in parameters))
            src_uri_width = max(
                len("SOURCE ADDRESS"),
                max(len(p["value"]["src-uri"]) for p in parameters),
            )
            dest_uri_width = max(
                len("DESTINATION ADDRESS"),
                max(len(p["value"]["dest-uri"]) for p in parameters),
            )
            binding_key = max(
                len("BINDING KEY"),
                max(len(p["value"]["src-exchange-key"]) for p in parameters),
            )
            fmt = "{:{}}  {:{}}  {:{}}  {:{}}\n"
            _stderr.write(
                fmt.format(
                    "NAME",
                    name_width,
                    "SOURCE ADDRESS",
                    src_uri_width,
                    "DESTINATION ADDRESS",
                    dest_uri_width,
                    "BINDING KEY",
                    binding_key,
                )
            )
            for param in parameters:
                _stdout.write(
                    fmt.format(
                        param["name"],
                        name_width,
                        param["value"]["src-uri"],
                        src_uri_width,
                        param["value"]["dest-uri"],
                        dest_uri_width,
                        param["value"]["src-exchange-key"],
                        binding_key,
                    )
                )
    except (AttributeError, KeyError) as ex:
        _stdout.write("Error in getting shovel parameters")


def list_fed_links(opts):
    links = None
    try:
        links = rmq_mgmt.get_federation_links()
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Federation links Found \n")
        return
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )
        return
    try:
        if links:
            name_width = max(5, max(len(lk["name"]) for lk in links))
            status_width = max(3, max(len(lk["status"]) for lk in links))
            fmt = "{:{}} {:{}}\n"
            _stderr.write(
                fmt.format("NAME", name_width, "STATUS", status_width))
            for link in links:
                _stdout.write(
                    fmt.format(link["name"], name_width, link["status"],
                               status_width)
                )
    except (AttributeError, KeyError) as ex:
        _stdout.write("Error in federation links")


def list_shovel_links(opts):
    links = None
    try:
        links = rmq_mgmt.get_shovel_links()
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Shovel links Found \n")
        return
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )
        return
    try:
        if links:
            name_width = max(5, max(len(lk["name"]) for lk in links))
            status_width = max(3, max(len(lk["status"]) for lk in links))
            src_exchange_key_width = max(
                3, max(len(lk["src_exchange_key"]) for lk in links)
            )
            src_uri_width = max(3, max(len(lk["src_uri"]) for lk in links))
            dest_uri_width = max(3, max(len(lk["dest_uri"]) for lk in links))
            fmt = "{:{}}  {:{}}  {:{}}  {:{}}  {:{}}\n"
            _stderr.write(
                fmt.format(
                    "NAME",
                    name_width,
                    "STATUS",
                    status_width,
                    "SRC_URI",
                    src_uri_width,
                    "DEST_URI",
                    dest_uri_width,
                    "SRC_EXCHANGE_KEY",
                    src_exchange_key_width,
                )
            )
            for link in links:
                _stdout.write(
                    fmt.format(
                        link["name"],
                        name_width,
                        link["status"],
                        status_width,
                        link["src_uri"],
                        src_uri_width,
                        link["dest_uri"],
                        dest_uri_width,
                        link["src_exchange_key"],
                        src_exchange_key_width,
                    )
                )
    except (AttributeError, KeyError) as ex:
        _stdout.write(f"Error in shovel links as {ex}")


def list_bindings(opts):
    bindings = None
    try:
        bindings = rmq_mgmt.get_bindings(opts.exchange)
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Bindings Found \n")
        return
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )
        return

    try:
        if bindings:
            src_width = max(5, max(len(b["source"]) for b in bindings))
            exch_width = len("EXCHANGE")
            dest_width = max(len("QUEUE"),
                             max(len(b["destination"]) for b in bindings))
            bindkey = len("BINDING KEY")
            rkey = max(10, max(len(b["routing_key"]) for b in bindings))
            fmt = "{:{}}  {:{}}  {:{}}\n"
            _stderr.write(
                fmt.format(
                    "EXCHANGE", exch_width, "QUEUE", dest_width, "BINDING KEY",
                    bindkey
                )
            )
            for b in bindings:
                _stdout.write(
                    fmt.format(
                        b["source"],
                        src_width,
                        b["destination"],
                        dest_width,
                        b["routing_key"],
                        rkey,
                    )
                )
    except (AttributeError, KeyError) as ex:
        _stdout.write("Error in getting bindings")


def list_policies(opts):
    policies = None
    try:
        policies = rmq_mgmt.get_policies()
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Policies Found \n")
        return
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )
        return
    try:
        if policies:
            name_width = max(5, max(len(p["name"]) for p in policies))
            apply_width = max(8, max(len(p["apply-to"]) for p in policies))
            fmt = "{:{}} {:{}}\n"
            _stderr.write(
                fmt.format("NAME", name_width, "APPLY-TO", apply_width))
            for policy in policies:
                _stdout.write(
                    fmt.format(
                        policy["name"], name_width, policy["apply-to"],
                        apply_width
                    )
                )
    except (AttributeError, KeyError) as ex:
        _stdout.write("Error in getting policies")


def remove_vhosts(opts):
    try:
        for vhost in opts.vhost:
            rmq_mgmt.delete_vhost(vhost)
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Vhost Found {} \n".format(opts.vhost))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def remove_users(opts):
    try:
        for user in opts.user:
            rmq_mgmt.delete_user(user)
    except requests.exceptions.HTTPError as e:
        _stdout.write("No User Found {} \n".format(opts.user))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def remove_exchanges(opts):
    try:
        for e in opts.exchanges:
            rmq_mgmt.delete_exchange(e)
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Exchange Found {} \n".format(opts.exchanges))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def remove_queues(opts):
    try:
        for q in opts.queues:
            rmq_mgmt.delete_queue(q)
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Queues Found {} \n".format(opts.queues))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def remove_fed_parameters(opts):
    try:
        for param in opts.parameters:
            delete_certs = _ask_yes_no(
                f"Do you wish to delete certificates as well for {param}?"
            )
            rmq_mgmt.delete_multiplatform_parameter(
                "federation-upstream", param, delete_certs=delete_certs
            )
    except requests.exceptions.HTTPError as e:
        _stdout.write(
            "No Federation Parameters Found {} \n".format(opts.parameters))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def remove_shovel_parameters(opts):
    try:
        for param in opts.parameters:
            delete_certs = _ask_yes_no(
                "Do you wish to delete certificates as well?")
            rmq_mgmt.delete_multiplatform_parameter(
                "shovel", param, delete_certs=delete_certs
            )
    except requests.exceptions.HTTPError as e:
        _stdout.write(
            "No Shovel Parameters Found {} \n".format(opts.parameters))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def remove_policies(opts):
    try:
        for policy in opts.policies:
            rmq_mgmt.delete_policy(policy)
    except requests.exceptions.HTTPError as e:
        _stdout.write("No Policies Found {} \n".format(opts.policies))
    except ConnectionError as e:
        _stdout.write(
            "Error making request to RabbitMQ Management interface.\n"
            "Check Connection Parameters: {} \n".format(e)
        )


def create_ssl_keypair(opts):
    fq_identity = utils.get_fq_identity(opts.identity)
    certs = Certs()
    certs.create_signed_cert_files(fq_identity)


def export_pkcs12_from_identity(opts):
    fq_identity = utils.get_fq_identity(opts.identity)

    certs = Certs()
    certs.export_pkcs12(fq_identity, opts.outfile)


def main():
    # Refuse to run as root
    if not getattr(os, "getuid", lambda: -1)():
        sys.stderr.write(
            "%s: error: refusing to run as root to prevent "
            "potential damage.\n" % os.path.basename(sys.argv[0])
        )
        sys.exit(77)

    volttron_home = get_home()

    os.environ["VOLTTRON_HOME"] = volttron_home

    global_args = config.ArgumentParser(description="global options",
                                        add_help=False)
    global_args.add_argument(
        "-c",
        "--config",
        metavar="FILE",
        action="parse_config",
        ignore_unknown=True,
        sections=[None, "global", "volttron-ctl"],
        help="read configuration from FILE",
    )
    global_args.add_argument(
        "--debug",
        action="store_true",
        help="show tracebacks for errors rather than a brief message",
    )
    global_args.add_argument(
        "-t",
        "--timeout",
        type=float,
        metavar="SECS",
        help="timeout in seconds for remote calls (default: %(default)g)",
    )
    global_args.add_argument(
        "--msgdebug", help="route all messages to an agent while debugging"
    )
    global_args.add_argument(
        "--vip-address",
        metavar="ZMQADDR",
        help="ZeroMQ URL to bind for VIP connections",
    )

    global_args.set_defaults(
        vip_address=get_address(),
        timeout=60,
    )

    filterable = config.ArgumentParser(add_help=False)
    filterable.add_argument(
        "--name",
        dest="by_name",
        action="store_true",
        help="filter/search by agent name",
    )
    filterable.add_argument(
        "--tag", dest="by_tag", action="store_true",
        help="filter/search by tag name"
    )
    filterable.add_argument(
        "--uuid",
        dest="by_uuid",
        action="store_true",
        help="filter/search by UUID (default)",
    )
    filterable.set_defaults(by_name=False, by_tag=False, by_uuid=False)

    parser = config.ArgumentParser(
        prog=os.path.basename(sys.argv[0]),
        add_help=False,
        description="Manage and control VOLTTRON agents.",
        usage="%(prog)s command [OPTIONS] ...",
        argument_default=argparse.SUPPRESS,
        parents=[global_args],
    )
    parser.add_argument(
        "-l",
        "--log",
        metavar="FILE",
        default=None,
        help="send log output to FILE instead of stderr",
    )
    parser.add_argument(
        "-L",
        "--log-config",
        metavar="FILE",
        help="read logging configuration from FILE",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="add_const",
        const=10,
        dest="verboseness",
        help="decrease logger verboseness; may be used multiple times",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="add_const",
        const=-10,
        dest="verboseness",
        help="increase logger verboseness; may be used multiple times",
    )
    parser.add_argument(
        "--verboseness",
        type=int,
        metavar="LEVEL",
        default=logging.WARNING,
        help="set logger verboseness",
    )
    parser.add_argument("--show-config", action="store_true",
                        help=argparse.SUPPRESS)
    parser.add_argument(
        "--json", action="store_true", default=False,
        help="format output to json"
    )

    parser.add_help_argument()
    parser.set_defaults(
        log_config=None,
        volttron_home=volttron_home,
    )

    top_level_subparsers = parser.add_subparsers(
        title="commands", metavar="", dest="command"
    )

    def add_parser(*args, **kwargs) -> argparse.ArgumentParser:
        parents = kwargs.get("parents", [])
        parents.append(global_args)
        kwargs["parents"] = parents
        subparser = kwargs.pop("subparser", top_level_subparsers)
        return subparser.add_parser(*args, **kwargs)

    add_install_agent_parser(add_parser, HAVE_RESTRICTED)

    tag = add_parser("tag", parents=[filterable],
                     help="set, show, or remove agent tag")
    tag.add_argument("agent", help="UUID or name of agent")
    group = tag.add_mutually_exclusive_group()
    group.add_argument("tag", nargs="?", const=None, help="tag to give agent")
    group.add_argument("-r", "--remove", action="store_true",
                       help="remove tag")
    tag.set_defaults(func=tag_agent, tag=None, remove=False)

    remove = add_parser("remove", parents=[filterable], help="remove agent")
    remove.add_argument("pattern", nargs="+", help="UUID or name of agent")
    remove.add_argument(
        "-f", "--force", action="store_true",
        help="force removal of multiple agents"
    )
    remove.set_defaults(func=remove_agent, force=False)

    peers = add_parser("peerlist",
                       help="list the peers connected to the platform")
    peers.set_defaults(func=list_peers)

    list_ = add_parser("list", parents=[filterable],
                       help="list installed agent")
    list_.add_argument("pattern", nargs="*", help="UUID or name of agent")
    list_.add_argument(
        "-n",
        dest="min_uuid_len",
        type=int,
        metavar="N",
        help="show at least N characters of UUID (0 to show all)",
    )
    list_.set_defaults(func=list_agents, min_uuid_len=1)

    status = add_parser("status", parents=[filterable],
                        help="show status of agents")
    status.add_argument("pattern", nargs="*", help="UUID or name of agent")
    status.add_argument(
        "-n",
        dest="min_uuid_len",
        type=int,
        metavar="N",
        help="show at least N characters of UUID (0 to show all)",
    )
    status.set_defaults(func=status_agents, min_uuid_len=1)

    health = add_parser(
        "health", parents=[filterable], help="show agent health as JSON"
    )
    health.add_argument("pattern", nargs=1, help="UUID or name of agent")
    health.set_defaults(func=agent_health, min_uuid_len=1)

    clear = add_parser("clear", help="clear status of defunct agents")
    clear.add_argument(
        "-a",
        "--all",
        dest="clear_all",
        action="store_true",
        help="clear the status of all agents",
    )
    clear.set_defaults(func=clear_status, clear_all=False)

    enable = add_parser(
        "enable", parents=[filterable],
        help="enable agent to start automatically"
    )
    enable.add_argument("pattern", nargs="+", help="UUID or name of agent")
    enable.add_argument(
        "-p", "--priority", type=priority,
        help="2-digit priority from 00 to 99"
    )
    enable.set_defaults(func=enable_agent, priority="50")

    disable = add_parser(
        "disable", parents=[filterable],
        help="prevent agent from start automatically"
    )
    disable.add_argument("pattern", nargs="+", help="UUID or name of agent")
    disable.set_defaults(func=disable_agent)

    start = add_parser("start", parents=[filterable],
                       help="start installed agent")
    start.add_argument("pattern", nargs="+", help="UUID or name of agent")
    if HAVE_RESTRICTED:
        start.add_argument(
            "--verify",
            action="store_true",
            dest="verify_agents",
            help="verify agent integrity during start",
        )
        start.add_argument(
            "--no-verify",
            action="store_false",
            dest="verify_agents",
            help=argparse.SUPPRESS,
        )
    start.set_defaults(func=start_agent)

    stop = add_parser("stop", parents=[filterable], help="stop agent")
    stop.add_argument("pattern", nargs="+", help="UUID or name of agent")
    stop.set_defaults(func=stop_agent)

    restart = add_parser("restart", parents=[filterable], help="restart agent")
    restart.add_argument("pattern", nargs="+", help="UUID or name of agent")
    restart.set_defaults(func=restart_agent)

    run = add_parser("run", help="start any agent by path")
    run.add_argument("directory", nargs="+", help="path to agent directory")
    if HAVE_RESTRICTED:
        run.add_argument(
            "--verify",
            action="store_true",
            dest="verify_agents",
            help="verify agent integrity during run",
        )
        run.add_argument(
            "--no-verify",
            action="store_false",
            dest="verify_agents",
            help=argparse.SUPPRESS,
        )
    run.set_defaults(func=run_agent)

    upgrade = add_parser(
        "upgrade",
        help="upgrade agent from wheel",
        epilog="Optionally you may specify the --tag argument to tag the "
               "agent during upgrade without requiring a separate call to "
               "the tag command. ",
    )
    upgrade.add_argument(
        "vip_identity", metavar="vip-identity",
        help="VIP IDENTITY of agent to upgrade"
    )
    upgrade.add_argument("wheel", help="path to new agent wheel")
    upgrade.add_argument("--tag", help="tag for the upgraded agent")
    if HAVE_RESTRICTED:
        upgrade.add_argument(
            "--verify",
            action="store_true",
            dest="verify_agents",
            help="verify agent integrity during upgrade",
        )
        upgrade.add_argument(
            "--no-verify",
            action="store_false",
            dest="verify_agents",
            help=argparse.SUPPRESS,
        )
    upgrade.set_defaults(func=upgrade_agent, verify_agents=True)

    # ====================================================
    # rpc commands
    # ====================================================
    rpc_ctl = add_parser("rpc", help="rpc controls")

    rpc_subparsers = rpc_ctl.add_subparsers(
        title="subcommands", metavar="", dest="store_commands"
    )

    rpc_code = add_parser(
        "code",
        subparser=rpc_subparsers,
        help="shows how to use rpc call in other agents",
    )

    rpc_code.add_argument(
        "pattern", nargs="*",
        help="Identity of agent, followed by method(s)" ""
    )
    rpc_code.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="list all subsystem rpc methods in addition to the agent's rpc "
             "methods",
    )

    rpc_code.set_defaults(func=list_agent_rpc_code, min_uuid_len=1)

    rpc_list = add_parser(
        "list", subparser=rpc_subparsers,
        help="lists all agents and their rpc methods"
    )

    rpc_list.add_argument(
        "-i", "--vip", dest="by_vip", action="store_true",
        help="filter by vip identity"
    )

    rpc_list.add_argument("pattern", nargs="*", help="UUID or name of agent")

    rpc_list.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="list all subsystem rpc methods in addition to the agent's rpc "
             "methods. If a method "
             "is specified, display the doc-string associated with the "
             "method.",
    )

    rpc_list.set_defaults(func=list_agents_rpc, min_uuid_len=1)

    # ====================================================
    # certs commands
    # ====================================================
    cert_cmds = add_parser("certs", help="manage certificate creation")

    certs_subparsers = cert_cmds.add_subparsers(
        title="subcommands", metavar="", dest="store_commands"
    )

    create_ssl_keypair_cmd = add_parser(
        "create-ssl-keypair", subparser=certs_subparsers,
        help="create a ssl keypair."
    )

    create_ssl_keypair_cmd.add_argument(
        "identity",
        help="Create a private key and cert for the given identity signed by "
             "the root ca of this platform.",
    )
    create_ssl_keypair_cmd.set_defaults(func=create_ssl_keypair)

    export_pkcs12 = add_parser(
        "export-pkcs12",
        subparser=certs_subparsers,
        help="create a PKCS12 encoded file containing private and public key "
             "from an agent. "
             "this function is useful to create a java key store using a p12 "
             "file.",
    )
    export_pkcs12.add_argument("identity",
                               help="identity of the agent to export")
    export_pkcs12.add_argument("outfile",
                               help="file to write the PKCS12 file to")
    export_pkcs12.set_defaults(func=export_pkcs12_from_identity)

    # ====================================================
    # auth commands
    # ====================================================
    auth_cmds = add_parser(
        "auth", help="manage authorization entries and encryption keys"
    )

    auth_subparsers = auth_cmds.add_subparsers(
        title="subcommands", metavar="", dest="store_commands"
    )

    auth_add = add_parser(
        "add", help="add new authentication record", subparser=auth_subparsers
    )
    auth_add.add_argument("--domain", default=None)
    auth_add.add_argument("--address", default=None)
    auth_add.add_argument("--mechanism", default=None)
    auth_add.add_argument("--credentials", default=None)
    auth_add.add_argument("--user_id", default=None)
    auth_add.add_argument("--identity", default=None)
    auth_add.add_argument(
        "--groups", default=None, help="delimit multiple entries with comma"
    )
    auth_add.add_argument(
        "--roles", default=None, help="delimit multiple entries with comma"
    )
    auth_add.add_argument(
        "--capabilities", default=None,
        help="delimit multiple entries with comma"
    )
    auth_add.add_argument("--comments", default=None)
    auth_add.add_argument("--disabled", action="store_true")
    auth_add.add_argument(
        "--add-known-host", action="store_true",
        help="adds entry in known host"
    )
    auth_add.set_defaults(func=add_auth)

    auth_add_group = add_parser(
        "add-group",
        subparser=auth_subparsers,
        help="associate a group name with a set of roles",
    )
    auth_add_group.add_argument("group", metavar="GROUP", help="name of group")
    auth_add_group.add_argument(
        "roles", metavar="ROLE", nargs="*",
        help="roles to associate with the group"
    )
    auth_add_group.set_defaults(func=add_group)

    auth_add_known_host = add_parser(
        "add-known-host",
        subparser=auth_subparsers,
        help="add server public key to known-hosts file",
    )
    auth_add_known_host.add_argument(
        "--host", required=True,
        help="hostname or IP address with optional port"
    )
    auth_add_known_host.add_argument("--serverkey", required=True)
    auth_add_known_host.set_defaults(func=add_server_key)

    auth_add_role = add_parser(
        "add-role",
        subparser=auth_subparsers,
        help="associate a role name with a set of capabilities",
    )
    auth_add_role.add_argument("role", metavar="ROLE", help="name of role")
    auth_add_role.add_argument(
        "capabilities",
        metavar="CAPABILITY",
        nargs="*",
        help="capabilities to associate with the role",
    )
    auth_add_role.set_defaults(func=add_role)

    auth_keypair = add_parser(
        "keypair",
        subparser=auth_subparsers,
        help="generate CurveMQ keys for encrypting VIP connections",
    )
    auth_keypair.set_defaults(func=gen_keypair)

    auth_list = add_parser(
        "list", help="list authentication records", subparser=auth_subparsers
    )
    auth_list.set_defaults(func=list_auth)

    auth_list_groups = add_parser(
        "list-groups",
        subparser=auth_subparsers,
        help="show list of group names and their sets of roles",
    )
    auth_list_groups.set_defaults(func=list_groups)

    auth_list_known_host = add_parser(
        "list-known-hosts",
        subparser=auth_subparsers,
        help="list entries from known-hosts file",
    )
    auth_list_known_host.set_defaults(func=list_known_hosts)

    auth_list_roles = add_parser(
        "list-roles",
        subparser=auth_subparsers,
        help="show list of role names and their sets of capabilities",
    )
    auth_list_roles.set_defaults(func=list_roles)

    auth_publickey = add_parser(
        "publickey",
        parents=[filterable],
        subparser=auth_subparsers,
        help="show public key for each agent",
    )
    auth_publickey.add_argument("pattern", nargs="*",
                                help="UUID or name of agent")
    auth_publickey.add_argument(
        "-n",
        dest="min_uuid_len",
        type=int,
        metavar="N",
        help="show at least N characters of UUID (0 to show all)",
    )
    auth_publickey.set_defaults(func=get_agent_publickey, min_uuid_len=1)

    auth_remove = add_parser(
        "remove",
        subparser=auth_subparsers,
        help="removes one or more authentication records by indices",
    )
    auth_remove.add_argument(
        "indices", nargs="+", type=int,
        help="index or indices of record(s) to remove"
    )
    auth_remove.set_defaults(func=remove_auth)

    auth_remove_group = add_parser(
        "remove-group",
        subparser=auth_subparsers,
        help="disassociate a group name from a set of roles",
    )
    auth_remove_group.add_argument("group", help="name of group")
    auth_remove_group.set_defaults(func=remove_group)

    auth_remove_known_host = add_parser(
        "remove-known-host",
        subparser=auth_subparsers,
        help="remove entry from known-hosts file",
    )
    auth_remove_known_host.add_argument(
        "host", metavar="HOST",
        help="hostname or IP address with optional port"
    )
    auth_remove_known_host.set_defaults(func=remove_known_host)

    auth_remove_role = add_parser(
        "remove-role",
        subparser=auth_subparsers,
        help="disassociate a role name from a set of capabilities",
    )
    auth_remove_role.add_argument("role", help="name of role")
    auth_remove_role.set_defaults(func=remove_role)

    auth_serverkey = add_parser(
        "serverkey",
        subparser=auth_subparsers,
        help="show the serverkey for the instance",
    )
    auth_serverkey.set_defaults(func=show_serverkey)

    auth_update = add_parser(
        "update",
        subparser=auth_subparsers,
        help="updates one authentication record by index",
    )
    auth_update.add_argument("index", type=int,
                             help="index of record to update")
    auth_update.set_defaults(func=update_auth)

    auth_update_group = add_parser(
        "update-group",
        subparser=auth_subparsers,
        help="update group to include (or remove) given roles",
    )
    auth_update_group.add_argument("group", metavar="GROUP",
                                   help="name of group")
    auth_update_group.add_argument(
        "roles",
        nargs="*",
        metavar="ROLE",
        help="roles to append to (or remove from) the group",
    )
    auth_update_group.add_argument(
        "--remove", action="store_true",
        help="remove (rather than append) given roles"
    )
    auth_update_group.set_defaults(func=update_group)

    auth_update_role = add_parser(
        "update-role",
        subparser=auth_subparsers,
        help="update role to include (or remove) given capabilities",
    )
    auth_update_role.add_argument("role", metavar="ROLE", help="name of role")
    auth_update_role.add_argument(
        "capabilities",
        nargs="*",
        metavar="CAPABILITY",
        help="capabilities to append to (or remove from) the role",
    )
    auth_update_role.add_argument(
        "--remove",
        action="store_true",
        help="remove (rather than append) given capabilities",
    )
    auth_update_role.set_defaults(func=update_role)

    auth_remote = add_parser(
        "remote",
        subparser=auth_subparsers,
        help="manage pending RMQ certs and ZMQ credentials",
    )
    auth_remote_subparsers = auth_remote.add_subparsers(
        title="remote subcommands", metavar="", dest="store_commands"
    )

    auth_remote_list_cmd = add_parser(
        "list",
        subparser=auth_remote_subparsers,
        help="lists approved, denied, and pending certs and credentials",
    )
    auth_remote_list_cmd.add_argument(
        "--status", help="Specify approved, denied, or pending"
    )
    auth_remote_list_cmd.set_defaults(func=list_remotes)

    auth_remote_approve_cmd = add_parser(
        "approve",
        subparser=auth_remote_subparsers,
        help="approves pending or denied remote connection",
    )
    auth_remote_approve_cmd.add_argument(
        "user_id",
        help="user_id or identity of pending credential or cert to approve"
    )
    auth_remote_approve_cmd.set_defaults(func=approve_remote)

    auth_remote_deny_cmd = add_parser(
        "deny",
        subparser=auth_remote_subparsers,
        help="denies pending or denied remote connection",
    )
    auth_remote_deny_cmd.add_argument(
        "user_id",
        help="user_id or identity of pending credential or cert to deny"
    )
    auth_remote_deny_cmd.set_defaults(func=deny_remote)

    auth_remote_delete_cmd = add_parser(
        "delete",
        subparser=auth_remote_subparsers,
        help="approves pending or denied remote connection",
    )
    auth_remote_delete_cmd.add_argument(
        "user_id",
        help="user_id or identity of pending credential or cert to delete"
    )
    auth_remote_delete_cmd.set_defaults(func=delete_remote)

    auth_rpc = add_parser(
        "rpc", subparser=auth_subparsers,
        help="Manage rpc method authorizations"
    )

    auth_rpc_subparsers = auth_rpc.add_subparsers(
        title="subcommands", metavar="", dest="store_commands"
    )
    auth_rpc_add = add_parser(
        "add", subparser=auth_rpc_subparsers,
        help="adds rpc method authorizations"
    )

    auth_rpc_add.add_argument(
        "pattern",
        nargs="*",
        help="Identity of agent and method, followed "
             "by capabilities. "
             "Should be in the format: "
             "agent_id.method authorized_capability1 "
             "authorized_capability2 ...",
    )
    auth_rpc_add.set_defaults(func=add_agent_rpc_authorizations,
                              min_uuid_len=1)

    auth_rpc_remove = add_parser(
        "remove",
        subparser=auth_rpc_subparsers,
        help="removes rpc method authorizations",
    )

    auth_rpc_remove.add_argument(
        "pattern",
        nargs="*",
        help="Identity of agent and method, "
             "followed by capabilities. "
             "Should be in the format: "
             "agent_id.method "
             "authorized_capability1 "
             "authorized_capability2 ...",
    )
    auth_rpc_remove.set_defaults(func=remove_agent_rpc_authorizations,
                                 min_uuid_len=1)

    # ====================================================
    # config commands
    # ====================================================
    config_store = add_parser("config",
                              help="manage the platform configuration store")

    config_store_subparsers = config_store.add_subparsers(
        title="subcommands", metavar="", dest="store_commands"
    )

    config_store_store = add_parser(
        "store", help="store a configuration",
        subparser=config_store_subparsers
    )

    config_store_store.add_argument("identity",
                                    help="VIP IDENTITY of the store")
    config_store_store.add_argument(
        "name", help="name used to reference the configuration by in the store"
    )
    config_store_store.add_argument(
        "infile",
        nargs="?",
        type=argparse.FileType("r"),
        default=sys.stdin,
        help="file containing the contents of the configuration",
    )
    config_store_store.add_argument(
        "--raw",
        const="raw",
        dest="config_type",
        action="store_const",
        help="interpret the input file as raw data",
    )
    config_store_store.add_argument(
        "--json",
        const="json",
        dest="config_type",
        action="store_const",
        help="interpret the input file as json",
    )
    config_store_store.add_argument(
        "--csv",
        const="csv",
        dest="config_type",
        action="store_const",
        help="interpret the input file as csv",
    )

    config_store_store.set_defaults(func=add_config_to_store,
                                    config_type="json")

    config_store_edit = add_parser(
        "edit",
        help="edit a configuration. (nano by default, respects EDITOR env "
             "variable)",
        subparser=config_store_subparsers,
    )

    config_store_edit.add_argument("identity",
                                   help="VIP IDENTITY of the store")
    config_store_edit.add_argument(
        "name", help="name used to reference the configuration by in the store"
    )
    config_store_edit.add_argument(
        "--editor",
        dest="editor",
        help="Set the editor to use to change the file. Defaults to nano if "
             "EDITOR is not set",
        default=os.getenv("EDITOR", "nano"),
    )
    config_store_edit.add_argument(
        "--raw",
        const="raw",
        dest="config_type",
        action="store_const",
        help="Interpret the configuration as raw data. If the file already "
             "exists this is ignored.",
    )
    config_store_edit.add_argument(
        "--json",
        const="json",
        dest="config_type",
        action="store_const",
        help="Interpret the configuration as json. If the file already "
             "exists this is ignored.",
    )
    config_store_edit.add_argument(
        "--csv",
        const="csv",
        dest="config_type",
        action="store_const",
        help="Interpret the configuration as csv. If the file already exists "
             "this is ignored.",
    )
    config_store_edit.add_argument(
        "--new",
        dest="new_config",
        action="store_true",
        help="Ignore any existing configuration and creates new empty file."
             " Configuration is not written if left empty. Type defaults to "
             "JSON.",
    )

    config_store_edit.set_defaults(func=edit_config, config_type="json")

    config_store_delete = add_parser(
        "delete", help="delete a configuration",
        subparser=config_store_subparsers
    )
    config_store_delete.add_argument("identity",
                                     help="VIP IDENTITY of the store")
    config_store_delete.add_argument(
        "name",
        nargs="?",
        help="name used to reference the configuration by in the store",
    )
    config_store_delete.add_argument(
        "--all",
        dest="delete_store",
        action="store_true",
        help="delete all configurations in the store",
    )

    config_store_delete.set_defaults(func=delete_config_from_store)

    config_store_list = add_parser(
        "list",
        help="list stores or configurations in a store",
        subparser=config_store_subparsers,
    )

    config_store_list.add_argument(
        "identity", nargs="?", help="VIP IDENTITY of the store to list"
    )

    config_store_list.set_defaults(func=list_store)

    config_store_get = add_parser(
        "get",
        help="get the contents of a configuration",
        subparser=config_store_subparsers,
    )

    config_store_get.add_argument("identity", help="VIP IDENTITY of the store")
    config_store_get.add_argument(
        "name", help="name used to reference the configuration by in the store"
    )
    config_store_get.add_argument(
        "--raw", action="store_true", help="get the configuration as raw data"
    )
    config_store_get.set_defaults(func=get_config)

    shutdown = add_parser("shutdown", help="stop all agents")
    shutdown.add_argument(
        "--platform", action="store_true",
        help="also stop the platform process"
    )
    shutdown.set_defaults(func=shutdown_agents, platform=False)

    send = add_parser("send", help="send agent and start on a remote platform")
    send.add_argument("wheel", nargs="+", help="agent package to send")
    send.set_defaults(func=send_agent)

    stats = add_parser("stats",
                       help="manage router message statistics tracking")
    op = stats.add_argument(
        "op", choices=["status", "enable", "disable", "dump", "pprint"],
        nargs="?"
    )
    stats.set_defaults(func=do_stats, op="status")

    # ==============================================================================
    global message_bus, rmq_mgmt

    if message_bus == "rmq":
        rmq_mgmt = RabbitMQMgmt()
        # ====================================================
        # rabbitmq commands
        # ====================================================
        rabbitmq_cmds = add_parser("rabbitmq", help="manage rabbitmq")
        rabbitmq_subparsers = rabbitmq_cmds.add_subparsers(
            title="subcommands", metavar="", dest="store_commands"
        )
        rabbitmq_add_vhost = add_parser(
            "add-vhost", help="add a new virtual host",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_add_vhost.add_argument("vhost", help="Virtual host")
        rabbitmq_add_vhost.set_defaults(func=add_vhost)

        rabbitmq_add_user = add_parser(
            "add-user",
            help="Add a new user. User will have admin privileges i.e,"
                 "configure, read and write",
            subparser=rabbitmq_subparsers,
        )
        rabbitmq_add_user.add_argument("user", help="user id")
        rabbitmq_add_user.add_argument("pwd", help="password")
        rabbitmq_add_user.set_defaults(func=add_user)

        rabbitmq_add_exchange = add_parser(
            "add-exchange", help="add a new exchange",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_add_exchange.add_argument("name", help="Name of the exchange")
        rabbitmq_add_exchange.add_argument(
            "type", help="Type of the exchange - fanout/direct/topic"
        )
        rabbitmq_add_exchange.set_defaults(func=add_exchange)

        rabbitmq_add_queue = add_parser(
            "add-queue", help="add a new queue", subparser=rabbitmq_subparsers
        )
        rabbitmq_add_queue.add_argument("name", help="Name of the queue")
        rabbitmq_add_queue.set_defaults(func=add_queue)
        # =====================================================================
        # List commands
        rabbitmq_list_vhosts = add_parser(
            "list-vhosts", help="List virtual hosts",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_list_vhosts.set_defaults(func=list_vhosts)

        rabbitmq_list_users = add_parser(
            "list-users", help="List users", subparser=rabbitmq_subparsers
        )
        rabbitmq_list_users.set_defaults(func=list_users)

        rabbitmq_list_user_properties = add_parser(
            "list-user-properties", help="List users",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_list_user_properties.add_argument("user",
                                                   help="RabbitMQ user id")
        rabbitmq_list_user_properties.set_defaults(func=list_user_properties)

        rabbitmq_list_exchanges = add_parser(
            "list-exchanges", help="List exhanges",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_list_exchanges.set_defaults(func=list_exchanges)

        rabbitmq_list_exchanges_props = add_parser(
            "list-exchange-properties",
            help="list exchanges with properties",
            subparser=rabbitmq_subparsers,
        )
        rabbitmq_list_exchanges_props.set_defaults(
            func=list_exchanges_with_properties)

        rabbitmq_list_queues = add_parser(
            "list-queues", help="list all queues",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_list_queues.set_defaults(func=list_queues)
        rabbitmq_list_queues_props = add_parser(
            "list-queue-properties",
            help="list queues with properties",
            subparser=rabbitmq_subparsers,
        )
        rabbitmq_list_queues_props.set_defaults(
            func=list_queues_with_properties)

        rabbitmq_list_bindings = add_parser(
            "list-bindings",
            help="list all bindings with exchange",
            subparser=rabbitmq_subparsers,
        )
        rabbitmq_list_bindings.add_argument("exchange", help="Source exchange")
        rabbitmq_list_bindings.set_defaults(func=list_bindings)

        rabbitmq_list_fed_parameters = add_parser(
            "list-federation-parameters",
            help="list all federation parameters",
            subparser=rabbitmq_subparsers,
        )
        rabbitmq_list_fed_parameters.set_defaults(func=list_fed_parameters)

        rabbitmq_list_fed_links = add_parser(
            "list-federation-links",
            help="list all federation links",
            subparser=rabbitmq_subparsers,
        )
        rabbitmq_list_fed_links.set_defaults(func=list_fed_links)

        rabbitmq_list_shovel_links = add_parser(
            "list-shovel-links",
            help="list all Shovel links",
            subparser=rabbitmq_subparsers,
        )
        rabbitmq_list_shovel_links.set_defaults(func=list_shovel_links)

        rabbitmq_list_shovel_parameters = add_parser(
            "list-shovel-parameters",
            help="list all shovel parameters",
            subparser=rabbitmq_subparsers,
        )
        rabbitmq_list_shovel_parameters.set_defaults(
            func=list_shovel_parameters)

        rabbitmq_list_policies = add_parser(
            "list-policies", help="list all policies",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_list_policies.set_defaults(func=list_policies)
        # =====================================================================
        # Remove commands
        rabbitmq_remove_vhosts = add_parser(
            "remove-vhosts", help="Remove virtual host/s",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_remove_vhosts.add_argument("vhost", nargs="+",
                                            help="Virtual host")
        rabbitmq_remove_vhosts.set_defaults(func=remove_vhosts)

        rabbitmq_remove_users = add_parser(
            "remove-users", help="Remove virtual user/s",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_remove_users.add_argument("user", nargs="+",
                                           help="Virtual host")
        rabbitmq_remove_users.set_defaults(func=remove_users)

        rabbitmq_remove_exchanges = add_parser(
            "remove-exchanges", help="Remove exchange/s",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_remove_exchanges.add_argument(
            "exchanges", nargs="+", help="Remove exchanges/s"
        )
        rabbitmq_remove_exchanges.set_defaults(func=remove_exchanges)

        rabbitmq_remove_queues = add_parser(
            "remove-queues", help="Remove queue/s",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_remove_queues.add_argument("queues", nargs="+", help="Queue")
        rabbitmq_remove_queues.set_defaults(func=remove_queues)

        rabbitmq_remove_fed_parameters = add_parser(
            "remove-federation-links",
            help="Remove federation parameter",
            subparser=rabbitmq_subparsers,
        )
        rabbitmq_remove_fed_parameters.add_argument(
            "parameters", nargs="+", help="parameter name/s"
        )
        rabbitmq_remove_fed_parameters.set_defaults(func=remove_fed_parameters)

        rabbitmq_remove_shovel_parameters = add_parser(
            "remove-shovel-links",
            help="Remove shovel parameter",
            subparser=rabbitmq_subparsers,
        )
        rabbitmq_remove_shovel_parameters.add_argument(
            "parameters", nargs="+", help="parameter name/s"
        )
        rabbitmq_remove_shovel_parameters.set_defaults(
            func=remove_shovel_parameters)

        rabbitmq_remove_policies = add_parser(
            "remove-policies", help="Remove policy",
            subparser=rabbitmq_subparsers
        )
        rabbitmq_remove_policies.add_argument(
            "policies", nargs="+", help="policy name/s"
        )
        rabbitmq_remove_policies.set_defaults(func=remove_policies)
    # ===============================================================================================
    if HAVE_RESTRICTED:
        cgroup = add_parser(
            "create-cgroups",
            help="setup VOLTTRON control group for restricted execution",
        )
        cgroup.add_argument(
            "-u", "--user", metavar="USER", help="owning user name or ID"
        )
        cgroup.add_argument(
            "-g", "--group", metavar="GROUP", help="owning group name or ID"
        )
        cgroup.set_defaults(func=create_cgroups, user=None, group=None)

    # Parse and expand options
    args = sys.argv[1:]

    # TODO: for auth some of the commands will work when volttron is down and
    # some will error (example vctl auth serverkey). Do check inside auth
    # function
    # Below vctl commands can work even when volttron is not up. For others
    # volttron need to be up.
    if len(args) > 0:
        if args[0] not in ("list", "tag", "auth", "rabbitmq", "certs"):
            # check pid file
            if not utils.is_volttron_running(volttron_home):
                _stderr.write(
                    "VOLTTRON is not running. This command "
                    "requires VOLTTRON platform to be running\n"
                )
                return 10

    conf = os.path.join(volttron_home, "config")
    if os.path.exists(conf) and "SKIP_VOLTTRON_CONFIG" not in os.environ:
        args = ["--config", conf] + args
    opts = parser.parse_args(args)

    if opts.log:
        opts.log = config.expandall(opts.log)
    if opts.log_config:
        opts.log_config = config.expandall(opts.log_config)
    opts.vip_address = config.expandall(opts.vip_address)
    if getattr(opts, "show_config", False):
        for name, value in sorted(vars(opts).items()):
            print(name, repr(value))
        return

    # Configure logging
    level = max(1, opts.verboseness)
    if opts.log is None:
        log_to_file(sys.stderr, level)
    elif opts.log == "-":
        log_to_file(sys.stdout, level)
    elif opts.log:
        log_to_file(opts.log, level,
                    handler_class=logging.handlers.WatchedFileHandler)
    else:
        log_to_file(None, 100, handler_class=lambda x: logging.NullHandler())
    if opts.log_config:
        logging.config.fileConfig(opts.log_config)

    opts.aip = aipmod.AIPplatform(opts)
    opts.aip.setup()

    opts.connection = None
    if utils.is_volttron_running(volttron_home):
        opts.connection = ControlConnection(opts.vip_address)

    try:
        with gevent.Timeout(opts.timeout):
            return opts.func(opts)
    except gevent.Timeout:
        _stderr.write("{}: operation timed out\n".format(opts.command))
        return 75
    except RemoteError as exc:
        print_tb = exc.print_tb
        error = exc.message
    except AttributeError as exc:
        _stderr.write(
            "Invalid command: '{}' or command requires additional arguments\n".format(
                opts.command
            )
        )
        parser.print_help()
        return 1
    # raised during install if wheel not found.
    except FileNotFoundError as exc:
        _stderr.write(f"{exc.args[0]}\n")
        return 1
    except SystemExit as exc:
        # Handles if sys.exit is called from within a function if not 0
        # then we know there was an error and processing will continue
        # else we return 0 from here.  This has the added effect of
        # allowing us to cascade short circuit calls.
        if exc.args[0] != 0:
            error = exc.message
        else:
            return 0
    except InstallRuntimeError as exrt:
        if opts.debug:
            _log.exception(exrt)
        _stderr.write(f"{exrt.args[0]}\n")
        return 1
    finally:
        # make sure the connection to the server is closed when this scriopt is about to exit.
        if opts.connection:
            try:
                opts.connection.kill()
            except Unreachable:
                # its ok for this to fail at this point it might not even be valid.
                pass
            finally:
                opts.connection = None

    _stderr.write("{}: error: {}\n".format(opts.command, error))
    return 20


def _main():
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)


if __name__ == "__main__":
    _main()
