#
# Copyright 2011 Red Hat, Inc.
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
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301 USA
#
# Refer to the README and COPYING files for full details of the license
#

import os
import os.path
import socket
import time
import threading
import uuid
from functools import partial
from weakref import proxy
from collections import defaultdict

from yajsonrpc.betterAsyncore import Reactor
from yajsonrpc.stompreactor import StompClient, StompRpcServer
from yajsonrpc import Notification, JsonRpcBindingsError
import alignmentScan
from vdsm.config import config
from momIF import MomClient
from vdsm.compat import pickle
from vdsm.define import doneCode, errCode
from vdsm.sslcompat import sslutils
import libvirt
from vdsm import libvirtconnection
from vdsm import constants
from vdsm import utils
import caps
import blkid
import supervdsm
from protocoldetector import MultiProtocolAcceptor

from virt import migration
from virt import sampling
from virt import secret
from virt import vm
from virt import vmstatus
from virt.vm import Vm, getVDSMDomains
from virt.vmchannels import Listener
from virt.vmdevices import hwclass
from virt.utils import isVdsmImage
try:
    import gluster.api as gapi
    _glusterEnabled = True
except ImportError:
    _glusterEnabled = False


class clientIF(object):
    """
    The client interface of vdsm.

    Exposes vdsm verbs as json-rpc or xml-rpc functions.
    """
    _instance = None
    _instanceLock = threading.Lock()

    def __init__(self, irs, log, scheduler):
        """
        Initialize the (single) clientIF instance

        :param irs: a Dispatcher object to be used as this object's irs.
        :type irs: :class:`storage.dispatcher.Dispatcher`
        :param log: a log object to be used for this object's logging.
        :type log: :class:`logging.Logger`
        """
        self.vmContainerLock = threading.Lock()
        self._networkSemaphore = threading.Semaphore()
        self._shutdownSemaphore = threading.Semaphore()
        self.irs = irs
        if self.irs:
            self._contEIOVmsCB = partial(clientIF.contEIOVms, proxy(self))
            self.irs.registerDomainStateChangeCallback(self._contEIOVmsCB)
        self.log = log
        self._recovery = True
        self.channelListener = Listener(self.log)
        self._generationID = str(uuid.uuid4())
        self.mom = None
        self.bindings = {}
        self._broker_client = None
        self._subscriptions = defaultdict(list)
        self._scheduler = scheduler
        if _glusterEnabled:
            self.gluster = gapi.GlusterApi(self, log)
        else:
            self.gluster = None
        try:
            self.vmContainer = {}
            self._hostStats = sampling.HostStatsThread(log=log)
            self._hostStats.start()
            self.lastRemoteAccess = 0
            self._enabled = True
            self._netConfigDirty = False
            self._prepareMOM()
            secret.clear()
            threading.Thread(target=self._recoverThread,
                             name='clientIFinit').start()
            self.channelListener.settimeout(
                config.getint('vars', 'guest_agent_timeout'))
            self.channelListener.start()
            self.threadLocal = threading.local()
            self.threadLocal.client = ''

            host = config.get('addresses', 'management_ip')
            port = config.getint('addresses', 'management_port')

            self._createAcceptor(host, port)
            self._prepareXMLRPCBinding()
            self._prepareJSONRPCBinding()
            self._connectToBroker()
        except:
            self.log.error('failed to init clientIF, '
                           'shutting down storage dispatcher')
            if self.irs:
                self.irs.prepareForShutdown()
            raise

    def getVMs(self):
        """
        Get a snapshot of the currently registered VMs.
        Return value will be a dict of {vmUUID: VM_object}
        """
        with self.vmContainerLock:
            return self.vmContainer.copy()

    @property
    def ready(self):
        return (self.irs is None or self.irs.ready) and not self._recovery

    def notify(self, event_id, **kwargs):
        """
        Send notification using provided subscription id as
        event_id and a dictionary as event body. Before sending
        there is notify_time added on top level to the dictionary.
        """
        if not self.ready:
            self.log.warning('Not ready yet, ignoring event %r args=%r',
                             event_id, kwargs)
            return

        notification = Notification(
            event_id,
            self._send_notification,
        )
        notification.emit(**kwargs)

    def _send_notification(self, message):
        self.bindings['jsonrpc'].reactor.server.send(message,
                                                     config.get('addresses',
                                                                'event_queue'))

    def contEIOVms(self, sdUUID, isDomainStateValid):
        # This method is called everytime the onDomainStateChange
        # event is emitted, this event is emitted even when a domain goes
        # INVALID if this happens there is nothing to do
        if not isDomainStateValid:
            return

        libvirtCon = libvirtconnection.get()
        libvirtVms = libvirtCon.listAllDomains(
            libvirt.VIR_CONNECT_LIST_DOMAINS_PAUSED)

        with self.vmContainerLock:
            self.log.info("vmContainerLock acquired")
            for libvirtVm in libvirtVms:
                state = libvirtVm.state(0)
                if state[1] == libvirt.VIR_DOMAIN_PAUSED_IOERROR:
                    vmId = libvirtVm.UUIDString()
                    vmObj = self.vmContainer[vmId]
                    if sdUUID in vmObj.sdIds:
                        self.log.info("Cont vm %s in EIO", vmId)
                        vmObj.cont()

    @classmethod
    def getInstance(cls, irs=None, log=None, scheduler=None):
        with cls._instanceLock:
            if cls._instance is None:
                if log is None:
                    raise Exception("Logging facility is required to create "
                                    "the single clientIF instance")
                else:
                    cls._instance = clientIF(irs, log, scheduler)
        return cls._instance

    def _createAcceptor(self, host, port):
        sslctx = sslutils.create_ssl_context()
        self._reactor = Reactor()

        self._acceptor = MultiProtocolAcceptor(self._reactor, host,
                                               port, sslctx)

    def _connectToBroker(self):
        if config.getboolean('vars', 'broker_enable'):
            broker_address = config.get('addresses', 'broker_address')
            broker_port = config.getint('addresses', 'broker_port')
            request_queues = config.get('addresses', 'request_queues')

            sslctx = sslutils.create_ssl_context()
            sock = socket.socket()
            sock.connect((broker_address, broker_port))
            if sslctx:
                sock = sslctx.wrapSocket(sock)

            self._broker_client = StompClient(sock, self._reactor)
            for destination in request_queues.split(","):
                self._subscriptions[destination] = StompRpcServer(
                    self.bindings['jsonrpc'].server,
                    self._broker_client,
                    destination,
                    broker_address,
                    config.getint('vars', 'connection_stats_timeout'),
                    self
                )

    def _prepareXMLRPCBinding(self):
        if config.getboolean('vars', 'xmlrpc_enable'):
            try:
                from rpc.bindingxmlrpc import BindingXMLRPC
                from rpc.bindingxmlrpc import XmlDetector
            except ImportError:
                self.log.error('Unable to load the xmlrpc server module. '
                               'Please make sure it is installed.')
            else:
                xml_binding = BindingXMLRPC(self, self.log)
                self.bindings['xmlrpc'] = xml_binding
                xml_detector = XmlDetector(xml_binding)
                self._acceptor.add_detector(xml_detector)

    def _prepareJSONRPCBinding(self):
        if config.getboolean('vars', 'jsonrpc_enable'):
            try:
                from rpc import Bridge
                from rpc.bindingjsonrpc import BindingJsonRpc
                from yajsonrpc.stompreactor import StompDetector
            except ImportError:
                self.log.warn('Unable to load the json rpc server module. '
                              'Please make sure it is installed.')
            else:
                bridge = Bridge.DynamicBridge()
                json_binding = BindingJsonRpc(
                    bridge, self._subscriptions,
                    config.getint('vars', 'connection_stats_timeout'),
                    self._scheduler, self)
                self.bindings['jsonrpc'] = json_binding
                stomp_detector = StompDetector(json_binding)
                self._acceptor.add_detector(stomp_detector)

    def _prepareMOM(self):
        momconf = config.get("mom", "conf")

        self.mom = MomClient(momconf)

    def prepareForShutdown(self):
        """
        Prepare server for shutdown.

        Should be called before taking server down.
        """
        if not self._shutdownSemaphore.acquire(blocking=False):
            self.log.debug('cannot run prepareForShutdown concurrently')
            return errCode['unavail']
        try:
            if not self._enabled:
                self.log.debug('cannot run prepareForShutdown twice')
                return errCode['unavail']

            self._acceptor.stop()
            for binding in self.bindings.values():
                binding.stop()

            self._enabled = False
            secret.clear()
            self.channelListener.stop()
            self._hostStats.stop()
            if self.irs:
                return self.irs.prepareForShutdown()
            else:
                return {'status': doneCode}
        finally:
            self._shutdownSemaphore.release()

    def start(self):
        for binding in self.bindings.values():
            binding.start()
        self.thread = threading.Thread(target=self._reactor.process_requests,
                                       name='Reactor thread')
        self.thread.setDaemon(True)
        self.thread.start()

    def _getUUIDSpecPath(self, uuid):
        try:
            return blkid.getDeviceByUuid(uuid)
        except blkid.BlockIdException:
            self.log.info('Error finding path for device', exc_info=True)
            raise vm.VolumeError(uuid)

    def prepareVolumePath(self, drive, vmId=None):
        if type(drive) is dict:
            device = drive['device']
            # PDIV drive format
            if device == 'disk' and isVdsmImage(drive):
                res = self.irs.prepareImage(
                    drive['domainID'], drive['poolID'],
                    drive['imageID'], drive['volumeID'])

                if res['status']['code']:
                    raise vm.VolumeError(drive)

                volPath = res['path']
                # The order of imgVolumesInfo is not guaranteed
                drive['volumeChain'] = res['imgVolumesInfo']
                drive['volumeInfo'] = res['info']

            # GUID drive format
            elif "GUID" in drive:
                res = self.irs.getDevicesVisibility([drive["GUID"]])
                if not res["visible"][drive["GUID"]]:
                    raise vm.VolumeError(drive)

                res = self.irs.appropriateDevice(drive["GUID"], vmId)
                if res['status']['code']:
                    raise vm.VolumeError(drive)

                # Update size for LUN volume
                drive["truesize"] = res['truesize']
                drive["apparentsize"] = res['apparentsize']

                volPath = res['path']

            # UUID drive format
            elif "UUID" in drive:
                volPath = self._getUUIDSpecPath(drive["UUID"])

            # cdrom and floppy drives
            elif (device in ('cdrom', 'floppy') and 'specParams' in drive):
                params = drive['specParams']
                if 'vmPayload' in params:
                    volPath = self._prepareVolumePathFromPayload(
                        vmId, device, params['vmPayload'])
                # next line can be removed in future, when < 3.3 engine
                # is not supported
                elif (params.get('path', '') == '' and
                      drive.get('path', '') == ''):
                    volPath = ''
                else:
                    volPath = drive.get('path', '')

            elif "path" in drive:
                volPath = drive['path']

            else:
                raise vm.VolumeError(drive)

        # For BC sake: None as argument
        elif not drive:
            volPath = drive

        #  For BC sake: path as a string.
        elif os.path.exists(drive):
            volPath = drive

        else:
            raise vm.VolumeError(drive)

        self.log.info("prepared volume path: %s", volPath)
        return volPath

    def _prepareVolumePathFromPayload(self, vmId, device, payload):
        """
        param vmId:
            VM UUID or None
        param device:
            either 'floppy' or 'cdrom'
        param payload:
            a dict formed like this:
            {'volId': 'volume id',   # volId is optional
             'file': {'filename': 'content', ...}}
        """
        funcs = {'cdrom': 'mkIsoFs', 'floppy': 'mkFloppyFs'}
        if device not in funcs:
            raise vm.VolumeError("Unsupported 'device': %s" % device)
        func = getattr(supervdsm.getProxy(), funcs[device])
        return func(vmId, payload['file'], payload.get('volId'))

    def teardownVolumePath(self, drive):
        res = {'status': doneCode}
        try:
            if isVdsmImage(drive):
                res = self.irs.teardownImage(drive['domainID'],
                                             drive['poolID'], drive['imageID'])
        except TypeError:
            # paths (strings) are not deactivated
            if not isinstance(drive, basestring):
                self.log.warning("Drive is not a vdsm image: %s",
                                 drive, exc_info=True)

        return res['status']['code']

    def getDiskAlignment(self, drive):
        """
        Returns the alignment of the disk partitions

        param drive:
        is either {"poolID": , "domainID": , "imageID": , "volumeID": }
        or {"GUID": }

        Return type: a dictionary with partition names as keys and
        True for aligned partitions and False for unaligned as values
        """
        aligning = {}
        volPath = self.prepareVolumePath(drive)
        try:
            out = alignmentScan.scanImage(volPath)
            for line in out:
                aligning[line.partitionName] = line.alignmentScanResult
        finally:
            self.teardownVolumePath(drive)

        return {'status': doneCode, 'alignment': aligning}

    def createVm(self, vmParams, vmRecover=False):
        with self.vmContainerLock:
            if not vmRecover:
                if vmParams['vmId'] in self.vmContainer:
                    return errCode['exist']
            vm = Vm(self, vmParams, vmRecover)
            self.vmContainer[vmParams['vmId']] = vm
        vm.run()
        return {'status': doneCode, 'vmList': vm.status()}

    def getAllVmStats(self):
        return [v.getStats() for v in self.vmContainer.values()]

    def createStompClient(self, client_socket):
        if 'jsonrpc' in self.bindings:
            json_binding = self.bindings['jsonrpc']
            reactor = json_binding.reactor
            return reactor.createClient(client_socket)
        else:
            raise JsonRpcBindingsError()

    @utils.traceback()
    def _recoverThread(self):
        # Trying to run recover process until it works. During that time vdsm
        # stays in recovery mode (_recover=True), means all api requests
        # returns with "vdsm is in initializing process" message.
        utils.retry(self._recoverExistingVms, sleep=5)

    def _recoverExistingVms(self):
        start_time = utils.monotonic_time()
        try:
            self.log.debug('recovery: started')

            # Starting up libvirt might take long when host under high load,
            # we prefer running this code in external thread to avoid blocking
            # API response.
            mog = min(config.getint('vars', 'max_outgoing_migrations'),
                      caps.CpuTopology().cores())
            migration.SourceThread.setMaxOutgoingMigrations(mog)

            # Recover stage 1: domains from libvirt
            doms = getVDSMDomains()
            num_doms = len(doms)
            for idx, v in enumerate(doms):
                vmId = v.UUIDString()
                if self._recoverVm(vmId):
                    self.log.info(
                        'recovery [1:%d/%d]: recovered domain %s from libvirt',
                        idx+1, num_doms, vmId)
                else:
                    self.log.info(
                        'recovery [1:%d/%d]: loose domain %s found,'
                        ' killing it.', idx+1, num_doms, vmId)
                    try:
                        v.destroy()
                    except libvirt.libvirtError:
                        self.log.exception(
                            'recovery [1:%d/%d]: failed to kill loose'
                            ' domain %s', idx+1, num_doms, vmId)

            # Recover stage 2: domains from recovery files
            # we do this to safely handle VMs which disappeared
            # from the host while VDSM was down/restarting
            rec_vms = self._getVDSMVmsFromRecovery()
            num_rec_vms = len(rec_vms)
            if rec_vms:
                self.log.warning(
                    'recovery: found %i VMs from recovery files not'
                    ' reported by libvirt. This should not happen!'
                    ' Will try to recover them.', num_rec_vms)

            for idx, vmId in enumerate(rec_vms):
                if self._recoverVm(vmId):
                    self.log.info(
                        'recovery [2:%d/%d]: recovered domain %s'
                        ' from data file', idx+1, num_rec_vms, vmId)
                else:
                    self.log.warning(
                        'recovery [2:%d/%d]: VM %s failed to recover from data'
                        ' file, reported as Down', idx+1, num_rec_vms, vmId)

            # recover stage 3: waiting for domains to go up
            while self._enabled:
                launching = sum(int(v.lastStatus == vmstatus.WAIT_FOR_LAUNCH)
                                for v in self.vmContainer.values())
                if not launching:
                    break
                else:
                    self.log.info(
                        'recovery: waiting for %d domains to go up',
                        launching)
                time.sleep(1)
            self._cleanOldFiles()
            self._recovery = False

            # Now if we have VMs to restore we should wait pool connection
            # and then prepare all volumes.
            # Actually, we need it just to get the resources for future
            # volumes manipulations
            while self._enabled and self.vmContainer and \
                    not self.irs.getConnectedStoragePoolsList()['poollist']:
                self.log.info('recovery: waiting for storage pool to go up')
                time.sleep(5)

            vm_objects = self.vmContainer.values()
            num_vm_objects = len(vm_objects)
            for idx, vm_obj in enumerate(vm_objects):
                # Let's recover as much VMs as possible
                try:
                    # Do not prepare volumes when system goes down
                    if self._enabled:
                        self.log.info(
                            'recovery [%d/%d]: preparing paths for'
                            ' domain %s',  idx+1, num_vm_objects, vm_obj.id)
                        vm_obj.preparePaths(
                            vm_obj.devSpecMapFromConf()[hwclass.DISK])
                except:
                    self.log.exception(
                        "recovery [%d/%d]: failed for vm %s",
                        idx+1, num_vm_objects, vm_obj.id)

            self.log.info('recovery: completed in %is',
                          utils.monotonic_time() - start_time)

        except:
            self.log.exception("recovery: failed")
            raise

    def _getVDSMVmsFromRecovery(self):
        vms = []
        for f in os.listdir(constants.P_VDSM_RUN):
            vmId, fileType = os.path.splitext(f)
            if fileType == ".recovery":
                if vmId not in self.vmContainer:
                    vms.append(vmId)
        return vms

    def _recoverVm(self, vmid):
        try:
            recoveryFile = constants.P_VDSM_RUN + vmid + ".recovery"
            params = pickle.load(file(recoveryFile))
            now = time.time()
            pt = float(params.pop('startTime', now))
            params['elapsedTimeOffset'] = now - pt
            self.log.debug("Trying to recover " + params['vmId'])
            if not self.createVm(params, vmRecover=True)['status']['code']:
                return recoveryFile
        except:
            self.log.debug("Error recovering VM", exc_info=True)
        return None

    def _cleanOldFiles(self):
        for f in os.listdir(constants.P_VDSM_RUN):
            try:
                vmId, fileType = f.split(".", 1)
                exts = ["guest.socket", "monitor.socket",
                        "stdio.dump", "recovery"]
                if fileType in exts and vmId not in self.vmContainer:
                    self.log.debug("removing old file " + f)
                    utils.rmFile(constants.P_VDSM_RUN + f)
            except:
                pass

    def dispatchLibvirtEvents(self, conn, dom, *args):
        try:
            eventid = args[-1]
            vmid = dom.UUIDString()
            v = self.vmContainer.get(vmid)

            if not v:
                self.log.debug('unknown vm %s eventid %s args %s',
                               vmid, eventid, args)
                return

            if eventid == libvirt.VIR_DOMAIN_EVENT_ID_LIFECYCLE:
                event, detail = args[:-1]
                v.onLibvirtLifecycleEvent(event, detail, None)
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_REBOOT:
                v.onReboot()
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_RTC_CHANGE:
                utcoffset, = args[:-1]
                v.onRTCUpdate(utcoffset)
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_IO_ERROR_REASON:
                srcPath, devAlias, action, reason = args[:-1]
                v.onIOError(devAlias, reason, action)
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_GRAPHICS:
                phase, localAddr, remoteAddr, authScheme, subject = args[:-1]
                v.log.debug('graphics event phase '
                            '%s localAddr %s remoteAddr %s'
                            'authScheme %s subject %s',
                            phase, localAddr, remoteAddr, authScheme, subject)
                if phase == libvirt.VIR_DOMAIN_EVENT_GRAPHICS_INITIALIZE:
                    v.onConnect(remoteAddr['node'], remoteAddr['service'])
                elif phase == libvirt.VIR_DOMAIN_EVENT_GRAPHICS_DISCONNECT:
                    v.onDisconnect(clientIp=remoteAddr['node'],
                                   clientPort=remoteAddr['service'])
            elif eventid == libvirt.VIR_DOMAIN_EVENT_ID_WATCHDOG:
                action, = args[:-1]
                v.onWatchdogEvent(action)
            else:
                v.log.warning('unknown eventid %s args %s', eventid, args)

        except:
            self.log.error("Error running VM callback", exc_info=True)