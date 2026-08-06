# game_logic.py - Segment 1 of 3
import random
import csv
from game_state import GameState, PlayerState

class GameLogic:
    def __init__(self):
        self.state = GameState()
        self.valid_cards = self._load_card_database("mtgnp_master_card_list - Master Card List.csv")
        self.last_grant_or_request_seq = 0
        self.player_expected_mulligan_seq = {}  # Tracks individual tokens per player  
        
        # In-order turn sequence progression (Section 7.1)
        self.phase_order = [
            "UNTAP", "UPKEEP", "DRAW", "PRECOMBAT_MAIN",
            "BEGIN_COMBAT", "DECLARE_ATTACKERS", "DECLARE_BLOCKERS", 
            "ASSIGN_DAMAGE_ORDER", "COMBAT_DAMAGE", "END_OF_COMBAT",
            "POSTCOMBAT_MAIN", "END_STEP", "CLEANUP"
        ]

    def _load_card_database(self, filepath: str) -> dict:
        """Loads and returns the entire card profile map from the master catalog CSV."""
        catalog = {}
        try:
            with open(filepath, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    card_id = row.get('Card ID Base', '').strip()
                    if card_id:
                        # Extract row strings and clean out non-numeric hyphen characters safely
                        raw_power = row.get('Power', '').strip()
                        raw_toughness = row.get('Toughness', '').strip()
                        
                        power_stat = int(raw_power) if raw_power and raw_power != '-' else 0
                        toughness_stat = int(raw_toughness) if raw_toughness and raw_toughness != '-' else 0

                        catalog[card_id] = {
                            "name": row.get('Card Name', '').strip(),
                            "type": row.get('Card Type', '').strip(),
                            "color": row.get('Color', '').strip(),
                            "power": power_stat,
                            "toughness": toughness_stat,
                            "cmc": int(row.get('CMC', 0) or 0),
                            "mana_cost": {
                                "W": int(row.get('W', 0) or 0),
                                "U": int(row.get('U', 0) or 0),
                                "B": int(row.get('B', 0) or 0),
                                "R": int(row.get('R', 0) or 0),
                                "G": int(row.get('G', 0) or 0),
                                "X": int(row.get('Generic', 0) or 0)
                            }
                        }
            return catalog
        except FileNotFoundError:
            print(f"Warning: {filepath} not found. Loading complete fallback catalogue data.")
            return {
                "mountain": {"name": "Mountain", "type": "Land", "mana_cost": {}, "power": 0, "toughness": 0},
                "swamp": {"name": "Swamp", "type": "Land", "mana_cost": {}, "power": 0, "toughness": 0},
                "lightning_bolt": {"name": "Lightning Bolt", "type": "Instant", "mana_cost": {"R": 1}, "power": 0, "toughness": 0},
                "goblin_guide": {"name": "Goblin Guide", "type": "Creature", "mana_cost": {"R": 1}, "power": 2, "toughness": 2},
                "black_knight": {"name": "Black Knight", "type": "Creature", "mana_cost": {"B": 2}, "power": 2, "toughness": 2}
            }


    def next_seq(self):
        self.state.seq_num += 1
        return self.state.seq_num

    def process_pdu(self, client_id: str, pdu: dict) -> list:
        """Main router for incoming player messages with automatic priority re-grant recovery."""
        pdu_type = pdu.get("type")
        incoming_seq = pdu.get("seq_num")

        if pdu_type in ["PLAYER_READY", "PING", "CONCEDE"]:
            pass
        elif pdu_type == "MULLIGAN_CHOICE":
            # Section 5.4: Validate against the specific PDU sequence sent to this player
            expected = self.player_expected_mulligan_seq.get(client_id, 0)
            if incoming_seq != expected:
                return [(client_id, {
                    "type": "ERROR",
                    "seq_num": self.next_seq(),
                    "code": "STALE_ACTION",
                    "message": f"Mulligan token mismatch. Expected {expected}, got {incoming_seq}.",
                    "rejected_action": pdu
                })]
        else:
            # Standard in-game global priority validation
            if incoming_seq != self.last_grant_or_request_seq:
                return [(client_id, {
                    "type": "ERROR",
                    "seq_num": self.next_seq(),
                    "code": "STALE_ACTION",
                    "message": f"Priority token mismatch. Expected {self.last_grant_or_request_seq}, got {incoming_seq}.",
                    "rejected_action": pdu
                })]

        outbound = []

        if self.state.phase == "LOBBY":
            if pdu_type == "PLAYER_READY":
                return self.handle_player_ready(client_id, pdu)
        elif self.state.phase == "MULLIGAN":
            if pdu_type == "MULLIGAN_CHOICE":
                return self.handle_mulligan_choice(client_id, pdu)
        else:
            if pdu_type == "PRIORITY_PASS":
                outbound = self.handle_priority_pass(client_id, pdu)
            elif pdu_type == "PLAY_LAND":
                outbound = self.handle_play_land(client_id, pdu)
            elif pdu_type == "CAST_SPELL":
                outbound = self.handle_cast_spell(client_id, pdu)
            elif pdu_type == "DECLARE_ATTACKERS":
                outbound = self.handle_declare_attackers(client_id, pdu)
            elif pdu_type == "DECLARE_BLOCKERS":
                outbound = self.handle_declare_blockers(client_id, pdu)
            elif pdu_type == "ASSIGN_DAMAGE_ORDER":
                outbound = self.handle_assign_damage_order(client_id, pdu)
            elif pdu_type == "CONCEDE":
                return self.handle_concede(client_id, pdu)
            elif pdu_type == "DISCARD" and self.state.phase == "CLEANUP":
                return self.handle_cleanup_discard(client_id, pdu)
            else:
                outbound = [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "UNKNOWN_TYPE", "message": "Invalid action or phase action pattern."})]

        # Section 11 Rule: If an action resulted in an ERROR,
        # automatically re-grant priority to the current player so the client loop doesn't freeze.
        if outbound and len(outbound) == 1:
            target, msg = outbound[0]
            if msg.get("type") == "ERROR":
                # Append the required recovery priority grant token
                outbound.extend(self.grant_priority_to_current())
        
        return outbound

    def check_state_based_actions(self) -> list:
        """Checks for player elimination conditions after every game event (Section 8.4)."""
        outbound = []
        dead_players = [p_id for p_id, p_state in self.state.players.items() if p_state.life <= 0]
        
        if dead_players:
            self.state.phase = "GAME_OVER"
            if len(dead_players) == 2:
                loser_id = self.state.active_player
                winner_id = next(p for p in self.state.players.keys() if p != loser_id)
            else:
                loser_id = dead_players[0]
                winner_id = next(p for p in self.state.players.keys() if p != loser_id)
                
            outbound.append(("ALL", {
                "type": "GAME_OVER",
                "seq_num": self.next_seq(),
                "winner_id": winner_id,
                "loser_id": loser_id,
                "reason": "LIFE_ZERO"
            }))
        return outbound
# game_logic.py - Segment 2 of 3

    def handle_player_ready(self, client_id: str, pdu: dict) -> list:
        player_id = pdu.get("player_id", client_id)
        deck_list = pdu.get("deck_list", [])

        if not (1 <= len(deck_list) <= 50):
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_DECK", "message": "Deck must contain 1 to 50 cards."})]

        if player_id not in self.state.players:
            p_state = PlayerState(player_id=player_id, library=list(deck_list))
            self.state.players[player_id] = p_state
        
        outbound = []
        if len(self.state.players) == 2:
            outbound.extend(self.start_game_setup())
        else:
            self.state.waiting_for = [p for p in ["player_1", "player_2"] if p != player_id]
            self.state.seq_num += 1
            outbound.append((client_id, self.state.generate_lobby_update()))
            
        return outbound

    def start_game_setup(self) -> list:
        outbound = []
        player_ids = list(self.state.players.keys())
        
        self.state.active_player = random.choice(player_ids)
        self.state.priority_holder = self.state.active_player
        self.state.turn = 0
        self.state.phase = "MULLIGAN"

        for p_id, p_state in self.state.players.items():
            p_state.life = 20
            p_state.has_kept = False
            p_state.mulligan_count = 0
            random.shuffle(p_state.library)
            p_state.hand = [p_state.library.pop() for _ in range(7) if p_state.library]

        # Record the current sequence number for both players
        initial_mulligan_seq = self.next_seq()
        self.last_grant_or_request_seq = initial_mulligan_seq
        
        for p_id in player_ids:
            self.player_expected_mulligan_seq[p_id] = initial_mulligan_seq
            msg = self.generate_custom_state_update(p_id)
            msg["seq_num"] = initial_mulligan_seq
            outbound.append((p_id, msg))
            
        return outbound

    def handle_mulligan_choice(self, client_id: str, pdu: dict) -> list:
        p_state = self.state.players.get(client_id)
        keep = pdu.get("keep", True)
        cards_to_bottom = pdu.get("cards_to_bottom", [])

        if not keep:
            p_state.library.extend(p_state.hand)
            p_state.hand = []
            random.shuffle(p_state.library)
            p_state.mulligan_count += 1
            p_state.hand = [p_state.library.pop() for _ in range(7) if p_state.library]
            p_state.has_kept = False
        else:
            if len(cards_to_bottom) != p_state.mulligan_count:
                return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": "Incorrect cards to bottom count."})]
            
            for card in cards_to_bottom:
                if card in p_state.hand:
                    p_state.hand.remove(card)
                    p_state.library.insert(0, card)
            p_state.has_kept = True

        all_kept = all(p.has_kept for p in self.state.players.values())
        outbound = []
        
        if all_kept:
            self.last_grant_or_request_seq = self.next_seq()
            self.state.turn = 1
            outbound.extend(self.enter_phase("UNTAP"))
        else:
            # Generate a unique update sequence number just for the player who requested a redraw
            next_redraw_seq = self.next_seq()
            self.player_expected_mulligan_seq[client_id] = next_redraw_seq
            self.last_grant_or_request_seq = next_redraw_seq
            
            msg = self.generate_custom_state_update(client_id)
            msg["seq_num"] = next_redraw_seq
            outbound.append((client_id, msg))

        return outbound
# game_logic.py - Segment 3 of 3

    def handle_priority_pass(self, client_id: str, pdu: dict) -> list:
        if self.state.priority_holder != client_id:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "NOT_YOUR_PRIORITY", "message": "You do not hold priority."})]

        outbound = []
        opp_id = next(p for p in self.state.players.keys() if p != client_id)
        self.state.consecutive_passes += 1
        
        if self.state.consecutive_passes >= 2:
            self.state.consecutive_passes = 0
            if self.state.stack:
                resolved_item = self.state.stack.pop()
                outbound.extend(self.resolve_top_stack_item(resolved_item))
                self.last_grant_or_request_seq = self.next_seq()
                outbound.append(("ALL", {
                    "type": "STACK_RESOLVE",
                    "seq_num": self.last_grant_or_request_seq,
                    "stack_item_id": resolved_item.get("stack_item_id"),
                    "result": "RESOLVED",
                    "state_changes": []
                }))
                
                sba_messages = self.check_state_based_actions()
                if sba_messages:
                    return sba_messages
                
                self.state.priority_holder = self.state.active_player
                outbound.extend(self.grant_priority_to_current())
            else:
                outbound.extend(self.advance_turn_phase())
        else:
            self.state.priority_holder = opp_id
            outbound.extend(self.grant_priority_to_current())
            
        return outbound

    def advance_turn_phase(self) -> list:
        """Determines the next logical phase/step within the state machine sequence (Section 7.1)."""
        current_index = self.phase_order.index(self.state.phase)
        
        if self.state.phase == "CLEANUP":
            self.state.turn += 1
            self.state.active_player = next(p for p in self.state.players.keys() if p != self.state.active_player)
            next_phase = "UNTAP"
        else:
            next_phase = self.phase_order[current_index + 1]

        return self.enter_phase(next_phase)

    def enter_phase(self, phase_name: str) -> list:
        """Executes phase-specific logic and broadcasts phase transition updates."""
        outbound = []
        from_phase = self.state.phase
        self.state.phase = phase_name
        self.state.consecutive_passes = 0

        self.last_grant_or_request_seq = self.next_seq()
        outbound.append(("ALL", {
            "type": "PHASE_TRANSITION",
            "seq_num": self.last_grant_or_request_seq,
            "from_phase": from_phase,
            "to_phase": phase_name,
            "active_player": self.state.active_player,
            "turn": self.state.turn
        }))

        if phase_name == "UNTAP":
            ap_state = self.state.players[self.state.active_player]
            ap_state.land_played_this_turn = False
            for perm in ap_state.battlefield:
                perm["tapped"] = False

            outbound.extend(self.broadcast_state_updates())
            return outbound + self.enter_phase("UPKEEP")

        elif phase_name == "DRAW":
            ap_state = self.state.players[self.state.active_player]
            player_ids = list(self.state.players.keys())

            # Play-draw rule skip validation on turn 1 (Section 7.4)
            if self.state.turn == 1 and self.state.active_player == player_ids[0]:
                pass
            else:
                if not ap_state.library:
                    self.state.phase = "GAME_OVER"
                    winner_id = next(p for p in self.state.players.keys() if p != self.state.active_player)
                    return [("ALL", {
                        "type": "GAME_OVER",
                        "seq_num": self.next_seq(),
                        "winner_id": winner_id,
                        "loser_id": self.state.active_player,
                        "reason": "DECK_EMPTY"
                    })]
                ap_state.hand.append(ap_state.library.pop())

            outbound.extend(self.broadcast_state_updates())

        elif phase_name == "CLEANUP":
            ap_state = self.state.players[self.state.active_player]
            if len(ap_state.hand) > 7:
                outbound.extend(self.broadcast_state_updates())
                return outbound

            for p_state in self.state.players.values():
                for perm in p_state.battlefield:
                    perm["damage"] = 0
            outbound.extend(self.broadcast_state_updates())
            return outbound + self.advance_turn_phase()

        elif phase_name == "COMBAT_DAMAGE":
            # Direct calculation dispatch for automatic combat damage distribution (Section 9.7)
            return outbound + self.resolve_combat_damage_event()

        # Phase rules requiring user interactions grant priority to active player first
        self.state.priority_holder = self.state.active_player
        outbound.extend(self.grant_priority_to_current())
        return outbound

    def handle_cleanup_discard(self, client_id: str, pdu: dict) -> list:
        if client_id != self.state.active_player:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": "Only the active player discards during cleanup."})]

        ap_state = self.state.players[self.state.active_player]
        card_ids = pdu.get("card_ids", [])

        for cid in card_ids:
            if cid in ap_state.hand:
                ap_state.hand.remove(cid)
                ap_state.graveyard.append(cid)
            else:
                return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": f"Card {cid} not in hand."})]

        outbound = []
        if len(ap_state.hand) > 7:
            outbound.extend(self.broadcast_state_updates())
            return outbound

        for p_state in self.state.players.values():
            for perm in p_state.battlefield:
                perm["damage"] = 0
        outbound.extend(self.broadcast_state_updates())
        return outbound + self.advance_turn_phase()

    def grant_priority_to_current(self) -> list:
        if self.state.priority_holder is None:
            return []
        return [(self.state.priority_holder, {
            "type": "PRIORITY_GRANT",
            "seq_num": self.last_grant_or_request_seq,
            "player_id": self.state.priority_holder,
            "time_limit_ms": 60000
            })]

    def broadcast_state_updates(self) -> list:
        outbound = []
        for p_id in self.state.players.keys():
            outbound.append((p_id, self.generate_custom_state_update(p_id)))
        return outbound

    def handle_concede(self, client_id: str, pdu: dict) -> list:
        opp_id = next(p for p in self.state.players.keys() if p != client_id)
        self.state.phase = "GAME_OVER"
        return [("ALL", {
            "type": "GAME_OVER",
            "seq_num": self.next_seq(),
            "winner_id": opp_id,
            "loser_id": client_id,
            "reason": "CONCEDE"
            })]

    def generate_custom_state_update(self, player_id: str) -> dict:
        """Generates custom state update including the private player-specific mulligan_count."""
        base_update = self.state.generate_personalized_update(player_id)
        p_state = self.state.players.get(player_id)
        if p_state and "state" in base_update:
            # Explicitly bind the local viewing player's mulligan count tracking metrics
            base_update["state"]["mulligan_count"] = getattr(p_state, 'mulligan_count', 0)
        return base_update
    
    def handle_play_land(self, client_id: str, pdu: dict) -> list:
        """Validates and executes playing a land card from hand (Section 7.5)."""
        if self.state.priority_holder != client_id:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "NOT_YOUR_PRIORITY", "message": "You do not hold priority."})]

        # Section 7.1: Lands can only be played at sorcery speed (Main Phases, empty stack)
        if self.state.phase not in ["PRECOMBAT_MAIN", "POSTCOMBAT_MAIN"]:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "WRONG_PHASE", "message": "Lands can only be played during Main Phases."})]
        if self.state.stack:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": "Cannot play a land while the stack is non-empty."})]

        ap_state = self.state.players[client_id]
        
        # Section 7.5: Enforce limit of playing max one land per turn
        if ap_state.land_played_this_turn:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": "You have already played a land this turn."})]

        card_id = pdu.get("card_id")
        if card_id not in ap_state.hand:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": f"Card '{card_id}' not found in your hand."})]

        # Map base card item back to raw master card catalog profile to confirm type matches
        base_catalog_id = card_id.split('_')[0] if '_' in card_id else card_id
        card_profile = self.valid_cards.get(base_catalog_id)
        
        if not card_profile or card_profile["type"] != "Land":
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": "Selected card is not a valid Land permanent."})]

        # Execute placement: Move from hand to battlefield zone
        ap_state.hand.remove(card_id)
        land_permanent = {
            "id": card_id,
            "name": card_profile["name"],
            "type": "Land",
            "tapped": False
        }
        ap_state.battlefield.append(land_permanent)
        ap_state.land_played_this_turn = True

        # Clear consecutive pass parameters since a major state change action was evaluated
        self.state.consecutive_passes = 0
        
        # Broadcast personalized updates, then re-grant priority token to Active Player (Section 7.5)
        outbound = self.broadcast_state_updates()
        outbound.extend(self.grant_priority_to_current())
        return outbound

    def handle_cast_spell(self, client_id: str, pdu: dict) -> list:
        """Validates spell parameters, checks timing/costs, and pushes to stack (Section 7.5)."""
        if self.state.priority_holder != client_id:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "NOT_YOUR_PRIORITY", "message": "You do not hold priority."})]

        card_instance_id = pdu.get("card_id")
        ap_state = self.state.players[client_id]

        if card_instance_id not in ap_state.hand:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": "Card not in hand."})]

        # Lookup card details from the parsed catalog database
        base_catalog_id = card_instance_id.split('_')[0] if '_' in card_instance_id else card_instance_id
        card_profile = self.valid_cards.get(base_catalog_id)

        if not card_profile:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": "Unknown card catalog entry."})]

        # Section 7.5: Enforce Sorcery vs Instant speed timing constraints
        card_type = card_profile["type"]
        if card_type in ["Sorcery", "Creature", "Artifact", "Enchantment"]:
            if self.state.phase not in ["PRECOMBAT_MAIN", "POSTCOMBAT_MAIN"] or self.state.active_player != client_id or self.state.stack:
                return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "WRONG_PHASE", "message": "Sorcery-speed spells can only be cast during your Main Phase on an empty stack."})]

        # Section 7.5: Implicit Mana Payment Deduction Rules
        declared_payment = pdu.get("mana_payment", {})
        required_cost = card_profile["mana_cost"]
        
        # Verify that the declared payment meets the card catalog requirement
        for color, amt in required_cost.items():
            if color != "X" and declared_payment.get(color, 0) < amt:
                return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "INSUFFICIENT_MANA", "message": "Declared payment does not meet card mana cost."})]

        # Verify that the player actually controls enough untapped lands matching the declaration
        available_lands = [perm for perm in ap_state.battlefield if perm["type"] == "Land" and not perm.get("tapped", False)]
        total_payment_needed = sum(declared_payment.values())

        if len(available_lands) < total_payment_needed:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "INSUFFICIENT_MANA", "message": "Not enough untapped mana sources available."})]

        # Deduct payment implicitly by tapping lands (Section 7.5)
        for _ in range(total_payment_needed):
            land_to_tap = available_lands.pop()
            land_to_tap["tapped"] = True

        # Cast Successful -> Remove from hand, create unique item ID, push to LIFO Stack (Section 8.3)
        ap_state.hand.remove(card_instance_id)
        stack_item_id = f"stk_{len(self.state.stack) + 1:02d}"
        
        stack_item = {
            "stack_item_id": stack_item_id,
            "item_type": "SPELL",
            "source": card_instance_id,
            "controller": client_id,
            "targets": pdu.get("targets", [])
        }
        self.state.stack.append(stack_item)
        self.state.consecutive_passes = 0  # Reset priority pass chain

        # Broadcast STACK_PUSH (Section 10.2.9)
        self.last_grant_or_request_seq = self.next_seq()
        outbound = [("ALL", {
            "type": "STACK_PUSH",
            "seq_num": self.last_grant_or_request_seq,
            "stack_item_id": stack_item_id,
            "item_type": "SPELL",
            "source": card_instance_id,
            "controller": client_id,
            "targets": pdu.get("targets", [])
        })]

        # Section 8.1 rule 3: Caster retains priority after adding an item to the stack
        outbound.extend(self.broadcast_state_updates())
        outbound.extend(self.grant_priority_to_current())
        return outbound

    def resolve_top_stack_item(self, resolved_item: dict) -> list:
        """Processes card effects when leaving the stack, supporting Creatures (Section 8.4)."""
        outbound = []
        source_card_id = resolved_item.get("source", "")
        controller_id = resolved_item.get("controller")
        targets = resolved_item.get("targets", [])
        
        base_id = source_card_id.split('_')[0] if '_' in source_card_id else source_card_id
        card_profile = self.valid_cards.get(base_id)
        state_changes = []
        main_target = targets[0] if targets else None

        if not card_profile:
            # Default fallback cleanup safety pop
            self.state.players[controller_id].graveyard.append(source_card_id)
            return outbound

        card_type = card_profile.get("type")

        # --- Handle Instant / Sorcery Spells ---
        if card_type in ["Instant", "Sorcery"]:
            if base_id in ["lightning_bolt", "shock", "searing_spear"]:
                damage_amount = 3 if base_id in ["lightning_bolt", "searing_spear"] else 2
                if main_target in self.state.players:
                    self.state.players[main_target].life -= damage_amount
                    state_changes.append({"change_type": "DAMAGE", "target": main_target, "amount": damage_amount})
                    
            elif base_id == "healing_salve":
                if main_target in self.state.players:
                    self.state.players[main_target].life += 3
                    state_changes.append({"change_type": "LIFE_GAIN", "target": main_target, "amount": 3})

            # Instants and Sorceries go straight to the graveyard on resolution
            self.state.players[controller_id].graveyard.append(source_card_id)

        # --- Handle Creature Permanent Spells (Section 3 / 9.3) ---
        elif card_type == "Creature":
            # Extract power/toughness from catalog row definitions (Pages 1-3)
            # Default to 1/1 if row entries omit baseline properties
            power_stat = card_profile.get("power", 1)
            toughness_stat = card_profile.get("toughness", 1)
            
            # Check for Haste trait attributes in card design definitions
            has_haste = base_id in ["goblin_guide", "monastery_swiftspear"]

            creature_permanent = {
                "id": source_card_id,
                "name": card_profile["name"],
                "type": "Creature",
                "tapped": False,
                "damage": 0,
                "power": power_stat,
                "toughness": toughness_stat,
                "summoning_sick": not has_haste  # Enforces Summoning Sickness unless Haste present
            }
            
            # Place onto the battlefield permanent registry mapping
            self.state.players[controller_id].battlefield.append(creature_permanent)
            
            state_changes.append({
                "change_type": "ENTER_BATTLEFIELD",
                "target": source_card_id,
                "controller": controller_id
            })

        # Broadcast STACK_RESOLVE event to all connected instances
        self.last_grant_or_request_seq = self.next_seq()
        outbound.append(("ALL", {
            "type": "STACK_RESOLVE",
            "seq_num": self.last_grant_or_request_seq,
            "stack_item_id": resolved_item.get("stack_item_id"),
            "result": "RESOLVED",
            "state_changes": state_changes
        }))
        
        # Always check state-based actions following zone updates
        sba_msg = self.check_state_based_actions()
        if sba_msg:
            return sba_msg

        return outbound

    def handle_declare_attackers(self, client_id: str, pdu: dict) -> list:
        """Processes attacker declarations, taps them, and opens priority (Section 9.3)."""
        if self.state.phase != "DECLARE_ATTACKERS":
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "WRONG_PHASE", "message": "Not in Declare Attackers Step."})]
        if client_id != self.state.active_player:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": "Only the Active Player can attack."})]

        ap_state = self.state.players[client_id]
        attackers_list = pdu.get("attackers", [])  # List of {"creature_id": str, "target": str}
        self.current_attackers = {}  # Tracks attacking assignments internally

        # Section 9.3: If no attackers declared, skip directly to End of Combat
        if not attackers_list:
            return self.enter_phase("END_OF_COMBAT")

        for attack_entry in attackers_list:
            cid = attack_entry.get("creature_id")
            creature = next((p for p in ap_state.battlefield if p["id"] == cid and p["type"] == "Creature"), None)
            
            if not creature:
                return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": f"Creature {cid} not found."})]
            
            # Enforce tapping and summoning sickness rules (Section 3 / 9.3)
            if creature.get("tapped") or creature.get("summoning_sick"):
                return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": f"Creature {cid} is tapped or has summoning sickness."})]

            creature["tapped"] = True
            self.current_attackers[cid] = {"target": attack_entry.get("target"), "blockers": [], "blocker_order": []}

        # Clear pass tracker and broadcast state updates
        self.state.consecutive_passes = 0
        outbound = self.broadcast_state_updates()
        
        # Open priority window starting with Active Player (Section 9.3)
        self.state.priority_holder = self.state.active_player
        outbound.extend(self.grant_priority_to_current())
        return outbound

    def handle_declare_blockers(self, client_id: str, pdu: dict) -> list:
        """Processes blocker assignments and determines if multi-blocking happens (Section 9.4)."""
        if self.state.phase != "DECLARE_BLOCKERS":
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "WRONG_PHASE", "message": "Not in Declare Blockers Step."})]
        if client_id == self.state.active_player:
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": "Active player cannot block."})]

        nap_state = self.state.players[client_id]
        blockers_list = pdu.get("blockers", [])  # List of {"creature_id": str, "blocking_id": str}

        for block_entry in blockers_list:
            blocker_id = block_entry.get("creature_id")
            attacker_id = block_entry.get("blocking_id")
            
            blocker = next((p for p in nap_state.battlefield if p["id"] == blocker_id and p["type"] == "Creature"), None)
            if not blocker or blocker.get("tapped"):
                return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": f"Blocker {blocker_id} invalid or tapped."})]

            if attacker_id in self.current_attackers:
                self.current_attackers[attacker_id]["blockers"].append(blocker_id)
                self.current_attackers[attacker_id]["blocker_order"].append(blocker_id)

        self.state.consecutive_passes = 0
        outbound = self.broadcast_state_updates()

        # Section 9.5: Check if any attacker is multiply-blocked to route to ASSIGN_DAMAGE_ORDER
        is_multiply_blocked = any(len(atk["blockers"]) >= 2 for atk in self.current_attackers.values())
        
        if is_multiply_blocked:
            return outbound + self.enter_phase("ASSIGN_DAMAGE_ORDER")
        else:
            return outbound + self.enter_phase("COMBAT_DAMAGE")

    def handle_assign_damage_order(self, client_id: str, pdu: dict) -> list:
        """Handles ordering choices when an attacker faces multiple blocking creatures (Section 9.5)."""
        if self.state.phase != "ASSIGN_DAMAGE_ORDER":
            return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "WRONG_PHASE", "message": "Not in Assign Damage Order Step."})]
        
        attacker_id = pdu.get("attacker_id")
        blocker_order = pdu.get("blocker_order", [])

        if attacker_id in self.current_attackers:
            # Validate that the incoming declaration contains exactly the blocking IDs present
            if set(blocker_order) == set(self.current_attackers[attacker_id]["blockers"]):
                self.current_attackers[attacker_id]["blocker_order"] = blocker_order
                return self.enter_phase("COMBAT_DAMAGE")

        return [(client_id, {"type": "ERROR", "seq_num": self.next_seq(), "code": "ILLEGAL_ACTION", "message": "Invalid damage assignment order structure."})]

    def resolve_combat_damage_event(self) -> list:
        """Computes and assigns simultaneous combat damage to players and creatures (Section 9.7)."""
        outbound = []
        damage_events = []
        creatures_died = []

        ap_id = self.state.active_player
        nap_id = next(p for p in self.state.players.keys() if p != ap_id)
        
        ap_state = self.state.players[ap_id]
        nap_state = self.state.players[nap_id]

        for atk_id, atk_data in list(self.current_attackers.items()):
            attacker = next((p for p in ap_state.battlefield if p["id"] == atk_id), None)
            if not attacker:
                continue

            atk_power = attacker.get("power", 0)
            blockers = atk_data["blocker_order"]

            if not blockers:
                # Unblocked Attacker: Deals full damage directly to the defending player (Section 9.7)
                nap_state.life -= atk_power
                damage_events.append({"source": atk_id, "target": nap_id, "amount": atk_power})
            else:
                # Blocked Attacker: Assign damage across blockers in designated order (Section 9.7)
                remaining_damage = atk_power
                for blk_id in blockers:
                    blocker = next((p for p in nap_state.battlefield if p["id"] == blk_id), None)
                    if not blocker:
                        continue
                    
                    blk_toughness = blocker.get("toughness", 1) - blocker.get("damage", 0)
                    assigned_damage = min(remaining_damage, blk_toughness)
                    if remaining_damage > 0 and blk_id == blockers[-1]:
                        assigned_damage = remaining_damage  # Remaining damage rolls into the last blocker

                    blocker["damage"] = blocker.get("damage", 0) + assigned_damage
                    damage_events.append({"source": atk_id, "target": blk_id, "amount": assigned_damage})
                    
                    # Blocker fights back: deals combat damage to the attacker simultaneously
                    blk_power = blocker.get("power", 0)
                    attacker["damage"] = attacker.get("damage", 0) + blk_power
                    damage_events.append({"source": blk_id, "target": atk_id, "amount": blk_power})
                    
                    remaining_damage -= assigned_damage

        # Apply State-Based Actions for lethal creature damage (Section 8.4)
        for p_state in self.state.players.values():
            for creature in list(p_state.battlefield):
                if creature["type"] == "Creature" and creature.get("damage", 0) >= creature.get("toughness", 1):
                    p_state.battlefield.remove(creature)
                    p_state.graveyard.append(creature["id"])
                    creatures_died.append(creature["id"])

        # Broadcast COMBAT_DAMAGE_RESULT (Section 10.2.18)
        self.last_grant_or_request_seq = self.next_seq()
        life_totals = {p_id: p_state.life for p_id, p_state in self.state.players.items()}
        
        outbound.append(("ALL", {
            "type": "COMBAT_DAMAGE_RESULT",
            "seq_num": self.last_grant_or_request_seq,
            "damage_events": damage_events,
            "life_totals": life_totals,
            "creatures_died": creatures_died
        }))

        # Run authoritative game over check if a player's life hits zero (Section 8.4)
        sba_check = self.check_state_based_actions()
        if sba_check:
            return sba_check

        outbound.extend(self.broadcast_state_updates())
        return outbound + self.enter_phase("END_OF_COMBAT")
