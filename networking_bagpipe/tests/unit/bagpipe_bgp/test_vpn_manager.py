# vim: tabstop=4 shiftwidth=4 softtabstop=4
# encoding: utf-8

# Copyright 2014 Orange
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import mock

from networking_bagpipe.bagpipe_bgp import constants as consts

from networking_bagpipe.bagpipe_bgp.common import utils
from networking_bagpipe.bagpipe_bgp.vpn import manager
from networking_bagpipe.tests.unit.bagpipe_bgp import base as t


REDIRECTED_INSTANCE_ID1 = 'redirected-id1'
REDIRECTED_INSTANCE_ID2 = 'redirected-id2'

MAC = "00:00:de:ad:be:ef"
IP = "10.0.0.1/32"
BRIDGE_NAME = "br-test"
LOCAL_PORT = {'linuxif': 'tap1'}
VPN_EXT_ID = 1
GW_IP = "10.0.0.1"
GW_MASK = 24
VNID = 255


class TestVPNManager(t.TestCase):

    def setUp(self):
        super(TestVPNManager, self).setUp()
        mock.patch("networking_bagpipe.bagpipe_bgp.vpn.dataplane_drivers."
                   "instantiate_dataplane_drivers",
                   return_value={
                       'evpn': mock.Mock(),
                       'ipvpn': mock.Mock()
                   }).start()
        self.manager = manager.VPNManager()

    def tearDown(self):
        super(TestVPNManager, self).tearDown()
        self.manager.stop()

    def test_redirect_traffic_single_instance(self):
        redirect_instance = self.manager.redirect_traffic_to_vpn(
            REDIRECTED_INSTANCE_ID1, consts.IPVPN, t._rt_to_string(t.RT5)
        )

        # Check some VPN manager and redirect instance lists consistency
        self.assertIn(
            manager.redirect_instance_extid(consts.IPVPN,
                                            t._rt_to_string(t.RT5)),
            self.manager.vpn_instances)
        self.assertIn(REDIRECTED_INSTANCE_ID1,
                      redirect_instance.redirected_instances)

    def test_redirect_traffic_multiple_instance(self):
        redirect_instance_1 = self.manager.redirect_traffic_to_vpn(
            REDIRECTED_INSTANCE_ID1, consts.IPVPN, t._rt_to_string(t.RT5)
        )
        redirect_instance_2 = self.manager.redirect_traffic_to_vpn(
            REDIRECTED_INSTANCE_ID2, consts.IPVPN, t._rt_to_string(t.RT5)
        )

        # Check that same redirect instance is returned
        self.assertEqual(redirect_instance_2, redirect_instance_1)
        # Check some VPN manager and redirect instance lists consistency
        self.assertIn(
            manager.redirect_instance_extid(consts.IPVPN,
                                            t._rt_to_string(t.RT5)),
            self.manager.vpn_instances)
        self.assertIn(REDIRECTED_INSTANCE_ID1,
                      redirect_instance_1.redirected_instances)
        self.assertIn(REDIRECTED_INSTANCE_ID2,
                      redirect_instance_1.redirected_instances)

    def test_stop_redirect_traffic_multiple_instance(self):
        redirect_instance = self.manager.redirect_traffic_to_vpn(
            REDIRECTED_INSTANCE_ID1, consts.IPVPN, t._rt_to_string(t.RT5)
        )
        self.manager.redirect_traffic_to_vpn(
            REDIRECTED_INSTANCE_ID2, consts.IPVPN, t._rt_to_string(t.RT5)
        )

        # Check some VPN manager and redirect instance lists consistency
        self.manager.stop_redirect_to_vpn(REDIRECTED_INSTANCE_ID2,
                                          consts.IPVPN, t._rt_to_string(t.RT5))

        self.assertNotIn(REDIRECTED_INSTANCE_ID2,
                         redirect_instance.redirected_instances)

        self.manager.stop_redirect_to_vpn(REDIRECTED_INSTANCE_ID1,
                                          consts.IPVPN, t._rt_to_string(t.RT5))

        self.assertTrue(not self.manager.vpn_instances)

    def test_plug_vif_to_vpn_with_forced_vni(self):
        with mock.patch.object(self.manager, "_get_vpn_instance") as mock_get_vpn_instance, \
                mock.patch.object(utils, "convert_route_targets"):
            self.manager.plug_vif_to_vpn(vpn_instance_id=VPN_EXT_ID,
                                         vpn_type=consts.EVPN,
                                         import_rt=[t.RT1],
                                         export_rt=[t.RT1],
                                         mac_address=MAC,
                                         ip_address=IP,
                                         gateway_ip=GW_IP,
                                         local_port=LOCAL_PORT,
                                         linuxbr=BRIDGE_NAME,
                                         vni=VNID)
        mock_get_vpn_instance.assert_called_once_with(
            VPN_EXT_ID, consts.EVPN, mock.ANY, mock.ANY, GW_IP, mock.ANY,
            None, None, None, linuxbr=BRIDGE_NAME, vni=VNID)

    def test_plug_vif_to_vpn_without_forced_vni(self):
        with mock.patch.object(self.manager, "_get_vpn_instance") as mock_get_vpn_instance, \
                mock.patch.object(utils, "convert_route_targets"):
            self.manager.plug_vif_to_vpn(vpn_instance_id=VPN_EXT_ID,
                                         vpn_type=consts.EVPN,
                                         import_rt=[t.RT1],
                                         export_rt=[t.RT1],
                                         mac_address=MAC,
                                         ip_address=IP,
                                         gateway_ip=GW_IP,
                                         local_port=LOCAL_PORT,
                                         linuxbr=BRIDGE_NAME)

        mock_get_vpn_instance.assert_called_once_with(
            VPN_EXT_ID, consts.EVPN, mock.ANY, mock.ANY, GW_IP, mock.ANY,
            None, None, None, linuxbr=BRIDGE_NAME)

    def test_get_vpn_instance_with_forced_vni(self):
        vpn_instance = self.manager._get_vpn_instance(VPN_EXT_ID,
                                                      consts.IPVPN,
                                                      [], [],
                                                      GW_IP, GW_MASK,
                                                      None, None,
                                                      vni=VNID)

        self.assertEqual(VNID, vpn_instance.instance_label,
                         "VPN instance label should be forced to VNID")

    def test_get_vpn_instance_without_forced_vni(self):
        vpn_instance = self.manager._get_vpn_instance(VPN_EXT_ID,
                                                      consts.IPVPN,
                                                      [], [],
                                                      GW_IP, GW_MASK,
                                                      None, None)

        self.assertIsNot(0, vpn_instance.instance_label,
                         "VPN instance label should be assigned locally")

    @mock.patch('networking_bagpipe.bagpipe_bgp.engine.bgp_manager.Manager')
    def test_manager_stop(self, mocked_bgp_manager):
        self.manager._get_vpn_instance("TEST_VPN_INSTANCE", consts.IPVPN,
                                       [t.RT1], [t.RT1], "192.168.0.1", 24,
                                       {}, {})
        self.manager.stop()
        self.assertTrue(not self.manager.vpn_instances)
