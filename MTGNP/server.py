import socket
import threading
import json
import time
import copy
import random
import csv
import os
import re
import argparse

HOST = "0.0.0.0"
PORT = 8080
VERBOSE = False  # Global verbose toggle

def vprint(message):
    """Prints debug/game flow messages only if VERBOSE is enabled."""
    if VERBOSE:
        print(message)

def admin_console():
    """Background thread to listen for server console commands."""
    global VERBOSE
    while True:
        try:
            # Wait for the server admin to type something and press Enter
            command = input().strip().lower()
            
            if command in ["v", "verbose"]:
                VERBOSE = not VERBOSE
                state = "ENABLED" if VERBOSE else "DISABLED"
                print(f"[*] Server Admin: Verbose mode is now {state}.")
            elif command in ["help", "?"]:
                print("[*] Available console commands: 'v' or 'verbose' to toggle logging.")
                
        except EOFError:
            break
        except Exception as e:
            print(f"[!] Console input error: {e}")

# --- Protocol Error Codes ---
ERR_INVALID_JSON = "INVALID_JSON"
ERR_ILLEGAL_DECK = "ILLEGAL_DECK"
ERR_UNKNOWN_TYPE = "UNKNOWN_TYPE"
ERR_STALE_ACTION = "STALE_ACTION"
ERR_NOT_YOUR_PRIORITY = "NOT_YOUR_PRIORITY"
ERR_ILLEGAL_ACTION = "ILLEGAL_ACTION"
ERR_ILLEGAL_TARGET = "ILLEGAL_TARGET"
ERR_TRIGGER_ORDER_INVALID = "TRIGGER_ORDER_INVALID"
ERR_TRIGGER_CHOICE_INVALID = "TRIGGER_CHOICE_INVALID"
ERR_INSUFFICIENT_MANA = "INSUFFICIENT_MANA"
ERR_WRONG_PHASE = "WRONG_PHASE"
ERR_DUPLICATE_ID = "DUPLICATE_ID"

KNOWN_PDU_TYPES = {
    "PING", "PLAYER_READY", "MULLIGAN_CHOICE", "PRIORITY_PASS",
    "PLAY_LAND", "CAST_SPELL", "ACTIVATE_ABILITY", "DECLARE_ATTACKERS",
    "DECLARE_BLOCKERS", "CONCEDE", "TRIGGER_ORDER_RESPONSE", "TRIGGER_CHOICE_RESPONSE",
    "DISCARD", "ASSIGN_DAMAGE_ORDER" 
}

# --- Known Base Fallback Mana Costs ---
KNOWN_CARD_MANA_COSTS = {
    "goblin_guide": "R",
    "lightning_bolt": "R",
    "shock": "R",
    "lava_spike": "R",
    "flame_slash": "R",
    "searing_spear": "1R",
    "incinerate": "1R",
    "skullcrack": "1R",
    "rift_bolt": "2R",
    "counterspell": "UU",
    "cancel": "1UU",
    "negate": "1U",
    "mana_leak": "1U",
    "unsummon": "U",
    "ponder": "U",
    "prodigal_sorcerer": "2U",
    "merfolk_looter": "1U",
    "rod_of_ruin": "4",
    "millstone": "2",
    "sol_ring": "1",
    "giant_growth": "G",
    "vines_of_vastwood": "G",
    "rampant_growth": "1G",
    "naturalize": "1G",
    "llanowar_elves": "G",
    "elvish_mystic": "G",
    "dark_ritual": "B",
    "doom_blade": "1B",
    "terror": "1B",
    "raise_dead": "B",
    "mind_rot": "2B",
    "swords_to_plowshares": "W",
    "path_to_exile": "W",
    "healing_salve": "W",
    "pacifism": "1W",
    "mother_of_runes": "W",
}

LAND_MANA_MAP = {
    "mountain": "R",
    "island": "U",
    "swamp": "B",
    "forest": "G",
    "plains": "W"
}

# --- Strict CSV Card Databases ---
VALID_INSTANCES = set()
INSTANCE_TO_BASE = {}
MASTER_CARD_DB = {}

def get_card_field(card_data, *possible_keys):
    """Safely retrieves field value from card dictionary matching any variation of key name."""
    if not card_data:
        return ""
    for key in possible_keys:
        for actual_key in card_data.keys():
            if actual_key.strip().lower() == key.strip().lower():
                val = card_data[actual_key]
                if val is not None:
                    return str(val).strip()
    return ""

def load_card_databases():
    """Strictly loads card definitions from CSV files."""
    master_file = "mtgnp_master_card_list - Master Card List.csv"
    instances_file = "mtgnp_master_card_list - Card Instances.csv"
    
    if not os.path.exists(master_file) or not os.path.exists(instances_file):
        raise FileNotFoundError(
            f"[FATAL] Required CSV database files missing. "
            f"Server requires '{master_file}' and '{instances_file}'."
        )

    with open(master_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            base_id = get_card_field(row, "Card ID Base", "Card_ID_Base", "Base ID").lower()
            if base_id:
                MASTER_CARD_DB[base_id] = row

    with open(instances_file, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # Skip title row
        next(reader, None)  # Skip header row
        for row in reader:
            if len(row) < 1: 
                continue
            inst_id = row[0].strip()
            if not inst_id:
                continue
            base_id = inst_id.rsplit('_', 1)[0].lower()
            
            if base_id in MASTER_CARD_DB:
                VALID_INSTANCES.add(inst_id)
                INSTANCE_TO_BASE[inst_id] = base_id

    vprint(f"[*] Strict CSV Validation Active: Loaded {len(MASTER_CARD_DB)} master cards and {len(VALID_INSTANCES)} legal instances.")

# --- Server State ---
clients = {}
client_locks = threading.Lock()
seq_counter = 1
current_priority_seq = None
priority_holder = None
passed_in_succession = 0

game_state = {}

def broadcast_phase_transition(new_phase, active_player):
    pdu = {
        "type": "PHASE_TRANSITION",
        "seq_num": get_next_seq(),
        "phase": new_phase,
        "active_player": active_player
    }
    with client_locks:
        for sock in clients.values():
            send_pdu(sock, pdu)

def broadcast_stack_push(stack_item):
    pdu = {
        "type": "STACK_PUSH",
        "seq_num": get_next_seq(),
        "item": stack_item
    }
    with client_locks:
        for sock in clients.values():
            send_pdu(sock, pdu)

def broadcast_stack_resolve(resolved_item):
    pdu = {
        "type": "STACK_RESOLVE",
        "seq_num": get_next_seq(),
        "item": resolved_item
    }
    with client_locks:
        for sock in clients.values():
            send_pdu(sock, pdu)

def broadcast_combat_damage_result(damage_events):
    pdu = {
        "type": "COMBAT_DAMAGE_RESULT",
        "seq_num": get_next_seq(),
        "events": damage_events
    }
    with client_locks:
        for sock in clients.values():
            send_pdu(sock, pdu)

def reset_game_state():
    global game_state, priority_holder, passed_in_succession, current_priority_seq
    priority_holder = None
    current_priority_seq = None
    passed_in_succession = 0
    game_state = {
        "turn": 1,
        "phase": "WAITING_FOR_PLAYERS",
        "active_player": None,
        "priority_player": None,
        "priority_time": None,
        "players": [],
        "life_totals": {},
        "mana_pools": {},
        "hand": {},
        "library": {},
        "battlefield": {},
        "graveyard": {},
        "exile": {},
        "stack": [],
        "mulligans": {},
        "pending_triggers": {},
        "combat": {
            "attackers": [],
            "blockers": []
        },
        "lands_played_this_turn": 0,
        "cant_gain_life_this_turn": False,
        "last_action": ""
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
    if priority_holder == player_id and player_id is not None:
        grant_priority(player_id, reuse_seq=True)

def format_battlefield(battlefield_data):
    """Formats the battlefield dictionary line-by-line including creature P/T stats."""
    lines = ["=== BATTLEFIELD ==="]
    if not battlefield_data:
        lines.append("  (No permanents in play)")
        return "\n".join(lines)

    for player_id, permanents in battlefield_data.items():
        lines.append(f"[{player_id}]")
        if not permanents:
            lines.append("  (No permanents)")
            continue

        for perm_id, details in permanents.items():
            base_id = INSTANCE_TO_BASE.get(perm_id) or (perm_id.rsplit('_', 1)[0].lower() if '_' in perm_id else perm_id.lower())
            card_info = MASTER_CARD_DB.get(base_id, {})
            card_name = get_card_field(card_info, "Card Name", "Name") or perm_id

            pt_str = ""
            base_p = get_card_field(card_info, "Power")
            base_t = get_card_field(card_info, "Toughness")

            if base_p != "" and base_t != "":
                try:
                    bp = int(base_p)
                    bt = int(base_t)
                    buffs = details.get("buffs", {})
                    tot_p = bp + buffs.get("power", 0)
                    tot_t = bt + buffs.get("toughness", 0)
                    
                    if buffs.get("power", 0) != 0 or buffs.get("toughness", 0) != 0:
                        pt_str = f" <{tot_p}/{tot_t} (Base: {bp}/{bt})>"
                    else:
                        pt_str = f" <{tot_p}/{tot_t}>"
                except ValueError:
                    pt_str = f" <{base_p}/{base_t}>"

            status_flags = []
            if details.get("tapped"):
                status_flags.append("Tapped")
            else:
                status_flags.append("Untapped")

            if details.get("summoning_sick"):
                status_flags.append("Summoning Sick")

            if details.get("auras"):
                status_flags.append(f"Auras: {', '.join(details['auras'])}")

            status_str = f"[{', '.join(status_flags)}]"
            lines.append(f"  - {perm_id} ({card_name}){pt_str} {status_str}")

    return "\n".join(lines)

def broadcast_game_state_update():
    state_copy = copy.deepcopy(game_state)
    state_copy["battlefield_text"] = format_battlefield(game_state["battlefield"])

    pdu = {
        "type": "GAME_STATE_UPDATE",
        "seq_num": get_next_seq(),
        "state": state_copy
    }
    with client_locks:
        for sock in clients.values():
            send_pdu(sock, pdu)

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

def grant_priority(player_id, reuse_seq=False):
    global priority_holder, current_priority_seq
    priority_holder = player_id
    game_state["priority_player"] = player_id
    game_state["priority_time"] = time.time()
    
    if not reuse_seq or current_priority_seq is None:
        current_priority_seq = get_next_seq()
        
    pdu = {
        "type": "PRIORITY_GRANT",
        "seq_num": current_priority_seq,
        "player_id": player_id,
        "timestamp": int(time.time())
    }
    
    with client_locks:
        if player_id in clients:
            send_pdu(clients[player_id], pdu)

def get_card_data(instance_id):
    if instance_id not in VALID_INSTANCES:
        return None
    base_id = INSTANCE_TO_BASE.get(instance_id)
    return MASTER_CARD_DB.get(base_id)

def get_land_mana_color(land_id):
    base_id = INSTANCE_TO_BASE.get(land_id) or (land_id.rsplit('_', 1)[0].lower() if '_' in land_id else land_id.lower())
    if base_id in LAND_MANA_MAP:
        return LAND_MANA_MAP[base_id]
        
    card_info = MASTER_CARD_DB.get(base_id, {})
    card_type = get_card_field(card_info, "Card Type", "Type")
    
    for land_name, color in LAND_MANA_MAP.items():
        if land_name.capitalize() in card_type or land_name in base_id:
            return color
    return "C"

def parse_mana_cost(cost_str):
    """Parses braced ({1}{R}) or bare (1R, R) mana cost formats into required colors and generic count."""
    cost_str = str(cost_str).strip().upper()
    colored_reqs = []
    generic_req = 0

    braced_tokens = re.findall(r'\{([^}]+)\}', cost_str)
    if braced_tokens:
        for token in braced_tokens:
            if token.isdigit():
                generic_req += int(token)
            else:
                for char in token:
                    if char in "RUBGW":
                        colored_reqs.append(char)
    else:
        nums = re.findall(r'\d+', cost_str)
        if nums:
            generic_req += sum(int(n) for n in nums)
        for char in cost_str:
            if char in "RUBGW":
                colored_reqs.append(char)

    return colored_reqs, generic_req

def validate_mana_payment(card_id, card_data, mana_payment_lands):
    base_id = INSTANCE_TO_BASE.get(card_id) or (card_id.rsplit('_', 1)[0].lower() if '_' in card_id else card_id.lower())
    
    mana_cost_str = get_card_field(card_data, "Mana Cost", "Mana_Cost", "ManaCost", "Cost", "Mana")
    if not mana_cost_str:
        mana_cost_str = KNOWN_CARD_MANA_COSTS.get(base_id, "")

    colored_reqs, generic_req = parse_mana_cost(mana_cost_str)
    total_req_count = len(colored_reqs) + generic_req

    if total_req_count == 0:
        cmc_val = get_card_field(card_data, "CMC", "Mana Value")
        if cmc_val.isdigit():
            total_req_count = int(cmc_val)

    if len(mana_payment_lands) < total_req_count:
        return False, f"Spell requires {total_req_count} mana, but only {len(mana_payment_lands)} land(s) provided."

    provided_colors = [get_land_mana_color(land_id) for land_id in mana_payment_lands]
    
    temp_provided = list(provided_colors)
    for req in colored_reqs:
        if req in temp_provided:
            temp_provided.remove(req)
        else:
            return False, f"Cannot pay color cost '{req}' for cost '{mana_cost_str}' using lands generating {provided_colors}."

    return True, ""

def prompt_trigger_order(player_id, triggers):
    """Sends a PDU asking the player how to order multiple simultaneous triggers."""
    pdu = {
        "type": "TRIGGER_ORDER",
        "seq_num": get_next_seq(),
        "triggers": triggers
    }
    with client_locks:
        if player_id in clients:
            send_pdu(clients[player_id], pdu)

def prompt_trigger_choice(player_id, trigger_id, valid_targets):
    """Sends a PDU asking the player to choose targets/modes for a specific trigger."""
    pdu = {
        "type": "TRIGGER_CHOICE",
        "seq_num": get_next_seq(),
        "trigger_id": trigger_id,
        "valid_targets": valid_targets
    }
    with client_locks:
        if player_id in clients:
            send_pdu(clients[player_id], pdu)

def evaluate_triggers(event_type, event_data):
    """
    Passively scans all permanents on the battlefield for triggered abilities 
    matching the event_type, collects them, and prompts players if order is needed.
    """
    triggers_fired = 0
    collected_triggers = {} # Groups triggers by player_id
    
    # 1. Collect all valid triggers
    for player_id, permanents in game_state.get("battlefield", {}).items():
        for perm_id, details in list(permanents.items()):
            c_data = get_card_data(perm_id)
            if not c_data: 
                continue
            
            effect = get_card_field(c_data, "Simplified Effect", "Effect", "Text").lower()
            trigger_obj = None

            # --- EVENT: SPELL CAST ---
            if event_type == "CAST":
                caster = event_data.get("caster")
                spell_type = event_data.get("spell_type", "").lower()
                
                if "prowess" in effect and caster == player_id and "creature" not in spell_type and "land" not in spell_type:
                    trigger_obj = {
                        "type": "TRIGGER",
                        "trigger_type": "PROWESS",
                        "card_id": perm_id,
                        "caster": player_id,
                        "targets": []
                    }

            # --- EVENT: ETB ---
            elif event_type == "ETB":
                entering_id = event_data.get("card_id")
                pass 

            # --- EVENT: DIES / LEAVES ---
            elif event_type in ["DIES", "LEAVES"]:
                leaving_id = event_data.get("card_id")
                if "whenever a creature dies" in effect or "blood artist" in perm_id.lower():
                    trigger_obj = {
                        "type": "TRIGGER",
                        "trigger_type": "CREATURE_DIES",
                        "card_id": perm_id,
                        "caster": player_id,
                        "targets": []
                    }

            # --- EVENT: DAMAGE ---
            elif event_type == "DAMAGE":
                target_p = event_data.get("target")
                source_id = event_data.get("source")
                pass
                
            if trigger_obj:
                # If the trigger requires a target, pause and prompt the player
                if trigger_obj.get("requires_target", False):
                    # Generate a unique temporary ID for this specific trigger instance
                    temp_trigger_id = f"{perm_id}_trig_{int(time.time())}"
                    
                    # Store it in pending state instead of the stack
                    game_state.setdefault("pending_triggers", {}).setdefault(player_id, {})
                    game_state["pending_triggers"][player_id][temp_trigger_id] = trigger_obj
                    
                    # Pause standard priority and prompt the player
                    prompt_trigger_choice(player_id, temp_trigger_id, valid_targets=["opponent_1", "creature_1"])
                    vprint(f"[*] ⏸️ Pausing to ask {player_id} for trigger targets.")
                else:
                    # Normal automated trigger (like Prowess)
                    game_state["stack"].append(trigger_obj)
                    broadcast_stack_push(trigger_obj)
                    triggers_fired += 1
                    vprint(f"[*] ⚡ Trigger added to stack: {trigger_obj['trigger_type']} from {perm_id}")

    # 2. Process collected triggers per player
    for player_id, trigs in collected_triggers.items():
        if len(trigs) == 1:
            # Only one trigger fired for this player; safe to push automatically
            single_trigger = trigs[0]
            game_state["stack"].append(single_trigger)
            broadcast_stack_push(single_trigger)
            vprint(f"[*] ⚡ Trigger added to stack: {single_trigger['trigger_type']} from {single_trigger['card_id']}")
        
        elif len(trigs) > 1:
            # Multiple triggers fired! We must pause and ask the player to order them.
            game_state.setdefault("pending_triggers", {})[player_id] = {}
            trigger_data_for_prompt = {}
            
            for i, t_obj in enumerate(trigs):
                # Generate a unique temporary ID so the client can reference it in their response
                t_id = f"{t_obj['card_id']}_trig_{i}_{int(time.time())}"
                
                # Store the full object in the server's pending state
                game_state["pending_triggers"][player_id][t_id] = t_obj
                
                # Create a lightweight dictionary to send to the client
                trigger_data_for_prompt[t_id] = {
                    "card_id": t_obj["card_id"],
                    "trigger_type": t_obj["trigger_type"]
                }
            
            # Fire the prompt out to the specific client
            prompt_trigger_order(player_id, trigger_data_for_prompt)
            vprint(f"[*] ⏸️ Pausing to ask {player_id} to order {len(trigs)} simultaneous triggers.")

    return triggers_fired > 0

def resolve_top_of_stack():
    """Resolves the top item on the stack when all players pass priority in succession."""
    if not game_state["stack"]:
        return False
        
    item = game_state["stack"].pop()
    broadcast_stack_resolve(item)
    
    caster = item["caster"]
    card_id = item["card_id"]
    targets = item.get("targets", [])
    item_type = item.get("type", "SPELL")
        
    # --- FIX: Define base_id and card_type before evaluation ---
    base_id = INSTANCE_TO_BASE.get(card_id) or (card_id.rsplit('_', 1)[0].lower() if '_' in card_id else card_id.lower())
    c_data = MASTER_CARD_DB.get(base_id, {})
    card_type = get_card_field(c_data, "Card Type", "Type")
    # -----------------------------------------------------------
    
    # ==========================================
    # 0. RESOLVE AUTOMATED TRIGGERS
    # ==========================================
    if item_type == "TRIGGER":
        trigger_type = item.get("trigger_type")
        if trigger_type == "PROWESS":
            if card_id in game_state["battlefield"].get(caster, {}):
                game_state["battlefield"][caster][card_id]["buffs"]["power"] += 1
                game_state["battlefield"][caster][card_id]["buffs"]["toughness"] += 1
                vprint(f"[*] Resolved Prowess trigger for {card_id}.")
        
        broadcast_game_state_update()
        return False

    # ==========================================
    # 1. ACTIVATED ABILITIES & PERMANENT EFFECTS
    # ==========================================
    if item_type == "ABILITY":
        if base_id in ["prodigal_sorcerer", "rod_of_ruin"]:
            if targets and targets[0] in game_state["life_totals"]:
                game_state["life_totals"][targets[0]] -= 1
            elif targets:
                for p, perms in game_state["battlefield"].items():
                    if targets[0] in perms:
                        del perms[targets[0]]
                        game_state["graveyard"][p].append(targets[0])
                        evaluate_triggers("DIES", {"card_id": targets[0], "player": p}) # Hook 4: Dies integration
                        break

        elif base_id == "merfolk_looter":
            if game_state["library"][caster]:
                drawn = game_state["library"][caster].pop(0)
                game_state["hand"][caster].append(drawn)
            if game_state["hand"][caster]:
                disc = game_state["hand"][caster].pop(0)
                game_state["graveyard"][caster].append(disc)

        elif base_id == "millstone":
            target_p = targets[0] if targets else ([p for p in game_state["players"] if p != caster] + [caster])[0]
            for _ in range(2):
                if game_state["library"][target_p]:
                    milled = game_state["library"][target_p].pop(0)
                    game_state["graveyard"][target_p].append(milled)

        elif base_id == "royal_assassin":
            if targets:
                target_id = targets[0]
                for p, perms in game_state["battlefield"].items():
                    if target_id in perms and perms[target_id].get("tapped"):
                        del perms[target_id]
                        game_state["graveyard"][p].append(target_id)
                        break

        elif base_id == "mother_of_runes":
            if targets:
                target_id = targets[0]
                for p, perms in game_state["battlefield"].items():
                    if target_id in perms:
                        perms[target_id]["protection"] = "chosen_color"
                        break

        elif base_id in ["sol_ring", "llanowar_elves", "elvish_mystic"]:
            color = "colorless" if base_id == "sol_ring" else "green"
            amount = 2 if base_id == "sol_ring" else 1
            game_state.setdefault("mana_pool", {}).setdefault(caster, {})[color] = \
                game_state.setdefault("mana_pool", {}).setdefault(caster, {}).get(color, 0) + amount

    # ==========================================
    # 2. CREATURES, ARTIFACTS & ENCHANTMENTS
    # ==========================================
    elif "Creature" in card_type or "Artifact" in card_type or ("Enchantment" in card_type and "Aura" not in card_type):
        game_state["battlefield"][caster][card_id] = {
            "tapped": False,
            "summoning_sick": True,
            "buffs": {"power": 0, "toughness": 0},
            "auras": [],
            "protection": None,
            "hexproof_until_eot": False
        }

        # --- ETB (Enters The Battlefield) Triggers ---
        if base_id == "gravedigger" and targets:
            target_card = targets[0]
            if target_card in game_state["graveyard"][caster]:
                game_state["graveyard"][caster].remove(target_card)
                game_state["hand"][caster].append(target_card)

        elif base_id == "gray_merchant_of_asphodel":
            opponents = [p for p in game_state["players"] if p != caster]
            drained_total = 0
            for opp in opponents:
                game_state["life_totals"][opp] -= 2
                drained_total += 2
            game_state["life_totals"][caster] += drained_total

    # ==========================================
    # 3. INSTANTS, SORCERIES & AURAS
    # ==========================================
    else:
        # --- COUNTERSPELLS ---
        if base_id in ["counterspell", "cancel", "negate", "mana_leak"]:
            if targets:
                target_card_id = targets[0]
                target_item = next((s for s in game_state["stack"] if s.get("card_id") == target_card_id), None)
                if target_item:
                    game_state["stack"].remove(target_item)
                    target_caster = target_item["caster"]
                    game_state["graveyard"][target_caster].append(target_card_id)
                    vprint(f"[*] Countered '{target_card_id}'!")

        # --- DIRECT DAMAGE ---
        elif base_id in ["lightning_bolt", "shock", "lava_spike", "flame_slash", "searing_spear", "incinerate", "skullcrack", "rift_bolt"]:
            dmg_map = {
                "lightning_bolt": 3, "shock": 2, "lava_spike": 3, 
                "flame_slash": 4, "searing_spear": 3, "incinerate": 3, 
                "skullcrack": 3, "rift_bolt": 3
            }
            dmg = dmg_map[base_id]
            if targets and targets[0] in game_state["life_totals"]:
                game_state["life_totals"][targets[0]] -= dmg
            elif targets:
                for p, perms in game_state["battlefield"].items():
                    if targets[0] in perms:
                        del perms[targets[0]]
                        game_state["graveyard"][p].append(targets[0])
                        break

        # --- REMOVAL / DESTROY / EXILE ---
        elif base_id in ["doom_blade", "terror", "naturalize"]:
            if targets:
                target_id = targets[0]
                for p, perms in game_state["battlefield"].items():
                    if target_id in perms:
                        del perms[target_id]
                        game_state["graveyard"][p].append(target_id)
                        break

        elif base_id in ["swords_to_plowshares", "path_to_exile"]:
            if targets:
                target_id = targets[0]
                for p, perms in game_state["battlefield"].items():
                    if target_id in perms:
                        del perms[target_id]
                        if base_id == "swords_to_plowshares":
                            game_state["life_totals"][p] += 3
                        game_state.setdefault("exile", {}).setdefault(p, []).append(target_id)
                        break

        # --- BOUNCE & RECURSION ---
        elif base_id == "unsummon":
            if targets:
                target_id = targets[0]
                for p, perms in game_state["battlefield"].items():
                    if target_id in perms:
                        del perms[target_id]
                        game_state["hand"][p].append(target_id)
                        break

        elif base_id == "raise_dead":
            if targets and targets[0] in game_state["graveyard"][caster]:
                target_id = targets[0]
                game_state["graveyard"][caster].remove(target_id)
                game_state["hand"][caster].append(target_id)

        # --- BUFFS & LIFE GAIN / DISCARD ---
        elif base_id in ["giant_growth", "vines_of_vastwood"]:
            if targets:
                target_id = targets[0]
                for p, perms in game_state["battlefield"].items():
                    if target_id in perms:
                        perms[target_id]["buffs"]["power"] += 4 if base_id == "vines_of_vastwood" else 3
                        perms[target_id]["buffs"]["toughness"] += 4 if base_id == "vines_of_vastwood" else 3
                        if base_id == "vines_of_vastwood":
                            perms[target_id]["hexproof_until_eot"] = True
                        break

        elif base_id == "healing_salve":
            if targets and targets[0] in game_state["life_totals"]:
                game_state["life_totals"][targets[0]] += 3

        elif base_id == "mind_rot":
            target_p = targets[0] if targets else ([p for p in game_state["players"] if p != caster] + [caster])[0]
            for _ in range(2):
                if game_state["hand"][target_p]:
                    disc = game_state["hand"][target_p].pop(0)
                    game_state["graveyard"][target_p].append(disc)

        # --- CANT RIPS / RAMP / FAST MANA ---
        elif base_id == "ponder":
            if game_state["library"][caster]:
                drawn = game_state["library"][caster].pop(0)
                game_state["hand"][caster].append(drawn)

        elif base_id == "dark_ritual":
            game_state.setdefault("mana_pool", {}).setdefault(caster, {})["black"] = \
                game_state.setdefault("mana_pool", {}).setdefault(caster, {}).get("black", 0) + 3

        elif base_id == "rampant_growth":
            library = game_state["library"][caster]
            land_idx = next((i for i, cid in enumerate(library) if "land" in cid.lower() or "forest" in cid.lower() or "mountain" in cid.lower() or "swamp" in cid.lower() or "island" in cid.lower() or "plains" in cid.lower()), None)
            if land_idx is not None:
                land_card = library.pop(land_idx)
                game_state["battlefield"][caster][land_card] = {
                    "tapped": True,
                    "summoning_sick": False,
                    "buffs": {"power": 0, "toughness": 0},
                    "auras": []
                }

        # --- AURAS ---
        elif base_id == "pacifism":
            if targets:
                target_id = targets[0]
                for p, perms in game_state["battlefield"].items():
                    if target_id in perms:
                        perms[target_id]["auras"].append(card_id)
                        game_state["battlefield"][caster][card_id] = {"attached_to": target_id}
                        break

        # Send non-aura instants/sorceries to graveyard
        if base_id != "pacifism":
            game_state["graveyard"][caster].append(card_id)

    broadcast_game_state_update()
    
    # Check Win / Loss Conditions
    for p, life in game_state["life_totals"].items():
        if life <= 0:
            winner = [win_p for win_p in game_state["players"] if win_p != p]
            winner_id = winner[0] if winner else caster
            broadcast_game_over(winner_id, f"Player {p} reached 0 or less life.")
            return True
            
    return False

def resolve_combat():
    """Resolves declared attackers against blockers, processes combat damage, and fires triggers."""
    active_p = game_state["active_player"]
    defending_p = [p for p in game_state["players"] if p != active_p][0]

    blocked_map = {}
    damage_events = [] # <--- Initialize tracker

    for blk in game_state["combat"]["blockers"]:
        att_id = blk.get("attacker_id")
        blk_id = blk.get("blocker_id")
        blocked_map.setdefault(att_id, []).append(blk_id)

    for att in game_state["combat"]["attackers"]:
        att_id = att.get("creature_id")
        if att_id not in game_state["battlefield"][active_p]:
            continue

        att_obj = game_state["battlefield"][active_p][att_id]
        att_stats = get_card_data(att_id) or {}
        
        base_p_str = get_card_field(att_stats, "Power")
        base_t_str = get_card_field(att_stats, "Toughness")
        
        base_p = int(base_p_str) if base_p_str.isdigit() else 0
        base_t = int(base_t_str) if base_t_str.isdigit() else 0
        
        att_power = base_p + att_obj["buffs"]["power"]
        att_tough = base_t + att_obj["buffs"]["toughness"]

        if att_id not in blocked_map:
            # Player Damage
            game_state["life_totals"][defending_p] -= att_power
            if att_power > 0:
                damage_events.append({"source": att_id, "target_player": defending_p, "damage": att_power})
                evaluate_triggers("DAMAGE", {"source": att_id, "target": defending_p, "amount": att_power})
        else:
            for blk_id in blocked_map[att_id]:
                if blk_id not in game_state["battlefield"][defending_p]:
                    continue
                blk_obj = game_state["battlefield"][defending_p][blk_id]
                blk_stats = get_card_data(blk_id) or {}
                
                blk_bp_str = get_card_field(blk_stats, "Power")
                blk_bt_str = get_card_field(blk_stats, "Toughness")
                
                blk_bp = int(blk_bp_str) if blk_bp_str.isdigit() else 0
                blk_bt = int(blk_bt_str) if blk_bt_str.isdigit() else 0
                
                blk_power = blk_bp + blk_obj["buffs"]["power"]
                blk_tough = blk_bt + blk_obj["buffs"]["toughness"]

                # Creature Damage
                if att_power > 0:
                    damage_events.append({"source": att_id, "target_creature": blk_id, "damage": att_power})
                if blk_power > 0:
                    damage_events.append({"source": blk_id, "target_creature": att_id, "damage": blk_power})

                if att_power >= blk_tough:
                    del game_state["battlefield"][defending_p][blk_id]
                    game_state["graveyard"][defending_p].append(blk_id)
                    evaluate_triggers("DIES", {"card_id": blk_id, "player": defending_p})

                if blk_power >= att_tough:
                    del game_state["battlefield"][active_p][att_id]
                    game_state["graveyard"][active_p].append(att_id)
                    evaluate_triggers("DIES", {"card_id": att_id, "player": active_p})

    # Broadcast all damage before resetting combat
    if damage_events:
        broadcast_combat_damage_result(damage_events)

    game_state["combat"] = {"attackers": [], "blockers": []}
    game_state["phase"] = "POSTCOMBAT_MAIN"
    vprint(f"[*] Combat resolved. Phase changed to POSTCOMBAT_MAIN")
    broadcast_game_state_update()

    for p, life in game_state["life_totals"].items():
        if life <= 0:
            winner = [win_p for win_p in game_state["players"] if win_p != p]
            winner_id = winner[0] if winner else active_p
            broadcast_game_over(winner_id, f"Player {p} reached 0 or less life.")
            return True
            
    return False

def pass_priority():
    global priority_holder, passed_in_succession
    passed_in_succession += 1
    
    players = game_state["players"]
    next_idx = (players.index(priority_holder) + 1) % len(players)
    next_player = players[next_idx]

    if passed_in_succession >= len(players):
        passed_in_succession = 0
        
        if len(game_state["stack"]) > 0:
            game_ended = resolve_top_of_stack()
            if not game_ended:
                grant_priority(game_state["active_player"])
            return

        current_phase = game_state["phase"]
        
        # 1. Exiting Precombat Main -> Entering Combat
        if current_phase == "PRECOMBAT_MAIN":
            game_state["phase"] = "COMBAT_BEGIN"
            vprint("[*] Phase changed to COMBAT_BEGIN")
            broadcast_phase_transition("COMBAT_BEGIN", game_state["active_player"])
            broadcast_game_state_update()
            grant_priority(game_state["active_player"])
            return
            
        elif current_phase == "COMBAT_BEGIN":
            game_state["phase"] = "COMBAT_ATTACKERS"
            vprint("[*] Phase changed to COMBAT_ATTACKERS")
            broadcast_phase_transition("COMBAT_ATTACKERS", game_state["active_player"])
            broadcast_game_state_update()
            grant_priority(game_state["active_player"])
            return

        elif current_phase == "COMBAT_ATTACKERS":
            game_state["phase"] = "POSTCOMBAT_MAIN"
            vprint("[*] Phase changed to POSTCOMBAT_MAIN")
            broadcast_phase_transition("POSTCOMBAT_MAIN", game_state["active_player"])
            broadcast_game_state_update()
            grant_priority(game_state["active_player"])
            return

        # 2. Exiting Postcombat Main -> Starting a New Turn
        elif current_phase == "POSTCOMBAT_MAIN":
            active_p = game_state["active_player"]
            
            # --- NEW: Check for Hand Size ---
            if len(game_state["hand"][active_p]) > 7:
                game_state["phase"] = "CLEANUP"
                vprint(f"[*] Phase changed to CLEANUP. Waiting for {active_p} to discard.")
                broadcast_phase_transition("CLEANUP", active_p)
                broadcast_game_state_update()
                # Grant priority back to the active player so they can send the DISCARD action
                grant_priority(active_p)
                return
            else:
                current_active_idx = game_state["players"].index(game_state["active_player"])
                next_active_idx = (current_active_idx + 1) % len(game_state["players"])
                new_active_player = game_state["players"][next_active_idx]
                
                game_state["turn"] += 1
                game_state["lands_played_this_turn"] = 0
                game_state["active_player"] = new_active_player
                game_state["phase"] = "PRECOMBAT_MAIN"
                
                vprint(f"\n[*] === TURN {game_state['turn']} | Active Player: {new_active_player} ===")
                vprint("[*] Phase changed to PRECOMBAT_MAIN")
                
                # Broadcast the start of the new turn
                broadcast_phase_transition("PRECOMBAT_MAIN", new_active_player)
                
                evaluate_triggers("PHASE_BEGIN", {"phase": "PRECOMBAT_MAIN", "player": new_active_player})
                
                # Untap Step
                for card_id, obj in game_state["battlefield"].get(new_active_player, {}).items():
                    obj["tapped"] = False
                    obj["summoning_sick"] = False
                    obj["buffs"] = {"power": 0, "toughness": 0}

                # Draw Step
                if len(game_state["library"][new_active_player]) > 0:
                    drawn = game_state["library"][new_active_player].pop(0)
                    game_state["hand"][new_active_player].append(drawn)
                    vprint(f"[*] Player {new_active_player} drew a card.")
                    evaluate_triggers("DRAW", {"player": new_active_player, "card_id": drawn})
                else:
                    winner = [p for p in game_state["players"] if p != new_active_player][0]
                    broadcast_game_over(winner, f"Player {new_active_player} attempted to draw from an empty library.")
                    return

                broadcast_game_state_update()
                grant_priority(new_active_player)
                return

    grant_priority(next_player)

def dispatch_pdu(sock, player_id, pdu):
    global priority_holder, passed_in_succession
    pdu_type = pdu.get("type")
    recv_seq = pdu.get("seq_num")

    if pdu_type not in KNOWN_PDU_TYPES:
        send_error(sock, player_id, ERR_UNKNOWN_TYPE, f"PDU type '{pdu_type}' is unrecognized by server protocol.", pdu)
        return

    if pdu_type == "PING":
        send_pdu(sock, {"type": "PONG", "seq_num": recv_seq, "timestamp": pdu.get("timestamp")})
        return

    action_types = ["PRIORITY_PASS", "PLAY_LAND", "CAST_SPELL", "ACTIVATE_ABILITY", "DECLARE_ATTACKERS", "DECLARE_BLOCKERS"]
    if pdu_type in action_types:
        if priority_holder != player_id:
            send_error(sock, player_id, ERR_NOT_YOUR_PRIORITY, "You do not currently hold priority.", pdu)
            return
        if recv_seq != current_priority_seq:
            send_error(sock, player_id, ERR_STALE_ACTION, f"Stale action seq_num {recv_seq}. Expected priority seq_num is {current_priority_seq}.", pdu)
            return

    # --- PLAYER READY ---
    if pdu_type == "PLAYER_READY":
        if not player_id:
            player_id = pdu.get("player_id")

        deck = pdu.get("deck_list", [])
        if not (1 <= len(deck) <= 50):
            send_error(sock, player_id, ERR_ILLEGAL_DECK, "Deck size must be between 1 and 50 cards.", pdu)
            return

        illegal_cards = [card_id for card_id in deck if card_id not in VALID_INSTANCES]
        if illegal_cards:
            send_error(sock, player_id, ERR_ILLEGAL_DECK, f"Deck contains unauthorized cards: {illegal_cards}", pdu)
            return

        with client_locks:
            clients[player_id] = sock

        if player_id not in game_state["players"]:
            game_state["players"].append(player_id)
            game_state["life_totals"][player_id] = 20
            game_state["battlefield"][player_id] = {}
            game_state["graveyard"][player_id] = []
            game_state["exile"][player_id] = []
            
            game_state["library"][player_id] = copy.deepcopy(deck)
            random.shuffle(game_state["library"][player_id])
            
            draw_count = min(7, len(game_state["library"][player_id]))
            game_state["hand"][player_id] = [game_state["library"][player_id].pop(0) for _ in range(draw_count)]
            
            vprint(f"[*] Player '{player_id}' sent PLAYER_READY. Total ready: {len(game_state['players'])}/2")

        if len(game_state["players"]) == 2:
            first_player = random.choice(game_state["players"])
            game_state["active_player"] = first_player
            game_state["phase"] = "MULLIGAN"
            game_state["mulligans"] = {p: {"kept": False, "count": 0} for p in game_state["players"]}
            vprint("[*] Both players connected and ready! Game state updated to MULLIGAN phase.")
            broadcast_game_state_update()
        return

    # --- MULLIGAN CHOICE ---
    if pdu_type == "MULLIGAN_CHOICE":
        if game_state["phase"] != "MULLIGAN":
            send_error(sock, player_id, ERR_WRONG_PHASE, "Game is not currently in MULLIGAN phase.", pdu)
            return

        m_info = game_state["mulligans"].get(player_id)
        if not m_info or m_info.get("kept"):
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, "Player has already kept their opening hand.", pdu)
            return

        keep = pdu.get("keep", True)
        if keep:
            bottom_cards = pdu.get("cards_to_bottom", [])
            mulligan_count = m_info["count"]

            if len(bottom_cards) != mulligan_count:
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, 
                           f"Expected {mulligan_count} card(s) to bottom, but got {len(bottom_cards)}.", pdu)
                return
            
            for cid in bottom_cards:
                if cid not in game_state["hand"][player_id]:
                    send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Card '{cid}' is not in your hand.", pdu)
                    return
            
            for cid in bottom_cards:
                game_state["hand"][player_id].remove(cid)
                game_state["library"][player_id].append(cid)
                
            m_info["kept"] = True
        else:
            m_info["count"] += 1
            game_state["library"][player_id].extend(game_state["hand"][player_id])
            game_state["hand"][player_id] = []
            random.shuffle(game_state["library"][player_id])

            draw_num = min(7, len(game_state["library"][player_id]))
            game_state["hand"][player_id] = [game_state["library"][player_id].pop(0) for _ in range(draw_num)]

        all_kept = all(info.get("kept", False) for info in game_state["mulligans"].values())
        if all_kept and len(game_state["mulligans"]) == 2:
            first_player = game_state["players"][0]
            game_state["phase"] = "PRECOMBAT_MAIN"
            vprint(f"[*] Both players kept hands. Phase changed to PRECOMBAT_MAIN. Player '{first_player}' begins Turn 1.")
            broadcast_game_state_update()
            grant_priority(first_player)
        else:
            broadcast_game_state_update()
        return

    # --- PRIORITY PASS ---
    if pdu_type == "PRIORITY_PASS":
        pass_priority()
        return

    # --- PLAY LAND ---
    if pdu_type == "PLAY_LAND":
        if game_state["active_player"] != player_id or game_state["phase"] not in ["PRECOMBAT_MAIN", "POSTCOMBAT_MAIN"]:
            send_error(sock, player_id, ERR_WRONG_PHASE, "Lands can only be played during your main phases.", pdu)
            return
        if game_state["lands_played_this_turn"] >= 1:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, "Maximum land limit reached for this turn.", pdu)
            return

        card_id = pdu.get("card_id")
        if card_id not in game_state["hand"][player_id]:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Card '{card_id}' is not in your hand.", pdu)
            return

        c_data = get_card_data(card_id)
        card_type = get_card_field(c_data, "Card Type", "Type")
        if "Land" not in card_type:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Card '{card_id}' is not a valid land card.", pdu)
            return

        game_state["hand"][player_id].remove(card_id)
        game_state["battlefield"][player_id][card_id] = {
            "tapped": False,
            "summoning_sick": False,
            "buffs": {"power": 0, "toughness": 0},
            "auras": []
        }
        game_state["lands_played_this_turn"] += 1
        evaluate_triggers("ETB", {"player": player_id, "card_id": card_id, "card_type": "Land"})
        game_state["last_action"] = f"Player '{player_id}' played land: {card_id}"
        vprint(f"[*] 🌍 {game_state['last_action']}")

        broadcast_game_state_update()
        grant_priority(player_id)
        return

    # --- CAST SPELL ---
    if pdu_type == "CAST_SPELL":
        card_id = pdu.get("card_id")
        mana_payment = pdu.get("mana_payment", [])
        targets = pdu.get("targets", [])

        if card_id not in game_state["hand"].get(player_id, []):
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Card '{card_id}' is not in your hand.", pdu)
            return

        base_id = INSTANCE_TO_BASE.get(card_id) or (card_id.rsplit('_', 1)[0].lower() if '_' in card_id else card_id.lower())
        c_data = MASTER_CARD_DB.get(base_id)
        if not c_data:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Card '{card_id}' does not exist in master CSV pool.", pdu)
            return

        card_type = get_card_field(c_data, "Card Type", "Type")
        effect = get_card_field(c_data, "Simplified Effect", "Effect").lower()

        is_targeted = "target" in effect or "Aura" in card_type

        if is_targeted and len(targets) == 0:
            send_error(sock, player_id, ERR_ILLEGAL_TARGET, f"Card '{card_id}' requires a legal target, but none was provided.", pdu)
            return

        if not is_targeted and len(targets) > 0:
            send_error(sock, player_id, ERR_ILLEGAL_TARGET, f"Card '{card_id}' ({card_type}) does not take targets, but targets {targets} were provided.", pdu)
            return

        caster_battlefield = game_state["battlefield"].get(player_id, {})
        for land_id in mana_payment:
            if land_id not in caster_battlefield:
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Land '{land_id}' is not on your battlefield.", pdu)
                return
            if caster_battlefield[land_id].get("tapped", False):
                send_error(sock, player_id, ERR_INSUFFICIENT_MANA, f"Land '{land_id}' is already tapped.", pdu)
                return

        valid_mana, err_msg = validate_mana_payment(card_id, c_data, mana_payment)
        if not valid_mana:
            send_error(sock, player_id, ERR_INSUFFICIENT_MANA, err_msg, pdu)
            return

        for land_id in mana_payment:
            game_state["battlefield"][player_id][land_id]["tapped"] = True

        game_state["hand"][player_id].remove(card_id)
        
        spell_item = {
            "type": "SPELL",
            "card_id": card_id,
            "caster": player_id,
            "targets": targets
        }
        game_state["stack"].append(spell_item)
        broadcast_stack_push(spell_item)
        evaluate_triggers("CAST", {"caster": player_id, "card_id": card_id, "spell_type": card_type})
        target_str = f" targeting [{', '.join(targets)}]" if targets else ""
        game_state["last_action"] = f"Player '{player_id}' cast spell: {card_id}{target_str}"
        vprint(f"[*] ⚡ {game_state['last_action']}")

        passed_in_succession = 0
        broadcast_game_state_update()
        
        players = game_state["players"]
        next_player = players[(players.index(player_id) + 1) % len(players)]
        grant_priority(next_player)
        return

    # --- ACTIVATE ABILITY ---
    if pdu_type == "ACTIVATE_ABILITY":
        card_id = pdu.get("card_id")
        targets = pdu.get("targets", [])

        if card_id not in game_state["battlefield"].get(player_id, {}):
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Permanent '{card_id}' is not on your battlefield.", pdu)
            return

        perm = game_state["battlefield"][player_id][card_id]
        if perm.get("tapped"):
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Permanent '{card_id}' is already tapped.", pdu)
            return

        perm["tapped"] = True

        ability_item = {
            "type": "ABILITY",
            "card_id": card_id,
            "caster": player_id,
            "targets": targets
        }
        game_state["stack"].append(ability_item)
        broadcast_stack_push(ability_item)

        target_str = f" targeting [{', '.join(targets)}]" if targets else ""
        game_state["last_action"] = f"Player '{player_id}' activated ability of: {card_id}{target_str}"

        passed_in_succession = 0
        broadcast_game_state_update()

        players = game_state["players"]
        next_player = players[(players.index(player_id) + 1) % len(players)]
        grant_priority(next_player)
        return

    # --- DISCARD (Cleanup Phase) ---
    if pdu_type == "DISCARD":
        if game_state["phase"] != "CLEANUP":
            send_error(sock, player_id, ERR_WRONG_PHASE, "You can only discard for hand size during the CLEANUP phase.", pdu)
            return
        if game_state["active_player"] != player_id:
            send_error(sock, player_id, ERR_NOT_YOUR_PRIORITY, "It is not your turn to discard.", pdu)
            return

        cards_to_discard = pdu.get("cards", [])
        hand = game_state["hand"][player_id]
        required_discards = len(hand) - 7

        # Validate they actually need to discard
        if required_discards <= 0:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, "You do not have more than 7 cards and cannot discard.", pdu)
            return
            
        # Validate they provided the exact correct amount
        if len(cards_to_discard) != required_discards:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"You must discard exactly {required_discards} cards.", pdu)
            return
            
        # Validate they actually own the cards
        for cid in cards_to_discard:
            if cid not in hand:
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Card '{cid}' is not in your hand.", pdu)
                return

        # Execute discard
        for cid in cards_to_discard:
            hand.remove(cid)
            game_state["graveyard"][player_id].append(cid)
            
        game_state["last_action"] = f"Player '{player_id}' discarded {len(cards_to_discard)} card(s) to maximum hand size."
        vprint(f"[*] 🗑️ {game_state['last_action']}")
        
        # After discarding, officially pass the turn to the next player
        # (This triggers the same logic that used to sit at the end of POSTCOMBAT_MAIN)
        current_active_idx = game_state["players"].index(game_state["active_player"])
        next_active_idx = (current_active_idx + 1) % len(game_state["players"])
        new_active_player = game_state["players"][next_active_idx]
        
        game_state["turn"] += 1
        game_state["lands_played_this_turn"] = 0
        game_state["active_player"] = new_active_player
        game_state["phase"] = "PRECOMBAT_MAIN"
        
        vprint(f"\n[*] === TURN {game_state['turn']} | Active Player: {new_active_player} ===")
        broadcast_phase_transition("PRECOMBAT_MAIN", new_active_player)
        
        # Untap & Draw (from your existing pass_priority logic)
        for card_id, obj in game_state["battlefield"].get(new_active_player, {}).items():
            obj["tapped"] = False
            obj["summoning_sick"] = False
            obj["buffs"] = {"power": 0, "toughness": 0}

        if len(game_state["library"][new_active_player]) > 0:
            drawn = game_state["library"][new_active_player].pop(0)
            game_state["hand"][new_active_player].append(drawn)
        else:
            winner = [p for p in game_state["players"] if p != new_active_player][0]
            broadcast_game_over(winner, f"Player {new_active_player} attempted to draw from an empty library.")
            return

        broadcast_game_state_update()
        grant_priority(new_active_player)
        return

    # --- DECLARE ATTACKERS ---
    if pdu_type == "DECLARE_ATTACKERS":
        if game_state["active_player"] != player_id:
            send_error(sock, player_id, ERR_NOT_YOUR_PRIORITY, "Only active player can declare attackers.", pdu)
            return

        if game_state["phase"] not in ["COMBAT_BEGIN", "COMBAT_ATTACKERS"]:
            send_error(sock, player_id, ERR_WRONG_PHASE, "Cannot declare attackers outside combat phase.", pdu)
            return
            
        attackers = pdu.get("attackers", [])
        att_summary = []

        for att in attackers:
            cid = att.get("creature_id")
            target = att.get("target")
            creature = game_state["battlefield"][player_id].get(cid)
            c_data = get_card_data(cid) or {}
            effect = get_card_field(c_data, "Simplified Effect", "Effect")
            
            if not creature:
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Attacking creature '{cid}' not on battlefield.", pdu)
                return
            if creature.get("tapped"):
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Creature '{cid}' is already tapped.", pdu)
                return
            if creature.get("summoning_sick") and "Haste" not in effect:
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Creature '{cid}' has summoning sickness and cannot attack this turn.", pdu)
                return

            target_str = f" targeting {target}" if target else ""
            att_summary.append(f"{cid}{target_str}")

        for att in attackers:
            cid = att.get("creature_id")
            c_data = get_card_data(cid) or {}
            effect = get_card_field(c_data, "Simplified Effect", "Effect")
            if "Vigilance" not in effect:
                game_state["battlefield"][player_id][cid]["tapped"] = True

        game_state["combat"]["attackers"] = attackers

        if att_summary:
            game_state["last_action"] = f"Player '{player_id}' declared attackers: {', '.join(att_summary)}"
        else:
            game_state["last_action"] = f"Player '{player_id}' declared NO attackers."
        
        if len(attackers) > 0:
            game_state["phase"] = "COMBAT_BLOCKERS"
            vprint(f"[*] Phase changed to COMBAT_BLOCKERS")
            defending_player = [p for p in game_state["players"] if p != player_id][0]
            passed_in_succession = 0
            broadcast_game_state_update()
            grant_priority(defending_player)
        else:
            game_state["phase"] = "POSTCOMBAT_MAIN"
            vprint(f"[*] Phase changed to POSTCOMBAT_MAIN")
            passed_in_succession = 0
            broadcast_game_state_update()
            grant_priority(player_id)
        return

    # --- DECLARE BLOCKERS ---
    if pdu_type == "DECLARE_BLOCKERS":
        if game_state["phase"] != "COMBAT_BLOCKERS":
            send_error(sock, player_id, ERR_WRONG_PHASE, "Cannot declare blockers outside COMBAT_BLOCKERS phase.", pdu)
            return

        blockers = pdu.get("blockers", [])
        blk_summary = []

        for blk in blockers:
            b_id = blk.get("blocker_id")
            att_id = blk.get("attacker_id")
            b_obj = game_state["battlefield"][player_id].get(b_id)
            
            if not b_obj:
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Blocker '{b_id}' not on battlefield.", pdu)
                return
            if b_obj.get("tapped"):
                send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Blocker '{b_id}' is tapped.", pdu)
                return

            blk_summary.append(f"{b_id} 🛡️ blocking {att_id}")

        game_state["combat"]["blockers"] = blockers
        passed_in_succession = 0

        if blk_summary:
            game_state["last_action"] = f"Player '{player_id}' declared blockers: {', '.join(blk_summary)}"
        else:
            game_state["last_action"] = f"Player '{player_id}' declared NO blockers."
        
        broadcast_game_state_update()
        grant_priority(game_state["active_player"])
        return

    # --- ASSIGN DAMAGE ORDER ---
    if pdu_type == "ASSIGN_DAMAGE_ORDER":
        if game_state["phase"] != "COMBAT_DAMAGE_ORDER":
            send_error(sock, player_id, ERR_WRONG_PHASE, "You can only assign damage order during the DAMAGE_ORDER phase.", pdu)
            return
        if game_state["active_player"] != player_id:
            send_error(sock, player_id, ERR_NOT_YOUR_PRIORITY, "Only the attacking player can assign damage order.", pdu)
            return

        # Expected payload structure: [{"attacker_id": "goblin_guide_1", "blocker_order": ["defender_1", "defender_2"]}]
        orders = pdu.get("orders", []) 
        
        # Store the requested damage sequence in the combat state
        game_state["combat"]["damage_orders"] = orders
        game_state["last_action"] = f"Player '{player_id}' assigned combat damage orders."
        vprint(f"[*] ⚔️ {game_state['last_action']}")
        
        # Advance directly to the combat resolution calculation
        game_state["phase"] = "COMBAT_DAMAGE"
        
        # Since damage orders are set, we immediately calculate and apply damage
        game_ended = resolve_combat()
        
        # If combat did not end the game, resolve_combat() handles the transition to POSTCOMBAT_MAIN
        if not game_ended:
            grant_priority(game_state["active_player"])
        return

    # --- TRIGGER ORDER RESPONSE ---
    if pdu_type == "TRIGGER_ORDER_RESPONSE":
        ordered_trigger_ids = pdu.get("ordered_ids", [])
        
        # Verify the player actually has pending triggers
        if player_id not in game_state["pending_triggers"] or not game_state["pending_triggers"][player_id]:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, "You have no pending triggers to order.", pdu)
            return

        pending = game_state["pending_triggers"][player_id]
        
        # Validate that the response contains the exact triggers we are waiting for
        if set(ordered_trigger_ids) != set(pending.keys()):
            send_error(sock, player_id, ERR_TRIGGER_ORDER_INVALID, "Provided order does not match pending triggers.", pdu)
            return

        # Push them to the stack in the requested order
        for t_id in ordered_trigger_ids:
            trigger_obj = pending[t_id]
            game_state["stack"].append(trigger_obj)
            broadcast_stack_push(trigger_obj)
            vprint(f"[*] ⚡ Trigger added to stack (Ordered): {trigger_obj['trigger_type']}")
            
        # Clear the pending queue and resume game flow
        game_state["pending_triggers"][player_id] = {}
        broadcast_game_state_update()
        grant_priority(game_state["active_player"])
        return

    # --- TRIGGER CHOICE RESPONSE ---
    if pdu_type == "TRIGGER_CHOICE_RESPONSE":
        trigger_id = pdu.get("trigger_id")
        chosen_targets = pdu.get("targets", [])
        
        if player_id not in game_state["pending_triggers"] or trigger_id not in game_state["pending_triggers"][player_id]:
            send_error(sock, player_id, ERR_ILLEGAL_ACTION, f"Trigger '{trigger_id}' is not pending for you.", pdu)
            return

        # Retrieve the pending trigger and assign the chosen targets
        trigger_obj = game_state["pending_triggers"][player_id].pop(trigger_id)
        trigger_obj["targets"] = chosen_targets
        
        # Push to stack and resume
        game_state["stack"].append(trigger_obj)
        broadcast_stack_push(trigger_obj)
        vprint(f"[*] ⚡ Trigger added to stack with targets {chosen_targets}: {trigger_obj['trigger_type']}")
        
        broadcast_game_state_update()
        grant_priority(game_state["active_player"])
        return

    # --- CONCEDE ---
    if pdu_type == "CONCEDE":
        winner = [p for p in game_state["players"] if p != player_id]
        winner_id = winner[0] if winner else "None"
        broadcast_game_over(winner_id, f"Player {player_id} conceded.")
        return

def handle_client(sock, addr):
    player_id = None
    buffer = ""
    vprint(f"[*] New TCP connection accepted from {addr}")
    
    # --- NEW: Set a short 1-second timeout to allow continuous time-checking ---
    sock.settimeout(1.0)
    
    try:
        while True:
            try:
                data = sock.recv(4096)
                if not data:
                    break
                buffer += data.decode('utf-8')

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    if not line.strip(): 
                        continue
                    
                    try:
                        pdu = json.loads(line)
                    except Exception:
                        if player_id:
                            send_error(sock, player_id, ERR_INVALID_JSON, "Payload failed JSON parsing.")
                        continue

                    if player_id is None and pdu.get("player_id"):
                        player_id = pdu["player_id"]

                    dispatch_pdu(sock, player_id, pdu)

            except socket.timeout:
                # --- NEW: Check the AFK Turn Timer every 1 second ---
                # Only apply the timer if this player currently holds priority
                if player_id and priority_holder == player_id:
                    p_time = game_state.get("priority_time")
                    if p_time and (time.time() - p_time > 30.0):
                        print(f"[!] ⏱️ Player {player_id} ran out of time to make a decision.")
                        
                        winner = [p for p in game_state.get("players", []) if p != player_id]
                        winner_id = winner[0] if winner else "None"
                        broadcast_game_over(winner_id, f"Player {player_id} forfeited due to 30 seconds of inactivity.")
                
                # If they aren't timed out, just continue the loop
                continue

    except Exception as e:
        print(f"[!] Client disconnect ({player_id or addr}): {e}")
    finally:
        with client_locks:
            if player_id and player_id in clients:
                del clients[player_id]
        sock.close()

def main():
    global VERBOSE
    
    parser = argparse.ArgumentParser(description="Start the MTGNP Game Server.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose debug logging.")
    args = parser.parse_args()
    
    VERBOSE = args.verbose
    
    load_card_databases()
    reset_game_state()
    
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((HOST, PORT))
    server.listen(5)
    
    print(f"[*] MTGNP Server online on {HOST}:{PORT}")
    if VERBOSE:
        print("[*] Verbose logging is enabled.")

    while True:
        sock, addr = server.accept()
        threading.Thread(target=handle_client, args=(sock, addr), daemon=True).start()

if __name__ == "__main__":
    main()