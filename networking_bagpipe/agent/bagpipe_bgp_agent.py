# Copyright (c) 2015 Orange.
# All Rights Reserved.
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
import socket

import httplib2
import json

from copy import deepcopy

from collections import defaultdict

from oslo_config import cfg

from oslo_log import log as logging

from oslo_concurrency import lockutils

from oslo_service import loopingcall

from networking_bagpipe._i18n import _

from networking_bagpipe.agent.common import constants as b_const

from neutron.conf.agent import common as config

from neutron_lib import exceptions as n_exc

from neutron.conf.plugins.ml2.drivers import ovs_conf

LOG = logging.getLogger(__name__)


# Having this at line 231 is apparently not enough, so adding here as well:
# pylint: disable=not-callable

bagpipe_bgp_opts = [
    cfg.IntOpt('ping_interval', default=10,
               help=_("The number of seconds the bagpipe-bgp client will "
                      "wait between polling for restart detection.")),
    cfg.StrOpt('bagpipe_bgp_ip', default='127.0.0.1',
               help=_("bagpipe-bgp REST service IP address.")),
    cfg.IntOpt('bagpipe_bgp_port', default=8082,
               help=_("bagpipe-bgp REST service IP port.")),
]

cfg.CONF.register_opts(bagpipe_bgp_opts, "BAGPIPE")
ovs_conf.register_ovs_agent_opts()
config.register_agent_state_opts_helper(cfg.CONF)


class BaGPipeBGPException(n_exc.NeutronException):
    message = "An exception occurred when calling bagpipe-bgp \
               REST service: %(reason)s"


class HTTPClientBase(object):
    """An HTTP client base class"""

    def __init__(self, host="127.0.0.1", port=8082,
                 client_name="HTTP client base"):
        """Create a new HTTP client

        :param host: HTTP server IP address
        :param port: HTTP server port
        """
        self.host = host
        self.port = port
        self.client_name = client_name

    def do_request(self, method, action, body=None):
        LOG.debug("bagpipe-bgp client request: %(method)s %(action)s "
                  "[%(body)s]",
                  {'method': method, 'action': action, 'body': str(body)})

        if isinstance(body, dict):
            body = json.dumps(body)
        try:
            headers = {'User-Agent': self.client_name,
                       "Content-Type": "application/json",
                       "Accept": "application/json"}
            uri = "http://%s:%s/%s" % (self.host, self.port, action)

            http = httplib2.Http()
            response, content = http.request(uri, method, body, headers)
            LOG.debug("bagpipe-bgp returns [%(status)s:%(content)s]",
                      {'status': str(response.status), 'content': content})

            if response.status == 200:
                if content and len(content) > 1:
                    return json.loads(content)
            else:
                reason = (
                    "An HTTP operation has failed on bagpipe-bgp."
                )
                raise BaGPipeBGPException(reason=reason)
        except (socket.error, IOError) as e:
            reason = "Failed to connect to bagpipe-bgp: %s" % str(e)
            raise BaGPipeBGPException(reason=reason)

    def get(self, action):
        return self.do_request("GET", action)

    def post(self, action, body=None):
        return self.do_request("POST", action, body=body)

    def put(self, action, body=None):
        return self.do_request("PUT", action, body=body)

    def delete(self, action):
        return self.do_request("DELETE", action)


class keydefaultdict(defaultdict):
    """Inherit defaultdict class to customize default_factory.

    Override __missing__ method to construct custom object as default_factory
    passing an argument.

    For example:
    class C(object):
        def __init__(self, value):
            self.value = value

    d = keydefaultdict(C)
    d[key] returns C(key)
    """
    def __missing__(self, key):
        if self.default_factory is None:
            raise KeyError(key)
        else:
            # pylint: disable=not-callable
            ret = self[key] = getattr(self, 'default_factory')(key)
            return ret


class CommonInfo(object):

    def __init__(self, id):
        self.id = id
        self.service_infos = dict()

    def add_service_info(self, service_name, service_info):
        if not service_info:
            return

        if service_info != self.service_infos.get(service_name, {}):
            self.service_infos[service_name] = service_info


class PortInfo(CommonInfo):

    def __init__(self, port_id):
        super(PortInfo, self).__init__(port_id)

        self.ip_address = None
        self.mac_address = None
        self.network = None
        self.local_port = dict()

    def __eq__(self, other):
        return (isinstance(other, self.__class__) and
                self.id == other.id)

    def __hash__(self):
        return hash(self.id)

    def set_local_port(self, linuxif):
        self.local_port = dict(linuxif=linuxif)

    def set_ip_mac_infos(self, ip_address, mac_address):
        self.ip_address = ip_address
        self.mac_address = mac_address

    def set_network(self, network):
        if not self.network:
            self.network = network
        else:
            LOG.warning('Network reference has already been set for port')


class NetworkInfo(CommonInfo):

    def __init__(self, network_id):
        super(NetworkInfo, self).__init__(network_id)

        self.gateway_info = b_const.NO_GW_INFO
        self.ports = set()

    def set_gateway_info(self, gateway_info):
        self.gateway_info = gateway_info


class BaGPipeBGPAgent(HTTPClientBase):
    """Implements a BaGPipe-BGP REST client"""

    _instance = None

    # bagpipe-bgp status
    BAGPIPEBGP_UP = 'UP'
    BAGPIPEBGP_DOWN = 'DOWN'

    @classmethod
    @lockutils.synchronized('bagpipe-bgp-agent')
    def _create_instance(cls, agent_type):
        if not cls.has_instance():
            cls._instance = cls(agent_type)

    @classmethod
    def has_instance(cls):
        return cls._instance is not None

    @classmethod
    def clear_instance(cls):
        cls._instance = None

    @classmethod
    def get_instance(cls, agent_type):
        # double checked locking
        if not cls.has_instance():
            cls._create_instance(agent_type)
        return cls._instance

    def __init__(self, agent_type):

        """Create a new BaGPipe-BGP REST service client.

        :param agent_type: bagpipe-bgp agent type (Linux bridge or OVS)

        """
        super(BaGPipeBGPAgent,
              self).__init__(cfg.CONF.BAGPIPE.bagpipe_bgp_ip,
                             cfg.CONF.BAGPIPE.bagpipe_bgp_port, agent_type)

        self.agent_type = agent_type

        self.ping_interval = cfg.CONF.BAGPIPE.ping_interval

        # Store all ports level network and service informations
        self.ports_info = keydefaultdict(PortInfo)
        # Store all networks level network and service informations
        self.networks_info = keydefaultdict(NetworkInfo)

        self.bagpipe_bgp_status = self.BAGPIPEBGP_DOWN
        self.seq_num = 0

        # Starts a greenthread for bagpipe-bgp status polling
        self._start_bagpipe_bgp_status_polling(self.ping_interval)

        self.registered_service = dict()

    def _check_bagpipe_bgp_status(self):
        """Trigger refresh on bagpipe-bgp restarts

        Check if bagpipe-bgp has restarted while sending ping request
        to detect sequence number change.
        If a restart is detected, re-send all registered attachments to
        bagpipe-bgp.
        """
        new_seq_num = self._request_ping()

        # Check bagpipe-bgp restart
        if new_seq_num != self.seq_num:
            if new_seq_num != -1:
                if self.seq_num != 0:
                    LOG.warning("bagpipe-bgp restart detected...")
                else:
                    LOG.info("bagpipe-bgp successfully detected")

                self.seq_num = new_seq_num
                self.bagpipe_bgp_status = self.BAGPIPEBGP_UP

                # Re-send all registered ports to bagpipe-bgp
                if self.ports_info:
                    LOG.info("Sending all registered ports to bagpipe-bgp")
                    LOG.debug("Registered ports list: %s", self.ports_info)
                    for port_id in self.ports_info:
                        self._do_port_plug(port_id)
                else:
                    LOG.info("No attachment to send to bagpipe-bgp")
            else:
                self.bagpipe_bgp_status = self.BAGPIPEBGP_DOWN

    def _start_bagpipe_bgp_status_polling(self, ping_interval=10):
        # Start bagpipe-bgp status polling at regular interval
        status_loop = loopingcall.FixedIntervalLoopingCall(
            self._check_bagpipe_bgp_status)
        status_loop.start(interval=ping_interval,
                          initial_delay=ping_interval)  # TM: why not zero ?

    def _get_network_port_infos(self, net_id, port_id):
        net_info = self.networks_info[net_id]
        port_info = self.ports_info[port_id]
        net_info.ports.add(port_info)
        port_info.set_network(net_info)

        return net_info, port_info

    def _remove_network_port_infos(self, net_id, port_id):
        port_info = self.ports_info.get(port_id)
        net_info = self.networks_info.get(net_id)

        if port_info:
            del self.ports_info[port_id]

        if net_info:
            net_info.ports.discard(port_info)
            if not net_info.ports:
                del self.networks_info[net_id]

    def _get_common_attach_info(self, port_info):
        """Format common attach informations

        {
            local_port: {
                linuxif: <LINUX_IF>
            },
            ip_address: <PORT_IP>,
            mac_address: <PORT_MAC>,
            gateway_ip: <NETWORK_GATEWAY_IP>
        }
        """
        net_info = port_info.network
        common_plug_details = {'local_port': port_info.local_port,
                               'ip_address': port_info.ip_address,
                               'mac_address': port_info.mac_address,
                               'gateway_ip': net_info.gateway_info.ip
                               }

        return common_plug_details

    def _compile_port_attach_info(self, port_id):
        port_info = self.ports_info.get(port_id)

        if not port_info:
            LOG.warning("No PortInfo found for port %s", port_id)
            return []

        net_info = port_info.network

        services = list(set(net_info.service_infos.keys()) |
                        set(port_info.service_infos.keys()))

        service_attach_info = {}
        for service in services:
            service_info = {}
            if net_info.service_infos.get(service, {}):
                service_info.update(deepcopy(net_info.service_infos[service]))
            if port_info.service_infos.get(service, {}):
                service_info.update(deepcopy(port_info.service_infos[service]))

            service_attach_info[service] = (
                getattr(self.registered_service[service],
                        '_compile_%s_attach_info' % service)(service_info,
                                                             port_info)
            )

        attach_list = defaultdict(list)
        for vpn_type in b_const.VPN_TYPES:
            attach_info = {}

            for service in services:
                if vpn_type in service_attach_info[service]:
                    if not attach_info:
                        attach_info = self._get_common_attach_info(port_info)
                        attach_info.update({'vpn_instance_id': '%s_%s' %
                                            (net_info.id, vpn_type)})

                    service_vpn_info = service_attach_info[service][vpn_type]

                    if vpn_type not in attach_info:
                        attach_info.update(dict(vpn_type=vpn_type))

                    # Check if static routes
                    static_routes = service_vpn_info.pop('static_routes', [])
                    if static_routes:
                        static_info = deepcopy(attach_info)
                        static_info.update({'advertise_subnet': True})
                        for static_route in static_routes:
                            static_info['ip_address'] = static_route
                            static_info.update(service_vpn_info)

                            attach_list[vpn_type].append(static_info)

                    for rt_type in b_const.RT_TYPES:
                        if rt_type in service_vpn_info:
                            if rt_type not in attach_info:
                                attach_info[rt_type] = []

                            attach_info[rt_type] += (
                                service_vpn_info.pop(rt_type)
                            )

                    # Check if plugging an EVPN into an IPVPN
                    if (vpn_type == b_const.IPVPN and
                            b_const.EVPN in attach_list):
                        attach_info['local_port'] = {
                            b_const.EVPN: {
                                'id': '%s_evpn' % net_info.id
                            }
                        }
                        service_vpn_info.pop('local_port', {})
                    else:
                        attach_info['local_port'].update(
                            service_vpn_info.pop('local_port', {})
                        )

                    attach_info.update(service_vpn_info)

            if attach_info:
                attach_list[vpn_type].append(attach_info)

        return attach_list

    def _request_ping(self):
        """Send ping request to bagpipe-bgp to get sequence number"""
        try:
            response = self.get('ping')
            LOG.debug("bagpipe-bgp PING response received with "
                      "sequence number %s", response)
            return response
        except BaGPipeBGPException as e:
            LOG.warning(str(e))
            return -1

    def _send_attach_local_port(self, local_port_details):
        """Send local port attach request to BaGPipe-BGP if running"""
        if self.bagpipe_bgp_status is self.BAGPIPEBGP_UP:
            try:
                self.post('attach_localport', local_port_details)
                LOG.debug("Local port has been attached to bagpipe-bgp with "
                          "details %s", local_port_details)
            except BaGPipeBGPException as e:
                LOG.error("Can't attach local port on bagpipe-bgp: %s", str(e))
        else:
            LOG.debug("Local port not yet attached to bagpipe-bgp (not up)")

    def _send_detach_local_port(self, local_port_details):
        """Send local port detach request to BaGPipe-BGP if running"""
        if self.bagpipe_bgp_status is self.BAGPIPEBGP_UP:
            try:
                self.post('detach_localport', local_port_details)
                LOG.debug("Local port has been detached from bagpipe-bgp "
                          "with details %s", local_port_details)
            except BaGPipeBGPException as e:
                LOG.error("Can't detach local port from bagpipe-bgp: %s",
                          str(e))
                raise
        else:
            LOG.debug("Local port not yet detached from bagpipe-bgp (not up)")

    def _do_port_plug(self, port_id):
        """Send port attach request to bagpipe-bgp."""
        all_plug_details = self._compile_port_attach_info(port_id)

        # First plug E-VPNs because they could be plugged into IP-VPNs
        for vpn_type in [t for t in b_const.VPN_TYPES
                         if t in all_plug_details]:
            for plug_detail in all_plug_details[vpn_type]:
                self._send_attach_local_port(plug_detail)

    def _do_port_unplug(self, port_id):
        """Send port detach request to bagpipe-bgp."""
        all_unplug_details = self._compile_port_attach_info(port_id)

        # First unplug IP-VPNs because E-VPNs could be plugged into them
        for vpn_type in [t for t in b_const.VPN_TYPES[::-1]
                         if t in all_unplug_details]:
            for unplug_detail in all_unplug_details[vpn_type]:
                self._send_detach_local_port(unplug_detail)

    @lockutils.synchronized('bagpipe-bgp-agent')
    def register_service(self, service_name, service):
        self.registered_service[service_name] = service
