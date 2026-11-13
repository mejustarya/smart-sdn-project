"""Discards background UDP traffic (runs on the server host h4) so senders get no ICMP errors."""
import argparse
import socket

ap = argparse.ArgumentParser(description="UDP sink")
ap.add_argument("--host", default="0.0.0.0")
ap.add_argument("--port", type=int, default=6000)
a = ap.parse_args()
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind((a.host, a.port))
while True:
    s.recv(65535)
