"""Domain errors that are safe to show without leaking credentials."""


class ThetaTrapError(Exception):
    """Base class for expected application failures."""


class ConfigurationError(ThetaTrapError):
    """Configuration is missing, contradictory, or unsafe."""


class AccountIdentityError(ThetaTrapError):
    """The broker account does not match the configured/database identity."""


class MCPContractError(ThetaTrapError):
    """The discovered MCP tool contract is incompatible with ThetaTrap."""


class MCPToolError(ThetaTrapError):
    """An MCP tool returned an error or malformed result."""


class PolicyError(ThetaTrapError):
    """A requested action does not match the deterministic trading policy."""


class AgentError(ThetaTrapError):
    """The model/tool loop failed before a broker mutation was authorized."""


class StateError(ThetaTrapError):
    """A persisted strategy-state transition is invalid or contradictory."""


class ExecutionError(ThetaTrapError):
    """Broker execution or reconciliation could not establish a safe state."""
