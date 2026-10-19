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
        self.total = 0
        self.epoch = 0
        self.complete = True

    def append(self, chunk: bytes) -> None:
        self.total += len(chunk)
        if self.size + len(chunk) > self.limit:
            self.chunks.clear()
            self.size = 0
            self.epoch += 1
            self.complete = False
        chunk = chunk[-self.limit:]
        self.chunks.append(chunk)
        self.size += len(chunk)


def query_kind(sequence: bytes, reply=False) -> bytes:
    """Identify ConPTY's CSI queries/replies, excluding rendering sequences."""
    body, final = sequence[2:-1], sequence[-1:]
    if reply:
        if final == b'R' and re.fullmatch(rb'\??[0-9]+;[0-9]+(?:;[0-9]+)?', body):
            return b'?6n' if body.startswith(b'?') else b'6n'
        if final == b'n' and body in (b'0', b'?0'):
            return b'?5n' if body.startswith(b'?') else b'5n'
        if final == b'c' and re.fullmatch(rb'[?>][0-9;]+', body):
            return b'da1' if body.startswith(b'?') else b'da2'
        if final == b'y' and (match := re.fullmatch(rb'(\??[0-9]+);[0-9]+\$', body)):
            return b'mode' + match[1]
        if final == b't' and re.fullmatch(rb'[0-9;]+', body):
            number = body.split(b';', 1)[0]
            return {b'1': b'11t', b'2': b'11t', b'3': b'13t', b'4': b'14t',
                    b'5': b'15t', b'6': b'16t', b'8': b'18t', b'9': b'19t'}.get(number, b'')
    else:
        if final == b'n' and body in (b'5', b'6', b'?5', b'?6'):
            return body + final
        if final == b'c' and body in (b'', b'0', b'>', b'>0'):
            return b'da2' if body.startswith(b'>') else b'da1'
        if final == b'p' and (match := re.fullmatch(rb'(\??[0-9]+)\$', body)):
            return b'mode' + match[1]
        if final == b't' and body in (b'11', b'13', b'14', b'15', b'16', b'18', b'19'):
            return body + final
    return b''


class QueryScanner:
    """Stream CSI requests without interpreting opaque OSC/DCS/APC payloads.

    The callback decides whether a complete query is retained. Sequence offsets
    let a replay retain exactly the still-unanswered request, rather than every
    historical request of that type. Pending storage is bounded independently
    of the size of graphics/control strings, which pass through unchanged.
    """
    def __init__(self, callback, offset=0, reply=False):
        self.callback, self.offset, self.reply = callback, offset, reply
        self.state = ''
        self.pending = bytearray()
        self.start = 0
        self.osc = False

    def feed(self, data: bytes) -> bytes:
        output = bytearray()
        position = 0
        while position < len(data):
            if not self.state or self.state == 'string':
                end = data.find(b'\x1b', position)
                end = len(data) if end < 0 else end
                if self.state == 'string' and self.osc:
                    bell = data.find(b'\x07', position)
                    if bell >= 0:
                        end = min(end, bell)
                output.extend(data[position:end])
                self.offset += end - position
                position = end
                if position == len(data):
                    break
            byte = data[position]
            position += 1
            self.offset += 1
            if not self.state:
                if byte == 27:
                    self.state, self.start = 'escape', self.offset - 1
                    self.pending.append(byte)
                else:
                    output.append(byte)
            elif self.state == 'escape':
                if byte == ord('['):
                    self.state = 'csi'
                    self.pending.append(byte)
                elif byte == 27:
                    output.extend(self.pending)
                    self.start = self.offset - 1
                else:
                    output.extend(self.pending)
                    output.append(byte)
                    self.pending.clear()
                    self.osc = byte == ord(']')
                    self.state = 'string' if byte in b']PX^_' else ''
            elif self.state == 'csi':
                if byte == 27:
                    output.extend(self.pending)
                    self.pending[:] = b'\x1b'
                    self.state, self.start = 'escape', self.offset - 1
                    continue
                self.pending.append(byte)
                if 0x40 <= byte <= 0x7e:
                    sequence = bytes(self.pending)
                    kind = query_kind(sequence, self.reply)
                    if not kind or self.callback(kind, (self.start, self.offset)):
                        output.extend(sequence)
                elif 0x20 <= byte <= 0x3f and len(self.pending) <= 256:
                    continue
                else:
                    output.extend(self.pending)
                self.pending.clear()
                self.state = ''
            else:
                output.append(byte)
                if (self.state == 'string-escape' and byte == ord('\\')) or (self.osc and byte == 7):
                    self.state = ''
                else:
                    self.state = 'string-escape' if byte == 27 else 'string'
        return bytes(output)

    def flush(self) -> bytes:
        value = bytes(self.pending)
        self.pending.clear()
        return value


class TerminalQueries:
    """Keep live query traffic intact and prevent replies to answered history."""
    def __init__(self):
        self.pending: dict[bytes, tuple[int, int]] = {}
        self.output = QueryScanner(self.request)
        self.input = QueryScanner(self.response, reply=True)

    def request(self, kind, span):
        # Each query type has at most one outstanding request. Bound unusual
        # applications that query thousands of different private mode numbers.
        if len(self.pending) >= 64 and kind not in self.pending:
            self.pending.pop(next(iter(self.pending)))
        self.pending[kind] = span
        return True

    def response(self, kind, span):
        self.pending.pop(kind, None)
        return True

    @staticmethod
    def replay(chunks, offset, unanswered):
        scanner = QueryScanner(lambda kind, span: span in unanswered, offset)
        for chunk in chunks:
            if data := scanner.feed(chunk):
                yield data
        if data := scanner.flush():
            yield data


class ResizeDecoder:
    """Extract pipe-bridge resizes without fragmenting other VT input.

    ConPTY can interpret a lone ESC write as an Escape key. Keep CSI replies
    and keys together even when reads split them; callers time out pending
    input so an actual Escape key is not held indefinitely.
    """
    def __init__(self):
        self.pending = b''

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        data = self.pending + data
        self.pending = b''
        result = []
        ordinary = bytearray()
        position = 0
        while position < len(data):
            at = data.find(b'\x1b', position)
            if at < 0:
                ordinary.extend(data[position:])
                break
            ordinary.extend(data[position:at])
            if at + 1 == len(data):
                self.pending = data[at:]
                break
            end = at + 2
            if data[at + 1] == ord('['):
                while end < len(data) and 0x20 <= data[end] <= 0x3f:
                    end += 1
                if end == len(data):
                    if len(data) - at <= 256:
                        self.pending = data[at:]
                    else:
                        ordinary.extend(data[at:])
                    break
                if 0x40 <= data[end] <= 0x7e:
                    end += 1
            elif data[at + 1] == ord('O'):
                if end == len(data):
                    self.pending = data[at:]
                    break
                end += 1
            sequence = data[at:end]
            match = re.fullmatch(rb'\x1b\[8;([0-9]{1,5});([0-9]{1,5})t', sequence)
            if match and all(0 < int(value) <= 32767 for value in match.groups()):
                rows, cols = map(int, match.groups())
                if ordinary:
                    result.append((INPUT, bytes(ordinary)))
                    ordinary.clear()
                result.append((RESIZE, SIZE.pack(rows, cols, 0, 0)))
            else:
                ordinary.extend(sequence)
            position = end
        if ordinary:
            result.append((INPUT, bytes(ordinary)))
        return result

    def flush(self) -> bytes:
        answer, self.pending = self.pending, b''
        return answer
