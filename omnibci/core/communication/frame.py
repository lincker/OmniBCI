#!/usr/bin/env python
# -*- coding: utf-8 -*-
# @Author   : lincker <haolin.d.wu@gmail.com>
# @Date     : 2024-12-02 09:01

class Frame:

    class Header:
        HEADER_SIZE = 8

        def __init__(self, version: int=1, body_size: int=0):
            self.version = version
            self.body_size = body_size

        def to_bytes(self):
            version_bytes = self.version.to_bytes(length=4, byteorder='big')
            body_size_bytes = self.body_size.to_bytes(length=4, byteorder='big')
            return version_bytes + body_size_bytes

        @classmethod
        def from_bytes(cls, header_bytes):
            version = int.from_bytes(header_bytes[:4], byteorder='big')
            body_size = int.from_bytes(header_bytes[4:], byteorder='big')
            return cls(version, body_size)


    def __init__(self, body: str=None):
        body_bytes = body.encode()
        body_size = len(body_bytes)
        self.header = Frame.Header(1, body_size)
        self.body = body

    def to_bytes(self):
        header_bytes = self.header.to_bytes()
        body_bytes = self.body.encode()
        return header_bytes + body_bytes

