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
L2 Agent extension to support bagpipe networking-bgpvpn driver RPCs in the
OpenVSwitch agent
"""
import itertools

import netaddr

from copy import deepcopy

from collections import defaultdict

from oslo_config import cfg

from oslo_concurrency import lockutils

from oslo_log import helpers as log_helpers
from oslo_log import log as logging

from networking_bagpipe.agent.common import constants as b_const

from networking_bagpipe.agent.bagpipe_bgp_agent import BaGPipeBGPAgent
from networking_bagpipe.agent.bagpipe_bgp_agent import BaGPipeBGPException

from networking_bagpipe.agent.bgpvpn import rpc_agent as bgpvpn_rpc
from networking_bagpipe.agent.bgpvpn.rpc_client import topics_BAGPIPE_BGPVPN

from neutron.agent.common import ovs_lib
from neutron.common import topics

from neutron_lib.callbacks import events
from neutron_lib.callbacks import registry
from neutron_lib.callbacks import resources
from neutron_lib import constants as n_const

from neutron.agent.l2 import agent_extension
from neutron.plugins.ml2.drivers.linuxbridge.agent.common \
    import constants as lnxbridge_agt_constants
from neutron.plugins.ml2.drivers.linuxbridge.agent.linuxbridge_neutron_agent \
    import LinuxBridgeManager
from neutron.plugins.ml2.drivers.openvswitch.agent.common \
    import constants as ovs_agt_constants
from neutron.plugins.ml2.drivers.openvswitch.agent.ovs_neutron_agent \
    import OVSNeutronAgent
from neutron.plugins.ml2.drivers.openvswitch.agent import vlanmanager

LOG = logging.getLogger(__name__)

bagpipe_bgp_opts = [
    cfg.StrOpt('mpls_bridge', default='br-mpls',
               help=_("OVS MPLS bridge to use")),
    cfg.StrOpt('tun_to_mpls_peer_patch_port', default='patch-to-mpls',
               help=_("OVS Peer patch port in tunnel bridge to MPLS bridge "
                      "(traffic to MPLS bridge)")),
    cfg.StrOpt('tun_from_mpls_peer_patch_port', default='patch-from-mpls',
               help=_("OVS Peer patch port in tunnel bridge to MPLS bridge "
                      "(traffic from MPLS bridge)")),
    cfg.StrOpt('mpls_to_tun_peer_patch_port', default='patch-to-tun',
               help=_("OVS Peer patch port in MPLS bridge to tunnel bridge "
                      "(traffic to tunnel bridge)")),
    cfg.StrOpt('mpls_from_tun_peer_patch_port', default='patch-from-tun',
               help=_("OVS Peer patch port in MPLS bridge to tunnel bridge "
                      "(traffic from tunnel bridge)")),
    cfg.StrOpt('mpls_to_int_peer_patch_port', default='patch-mpls-to-int',
               help=_("OVS Peer patch port in MPLS bridge to int bridge "
                      "(traffic to int bridge)")),
    cfg.StrOpt('int_from_mpls_peer_patch_port', default='patch-int-from-mpls',
               help=_("OVS Peer patch port in int bridge to MPLS bridge "
                      "(traffic from MPLS bridge)")),
]

cfg.CONF.register_opts(bagpipe_bgp_opts, "BAGPIPE")


class DummyOVSAgent(OVSNeutronAgent):
    # this class is used only to 'borrow' setup_entry_for_arp_reply
    # from OVSNeutronAgent
    arp_responder_enabled = True

    def __init__(self):
        pass


def has_attachement(bgpvpn_info, vpn_type):
    return (vpn_type in bgpvpn_info and (
            bgpvpn_info[vpn_type].get(b_const.RT_IMPORT) or
            bgpvpn_info[vpn_type].get(b_const.RT_EXPORT))
            )


class BagpipeBgpvpnAgentExtension(agent_extension.AgentCoreResourceExtension,
                                  bgpvpn_rpc.BGPVPNAgentRpcCallBackMixin):

    @log_helpers.log_method_call
    def consume_api(self, agent_api):
        self.agent_api = agent_api

    @log_helpers.log_method_call
    def initialize(self, connection, driver_type):
        if driver_type == ovs_agt_constants.EXTENSION_DRIVER_TYPE:
            self.int_br = self.agent_api.request_int_br()
            self.tun_br = self.agent_api.request_tun_br()

            if self.tun_br is None:
                raise Exception("tunneling is not enabled in OVS agent, "
                                "however bagpipe_bgpvpn extensions needs it")

            self.bagpipe_bgp_agent = BaGPipeBGPAgent.get_instance(
                n_const.AGENT_TYPE_OVS)

            if self.bagpipe_bgp_agent.agent_type != n_const.AGENT_TYPE_OVS:
                raise Exception("agent already configured with another type")

            self._setup_mpls_br()

            registry.subscribe(self.ovs_restarted,
                               resources.AGENT,
                               events.OVS_RESTARTED)

        elif driver_type == lnxbridge_agt_constants.EXTENSION_DRIVER_TYPE:
            self.bagpipe_bgp_agent = BaGPipeBGPAgent.get_instance(
                n_const.AGENT_TYPE_LINUXBRIDGE
            )

            if (self.bagpipe_bgp_agent.agent_type !=
                    n_const.AGENT_TYPE_LINUXBRIDGE):
                raise Exception("agent already configured with another type")
        else:
            raise Exception("driver type not supported: %s", driver_type)

        self._setup_rpc(connection)

        self.bagpipe_bgp_agent.register_service(b_const.BGPVPN_SERVICE,
                                                self)

        self.vlan_manager = vlanmanager.LocalVlanManager()

    def _setup_rpc(self, connection):
        connection.create_consumer(topics.get_topic_name(topics.AGENT,
                                                         topics_BAGPIPE_BGPVPN,
                                                         topics.UPDATE),
                                   [self], fanout=True)
        connection.create_consumer(topics.get_topic_name(topics.AGENT,
                                                         topics_BAGPIPE_BGPVPN,
                                                         topics.UPDATE,
                                                         cfg.CONF.host),
                                   [self], fanout=False)

    def _setup_mpls_br(self):
        '''Setup the MPLS bridge for bagpipe-bgp.

        Creates MPLS bridge, and links it to the integration and tunnel
        bridges using patch ports.

        :param mpls_br: the name of the MPLS bridge.
        '''
        mpls_br = cfg.CONF.BAGPIPE.mpls_bridge
        self.mpls_br = ovs_lib.OVSBridge(mpls_br)

        if not self.mpls_br.bridge_exists(mpls_br):
            LOG.error("Unable to enable MPLS on this agent, MPLS bridge "
                      "%(mpls_br)s doesn't exist. Agent terminated!",
                      {"mpls_br": mpls_br})
            exit(1)

        # patch ports for traffic from tun bridge to mpls bridge
        self.patch_tun_to_mpls_ofport = self.tun_br.add_patch_port(
            cfg.CONF.BAGPIPE.tun_to_mpls_peer_patch_port,
            cfg.CONF.BAGPIPE.mpls_from_tun_peer_patch_port)
        self.patch_mpls_from_tun_ofport = self.mpls_br.add_patch_port(
            cfg.CONF.BAGPIPE.mpls_from_tun_peer_patch_port,
            cfg.CONF.BAGPIPE.tun_to_mpls_peer_patch_port)

        # patch ports for traffic from mpls bridge to tun bridge
        self.patch_mpls_to_tun_ofport = self.mpls_br.add_patch_port(
            cfg.CONF.BAGPIPE.mpls_to_tun_peer_patch_port,
            cfg.CONF.BAGPIPE.tun_from_mpls_peer_patch_port)
        self.patch_tun_from_mpls_ofport = self.tun_br.add_patch_port(
            cfg.CONF.BAGPIPE.tun_from_mpls_peer_patch_port,
            cfg.CONF.BAGPIPE.mpls_to_tun_peer_patch_port)

        # patch ports for traffic from mpls bridge to int bridge
        self.patch_mpls_to_int_ofport = self.mpls_br.add_patch_port(
            cfg.CONF.BAGPIPE.mpls_to_int_peer_patch_port,
            cfg.CONF.BAGPIPE.int_from_mpls_peer_patch_port)
        self.patch_int_from_mpls_ofport = self.int_br.add_patch_port(
            cfg.CONF.BAGPIPE.int_from_mpls_peer_patch_port,
            cfg.CONF.BAGPIPE.mpls_to_int_peer_patch_port)

        if (int(self.patch_tun_to_mpls_ofport) < 0 or
                int(self.patch_mpls_from_tun_ofport) < 0 or
                int(self.patch_mpls_to_tun_ofport) < 0 or
                int(self.patch_tun_from_mpls_ofport) < 0 or
                int(self.patch_int_from_mpls_ofport) < 0 or
                int(self.patch_mpls_to_int_ofport) < 0):
            LOG.error("Failed to create OVS patch port. Cannot have "
                      "MPLS enabled on this agent, since this version "
                      "of OVS does not support patch ports. "
                      "Agent terminated!")
            exit(1)

        patch_int_ofport = self.tun_br.get_port_ofport(
            cfg.CONF.OVS.tun_peer_patch_port)

        # In br-tun, redirect all traffic from VMs toward a BGPVPN
        # default gateway MAC address to the MPLS bridge.
        #
        # (priority >0 is needed or we hit the rule redirecting unicast to
        # the UCAST_TO_TUN table)
        self.tun_br.add_flow(
            table=ovs_agt_constants.PATCH_LV_TO_TUN,
            priority=1,
            in_port=patch_int_ofport,
            dl_dst=b_const.DEFAULT_GATEWAY_MAC,
            actions="output:%s" % self.patch_tun_to_mpls_ofport
        )

        # Redirect traffic from the MPLS bridge to br-int
        self.tun_br.add_flow(in_port=self.patch_tun_from_mpls_ofport,
                             actions="output:%s" % patch_int_ofport)

    def ovs_restarted(self, resources, event, trigger):
        self._setup_mpls_br()
        self.ovs_restarted_bgpvpn()
        # TODO(tmorin): need to handle restart on bagpipe-bgp side, in the
        # meantime after an OVS restart, restarting bagpipe-bgp is required

    @log_helpers.log_method_call
    def _enable_gw_redirect(self, vlan, gateway_ip):
        # Add ARP responder entry for default gateway in br-tun

        # We may compete with the ARP responder entry for the real MAC
        # if the router is on a network node and we are a compute node,
        # so we must add our rule with a higher priority. Using a different
        # priority also means that arp_responder will not remove our ARP
        # responding flows and we won't remove theirs.

        # NOTE(tmorin): consider adding priority to install_arp_responder
        # and then use it here

        # (mostly copy-pasted ovs_ofctl....install_arp_responder)
        actions = ovs_agt_constants.ARP_RESPONDER_ACTIONS % {
            'mac': netaddr.EUI(b_const.DEFAULT_GATEWAY_MAC,
                               dialect=netaddr.mac_unix),
            'ip': netaddr.IPAddress(gateway_ip),
        }
        self.tun_br.add_flow(table=ovs_agt_constants.ARP_RESPONDER,
                             priority=2,  # see above
                             dl_vlan=vlan,
                             proto='arp',
                             arp_op=0x01,
                             arp_tpa='%s' % gateway_ip,
                             actions=actions)

    @log_helpers.log_method_call
    def _disable_gw_redirect(self, vlan, gateway_ip):
        # Remove ARP responder entry for default gateway in br-tun
        self.tun_br.delete_flows(
            strict=True,
            table=ovs_agt_constants.ARP_RESPONDER,
            priority=2,
            dl_vlan=vlan,
            proto='arp',
            arp_op=0x01,
            arp_tpa='%s' % gateway_ip)

    @log_helpers.log_method_call
    def _hide_real_gw_arp(self, vlan, gateway_info):
        # Kill ARP replies for the gateway IP coming on br-int from the real
        # router, if any.
        #
        # NOTE(tmorin): we assume that the router MAC exists only in this vlan.
        # Doing filtering based on the local vlan would be better, but
        # we can't do this in br-int because this bridge does tagging based
        # on ovs-vsctl port tags.
        self.int_br.add_flow(table=ovs_agt_constants.LOCAL_SWITCHING,
                             priority=2,
                             proto='arp',
                             arp_op=0x2,
                             dl_src=gateway_info.mac,
                             arp_sha=gateway_info.mac,
                             arp_spa=gateway_info.ip,
                             actions="drop")

        # ARP requests from the real gateway need to
        # have their IP address changed to hide the gateway
        # address or the VMs will use it to update their
        # ARP cache implicitly. Below we overwrite it with 0.0.0.0.
        self.int_br.add_flow(table=ovs_agt_constants.LOCAL_SWITCHING,
                             priority=2,
                             proto='arp',
                             arp_op=0x01,
                             dl_src=gateway_info.mac,
                             arp_spa=gateway_info.ip,
                             arp_sha=gateway_info.mac,
                             actions="load:0x0->NXM_OF_ARP_SPA[],NORMAL")

    @log_helpers.log_method_call
    def _unhide_real_gw_arp(self, vlan, gateway_mac):
        LOG.debug("unblocking ARP from real gateway for vlan %d (%s)",
                  vlan, gateway_mac)
        self.int_br.delete_flows(table=ovs_agt_constants.LOCAL_SWITCHING,
                                 proto='arp',
                                 dl_src=gateway_mac,
                                 arp_sha=gateway_mac)

    @log_helpers.log_method_call
    def _check_arp_voodoo_plug(self, net_info, gateway_info):

        if (self.bagpipe_bgp_agent.agent_type != n_const.AGENT_TYPE_OVS):
            return

        # See if we need to update gateway redirection and gateway ARP
        # voodoo

        vlan = self.vlan_manager.get(net_info.id).vlan

        # NOTE(tmorin): can be improved, only needed on first plug...
        self._enable_gw_redirect(vlan, gateway_info.ip)

        # update real gateway ARP blocking...
        # remove old ARP blocking ?
        if net_info.gateway_info.mac is not None:
            self._unhide_real_gw_arp(vlan, net_info.gateway_info.mac)
        # add new ARP blocking ?
        if gateway_info.mac:
            self._hide_real_gw_arp(vlan, gateway_info)

    @log_helpers.log_method_call
    def _check_arp_voodoo_unplug(self, net_id):

        if (self.bagpipe_bgp_agent.agent_type != n_const.AGENT_TYPE_OVS):
            return

        net_info = self.bagpipe_bgp_agent.networks_info.get(net_id)

        if not net_info:
            return

        # if last port for this network, then cleanup gateway redirection
        # NOTE(tmorin): shouldn't we check for last *ipvpn* attachment?
        if len(net_info.ports) == 1:
            LOG.debug("last unplug, undoing voodoo ARP")
            # NOTE(tmorin): vlan lookup might break if port is already
            # unplugged from bridge ?
            vlan = self.vlan_manager.get(net_id).vlan
            self._disable_gw_redirect(vlan, net_info.gateway_info.ip)
            if net_info.gateway_info.mac is not None:
                self._unhide_real_gw_arp(vlan, net_info.gateway_info.mac)

    def _is_last_bgpvpn_info(self, net_info, service_info):
        if b_const.BGPVPN_SERVICE not in net_info.service_infos:
            return

        orig_info = deepcopy(net_info.service_infos[b_const.BGPVPN_SERVICE])

        for vpn_type in b_const.BGPVPN_TYPES:
            if vpn_type in service_info:
                if vpn_type in orig_info:
                    for rt_type in b_const.RT_TYPES:
                        if rt_type in service_info[vpn_type]:
                            orig_info[vpn_type][rt_type] = list(
                                set(orig_info[vpn_type][rt_type]) -
                                set(service_info[vpn_type][rt_type]))

                            if not orig_info[vpn_type][rt_type]:
                                del(orig_info[vpn_type][rt_type])

                    if not orig_info[vpn_type]:
                        del(orig_info[vpn_type])

        return (not orig_info, orig_info)

    def _compile_bgpvpn_attach_info(self, service_info, port_info):
        attach_info = {}

        for bgpvpn_type, rt_type in list(
                itertools.product(b_const.BGPVPN_TYPES, b_const.RT_TYPES)):
            if rt_type in service_info.get(bgpvpn_type, {}):
                bagpipe_bgp_vpn_type = b_const.BGPVPN_TYPES_MAP[bgpvpn_type]
                if bagpipe_bgp_vpn_type not in attach_info:
                    attach_info[bagpipe_bgp_vpn_type] = defaultdict(list)

                attach_info[bagpipe_bgp_vpn_type][rt_type] += (
                    service_info[bgpvpn_type][rt_type]
                )

        if self.bagpipe_bgp_agent.agent_type == n_const.AGENT_TYPE_OVS:
            # Add OVS VLAN information
            vlan = self.vlan_manager.get(port_info.network.id).vlan
            for vpn_type in (vt for vt in b_const.VPN_TYPES
                             if vt in attach_info):
                attach_info[vpn_type].update({
                    'local_port': {
                        'ovs': {
                            'plugged': True,
                            'port_number': self.patch_mpls_from_tun_ofport,
                            'to_vm_port_number': self.patch_mpls_to_tun_ofport,
                            'vlan': vlan
                        }
                    }
                })

            if has_attachement(attach_info, b_const.IPVPN):
                # Add fallback information if needed as well
                if port_info.network.gateway_info.mac:
                    attach_info[b_const.IPVPN].update({
                        'fallback': {
                            'dst_mac': port_info.network.gateway_info.mac,
                            'src_mac': b_const.FALLBACK_SRC_MAC,
                            'ovs_port_number': self.patch_mpls_to_int_ofport
                        }
                    })
        else:
            if has_attachement(attach_info, b_const.EVPN):
                attach_info[b_const.EVPN]['linuxbr'] = (
                    LinuxBridgeManager.get_bridge_name(port_info.network.id)
                )
            if has_attachement(attach_info, b_const.IPVPN):
                # the interface we need to pass to bagpipe is the
                # bridge
                attach_info[b_const.IPVPN]['local_port'] = {
                    'linuxif':
                        LinuxBridgeManager.get_bridge_name(
                            port_info.network.id)
                }
                # NOTE(tmorin): fallback support still missing

        return attach_info

    def ovs_restarted_bgpvpn(self):
        for net_info in self.bagpipe_bgp_agent.networks_info.values():
            if net_info.ports and net_info.gateway_info != b_const.NO_GW_INFO:
                bgpvpn_info = net_info.service_infos.get(
                    b_const.BGPVPN_SERVICE)
                if has_attachement(bgpvpn_info, b_const.BGPVPN_L3):
                    self._check_arp_voodoo_plug(net_info,
                                                net_info.gateway_info)

    @log_helpers.log_method_call
    def create_bgpvpn(self, context, bgpvpn):
        self.update_bgpvpn(context, bgpvpn)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgp-agent')
    def update_bgpvpn(self, context, bgpvpn):
        net_id = bgpvpn.pop('network_id')

        net_info = self.bagpipe_bgp_agent.networks_info.get(net_id)

        if not net_info:
            return

        new_gw_info = b_const.GatewayInfo(
            bgpvpn.pop('gateway_mac', None),
            net_info.gateway_info.ip
        )

        if has_attachement(bgpvpn, b_const.BGPVPN_L3):
            self._check_arp_voodoo_plug(net_info, new_gw_info)

        net_info.set_gateway_info(new_gw_info)

        net_info.add_service_info(b_const.BGPVPN_SERVICE, bgpvpn)

        for port_info in net_info.ports:
            self.bagpipe_bgp_agent._do_port_plug(port_info.id)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgp-agent')
    def delete_bgpvpn(self, context, bgpvpn):
        net_id = bgpvpn.pop('network_id')

        net_info = self.bagpipe_bgp_agent.networks_info.get(net_id)

        if not net_info:
            return

        # Check if remaining BGPVPN informations, otherwise unplug
        # port from bagpipe-bgp
        last_bgpvpn, updated_info = (
            self._is_last_bgpvpn_info(net_info, bgpvpn)
        )

        if self.bagpipe_bgp_agent.agent_type == n_const.AGENT_TYPE_OVS:
            if (last_bgpvpn or
                    not has_attachement(updated_info, b_const.BGPVPN_L3)):
                self._check_arp_voodoo_unplug(net_id)

        if last_bgpvpn:
            if len(net_info.service_infos) == 1:
                for port_info in net_info.ports:
                    self.bagpipe_bgp_agent._do_port_unplug(port_info.id)

                del(net_info.service_infos[b_const.BGPVPN_SERVICE])
            else:
                del(net_info.service_infos[b_const.BGPVPN_SERVICE])

                for port_info in net_info.ports:
                    self.bagpipe_bgp_agent._do_port_plug(port_info.id)
        else:
            net_info.service_infos[b_const.BGPVPN_SERVICE] = updated_info

            for port_info in net_info.ports:
                self.bagpipe_bgp_agent._do_port_plug(port_info.id)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgp-agent')
    def bgpvpn_port_attach(self, context, port_bgpvpn_info):
        port_id = port_bgpvpn_info.pop('id')
        net_id = port_bgpvpn_info.pop('network_id')

        net_info, port_info = (
            self.bagpipe_bgp_agent._get_network_port_infos(net_id, port_id)
        )

        # Set IP and MAC adresses in PortInfo
        ip_address = port_bgpvpn_info.pop('ip_address')
        mac_address = port_bgpvpn_info.pop('mac_address')
        port_info.set_ip_mac_infos(ip_address, mac_address)

        # Set gateway IP and MAC (if defined) addresses in NetworkInfo
        gateway_info = b_const.GatewayInfo(port_bgpvpn_info.pop('gateway_mac',
                                                                None),
                                           port_bgpvpn_info.pop('gateway_ip'))

        if has_attachement(port_bgpvpn_info, b_const.BGPVPN_L3):
            self._check_arp_voodoo_plug(net_info, gateway_info)

        net_info.set_gateway_info(gateway_info)

        if self.bagpipe_bgp_agent.agent_type == n_const.AGENT_TYPE_OVS:
            vlan = self.vlan_manager.get(net_id).vlan
            port_info.set_local_port('%s:%s' % (b_const.LINUXIF_PREFIX, vlan))
        else:
            port_info.set_local_port(
                LinuxBridgeManager.get_tap_device_name(port_id)
            )

        net_info.add_service_info(b_const.BGPVPN_SERVICE, port_bgpvpn_info)

        self.bagpipe_bgp_agent._do_port_plug(port_id)

    @log_helpers.log_method_call
    @lockutils.synchronized('bagpipe-bgp-agent')
    def bgpvpn_port_detach(self, context, port_bgpvpn_info):
        port_id = port_bgpvpn_info['id']
        net_id = port_bgpvpn_info['network_id']

        if self.bagpipe_bgp_agent.ports_info.get(port_id):
            try:
                self.bagpipe_bgp_agent._do_port_unplug(port_id)
                if self.bagpipe_bgp_agent.agent_type == n_const.AGENT_TYPE_OVS:
                    net_info = self.bagpipe_bgp_agent.networks_info.get(net_id)
                    bgpvpn_info = net_info.service_infos.get(
                        b_const.BGPVPN_SERVICE)

                    if has_attachement(bgpvpn_info, b_const.BGPVPN_L3):
                        self._check_arp_voodoo_unplug(net_id)
            except BaGPipeBGPException as e:
                LOG.error("Can't detach BGPVPN port from bagpipe-bgp %s",
                          str(e))
            finally:
                self.bagpipe_bgp_agent._remove_network_port_infos(net_id,
                                                                  port_id)
        else:
            LOG.warning('bagpipe-bgp agent inconsistent for BGPVPN or updated '
                        'with another detach')

    @log_helpers.log_method_call
    def handle_port(self, context, data):
        pass

    @log_helpers.log_method_call
    def delete_port(self, context, data):
        pass
