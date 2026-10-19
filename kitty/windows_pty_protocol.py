# License: GPL v3
"""The kitty-pty-broker v1 terminal frames over Windows message-mode pipes.

Wire constants/layouts follow kitty-pty-broker (MIT, Copyright (c) 2026
itsmygithubacct), revision 8cf3eb3671458370e57539be6b7bd6f4a1b8f615.
See WINDOWS-PTY-BROKER-NOTICE. No Python objects are unpickled by this transport.
Windows uses the same frame header and terminal messages. Native clients opt
into a separate attach type for full DWORD exits; ordinary v1 clients retain
POSIX wait-status semantics. V2 observer/resume requests are not implemented.
"""
from __future__ import annotations

import re
import struct
from collections import deque

MAGIC = 0x4b504231
VERSION = 1
MAX_PAYLOAD = 1024 * 1024
HEADER = struct.Struct('!IHHI')
SIZE = struct.Struct('!HHHH')
EXIT_STATUS = struct.Struct('!I')
# The C protocol's status has native 64-bit alignment and trailing padding.
STATUS_DATA = struct.Struct('!I4xqqqQQQIIHH65s4096s512s3x')
ATTACH, INPUT, RESIZE, DETACH, STATUS, TERMINATE = range(1, 7)
OUTPUT, REPLAY_DONE, EXIT, ERROR, STATUS_REPLY, ACK = range(7, 13)
RESET = 15
# Private Windows extension, outside the KPB frame-type range. Payload is the
# existing 8-byte winsize followed by uint32 version=1. Only clients requesting
# it receive NATIVE_EXIT with the exact DWORD; v1 ATTACH is never reinterpreted.
NATIVE_ATTACH = 0x8001
NATIVE_EXIT = 0x8002
NATIVE_VERSION = struct.Struct('!I')


def encode(kind: int, payload: bytes = b'') -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError('Broker message is too large')
    return HEADER.pack(MAGIC, VERSION, kind, len(payload)) + payload


def decode(message: bytes) -> tuple[int, bytes]:
    if len(message) < HEADER.size:
        raise ValueError('Truncated broker header')
    magic, version, kind, size = HEADER.unpack_from(message)
    if magic != MAGIC or version != VERSION or size > MAX_PAYLOAD or len(message) != HEADER.size + size:
        raise ValueError('Invalid broker frame')
    return kind, message[HEADER.size:]


def receive(connection: object) -> tuple[int, bytes]:
    return decode(connection.recv_bytes(HEADER.size + MAX_PAYLOAD))


def send(connection: object, kind: int, payload: bytes = b'') -> None:
    connection.send_bytes(encode(kind, payload))


def status_bytes(data: dict) -> bytes:
    strings = [str(data[key]).encode('utf-8')[:limit - 1] for key, limit in
               (('id', 65), ('cwd', 4096), ('command', 512))]
    return STATUS_DATA.pack(VERSION, data['broker_pid'], data['child_pid'], -1,
                            data['started_millis'], data['journal_bytes'], data['journal_epoch'],
                            data['attached'], data['replay_complete'], data['rows'], data['columns'], *strings)


def status_dict(payload: bytes) -> dict:
    fields = STATUS_DATA.unpack(payload)
    if fields[0] != VERSION:
        raise ValueError('Unsupported broker status version')
    keys = ('broker_pid', 'child_pid', 'foreground_pgrp', 'started_millis', 'journal_bytes',
            'journal_epoch', 'attached', 'replay_complete', 'rows', 'columns', 'id', 'cwd', 'command')
    result = dict(zip(keys, fields[1:]))
    for key in ('id', 'cwd', 'command'):
        result[key] = result[key].split(b'\0', 1)[0].decode('utf-8', 'replace')
    for key in ('attached', 'replay_complete'):
        result[key] = bool(result[key])
    return result


class Journal:
    def __init__(self, limit: int = 64 * 1024 * 1024):
        self.limit = max(32768, limit)
        self.chunks: deque[bytes] = deque()
        self.size = 0
        self.epoch = 0
        self.complete = True

    def append(self, chunk: bytes) -> None:
        if self.size + len(chunk) > self.limit:
            self.chunks.clear()
            self.size = 0
            self.epoch += 1
            self.complete = False
        chunk = chunk[-self.limit:]
        self.chunks.append(chunk)
        self.size += len(chunk)


class ResizeDecoder:
    """The existing engine pipe-bridge resize escape, including split writes."""
    def __init__(self):
        self.pending = b''

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        data = self.pending + data
        self.pending = b''
        result = []
        while data:
            at = data.find(b'\x1b')
            if at < 0:
                result.append((INPUT, data))
                break
            if at:
                result.append((INPUT, data[:at]))
                data = data[at:]
            match = re.match(rb'\x1b\[8;([0-9]{1,5});([0-9]{1,5})t', data)
            if match:
                rows, cols = map(int, match.groups())
                if 0 < rows <= 32767 and 0 < cols <= 32767:
                    result.append((RESIZE, SIZE.pack(rows, cols, 0, 0)))
                else:
                    result.append((INPUT, match[0]))
                data = data[match.end():]
            elif (len(data) < 18 and (b'\x1b[8;'.startswith(data)
                                      or re.fullmatch(rb'\x1b\[8;[0-9;]*', data))):
                self.pending = data
                break
            else:
                result.append((INPUT, data[:1]))
                data = data[1:]
        return result

    def flush(self) -> bytes:
        answer, self.pending = self.pending, b''
        return answer
