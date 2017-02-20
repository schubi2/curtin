from .releases import base_vm_classes as relbase
from .test_network import TestNetworkBaseTestsAbs


class TestNetworkPassthroughAbs(TestNetworkBaseTestsAbs):
    """ Multi-ip address network testing
    """
    conf_file = "examples/tests/network_passthrough.yaml"

    # FIXME: cloud-init and curtin eni rendering differ
    def test_etc_network_interfaces(self):
        pass


class TestNetworkV2PassthroughAbs(TestNetworkPassthroughAbs):
    """Test network passthrough with v2 netconfig"""
    conf_file = "examples/tests/network_v2_passthrough.yaml"


class PreciseHWETTestNetworkPassthrough(relbase.precise_hwe_t,
                                        TestNetworkPassthroughAbs):
    # cloud-init too old
    __test__ = False


class TrustyTestNetworkPassthrough(relbase.trusty, TestNetworkPassthroughAbs):
    # cloud-init too old
    __test__ = False


class XenialTestNetworkPassthrough(relbase.xenial, TestNetworkPassthroughAbs):
    __test__ = True


class YakketyTestNetworkPassthrough(relbase.yakkety,
                                    TestNetworkPassthroughAbs):
    __test__ = True


class XenialTestNetworkV2Passthrough(
        relbase.xenial, TestNetworkV2PassthroughAbs):
    # test for v2 only available on xenial due to repo add syntax
    __test__ = True
    required_net_ifaces = ['52:54:00:12:34:00']
