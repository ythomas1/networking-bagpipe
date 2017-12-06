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

import six
import sqlalchemy as sa
from sqlalchemy import orm

from neutron.db import common_db_mixin

from neutron_lib import context as n_context
from neutron_lib.db import model_base

from oslo_log import helpers as log_helpers

from networking_sfc.db.flowclassifier_db import FlowClassifier

AS_NUMBER = 64512


class BaGPipePpgRTAssoc(model_base.BASEV2, model_base.HasId):
    '''
    Port Pair Group route target network number associations
    '''
    __tablename__ = 'sfc_bagpipe_ppg_rtnn_associations'
    ppg_id = sa.Column(sa.String(36), primary_key=True)
    rtnn = sa.Column(sa.Integer, unique=True, nullable=False)
    is_redirect = sa.Column(sa.Boolean(), nullable=False)
    reverse = sa.Column(sa.Boolean(), nullable=False)

    def __init__(self, ppg_id, rtnn, is_redirect, reverse):
        self.ppg_id = ppg_id
        self.rtnn = rtnn
        self.is_redirect = is_redirect
        self.reverse = reverse


def singleton(class_):
    instances = {}

    def getinstance(*args, **kwargs):
        if class_ not in instances:
            instances[class_] = class_(*args, **kwargs)
        return instances[class_]
    return getinstance


@singleton
class RTAllocator(object):
    def __init__(self):
        # Get the initial range from conf file.
        conf_obj = {'rtnn': [5000, 5999]}
        self.conf_obj = conf_obj
        self.session = n_context.get_admin_context().session

    def _get_rt_from_rtnn(self, rtnn):
        return ':'.join([str(AS_NUMBER), str(rtnn)])

    @log_helpers.log_method_call
    def allocate_rt(self, ppg_id, is_redirect=False, reverse=False):
        query = self.session.query(
            BaGPipePpgRTAssoc).order_by(
            BaGPipePpgRTAssoc.rtnn)

        allocated_rtnns = {obj.rtnn for obj in query.all()}

        # Find first one available in range
        start, end = self.conf_obj['rtnn'][0], self.conf_obj['rtnn'][1] + 1
        for rtnn in six.moves.range(start, end):
            if rtnn not in allocated_rtnns:
                with self.session.begin(subtransactions=True):
                    ppg_rtnn = BaGPipePpgRTAssoc(
                        ppg_id, rtnn, is_redirect, reverse)
                    self.session.add(ppg_rtnn)
                return self._get_rt_from_rtnn(rtnn)
        else:
            return None

    @log_helpers.log_method_call
    def get_rts_by_ppg(self, ppg_id):

        ppg_rts = self.session.query(
            BaGPipePpgRTAssoc).filter_by(ppg_id=ppg_id).all()
        if not ppg_rts:
            return None

        return [ppg_rt.rtnn for ppg_rt in ppg_rts]

    @log_helpers.log_method_call
    def get_redirect_rt_by_ppg(self, ppg_id, reverse=False):
        ppg_redirect_rt = self.session.query(
            BaGPipePpgRTAssoc).filter_by(ppg_id=ppg_id,
                                         is_redirect=True,
                                         reverse=reverse).one()

        return self._get_rt_from_rtnn(ppg_redirect_rt.rtnn)

    @log_helpers.log_method_call
    def release_rt(self, rtnn):
        with self.session.begin(subtransactions=True):
            ppg_rtnn = self.session.query(
                BaGPipePpgRTAssoc).filter_by(rtnn=rtnn).first()

            if ppg_rtnn:
                self.session.delete(ppg_rtnn)


class BaGPipeChainHop(model_base.BASEV2, model_base.HasId,
                      model_base.HasProject):
    __tablename__ = 'sfc_bagpipe_chain_hops'
    ingress_ppg = sa.Column(sa.String(36), nullable=True)
    egress_ppg = sa.Column(sa.String(36), nullable=True)
    ingress_network = sa.Column(sa.String(36), nullable=True)
    egress_network = sa.Column(sa.String(36), nullable=True)
    rts = sa.Column(sa.String(255))
    ingress_gw = sa.Column(sa.String(64))
    egress_gw = sa.Column(sa.String(64))
    readv_from_rts = sa.Column(sa.String(255))
    readv_to_rts = sa.Column(sa.String(255))
    attract_to_rts = sa.Column(sa.String(255))
    redirect_rts = sa.Column(sa.String(255))
    classifiers = sa.Column(sa.String(255))
    reverse_hop = sa.Column(sa.Boolean(), nullable=False)
    portchain_id = sa.Column(sa.String(36),
                             sa.ForeignKey('sfc_port_chains.id',
                                           ondelete='CASCADE'),
                             nullable=False,
                             primary_key=True)


class BaGPipeSfcDriverDB(common_db_mixin.CommonDbMixin):

    def initialize(self):
        self.admin_context = n_context.get_admin_context()
