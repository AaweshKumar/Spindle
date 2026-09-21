# Component: Spindle Architecture
# File: errors.py
# Description: Source code module for the Spindle workflow orchestration platform.

class InvalidTransition(Exception):
    """Raised when an event is not valid for the saga's current state."""