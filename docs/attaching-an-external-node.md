# Attaching a Node From Outside the Host

How to join a node running on a **different machine** to a shard started by
these compose files. For the node's own flags and what a completed join looks
like, see the node repo's
[Joining an Existing Network](https://github.com/F1R3FLY-io/f1r3node-rust/blob/dev/docs/node/joining-a-network.md).

## The problem

Shard members advertise the hostnames they have on the `f1r3fly` Docker
network — `rnode.bootstrap`, `rnode.validator1`, and so on. Those names mean
nothing outside the host, and the container addresses behind them are not
routable from anywhere else.

Peering is not one-directional. A joiner that can reach the bootstrap node
still fails if the bootstrap node cannot reach **it**: the handshake reply
dials the joiner back at the address the joiner advertised. So an attach needs
three things:

1. The joiner can reach the shard's bootstrap port.
2. The shard can reach the joiner on the address it advertises.
3. The joiner can resolve the hostname the bootstrap node advertises for
   itself, because the handshake reply is addressed to that name.

Only the first is usually in place.

## Shape of a working setup

The joiner advertises an address the shard's containers can route to, and
something forwards that address back to the joiner.

**Reaching the joiner.** The simplest route that needs no public port on the
joiner's machine is a reverse SSH tunnel onto the Docker bridge gateway —
`172.18.0.1` on the shard host — which every container can reach. The joiner
opens it outbound:

```bash
ssh -N \
  -R 172.18.0.1:<protocol-port>:localhost:<protocol-port> \
  -R 172.18.0.1:<discovery-port>:localhost:<discovery-port> \
  <user>@<shard-host>
```

This needs `GatewayPorts clientspecified` in the shard host's sshd config —
without it, sshd binds the forward to loopback only and the containers cannot
reach it. It also needs the host firewall to allow the bridge subnet to reach
those ports, for example a rule accepting `172.18.0.0/16` on the port range you
chose.

**Resolving the bootstrap name.** Give the joiner's container a mapping for the
name the shard advertises:

```
--add-host rnode.bootstrap:<shard-host-ip>
```

**Joiner flags.** Advertise the gateway address, pick the ports you tunnelled,
and point at the bootstrap node's public address:

```
run --host=172.18.0.1 \
    --protocol-port=<protocol-port> --discovery-port=<discovery-port> \
    --bootstrap=rnode://<bootstrap-node-id>@<shard-host>?protocol=40400&discovery=40404 \
    --allow-private-addresses --no-upnp
```

`--allow-private-addresses` is required: the shard advertises private addresses
and the joiner rejects them without it. Read `<bootstrap-node-id>` from the
bootstrap node's `/api/status`, whose `address` field is the full `rnode://`
URL.

Use a distinct port band per joiner. Reusing a band that a previous joiner
used leaves the old identity in peers' routing tables pointing at the same
address.

## Notes

- **The shard host changes persist.** The sshd setting and the firewall rule
  outlive the joiner; remove them when the arrangement ends.
- **A departed joiner is not forgotten.** Shard nodes keep retrying its
  address, logging an error per attempt, and re-learn it from each other across
  restarts. Clearing it takes stopping every shard node at once.
- **Do not wipe the joiner's volume.** Its node identity is the TLS
  certificate inside it, and peers pin that identity.
- **Genesis funds no joiner key.** To bond one, transfer to it first — see
  [Adding a Validator](adding-a-validator.md).
