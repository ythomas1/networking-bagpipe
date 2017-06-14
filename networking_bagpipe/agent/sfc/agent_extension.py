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

"""
L2 Agent extension to support bagpipe networking-sfc driver RPCs in the
Linux Bridge agent
"""
from copy import deepcopy

from oslo_log import helpers as log_helpers
from oslo_log import log as logging
from oslo_serialization import jsonutils

from oslo_concurrency import lockutils

from networking_bagpipe.agent import agent_base_info
from networking_bagpipe.agent.common import constants as b_const

from networking_bagpipe.agent import bagpipe_bgp_agent

from networking_bagpipe.driver.sfc.common import constants as sfc_const
from networking_bagpipe.objects.sfc import chain_hop

from neutron_lib.agent import l2_extension
from neutron_lib import constants as n_const

from neutron.api.rpc.callbacks import events as rpc_events
from neutron.api.rpc.callbacks.consumer import registry as cons_registry
from neutron.api.rpc.callbacks import resources
from neutron.api.rpc.handlers import resources_rpc

from neutron.plugins.ml2.drivers.linuxbridge.agent.common \
    import constants as lb_agt_constants
from neutron.plugins.ml2.drivers.linuxbridge.agent.linuxbridge_neutron_agent \
    import LinuxBridgeManager

LOG = logging.getLogger(__name__)

SFC_SERVICE = 'sfc'


class BagpipeSfcAgentExtension(l2_extension.L2AgentExtension,
                               agent_base_info.BaseInfoManager):

    def initialize(self, connection, driver_type):

        if driver_type != lb_agt_constants.EXTENSION_DRIVER_TYPE:
            raise Exception("This extension is currently working only with the"
                            " Linux Bridge Agent")

        self.bagpipe_bgp_agent = (
            bagpipe_bgp_agent.BaGPipeBGPAgent.get_instance(
                n_const.AGENT_TYPE_LINUXBRIDGE)
        )

        self.bagpipe_bgp_agent.register_build_callback(
            SFC_SERVICE,
            self.build_sfc_attach_info)

        self.ports = set()
        self.bagpipe_bgp_agent.register_port_list(SFC_SERVICE,
                                                  self.ports)

        self._setup_rpc(connection)

    def _setup_rpc(self, connection):
        self._pull_rpc = resources_rpc.ResourcesPullRpcApi()
        self._register_rpc_consumers(connection)

    def _register_rpc_consumers(self, connection):
        endpoints = [resources_rpc.ResourcesPushRpcCallback()]

        # Consume BaGPipeChainHop OVO RPC
        cons_registry.register(self.handle_sfc_chain_hops,
                               chain_hop.BaGPipeChainHop.obj_name())
        topic_chain_hop = resources_rpc.resource_type_versioned_topic(
            chain_hop.BaGPipeChainHop.obj_name())
        connection.create_consumer(topic_chain_hop, endpoints, fanout=True)

        # Consume BaGPipePortHops OVO RPC
        cons_registry.register(self.handle_sfc_port_hops,
                               chain_hop.BaGPipePortHops.obj_name())
        topic_port_hops = resources_rpc.resource_type_versioned_topic(
            chain_hop.BaGPipePortHops.obj_name())
        connection.create_consumer(topic_port_hops, endpoints, fanout=True)

    def _add_sfc_service_info_helper(self, port_ids, service_info, side):
        for index, port_id in enumerate(port_ids):
            if port_id not in self.ports_info:
                LOG.warning("%s service inconsistent for port %s",
                            SFC_SERVICE, port_id)
                continue

            orig_info = deepcopy(service_info)
            port_info = self.ports_info[port_id]

            # Set gateway IP address in NetworkInfo if necessary
            net_info = port_info.network
            if net_info.gateway_info == b_const.NO_GW_INFO:
                gateway_info = b_const.GatewayInfo(None,
                                                   service_info[side + '_gw'])
                net_info.set_gateway_info(gateway_info)

            if side == sfc_const.EGRESS:
                orig_info['lb_consistent_hash_order'] = index

            port_info.add_service_info({side: orig_info})

    def _remove_sfc_service_info_helper(self, port_id, service_info, side):
        port_info = self.ports_info[port_id]

        sfc_info = port_info.service_infos
        if side in sfc_info:
            if side == sfc_const.EGRESS:
                sfc_info[side].pop('lb_consistent_hash_order')

            if service_info != sfc_info[side]:
                LOG.warning("%s service inconsistent %s informations for "
                            "port %s", SFC_SERVICE, side, port_id)
                return

            sfc_info.pop(side)

    def build_sfc_attach_info(self, port_id):
        if port_id not in self.ports_info:
            LOG.warning("%s service has no PortInfo for port %s",
                        SFC_SERVICE, port_id)
            return {}

        port_info = self.ports_info[port_id]

        if not port_info.service_infos:
            return {}

        service_info = port_info.service_infos
        attach_info = {
            'network_id': port_info.network.id,
            'ip_address': port_info.ip_address,
            'mac_address': port_info.mac_address,
            'gateway_ip': port_info.network.gateway_info.ip,
            'local_port': port_info.local_port,
            b_const.IPVPN: {
#                'vpn_instance_id': '%s_%s' % (port_info.id, b_const.IPVPN),
                b_const.RT_IMPORT: (service_info[sfc_const.INGRESS]['rts']
                                    if service_info.get(sfc_const.INGRESS)
                                    else []),
                b_const.RT_EXPORT: (service_info[sfc_const.EGRESS]['rts']
                                    if service_info.get(sfc_const.EGRESS)
                                    else [])
            }
        }

        if not all(side in service_info for side in [sfc_const.INGRESS,
                                                     sfc_const.EGRESS]):
            if service_info.get(sfc_const.EGRESS):
                attach_info[b_const.IPVPN].update(
                    dict(direction='only-to-port')
                )
            elif service_info.get(sfc_const.INGRESS):
                attach_info[b_const.IPVPN].update(
                    dict(direction='only-from-port')
                )
            else:
                LOG.warning("%s service inconsistent for INGRESS and EGRESS "
                            "informations for port %s", SFC_SERVICE, port_id)

        egress_info = service_info.get(sfc_const.EGRESS)
        if egress_info:
            if egress_info.get('readv_to_rts'):
                attach_info[b_const.IPVPN].update({
                    'readvertise': dict(
                        from_rt=egress_info.get('readv_from_rts', []),
                        to_rt=egress_info['readv_to_rts']
                    )
                })

            if (egress_info.get('redirect_rts') and
                    egress_info.get('classifiers')):
                classifier = jsonutils.loads(egress_info['classifiers'])[0]

                attract_traffic = dict(
                    redirect_rts=egress_info['redirect_rts'],
                    classifier=classifier
                )

                if egress_info.get('attract_to_rts'):
                    destination_prefix = classifier.get('destinationPrefix',
                                                        '0.0.0.0/0')

                    attract_traffic.update(dict(
                        to_rt=egress_info['attract_to_rts'],
                        static_destination_prefixes=[destination_prefix])
                    )

                attach_info[b_const.IPVPN].update(
                    dict(attract_traffic=attract_traffic)
                )

            attach_info[b_const.IPVPN].update(dict(
                lb_consistent_hash_order=egress_info['lb_consistent_hash_order'])
            )

        attach_info[b_const.IPVPN].update(
            dict(linuxbr=LinuxBridgeManager.get_bridge_name(
                 port_info.network.id))
        )

        return attach_info

    def _build_sfc_detach_info(self, port_id):
        port_info = self.ports_info[port_id]

        detach_info = {
            b_const.IPVPN: {
#                'vpn_instance_id': '%s_%s' % (port_info.id, b_const.IPVPN),
                'network_id': port_info.network.id,
                'ip_address': port_info.ip_address,
                'mac_address': port_info.mac_address,
                'local_port': port_info.local_port
            }
        }

        egress_info = port_info.service_infos.get(sfc_const.EGRESS)
        if egress_info:
            if egress_info.get('readv_to_rts'):
                detach_info[b_const.IPVPN].update({
                    'readvertise': dict(
                        from_rt=egress_info.get('readv_from_rts', []),
                        to_rt=egress_info['readv_to_rts']
                    )
                })

        return detach_info

    def handle_sfc_chain_hops(self, context, resource_type, chain_hops,
                              event_type):
        LOG.debug("handle_sfc_chain_hops called with: {resource_type: %s, "
                  "chain_hops: %s, event_type: %s" %
                  (resource_type, chain_hops, event_type))
        if event_type == rpc_events.CREATED:
            self.sfc_ports_attach(chain_hops)
        elif event_type == rpc_events.DELETED:
            self.sfc_ports_detach(chain_hops)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgp-agent')
    def sfc_ports_attach(self, chain_hops):
        ports_to_attach = set()

        for chain_hop in chain_hops:
            chain_hop_dict = chain_hop.to_dict()

            ingress_ids = chain_hop_dict.pop('ingress_ports')
            egress_ids = chain_hop_dict.pop('egress_ports')

            if ingress_ids:
                self._add_sfc_service_info_helper(ingress_ids, chain_hop_dict,
                                                  sfc_const.INGRESS)
                ports_to_attach.update(ingress_ids)

            if egress_ids:
                self._add_sfc_service_info_helper(egress_ids, chain_hop_dict,
                                                  sfc_const.EGRESS)
                ports_to_attach.update(egress_ids)

        for port_id in ports_to_attach:
            self.bagpipe_bgp_agent.do_port_plug(port_id)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgp-agent')
    def sfc_ports_detach(self, chain_hops):
        ports_to_detach = dict()

        for chain_hop in chain_hops:
            chain_hop_dict = chain_hop.to_dict()

            port_ids = (chain_hop_dict.pop('ingress_ports') +
                        chain_hop_dict.pop('egress_ports'))

            for port_id in port_ids:
                if port_id not in self.ports_info:
                    LOG.warning("%s service inconsistent for port %s",
                                SFC_SERVICE, port_id)
                    continue

                if port_id not in ports_to_detach:
                    detach_info = self._build_sfc_detach_info(port_id)
                    self.ports_info[port_id].service_infos = {}

                    ports_to_detach[port_id] = detach_info

        for port_id, detach_info in ports_to_detach.items():
            self.bagpipe_bgp_agent.do_port_plug_refresh(port_id, detach_info)

    def handle_sfc_port_hops(self, context, resource_type, port_hops,
                              event_type):
        LOG.debug("handle_sfc_port_hops called with: {resource_type: %s, "
                  "port_hops: %s, event_type: %s" %
                  (resource_type, port_hops, event_type))

    def handle_port(self, context, port):
        port_id = port['port_id']
        net_id = port['network_id']

        net_info, port_info = (
            self._get_network_port_infos(net_id, port_id)
        )

        # Set IP and MAC addresses in PortInfo
        ip_address = port['fixed_ips'][0]['ip_address'] + '/32'
        mac_address = port['mac_address']
        port_info.set_ip_mac_infos(ip_address, mac_address)

        port_info.set_local_port(port['device'])

        port_hops = self._pull_rpc.pull(context,
                                        chain_hop.BaGPipePortHops.obj_name(),
                                        port_id)

        if port_hops:
            for ingress_hop in port_hops.ingress_hops:
                self._add_sfc_service_info_helper([port_id],
                                                  ingress_hop.to_dict(),
                                                  sfc_const.INGRESS)

            for egress_hop in port_hops.egress_hops:
                self._add_sfc_service_info_helper([port_id],
                                                  egress_hop.to_dict(),
                                                  sfc_const.EGRESS)

            self.bagpipe_bgp_agent.do_port_plug(port_id)

        self.ports.add(port_id)

    def delete_port(self, context, port):
        port_id = port['port_id']
        net_id = port['network_id']

        self._remove_network_port_infos(net_id, port_id)
        self.ports.remove(port_id)
