import asyncio
import struct
import json
import sys
import argparse
from datetime import datetime
from game_logic import GameLogic

class MTGNPServer:
    def __init__(self, host='0.0.0.0', port=4444, verbose: bool = False):
        self.host = host
        self.port = port
        self.verbose = verbose
        self.clients = {}  # Maps StreamWriter connection -> player_id
        self.engine = GameLogic(verbose=verbose)

    def log_verbose(self, msg: str):
        """Prints timestamped network logs when verbose mode is enabled."""
        if self.verbose:
            timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
            print(f"[VERBOSE - SERVER {timestamp}] {msg}")

    def toggle_verbose(self):
        """Toggles verbose logging across the server, logic engine, and game state."""
        self.verbose = not self.verbose
        self.engine.verbose = self.verbose
        if hasattr(self.engine, 'state'):
            self.engine.state.verbose = self.verbose
        status = "ENABLED" if self.verbose else "DISABLED"
        print(f"\n>>> [SERVER] Verbose logging is now {status} <<<\n")

    async def listen_for_admin_commands(self):
        """Non-blocking background listener for terminal commands to toggle verbose mode at runtime."""
        loop = asyncio.get_running_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            command = line.strip().lower()
            if command in ['v', 'verbose', 'toggle']:
                self.toggle_verbose()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        client_addr = writer.get_extra_info('peername')
        print(f"Client connected: {client_addr}")
        self.log_verbose(f"New TCP connection opened from {client_addr}")
        
        # MTGNP allows a maximum of 2 connected players
        if len(self.clients) >= 2:
            print(f"Server full. Rejecting connection from {client_addr}.")
            self.log_verbose(f"Connection rejected for {client_addr}: Client cap (2) reached.")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
            
        # Assign temporary client ID until PLAYER_READY provides player_id
        temp_id = f"client_{len(self.clients) + 1}"
        self.clients[writer] = temp_id
        self.log_verbose(f"Assigned temporary ID '{temp_id}' to {client_addr}")

        try:
            while True:
                # Read 4-byte big-endian unsigned integer length prefix
                length_prefix = await reader.readexactly(4)
                msg_length = struct.unpack('>I', length_prefix)[0]
                self.log_verbose(f"Rx Header from {self.clients.get(writer)}: {length_prefix.hex()} (Expecting {msg_length} bytes)")
                
                # Read payload bytes based on length header
                payload_bytes = await reader.readexactly(msg_length)
                raw_str = payload_bytes.decode('utf-8', errors='replace')
                self.log_verbose(f"Rx Raw Payload ({len(payload_bytes)} bytes): {raw_str}")
                
                try:
                    pdu = json.loads(payload_bytes.decode('utf-8'))
                    await self.process_client_message(writer, pdu)
                except json.JSONDecodeError:
                    self.log_verbose(f"JSON decode failed for payload from {self.clients.get(writer)}")
                    await self.send_error(writer, "INVALID_JSON", "Could not parse JSON payload.")
                    
        except (asyncio.IncompleteReadError, ConnectionResetError, ConnectionAbortedError, OSError) as e:
            self.log_verbose(f"Socket exception for {client_addr}: {type(e).__name__} - {e}")
            print(f"Client {client_addr} ({self.clients.get(writer, 'unknown')}) disconnected.")
        finally:
            print(f"Closing connection for {client_addr}")
            if writer in self.clients:
                del self.clients[writer]
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def process_client_message(self, writer: asyncio.StreamWriter, pdu: dict):
        client_id = self.clients[writer]
        
        # Dynamically map socket connection to player_id sent in PLAYER_READY
        if pdu.get("type") == "PLAYER_READY" and "player_id" in pdu:
            client_id = pdu["player_id"]
            self.clients[writer] = client_id
            self.log_verbose(f"Client mapped: Assigned permanent player_id '{client_id}' to socket {writer.get_extra_info('peername')}")

        self.log_verbose(f"Dispatching PDU to engine from '{client_id}': {pdu.get('type')}")
        # Route PDU to the engine
        outbound_messages = self.engine.process_pdu(client_id, pdu)
        
        # Send resulting messages to intended recipient(s)
        for target, msg in outbound_messages:
            if target == "ALL":
                self.log_verbose(f"Broadcasting message to ALL connected clients: {msg.get('type')}")
                for w in list(self.clients.keys()):
                    await self.send_message(w, msg)
            else:
                target_writer = next((w for w, cid in self.clients.items() if cid == target), None)
                if target_writer:
                    self.log_verbose(f"Sending targeted message to '{target}': {msg.get('type')}")
                    await self.send_message(target_writer, msg)
                else:
                    self.log_verbose(f"Target '{target}' not found. Defaulting response to sender '{client_id}'")
                    await self.send_message(writer, msg)

    async def send_message(self, writer: asyncio.StreamWriter, message: dict):
        """Frames and sends an MTGNP message over TCP."""
        try:
            payload = json.dumps(message).encode('utf-8')
            length_prefix = struct.pack('>I', len(payload))
            
            target_id = self.clients.get(writer, "unknown")
            self.log_verbose(f"Tx Header to '{target_id}': {length_prefix.hex()} (Size: {len(payload)} bytes)")
            self.log_verbose(f"Tx Payload to '{target_id}': {json.dumps(message)}")

            writer.write(length_prefix + payload)
            await writer.drain()
        except Exception as e:
            print(f"Error sending message: {e}")
            self.log_verbose(f"Exception during send_message to {self.clients.get(writer)}: {e}")

    async def send_error(self, writer: asyncio.StreamWriter, code: str, message: str):
        error_pdu = {
            "type": "ERROR",
            "seq_num": self.engine.next_seq(),
            "code": code,
            "message": message
        }
        await self.send_message(writer, error_pdu)

    async def start(self):
        server = await asyncio.start_server(self.handle_client, self.host, self.port)
        addr = server.sockets[0].getsockname()
        print(f"MTGNP Server listening on {addr}. Type 'v' or 'verbose' + Enter anytime to toggle logs.")

        # Launch background admin console listener
        asyncio.create_task(self.listen_for_admin_commands())

        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MTGNP Game Server")
    parser.add_argument("--host", default="0.0.0.0", help="Host IP address to bind (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=4444, help="Port to listen on (default: 4444)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose logging from start")
    
    args = parser.parse_args()

    mtgnp_server = MTGNPServer(host=args.host, port=args.port, verbose=args.verbose)
    asyncio.run(mtgnp_server.start())