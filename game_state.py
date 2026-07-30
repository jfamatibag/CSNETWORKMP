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

    def generate_lobby_update(self) -> dict:
        """Generates a LOBBY state update."""
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
        """Filters hidden state (opponent's hand) to generate an in-game update."""
        life_totals = {}
        hand_counts = {}
        library_counts = {}
        battlefield = {}
        graveyard = {}

        for p_id, p_state in self.players.items():
            life_totals[p_id] = p_state.life
            hand_counts[p_id] = len(p_state.hand)
            library_counts[p_id] = len(p_state.library)
            battlefield[p_id] = p_state.battlefield
            graveyard[p_id] = p_state.graveyard

        state_dict = {
            "turn": self.turn,
            "phase": self.phase,
            "active_player": self.active_player,
            "life_totals": life_totals,
            "hand": self.players[target_player_id].hand,
            "hand_counts": hand_counts,
            "library_counts": library_counts,
            "battlefield": battlefield,
            "graveyard": graveyard,
            "stack": self.stack
        }

        # Add land_played flag if the target is the active player
        if self.active_player == target_player_id:
            state_dict["land_played"] = self.players[target_player_id].land_played_this_turn

        return {
            "type": "GAME_STATE_UPDATE",
            "seq_num": self.seq_num,
            "state": state_dict
        }