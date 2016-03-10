import abc

from oslo_config import cfg
import six


@six.add_metaclass(abc.ABCMeta)
class GBPServiceChainAgentRpcCallBackMixin(object):
    """
    Mix-in to support Group Based Policy Service Chain service plugin
    notifications in agent implementations.
    """
    def attach_port_on_gbp_servicechain_network(self, context,
                                                port_gbp_sc_info,
                                                host=None):
        """
        Handle RPC cast from service plugin to attach port on GBP Service Chain
        network.
        """
        if not host or host == cfg.CONF.host:
            self.gbp_servicechain_port_attach(context, port_gbp_sc_info)

    def detach_port_from_gbp_servicechain_network(self, context,
                                                  port_gbp_sc_info,
                                                  host=None):
        """
        Handle RPC cast from service plugin to detach port from GBP Service
        Chain network.
        """
        if not host or host == cfg.CONF.host:
            self.gbp_servicechain_port_detach(context, port_gbp_sc_info)

    @abc.abstractmethod
    def gbp_servicechain_port_attach(self, context, port_gbp_sc_info):
        pass

    @abc.abstractmethod
    def gbp_servicechain_port_detach(self, context, port_gbp_sc_info):
        pass
