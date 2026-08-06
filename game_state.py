# game_state.py
import json
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Any

@dataclass
class PlayerState:
    player_id: str
    life: int = 20
    hand: List[str] = field(default_factory=list)
    library: List[str] = field(default_factory=list)
    graveyard: List[str] = field(default_factory=list)
    battlefield: List[Dict[str, Any]] = field(default_factory=list)
    mulligan_count: int = 0
    has_kept: bool = False
    land_played_this_turn: bool = False

@dataclass
class GameState:
    phase: str = "LOBBY"
    turn: int = 0
    active_player: Optional[str] = None
    priority_holder: Optional[str] = None
    players: Dict[str, PlayerState] = field(default_factory=dict)
    stack: List[Dict[str, Any]] = field(default_factory=list)
    seq_num: int = 0
    waiting_for: List[str] = field(default_factory=list)
    consecutive_passes: int = 0  # Internal optimization tracker for stack resolution

    def generate_lobby_update(self) -> dict:
        """Generates a LOBBY state update matching section 10.2.2."""
        return {
            "type": "GAME_STATE_UPDATE",
            "seq_num": self.seq_num,
            "state": {
                "phase": self.phase,
                "players_ready": len(self.players),
                "waiting_for": self.waiting_for
            }
        }

    def generate_personalized_update(self, target_player_id: str) -> dict:
        """Filters hidden state (opponent's hand) to generate an in-game update matching section 10.2.2."""
        life_totals = {}
        hand_counts = {}
        library_counts = {}
        battlefield_data = {}
        graveyard_data = {}

        for p_id, p_state in self.players.items():
            life_totals[p_id] = p_state.life
            hand_counts[p_id] = len(p_state.hand)
            library_counts[p_id] = len(p_state.library)
            battlefield_data[p_id] = p_state.battlefield
            graveyard_data[p_id] = p_state.graveyard

        # Section 10.2.2: priority_holder is explicitly null during UNTAP and CLEANUP steps
        current_priority = self.priority_holder
        if self.phase in ["UNTAP", "CLEANUP"]:
            current_priority = None

        state_dict = {
            "turn": self.turn,
            "active_player": self.active_player,
            "phase": self.phase,
            "priority_holder": current_priority,
            "life_totals": life_totals,
            "stack": self.stack,
            "battlefield": battlefield_data,
            "graveyard": graveyard_data,
            "hand": self.players[target_player_id].hand,  # Opponent's hand is cleanly stripped out
            "hand_counts": hand_counts,
            "library_counts": library_counts,
            # Section 10.2.2 requires exposing land tracking state boolean
            "land_played_this_turn": self.players[target_player_id].land_played_this_turn
        }

        return {
            "type": "GAME_STATE_UPDATE",
            "seq_num": self.seq_num,
            "state": state_dict
        }
