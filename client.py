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
    client_socket.connect((host, port))
    print(f"Connected to MTGNP server as '{player_id}'...")

    deck = load_deck_from_csv(deck_size=20)

    ready_pdu = {
        "type": "PLAYER_READY",
        "player_id": player_id,
        "deck_list": deck
    }
    
    payload_bytes = json.dumps(ready_pdu).encode('utf-8')
    length_prefix = struct.pack('>I', len(payload_bytes))
    client_socket.sendall(length_prefix + payload_bytes)
    print(f"Sent PLAYER_READY PDU with {len(deck)} cards from CSV.")

    kept_hand = False

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
            print("\nReceived update:")
            print(json.dumps(pdu_response, indent=2))

            state = pdu_response.get("state", {})
            phase = state.get("phase")

            # Interactive London Mulligan Logic
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
                    "keep": is_keep,
                    "cards_to_bottom": bottom_cards
                }

                payload = json.dumps(choice_pdu).encode('utf-8')
                prefix = struct.pack('>I', len(payload))
                client_socket.sendall(prefix + payload)

                if is_keep:
                    print(f"Hand kept. Remaining hand size will be {7 - mulligan_count}. Waiting for opponent...")
                else:
                    print("Mulligan requested. Drawing 7 new cards...")

            elif phase == "IN_GAME":
                active_player = state.get("active_player")
                turn = state.get("turn")
                print(f"\n>>> GAME STARTED! Turn {turn} - Active Player: {active_player} <<<")

    except KeyboardInterrupt:
        print("Disconnecting client.")
    finally:
        client_socket.close()

if __name__ == "__main__":
    player_identifier = sys.argv[1] if len(sys.argv) > 1 else input("Enter unique Player ID: ")
    connect_to_mtgnp(player_id=player_identifier)