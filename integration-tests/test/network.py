from typing import (
    TYPE_CHECKING,
    List,
)

if TYPE_CHECKING:
    # pylint: disable=cyclic-import
    from .rnode import (
        Node,
    )


class Network:
    def __init__(self, network: str, bootstrap: "Node", peers: List["Node"]):
        self.network = network
        self.bootstrap = bootstrap
        self.peers = peers
        self.nodes = [bootstrap] + peers
