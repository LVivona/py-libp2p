import os
import sys

import random
import base58
import secrets
import logging
import argparse

import trio
import multiaddr

from typing import Literal, Optional, List, Union

from libp2p import new_host
from libp2p.abc import IHost
from libp2p.crypto.secp256k1 import create_new_key_pair
from libp2p.kad_dht.kad_dht import DHTMode, KadDHT
from libp2p.kad_dht.utils import create_key_from_binary
from libp2p.tools.async_service import background_trio_service
from libp2p.tools.utils import info_from_p2p_addr
from libp2p.exceptions import BaseLibp2pError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("sfsn-kademlia-example")

# Configure DHT module loggers to inherit from the parent logger
# This ensures all kademlia-example.* loggers use the same configuration
# Get the directory where this script is located
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_ADDR_LOG = os.path.join(SCRIPT_DIR, "server_node_addr.txt")

# Set the level for all child loggers
for module in [
    "kad_dht",
    "value_store",
    "peer_routing",
    "routing_table",
    "provider_store",
]:
    child_logger = logging.getLogger(f"kademlia-example.{module}")
    child_logger.setLevel(logging.INFO)
    child_logger.propagate = True  # Allow propagation to parent

VALIDATE_PORT_RANGE = lambda port: not bool(port >> 16)
bootstrap_nodes = []


async def connect_to_bootstrap_nodes(
    host: IHost, bootstrap_nodes: List[multiaddr.Multiaddr]
):
    for nodes in bootstrap_nodes:
        try:
            p_info = info_from_p2p_addr(nodes)
            logger.info(f"{p_info.addrs} {p_info.peer_id}")
            host.get_peerstore().add_addrs(p_info.peer_id, p_info.addrs, 3600)
            await host.connect(p_info)
        except BaseLibp2pError as e:
            logger.error(f"Failed to connect to bootstrap node {nodes}: {e}")


def save_server_addr(addr: str) -> None:
    """Append the server's multiaddress to the log file."""
    try:
        with open(SERVER_ADDR_LOG, "w") as f:
            f.write(addr + "\n")
        logger.info(f"Saved server address to log: {addr}")
    except Exception as e:
        logger.error(f"Failed to save server address: {e}")


def load_server_addrs() -> list[str]:
    """Load all server multiaddresses from the log file."""
    if not os.path.exists(SERVER_ADDR_LOG):
        return []
    try:
        with open(SERVER_ADDR_LOG) as f:
            return [line.strip() for line in f if line.strip()]
    except Exception as e:
        logger.error(f"Failed to load server addresses: {e}")
        return []


async def main(
    port: int,
    mode: Literal["server", "clinet"],
    key: str,
    path: Optional[Union[os.PathLike, str]] = None,
    bootstrap_addrs: Optional[List[str]] = None,
):
    """Main loop that runs client/server function."""
    try:
        addr = "0.0.0.0"
        if port <= 0:
            port = random.randint(10000, 60000)
        assert VALIDATE_PORT_RANGE(port)
        dht_mode = None
        if mode is None or mode.lower() == "client":
            dht_mode = DHTMode.CLIENT
        elif mode.lower() == "server":
            dht_mode = DHTMode.SERVER
        else:
            logger.error(f"Invalid mode: {mode}. Must be 'client' or 'server'")
            sys.exit(1)

        # Load server addresses for client mode
        if dht_mode == DHTMode.CLIENT:
            server_addrs = load_server_addrs()
            if server_addrs:
                logger.info(f"Loaded {len(server_addrs)} server addresses from log")
                for server in server_addrs:
                    bootstrap_nodes.append(
                        multiaddr.Multiaddr(server)
                    )  # Use the first server address
            else:
                logger.warning("No server addresses found in log file")

        if bootstrap_addrs:
            for b_addr in bootstrap_addrs:
                bootstrap_nodes.append(multiaddr.Multiaddr(b_addr))

        # create address to listen
        key_pair = create_new_key_pair(secrets.token_bytes(32))
        host = new_host(key_pair=key_pair)

        listen_addr = [
            multiaddr.Multiaddr(f"/ip4/{addr}/tcp/{port}"),
            multiaddr.Multiaddr(f"/ip6/::1/tcp/{port}"),
        ]

        async with host.run(listen_addrs=listen_addr):
            peer_id = host.get_id().pretty()
            addr_str = f"/ip4/{addr}/tcp/{port}/p2p/{peer_id}"
            await connect_to_bootstrap_nodes(host, bootstrap_nodes)
            dht = KadDHT(host, dht_mode)
            for peer_id in host.get_peerstore().peer_ids():
                await dht.routing_table.add_peer(peer_id)
            logger.info(f"Connected to bootstrap nodes: {host.get_connected_peers()}")
            bootstrap_cmd = f"--bootstrap {addr_str}"
            logger.info("To connect to this node, use: %s", bootstrap_cmd)

            if dht_mode == DHTMode.SERVER:
                save_server_addr(addr_str)

            async with background_trio_service(dht):
                logger.info(f"DHT service started in {dht_mode.value} mode")
                value_key = create_key_from_binary(str(path).encode())

                content = key.encode()
                content_key = create_key_from_binary(content)

                if dht_mode == DHTMode.SERVER:
                    # publish node information.
                    file = open(path, "rb")
                    value_data = file.read(-1)
                    await dht.put_value(value_key, value_data)
                    logger.info(
                        f"Stored value '{value_key}'"
                        f"with key: {base58.b58encode(value_key).decode()}"
                    )
                    success = await dht.provider_store.provide(content_key)
                    if success:
                        logger.info(
                            "Successfully advertised as server"
                            f"for content: {content_key.hex()}"
                        )
                    else:
                        logger.warning("Failed to advertise as content server")
                else:
                    # retrieve the value
                    logger.info(
                        "Looking up key: %s", base58.b58encode(value_key).decode()
                    )

                    val_data = await dht.get_value(value_key)
                    if val_data:
                        try:
                            logger.info(f"Retrieved value: {val_data.decode()}")
                        except UnicodeDecodeError:
                            logger.info(f"Retrieved value (bytes): {val_data!r}")
                            return
                    else:
                        logger.warning("Failed to retrieve value")

                    # Also check if we can find servers for our own content
                    logger.info("Looking for servers of content: %s", content_key.hex())
                    providers = await dht.provider_store.find_providers(content_key)

                    if providers:
                        logger.info(
                            "Found %d servers for content: %s",
                            len(providers),
                            [p.peer_id.pretty() for p in providers],
                        )
                    else:
                        logger.warning(
                            "No servers found for content %s", content_key.hex()
                        )
                    return

                while True:
                    logger.debug(
                        "Status - Connected peers: %d,"
                        "Peers in store: %d, Values in store: %d",
                        len(dht.host.get_connected_peers()),
                        len(dht.host.get_peerstore().peer_ids()),
                        len(dht.value_store.store),
                    )
                    await trio.sleep(10)
    except Exception as e:
        logger.error(f"Server node error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    description = r"""
┌──────────────────────────────────────────────────────────────┐
│        ___           ___           ___           ___         │
│       /\__\         /\__\         /\__\         /\  \        │
│      /:/ _/_       /:/ _/_       /:/ _/_        \:\  \       │
│     /:/ /\  \     /:/ /\__\     /:/ /\  \        \:\  \      │
│    /:/ /::\  \   /:/ /:/  /    /:/ /::\  \   _____\:\  \     │
│   /:/_/:/\:\__\ /:/_/:/  /    /:/_/:/\:\__\ /::::::::\__\    │
│   \:\/:/ /:/  / \:\/:/  /     \:\/:/ /:/  / \:\~~\~~\/__/    │
│    \::/ /:/  /   \::/__/       \::/ /:/  /   \:\  \          │
│     \/_/:/  /     \:\  \        \/_/:/  /     \:\  \         │
│       /:/  /       \:\__\         /:/  /       \:\__\        │
│       \/__/         \/__/         \/__/         \/__/        │
│                                                              │
│                Simple File Sharing Network                   │
│       LibP2P Kademlia DHT Node - Client/Server Mode          │
└──────────────────────────────────────────────────────────────┘

This script allows running a libp2p node that participates in a Kademlia DHT network.
It supports both client and server modes.

In server mode:
  - The node advertises and stores a file on the network.
  - It logs its address to `server_node_addr.txt` so that future clients can connect.

In client mode:
  - The node attempts to connect to bootstrap peers.
  - It retrieves values and searches for providers using the specified key.

Example usage (server):
  python script.py --mode server -f ./example.txt -k examplekey

Example usage (client):
  python script.py --mode client -f ./example.txt -k examplekey \
    --bootstrap /ip4/127.0.0.1/tcp/12345/p2p/QmNodeIdHere
"""

    parser = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "-p", "--port", default=0, type=int, help="source peer port number"
    )

    parser.add_argument(
        "--mode",
        default="server",
        help="Run as a server or client node",
    )
    parser.add_argument(
        "--bootstrap",
        type=str,
        nargs="*",
        help=(
            "Multiaddrs of bootstrap nodes. "
            "Provide a space-separated list of addresses. "
            "This is required for client mode."
        ),
    )
    parser.add_argument(
        "-f",
        "--file",
        type=str,
        help="Name of the file we would like to serve.",
        required=True,
    )
    parser.add_argument(
        "-k", "--key", type=str, help="Content key of providers.", required=True
    )

    # add option to use verbose logging
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
    else:
        logging.getLogger().setLevel(logging.INFO)

    try:
        trio.run(
            main,
            *(args.port, args.mode, args.key, args.file, args.bootstrap),
        )
    except KeyboardInterrupt:
        pass
    except BaseLibp2pError as e:
        logger.critical(f"Script failed: {e}", exc_info=True)
        sys.exit(1)
