"""
File Upload/Download Integration Tests (Streamlined)

Focused E2E tests that verify the full pipeline across real Docker nodes.
Logic already covered by Scala unit tests (SyntheticDeploySpec, FileUploadCostSpec,
DownloadFileAPITest, FileSystemProcessSpec, FileReplicationSpec, etc.) is NOT
duplicated here.

Tests:
  1. Cross-node replication: upload → replicate → download from readonly
     → owner-delete → verify gone → unauthorized-delete → verify intact

Uses the session-scoped shard fixture with heartbeat-driven block creation.
"""

import logging
import time

import grpc
import pytest
from docker.client import DockerClient
from f1r3fly.client import F1r3flyClient, F1r3flyClientException
from f1r3fly.pb.DeployServiceV1_pb2 import FileDownloadRequest
from f1r3fly.util import blake2b_256_hex, create_file_upload_metadata

from .common import TestingContext
from .conftest import (
    ALL_CONTAINERS,
    VALIDATOR1_KEY,
    VALIDATOR2_KEY,
    assert_containers_running,
)
from .rnode import Node, _GRPC_OPTIONS, default_shard_id, DOCKER_HOST

pytestmark = pytest.mark.xdist_group("shard")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _generate_test_data(size: int = 1024, salt: str = '') -> bytes:
    """Generate deterministic test data of the given size.
    
    If salt is provided, it is mixed into the pattern to ensure
    different calls produce unique content (and therefore unique hashes).
    """
    pattern = f'F1R3FLY-test-data-{salt}-'.encode() if salt else b'F1R3FLY-test-data-'
    repeats = (size // len(pattern)) + 1
    return (pattern * repeats)[:size]


def _upload_file(
    node: Node,
    private_key,
    data: bytes,
    file_name: str = 'test-file.bin',
    phlo_limit: int = 500_000_000,
    phlo_price: int = 1,
    shard_id: str = default_shard_id,
):
    """Upload a file to the given node and return the FileUploadResult."""
    file_hash = blake2b_256_hex(data)
    file_size = len(data)
    valid_after_block_no = max(0, node.get_current_block_number() - 1)

    metadata = create_file_upload_metadata(
        key=private_key,
        file_hash=file_hash,
        file_size=file_size,
        file_name=file_name,
        phlo_price=phlo_price,
        phlo_limit=phlo_limit,
        valid_after_block_no=valid_after_block_no,
        shard_id=shard_id,
    )

    with F1r3flyClient(
        DOCKER_HOST, node.get_external_grpc_port(), grpc_options=_GRPC_OPTIONS
    ) as client:
        return client.upload_file(metadata, data)


# Extended gRPC options for downloads.
_DOWNLOAD_GRPC_OPTIONS: tuple = (
    ('grpc.enable_retries', 0),
    ('grpc.keepalive_time_ms', 30000),
    ('grpc.keepalive_timeout_ms', 10000),
    ('grpc.keepalive_permit_without_calls', 1),
    ('grpc.max_receive_message_length', 16 * 1024 * 1024),
    ('grpc.http2.max_pings_without_data', 0),
    ('grpc.http2.min_time_between_pings_ms', 10000),
    ('grpc.http2.min_ping_interval_without_data_ms', 10000),
)


def _download_file(node: Node, file_hash: str, offset: int = 0,
                   timeout: float = 600) -> bytes:
    """Download a file from the given node."""
    with F1r3flyClient(
        DOCKER_HOST, node.get_external_grpc_port(),
        grpc_options=_DOWNLOAD_GRPC_OPTIONS
    ) as client:
        request = FileDownloadRequest(fileHash=file_hash, offset=offset)
        response_stream = client._deploy_stub.downloadFile(
            request, timeout=timeout
        )
        chunks = []
        for chunk in response_stream:
            which = chunk.WhichOneof('chunk')
            if which == 'data':
                chunks.append(chunk.data)
        return b''.join(chunks)


def _wait_for_deploy_in_block(node: Node, deploy_id: str, timeout: float):
    """Poll find_deploy until the deploy is included in a block."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            return node.find_deploy(deploy_id)
        except F1r3flyClientException:
            time.sleep(3)
    pytest.fail(
        f"Deploy {deploy_id[:24]}... not found in a block within {timeout}s"
    )


def _wait_for_finalization(node: Node, block_hash: str, timeout: float = 120):
    """Poll until block is finalized."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if node.is_finalized(block_hash):
            return
        time.sleep(3)
    pytest.fail(f"Block {block_hash[:16]} not finalized within {timeout}s")


def _download_with_retry(node: Node, file_hash: str, node_name: str, timeout: int) -> bytes:
    """Download a file with retry loop for finalization and P2P propagation delay."""
    start_time = time.time()
    last_err = None
    while time.time() - start_time < timeout:
        try:
            return _download_file(node, file_hash)
        except (F1r3flyClientException, grpc.RpcError) as e:
            last_err = e
            time.sleep(2)

    # Try to extract grpc details
    err_details = str(last_err)
    if hasattr(last_err, "details") and hasattr(last_err, "code"):
        err_details += f" | Code: {last_err.code()} | Details: {last_err.details()}"

    pytest.fail(
        f"File {file_hash[:16]}... not downloadable from {node_name} "
        f"within {timeout}s: {err_details}"
    )


def _make_delete_script(file_hash: str) -> str:
    """Generate a Rholang script that deletes a file via the FileRegistry contract."""
    return f"""
new rl(`rho:registry:lookup`), fileRegistryCh, deployData(`rho:deploy:data`), deployDataCh,
    authKeyCh, fileHandleCh, deleteCh, deployerIdOps(`rho:system:deployerId:ops`), pubKeyCh,
    stdout(`rho:io:stdout`)
in {{
  rl!(`rho:id:m6rqma7yas7o6ieos45ai4dskmc6zugs9rmsp6i3zan8qe5hsfqsdt`, *fileRegistryCh) |
  for (@(_, FileRegistry) <- fileRegistryCh) {{
    deployData!(*deployDataCh) |
    for (_, deployerId, _ <- deployDataCh) {{
      deployerIdOps!("pubKeyBytes", *deployerId, *pubKeyCh) |
      for (@pubKeyBytes <- pubKeyCh) {{
        @FileRegistry!("ownerAuthKey", "{file_hash}", *deployerId, *authKeyCh) |
        for (@authKey <- authKeyCh) {{
          if (authKey != Nil) {{
            @FileRegistry!("lookup", "{file_hash}", *fileHandleCh) |
            for (@fileHandle <- fileHandleCh) {{
              if (fileHandle != Nil) {{
                @fileHandle!("delete", pubKeyBytes, authKey, *deleteCh) |
                for (@deleteResult <- deleteCh) {{
                  stdout!(("DELETE_RESULT", deleteResult))
                }}
              }}
            }}
          }}
        }}
      }}
    }}
  }}
}}
"""


# ---------------------------------------------------------------------------
# Tests — Focused E2E
# ---------------------------------------------------------------------------


class TestFileUploadE2E:
    """End-to-end file upload/download tests across real Docker nodes.

    These test only what unit tests cannot: the full gRPC → synthetic deploy →
    consensus → finalization → download pipeline across actual containers.

    Logic already covered by Scala unit tests is NOT duplicated here.
    See implementation_plan.md for the full keep/drop analysis.
    """

    def test_cross_node_replication(
        self,
        docker_client: DockerClient,
        testing_context: TestingContext,
        validator1_node: Node,
        validator2_node: Node,
        readonly_node: Node,
    ) -> None:
        """Full lifecycle: upload → replicate → download → delete → unauthorized-delete.

        Validates: (1) file replicates across the shard, (2) download from
        readonly works, (3) owner can delete, (4) non-owner delete is denied.
        """
        assert_containers_running(docker_client, ALL_CONTAINERS)

        data = _generate_test_data(4096, salt='file1')
        result = _upload_file(validator1_node, VALIDATOR1_KEY, data)

        # Wait for block inclusion on validator1
        find_timeout = int(120 * testing_context.timeout_scale)
        block = _wait_for_deploy_in_block(
            validator1_node, result.deployId, find_timeout,
        )
        logging.info("Deploy in block #%d on validator1", block.blockNumber)

        # Verify validator2 has the deploy (block was replicated)
        v2_timeout = int(120 * testing_context.timeout_scale)
        v2_block = _wait_for_deploy_in_block(
            validator2_node, result.deployId, v2_timeout,
        )
        assert v2_block.blockHash != ''
        logging.info(
            "Deploy found on validator2 in block #%d (%s) — replication OK",
            v2_block.blockNumber, v2_block.blockHash[:16],
        )

        # Verify readonly (observer) has the deploy
        readonly_timeout = int(120 * testing_context.timeout_scale)
        readonly_block = _wait_for_deploy_in_block(
            readonly_node, result.deployId, readonly_timeout,
        )
        assert readonly_block.blockHash != ''
        logging.info(
            "Deploy found on readonly in block #%d (%s) — replication OK",
            readonly_block.blockNumber, readonly_block.blockHash[:16],
        )

        dl_timeout = int(120 * testing_context.timeout_scale)
        _wait_for_finalization(readonly_node, readonly_block.blockHash, dl_timeout)
        downloaded = _download_with_retry(
            readonly_node, result.fileHash, "readonly", dl_timeout
        )
        assert downloaded == data, "Downloaded data does not match original"

        # Upload File 2 on validator1 (by Alice)
        data2 = _generate_test_data(4096, salt='file2')
        result2 = _upload_file(validator1_node, VALIDATOR1_KEY, data2)
        block2 = _wait_for_deploy_in_block(
            validator1_node, result2.deployId, find_timeout,
        )
        logging.info("Deploy 2 (file2) in block #%d on validator1", block2.blockNumber)
        _wait_for_deploy_in_block(validator2_node, result2.deployId, v2_timeout)
        readonly_block2 = _wait_for_deploy_in_block(readonly_node, result2.deployId, readonly_timeout)
        _wait_for_finalization(readonly_node, readonly_block2.blockHash, dl_timeout)
        
        # Delete file 1 (by Alice — the owner)
        rho_script_alice = _make_delete_script(result.fileHash)

        delete_deploy_id = validator1_node.deploy_string(rho_script_alice, VALIDATOR1_KEY, phlo_limit=500_000_000)
        delete_block = _wait_for_deploy_in_block(validator1_node, delete_deploy_id, find_timeout)
        binfo = validator1_node.get_block(delete_block.blockHash)
        for d in binfo.deploys:
            if d.sig == delete_deploy_id:
                assert not d.errored, f"Delete deploy failed: {getattr(d, 'systemDeployError', 'Unknown Error')}"
                break
        else:
            pytest.fail(f"Delete deploy {delete_deploy_id[:24]}... not found in block deploys")

        logging.info("Delete deploy in block #%d (%s) on validator1", delete_block.blockNumber, delete_block.blockHash[:16])

        # Wait for the delete block to be propagated and finalized on the readonly node too!
        _wait_for_finalization(readonly_node, delete_block.blockHash, dl_timeout)
        logging.info("Delete block finalized on readonly_node")

        try:
            _download_file(readonly_node, result.fileHash, timeout=5)
            assert False, "Alice's file download succeeded when it should have failed!"
        except grpc.RpcError as e:
            logging.info(f"CAPTURED GRPC ERROR for Alice: code={e.code()}, details={e.details()}, str={str(e)}")
            assert e.code() in [grpc.StatusCode.UNKNOWN, grpc.StatusCode.NOT_FOUND, grpc.StatusCode.PERMISSION_DENIED], f"Unexpected grpc code: {e.code()}"

        # -------------------------------------------------------------------
        # Delete file 2 by Bob (should fail authorization under the hood)
        # -------------------------------------------------------------------
        rho_script_bob = _make_delete_script(result2.fileHash)
        bob_delete_deploy_id = validator1_node.deploy_string(rho_script_bob, VALIDATOR2_KEY, phlo_limit=500_000_000)
        bob_delete_block = _wait_for_deploy_in_block(validator1_node, bob_delete_deploy_id, find_timeout)
        binfo_bob = validator1_node.get_block(bob_delete_block.blockHash)
        for d in binfo_bob.deploys:
            if d.sig == bob_delete_deploy_id:
                assert not d.errored, f"Delete deploy failed natively for Bob: {getattr(d, 'systemDeployError', 'Unknown Error')}"
                break
        else:
            pytest.fail(f"Bob's delete deploy {bob_delete_deploy_id[:24]}... not found in block deploys")
        
        _wait_for_finalization(readonly_node, bob_delete_block.blockHash, dl_timeout)
        logging.info("Bob's delete block finalized on readonly_node")

        # Verify that Bob's deletion attempt did NOT work, and file2 is STILL DOWNLOADABLE
        downloaded_file2 = _download_with_retry(
            readonly_node, result2.fileHash, "readonly", dl_timeout
        )
        assert downloaded_file2 == data2, "Bob somehow successfully deleted Alice's file!"
        logging.info("Bob's unauthorized deletion correctly failed; File 2 is fully intact.")

