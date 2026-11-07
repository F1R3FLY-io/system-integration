"""Raw P2P client that takes part in a node's TLS transport as a peer.

Tests use it to fetch a block's exact protobuf from a node and to inject
crafted blocks, which the gRPC and HTTP APIs cannot do. The client runs a
small transport server of its own, because a node answers a peer by
dialing it back at the address the peer advertises.
"""

from __future__ import annotations

import hashlib
import os
import socket
import threading
import time
from concurrent import futures
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Generator, Iterator, List, Tuple

import grpc
import lz4.block
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from eth_hash.auto import keccak
from f1r3fly.crypto import commitment_hash
from f1r3fly.pb import CasperMessage_pb2
from f1r3fly.pb.CasperMessage_pb2 import BlockMessageProto as BlockMessage
from f1r3fly.pb.CasperMessage_pb2 import BlockRequestProto as BlockRequest
from f1r3fly.pb.routing_pb2 import (
    Ack,
    Chunk,
    Header,
    InternalServerError,
    Packet,
    Protocol,
    ProtocolHandshake,
    TLRequest,
    TLResponse,
)
from f1r3fly.pb.routing_pb2 import Node as NodeProto
from f1r3fly.pb.routing_pb2_grpc import (
    TransportLayerServicer,
    TransportLayerStub,
    add_TransportLayerServicer_to_server,
)

from .node import Node

_HANDSHAKE_RESPONSE = "ProtocolHandshakeResponse"


class P2PTimeoutError(Exception):
    """A node did not deliver the expected message in time."""

    def __init__(self, expected: str, node: Node):
        super().__init__(f"{node.name} did not deliver {expected}")
        self.node = node


def _node_id(public_key: ec.EllipticCurvePublicKey) -> bytes:
    """The node ID a TLS key stands for: the last 20 bytes of
    ``keccak(x || y)`` of its secp256r1 public key."""
    numbers = public_key.public_numbers()
    return keccak(numbers.x.to_bytes(32, "big") + numbers.y.to_bytes(32, "big"))[12:]


def _generate_credentials() -> Tuple[bytes, bytes]:
    """A fresh self-signed secp256r1 certificate and PKCS#8 key whose CN is
    the node ID of the key, as the node's TLS verification requires."""
    private_key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, _node_id(private_key.public_key()).hex())]
    )
    now = datetime.now(timezone.utc)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=365))
        .sign(private_key, hashes.SHA256())
    )
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return certificate.public_bytes(serialization.Encoding.PEM), key_pem


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _decompress(content: bytes, length: int) -> bytes:
    """Undo the node's chunk compression: an LZ4 block behind a varint of
    its uncompressed length."""
    encoded, shift, offset = 0, 0, 0
    while True:
        byte = content[offset]
        offset += 1
        encoded |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    if encoded != length:
        raise ValueError(f"compressed content claims {encoded} bytes, header says {length}")
    return lz4.block.decompress(content[offset:], uncompressed_size=length)


def _decode(type_id: str, content: bytes) -> object:
    """Parse a packet by its type id. A type without a CasperMessage proto
    (none is expected from a node) is kept as its raw bytes."""
    message_cls = getattr(CasperMessage_pb2, f"{type_id}Proto", None)
    if message_cls is None:
        return content
    message = message_cls()
    message.ParseFromString(content)
    return message


class _Inbox:
    """Messages received from nodes, taken by the first waiter they match."""

    def __init__(self) -> None:
        self._items: List[Tuple[str, object]] = []
        self._condition = threading.Condition()

    def put(self, type_id: str, message: object) -> None:
        with self._condition:
            self._items.append((type_id, message))
            self._condition.notify_all()

    def take(self, matches: Callable[[str, object], bool], timeout: float) -> object:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                for index, (type_id, message) in enumerate(self._items):
                    if matches(type_id, message):
                        del self._items[index]
                        return message
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                self._condition.wait(remaining)


class _TransportServer(TransportLayerServicer):
    def __init__(self, header: Header, inbox: _Inbox) -> None:
        super().__init__()
        self._header = header
        self._inbox = inbox

    def Send(self, request: TLRequest, context: grpc.ServicerContext) -> TLResponse:  # noqa: N802
        protocol = request.protocol
        kind = protocol.WhichOneof("message")
        if kind == "packet":
            packet = protocol.packet
            self._inbox.put(packet.typeId, _decode(packet.typeId, packet.content))
        elif kind == "protocol_handshake_response":
            self._inbox.put(_HANDSHAKE_RESPONSE, protocol.header)
        return TLResponse(ack=Ack(header=self._header))

    def Stream(  # noqa: N802
        self, request_iterator: Iterator[Chunk], context: grpc.ServicerContext
    ) -> TLResponse:
        header = None
        data = bytearray()
        for chunk in request_iterator:
            kind = chunk.WhichOneof("content")
            if kind == "header":
                header = chunk.header
            elif kind == "data":
                data += chunk.data.contentData
        if header is None:
            return TLResponse(
                internalServerError=InternalServerError(error=b"stream carried no header")
            )
        content = bytes(data)
        if header.compressed:
            content = _decompress(content, header.contentLength)
        self._inbox.put(header.typeId, _decode(header.typeId, content))
        return TLResponse(ack=Ack(header=self._header))


class NodeClient:
    """A peer on nodes' P2P transport, with its own TLS identity.

    A node accepts only messages naming its network, and pushed blocks only
    from a peer naming its chain. Before its first packet to a node, the
    client reads the node's network from its status and handshakes, then
    names the network and chain the node answers with.
    """

    def __init__(self, reply_host: str, receive_timeout: float) -> None:
        self._certificate_pem, self._key_pem = _generate_credentials()
        private_key = serialization.load_pem_private_key(self._key_pem, password=None)
        assert isinstance(private_key, ec.EllipticCurvePrivateKey)
        self._receive_timeout = receive_timeout
        self._inbox = _Inbox()
        self._identities: Dict[Tuple[str, int], Header] = {}
        self._server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
        port = self._server.add_secure_port(
            f"{reply_host}:0",
            grpc.ssl_server_credentials([(self._key_pem, self._certificate_pem)]),
        )
        self._sender = NodeProto(
            id=_node_id(private_key.public_key()),
            host=reply_host.encode(),
            tcp_port=port,
            udp_port=_free_port(),
        )
        add_TransportLayerServicer_to_server(
            _TransportServer(Header(sender=self._sender), self._inbox), self._server
        )
        self._server.start()

    def send_block(self, block: BlockMessage, peer: Node) -> None:
        """Push ``block`` to ``peer``."""
        self._send_packet(Packet(typeId="BlockMessage", content=block.SerializeToString()), peer)

    def block_request(self, block_hash: str, peer: Node) -> BlockMessage:
        """Fetch the block ``block_hash`` (hex) from ``peer``."""
        wanted = bytes.fromhex(block_hash)
        request = BlockRequest(hash=wanted)
        self._send_packet(Packet(typeId="BlockRequest", content=request.SerializeToString()), peer)
        try:
            block = self._inbox.take(
                lambda type_id, message: type_id == "BlockMessage" and message.blockHash == wanted,
                self._receive_timeout,
            )
        except TimeoutError as e:
            raise P2PTimeoutError(f"block {block_hash}", peer) from e
        assert isinstance(block, BlockMessage)
        return block

    def stop(self) -> None:
        self._server.stop(0)

    def _send_packet(self, packet: Packet, peer: Node) -> None:
        identity = self._identity_of(peer)
        header = Header(sender=self._sender, networkId=identity.networkId, chainId=identity.chainId)
        self._send(Protocol(header=header, packet=packet), peer)

    def _identity_of(self, peer: Node) -> Header:
        address = peer.p2p_address()
        if address not in self._identities:
            peer_id = self._peer_node_id(peer)
            header = Header(sender=self._sender, networkId=peer.grpc_status().networkId)
            handshake = ProtocolHandshake(nonce=os.urandom(16))
            self._send(Protocol(header=header, protocol_handshake=handshake), peer)
            try:
                response = self._inbox.take(
                    lambda type_id, message: (
                        type_id == _HANDSHAKE_RESPONSE and message.sender.id == peer_id
                    ),
                    self._receive_timeout,
                )
            except TimeoutError as e:
                raise P2PTimeoutError("a handshake response", peer) from e
            assert isinstance(response, Header)
            self._identities[address] = response
        return self._identities[address]

    def _send(self, protocol: Protocol, peer: Node) -> None:
        host, port = peer.p2p_address()
        credentials = grpc.ssl_channel_credentials(
            root_certificates=peer.tls_certificate(),
            private_key=self._key_pem,
            certificate_chain=self._certificate_pem,
        )
        channel = grpc.secure_channel(
            f"{host}:{port}",
            credentials,
            options=(("grpc.ssl_target_name_override", self._peer_node_id(peer).hex()),),
        )
        try:
            TransportLayerStub(channel).Send(
                TLRequest(protocol=protocol), timeout=self._receive_timeout
            )
        finally:
            channel.close()

    @staticmethod
    def _peer_node_id(peer: Node) -> bytes:
        public_key = x509.load_pem_x509_certificate(peer.tls_certificate()).public_key()
        assert isinstance(public_key, ec.EllipticCurvePublicKey)
        return _node_id(public_key)


@contextmanager
def p2p_protocol_client(
    reply_host: str, receive_timeout: float
) -> Generator[NodeClient, None, None]:
    """Yield a ``NodeClient`` whose transport server listens on
    ``reply_host``, an address the target nodes can dial."""
    client = NodeClient(reply_host, receive_timeout)
    try:
        yield client
    finally:
        client.stop()


def generate_block_hash() -> bytes:
    """A fixed digest no valid block carries, for tampering a block's hash."""
    return hashlib.blake2b(b"evil", digest_size=32).digest()


def slash_evidence(block: BlockMessage) -> List[Tuple[bytes, bytes]]:
    """The hashes of the two blocks each slash in ``block`` proves were signed
    at one sequence number, in the order the slash carries them."""
    return [
        (
            commitment_hash(deploy.systemDeploy.slashSystemDeploy.first),
            commitment_hash(deploy.systemDeploy.slashSystemDeploy.second),
        )
        for deploy in block.body.systemDeploys
        if deploy.systemDeploy.WhichOneof("systemDeploy") == "slashSystemDeploy"
    ]
