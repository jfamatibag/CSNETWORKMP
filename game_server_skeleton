import socket
import threading
from network_utils import send_pdu, receive_pdu

# Game States
LOBBY = "LOBBY"
GAME_SETUP = "GAME_SETUP"
MULLIGAN = "MULLIGAN"
IN_GAME = "IN_GAME"
GAME_OVER = "GAME_OVER"

class MTGServer:
    def __init__(self, host='127.0.0.1', port=65432, verbose=True):
        self.host = host
        self.port = port
        self.verbose = verbose
        self.current_state = LOBBY
        self.clients = []
        
    def handle_client(self, conn, addr):
        print(f"[SERVER] Connected by {addr}")
        try:
            while True:
                pdu = receive_pdu(conn, self.verbose)
                if not pdu:
                    break # Client disconnected
                
                # --- STATE MACHINE LOGIC GOES HERE ---
                if self.current_state == LOBBY:
                    self.process_lobby_pdu(conn, pdu)
                elif self.current_state == GAME_SETUP:
                    # Handle setup...
                    pass
                # ... handle other states ...
                
        except Exception as e:
            print(f"[SERVER] Error with {addr}: {e}")
        finally:
            print(f"[SERVER] Connection closed for {addr}")
            if conn in self.clients:
                self.clients.remove(conn)
            conn.close()

    def process_lobby_pdu(self, conn, pdu):
        """Example handler for LOBBY state."""
        # Check PDU type and validate (You will need to implement specific PDU types from your RFC)
        if pdu.get("type") == "JOIN_REQUEST":
            response = {"type": "JOIN_ACCEPT", "message": "Welcome to the Lobby"}
            send_pdu(conn, response, self.verbose)
            
            # If 2 players have joined, transition to GAME_SETUP
            if len(self.clients) == 2:
                self.current_state = GAME_SETUP
                print("[SERVER] State changed to GAME_SETUP")

    def start(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((self.host, self.port))
            s.listen()
            print(f"[SERVER] Listening on {self.host}:{self.port}")
            print(f"[SERVER] Current State: {self.current_state}")
            
            while True:
                conn, addr = s.accept()
                if len(self.clients) < 2:
                    self.clients.append(conn)
                    thread = threading.Thread(target=self.handle_client, args=(conn, addr))
                    thread.start()
                else:
                    # Reject connection if game is full
                    send_pdu(conn, {"type": "ERROR", "message": "Server full"}, self.verbose)
                    conn.close()

if __name__ == "__main__":
    # Ensure verbose mode is on for grading requirements
    server = MTGServer(verbose=True) 
    server.start()