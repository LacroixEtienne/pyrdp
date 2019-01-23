#
# This file is part of the PyRDP project.
# Copyright (C) 2019 GoSecure Inc.
# Licensed under the GPLv3 or later.
#

FROM ubuntu:18.04
# Uncomment the following option only if you run the player.
# The purpose of this is to stop Qt from using the MITM-SHM X11 Shared Memory Extension.
#ENV QT_X11_NO_MITSHM=1
RUN apt-get update

# Install Dependencies
RUN apt-get install python3 python3-pip -y
RUN apt-get install notify-osd dbus-x11 python3-pyqt4 -y
RUN pip3 install --upgrade setuptools cryptography

COPY . /pyrdp

RUN cd /pyrdp \
    && python3 setup.py install

# Create user
RUN useradd --create-home --home-dir /home/pyrdp pyrdp 
USER pyrdp

WORKDIR /home/pyrdp