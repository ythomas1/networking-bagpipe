import oslo_messaging

from neutron.common import rpc as n_rpc
from neutron.common import topics
from oslo_log import log as logging

LOG = logging.getLogger(__name__)

# until we have a better way to add something in the topic namespace
# from a python package external to Neutron...
topics_BAGPIPE_GBP_SC = "bagpipe-gbp-servicechain"


class GBPServiceChainAgentNotifyApi(object):
    """
    Base class for Group Based Policy Service Chain Service Plugin notification
    to agent RPC API.
    """

    def __init__(self, topic=topics.AGENT):
        self.topic = topic

        self.topic_gbp_sc_update = topics.get_topic_name(self.topic,
                                                         topics_BAGPIPE_GBP_SC,
                                                         topics.UPDATE)

        target = oslo_messaging.Target(topic=topic, version='1.0')
        self.client = n_rpc.get_client(target)

    # Port attach/detach on/from GBP Service Chain network notifications
    # ------------------------------------------------------------------
    def _notification_host(self, context, method, port_gbp_sc_info, host):
        LOG.debug(_('Notify GBP Service Chain agent %(host)s at %(topic)s '
                    'the message %(method)s with %(port_gbp_sc_info)s'),
                  {'host': host,
                   'topic': '%s.%s' % (self.topic_gbp_sc_update, host),
                   'method': method,
                   'port_gbp_sc_info': port_gbp_sc_info})
        # TODO: per-host topics ?
        cctxt = self.client.prepare(topic=self.topic_gbp_sc_update,
                                    server=host)
        cctxt.cast(context, method, port_gbp_sc_info=port_gbp_sc_info)

    def attach_port_on_gbp_servicechain_network(self, context,
                                                port_gbp_sc_info, host=None):
        if port_gbp_sc_info:
            self._notification_host(context,
                                    'attach_port_on_gbp_servicechain_network',
                                    port_gbp_sc_info, host)

    def detach_port_from_gbp_servicechain_network(self, context,
                                                  port_gbp_sc_info, host=None):
        self._notification_host(context,
                                'detach_port_from_gbp_servicechain_network',
                                port_gbp_sc_info, host)
