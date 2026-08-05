import socket
import threading
import json
import sys
import time
import csv
import os

HOST = "127.0.0.1"
PORT = 8080

def load_official_instances(instance_file="mtgnp_master_card_list - Card Instances.csv"):
    """Loads all available unique card_id instances grouped by base card name."""
    available_instances = {}
    if not os.path.exists(instance_file):
        print(f"[Warning] Official instance file '{instance_file}' not found. Relying on raw inputs.")
        return available_instances

    with open(instance_file, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # Skip title row
        next(reader, None)  # Skip header row
        
        for row in reader:
            if len(row) < 2: 
                continue
            card_id = row[0].strip()
            card_name = row[1].strip().lower()
            
            if card_name not in available_instances:
                available_instances[card_name] = []
            available_instances[card_name].append(card_id)
            
    return available_instances

def load_deck_from_csv(custom_deck_file, instance_file="mtgnp_master_card_list - Card Instances.csv"):
    """
    Loads deck list from CSV. Supports generic card names (e.g. 'Shock, 10')
    by cycling through available instance IDs from the master list.
    """
    deck = []
    available_instances = load_official_instances(instance_file)
    
    if not os.path.exists(custom_deck_file):
        print(f"[*] '{custom_deck_file}' not found. Creating default sample...")
        with open(custom_deck_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["Card Name", "Quantity"])
            writer.writerows([
                ("Mountain", 12),
                ("Shock", 4),
                ("Goblin Guide", 4),
                ("Lightning Bolt", 4)
            ])

    with open(custom_deck_file, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        for row in reader:
            if not row: 
                continue
            
            card_entry = row[0].strip()
            if card_entry.lower() in ["card_id", "name", "card", "title", "card name"]:
                continue
            
            qty = 1
            if len(row) > 1 and row[1].strip().isdigit():
                qty = int(row[1].strip())
            
            search_name = card_entry.lower()
            
            if search_name in available_instances:
                instances = available_instances[search_name]
                for i in range(qty):
                    deck.append(instances[i % len(instances)])
            else:
                deck.extend([card_entry] * qty)
            
    print(f"[*] Successfully loaded {len(deck)} cards from '{custom_deck_file}'.")
    return deck


class MTGNPClient:
    def __init__(self, player_id, deck_list):
        self.player_id = player_id
        self.deck_list = deck_list
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.current_priority_seq = None
        self.running = True

    def connect(self):
        self.sock.connect((HOST, PORT))
        print(f"[*] Connected to MTGNP server as {self.player_id}")
        threading.Thread(target=self.receive_loop, daemon=True).start()
        
        self.send_pdu({
            "type": "PLAYER_READY",
            "seq_num": 1,
            "player_id": self.player_id,
            "deck_list": self.deck_list
        })

    def send_pdu(self, pdu):
        try:
            data = json.dumps(pdu) + "\n"
            self.sock.sendall(data.encode('utf-8'))
        except Exception as e:
            print(f"[Error] Send failed: {e}")

    def receive_loop(self):
        buffer = ""
        while self.running:
            try:
                data = self.sock.recv(4096)
                if not data:
                    print("[*] Connection closed by server.")
                    break
                buffer += data.decode('utf-8')

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if line.strip():
                        self.handle_pdu(json.loads(line))
            except Exception as e:
                print(f"[Error] Receive loop exception: {e}")
                break

    def handle_pdu(self, pdu):
        pdu_type = pdu.get("type")

        if pdu_type == "PING":
            self.send_pdu({"type": "PONG", "seq_num": pdu.get("seq_num"), "timestamp": pdu.get("timestamp")})
            return

        if pdu_type == "PRIORITY_GRANT":
            if pdu.get("player_id") == self.player_id:
                self.current_priority_seq = pdu.get("seq_num")
                print(f"\n[>>> PRIORITY GRANTED <<<] Token seq_num: {self.current_priority_seq}")
                print("Commands: 'pass', 'land <card_id>', 'cast <card_id> [target]', 'attack <creature_id> [target]', 'block <blocker_id> <attacker_id>'")

        elif pdu_type == "GAME_STATE_UPDATE":
            state = pdu.get("state", {})
            phase = state.get("phase")
            print(f"\n--- STATE UPDATE (Turn {state.get('turn')}, Phase: {phase}) ---")
            print(f"Life: {state.get('life_totals')}")
            print(f"Your Hand: {state.get('hand', {}).get(self.player_id, [])}")
            print(f"Battlefield: {state.get('battlefield')}")
            print(f"Stack: {state.get('stack')}")

            # ==========================================
            # INSERT THE ATTACKING NOTIFICATION HERE
            # ==========================================
            if phase == "COMBAT_BLOCKERS":
                attackers = state.get("combat", {}).get("attackers", [])
                active_player = state.get("active_player")
                
                # Only show this alert if we are the defending player
                if attackers and active_player != self.player_id:
                    print("\n" + "="*40)
                    print("🚨 INCOMING ATTACK 🚨")
                    for att in attackers:
                        cid = att.get("creature_id")
                        print(f"[*] {active_player} is attacking you with: {cid}!")
                    print("="*40 + "\n")
            # ==========================================

            if phase == "MULLIGAN":
                mull_info = state.get("mulligans", {}).get(self.player_id, {})
                mull_count = mull_info.get("count", 0)
                
                if mull_info.get("status") != "KEPT":
                    print("\n[MULLIGAN PHASE] Commands:")
                    if mull_count == 0:
                        print("  - Type 'keep' to keep this hand.")
                    else:
                        print(f"  - Type 'bottom <card_id1> ...' (specify {mull_count} card(s) to put on bottom).")
                    print("  - Type 'mulligan' to draw a new hand.")
                else:
                    print("\n[MULLIGAN PHASE] Hand kept! Waiting for opponent...")

        elif pdu_type == "ERROR":
            print(f"\n[SERVER ERROR] Code: {pdu.get('code')} - Message: {pdu.get('message')}")

        elif pdu_type == "GAME_OVER":
            print(f"\n[GAME OVER] Winner: {pdu.get('winner_id')} | Reason: {pdu.get('reason')}")
            
            # Ask to rematch instead of killing the connection
            print("\nWould you like to play again? Type 'ready' to restart or 'quit' to exit.")

        else:
            print(f"\n[RECV PDU] {pdu_type}: {pdu}")

    def interactive_loop(self):
        time.sleep(0.5)
        while self.running:
            try:
                cmd = input().strip().split()
                if not cmd: 
                    continue
                action = cmd[0].lower()

                # Mulligan Actions
                if action == "keep":
                    self.send_pdu({
                        "type": "MULLIGAN_CHOICE",
                        "keep": True,
                        "cards_to_bottom": []
                    })

                elif action == "bottom":
                    cards_to_bottom = cmd[1:]
                    self.send_pdu({
                        "type": "MULLIGAN_CHOICE",
                        "keep": True,
                        "cards_to_bottom": cards_to_bottom
                    })

                elif action == "mulligan":
                    self.send_pdu({
                        "type": "MULLIGAN_CHOICE",
                        "keep": False,
                        "cards_to_bottom": []
                    })

                # Gameplay Actions
                elif action == "pass":
                    if self.current_priority_seq is None: 
                        print("You do not hold priority.")
                        continue
                    self.send_pdu({"type": "PRIORITY_PASS", "seq_num": self.current_priority_seq})
                    self.current_priority_seq = None

                elif action == "land" and len(cmd) > 1:
                    self.send_pdu({"type": "PLAY_LAND", "seq_num": self.current_priority_seq, "card_id": cmd[1]})

                elif action == "cast" and len(cmd) > 1:
                    self.send_pdu({
                        "type": "CAST_SPELL",
                        "seq_num": self.current_priority_seq,
                        "card_id": cmd[1],
                        "targets": [cmd[2]] if len(cmd) > 2 else []
                    })

                elif action == "attack" and len(cmd) > 1:
                    self.send_pdu({
                        "type": "DECLARE_ATTACKERS",
                        "seq_num": self.current_priority_seq,
                        "attackers": [{
                            "creature_id": cmd[1], 
                            "target": cmd[2] if len(cmd) > 2 else ""
                        }]
                    })

                elif action == "block" and len(cmd) > 2:
                    self.send_pdu({
                        "type": "DECLARE_BLOCKERS",
                        "seq_num": self.current_priority_seq,
                        "blockers": [{
                            "blocker_id": cmd[1],
                            "attacker_id": cmd[2]
                        }]
                    })

                elif action == "concede":
                    self.send_pdu({"type": "CONCEDE", "seq_num": 1, "player_id": self.player_id})

                # Inside interactive_loop(self)
                elif action == "quit":
                    self.running = False
                    
                elif action == "ready":
                    self.send_pdu({
                        "type": "PLAYER_READY",
                        "seq_num": 1,
                        "player_id": self.player_id,
                        "deck_list": self.deck_list
                    })

            except KeyboardInterrupt:
                break

if __name__ == "__main__":
    player_name = sys.argv[1] if len(sys.argv) > 1 else input("Enter player ID: ").strip()
    custom_deck_file = sys.argv[2] if len(sys.argv) > 2 else "deck.csv"

    deck = load_deck_from_csv(custom_deck_file)
    client = MTGNPClient(player_name, deck)
    client.connect()
    client.interactive_loop()