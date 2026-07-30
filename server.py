import asyncio
import struct
import json
from game_logic import GameLogic

class MTGNPServer:
    def __init__(self, host='0.0.0.0', port=4444):
        self.host = host
        self.port = port
        self.clients = {}  # Maps StreamWriter connection -> player_id
        self.engine = GameLogic()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        client_addr = writer.get_extra_info('peername')
        print(f"Client connected: {client_addr}")
        
        # MTGNP allows a maximum of 2 connected players
        if len(self.clients) >= 2:
            print(f"Server full. Rejecting connection from {client_addr}.")
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
            
        # Assign temporary client ID until PLAYER_READY provides player_id
        temp_id = f"client_{len(self.clients) + 1}"
        self.clients[writer] = temp_id

        try:
            while True:
                # Read 4-byte big-endian unsigned integer length prefix[cite: 2]
                length_prefix = await reader.readexactly(4)
                msg_length = struct.unpack('>I', length_prefix)[0]
                
                # Read payload bytes based on length header
                payload_bytes = await reader.readexactly(msg_length)
                
                try:
                    pdu = json.loads(payload_bytes.decode('utf-8'))
                    await self.process_client_message(writer, pdu)
                except json.JSONDecodeError:
                    await self.send_error(writer, "INVALID_JSON", "Could not parse JSON payload.")
                    
        except (asyncio.IncompleteReadError, ConnectionResetError, ConnectionAbortedError, OSError):
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

        # Route PDU to the engine
        outbound_messages = self.engine.process_pdu(client_id, pdu)
        
        # Send resulting messages to intended recipient(s)
        for target, msg in outbound_messages:
            if target == "ALL":
                for w in list(self.clients.keys()):
                    await self.send_message(w, msg)
            else:
                target_writer = next((w for w, cid in self.clients.items() if cid == target), None)
                if target_writer:
                    await self.send_message(target_writer, msg)
                else:
                    await self.send_message(writer, msg)

    async def send_message(self, writer: asyncio.StreamWriter, message: dict):
        """Frames and sends an MTGNP message over TCP[cite: 2]."""
        try:
            payload = json.dumps(message).encode('utf-8')
            length_prefix = struct.pack('>I', len(payload))
            writer.write(length_prefix + payload)
            await writer.drain()
        except Exception as e:
            print(f"Error sending message: {e}")

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
        print(f"MTGNP Server listening on {addr}")

        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    mtgnp_server = MTGNPServer()
    asyncio.run(mtgnp_server.start())