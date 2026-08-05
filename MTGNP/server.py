import socket
import threading
import json
import time
import copy
import random
import csv
import os

HOST = "127.0.0.1"
PORT = 8080

# --- Error Codes ---
ERR_ILLEGAL_DECK = 101
ERR_DUPLICATE_ID = 102
ERR_WRONG_PHASE = 201
ERR_ILLEGAL_ACTION = 202
ERR_NOT_YOUR_TURN = 203
ERR_NO_PRIORITY = 204

# --- Card Databases ---
VALID_INSTANCES = set()         # Set of legal instance IDs
INSTANCE_TO_BASE = {}           # Maps "mountain_001" -> "mountain"
MASTER_CARD_DB = {}             # Maps "mountain" -> { "Card Type": "Land", ... }

def load_card_databases():
    """Loads card rules and instance IDs from official CSV databases."""
    master_file = "mtgnp_master_card_list - Master Card List.csv"
    instances_file = "mtgnp_master_card_list - Card Instances.csv"
    
    # 1. Load Master Stats
    if os.path.exists(master_file):
        with open(master_file, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                base_id = row.get("Card ID Base", "").strip().lower()
                if base_id:
                    MASTER_CARD_DB[base_id] = row
        print(f"[*] Loaded {len(MASTER_CARD_DB)} unique master cards.")
    else:
        print(f"[Warning] '{master_file}' not found! Rule enforcement will be disabled.")

    # 2. Load Valid Card Instances
    if os.path.exists(instances_file):
        with open(instances_file, "r", encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader, None)  # Skip title row
            next(reader, None)  # Skip header row
            for row in reader:
                if len(row) < 2: 
                    continue
                inst_id = row[0].strip()
                base_id = inst_id.rsplit('_', 1)[0]
                VALID_INSTANCES.add(inst_id)
                INSTANCE_TO_BASE[inst_id] = base_id
        print(f"[*] Loaded {len(VALID_INSTANCES)} legal card instances.")
    else:
        print(f"[Warning] '{instances_file}' not found! Deck validation will be disabled.")

# --- Server State ---
clients = {}            # player_id -> socket
client_locks = threading.Lock()
seq_counter = 1
priority_holder = None

game_state = {
    "turn": 1,
    "phase": "MULLIGAN",
    "active_player": None,
    "priority_player": None,
    "players": [],
    "life_totals": {},
    "hand": {},
    "library": {},
    "battlefield": {},
    "graveyard": {},
    "stack": [],
    "mulligans": {},      # player_id -> {"count": int, "status": "WAITING"|"KEPT"}
    "combat": {
        "attackers": [],  # List of {"creature_id": "...", "target": "..."}
        "blockers": []    # List of {"blocker_id": "...", "attacker_id": "..."}
    },
    "lands_played_this_turn": 0
}

def get_next_seq():
    global seq_counter
    seq = seq_counter
    seq_counter += 1
    return seq

def send_pdu(sock, pdu):
    try:
        data = json.dumps(pdu) + "\n"
        sock.sendall(data.encode('utf-8'))
    except Exception as e:
        print(f"[Error] Failed to send PDU: {e}")

def send_error(sock, player_id, code, message, original_pdu=None):
    pdu = {
        "type": "ERROR",
        "seq_num": get_next_seq(),
        "code": code,
        "message": message,
        "original_pdu": original_pdu
    }
    send_pdu(sock, pdu)

def broadcast_game_state_update():
    pdu = {
        "type": "GAME_STATE_UPDATE",
        "seq_num": get_next_seq(),
        "state": copy.deepcopy(game_state)
    }
    with client_locks:
        for p_id, sock in clients.items():
            send_pdu(sock, pdu)

def reset_game_state():
    global game_state, priority_holder
    priority_holder = None
    game_state = {
        "turn": 1,
        "phase": "MULLIGAN",
        "active_player": None,
        "priority_player": None,
        "players": [],
        "life_totals": {},
        "hand": {},
        "library": {},
        "battlefield": {},
        "graveyard": {},
        "stack": [],
        "mulligans": {},      
        "combat": {
            "attackers": [],  
            "blockers": []    
        },
        "lands_played_this_turn": 0
    }

def broadcast_game_over(winner_id, reason):
    pdu = {
        "type": "GAME_OVER",
        "seq_num": get_next_seq(),
        "winner_id": winner_id,
        "reason": reason
    }
    with client_locks:
        for s in clients.values():
            send_pdu(s, pdu)
    reset_game_state()

def grant_priority(player_id):
    global priority_holder
    priority_holder = player_id
    game_state["priority_player"] = player_id
    
    seq = get_next_seq()
    pdu = {
        "type": "PRIORITY_GRANT",
        "seq_num": seq,
        "player_id": player_id,
        "timestamp": int(time.time())
    }
    
    with client_locks:
        if player_id in clients:
            send_pdu(clients[player_id], pdu)

def resolve_combat():
    """Calculates combat damage and updates battlefield/graveyard/life totals."""
    active_p = game_state["active_player"]
    defending_p = [p for p in game_state["players"] if p != active_p][0]

    blocked_map = {}
    for blk in game_state["combat"]["blockers"]:
        att_id = blk.get("attacker_id")
        blk_id = blk.get("blocker_id")
        blocked_map.setdefault(att_id, []).append(blk_id)

    game_over = False
    winner_id = None

    for att in game_state["combat"]["attackers"]:
        att_id = att.get("creature_id")
        att_base = INSTANCE_TO_BASE.get(att_id, att_id.rsplit('_', 1)[0])
        att_stats = MASTER_CARD_DB.get(att_base, {})
        att_power = int(att_stats.get("Power", 0) if str(att_stats.get("Power", "-")).isdigit() else 0)
        att_tough = int(att_stats.get("Toughness", 0) if str(att_stats.get("Toughness", "-")).isdigit() else 0)

        if att_id not in blocked_map:
            # Unblocked attacker: Damage directly to defending player
            game_state["life_totals"][defending_p] -= att_power
            print(f"[*] {att_id} dealt {att_power} unblocked damage to {defending_p}! (HP now: {game_state['life_totals'][defending_p]})")

            if game_state["life_totals"][defending_p] <= 0:
                game_over = True
                winner_id = active_p
        else:
            # Blocked attacker: Combat between creature instances
            for blk_id in blocked_map[att_id]:
                blk_base = INSTANCE_TO_BASE.get(blk_id, blk_id.rsplit('_', 1)[0])
                blk_stats = MASTER_CARD_DB.get(blk_base, {})
                blk_power = int(blk_stats.get("Power", 0) if str(blk_stats.get("Power", "-")).isdigit() else 0)
                blk_tough = int(blk_stats.get("Toughness", 0) if str(blk_stats.get("Toughness", "-")).isdigit() else 0)

                if att_power >= blk_tough and blk_id in game_state["battlefield"][defending_p]:
                    game_state["battlefield"][defending_p].remove(blk_id)
                    game_state["graveyard"][defending_p].append(blk_id)
                    print(f"[*] Blocker {blk_id} died in combat!")

                if blk_power >= att_tough and att_id in game_state["battlefield"][active_p]:
                    game_state["battlefield"][active_p].remove(att_id)
                    game_state["graveyard"][active_p].append(att_id)
                    print(f"[*] Attacker {att_id} died in combat!")

    # Reset combat state & move to post-combat main phase
    game_state["combat"] = {"attackers": [], "blockers": []}
    game_state["phase"] = "POSTCOMBAT_MAIN"
    return game_over, winner_id

def pass_priority():
    global priority_holder

    # Check if priority is passed during COMBAT_BLOCKERS (defender passed on blocking)
    if game_state["phase"] == "COMBAT_BLOCKERS":
        game_over, winner_id = resolve_combat()
        
        # Broadcast the updated game state FIRST so clients see the life total drop
        broadcast_game_state_update()
        
        if game_over:
            broadcast_game_over(winner_id, f"A player reached 0 or less life.")
            return
            
        grant_priority(game_state["active_player"])
        return

    current = game_state["priority_player"]
    players = game_state["players"]
    if not players:
        return
        
    next_idx = (players.index(current) + 1) % len(players)
    next_player = players[next_idx]
    
    # If priority circles back to active player and stack is empty, advance turn
    if next_player == game_state["active_player"] and len(game_state["stack"]) == 0:
        current_active_idx = players.index(game_state["active_player"])
        next_active_idx = (current_active_idx + 1) % len(players)
        new_active_player = players[next_active_idx]
        
        game_state["turn"] += 1
        game_state["lands_played_this_turn"] = 0
        game_state["active_player"] = new_active_player
        game_state["phase"] = "PRECOMBAT_MAIN"
        
        if len(game_state["library"][new_active_player]) > 0:
            drawn_card = game_state["library"][new_active_player].pop(0)
            game_state["hand"][new_active_player].append(drawn_card)
            
        print(f"[*] Turn {game_state['turn']} started. Active player: {new_active_player}")
        broadcast_game_state_update()
        grant_priority(new_active_player)
    else:
        grant_priority(next_player)

def dispatch_pdu(sock, player_id, pdu):
    global priority_holder
    pdu_type = pdu.get("type")

    # --- PING / PONG ---
    if pdu_type == "PING":
        send_pdu(sock, {
            "type": "PONG",
            "seq_num": pdu.get("seq_num"),
            "timestamp": pdu.get("timestamp")
        })
        return

    # --- PLAYER READY ---
    if pdu_type == "PLAYER_READY":
        deck = pdu.get("deck_list", [])
        
        if not (1 <= len(deck) <= 60):
            send_error(sock, player_id, ERR_ILLEGAL_DECK, "Deck size must be between 1 and 60 cards.", pdu)
            return

        if VALID_INSTANCES:
            illegal_cards = [c for c in deck if c not in VALID_INSTANCES]
            if illegal_cards:
                send_error(sock, player_id, ERR_ILLEGAL_DECK, f"Deck contains invalid cards: {illegal_cards}", pdu)
                return

        with client_locks:
            if player_id in clients and clients[player_id] != sock:
                send_error(sock, player_id, ERR_DUPLICATE_ID, "ID already taken.", pdu)
                return
            clients[player_id] = sock

        if player_id not in game_state["players"]:
            game_state["players"].append(player_id)
            game_state["life_totals"][player_id] = 20
            game_state["battlefield"][player_id] = []
            game_state["graveyard"][player_id] = []
            
            # Shuffle Library & Draw 7 Cards
            game_state["library"][player_id] = copy.deepcopy(deck)
            random.shuffle(game_state["library"][player_id])
            
            draw_count = min(7, len(game_state["library"][player_id]))
            game_state["hand"][player_id] = [
                game_state["library"][player_id].pop(0) for _ in range(draw_count)
            ]

        if len(game_state["players"]) == 2:
            first_player = random.choice(game_state["players"])
            game_state["active_player"] = first_player
            print(f"[*] Coinflip selected starting player: {first_player}")
            game_state["phase"] = "MULLIGAN"
            game_state["mulligans"] = {
                p: {"count": 0, "status": "WAITING"} for p in game_state["players"]
            }
            broadcast_game_state_update()
        else:
            broadcast_game_state_update()
        return

    # --- MULLIGAN CHOICE ---
    if pdu_type == "MULLIGAN_CHOICE":
        if game_state["phase"] != "MULLIGAN":
            send_error(sock, player_id, ERR_WRONG_PHASE, "Not currently in Mulligan phase.", pdu)
            return

        keep = pdu.get("keep", False)
        cards_to_bottom = pdu.get("cards_to_bottom", [])
        m_info = game_state["mulligans"].get(player_id, {"count": 0, "status": "WAITING"})

        if not keep:
            m_info["count"] += 1
            game_state["library"][player_id].extend(game_state["hand"][player_id])
            game_state["hand"][player_id] = []
            random.shuffle(game_state["library"][player_id])

            draw_count = min(7, len(game_state["library"][player_id]))
            game_state["hand"][player_id] = [
                game_state["library"][player_id].pop(0) for _ in range(draw_count)
            ]
            
            broadcast_game_state_update()
            return
        else:
            if len(cards_to_bottom) != m_info["count"]:
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, 
                           f"Must put exactly {m_info['count']} card(s) on bottom of library.", pdu)
                return

            for card_id in cards_to_bottom:
                if card_id in game_state["hand"][player_id]:
                    game_state["hand"][player_id].remove(card_id)
                    game_state["library"][player_id].append(card_id)

            m_info["status"] = "KEPT"
            broadcast_game_state_update()

            if all(info["status"] == "KEPT" for info in game_state["mulligans"].values()):
                game_state["phase"] = "PRECOMBAT_MAIN"
                broadcast_game_state_update()
                grant_priority(game_state["active_player"])
            return

    # --- PRIORITY PASS ---
    if pdu_type == "PRIORITY_PASS":
        if priority_holder != player_id:
            send_error(sock, player_id, ERR_NO_PRIORITY, "You do not have priority.", pdu)
            return
        pass_priority()
        return

    # --- PLAY LAND ---
    if pdu_type == "PLAY_LAND":
        if priority_holder != player_id:
            send_error(sock, player_id, ERR_NO_PRIORITY, "You do not have priority.", pdu)
            return
        if game_state["active_player"] != player_id:
            send_error(sock, player_id, ERR_NOT_YOUR_TURN, "You can only play lands on your turn.", pdu)
            return
        if game_state["lands_played_this_turn"] >= 1:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, "Land play limit reached for this turn.", pdu)
            return

        card_id = pdu.get("card_id")
        if card_id not in game_state["hand"][player_id]:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Card '{card_id}' not in hand.", pdu)
            return

        if INSTANCE_TO_BASE and MASTER_CARD_DB:
            base_id = INSTANCE_TO_BASE.get(card_id, card_id.rsplit('_', 1)[0])
            card_data = MASTER_CARD_DB.get(base_id, {})
            if card_data.get("Card Type") != "Land":
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Card '{card_id}' is not a Land.", pdu)
                return

        game_state["hand"][player_id].remove(card_id)
        game_state["battlefield"][player_id].append(card_id)
        game_state["lands_played_this_turn"] += 1

        broadcast_game_state_update()
        grant_priority(player_id)
        return

    # --- CAST SPELL ---
    if pdu_type == "CAST_SPELL":
        if priority_holder != player_id:
            send_error(sock, player_id, ERR_NO_PRIORITY, "You do not have priority.", pdu)
            return

        card_id = pdu.get("card_id")
        targets = pdu.get("targets", [])

        if card_id not in game_state["hand"][player_id]:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Card '{card_id}' not in hand.", pdu)
            return

        base_id = INSTANCE_TO_BASE.get(card_id, card_id.rsplit('_', 1)[0])
        card_data = MASTER_CARD_DB.get(base_id, {})
        c_type = card_data.get("Card Type", "")

        if c_type == "Land":
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, "Lands cannot be cast as spells.", pdu)
            return

        # Remove the card from hand
        game_state["hand"][player_id].remove(card_id)

        # Resolve spell type
        if "Creature" in c_type or "Artifact" in c_type or "Enchantment" in c_type:
            game_state["battlefield"][player_id].append(card_id)
        else:
            if targets and targets[0] in game_state["life_totals"]:
                target_id = targets[0]
                if base_id == "lightning_bolt":
                    game_state["life_totals"][target_id] -= 3
                    print(f"[*] Lightning Bolt dealt 3 damage to {target_id}!")
                elif base_id == "shock":
                    game_state["life_totals"][target_id] -= 2
                    print(f"[*] Shock dealt 2 damage to {target_id}!")
            game_state["graveyard"][player_id].append(card_id)

        # Broadcast the new state (showing the damage taken and spell in graveyard)
        broadcast_game_state_update()

        # Check for lethal damage
        for p, life in game_state["life_totals"].items():
            if life <= 0:
                # Find the opponent to declare them the winner
                winner = [win_p for win_p in game_state["players"] if win_p != p]
                winner_id = winner[0] if winner else player_id
                
                broadcast_game_over(winner_id, f"Player {p} reached 0 or less life.")
                return

        # If no one died, normal priority resumption
        grant_priority(player_id)
        return

    # --- DECLARE ATTACKERS ---
    if pdu_type == "DECLARE_ATTACKERS":
        if priority_holder != player_id:
            send_error(sock, player_id, ERR_NO_PRIORITY, "You do not have priority.", pdu)
            return
        if game_state["active_player"] != player_id:
            send_error(sock, player_id, ERR_NOT_YOUR_TURN, "You can only attack on your turn.", pdu)
            return

        attackers = pdu.get("attackers", [])
        if not attackers:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, "No attackers declared.", pdu)
            return

        for att in attackers:
            cid = att.get("creature_id")
            if cid not in game_state["battlefield"][player_id]:
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Creature '{cid}' not on battlefield.", pdu)
                return
            # --- Added Attack Statement here ---
            print(f"[*] Player {player_id} is attacking with {cid}!")

        game_state["combat"]["attackers"] = attackers
        game_state["combat"]["blockers"] = []
        game_state["phase"] = "COMBAT_BLOCKERS"

        defending_player = [p for p in game_state["players"] if p != player_id][0]
        broadcast_game_state_update()
        grant_priority(defending_player)
        return

    # --- DECLARE BLOCKERS ---
    if pdu_type == "DECLARE_BLOCKERS":
        if priority_holder != player_id:
            send_error(sock, player_id, ERR_NO_PRIORITY, "You do not have priority.", pdu)
            return

        blockers = pdu.get("blockers", [])
        for blk in blockers:
            b_id = blk.get("blocker_id")
            if b_id not in game_state["battlefield"][player_id]:
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Blocker '{b_id}' not on battlefield.", pdu)
                return

        game_state["combat"]["blockers"] = blockers
        game_over, winner_id = resolve_combat()

        # Broadcast state so negative/0 HP is sent to clients before checking game_over
        broadcast_game_state_update()

        if game_over:
            broadcast_game_over(winner_id, f"A player reached 0 or less life.")
            return

        grant_priority(game_state["active_player"])
        return

    # --- CONCEDE ---
    if pdu_type == "CONCEDE":
        winner = [p for p in game_state["players"] if p != player_id]
        winner_id = winner[0] if winner else "None"
        broadcast_game_over(winner_id, f"Player {player_id} conceded.")
        return

def handle_client(sock, addr):
    print(f"[*] Connection accepted from {addr}")
    player_id = None
    buffer = ""
    
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
            buffer += data.decode('utf-8')

            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                
                pdu = json.loads(line)
                if player_id is None and "player_id" in pdu:
                    player_id = pdu["player_id"]

                dispatch_pdu(sock, player_id, pdu)

    except Exception as e:
        print(f"[!] Client {player_id or addr} disconnected: {e}")
    finally:
        with client_locks:
            if player_id and player_id in clients:
                del clients[player_id]
        sock.close()

def main():
    load_card_databases()
    
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(5)
    print(f"[*] MTGNP Server listening on {HOST}:{PORT}")

    while True:
        sock, addr = server.accept()
        threading.Thread(target=handle_client, args=(sock, addr), daemon=True).start()

if __name__ == "__main__":
    main()