class InvalidTransition(Exception):
    """Raised when an event is not valid for the saga's current state."""