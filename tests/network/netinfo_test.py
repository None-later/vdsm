#
# Copyright 2012-2016 Red Hat, Inc.
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA
#
# Refer to the README and COPYING files for full details of the license
#
from __future__ import absolute_import
import os
import io

from nose.plugins.attrib import attr

from vdsm.network import ipwrapper
from vdsm.network.netinfo import addresses, bonding, dns, misc, nics, routes
from vdsm.network.netinfo.cache import get
from vdsm.utils import random_iface_name
from vdsm import sysctl

from modprobe import RequireBondingMod
from .nettestlib import dnsmasq_run, dummy_device, veth_pair, wait_for_ipv6
from testlib import mock
from testlib import VdsmTestCase as TestCaseBase, namedTemporaryDir
from testValidation import ValidateRunningAsRoot
from testValidation import brokentest

# speeds defined in ethtool
ETHTOOL_SPEEDS = set([10, 100, 1000, 2500, 10000])


@attr(type='unit')
class TestNetinfo(TestCaseBase):

    def testGetHostNameservers(self):
        RESOLV_CONF = (
            '# Generated by NetworkManager\n'
            'search example.com company.net\n'
            'domain example.com\n'
            'nameserver 192.168.0.100\n'
            'nameserver 8.8.8.8\n'
            'nameserver 8.8.4.4\n'
        )
        nameservers = ['192.168.0.100', '8.8.8.8', '8.8.4.4']
        with namedTemporaryDir() as temp_dir:
            file_path = os.path.join(temp_dir, 'resolv.conf')

            with mock.patch.object(dns, 'DNS_CONF_FILE', file_path):
                for content in (RESOLV_CONF, RESOLV_CONF + '\n'):
                    with open(file_path, 'w') as file_object:
                        file_object.write(content)

                    self.assertEqual(dns.get_host_nameservers(), nameservers)

    def testNetmaskConversions(self):
        path = os.path.join(os.path.dirname(__file__), "netmaskconversions")
        with open(path) as netmaskFile:
            for line in netmaskFile:
                if line.startswith('#'):
                    continue
                bitmask, address = [value.strip() for value in line.split()]
                self.assertEqual(addresses.prefix2netmask(int(bitmask)),
                                 address)
        self.assertRaises(ValueError, addresses.prefix2netmask, -1)
        self.assertRaises(ValueError, addresses.prefix2netmask, 33)

    def testSpeedInvalidNic(self):
        nicName = '0' * 20  # devices can't have so long names
        self.assertEqual(nics.speed(nicName), 0)

    def testSpeedInRange(self):
        for d in nics.nics():
            s = nics.speed(d)
            self.assertFalse(s < 0)
            self.assertTrue(s in ETHTOOL_SPEEDS or s == 0)

    @mock.patch.object(nics, 'operstate')
    @mock.patch.object(nics.io, 'open')
    def testValidNicSpeed(self, mock_io_open, mock_operstate):
        values = ((0,           nics.OPERSTATE_UP, 0),
                  (-10,         nics.OPERSTATE_UP, 0),
                  (2 ** 16 - 1, nics.OPERSTATE_UP, 0),
                  (2 ** 32 - 1, nics.OPERSTATE_UP, 0),
                  (123,         nics.OPERSTATE_UP, 123),
                  ('',          nics.OPERSTATE_UP, 0),
                  ('',          'unknown',    0),
                  (123,         'unknown',    0))

        for passed, operstate, expected in values:
            mock_io_open.return_value = io.BytesIO(str(passed))
            mock_operstate.return_value = operstate

            self.assertEqual(nics.speed('fake_nic'), expected)

    @mock.patch('vdsm.network.netinfo.cache.libvirt.networks',
                lambda: {'fake': {'bridged': True}})
    def testGetNonExistantBridgeInfo(self):
        # Getting info of non existing bridge should not raise an exception,
        # just log a traceback. If it raises an exception the test will fail as
        # it should.
        get()

    @mock.patch('vdsm.network.netinfo.cache.getLinks')
    @mock.patch('vdsm.network.netinfo.cache.libvirt.networks')
    def testGetEmpty(self, mock_networks, mock_getLinks):
        result = {}
        result.update(get())
        self.assertEqual(result['networks'], {})
        self.assertEqual(result['bridges'], {})
        self.assertEqual(result['nics'], {})
        self.assertEqual(result['bondings'], {})
        self.assertEqual(result['vlans'], {})

    def testIPv4toMapped(self):
        self.assertEqual('::ffff:127.0.0.1',
                         addresses.IPv4toMapped('127.0.0.1'))

    def testGetDeviceByIP(self):
        NL_ADDRESS4 = {'label': 'iface0',
                       'address': '127.0.0.1/32',
                       'family': 'inet'}
        NL_ADDRESS6 = {'label': 'iface1',
                       'address': '2001::1:1:1/48',
                       'family': 'inet6'}
        NL_ADDRESSES = [NL_ADDRESS4, NL_ADDRESS6]

        with mock.patch.object(addresses.nl_addr, 'iter_addrs',
                               lambda: NL_ADDRESSES):
            for nl_addr in NL_ADDRESSES:
                self.assertEqual(
                    nl_addr['label'],
                    addresses.getDeviceByIP(nl_addr['address'].split('/')[0]))

    @mock.patch.object(ipwrapper.Link, '_hiddenNics', ['hid*'])
    @mock.patch.object(ipwrapper.Link, '_hiddenBonds', ['jb*'])
    @mock.patch.object(ipwrapper.Link, '_fakeNics', ['fake*'])
    @mock.patch.object(ipwrapper.Link, '_detectType', lambda x: None)
    @mock.patch.object(ipwrapper, '_bondExists', lambda x: x == 'jbond')
    @mock.patch.object(misc, 'getLinks')
    def testNics(self, mock_getLinks):
        """
        managed by vdsm: em, me, fake0, fake1
        not managed due to hidden bond (jbond) enslavement: me0, me1
        not managed due to being hidden nics: hid0, hideous
        """
        mock_getLinks.return_value = self._LINKS_REPORT

        self.assertEqual(set(nics.nics()), set(['em', 'me', 'fake', 'fake0']))

    # Creates a test fixture so that nics() reports:
    # physical nics: em, me, me0, me1, hid0 and hideous
    # dummies: fake and fake0
    # bonds: jbond (over me0 and me1)
    _LINKS_REPORT = [
        ipwrapper.Link(address='f0:de:f1:da:aa:e7', index=2,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='em', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='ff:de:f1:da:aa:e7', index=3,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='me', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='ff:de:fa:da:aa:e7', index=4,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='hid0', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='ff:de:11:da:aa:e7', index=5,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='hideous', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='66:de:f1:da:aa:e7', index=6,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='me0', qdisc='pfifo_fast', state='up',
                       master='jbond'),
        ipwrapper.Link(address='66:de:f1:da:aa:e7', index=7,
                       linkType=ipwrapper.LinkType.NIC, mtu=1500,
                       name='me1', qdisc='pfifo_fast', state='up',
                       master='jbond'),
        ipwrapper.Link(address='ff:aa:f1:da:aa:e7', index=34,
                       linkType=ipwrapper.LinkType.DUMMY, mtu=1500,
                       name='fake0', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='ff:aa:f1:da:bb:e7', index=35,
                       linkType=ipwrapper.LinkType.DUMMY, mtu=1500,
                       name='fake', qdisc='pfifo_fast', state='up'),
        ipwrapper.Link(address='66:de:f1:da:aa:e7', index=419,
                       linkType=ipwrapper.LinkType.BOND, mtu=1500,
                       name='jbond', qdisc='pfifo_fast', state='up')
    ]

    @attr(type='integration')
    @ValidateRunningAsRoot
    @mock.patch.object(ipwrapper.Link, '_fakeNics', ['veth_*', 'dummy_*'])
    def testFakeNics(self):
        with veth_pair() as (v1a, v1b):
            with dummy_device() as d1:
                fakes = set([d1, v1a, v1b])
                _nics = nics.nics()
                self.assertTrue(fakes.issubset(_nics),
                                'Fake devices %s are not listed in nics '
                                '%s' % (fakes, _nics))

        with veth_pair(prefix='mehv_') as (v2a, v2b):
            with dummy_device(prefix='mehd_') as d2:
                hiddens = set([d2, v2a, v2b])
                _nics = nics.nics()
                self.assertFalse(hiddens.intersection(_nics), 'Some of '
                                 'hidden devices %s is shown in nics %s' %
                                 (hiddens, _nics))

    def testGetIfaceCfg(self):
        deviceName = "___This_could_never_be_a_device_name___"
        ifcfg = ('GATEWAY0=1.1.1.1\n' 'NETMASK=255.255.0.0\n')
        with namedTemporaryDir() as tempDir:
            ifcfgPrefix = os.path.join(tempDir, 'ifcfg-')
            filePath = ifcfgPrefix + deviceName

            with mock.patch.object(misc, 'NET_CONF_PREF', ifcfgPrefix):
                with open(filePath, 'w') as ifcfgFile:
                    ifcfgFile.write(ifcfg)
                self.assertEqual(
                    misc.getIfaceCfg(deviceName)['GATEWAY'], '1.1.1.1')
                self.assertEqual(
                    misc.getIfaceCfg(deviceName)['NETMASK'], '255.255.0.0')

    @brokentest("Skipped becasue it breaks randomly on the CI")
    @mock.patch.object(bonding, 'BONDING_DEFAULTS',
                       bonding.BONDING_DEFAULTS
                       if os.path.exists(bonding.BONDING_DEFAULTS)
                       else '../vdsm/bonding-defaults.json')
    @attr(type='integration')
    @ValidateRunningAsRoot
    @RequireBondingMod
    def testGetBondingOptions(self):
        INTERVAL = '12345'
        bondName = random_iface_name()

        with open(bonding.BONDING_MASTERS, 'w') as bonds:
            bonds.write('+' + bondName)
            bonds.flush()

            try:  # no error is anticipated but let's make sure we can clean up
                self.assertEqual(
                    bonding._getBondingOptions(bondName), {}, "This test fails"
                    " when a new bonding option is added to the kernel. Please"
                    " run vdsm-tool dump-bonding-options` and retest.")

                with open(bonding.BONDING_OPT % (bondName, 'miimon'),
                          'w') as opt:
                    opt.write(INTERVAL)

                self.assertEqual(bonding._getBondingOptions(bondName),
                                 {'miimon': INTERVAL})

            finally:
                bonds.write('-' + bondName)

    def test_get_bonding_option_numeric_val_exists(self):
        mode_num = bonding.BONDING_MODES_NAME_TO_NUMBER["balance-rr"]
        self.assertNotEqual(bonding.get_bonding_option_numeric_val(
                            mode_num, "ad_select", "stable"),
                            None)

    def test_get_bonding_option_numeric_val_does_not_exists(self):
        mode_num = bonding.BONDING_MODES_NAME_TO_NUMBER["balance-rr"]
        self.assertEqual(bonding.get_bonding_option_numeric_val(
                         mode_num, "opt_does_not_exist", "none"),
                         None)

    def test_get_gateway(self):
        TEST_IFACE = 'test_iface'
        # different tables but the gateway is the same so it should be reported
        DUPLICATED_GATEWAY = {TEST_IFACE: [
            {
                'destination': 'none',
                'family': 'inet',
                'gateway': '12.34.56.1',
                'oif': TEST_IFACE,
                'oif_index': 8,
                'scope': 'global',
                'source': None,
                'table': 203569230,  # lucky us, we got the address 12.34.56.78
            }, {
                'destination': 'none',
                'family': 'inet',
                'gateway': '12.34.56.1',
                'oif': TEST_IFACE,
                'oif_index': 8,
                'scope': 'global',
                'source': None,
                'table': 254,
            }]}
        SINGLE_GATEWAY = {TEST_IFACE: [DUPLICATED_GATEWAY[TEST_IFACE][0]]}

        gateway = routes.get_gateway(SINGLE_GATEWAY, TEST_IFACE)
        self.assertEqual(gateway, '12.34.56.1')
        gateway = routes.get_gateway(DUPLICATED_GATEWAY, TEST_IFACE)
        self.assertEqual(gateway, '12.34.56.1')

    @attr(type='integration')
    @ValidateRunningAsRoot
    def test_ip_info(self):
        def get_ip_info(*a, **kw):
            """filter away ipv6 link local addresses that may or may not exist
            on the device depending on OS configuration"""
            ipv4addr, ipv4netmask, ipv4addrs, ipv6addrs = \
                addresses.getIpInfo(*a, **kw)
            return ipv4addr, ipv4netmask, ipv4addrs, ipv6addrs

        IP_ADDR = '192.0.2.2'
        IP_ADDR_SECOND = '192.0.2.3'
        IP_ADDR_GW = '192.0.2.1'
        IP_ADDR2 = '198.51.100.9'
        IP_ADDR3 = '198.51.100.11'
        IP_ADDR2_GW = '198.51.100.1'
        IPV6_ADDR = '2607:f0d0:1002:51::4'
        NET_MASK = '255.255.255.0'
        PREFIX_LENGTH = 24
        IPV6_PREFIX_LENGTH = 64
        IP_ADDR_CIDR = self._cidr_form(IP_ADDR, PREFIX_LENGTH)
        IP_ADDR_SECOND_CIDR = self._cidr_form(IP_ADDR_SECOND, PREFIX_LENGTH)
        IP_ADDR2_CIDR = self._cidr_form(IP_ADDR2, PREFIX_LENGTH)
        IP_ADDR3_CIDR = self._cidr_form(IP_ADDR3, 32)
        IPV6_ADDR_CIDR = self._cidr_form(IPV6_ADDR, IPV6_PREFIX_LENGTH)
        with dummy_device() as device:
            ipwrapper.addrAdd(device, IP_ADDR, PREFIX_LENGTH)
            ipwrapper.addrAdd(device, IP_ADDR_SECOND, PREFIX_LENGTH)
            ipwrapper.addrAdd(device, IP_ADDR2, PREFIX_LENGTH)
            ipwrapper.addrAdd(device, IPV6_ADDR, IPV6_PREFIX_LENGTH, family=6)
            # 32 bit addresses are reported slashless by netlink
            ipwrapper.addrAdd(device, IP_ADDR3, 32)
            self.assertEqual(
                get_ip_info(device),
                (IP_ADDR, NET_MASK,
                 [IP_ADDR_CIDR, IP_ADDR2_CIDR, IP_ADDR3_CIDR,
                  IP_ADDR_SECOND_CIDR],
                 [IPV6_ADDR_CIDR]))
            self.assertEqual(
                get_ip_info(device, ipv4_gateway=IP_ADDR_GW),
                (IP_ADDR, NET_MASK,
                 [IP_ADDR_CIDR, IP_ADDR2_CIDR, IP_ADDR3_CIDR,
                  IP_ADDR_SECOND_CIDR],
                 [IPV6_ADDR_CIDR]))
            self.assertEqual(
                get_ip_info(device, ipv4_gateway=IP_ADDR2_GW),
                (IP_ADDR2, NET_MASK,
                 [IP_ADDR_CIDR, IP_ADDR2_CIDR, IP_ADDR3_CIDR,
                  IP_ADDR_SECOND_CIDR],
                 [IPV6_ADDR_CIDR]))

    def test_netinfo_ignoring_link_scope_ip(self):
        v4_link = {'family': 'inet', 'address': '169.254.0.0/16',
                   'scope': 'link', 'prefixlen': 16, 'flags': ['permanent']}
        v4_global = {'family': 'inet', 'address': '192.0.2.2/24',
                     'scope': 'global', 'prefixlen': 24,
                     'flags': ['permanent']}
        v6_link = {'family': 'inet6', 'address': 'fe80::5054:ff:fea3:f9f3/64',
                   'scope': 'link', 'prefixlen': 64, 'flags': ['permanent']}
        v6_global = {'family': 'inet6',
                     'address': 'ee80::5054:ff:fea3:f9f3/64',
                     'scope': 'global', 'prefixlen': 64,
                     'flags': ['permanent']}
        ipaddrs = {'eth0': (v4_link, v4_global, v6_link, v6_global)}
        ipv4addr, ipv4netmask, ipv4addrs, ipv6addrs = \
            addresses.getIpInfo('eth0', ipaddrs=ipaddrs)
        self.assertEqual(ipv4addrs, ['192.0.2.2/24'])
        self.assertEqual(ipv6addrs, ['ee80::5054:ff:fea3:f9f3/64'])

    def _cidr_form(self, ip_addr, prefix_length):
        return '{}/{}'.format(ip_addr, prefix_length)

    def test_parse_bond_options(self):
        self.assertEqual(bonding.parse_bond_options('mode=4 custom=foo:bar'),
                         {'custom': {'foo': 'bar'}, 'mode': '4'})


@attr(type='integration')
class TestIPv6Addresses(TestCaseBase):
    @ValidateRunningAsRoot
    def test_local_auto_when_ipv6_is_disabled(self):
        with dummy_device() as dev:
            sysctl.disable_ipv6(dev)
            self.assertEqual(False, addresses.is_ipv6_local_auto(dev))

    @ValidateRunningAsRoot
    def test_local_auto_without_router_advertisement_server(self):
        with dummy_device() as dev:
            self.assertEqual(True, addresses.is_ipv6_local_auto(dev))

    @ValidateRunningAsRoot
    def test_local_auto_with_static_address_without_ra_server(self):
        with dummy_device() as dev:
            ipwrapper.addrAdd(dev, '2001::88', '64', family=6)
            ip_addrs = addresses.getIpAddrs()[dev]
            self.assertEqual(True, addresses.is_ipv6_local_auto(dev))
            self.assertEqual(2, len(ip_addrs))
            self.assertTrue(addresses.is_ipv6(ip_addrs[0]))
            self.assertTrue(not addresses.is_dynamic(ip_addrs[0]))

    @ValidateRunningAsRoot
    def test_local_auto_with_dynamic_address_from_ra(self):
        IPV6_NETADDRESS = '2001:1:1:1'
        IPV6_NETPREFIX_LEN = '64'
        with veth_pair() as (server, client):
            with dnsmasq_run(server, ipv6_slaac_prefix=IPV6_NETADDRESS + '::'):
                with wait_for_ipv6(client):
                    ipwrapper.linkSet(client, ['up'])
                    ipwrapper.linkSet(server, ['up'])
                    ipwrapper.addrAdd(server, IPV6_NETADDRESS + '::1',
                                      IPV6_NETPREFIX_LEN, family=6)

                # Expecting link and global addresses on client iface
                # The addresses are given randomly, so we sort them
                ip_addrs = sorted(addresses.getIpAddrs()[client],
                                  key=lambda ip: ip['address'])
                self.assertEqual(2, len(ip_addrs))

                self.assertTrue(addresses.is_dynamic(ip_addrs[0]))
                self.assertEqual('global', ip_addrs[0]['scope'])
                self.assertEqual(IPV6_NETADDRESS,
                                 ip_addrs[0]['address'][:len(IPV6_NETADDRESS)])

                self.assertEqual('link', ip_addrs[1]['scope'])
