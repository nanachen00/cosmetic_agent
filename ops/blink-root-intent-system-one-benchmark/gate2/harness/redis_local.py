"""Minimal RESP PING proof against the owned test Redis only. No destructive command."""
import socket
import time
from common import Refuse

def command(sock,*parts):
    encoded=[str(x).encode() for x in parts]
    wire=b'*'+str(len(encoded)).encode()+b'\r\n'+b''.join(b'$'+str(len(x)).encode()+b'\r\n'+x+b'\r\n' for x in encoded)
    sock.sendall(wire)
    data=b''
    while not data.endswith(b'\r\n'):
        chunk=sock.recv(1024)
        if not chunk: raise Refuse('Redis disconnected')
        data+=chunk
        if len(data)>4096: raise Refuse('Unexpected RESP response')
    return data

def ping(port,password,timeout=30):
    if port==6379: raise Refuse('Production Redis is not a test target')
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        try:
            with socket.create_connection(('127.0.0.1',port),timeout=2) as s:
                if command(s,'AUTH',password)!=b'+OK\r\n': raise Refuse('Test Redis authentication failed')
                if command(s,'PING')!=b'+PONG\r\n': raise Refuse('Test Redis ping failed')
                return
        except (OSError,Refuse): time.sleep(.25)
    raise Refuse('Test Redis did not become ready')
