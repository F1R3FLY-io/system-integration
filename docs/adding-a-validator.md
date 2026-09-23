# Adding a Validator to a Running Shard

What an SI shard supplies for the bonding procedure: which node to talk to,
which key to pay with, and how long each stage takes. The commands themselves
live in the `rust-client` guide —
[Bonding a validator](https://github.com/F1R3FLY-io/rust-client/blob/dev/docs/guides/bonding-a-validator.md).

## What this shard gives you

| Fact | Value (default `compose/f1r3node-rust.yml`) |
|---|---|
| Bonded validators | `rnode.validator1/2/3`, listed in `genesis/bonds.txt` |
| Not bonded | `rnode.bootstrap` (ceremony master, `--heartbeat-disabled`) and `rnode.readonly` |
| Submit deploys to | a bonded validator — validator1 is gRPC 40412, HTTP 40413 |
| Run queries against | `rnode.readonly` — gRPC 40452, HTTP 40453 |
| Epoch length | 50 blocks (`conf/rust.conf`) |
| Quarantine length | 10 blocks (`conf/rust.conf`) |
| Funded keys | the vaults in `genesis/wallets.txt`; validator keys are in `.env.node` |

`compose/f1r3node-rust-validator4.yml` adds a fourth validator on ports
40440-40445. It joins the shard as soon as it starts; bonding is a separate
step and does not happen automatically.

## Order of operations

1. **Start the joiner and let it sync.** It reports `not bonded` and does not
   propose. That is expected — bond it only once it is Running and following
   the tip.
2. **Fund its vault.** The bond is paid for by the key being bonded. SI's
   genesis funds the wallets in `genesis/wallets.txt`, and a new key is not
   among them, so transfer to it from a funded key first. Budget the deploy's
   phlo limit **plus** the stake: an under-funded vault produces a deploy that
   is included in a block and then fails there with
   `Deploy payment failed: Insufficient funds`.
3. **Bond, from a bonded validator.** Submitting the bond deploy to
   `rnode.bootstrap` — the node whose port the README mentions first — accepts
   it and strands it forever.
4. **Wait for the epoch boundary.** With `epoch-length = 50`, a bond waits up
   to 50 blocks to enter the active set. Until then it is bonded but not
   active.
5. **Unbonding reverses it**, over two boundaries plus `quarantine-length`
   before the stake and rewards are paid back.

## Verifying

```bash
# bonds and active set (read-only node, HTTP port)
node_cli bonds -H localhost -p 40453
node_cli active-validators -H localhost -p 40453

# one validator's bond / withdrawal state
node_cli validator-status -k <public key> --http-port 40453
```

`/api/status`'s `isValidator` field does **not** answer "is this node a bonded
validator" — it reports whether autopropose is enabled, and a bonded,
proposing validator shows `false`. Use `validator-status`.

## Only bond a validator you will keep running

A bonded validator that does not propose still counts toward consensus while
contributing nothing to it, and it degrades the shard for anyone who joins
later. Unbond it rather than leaving it idle.
