import random
import csv
from game_state import GameState, PlayerState

class GameLogic:
    def __init__(self):
        self.state = GameState()
        self.valid_cards = self._load_card_database("mtgnp_master_card_list - Master Card List.csv")
    
    def _load_card_database(self, filepath: str) -> set:
        """Loads valid card IDs from the master catalog CSV."""
        try:
            valid = set()
            with open(filepath, 'r', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                headers = {name.strip().lower(): name for name in reader.fieldnames} if reader.fieldnames else {}
                target_key = headers.get('card_id') or headers.get('id') or (reader.fieldnames[0] if reader.fieldnames else None)

                for row in reader:
                    if target_key and row.get(target_key):
                        valid.add(row[target_key].strip())

            return valid
        except FileNotFoundError:
            print(f"Warning: {filepath} not found. Using fallback card catalog.")
            return {"lightning_bolt_001", "mountain_001", "goblin_guide_001"}

    def next_seq(self):
        self.state.seq_num += 1
        return self.state.seq_num

    def process_pdu(self, client_id: str, pdu: dict) -> list:
        """Main router for incoming player messages."""
        pdu_type = pdu.get("type")
        
        if self.state.phase == "LOBBY":
            if pdu_type == "PLAYER_READY":
                return self.handle_player_ready(client_id, pdu)
        elif self.state.phase == "MULLIGAN":
            if pdu_type == "MULLIGAN_CHOICE":
                return self.handle_mulligan_choice(client_id, pdu)
        
        return [(client_id, {"type": "ERROR", "code": "UNKNOWN_TYPE", "message": "Invalid action for current phase."})]

    def handle_player_ready(self, client_id: str, pdu: dict) -> list:
        """Registers players and validates decks against CSV."""
        player_id = pdu.get("player_id", client_id)
        deck_list = pdu.get("deck_list", [])

        if not (1 <= len(deck_list) <= 50):
            return [(client_id, {"type": "ERROR", "code": "ILLEGAL_DECK", "message": "Deck must contain 1 to 50 cards."})]

        invalid_cards = [c for c in deck_list if c not in self.valid_cards]
        if invalid_cards:
            return [(client_id, {"type": "ERROR", "code": "ILLEGAL_DECK", "message": f"Invalid cards: {invalid_cards[:3]}"})]

        if player_id not in self.state.players:
            p_state = PlayerState(player_id=player_id, library=list(deck_list))
            p_state.has_kept = False
            p_state.mulligan_count = 0
            self.state.players[player_id] = p_state
        
        if player_id in self.state.waiting_for:
            self.state.waiting_for.remove(player_id)

        outbound = []
        if len(self.state.players) == 2:
            self.state.phase = "GAME_SETUP"
            outbound.extend(self.start_game_setup())
        else:
            self.state.waiting_for = ["player_2"]
            outbound.append((client_id, self.state.generate_lobby_update()))
            
        return outbound

    def start_game_setup(self) -> list:
        """Initializes game setup, shuffles decks, and draws 7 cards for each player."""
        outbound = []
        player_ids = list(self.state.players.keys())
        
        self.state.active_player = random.choice(player_ids)
        self.state.turn = 0
        self.state.phase = "MULLIGAN"

        for p_id, p_state in self.state.players.items():
            p_state.life = 20
            p_state.has_kept = False
            p_state.mulligan_count = 0
            random.shuffle(p_state.library)
            
            p_state.hand = []
            for _ in range(7):
                if p_state.library:
                    p_state.hand.append(p_state.library.pop())

        self.next_seq()
        for p_id in player_ids:
            outbound.append((p_id, self.generate_custom_state_update(p_id)))
            
        return outbound

    def handle_mulligan_choice(self, client_id: str, pdu: dict) -> list:
        """Executes London Mulligan rules."""
        p_state = self.state.players.get(client_id)
        if not p_state:
            return [(client_id, {"type": "ERROR", "code": "PLAYER_NOT_FOUND", "message": "Player not found."})]

        keep = pdu.get("keep", True)
        cards_to_bottom = pdu.get("cards_to_bottom", [])

        if not keep:
            # London Mulligan Rule: Shuffle hand into library and draw 7 NEW cards
            p_state.library.extend(p_state.hand)
            p_state.hand = []
            random.shuffle(p_state.library)
            
            p_state.mulligan_count += 1
            for _ in range(7):
                if p_state.library:
                    p_state.hand.append(p_state.library.pop())
            p_state.has_kept = False

        else:
            # Player chooses KEEP -> Must put mulligan_count cards on bottom of library
            required_bottom_count = p_state.mulligan_count
            if len(cards_to_bottom) != required_bottom_count:
                return [(client_id, {
                    "type": "ERROR", 
                    "code": "INVALID_MULLIGAN", 
                    "message": f"Expected exactly {required_bottom_count} cards for deck bottom."
                })]

            # Validate that selected cards exist in player's current hand
            temp_hand = list(p_state.hand)
            for card in cards_to_bottom:
                if card in temp_hand:
                    temp_hand.remove(card)
                else:
                    return [(client_id, {
                        "type": "ERROR", 
                        "code": "INVALID_CARD", 
                        "message": f"Card '{card}' not found in hand."
                    })]

            # Remove from hand and insert at bottom of library (index 0)
            for card in cards_to_bottom:
                p_state.hand.remove(card)
                p_state.library.insert(0, card)

            p_state.has_kept = True

        all_kept = all(getattr(p, 'has_kept', False) for p in self.state.players.values())
        outbound = []
        
        if all_kept:
            # Both players kept -> Transition to IN_GAME Turn 1
            self.state.phase = "IN_GAME"
            self.state.turn = 1
            self.next_seq()
            for p_id in self.state.players.keys():
                outbound.append((p_id, self.generate_custom_state_update(p_id)))
        else:
            self.next_seq()
            outbound.append((client_id, self.generate_custom_state_update(client_id)))

        return outbound

    def generate_custom_state_update(self, player_id: str) -> dict:
        """Generates state update including mulligan_count for the client."""
        base_update = self.state.generate_personalized_update(player_id)
        p_state = self.state.players.get(player_id)
        if p_state and "state" in base_update:
            base_update["state"]["mulligan_count"] = getattr(p_state, 'mulligan_count', 0)
        return base_update