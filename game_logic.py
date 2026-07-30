import random
import csv
from game_state import GameState, PlayerState

class GameLogic:
    def __init__(self):
        self.state = GameState()
        self.valid_cards = self._load_card_database("mtgnp_master_card_list - Master Card List.csv")
    
    def _load_card_database(self, filepath: str) -> set:
        """Loads valid card IDs from the external out-of-band catalog."""
        # Simulated loading of the master card list
        try:
            valid = set()
            with open(filepath, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    valid.add(row['card_id'])
            return valid
        except FileNotFoundError:
            # Fallback for demonstration
            return {"lightning_bolt_001", "mountain_001", "goblin_guide_001"}

    def next_seq(self):
        self.state.seq_num += 1
        return self.state.seq_num

    def process_pdu(self, client_id: str, pdu: dict) -> list:
        """Main router for game logic based on PDU type."""
        pdu_type = pdu.get("type")
        
        if self.state.phase == "LOBBY":
            if pdu_type == "PLAYER_READY":
                return self.handle_player_ready(client_id, pdu)
        elif self.state.phase == "MULLIGAN":
            if pdu_type == "MULLIGAN_CHOICE":
                return self.handle_mulligan_choice(client_id, pdu)
        # Add routing for IN_GAME phases here...
        
        return [{"type": "ERROR", "code": "UNKNOWN_TYPE", "message": "Invalid action."}]

    def handle_player_ready(self, client_id: str, pdu: dict) -> list:
        player_id = pdu.get("player_id")
        deck_list = pdu.get("deck_list", [])

        # Validate deck size (1 to 50 cards)
        if not (1 <= len(deck_list) <= 50):
            return [{"type": "ERROR", "code": "ILLEGAL_DECK", "message": "Deck must be 1-50 cards."}]

        if player_id not in self.state.players:
            self.state.players[player_id] = PlayerState(player_id=player_id, library=deck_list)
        
        # Remove from waiting_for if present
        if player_id in self.state.waiting_for:
            self.state.waiting_for.remove(player_id)

        outbound_messages = []
        
        # Transition to GAME_SETUP if both players are ready
        if len(self.state.players) == 2:
            self.state.phase = "GAME_SETUP"
            outbound_messages.extend(self.start_game_setup())
        else:
            self.state.waiting_for = ["player_2"] # Placeholder logic for opponent
            outbound_messages.append((client_id, self.state.generate_lobby_update()))
            
        return outbound_messages

    def start_game_setup(self) -> list:
        """Automated GAME_SETUP sequence transitioning into MULLIGAN."""
        outbound = []
        player_ids = list(self.state.players.keys())
        
        # Determine who goes first
        self.state.active_player = random.choice(player_ids)
        self.state.turn = 0
        self.state.phase = "MULLIGAN"

        for p_id, p_state in self.state.players.items():
            p_state.life = 20
            random.shuffle(p_state.library)
            # Draw initial 7 cards
            for _ in range(7):
                if p_state.library:
                    p_state.hand.append(p_state.library.pop())

        self.next_seq()
        
        # Broadcast personalized states
        for p_id in player_ids:
            outbound.append((p_id, self.state.generate_personalized_update(p_id)))
            
        return outbound

    def handle_mulligan_choice(self, client_id: str, pdu: dict) -> list:
        """Processes London Mulligan rules."""
        # Implementation of mulligan state modifications, redraws, and phase advancing to IN_GAME
        pass