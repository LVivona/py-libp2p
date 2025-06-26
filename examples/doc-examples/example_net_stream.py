"""
Enhanced NetStream Example for py-libp2p with State Management

This example demonstrates the new NetStream features including:
- State tracking and transitions
- Proper error handling and validation
- Resource cleanup and event notifications
- Thread-safe operations with Trio locks

Based on the standard echo demo but enhanced to show NetStream state management.
"""

import argparse
import random
import secrets

import multiaddr
import trio


from libp2p import (
    new_host,
)
from libp2p.peer.id import (
    ID
)
from libp2p.crypto.secp256k1 import (
    create_new_key_pair,
)
from libp2p.abc import (
    IPeerStore,
    IPeerRouting,
)
from libp2p.custom_types import (
    TProtocol,
    TMuxerOptions,
    TSecurityOptions,
)
from libp2p.crypto.keys import (
    KeyPair
)
from libp2p.network.stream.exceptions import (
    StreamClosed,
    StreamEOF,
    StreamReset,
)
from libp2p.network.stream.net_stream import (
    NetStream,
    StreamState,
)
from libp2p.peer.peerinfo import (
    info_from_p2p_addr,
    PeerInfo
)


from typing import Optional, Literal, Sequence
from enum import Enum

PROTOCOL_ID = TProtocol("/echo/1.0.0")

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()],
)
logger = logging.getLogger("net_stream.example.libp2p")


class NetworkType(Enum):
    server = "server"
    client = "client"

class NetworkEcho:

    def __init__(
        self,
        key_pair: Optional[KeyPair] = None,
        muxer_opt: Optional[TMuxerOptions] = None,
        sec_opt: TSecurityOptions | None = None,
        peerstore_opt: IPeerStore | None = None,
        disc_opt: IPeerRouting | None = None,
        muxer_preference: Literal["YAMUX", "MPLEX"] | None = None,
        listen_addrs: Sequence[multiaddr.Multiaddr] | None = None,
        type : NetworkType = NetworkType.client,
    ):
        self.host = new_host(
            key_pair=key_pair,
            muxer_opt=muxer_opt,
            sec_opt=sec_opt,
            peerstore_opt=peerstore_opt,
            disc_opt=disc_opt,
            muxer_preference=muxer_preference,
            listen_addrs=listen_addrs,
        )
        self.protocol = PROTOCOL_ID
        self.listen_addrs = listen_addrs
        
        if type == NetworkType.server:
            self.host.set_stream_handler(self.protocol, self._handle_echo_stream)

    @staticmethod
    async def _handle_echo_stream(stream: NetStream) -> None:

        try:
            assert await stream.state == StreamState.OPEN
            assert await stream.is_readable()
            assert await stream.is_writable()
            logger.info("stream succesfully open state, waiting for client...")

            while await stream.is_readable():
                try:
                    # Read data from client
                    logger.info("Begining of readable. bytes.")
                    data = await stream.read(1024)
                    if not data:
                        logger.info("Received empty data, client may have closed")
                        break

                    logger.info(f"Received: {data.decode('utf-8').strip()}")

                    # Check if we can still write before echoing
                    if await stream.is_writable():
                        await stream.write(data)
                        logger.info(f"Echoed: {data.decode('utf-8').strip()}")
                    else:
                        logger.warning("Cannot echo - stream not writable")
                        break

                except StreamEOF:
                    logger.error("Client closed their write side (EOF)")
                    break
                except StreamReset:
                    logger.error("Stream was reset by client")
                    return
                except StreamClosed as e:
                    logger.error(f"Stream operation failed: {e}")
                    break

            # Demonstrate graceful closure
            current_state = await stream.state
            logger.info(f"Current state before close: {current_state}")

            if current_state not in [StreamState.CLOSE_BOTH, StreamState.CLOSE_READ, StreamState.RESET]:
                await stream.close()
                logger.info("Server closed write side")

            final_state = await stream.state
            logger.info(f"Final stream state: {final_state}")

        except Exception as e:
            logger.error(f"Handle error: {e}")
            if await stream.state not in [StreamState.RESET, StreamState.CLOSE_BOTH]:
                await stream.reset()
                logger.error("Stream reset due to error")
            raise e

    async def connect(self, peer_info : PeerInfo) -> None:
        await self.host.connect(peer_info)

    async def echo(self, peer_id: ID, message: bytes) -> None:
        """client stream"""
        try:
            stream = await self.host.new_stream(peer_id, [self.protocol])
            # Verify initial state
            assert await stream.state == StreamState.OPEN

            if await stream.is_writable():
                await stream.write(message)
                logger.info(f"Sent: {message.decode('utf-8').strip()}")
            else:
                logger.info("Cannot write - stream not writable")
                return
            
            
            await stream.close()
        

            # # Verify state transition
            state_after_close = await stream.state
            logger.info(f"State: {state_after_close}")
            assert state_after_close == StreamState.CLOSE_WRITE
            assert await stream.is_readable()  # Should still be readable
            assert not await stream.is_writable()  # Should not be writable

            # Try to write (should fail)
            try:
                await stream.write(b"This should fail")
                logger.debug("ERROR: Write succeeded when it should have failed!")
            except StreamClosed as e:
                logger.debug(f"✓ Expected error when writing to closed stream: {e}")

            # Read the echo response
            if await stream.is_readable():
                try:
                    response = await stream.read(len(message))
                    logger.info(f"Received echo: {response.decode('utf-8').strip()}")
                except StreamEOF:
                    logger.error("Server closed their write side")
                except StreamReset:
                    logger.error("Stream was reset")

            # Check final state
            final_state = await stream.state
            logger.info(f"Final client state: {final_state}")

        except Exception as e:
            logger.error(f"Client error: {e}")
            # Reset on error
            await stream.reset()
            logger.error("Client reset stream due to error")

    async def __aenter__(self) -> "NetworkEcho":
        """Start the host and return self for async context manager usage"""
        self._ctx = self.host.run(listen_addrs=self.listen_addrs)
        await self._ctx.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        """Close the host when exiting the async context manager"""
        await self._ctx.__aexit__(exc_type, exc_value, traceback)


async def run_demo(
    port: int, destination: str, seed: int | None = None, message: str | None = None
) -> None:
    """
    Run echo demo with NetStream state management.
    """

    if port <= 0:
        port = random.randint(10000, 60000)

    listen_addrs = [
        multiaddr.Multiaddr(f"/ip4/0.0.0.0/tcp/{port}"),
    ]

    # Generate or use provided key
    if seed:
        random.seed(seed)
        secret_number = random.getrandbits(32 * 8)
        secret = secret_number.to_bytes(length=32, byteorder="big")
    else:
        secret = secrets.token_bytes(32)

    key_pair = create_new_key_pair(secret)

    type = NetworkType.client
    if not destination:
        type = NetworkType.server

    try: 
        async with NetworkEcho(
            key_pair=key_pair, listen_addrs=listen_addrs, type=type
        ) as client:
            logger.info(f"Host ID: {str(client.host.get_id())}")
            if listen_addrs:
                for addr in listen_addrs:
                    logger.info(f"Listening: {str(addr)}")
            if not destination:  # Server mode
                
                logger.info(
                    "Run client from another console:\n"
                    f"python3 example_net_stream.py "
                    f"-d {",".join([str(addr)  for addr in client.host.get_addrs()])}\n"
                )
                logger.info("Waiting for connections...")
                logger.info("Press Ctrl+C to stop server")
                await trio.sleep_forever()

            else:  # Client mode
                
                # Connect to server
                maddr = multiaddr.Multiaddr(destination)
                logger.info(f"looking for peer: {str(maddr)}")
                info = info_from_p2p_addr(maddr)
                await client.connect(info)
                logger.info(f"Connected to server: {info.peer_id.pretty()}")

                # Create stream and run enhanced demo
                await client.echo(info.peer_id, message)
                

    except KeyboardInterrupt as e:
        raise e
    except Exception as e:
        logger.error(f"handle error: {e}")
        raise e

def main() -> None:
    example_maddr = (
        "/ip4/127.0.0.1/tcp/8000/p2p/QmQn4SwGkDZKkUEpBRBvTmheQycxAHJUNmVEnjA2v1qe8Q"
    )

    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-p", "--port", default=0, type=int, help="source port number")
    parser.add_argument(
        "-d",
        "--destination",
        type=str,
        help=f"destination multiaddr string, e.g. {example_maddr}",
    )
    parser.add_argument(
        "-s",
        "--seed",
        type=int,
        help="seed for deterministic peer ID generation",
    )
    parser.add_argument(
        "-m",
        "--message",
        type=str,
        default=None,
        help="send message to server node."
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="verbose mode."
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.INFO)
    
    if (message := args.message) is None:
        message = "echo from peer."
    try:
        trio.run(run_demo, args.port, args.destination, args.seed, message.encode() )
    except Exception as e:
        print(f"❌ Demo failed: {e}")


if __name__ == "__main__":
    main()
