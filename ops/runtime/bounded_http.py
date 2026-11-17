"""Small loopback service HTTP bounds; no model imports or request logging."""
import io,json,logging,threading,time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer


class DeadlineReader(io.RawIOBase):
    def __init__(self,connection,deadline):
        super().__init__();self.connection=connection;self.deadline=deadline
    def readable(self):return True
    def readinto(self,buffer):
        remaining=self.deadline-time.monotonic()
        if remaining<=0:raise TimeoutError('Request read deadline exceeded')
        self.connection.settimeout(remaining)
        return self.connection.recv_into(buffer)


class BoundedHTTPServer(ThreadingHTTPServer):
    daemon_threads=True
    request_queue_size=8
    def __init__(self,address,handler,*,max_connections=8,header_timeout=10,body_timeout=20):
        self.slots=threading.BoundedSemaphore(max_connections)
        self.header_timeout=header_timeout;self.body_timeout=body_timeout
        super().__init__(address,handler)
    def get_request(self):
        connection,address=super().get_request();connection.settimeout(self.header_timeout)
        return connection,address
    def process_request(self,connection,address):
        if not self.slots.acquire(blocking=False):
            try:
                connection.settimeout(.2);data=b'{"error":"connection_limit"}'
                connection.sendall(b'HTTP/1.0 503 Service Unavailable\r\nContent-Type: application/json\r\nCache-Control: no-store\r\nConnection: close\r\nContent-Length: '+str(len(data)).encode()+b'\r\n\r\n'+data)
            except OSError:pass
            finally:self.shutdown_request(connection)
            return
        try:super().process_request(connection,address)
        except BaseException:self.slots.release();raise
    def process_request_thread(self,connection,address):
        try:super().process_request_thread(connection,address)
        finally:self.slots.release()
    def handle_error(self,request,address):logging.error('Bounded HTTP request failed')


class BoundedHandler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup();self.rfile.close()
        self.deadline_reader=DeadlineReader(self.connection,time.monotonic()+self.server.header_timeout)
        self.rfile=io.BufferedReader(self.deadline_reader)
    def log_message(self,*args):pass
    def reply(self,status,value,headers=None):
        payload=json.dumps(value,ensure_ascii=False,allow_nan=False).encode();self.close_connection=True
        try:
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(payload)))
            self.send_header('Cache-Control','no-store');self.send_header('Connection','close')
            for name,value in (headers or {}).items():self.send_header(name,value)
            self.end_headers();self.wfile.write(payload)
        except OSError:pass
    def read_json(self,max_bytes):
        lengths=self.headers.get_all('Content-Length',[])
        if len(lengths)!=1 or self.headers.get_all('Transfer-Encoding') or not lengths[0].isascii() or not lengths[0].isdigit():
            raise RequestError(400,'invalid_request_framing')
        try:length=int(lengths[0])
        except ValueError:raise RequestError(400,'invalid_request_framing')
        if not 0<length<=max_bytes:raise RequestError(413,'request_too_large')
        self.deadline_reader.deadline=time.monotonic()+self.server.body_timeout
        try:
            data=self.rfile.read(length)
            if len(data)!=length:raise RequestError(400,'incomplete_body')
            return json.loads(data)
        except (ValueError,OSError):raise RequestError(400,'invalid_request')


class RequestError(Exception):
    def __init__(self,status,code):self.status=status;self.code=code
