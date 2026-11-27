from pythonosc import dispatcher
import pythonosc
from pythonosc import osc_server

def print_osc_message(address, *args):
    print(f"OSC message received: {address} {args}")

dispatcher = dispatcher.Dispatcher()
dispatcher.map("/*", print_osc_message)

ip = "0.0.0.0"
port = 39539
sever = osc_server.ThreadingOSCUDPServer((ip, port), dispatcher)

print(f"OSC server listening on {ip}:{port}")
sever.serve_forever()