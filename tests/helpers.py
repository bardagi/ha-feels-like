"""Small typed helpers shared by the tests."""

from homeassistant.core import HomeAssistant, State


def state_of(hass: HomeAssistant, entity_id: str) -> State:
    """Return an entity's state, failing the test if it does not exist."""
    state = hass.states.get(entity_id)
    assert state is not None, f"{entity_id} has no state"
    return state
