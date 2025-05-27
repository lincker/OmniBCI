#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2025-05-19 16:02
from neuroengine.message.command_server import CommandServer

# ACQUISITION SETTINGS
FILE_READER_CHUNK_SIZE = 1024
FILE_READER_PLAY_SPEED = 1.0

DATA_PROVIDER_IP = '192.168.0.3'
DATA_PROVIDER_PORT = 4000


# NEUROENGINE SETTINGS
MESSAGE_SERVER_IP = 'localhost'
MESSAGE_SERVER_PORT = 8000

COMMAND_SERVER_IP = 'localhost'
COMMAND_SERVER_PORT = 8080

