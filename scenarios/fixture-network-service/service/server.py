import socketserver


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b"MAGI Colosseum protocol fixture ready\n")
        line = self.rfile.readline(256).strip()
        self.wfile.write(b"ack:" + line + b"\n")


socketserver.ThreadingTCPServer.allow_reuse_address = True
with socketserver.ThreadingTCPServer(("0.0.0.0", 31337), Handler) as server:
    server.serve_forever()
