import asyncio
import struct
import json
from game_logic import GameLogic

class MTGNPServer:
    def __init__(self, host='0.0.0.0', port=4444):
        self.host = host
        self.port = port
        self.clients = {} # Maps writer/socket to player_id
        self.engine = GameLogic()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        client_addr = writer.get_extra_info('peername')
        print(f"Client connected: {client_addr}")
        
        # We need exactly 2 clients according to the RFC
        if len(self.clients) >= 2:
            print("Server full. Rejecting connection.")
            writer.close()
            await writer.wait_closed()
            return
            
        self.clients[writer] = f"client_{len(self.clients)+1}"

        try:
            while True:
                # 1. Read the 4-byte big-endian unsigned integer length prefix
                length_prefix = await reader.readexactly(4)
                if not length_prefix:
                    break
                    
                msg_length = struct.unpack('>I', length_prefix)[0]
                
                # 2. Read the JSON payload based on the length
                payload_bytes = await reader.readexactly(msg_length)
                
                try:
                    pdu = json.loads(payload_bytes.decode('utf-8'))
                    await self.process_client_message(writer, pdu)
                except json.JSONDecodeError:
                    await self.send_error(writer, "INVALID_JSON", "Could not parse JSON.")
                    
        except asyncio.IncompleteReadError:
            print(f"Client {client_addr} disconnected improperly.")
        finally:
            print(f"Closing connection to {client_addr}")
            del self.clients[writer]
            writer.close()
            await writer.wait_closed()

    async def process_client_message(self, writer: asyncio.StreamWriter, pdu: dict):
        client_id = self.clients[writer]
        
        # Route to logic engine
        outbound_messages = self.engine.process_pdu(client_id, pdu)
        
        # Dispatch resulting PDUs to appropriate clients
        for target, msg in outbound_messages:
            if target == "ALL":
                for w in self.clients.keys():
                    await self.send_message(w, msg)
            else:
                # Find writer associated with target client_id
                target_writer = next((w for w, cid in self.clients.items() if cid == target), writer)
                await self.send_message(target_writer, msg)

    async def send_message(self, writer: asyncio.StreamWriter, message: dict):
        """Frames and sends an MTGNP message over TCP."""
        payload = json.dumps(message).encode('utf-8')
        length_prefix = struct.pack('>I', len(payload))
        writer.write(length_prefix + payload)
        await writer.drain()

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