from .releases import base_vm_classes as relbase
from .test_network_ipv6 import TestNetworkIPV6Abs


# reuse basic network tests but with different config (static, no dhcp)
class TestNetworkIPV6StaticAbs(TestNetworkIPV6Abs):
    conf_file = "examples/tests/basic_network_static_ipv6.yaml"


class PreciseHWETTestNetworkIPV6Static(relbase.precise_hwe_t,
                                       TestNetworkIPV6StaticAbs):
    __test__ = True


class TrustyTestNetworkIPV6Static(relbase.trusty, TestNetworkIPV6StaticAbs):
    __test__ = True


class TrustyHWEUTestNetworkIPV6Static(relbase.trusty_hwe_u,
                                      TestNetworkIPV6StaticAbs):
    # unsupported kernel, 2016-08
    __test__ = False


class TrustyHWEVTestNetworkIPV6Static(relbase.trusty_hwe_v,
                                      TestNetworkIPV6StaticAbs):
    # unsupported kernel, 2016-08
    __test__ = False


class TrustyHWEWTestNetworkIPV6Static(relbase.trusty_hwe_w,
                                      TestNetworkIPV6StaticAbs):
    # unsupported kernel, 2016-08
    __test__ = False


class XenialTestNetworkIPV6Static(relbase.xenial, TestNetworkIPV6StaticAbs):
    __test__ = True


class YakketyTestNetworkIPV6Static(relbase.yakkety, TestNetworkIPV6StaticAbs):
    __test__ = True
