"""Shared protocol definitions and helpers for the time-sync project.

Packet layout (network byte order), 31 bytes:
    magic(2s) | kind(B) | seq(I) | t1(d) | t2(d) | t3(d)

    t1 = client send time    (client clock)
    t2 = server receive time (server clock)
    t3 = server send time    (server clock)
    t4 = client receive time (client clock, never sent on the wire)
"""
import socket
import struct
import time

MAGIC_SYNC = b"TS"   # synchronization traffic
MAGIC_BG = b"BG"     # background (cross) traffic
REQ, RESP = 1, 2

_FMT = "!2sBIddd"
SYNC_SIZE = struct.calcsize(_FMT)
DSCP_EF = 46         # "Expedited Forwarding" class, used to mark sync packets


def pack(kind, seq, t1=0.0, t2=0.0, t3=0.0):
    return struct.pack(_FMT, MAGIC_SYNC, kind, seq, t1, t2, t3)


def unpack(data):
    _, kind, seq, t1, t2, t3 = struct.unpack(_FMT, data[:SYNC_SIZE])
    return kind, seq, t1, t2, t3


def is_sync(data):
    return len(data) >= SYNC_SIZE and data[:2] == MAGIC_SYNC


class Clock:
    """A software clock = real time + adjustable offset.

    Giving every client a different initial offset lets us know the *true*
    error exactly (the reference server uses the unmodified system time).
    """

    def __init__(self, offset=0.0):
        self.offset = offset

    def now(self):
        return time.time() + self.offset


def mark_dscp(sock, dscp=DSCP_EF):
    """Set the IP TOS/DSCP bits (best effort; ignored if the OS refuses)."""
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, dscp << 2)
    except (OSError, AttributeError):
        pass
