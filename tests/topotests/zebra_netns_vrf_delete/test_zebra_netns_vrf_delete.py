#!/usr/bin/env python
# SPDX-License-Identifier: ISC
#
# test_zebra_netns_vrf_delete.py
#

"""
test_zebra_netns_vrf_delete.py: Delete netns VRFs that hold many routes.

Deleting the network namespace of a netns VRF makes zebra queue a kernel
delete for every route in that VRF and then close the namespace's dataplane
netlink socket while the dataplane pthread is still sending those deletes.
zebra must survive every deletion.
"""

import os
import sys
import pytest
from functools import partial

CWD = os.path.dirname(os.path.realpath(__file__))
sys.path.append(os.path.join(CWD, "../"))

# pylint: disable=C0413
from lib import topotest
from lib.topogen import Topogen, TopoRouter, get_topogen
from lib.topolog import logger

pytestmark = [pytest.mark.sharpd]

# netns VRF name, interface moved into it, next hop on its connected subnet
VRFS = [
    ("vrf-red", "r1-eth0", "10.0.0.2"),
    ("vrf-blue", "r1-eth1", "10.0.1.2"),
    ("vrf-green", "r1-eth2", "10.0.2.2"),
]

ROUTE_COUNT = 10000


def build_topo(tgen):
    "One router with one link per netns VRF."
    tgen.add_router("r1")
    for i in range(len(VRFS)):
        switch = tgen.add_switch("s{}".format(i + 1))
        switch.add_link(tgen.gears["r1"])


def setup_module(mod):
    "Create the netns VRFs, move one interface into each and start FRR."
    tgen = Topogen(build_topo, mod.__name__)
    tgen.start_topology()

    r1 = tgen.gears["r1"]
    if r1.check_capability(TopoRouter.RD_ZEBRA, "--vrfwnetns") is False:
        tgen.set_error("VRF NETNS backend not available on FRR")
    if os.system("ip netns list") != 0:
        tgen.set_error("NETNS not available on system")
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    for vrf, ifname, _ in VRFS:
        r1.net.add_netns(vrf)
        r1.net.set_intf_netns(ifname, vrf, up=True)

    r1.use_netns_vrf()
    r1.load_frr_config(extra_daemons=["sharpd"])
    tgen.start_router()


def teardown_module(_mod):
    tgen = get_topogen()
    tgen.stop_topology()


def test_netns_vrfs_up():
    "Each netns VRF has its interface up with its address."
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]
    expected = {
        ifname: {
            "operationalStatus": "up",
            "vrfName": vrf,
        }
        for vrf, ifname, _ in VRFS
    }
    test_func = partial(
        topotest.router_json_cmp, r1, "show interface vrf all json", expected
    )
    _, result = topotest.run_and_expect(test_func, None, count=30, wait=1)
    assert result is None, "netns VRF interfaces are not up: {}".format(result)


def test_install_routes():
    "sharpd installs ROUTE_COUNT routes into the kernel of each netns VRF."
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]
    # sharpd tracks one install at a time, so wait for each VRF in turn.
    for i, (vrf, _, nexthop) in enumerate(VRFS):
        logger.info("Installing {} routes into {}".format(ROUTE_COUNT, vrf))
        r1.vtysh_cmd(
            "sharp install routes vrf {} 10.{}.0.0 nexthop {} {}".format(
                vrf, 100 + i, nexthop, ROUTE_COUNT
            )
        )

        expected = {
            "routes": [{"type": "sharp", "rib": ROUTE_COUNT, "fib": ROUTE_COUNT}]
        }
        test_func = partial(
            topotest.router_json_cmp,
            r1,
            "show ip route vrf {} summary json".format(vrf),
            expected,
        )
        _, result = topotest.run_and_expect(test_func, None, count=60, wait=1)
        assert result is None, "{} routes are not in the FIB: {}".format(vrf, result)


def test_delete_netns_vrfs():
    "Deleting each loaded netns VRF deactivates it and leaves zebra running."
    tgen = get_topogen()
    if tgen.routers_have_failure():
        pytest.skip(tgen.errors)

    r1 = tgen.gears["r1"]
    for vrf, _, _ in VRFS:
        logger.info("Deleting netns {}".format(vrf))
        r1.net.delete_netns(vrf)

        def _vrf_inactive(vrf):
            output = r1.vtysh_cmd("show vrf", isjson=False)
            return "vrf {} id ".format(vrf) not in output

        test_func = partial(_vrf_inactive, vrf)
        _, result = topotest.run_and_expect(test_func, True, count=30, wait=1)
        assert result, "{} is still active in zebra".format(vrf)

        status = r1.check_router_running()
        assert status == "", "daemon died after deleting {}: {}".format(vrf, status)


def test_memory_leak():
    "Run the memory leak test and report results."
    tgen = get_topogen()
    if not tgen.is_memleak_enabled():
        pytest.skip("Memory leak test/report is disabled")

    tgen.report_memory_leaks()


if __name__ == "__main__":
    args = ["-s"] + sys.argv[1:]
    sys.exit(pytest.main(args))
