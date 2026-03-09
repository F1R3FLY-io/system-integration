import re
import os
import shlex
import time
import logging
import threading
from threading import Event
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import (
    Dict,
    List,
    Tuple,
    Optional,
    Set
)
from dataclasses import dataclass
import requests
import grpc as _grpc
from f1r3fly.client import F1r3flyClient, F1r3flyClientException
from f1r3fly.client import LightBlockInfo, BlockInfo
from f1r3fly.crypto import PrivateKey
from f1r3fly.const import DEFAULT_PHLO_LIMIT, DEFAULT_PHLO_PRICE
from f1r3fly.util import create_deploy_data
from f1r3fly.pb.CasperMessage_pb2 import DeployDataProto  # pylint: disable=no-name-in-module
from docker.client import DockerClient
from docker.models.containers import Container
from docker.models.containers import ExecResult

from .common import (
    TestingContext,
    NonZeroExitCodeError,
    GetBlockError,
    ParsingError,
    SynchronyConstraintError,
    NotAnActiveValidatorError,
    ValidatorNotContainsLatestMes,
    NodeCrashedError,
)

from .error import(
    RNodeAddressNotFoundError,
    CommandTimeoutError,
)

from .utils import (
    parse_mvdag_str,
)

DEFAULT_IMAGE = os.environ.get("DEFAULT_IMAGE", "f1r3flyindustries/f1r3fly-scala-node:latest")

# gRPC channel options to fail fast when a node is down rather than
# hanging indefinitely on a dead connection.
_GRPC_OPTIONS: tuple = (
    ('grpc.enable_retries', 0),
    ('grpc.initial_reconnect_backoff_ms', 500),
    ('grpc.max_reconnect_backoff_ms', 2000),
    ('grpc.keepalive_time_ms', 10000),
    ('grpc.keepalive_timeout_ms', 5000),
    ('grpc.keepalive_permit_without_calls', 1),
)

rnode_binary = '/opt/docker/bin/node'
rnode_directory = "/var/lib/rnode"
rnode_deploy_dir = f"{rnode_directory}/deploy"
rnode_bonds_file = f'{rnode_directory}/genesis/bonds.txt'
rnode_wallets_file = f'{rnode_directory}/genesis/wallets.txt'
rnode_certificate_path = f'{rnode_directory}/node.certificate.pem'
rnode_key_path = f'{rnode_directory}/node.key.pem'

# Default ports inside container (all nodes use same internal ports)
default_http_port = 40403
default_internal_grpc_port = 40402
default_external_grpc_port = 40401

# Shard ID matches the shared HOCON configs (casper.shard-name = root)
default_shard_id = 'root'




# Module-level function to avoid pickle issues with nested functions
def _command_executor(container: Container, cmd: Tuple[str, ...], stderr: bool) -> Tuple[int, str]:
    """Execute container command and return result."""
    exec_result: ExecResult = container.exec_run(cmd, stderr=stderr)
    return (exec_result.exit_code, exec_result.output.decode('utf-8'))


@dataclass
class PortMapping:
    """Maps container internal ports to host-accessible ports."""
    http: int
    external_grpc: int
    internal_grpc: int


# Host port mappings for each node in the compose shard topology.
# These match the port mappings in integration-tests/docker-compose.yml.
COMPOSE_PORT_MAPPINGS = {
    'rnode.bootstrap': PortMapping(http=40403, external_grpc=40401, internal_grpc=40402),
    'rnode.validator1': PortMapping(http=40413, external_grpc=40411, internal_grpc=40412),
    'rnode.validator2': PortMapping(http=40423, external_grpc=40421, internal_grpc=40422),
    'rnode.validator3': PortMapping(http=40433, external_grpc=40431, internal_grpc=40432),
    'rnode.readonly': PortMapping(http=40453, external_grpc=40451, internal_grpc=40452),
    # Standalone node (ports 40460-40465, avoids conflict with shard)
    'rnode.standalone': PortMapping(http=40463, external_grpc=40461, internal_grpc=40462),
    # Custom shard nodes (ports 40500-40545, for per-test custom shard infrastructure)
    'rnode.custom.boot': PortMapping(http=40503, external_grpc=40501, internal_grpc=40502),
    'rnode.custom.validator1': PortMapping(http=40513, external_grpc=40511, internal_grpc=40512),
    'rnode.custom.validator2': PortMapping(http=40523, external_grpc=40521, internal_grpc=40522),
    'rnode.custom.validator3': PortMapping(http=40533, external_grpc=40531, internal_grpc=40532),
    'rnode.custom.joiner': PortMapping(http=40543, external_grpc=40541, internal_grpc=40542),
}


class Node:
    """Wraps an existing Docker container (compose-managed) for test interaction.

    Unlike the legacy implementation that created containers via the Docker
    Python SDK, this class connects to containers that are already running
    via docker-compose. It never creates or destroys containers.
    """

    def __init__(self, *, container: Container, command_timeout: int,
                 network: str, ports: PortMapping) -> None:
        self.container = container
        self.name = container.name
        self.command_timeout = command_timeout
        self.network = network
        self.ports = ports
        self.terminate_background_logging_event = threading.Event()
        self.background_logging = LoggingThread(
            container=container,
            logger=logging.getLogger('peers'),
            terminate_thread_event=self.terminate_background_logging_event,
        )
        self.background_logging.daemon = True
        self.background_logging.start()

    def __repr__(self) -> str:
        return f'<Node(name={repr(self.name)})>'

    def check_alive(self) -> None:
        """Raise NodeCrashedError immediately if the container is not running.

        Called at the top of logs(), gRPC, and HTTP methods so that any
        operation against a dead node fails instantly instead of hanging
        until a timeout expires.
        """
        self.container.reload()
        status = self.container.status
        if status != 'running':
            exit_code = self.container.attrs.get('State', {}).get('ExitCode', '?')
            raise NodeCrashedError(self.name, status, str(exit_code))

    @classmethod
    def from_container_name(cls, docker_client: DockerClient, container_name: str,
                            command_timeout: int, network: str) -> 'Node':
        """Create a Node by looking up an existing container by name."""
        container = docker_client.containers.get(container_name)
        ports = COMPOSE_PORT_MAPPINGS.get(container_name)
        if ports is None:
            raise ValueError(f"No port mapping defined for container: {container_name}")
        return cls(
            container=container,
            command_timeout=command_timeout,
            network=network,
            ports=ports,
        )

    def get_node_pem_cert(self) -> bytes:
        return self.shell_out("cat", rnode_certificate_path).encode('utf8')

    def get_node_pem_key(self) -> bytes:
        return self.shell_out("cat", rnode_key_path).encode('utf8')

    def view_file(self, path: str) -> str:
        return self.shell_out("cat", path)

    def logs(self) -> str:
        self.check_alive()
        return self.container.logs().decode('utf-8')

    def get_rnode_address(self) -> str:
        log_content = self.logs()
        regex = r"Listening for traffic on (rnode://[^@]+@[^?]+\?protocol=\d+&discovery=\d+)\."
        match = re.search(regex, log_content, re.MULTILINE | re.DOTALL)
        if match is None:
            raise RNodeAddressNotFoundError(regex)
        address = match.group(1)
        return address

    def get_metrics(self) -> str:
        self.check_alive()
        resp = requests.get(f"http://localhost:{self.ports.http}/metrics", timeout=60)
        return resp.content.decode('utf8')

    def get_connected_peers_metric_value(self) -> str:
        self.check_alive()
        try:
            resp = requests.get(f"http://localhost:{self.ports.http}/metrics", timeout=60)
            result = ''
            for line in resp.content.decode('utf8').splitlines():
                if line.startswith("peers{") or line.startswith("f1r3fly_comm_rp_connect_peers"):
                    result = line
                    break
            return result
        except NonZeroExitCodeError as e:
            if e.exit_code == 1:
                return ''
            raise

    def get_http_port(self) -> int:
        return self.ports.http

    def get_external_grpc_port(self) -> int:
        return self.ports.external_grpc

    def get_internal_grpc_port(self) -> int:
        return self.ports.internal_grpc

    def stop_logging(self) -> None:
        """Stop the background logging thread without removing the container."""
        self.terminate_background_logging_event.set()
        self.background_logging.join(timeout=5)

    def get_current_block_number(self) -> int:
        """Return the block number of the most recent block on the chain.

        Used to set ``validAfterBlockNumber`` on new deploys so they are
        not expired by the node's deploy-pool filter (which discards
        deploys with ``validAfterBlockNumber <= currentBlock - 50``).
        """
        blocks = self.get_blocks(1)
        if blocks:
            return max(b.blockNumber for b in blocks)
        return 0

    def get_blocks_count(self, depth: int) -> int:
        show_blocks = self.get_blocks(depth)
        return len(show_blocks)

    def get_blocks(self, depth: int) -> List[LightBlockInfo]:
        self.check_alive()
        with F1r3flyClient('localhost', self.get_external_grpc_port(), grpc_options=_GRPC_OPTIONS) as client:
            return client.show_blocks(depth)

    def get_block(self, hash: str) -> BlockInfo:
        self.check_alive()
        with F1r3flyClient('localhost', self.get_external_grpc_port(), grpc_options=_GRPC_OPTIONS) as client:
            try:
                return client.show_block(hash)
            except F1r3flyClientException as e:
                message = e.args[0]
                raise GetBlockError(('show-block',), 1, message) from e

    def _exec_run_with_timeout(self, cmd: Tuple[str, ...], stderr: bool = True) -> Tuple[int, str]:
        self.check_alive()
        logging.info("COMMAND %s %s", self.name, cmd)

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_command_executor, self.container, cmd, stderr)
            try:
                exit_code, output = future.result(timeout=self.command_timeout)
            except FutureTimeoutError as e:
                raise CommandTimeoutError(cmd, self.command_timeout) from e

        if exit_code != 0:
            for line in output.splitlines():
                logging.info('%s: %s', self.name, line)
            logging.warning("EXITED %s %s %d", self.name, cmd, exit_code)
        else:
            for line in output.splitlines():
                logging.debug('%s: %s', self.name, line)
            logging.debug("EXITED %s %s %d", self.name, cmd, exit_code)
        return exit_code, output

    def shell_out(self, *cmd: str, stderr: bool = True) -> str:
        exit_code, output = self._exec_run_with_timeout(cmd, stderr=stderr)
        if exit_code != 0:
            raise NonZeroExitCodeError(command=cmd, exit_code=exit_code, output=output)
        return output

    def rnode_command(self, *node_args: str, stderr: bool = True) -> str:
        return self.shell_out(rnode_binary, '--grpc-port=40402',
                               *node_args, stderr=stderr)

    def eval(self, rho_file_path: str) -> str:
        return self.rnode_command('eval', rho_file_path)

    def deploy(self, rho_file_path: str, private_key: PrivateKey, phlo_limit: int = DEFAULT_PHLO_LIMIT,
               phlo_price: int = DEFAULT_PHLO_PRICE, valid_after_block_no: Optional[int] = None,
               shard_id: str = default_shard_id) -> str:
        return self.deploy_string(
            self.view_file(rho_file_path), private_key, phlo_limit, phlo_price,
            valid_after_block_no, shard_id,
        )

    def deploy_string(self, rholang_code: str, private_key: PrivateKey, phlo_limit: int = DEFAULT_PHLO_LIMIT,
                      phlo_price: int = DEFAULT_PHLO_PRICE, valid_after_block_no: Optional[int] = None,
                      shard_id: str = default_shard_id) -> str:
        self.check_alive()
        if valid_after_block_no is None or valid_after_block_no < 0:
            # Keep deploy validity near chain tip to avoid stale deploy filtering.
            valid_after_block_no = max(0, self.get_current_block_number() - 1)
        try:
            now_time = int(time.time() * 1000)
            deploy_data = create_deploy_data(
                private_key, rholang_code, phlo_price, phlo_limit,
                valid_after_block_no, now_time, shard_id,
            )
            with F1r3flyClient('localhost', self.get_external_grpc_port(), grpc_options=_GRPC_OPTIONS) as client:
                return client.send_deploy(deploy_data)
        except F1r3flyClientException as e:
            message = e.args[0]
            if "Parsing error" in message:
                raise ParsingError(command=("deploy",), exit_code=1, output=message) from e
            raise e

    def get_vdag(self) -> str:
        return self.rnode_command('vdag', stderr=False)

    def get_mvdag(self) -> str:
        return self.rnode_command('mvdag', stderr=False)

    def get_parsed_mvdag(self) -> Dict[str, Set[str]]:
        return parse_mvdag_str(self.get_mvdag())

    def deploy_contract_with_substitution(self, substitute_dict: Dict[str, str], rho_file_path: str,
                                          private_key: PrivateKey, phlo_limit: int = DEFAULT_PHLO_LIMIT,
                                          phlo_price: int = DEFAULT_PHLO_PRICE,
                                          shard_id: str = default_shard_id,
                                          valid_after_block_no: Optional[int] = None) -> str:
        """Deploy a contract with string substitutions applied.

        Reads the .rho template from the local filesystem, applies
        substitutions in Python, then deploys via gRPC. No file mounting
        or container file system access needed.

        Relative paths (e.g. 'resources/storage/store-data.rho') are resolved
        relative to the integration-tests/ directory so tests work regardless
        of the working directory pytest is invoked from.

        Args:
            valid_after_block_no: If >= 0, the deploy is only valid after this
                block number. Use this to enforce causal ordering (e.g. a read
                deploy that depends on a prior store deploy's block).

        Returns the deploy ID. The caller is responsible for waiting for
        block inclusion (heartbeat will auto-propose).
        """
        resolved_path = rho_file_path
        if not os.path.isabs(rho_file_path) and not os.path.exists(rho_file_path):
            integration_tests_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            resolved_path = os.path.join(integration_tests_dir, rho_file_path)
        with open(resolved_path, 'r') as f:
            contract_content = f.read()

        for key, value in substitute_dict.items():
            contract_content = contract_content.replace(key, value)

        return self.deploy_string(contract_content, private_key, phlo_limit, phlo_price,
                                  valid_after_block_no=valid_after_block_no, shard_id=shard_id)

    def find_deploy(self, deploy_id: str) -> LightBlockInfo:
        self.check_alive()
        with F1r3flyClient('localhost', self.get_external_grpc_port(), grpc_options=_GRPC_OPTIONS) as client:
            return client.find_deploy(deploy_id)

    def propose(self, retries: int = 5, retry_delay: float = 3.0) -> str:
        """Propose a new block.

        Retries automatically when the heartbeat proposer is already in
        progress ('another propose is in progress').  Other errors are
        raised immediately.
        """
        for attempt in range(retries):
            self.check_alive()
            try:
                with F1r3flyClient('localhost', self.get_internal_grpc_port(), grpc_options=_GRPC_OPTIONS) as client:
                    return client.propose()
            except F1r3flyClientException as e:
                message = e.args[0]
                if "another propose is in progress" in message:
                    if attempt < retries - 1:
                        logging.info(
                            "Propose contention (attempt %d/%d), retrying in %.1fs",
                            attempt + 1, retries, retry_delay,
                        )
                        time.sleep(retry_delay)
                        continue
                    raise e
                if "Must wait for more blocks from other validators" in message or "NotEnoughNewBlocks" in message:
                    raise SynchronyConstraintError(command=('propose',), exit_code=1, output=message) from e
                if "ReadOnlyMode" in message:
                    raise NotAnActiveValidatorError(command=('propose',), exit_code=1, output=message) from e
                if "Validator does not have a latest message" in message:
                    raise ValidatorNotContainsLatestMes(command=('propose',), exit_code=1, output=message) from e
                raise e
        raise RuntimeError("propose: exhausted retries")

    def last_finalized_block(self) -> BlockInfo:
        self.check_alive()
        with F1r3flyClient('localhost', self.get_external_grpc_port(), grpc_options=_GRPC_OPTIONS) as client:
            return client.last_finalized_block()

    def repl(self, rholang_code: str, stderr: bool = False) -> str:
        quoted_rholang_code = shlex.quote(rholang_code)
        exit_code, output = self._exec_run_with_timeout(
            (
                'sh',
                '-c',
                f'echo {quoted_rholang_code} | {rnode_binary} --grpc-port=40402 repl',
            ),
            stderr=stderr,
        )
        if stderr and exit_code != 0:
            raise NonZeroExitCodeError(
                command=('sh', '-c', f'echo {quoted_rholang_code} | {rnode_binary} --grpc-port=40402 repl'),
                exit_code=exit_code,
                output=output
            )
        return output

    __timestamp_rx = "\\d\\d:\\d\\d:\\d\\d\\.\\d\\d\\d"
    __log_message_rx = re.compile("^{timestamp_rx} (.*?)(?={timestamp_rx})"
                                  .format(timestamp_rx=__timestamp_rx), re.MULTILINE | re.DOTALL)

    def log_lines(self) -> List[str]:
        log_content = self.logs()
        return Node.__log_message_rx.split(log_content)


class LoggingThread(threading.Thread):
    def __init__(self, terminate_thread_event: Event, container: Container, logger: logging.Logger) -> None:
        super().__init__()
        self.terminate_thread_event = terminate_thread_event
        self.container = container
        self.logger = logger

    def run(self) -> None:
        containers_log_lines_generator = self.container.logs(stream=True, follow=True)
        try:
            while True:
                if self.terminate_thread_event.is_set():
                    break
                line = next(containers_log_lines_generator)
                self.logger.info('%11s: %s', self.container.name[-11:], line.decode('utf-8').rstrip())
        except StopIteration:
            pass
