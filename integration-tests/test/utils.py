import os
import platform
import socket
import subprocess
from collections import defaultdict
from contextlib import contextmanager
from typing import Dict, Generator, Set

from docker import DockerClient


def _is_native_linux() -> bool:
    """Check if running on native Linux (not WSL2).

    WSL2 is detected as Linux but can't route to Docker bridge networks,
    so it needs to use port mapping like macOS/Windows.
    """
    if platform.system() != "Linux":
        return False
    release = platform.uname().release.lower()
    return "microsoft" not in release and "wsl" not in release


ISLINUX = _is_native_linux()


def parse_mvdag_str(mvdag_output: str) -> Dict[str, Set[str]]:
    dag_dict: Dict[str, Set[str]] = defaultdict(set)

    lines = mvdag_output.splitlines()
    for line in lines:
        parent_hash, child_hash = line.split(" ")
        dag_dict[parent_hash].add(child_hash)
    return dag_dict


def get_current_container_id() -> str:
    hostname = subprocess.run(["hostname"], check=True, stdout=subprocess.PIPE)
    return hostname.stdout.decode("utf8").strip("\n")


@contextmanager
def get_node_ip_of_network(
    docker_client: DockerClient, network_name: str
) -> Generator[str, None, None]:
    """Get an IP address that can reach containers on the given Docker network.

    In CI (DRONE=true), attaches the current container to the network.
    On native Linux, returns the gateway IP.
    On WSL2/macOS/Windows, yields 'localhost' (use port mapping instead).
    """
    if os.environ.get("DRONE") == "true":
        current_container_id = get_current_container_id()
        current_container = docker_client.containers.get(current_container_id)
        network = docker_client.networks.get(network_name)
        try:
            network.connect(current_container)
            network.reload()
            container_network_config = network.attrs["Containers"][current_container.id]
            ip, _ = container_network_config["IPv4Address"].split("/")
            yield ip
        finally:
            network.disconnect(current_container)
    elif ISLINUX:
        network_attr = docker_client.networks.get(network_name).attrs
        ipam_attr = network_attr.get("IPAM")
        assert ipam_attr is not None
        ip = ipam_attr["Config"][0]["Gateway"]
        yield ip
    else:
        yield "localhost"


def get_free_tcp_port() -> int:
    tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        tcp.bind(("", 0))
        _, port = tcp.getsockname()
    finally:
        tcp.close()
    return port
