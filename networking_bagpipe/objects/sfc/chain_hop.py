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

import netaddr
import sqlalchemy as sa
from sqlalchemy.orm import exc

from oslo_versionedobjects import fields as obj_fields

from neutron.api.rpc.callbacks import resources
from neutron.db import api as db_api
from neutron.db import models_v2
from neutron.objects import base
from neutron.objects import common_types
from neutron.objects.ports import Port

from networking_bagpipe.db.sfc import sfc_db as bagpipe_db

from networking_sfc.db import sfc_db


@base.NeutronObjectRegistry.register
class BaGPipeChainHop(base.NeutronDbObject):
    # Version 1.0: Initial version
    VERSION = '1.0'

    db_model = bagpipe_db.BaGPipeChainHop

    fields = {
        'id': common_types.UUIDField(),
        'project_id': obj_fields.StringField(),
        'rts': obj_fields.ListOfStringsField(),
        'ingress_gw': obj_fields.IPAddressField(),
        'egress_gw': obj_fields.IPAddressField(),
        'ingress_ppg': common_types.UUIDField(nullable=True, default=None),
        'egress_ppg': common_types.UUIDField(nullable=True, default=None),
        'ingress_network': common_types.UUIDField(nullable=True, default=None),
        'egress_network': common_types.UUIDField(nullable=True, default=None),
        'ingress_ports': obj_fields.ListOfStringsField(nullable=True),
        'egress_ports': obj_fields.ListOfStringsField(nullable=True),
        'readv_from_rts': obj_fields.ListOfStringsField(nullable=True),
        'readv_to_rts': obj_fields.ListOfStringsField(nullable=True),
        'attract_to_rts': obj_fields.ListOfStringsField(nullable=True),
        'redirect_rts': obj_fields.ListOfStringsField(nullable=True),
        'classifiers': obj_fields.StringField(nullable=True),
        'reverse_hop': obj_fields.BooleanField(default=False),

        'portchain_id': common_types.UUIDField(),
    }

    fields_no_update = ['id', 'project_id']

    primary_keys = ['portchain_id']

    foreign_keys = {
        'PortChain': {'portchain_id': 'id'}
    }

    synthetic_fields = ['ingress_ports',
                        'egress_ports']

    def from_db_object(self, db_obj):
        super(BaGPipeChainHop, self).from_db_object(db_obj)

        self._load_ingress_ports(db_obj)
        self._load_egress_ports(db_obj)

    def _load_ingress_ports(self, db_obj=None):
        ingress_ports = []
        if self.ingress_ppg:
            query = self.obj_context.session.query(sfc_db.PortPair)
            query = query.filter(
                sfc_db.PortPair.portpairgroup_id == self.ingress_ppg)
            ingress_ports = [pp_obj.egress for pp_obj in query.all()]
        elif self.egress_network:
            port_objs = Port.get_objects(self.obj_context,
                                         network_id=self.ingress_network)

            ingress_ports = [port_obj.id for port_obj in port_objs]

        setattr(self, 'ingress_ports', ingress_ports)
        self.obj_reset_changes(['ingress_ports'])

    def _load_egress_ports(self, db_obj=None):
        egress_ports = []
        if self.egress_ppg:
            query = self.obj_context.session.query(sfc_db.PortPair)
            query = query.filter(
                sfc_db.PortPair.portpairgroup_id == self.egress_ppg)
            egress_ports = [pp_obj.ingress for pp_obj in query.all()]
        elif self.egress_network:
            port_objs = Port.get_objects(self.obj_context,
                                         network_id=self.egress_network)

            egress_ports = [port_obj.id for port_obj in port_objs]

        setattr(self, 'egress_ports', egress_ports)
        self.obj_reset_changes(['egress_ports'])

    @staticmethod
    def _is_port_in_pp(context, port_id):
        try:
            query = context.session.query(sfc_db.PortPair)
            query = query.filter(
                sa.or_(sfc_db.PortPair.ingress == port_id,
                       sfc_db.PortPair.egress == port_id))
            result = query.one()
            return True
        except exc.NoResultFound:
            return False

    @staticmethod
    def _get_ingress_hops_for_ppg(context, port_id):
        query = context.session.query(bagpipe_db.BaGPipeChainHop)
        query = query.join(
            sfc_db.PortPairGroup,
            sfc_db.PortPairGroup.id == bagpipe_db.BaGPipeChainHop.ingress_ppg)
        query = query.join(
            sfc_db.PortPair,
            sfc_db.PortPair.portpairgroup_id == sfc_db.PortPairGroup.id)
        query = query.filter(
            sa.or_(
                sa.and_(sfc_db.PortPair.egress == port_id,
                        bagpipe_db.BaGPipeChainHop.reverse_hop == False),
                sa.and_(sfc_db.PortPair.ingress == port_id,
                        bagpipe_db.BaGPipeChainHop.reverse_hop == True)))

        return query.all()

    @staticmethod
    def _get_ingress_hops_for_network(context, port_id):
        query = context.session.query(bagpipe_db.BaGPipeChainHop)
        query = query.join(models_v2.Port, models_v2.Port.id == port_id)
        query = query.filter(
            sa.or_(
                sa.and_(models_v2.Port.network_id ==
                        bagpipe_db.BaGPipeChainHop.ingress_network,
                        bagpipe_db.BaGPipeChainHop.reverse_hop == False),
                sa.and_(models_v2.Port.network_id ==
                        bagpipe_db.BaGPipeChainHop.egress_network,
                        bagpipe_db.BaGPipeChainHop.reverse_hop == True)))

        return query.all()

    @classmethod
    def get_ingress_hops_for_port(cls, context, port_id):
        if cls._is_port_in_pp(context, port_id):
            db_objs = cls._get_ingress_hops_for_ppg(context, port_id)
        else:
            db_objs = cls._get_ingress_hops_for_network(context, port_id)

        return [cls._load_object(context, db_obj) for db_obj in db_objs]

    @staticmethod
    def _get_egress_hops_for_ppg(context, port_id):
        query = context.session.query(bagpipe_db.BaGPipeChainHop)
        query = query.join(
            sfc_db.PortPairGroup,
            sfc_db.PortPairGroup.id == bagpipe_db.BaGPipeChainHop.egress_ppg)
        query = query.join(
            sfc_db.PortPair,
            sfc_db.PortPair.portpairgroup_id == sfc_db.PortPairGroup.id)
        query = query.filter(
            sa.or_(
                sa.and_(sfc_db.PortPair.ingress == port_id,
                        bagpipe_db.BaGPipeChainHop.reverse_hop == False),
                sa.and_(sfc_db.PortPair.egress == port_id,
                        bagpipe_db.BaGPipeChainHop.reverse_hop == True)))

        return query.all()

    @staticmethod
    def _get_egress_hops_for_network(context, port_id):
        query = context.session.query(bagpipe_db.BaGPipeChainHop)
        query = query.join(models_v2.Port, models_v2.Port == port_id)
        query = query.filter(
            sa.or_(
                sa.and_(models_v2.Port.network_id ==
                        bagpipe_db.BaGPipeChainHop.egress_network,
                        bagpipe_db.BaGPipeChainHop.reverse_hop == False),
                sa.and_(models_v2.Port.network_id ==
                        bagpipe_db.BaGPipeChainHop.ingress_network,
                        bagpipe_db.BaGPipeChainHop.reverse_hop == True)))

        return query.all()

    @classmethod
    def get_egress_hops_for_port(cls, context, port_id):
        if cls._is_port_in_pp(context, port_id):
            db_objs = cls._get_egress_hops_for_ppg(context, port_id)
        else:
            db_objs = cls._get_egress_hops_for_network(context, port_id)

        return [cls._load_object(context, db_obj) for db_obj in db_objs]

    @classmethod
    def modify_fields_from_db(cls, db_obj):
        fields = super(BaGPipeChainHop, cls).modify_fields_from_db(db_obj)

        for field in ['rts',
                      'readv_from_rts',
                      'readv_to_rts',
                      'attract_to_rts',
                      'redirect_rts']:
            if fields.get(field) is not None:
                fields[field] = fields[field].split(',')
            else:
                fields[field] = []

        for gw_ip in ['ingress_gw', 'egress_gw']:
            if gw_ip in fields and fields[gw_ip] is not None:
                fields[gw_ip] = netaddr.IPAddress(fields[gw_ip])

        return fields

    @classmethod
    def modify_fields_to_db(cls, fields):
        result = super(BaGPipeChainHop, cls).modify_fields_to_db(fields)

        for field in ['rts',
                      'readv_from_rts',
                      'readv_to_rts',
                      'attract_to_rts',
                      'redirect_rts']:
            if field in result:
                result[field] = ','.join(result[field])

        for gw_ip in ['ingress_gw', 'egress_gw']:
            if gw_ip in result and result[gw_ip] is not None:
                result[gw_ip] = cls.filter_to_str(result[gw_ip])

        return result


@base.NeutronObjectRegistry.register
class BaGPipePortHops(base.NeutronObject):
    # Version 1.0: Initial version
    VERSION = '1.0'

    fields = {
        'port_id': common_types.UUIDField(),
        'ingress_hops': obj_fields.ListOfObjectsField(
            'BaGPipeChainHop',
            nullable=True),
        'egress_hops': obj_fields.ListOfObjectsField(
            'BaGPipeChainHop',
            nullable=True),
        'service_function_parameters': obj_fields.DictOfStringsField(
            nullable=True)
    }

    synthetic_fields = {'ingress_hops',
                        'egress_hops'}

    def __init__(self, context=None, **kwargs):
        super(BaGPipePortHops, self).__init__(context, **kwargs)
        self.add_extra_filter_name('port_id')

    def create(self):
        self.obj_load_attr('ingress_hops')
        self.obj_load_attr('egress_hops')

    def update(self):
        if 'ingress_hops' in self.obj_what_changed():
            self.obj_load_attr('ingress_hops')
        if 'egress_hops' in self.obj_what_changed():
            self.obj_load_attr('egress_hops')

    def _load_ingress_hops(self, db_obj=None):
        ingress_hops = (
            BaGPipeChainHop.get_ingress_hops_for_port(self.obj_context,
                                                      self.port_id)
        )
        setattr(self, 'ingress_hops', ingress_hops)
        self.obj_reset_changes(['ingress_hops'])

    def _load_egress_hops(self, db_obj=None):
        egress_hops = (
            BaGPipeChainHop.get_egress_hops_for_port(self.obj_context,
                                                     self.port_id)
        )
        setattr(self, 'egress_hops', egress_hops)
        self.obj_reset_changes(['egress_hops'])

    @classmethod
    def get_object(cls, context, **kwargs):
        port_id = kwargs['port_id']
        ingress_hops = BaGPipeChainHop.get_ingress_hops_for_port(context,
                                                                 port_id)
        egress_hops = BaGPipeChainHop.get_egress_hops_for_port(context,
                                                               port_id)
        return BaGPipePortHops(port_id=port_id,
                               ingress_hops=ingress_hops,
                               egress_hops=egress_hops)


resources.register_resource_class(BaGPipeChainHop)
resources.register_resource_class(BaGPipePortHops)