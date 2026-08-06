import socket
import json
import struct
import sys
import csv
import random

def load_deck_from_csv(filepath="mtgnp_master_card_list - Master Card List.csv", deck_size=20) -> list:
    """Reads card IDs from the master catalog CSV."""
    valid_cards = []
    try:
        with open(filepath, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            headers = {name.strip().lower(): name for name in reader.fieldnames} if reader.fieldnames else {}
            key = headers.get('card_id') or headers.get('id') or (reader.fieldnames[0] if reader.fieldnames else None)
            
            for row in reader:
                if key and row.get(key):
                    valid_cards.append(row[key].strip())
    except FileNotFoundError:
        print(f"Warning: {filepath} not found on client. Using fallback card list.")
        valid_cards = ["mountain_001", "lightning_bolt_001", "goblin_guide_001"]

    if not valid_cards:
        valid_cards = ["mountain_001", "lightning_bolt_001", "goblin_guide_001"]

    return [random.choice(valid_cards) for _ in range(deck_size)]

def prompt_cards_for_bottom(hand: list, mulligan_count: int) -> list:
    """Prompts player to pick N cards from hand to place on the bottom of the library."""
    cards_to_bottom = []
    available_hand = list(hand)
    
    print(f"\nSince you took {mulligan_count} mulligan(s), select {mulligan_count} card(s) to put on the bottom of your deck:")
    
    while len(cards_to_bottom) < mulligan_count:
        print("\nCurrent available hand:")
        for idx, card in enumerate(available_hand, 1):
            print(f"  {idx}. {card}")
            
        choice = input(f"Select card #{len(cards_to_bottom)+1} to bottom (1-{len(available_hand)}): ").strip()
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(available_hand):
                selected = available_hand.pop(idx)
                cards_to_bottom.append(selected)
                print(f"Moved '{selected}' to bottom list.")
            else:
                print("Invalid card number.")
        else:
            print("Please enter a valid number.")

    return cards_to_bottom

def connect_to_mtgnp(player_id, host='127.0.0.1', port=4444):
    client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    
    try:
        print(f"Connecting to MTGNP server at {host}:{port} as '{player_id}'...")
        client_socket.connect((host, int(port)))
        print("Connected successfully!")
    except ConnectionRefusedError:
        print(f"\n[ERROR] Connection Refused: server.py is NOT running on {host}:{port}.")
        print("-> Make sure server.py is running in another terminal window first.")
        return
    except TimeoutError:
        print(f"\n[ERROR] Connection Timed Out: Host {host} did not respond.")
        print("-> Check if Windows Firewall is blocking Port 4444 on the server machine.")
        return
    except Exception as e:
        print(f"\n[ERROR] Could not connect to {host}:{port} -> {e}")
        return

    deck = load_deck_from_csv(deck_size=20)

    # PLAYER_READY uses its own separate incremental tracker starting at 1
    ready_seq = 1
    ready_pdu = {
        "type": "PLAYER_READY",
        "seq_num": ready_seq,
        "player_id": player_id,
        "deck_list": deck
    }
    
    payload_bytes = json.dumps(ready_pdu).encode('utf-8')
    length_prefix = struct.pack('>I', len(payload_bytes))
    client_socket.sendall(length_prefix + payload_bytes)
    print(f"Sent PLAYER_READY PDU with {len(deck)} cards from CSV.")

    kept_hand = False
    current_seq_num = 0  # Tracker for echoing current server seq_num

    try:
        while True:
            header = client_socket.recv(4)
            if not header:
                print("Server closed connection.")
                break
            msg_len = struct.unpack('>I', header)[0]
            
            data = bytearray()
            while len(data) < msg_len:
                packet = client_socket.recv(msg_len - len(data))
                if not packet:
                    break
                data.extend(packet)
                
            pdu_response = json.loads(data.decode('utf-8'))
            pdu_type = pdu_response.get("type")

            # Continuously cache sequence updates sent down by the server authoritative node
            if "seq_num" in pdu_response:
                current_seq_num = pdu_response["seq_num"]

            # --- ROUTE 1: Handle LOBBY/MULLIGAN phase updates ---
            state = pdu_response.get("state", {})
            phase = state.get("phase") if state else None

            if pdu_type == "PHASE_TRANSITION":
                print(f"\n📢 PHASE CHANGE: {pdu_response.get('from_phase')} ➔ {pdu_response.get('to_phase')} (Turn {pdu_response.get('turn')})")
                continue

            if pdu_type == "ERROR":
                print(f"\n❌ [SERVER ERROR] Code: {pdu_response.get('code')} | Message: {pdu_response.get('message')}")
                continue

            if phase == "MULLIGAN" and not kept_hand:
                current_hand = state.get("hand", [])
                mulligan_count = state.get("mulligan_count", 0)

                print("\n" + "="*50)
                print(f"YOUR STARTING HAND (Mulligans taken: {mulligan_count}):")
                for idx, card in enumerate(current_hand, 1):
                    print(f"  {idx}. {card}")
                print("="*50)

                choice = ""
                while choice not in ['k', 'm']:
                    choice = input("Do you want to (K)eep or (M)ulligan? [k/m]: ").strip().lower()

                is_keep = (choice == 'k')
                bottom_cards = []

                if is_keep:
                    if mulligan_count > 0:
                        bottom_cards = prompt_cards_for_bottom(current_hand, mulligan_count)
                    kept_hand = True

                choice_pdu = {
                    "type": "MULLIGAN_CHOICE",
                    "seq_num": current_seq_num,
                    "keep": is_keep,
                    "cards_to_bottom": bottom_cards
                }

                payload = json.dumps(choice_pdu).encode('utf-8')
                prefix = struct.pack('>I', len(payload))
                client_socket.sendall(prefix + payload)
                continue

            # --- ROUTE 2: Handle Authoritative In-Game State Changes ---
            if pdu_type == "GAME_STATE_UPDATE" and phase != "LOBBY" and phase != "MULLIGAN":
                print(f"\n==================================================")
                print(f">>> TURN {state.get('turn')} | Phase: {phase} <<<")
                print(f"Active Player: {state.get('active_player')} | Priority Holder: {state.get('priority_holder')}")
                print(f"Your Life Total: {state.get('life_totals', {}).get(player_id)} | Opponent Life: {next((v for k,v in state.get('life_totals',{}).items() if k != player_id), 20)}")
                print(f"==================================================")
                print("Your Current Hand:")
                cached_hand = state.get("hand", [])
                for idx, card in enumerate(cached_hand, 1):
                    print(f"  [{idx}] {card}")
                continue

            # --- ROUTE 3: Traps and unlocks the PRIORITY_GRANT block completely (Section 10.2.5) ---
            if pdu_type == "PRIORITY_GRANT":
                grantee = pdu_response.get("player_id")
                
                if grantee == player_id:
                    print(f"\n⭐ YOU HAVE PRIORITY! (Current Action Token Seq: {current_seq_num})")
                    print("Choose an Action:")
                    print("  [p] Pass Priority")
                    print("  [l] Play a Land Permanent")
                    print("  [c] Cast a Spell from Hand")
                    
                    cmd = input("\nEnter selection (p/l/c): ").strip().lower()
                    action_pdu = None

                    if cmd == 'p':
                        action_pdu = {
                            "type": "PRIORITY_PASS",
                            "seq_num": current_seq_num
                        }
                        print("Passing priority to opponent...")
                        
                    elif cmd == 'l':
                        cid = input("Enter exact Land instance ID from hand (e.g. swamp): ").strip()
                        action_pdu = {
                            "type": "PLAY_LAND",
                            "seq_num": current_seq_num,
                            "card_id": cid
                        }
                        print(f"Attempting to deploy land permanent: {cid}...")
                        
                    elif cmd == 'c':
                        cid = input("Enter exact Card instance ID to cast (e.g. shock): ").strip()
                        tgt = input("Enter target player ID (e.g. player_1) or press Enter for no target: ").strip()
                        color_key = input("Enter mana color shorthand required (W/U/B/R/G): ").strip().upper()
                        
                        action_pdu = {
                            "type": "CAST_SPELL",
                            "seq_num": current_seq_num,
                            "card_id": cid,
                            "targets": [tgt] if tgt else [],
                            "mana_payment": {color_key: 1} if color_key else {}
                        }
                        print(f"Casting spell {cid} targeting {tgt if tgt else 'none'}...")
                    else:
                        print("Invalid entry selection. Re-triggering choices.")
                        continue

                    if action_pdu:
                        payload = json.dumps(action_pdu).encode('utf-8')
                        prefix = struct.pack('>I', len(payload))
                        client_socket.sendall(prefix + payload)
                else:
                    print(f"⏳ Waiting for priority holder ({grantee}) to complete action...")




    except KeyboardInterrupt:
        print("Disconnecting client.")
    finally:
        client_socket.close()

if __name__ == "__main__":
    player_identifier = sys.argv[1] if len(sys.argv) > 1 else input("Enter unique Player ID: ")
    
    target_ip = sys.argv[2] if len(sys.argv) > 2 else input("Enter Server IP Address (press Enter for 127.0.0.1): ").strip()
    if not target_ip:
        target_ip = "127.0.0.1"

    connect_to_mtgnp(player_id=player_identifier, host=target_ip)
