import socket
import json
import struct

def connect_to_mtgnp(host='127.0.0.1', port=4444):
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client_socket.connect((host, port))
    print(f"Connected to MTGNP server at {host}:{port}")

    ready_pdu = {
        "type": "PLAYER_READY",
        "player_id": "chandra_nalaar_99",
        "deck_list": ["mountain_001", "lightning_bolt_001", "goblin_guide_001"]
    }
    
    payload_bytes = json.dumps(ready_pdu).encode('utf-8')
    length_prefix = struct.pack('>I', len(payload_bytes))
    client_socket.sendall(length_prefix + payload_bytes)
    print("Sent PLAYER_READY PDU.")

    # Keep client running to receive responses
    try:
        while True:
            header = client_socket.recv(4)
            if not header:
                print("Server closed connection.")
                break
            msg_len = struct.unpack('>I', header)[0]
            data = client_socket.recv(msg_len)
            print("Received update:", json.loads(data.decode('utf-8')))
    except KeyboardInterrupt:
        print("Disconnecting client.")
    finally:
        client_socket.close()

if __name__ == "__main__":
    connect_to_mtgnp()