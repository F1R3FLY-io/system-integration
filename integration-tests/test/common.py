import os
import random
import string
import tempfile
from typing import (
    List,
    Tuple,
    Optional,
    TYPE_CHECKING,
)
import dataclasses
from f1r3fly.crypto import PrivateKey

from docker.client import DockerClient

if TYPE_CHECKING:
    # pylint: disable=cyclic-import
    from .wait import PredicateProtocol


@dataclasses.dataclass
class CommandLineOptions:
    node_startup_timeout: int
    network_converge_timeout: int
    receive_timeout: int
    command_timeout: int
    random_seed: Optional[int]
    timeout_scale: float = 1.0


@dataclasses.dataclass # pylint: disable=too-many-instance-attributes
class TestingContext:
    """Context for compose-based integration tests.

    Wraps the command-line options, Docker client, and random generator
    used across the test session. Bonds and wallets are managed by the
    shared genesis files in the integration-tests directory.
    """
    __test__ = False

    node_startup_timeout: int
    network_converge_timeout: int
    receive_timeout: int
    command_timeout: int
    docker: DockerClient
    random_generator: random.Random
    timeout_scale: float = 1.0


class NonZeroExitCodeError(Exception):
    def __init__(self, command: Tuple[str, ...], exit_code: int, output: str) -> None:
        super().__init__()
        self.command = command
        self.exit_code = exit_code
        self.output = output


class GetBlockError(NonZeroExitCodeError):
    pass


class ParsingError(NonZeroExitCodeError):
    pass

class SynchronyConstraintError(NonZeroExitCodeError):
    pass

class NotAnActiveValidatorError(NonZeroExitCodeError):
    pass

class ValidatorNotContainsLatestMes(NonZeroExitCodeError):
    pass

class WaitTimeoutError(Exception):
    def __init__(self, predicate: 'PredicateProtocol', timeout: int) -> None:
        super().__init__()
        self.predicate = predicate
        self.timeout = timeout

class TransderFundsError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__()
        self.reason = reason


class NodeCrashedError(Exception):
    """Raised when a node container has stopped or crashed mid-operation."""
    def __init__(self, node_name: str, status: str, exit_code: str = '?') -> None:
        self.node_name = node_name
        self.status = status
        self.exit_code = exit_code
        super().__init__(
            f"Node {node_name} has crashed: status={status}, exit_code={exit_code}"
        )


def random_string(context: TestingContext, length: int) -> str:
    return ''.join(context.random_generator.choice(string.ascii_letters) for m in range(length))


def make_tempfile(prefix: str, content: str) -> str:
    fd, path = tempfile.mkstemp(prefix=prefix)

    with os.fdopen(fd, 'w') as tmp:
        tmp.write(content)

    return path


def make_tempdir(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix)
