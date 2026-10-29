STREAM_KEY = "spindle:commands"

# Result stream (connectors to orchestrator).
RESULTS_STREAM_KEY = "spindle:results"

# Per-step-type work streams.  The router fan-outs commands here; connectors
# consume from spindle:work:{step_type}.  Promoted from connectors/consumer.py
# so streams/ never imports from connectors/.
WORK_STREAM_PREFIX = "spindle:work:"

# Dead-letter stream for commands that cannot be parsed or routed.
DEAD_COMMANDS_STREAM_KEY = "spindle:dead:commands"

# Consumer-group names — kept together so all stream identities are visible in
# one place.  Each group name is unique across the system.
ROUTER_GROUP_NAME = "spindle-router"
