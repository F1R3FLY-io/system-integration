"""Raw-P2P TLS client for slashing integration tests.

The slashing E2E suite needs to inject a forged ``BlockMessage`` over
the node's TLS-encrypted P2P transport layer (port 40400 inside the
container). The high-level ``f1r3fly.client.F1r3flyClient`` only
exposes the plaintext DeployService/ProposeService gRPC ports on the
host — it has no way to reach the transport-layer port or speak the
``routing.TransportLayer`` service.

This module fills that gap. Ported from the legacy
``integration-tests/test/node_client.py`` and adapted to the v2
framework (``infra/`` + ``Provider``/``Node``):

- ``_generate_fresh_p2p_credentials`` — fresh secp256r1 cert + key
  whose CN is the F1r3fly address of its own pubkey. Replaces the
  stored ``protocol.{cert,key}.pem`` (expired since 2020 + Rust node
  hostname-verification quirk on the PrintableString-encoded CN).
- ``NodeClient`` — opens an mTLS channel to a peer's transport port,
  ``Send``/``Stream``/``block_request``/``send_block``.
- ``p2p_protocol_client`` — context manager that resolves the host's
  IP on the shard's Docker network and yields a ``NodeClient``.
- ``rust_block_hash`` / ``is_exist_slash_deploy`` — block utilities
  used by every slashing test.

Platform: NodeClient opens a raw TCP socket from the host into a
container's transport port; this requires native Linux Docker bridge
routing. macOS, Windows, and WSL2 hosts cannot reach container IPs
and the slashing tests are ``skipif``'d there.
"""
from __future__ import annotations

import hashlib
import json
import logging
import socket
import subprocess
from concurrent import futures
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from queue import Empty, Queue
from typing import Generator, Iterator, Tuple

import grpc
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key
from cryptography.x509.oid import NameOID
from eth_hash.auto import keccak

from f1r3fly.pb import CasperMessage_pb2
from f1r3fly.pb.CasperMessage_pb2 import (  # pylint: disable=no-name-in-module
    BlockMessageProto as BlockMessage,
    BlockRequestProto as BlockRequest,
)
from f1r3fly.pb.routing_pb2 import (
    Ack,
    Chunk,
    Header,
    Node as NodeProto,
    Packet,
    Protocol,
    TLRequest,
    TLResponse,
)
from f1r3fly.pb.routing_pb2_grpc import (
    TransportLayerServicer,
    TransportLayerStub,
    add_TransportLayerServicer_to_server,
)

from .node import Node

DEFAULT_TRANSPORT_SERVER_PORT = 40400
DEFAULT_NETWORK_ID = "testnet"

logger = logging.getLogger(__name__)


class BlockNotFound(Exception):
    """Raised when ``NodeClient.block_request`` times out without a reply."""

    def __init__(self, block_hash: str, node: Node):
        super().__init__(f"block {block_hash} not delivered by {node.name}")
        self.block_hash = block_hash
        self.node = node


# ── Helpers ──────────────────────────────────────────────────────────


def _get_free_tcp_port() -> int:
    """Bind a transient TCP socket to find an unused port."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("", 0))
        _, port = sock.getsockname()
    finally:
        sock.close()
    return port


def _get_network_gateway_ip(network_name: str) -> str:
    """Return the IPv4 gateway address of ``network_name``.

    Used as the source/host address for the in-process
    ``TransportServer`` so containers on the same Docker network can
    reply to it. Substring match on the network name; raises if no
    network matches or the matched network has no IPAM gateway.
    """
    result = subprocess.run(
        ["docker", "network", "ls", "--format", "{{.Name}}"],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"docker network ls failed: {result.stderr.strip()}"
        )
    matches = [n for n in result.stdout.splitlines() if network_name in n]
    if not matches:
        raise RuntimeError(
            f"no docker network matches {network_name!r}; "
            f"available: {result.stdout.splitlines()}"
        )
    full_name = matches[0]
    inspect = subprocess.run(
        ["docker", "network", "inspect", full_name],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    if inspect.returncode != 0:
        raise RuntimeError(
            f"docker network inspect {full_name} failed: {inspect.stderr.strip()}"
        )
    payload = json.loads(inspect.stdout)
    if not payload:
        raise RuntimeError(f"docker network inspect {full_name} returned empty payload")
    configs = payload[0].get("IPAM", {}).get("Config", [])
    for cfg in configs:
        gateway = cfg.get("Gateway")
        if gateway:
            return gateway
    raise RuntimeError(
        f"docker network {full_name} has no IPAM gateway; IPAM={payload[0].get('IPAM')!r}"
    )


def get_node_id_raw(key: EllipticCurvePrivateKey) -> bytes:
    """Last 20 bytes of ``keccak(x || y)`` for the secp256r1 pubkey ``(x, y)``."""
    curve = key.public_key().public_numbers()
    pk_bytes = curve.x.to_bytes(32, "big") + curve.y.to_bytes(32, "big")
    return keccak(pk_bytes)[12:]


def get_node_id_str(key: EllipticCurvePrivateKey) -> str:
    """Hex-encoded ``get_node_id_raw`` — matches the CN on the node's TLS cert."""
    return get_node_id_raw(key).hex()


def _generate_fresh_p2p_credentials() -> Tuple[bytes, bytes]:
    """Generate a fresh secp256r1 X.509 cert + PKCS#8 key pair.

    Replaces ``resources/bootstrap_certificate/protocol.{cert,key}.pem``,
    which has been expired since ``Jul  8 05:31:44 2020 GMT``. The Rust
    node's ``comm::rust::transport::HostnameTrustManager`` also rejects
    the stored cert at its hostname-verification step (the CN
    byte-matches the algorithm-derived F1r3fly address but
    ``x509_parser::AttributeTypeAndValue::attr_value().as_str()`` has a
    quirk with PrintableString encoding) — so a fresh cert sidesteps
    both issues at once.

    The cert's CN is set to the Keccak256-based F1r3fly address of its
    own pubkey (last 20 bytes of ``keccak(x || y)`` where ``x`` / ``y``
    are the 32-byte big-endian secp256r1 coordinates), matching the
    algorithm in :func:`get_node_id_raw` above and
    ``crypto/src/rust/util/certificate_helper.rs::public_address`` in
    f1r3node-rust. Validity is 100 years from generation, so the cert
    never silently expires inside a test run.
    """
    private_key = ec.generate_private_key(ec.SECP256R1(), default_backend())
    pub = private_key.public_key().public_numbers()
    pk_bytes = pub.x.to_bytes(32, "big") + pub.y.to_bytes(32, "big")
    address_hex = keccak(pk_bytes)[12:].hex()

    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, address_hex)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365 * 100))
        .sign(private_key, hashes.SHA256())
    )

    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return cert_pem, key_pem


# ── Block helpers (moved from test_slash.py) ────────────────────────


def generate_block_hash() -> bytes:
    """Return a known-invalid blake2b-32 digest used as the tampered hash.

    Computed once via ``blake2b(b'evil')`` so every call yields the
    same value (tests pre-compute it for the regex-match assertion).
    """
    blake = hashlib.blake2b(digest_size=32)
    blake.update(b"evil")
    return blake.digest()


def rust_block_hash(block: BlockMessage) -> bytes:
    """Compute the block hash the way f1r3node does.

    Mirrors the byte layout in
    ``casper/src/rust/util/proto_util.rs::hash_block`` of the
    f1r3node-rust repo exactly::

        header.SerializeToString() ++ body.SerializeToString()
            ++ sender (raw bytes)
            ++ sigAlgorithm.encode('utf-8')   # pyf1r3fly wraps in StringValue
            ++ seqNum as i32 little-endian    # pyf1r3fly wraps in Int32Value
            ++ shardId.encode('utf-8')        # pyf1r3fly wraps in StringValue
            ++ extraBytes (raw)

    Hashed with Blake2b at digest size 32. Forged blocks re-hashed
    with this helper pass ``Validate::block_hash`` on the Rust node
    and reach the intended offense rather than being rejected as
    ``InvalidBlockHash`` first.
    """
    buf = b"".join(
        [
            block.header.SerializeToString(),
            block.body.SerializeToString(),
            block.sender,
            block.sigAlgorithm.encode("utf-8"),
            block.seqNum.to_bytes(4, "little", signed=True),
            block.shardId.encode("utf-8"),
            block.extraBytes,
        ]
    )
    return hashlib.blake2b(buf, digest_size=32).digest()


def is_exist_slash_deploy(block: BlockMessage) -> bool:
    """Return True if any system deploy in the block is a slashSystemDeploy."""
    for system_deploy in block.body.systemDeploys:
        if system_deploy.systemDeploy.WhichOneof("systemDeploy") == "slashSystemDeploy":
            return True
    return False


# ── TransportServer + NodeClient ─────────────────────────────────────


class TransportServer(TransportLayerServicer):
    """In-process gRPC server that receives Stream replies from peers."""

    def __init__(self, node: NodeProto, network_id: str, return_queue: Queue):
        super().__init__()
        self.node = node
        self.header = Header(sender=self.node, networkId=network_id)
        self.return_queue = return_queue

    def Send(self, request: TLRequest, context: grpc.ServicerContext) -> TLResponse:
        return TLResponse(noResponse=Ack(header=self.header))

    def Stream(
        self, request_iterator: Iterator[Chunk], context: grpc.ServicerContext
    ) -> TLResponse:
        message_cls = None
        data = b""
        for chunk in request_iterator:
            content_type = chunk.WhichOneof("content")
            if content_type == "header":
                type_id = chunk.header.typeId
                message_cls = getattr(CasperMessage_pb2, f"{type_id}Proto")
            elif content_type == "data":
                data = chunk.data.contentData
            else:
                raise NotImplementedError()
        assert message_cls is not None
        stream_message = message_cls()
        stream_message.ParseFromString(data)
        self.return_queue.put(stream_message)
        return TLResponse(ack=Ack(header=self.header))


class NodeClient:
    """Raw-P2P client for the F1r3fly TLS transport layer.

    Hosts a small in-process ``TransportServer`` so peers can reply
    (e.g. ``BlockRequest`` → ``BlockMessage`` stream). Sends are mTLS
    over an ephemeral channel per call.

    See module docstring for platform constraints (Linux-only).
    """

    def __init__(  # pylint: disable=too-many-positional-arguments
        self,
        node_pem_cert: bytes,
        node_pem_key: bytes,
        host: str,
        network_name: str,
        receive_timeout: float,
        network_id: str = DEFAULT_NETWORK_ID,
    ) -> None:
        self.node_pem_cert = node_pem_cert
        self.node_pem_key = node_pem_key
        self.ec_key = load_pem_private_key(
            self.node_pem_key, password=None, backend=default_backend()
        )
        self.network_id = network_id

        self.host = host
        self.tcp_port = 0
        self.udp_port = _get_free_tcp_port()
        self.network_name = network_name
        self._receive_timeout = receive_timeout

        self.return_queue: Queue = Queue()

        self.server = self._start_transport_server()

    @property
    def node_pb(self) -> NodeProto:
        node_id = get_node_id_raw(self.ec_key)  # type: ignore[arg-type]
        return NodeProto(
            id=node_id,
            host=self.host.encode("utf8"),
            tcp_port=self.tcp_port,
            udp_port=self.udp_port,
        )

    @property
    def header_pb(self) -> Header:
        return Header(sender=self.node_pb, networkId=self.network_id)

    def _start_transport_server(self) -> grpc.Server:
        server_credential = grpc.ssl_server_credentials(
            [(self.node_pem_key, self.node_pem_cert)]
        )
        server = grpc.server(futures.ThreadPoolExecutor())
        add_TransportLayerServicer_to_server(
            TransportServer(self.node_pb, self.network_id, self.return_queue),
            server,
        )
        self.tcp_port = server.add_secure_port(f"{self.host}:0", server_credential)
        server.start()
        return server

    def block_request(self, block_hash: str, peer: Node) -> BlockMessage:
        """Fetch the proto for ``block_hash`` from ``peer`` via the transport layer."""
        block_request = BlockRequest(hash=bytes.fromhex(block_hash))
        request_msg_packet = Packet(
            typeId="BlockRequest", content=block_request.SerializeToString()
        )
        protocol = Protocol(header=self.header_pb, packet=request_msg_packet)
        request = TLRequest(protocol=protocol)
        self.send_request(request, peer)
        try:
            return self.return_queue.get(timeout=self._receive_timeout)
        except Empty as e:
            raise BlockNotFound(block_hash, peer) from e

    def send_request(self, request: TLRequest, peer: Node) -> None:
        """Open an mTLS channel to ``peer`` and ``Send`` ``request``."""
        peer_cert = peer.peer_cert()
        peer_key = peer.peer_key()
        credential = grpc.ssl_channel_credentials(
            peer_cert, self.node_pem_key, self.node_pem_cert
        )
        peer_ip = peer.peer_ip(self.network_name)
        channel = grpc.secure_channel(
            f"{peer_ip}:{DEFAULT_TRANSPORT_SERVER_PORT}",
            credential,
            options=(
                (
                    "grpc.ssl_target_name_override",
                    get_node_id_str(
                        load_pem_private_key(peer_key, None, default_backend())  # type: ignore[arg-type]
                    ),
                ),
            ),
        )
        try:
            stub = TransportLayerStub(channel)
            stub.Send(request)
        finally:
            channel.close()

    def send_block(self, block: BlockMessage, peer: Node) -> None:
        """Inject ``block`` into ``peer`` via the transport layer."""
        block_msg_packet = Packet(typeId="BlockMessage", content=block.SerializeToString())
        protocol = Protocol(header=self.header_pb, packet=block_msg_packet)
        request = TLRequest(protocol=protocol)
        self.send_request(request, peer)

    def stop(self) -> None:
        self.server.stop(0)


@contextmanager
def p2p_protocol_client(
    network_name: str, receive_timeout: float = 30.0
) -> Generator[NodeClient, None, None]:
    """Yield a ``NodeClient`` configured to act as a peer on ``network_name``.

    Resolves the host's gateway IP on the shard's Docker network so
    containers on that network can reply to the client's in-process
    ``TransportServer``. The client itself opens mTLS channels to
    specific peers per ``block_request`` / ``send_block`` call —
    pass the target ``Node`` to those methods.
    """
    cert, key = _generate_fresh_p2p_credentials()
    host_ip = _get_network_gateway_ip(network_name)
    client = NodeClient(cert, key, host_ip, network_name, receive_timeout)
    try:
        yield client
    finally:
        client.stop()
