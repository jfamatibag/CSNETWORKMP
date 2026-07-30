import socket
import json
import struct
import sys

def connect_to_mtgnp(player_id, host='127.0.0.1', port=4444):
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((host, port))
    print(f"Connected to MTGNP server as '{player_id}'...")

    ready_pdu = {
        "type": "PLAYER_READY",
        "player_id": player_id,
        "deck_list": ["mountain_001", "lightning_bolt_001", "goblin_guide_001"]
    }
    
    payload_bytes = json.dumps(ready_pdu).encode('utf-8')
    length_prefix = struct.pack('>I', len(payload_bytes))
    client_socket.sendall(length_prefix + payload_bytes)
    print(f"Sent PLAYER_READY PDU for '{player_id}'.")

    try:
        while True:
            header = client_socket.recv(4)
            if not header:
                print("Server closed connection.")
                break
            msg_len = struct.unpack('>I', header)[0]
            
            # Read full payload chunk
            data = bytearray()
            while len(data) < msg_len:
                packet = client_socket.recv(msg_len - len(data))
                if not packet:
                    break
                data.extend(packet)
                
            print("\nReceived update:")
            print(json.dumps(json.loads(data.decode('utf-8')), indent=2))
    except KeyboardInterrupt:
        print("Disconnecting client.")
    finally:
        client_socket.close()

if __name__ == "__main__":
    # Get player_id from terminal argument, or prompt if omitted
    p_id = sys.argv[1] if len(sys.argv) > 1 else input("Enter unique Player ID: ")
    connect_to_mtgnp(player_id=p_id)