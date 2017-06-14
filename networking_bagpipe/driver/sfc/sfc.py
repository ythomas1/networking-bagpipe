# Copyright (c) 2017 Orange.
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

from netaddr.core import AddrFormatError
from netaddr.ip import IPNetwork

from neutron_lib.api.definitions import bgpvpn as bgpvpn_def
from neutron_lib.api.definitions import provider_net as pnet
from neutron_lib import context as n_context
from neutron_lib.plugins import directory

from oslo_log import helpers as log_helpers
from oslo_log import log as logging
from oslo_serialization import jsonutils
from oslo_utils import uuidutils

from neutron.api.rpc.callbacks import events as rpc_events
from neutron.api.rpc.callbacks.producer import registry as rpc_registry
from neutron.api.rpc.handlers import resources_rpc

from neutron.db import models_v2

from networking_bagpipe.db.sfc import sfc_db
from networking_bagpipe.driver.sfc.common import constants
from networking_bagpipe.objects.sfc import chain_hop as sfc_object

from networking_sfc.extensions import flowclassifier
from networking_sfc.services.sfc.common import exceptions as exc
from networking_sfc.services.sfc.drivers import base as driver_base

LOG = logging.getLogger(__name__)


@log_helpers.log_method_call
def _get_chain_hops_by_port(resource, port_id, **kwargs):
    context = kwargs.get('context')
    if context is None:
        LOG.warning(
            'Received pull for %(resource)s %(port_id)s without context',
            {'resource': resource, 'port_id': port_id})
        return

    return sfc_object.BaGPipePortHops.get_object(context,
                                                 port_id=port_id)


class BaGPipeSfcDriver(driver_base.SfcDriverBase,
                       sfc_db.BaGPipeSfcDriverDB):
    """BaGPipe Sfc Driver Base Class."""

    def initialize(self):
        super(BaGPipeSfcDriver, self).initialize()
        self.rt_allocator = sfc_db.RTAllocator()
        self._setup_rpc()

    def _setup_rpc(self):
        self._push_rpc = resources_rpc.ResourcesPushRpcApi()

        rpc_registry.provide(_get_chain_hops_by_port,
                             sfc_object.BaGPipePortHops.obj_name())

    def _parse_ipaddress_prefix(self, cidr):
        ip_address = ""
        mask = 0
        try:
            net = IPNetwork(cidr)
            (ip_address, mask) = (str(net.ip), net.prefixlen)
        except AddrFormatError:
            raise exc.SfcDriverError(message=(
                "Malformed IP prefix: %s" % cidr))

        return (ip_address, mask)

    def _get_subnet_by_port(self, port_id):
        core_plugin = directory.get_plugin()
        port = core_plugin.get_port(self.admin_context, port_id)
        subnet = None
        for ip in port['fixed_ips']:
            subnet = core_plugin.get_subnet(self.admin_context,
                                            ip["subnet_id"])
            # currently only support one subnet for a port
            break

        return subnet

    def _get_subnet_by_prefix(self, cidr):
        core_plugin = directory.get_plugin()

        # Parse address/mask
        (ip_address, mask) = self._parse_ipaddress_prefix(cidr)
        if mask == 32:
            port = core_plugin.get_ports(
                self.admin_context,
                filters={'fixed_ips': {'ip_address': [ip_address]}}
            )
            print "_get_subnet_by_prefix %s: %s " % (ip_address, port)
            for fixed_ip in port[0]['fixed_ips']:
                subnet = core_plugin.get_subnet(self.admin_context,
                                                fixed_ip["subnet_id"])
                # currently only support one subnet for a port
                break
        else:
            subnet = core_plugin.get_subnets(self.admin_context,
                                             filters={'cidr': [cidr]})[0]

        return subnet

    def _get_ports_by_network(self, network_id):
        core_plugin = directory.get_plugin()
        ports = core_plugin.get_ports(self.admin_context,
                                      filters={'network_id': [network_id]})

        return [port['id'] for port in ports]

    def _get_subnet_gw_by_port(self, port_id):
        subnet = self._get_subnet_by_port(port_id)

        return subnet['gateway_ip']

    def _get_subnet_gw(self, subnets):
        core_plugin = directory.get_plugin()

        subnet_gw = {}
        for subnet_id in subnets:
            subnet = core_plugin.get_subnet(self.admin_context, subnet_id)
            subnet_gw[subnet_id] = subnet['gateway_ip']
            # currently only support one subnet for a port
            break

        return subnet_gw

    def _get_bgpvpns_by_network(self, context, network_id):
        """
        Retrieve BGP VPNs associated to network depending on BGP VPN service
        plugin activation
        """
        bgpvpns = []
        if not network_id:
            return bgpvpns

        bgpvpn_plugin = (
            directory.get_plugin(bgpvpn_def.LABEL)
        )
        if not bgpvpn_plugin:
            LOG.warning("BGPVPN service not found")
            return bgpvpns

        tenant_bgpvpns = bgpvpn_plugin.get_bgpvpns(
            self.admin_context,
            filters=dict(tenant_id=[context.current['tenant_id']]))

        if tenant_bgpvpns:
            for bgpvpn in tenant_bgpvpns:
                if network_id in bgpvpn['networks']:
                    bgpvpns.append(bgpvpn)

        LOG.debug("BGPVPNs associated to network %s: %s" %
                  (network_id, bgpvpns))

        return bgpvpns

    def _get_network_rt(self, network_id):
        core_plugin = directory.get_plugin()
        network = core_plugin.get_network(self.admin_context, network_id)

        return ':'.join([str(sfc_db.AS_NUMBER),
                         str(network[pnet.SEGMENTATION_ID])])

    def _get_bgpvpn_rts(self, bgpvpn_list):
        import_rt = []
        export_rt = []
        for bgpvpn in bgpvpn_list:
            if 'route_targets' in bgpvpn:
                import_rt += bgpvpn['route_targets']
                export_rt += bgpvpn['route_targets']
            if 'import_targets' in bgpvpn:
                import_rt += bgpvpn['import_targets']
            if 'export_targets' in bgpvpn:
                import_rt += bgpvpn['export_targets']

        return (import_rt, export_rt)

    def _get_fcs_by_ids(self, fc_ids):
        flow_classifiers = []
        if not fc_ids:
            return flow_classifiers

        # Get the portchain flow classifiers
        fc_plugin = (
            directory.get_plugin(flowclassifier.FLOW_CLASSIFIER_EXT)
        )
        if not fc_plugin:
            LOG.warning("Not found the flow classifier service plugin")
            return flow_classifiers

        for fc_id in fc_ids:
            fc = fc_plugin.get_flow_classifier(self.admin_context, fc_id)
            flow_classifiers.append(fc)

        return flow_classifiers

    def _build_bagpipe_classifier_from_fc(self, fc, reverse, source_bgpvpns):
        classifier = {}
        classifier['protocol'] = fc['protocol']

        for side in (constants.SOURCE, constants.DESTINATION):
            flow_side = constants.REVERSE_FLOW_SIDE[side] if reverse else side

            port_range_min = fc[side + '_port_range_min']
            port_range_max = fc[side + '_port_range_max']
            if port_range_min is not None:
                if (port_range_max is not None and
                        port_range_min != port_range_max):
                    classifier[flow_side + 'Port'] = ':'.join(
                        [port_range_min, port_range_max]
                    )
                else:
                    classifier[flow_side + 'Port'] = port_range_min

            if flow_side == constants.SOURCE and source_bgpvpns:
                continue

            logical_port = fc['logical_' + side + '_port']
            if logical_port is not None:
                port = self._get_by_id(
                    self.admin_context, models_v2.Port, logical_port
                )
                if fc[side + '_ip_prefix'] is None:
                    ips = port['fixed_ips']
                    # For now, only handle when the port has a single IP
                    if len(ips) == 1:
                        classifier[flow_side + 'Prefix'] = (
                            ips[0]['ip_address'] + '/32'
                        )
                else:
                    classifier[flow_side + 'Prefix'] = (
                        fc[side + '_ip_prefix']
                    )
            else:
                if fc[side + '_ip_prefix'] is not None:
                    classifier[flow_side + 'Prefix'] = (
                        fc[side + '_ip_prefix']
                    )

        return classifier

    def _get_ports_by_portpairs_side(self, pps, side):
        # Sort port pairs list ordering ports for load balancing
        sorted_pps = sorted(pps, key=lambda k: k['id'])

        ports = list()
        for pp in sorted_pps:
            ports.append(pp[side])

        return ports

    def _get_rts_ports_from_fc_side(self, fc, side):
        if fc.get('logical_' + side + '_port'):
            fp_net = self._get_network_by_port(
                fc['logical_' + side + '_port'])
            fp_ports = [fc['logical_' + side + '_port']]
        else:
            fp_net = self._get_network_by_prefix(
                fc[side + '_ip_prefix'])
            fp_ports = self._get_ports_by_network(fp_net)

        fp_rts = self._get_network_rt(fp_net)

        return fp_rts, fp_ports

    def create_bagpipe_chain_hop_detail(self, context, project_id, chain_id,
                                        rts, ingress_gw, egress_gw,
                                        reverse_hop,
                                        **optional):
        """
        Create BaGPipe Chain Hop detail as follow:
        {
            portchain_id: <PORT_CHAIN_UUID>,
            rts: [<ASN:NN>, ...],
            ingress_gw: <GATEWAY_IP>,
            egress_gw: <GATEWAY_IP>,
            reverse_hop: <BOOLEAN>,

            # Optional parameters
            ingress_ppg: <PORT_PAIR_GROUP_UUID>,
            egress_ppg: <PORT_PAIR_GROUP_UUID>,
            ingress_network: <NETWORK_UUID>,
            egress_network: <NETWORK_UUID>,
            readv_from_rts: [<ASN:NN>],
            readv_to_rts: [<ASN:NN>],
            attract_to_rts: [<ASN:NN>],
            redirect_rts: [<ASN:NN>],
            classifiers: [<FLOW_CLASSIFIER_UUID>, ...]
        }
        """
        hop_detail = {'id': uuidutils.generate_uuid(),
                      'project_id': project_id,
                      'portchain_id': chain_id,
                      'rts': rts,
                      'ingress_gw': ingress_gw,
                      'egress_gw': egress_gw,
                      'reverse_hop': reverse_hop}

        for key in ('ingress_ppg', 'egress_ppg',
                    'ingress_network', 'egress_network',
                    'redirect_rts', 'readv_from_rts', 'readv_to_rts',
                    'attract_to_rts', 'classifiers'):
            if key in optional:
                hop_detail.update({key: optional[key]})

        hop_detail_obj = sfc_object.BaGPipeChainHop(context,
                                                    **hop_detail)
        hop_detail_obj.create()

        return hop_detail_obj

    @log_helpers.log_method_call
    def _create_portchain_hop_details(self, context, port_chain,
                                      reverse=False):
        project_id = port_chain['tenant_id']
        hop_details = []
        port_pair_groups = port_chain['port_pair_groups']

        fcs = self._get_fcs_by_ids(port_chain['flow_classifiers'])

        classifiers = []
        src_rts = []
        src_ports = []
        dest_rts = []
        dest_ports = []
        for fc in fcs:
            if fc.get('logical_source_port'):
                src_subnet = self._get_subnet_by_port(
                    fc['logical_source_port'])
                src_ports.append(fc['logical_source_port'])
            else:
                src_subnet = self._get_subnet_by_prefix(
                    fc['source_ip_prefix'])
                src_ports.extend(
                    self._get_ports_by_network(src_subnet['network_id'])
                )

            # Check if network is associated to BGPVPNs
            src_bgpvpns = self._get_bgpvpns_by_network(context,
                                                       src_subnet['network_id'])
            if src_bgpvpns:
                src_rts.extend(self._get_bgpvpn_rts(src_bgpvpns)[0])
            else:
                src_rts.append(self._get_network_rt(src_subnet['network_id']))

            if fc.get('logical_destination_port'):
                dest_subnet = self._get_subnet_by_port(
                    fc['logical_destination_port'])
                dest_ports.append(fc['logical_destination_port'])
            else:
                dest_subnet = self._get_subnet_by_prefix(
                    fc['destination_ip_prefix'])
                dest_ports.extend(
                    self._get_ports_by_network(dest_subnet['network_id'])
                )

            # Check if network is associated to BGPVPNs
            dest_bgpvpns = self._get_bgpvpns_by_network(context,
                                                        dest_subnet['network_id'])
            if dest_bgpvpns:
                dest_rts.extend(self._get_bgpvpn_rts(dest_bgpvpns)[0])
            else:
                dest_rts.append(self._get_network_rt(dest_subnet['network_id']))

            ingress_bgpvpns = dest_bgpvpns if reverse else src_bgpvpns
            egress_bgpvpns = src_bgpvpns if reverse else dest_bgpvpns

            classifiers.append(
                self._build_bagpipe_classifier_from_fc(fc, reverse,
                                                       ingress_bgpvpns)
            )

            # bagpipe-bgp only support one flow classifier for the moment
            break

        # Iterate in reversed order to propagate default route from last
        # ingress VRF
        reversed_ppg = port_pair_groups[::-1] if reverse else port_pair_groups
        reversed_ingress = (constants.REVERSE_PORT_SIDE[constants.INGRESS]
                            if reverse else constants.INGRESS)
        reversed_egress = (constants.REVERSE_PORT_SIDE[constants.EGRESS]
                           if reverse else constants.EGRESS)
        for position in range(len(reversed_ppg)):
            # Last Hop:
            # - Between last SF egress and Destination ports
            # - Between first SF ingress and Source ports if symmetric reverse
            #   traffic
            if position == len(reversed_ppg)-1:
                last_ppg = context._plugin._get_port_pair_group(
                    context._plugin_context,
                    reversed_ppg[position])

                last_eports = self._get_ports_by_portpairs_side(
                    last_ppg['port_pairs'], reversed_egress)

                last_subnet = self._get_subnet_by_port(last_eports[0])

                hop_details.append(
                    self.create_bagpipe_chain_hop_detail(
                        context._plugin_context,
                        project_id,
                        port_chain['id'],
                        src_rts if reverse else dest_rts,
                        last_subnet['gateway_ip'],
                        (src_subnet['gateway_ip'] if reverse
                            else dest_subnet['gateway_ip']),
                        reverse,
                        ingress_ppg=last_ppg['id'],
                        egress_network=(src_subnet['network_id'] if reverse
                                        else dest_subnet['network_id']))
                )

            # Intermediate Hop: Between one SF ingress and previous (reversed
            # order) SF egress ports
            if (position < len(reversed_ppg)-1 and
                    len(reversed_ppg) > 1):
                current_ppg = context._plugin._get_port_pair_group(
                    context._plugin_context,
                    reversed_ppg[position])

                current_eports = self._get_ports_by_portpairs_side(
                    current_ppg['port_pairs'], reversed_egress)

                current_subnet = self._get_subnet_by_port(current_eports[0])

                prev_ppg = context._plugin._get_port_pair_group(
                    context._plugin_context,
                    reversed_ppg[position+1])

                prev_iports = self._get_ports_by_portpairs_side(
                    prev_ppg['port_pairs'], reversed_ingress)

                prev_subnet = self._get_subnet_by_port(prev_iports[0])

                prev_ppg_rt = self.rt_allocator.allocate_rt(
                    reversed_ppg[position+1],
                    reverse=reverse)

                prev_redirect_rt = self.rt_allocator.allocate_rt(
                    reversed_ppg[position+1],
                    is_redirect=True,
                    reverse=reverse)

                prev_readv_from_rts = ((src_rts if reverse else dest_rts)
                                       if egress_bgpvpns else None)
                prev_readv_to_rts = ([prev_redirect_rt] if egress_bgpvpns
                                     else None)
                prev_attract_to_rts = ([prev_redirect_rt] if not egress_bgpvpns
                                       else None)

#                if position+1 == len(reversed_ppg)-1:
                hop_details.append(
                    self.create_bagpipe_chain_hop_detail(
                        context._plugin_context,
                        project_id,
                        port_chain['id'],
                        [prev_ppg_rt],
                        current_subnet['gateway_ip'],
                        prev_subnet['gateway_ip'],
                        reverse,
                        ingress_ppg=current_ppg['id'],
                        egress_ppg=prev_ppg['id'],
                        readv_from_rts=prev_readv_from_rts,
                        readv_to_rts=prev_readv_to_rts,
                        attract_to_rts=prev_attract_to_rts,
                        redirect_rts=[prev_ppg_rt],
                        classifiers=jsonutils.dumps(classifiers))
                )
#                 else:
#                     from_redirect_rt = self.rt_allocator.get_redirect_rt_by_ppg(
#                         reversed_ppg[position+2])
# 
#                     hop_details.append(
#                         self.create_bagpipe_chain_hop_detail(
#                             context._plugin_context,
#                             project_id,
#                             port_chain['id'],
#                             prev_ppg_rt,
#                             current_subnet['gateway_ip'],
#                             prev_subnet['gateway_ip'],
#                             current_ppg['id'],
#                             prev_ppg['id'],
#                             current_eports,
#                             prev_iports,
#                             readv_from_rts=[from_redirect_rt],
#                             readv_to_rts=[prev_redirect_rt],
#                             redirect_rts=[prev_ppg_rt],
#                             classifiers=classifiers)
#                     )

            # First Hop:
            # - Between Source and first SF ingress ports
            # - Between Destination and last SF egress ports if symmetric
            #   reverse traffic
            if position == 0:
                first_ppg = context._plugin._get_port_pair_group(
                    context._plugin_context,
                    reversed_ppg[position])

                first_iports = self._get_ports_by_portpairs_side(
                    first_ppg['port_pairs'], reversed_ingress)

                first_subnet = self._get_subnet_by_port(first_iports[0])

                first_ppg_rt = self.rt_allocator.allocate_rt(
                    reversed_ppg[position],
                    reverse=reverse)

                first_redirect_rt = self.rt_allocator.allocate_rt(
                    reversed_ppg[position],
                    is_redirect=True,
                    reverse=reverse)

#                 from_redirect_rt = self.rt_allocator.get_redirect_rt_by_ppg(
#                     reversed_ppg[position+1])

                first_readv_from_rts = ((src_rts if reverse else dest_rts)
                                        if egress_bgpvpns else None)
                first_readv_to_rts = ([first_redirect_rt] if egress_bgpvpns
                                      else None)
                first_attract_to_rts = ([first_redirect_rt]
                                        if not egress_bgpvpns else None)
                first_rts = ((dest_rts if reverse else src_rts)
                             if ingress_bgpvpns else [first_ppg_rt])

                hop_details.append(
                    self.create_bagpipe_chain_hop_detail(
                        context._plugin_context,
                        project_id,
                        port_chain['id'],
                        first_rts,
                        (dest_subnet['gateway_ip'] if reverse
                            else src_subnet['gateway_ip']),
                        first_subnet['gateway_ip'],
                        reverse,
                        ingress_network=(dest_subnet['network_id'] if reverse
                                         else src_subnet['network_id']),
                        egress_ppg=first_ppg['id'],
                        readv_from_rts=first_readv_from_rts,
                        readv_to_rts=first_readv_to_rts,
                        attract_to_rts=first_attract_to_rts,
                        redirect_rts=first_rts,
                        classifiers=jsonutils.dumps(classifiers))
                )

        LOG.debug("BaGPipe SFC driver Chain Hop details: %s" %
                  hop_details)

        return hop_details

    @log_helpers.log_method_call
    def create_port_chain(self, context):
        port_chain = context.current
        symmetric = port_chain['chain_parameters'].get('symmetric')

        hop_objs = self._create_portchain_hop_details(context,
                                                      port_chain)

        if symmetric:
            hop_objs.extend(self._create_portchain_hop_details(context,
                                                               port_chain,
                                                               reverse=True))

        self._push_rpc.push(context._plugin_context, hop_objs,
                            rpc_events.CREATED)

    @log_helpers.log_method_call
    def delete_port_chain(self, context):
        port_chain = context.current

        # Release PPG route targets
        for ppg_id in port_chain['port_pair_groups']:
            ppg_rtnns = self.rt_allocator.get_rts_by_ppg(ppg_id)

            if ppg_rtnns:
                for rtnn in ppg_rtnns:
                    self.rt_allocator.release_rt(rtnn)

        hop_objs = sfc_object.BaGPipeChainHop.get_objects(
            context._plugin_context,
            portchain_id=port_chain['id'])

        for hop_obj in hop_objs:
            hop_obj.delete()

        self._push_rpc.push(context._plugin_context, hop_objs,
                            rpc_events.DELETED)

    @log_helpers.log_method_call
    def update_port_chain(self, context):
        pass

    @log_helpers.log_method_call
    def create_port_pair_group_precommit(self, context):
        port_pair_group = context._plugin._get_port_pair_group(
            context._plugin_context,
            context.current['id'])
        port_pairs = port_pair_group['port_pairs']

        for side in [constants.INGRESS, constants.EGRESS]:
            port_ids = self._get_ports_by_portpairs_side(port_pairs, side)

            network_ids = set()
            for port_id in port_ids:
                subnet = self._get_subnet_by_port(port_id)
                network_ids.add(subnet['network_id'])

            if len(network_ids) > 1:
                raise exc.SfcBadRequest(message=(
                    'PortPairGroup %s %s ports must be on same network'
                    % (port_pair_group['id'], side)))

    @log_helpers.log_method_call
    def create_port_pair_group(self, context):
        pass

    @log_helpers.log_method_call
    def delete_port_pair_group(self, context):
        pass

    @log_helpers.log_method_call
    def update_port_pair_group(self, context):
        current = context.current
        original = context.original

        if set(current['port_pairs']) == set(original['port_pairs']):
            return

        # Update BaGPipe Chain Hop details for each port chain containing
        # this port pair group
        ppg = context._plugin._get_port_pair_group(context._plugin_context,
                                                   current['id'])
        port_chains = [assoc.portchain_id for assoc in
                       ppg.chain_group_associations]

        hop_objs = []
        for chain_id in port_chains:
            for side in [constants.INGRESS, constants.EGRESS]:
                hop_obj = sfc_object.BaGPipeChainHop.get_object(
                    context._plugin_context,
                    **{'portchain_id': chain_id, side + '_ppg': current['id']}
                )

                if not hop_obj:
                    LOG.error('BaGPipe Chain Hop details inconsistent when \
                               updating port pair group %(ppg)s in port chain \
                               %(chain)s for %(side)s side',
                              {'ppg': current['id'], 'chain': chain_id,
                               'side': side})
                    continue

                port_ids = (
                    self._get_ports_by_portpairs_side(current['port_pairs'],
                                                      side)
                )

                hop_obj.update_fields({side + '_ports': port_ids})
                hop_obj.update()

                hop_objs.append(hop_obj)

        if hop_objs:
            self._push_rpc.push(context._plugin_context, hop_objs,
                                rpc_events.UPDATED)

    @log_helpers.log_method_call
    def create_port_pair(self, context):
        pass

    @log_helpers.log_method_call
    def update_port_pair(self, context):
        pass

    @log_helpers.log_method_call
    def delete_port_pair(self, context):
        pass
