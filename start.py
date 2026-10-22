"""
Start HomeStream so other devices on your network can reach it.

Usage examples:
    python start.py                 start for everyone on your network
    python start.py --local-only    start for this laptop only (use this to create the first account)
    python start.py --port 8080     use a different port
    python start.py --reload        restart automatically when you edit code (for development)
"""

import argparse
import ipaddress
import socket
import sys

import uvicorn

# Change this to the port number you like, so you don't have to type --port every time
DEFAULT_PORT = 8000

TAILSCALE_RANGE = ipaddress.ip_network("100.64.0.0/10")


def find_network_addresses() -> list[str]:
    """Find the addresses other devices can use to reach this computer."""
    found = []

    # "Connecting" a UDP socket sends nothing. It just makes Windows choose the network
    # card it would use to reach the outside, and tells us that card's address.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
    except OSError:
        pass  # no route out (for example a network with no internet): try the next method

    # Also ask for every address this computer's name resolves to
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass

    addresses = []
    for address in found:
        ip = ipaddress.ip_address(address)
        if ip.is_loopback or ip.is_link_local or address in addresses:
            continue  # 127.x is "this computer only" and 169.254.x means "no real network"
        addresses.append(address)
    return addresses


def port_is_free(host: str, port: int) -> bool:
    """Check that no other program is already listening on this port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test:
        try:
            test.bind((host, port))
        except OSError:
            return False
    return True


def main():
    parser = argparse.ArgumentParser(description="Start the HomeStream server.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port number (default {DEFAULT_PORT})")
    parser.add_argument("--local-only", action="store_true",
                        help="Only this laptop can connect (safe for creating the first account)")
    parser.add_argument("--reload", action="store_true", help="Restart when code changes (development)")
    args = parser.parse_args()

    # 127.0.0.1 means "only this computer". 0.0.0.0 means "any network card", so phones can connect.
    host = "127.0.0.1" if args.local_only else "0.0.0.0"

    if not port_is_free(host, args.port):
        sys.exit(
            f"Port {args.port} is already in use. Another HomeStream window is probably still open.\n"
            f"Close it, or pick another port with: python start.py --port {args.port + 1}"
        )

    print()
    print("HomeStream is starting.")
    print(f"  On this laptop:   http://localhost:{args.port}")

    if args.local_only:
        print("\n  Local-only mode: phones and other devices can't connect.")
        print("  Create your first account now, then restart without --local-only.")
    else:
        addresses = find_network_addresses()
        if addresses:
            print("  On your phone or TV (same WiFi):")
            for address in addresses:
                label = "   (Tailscale)" if ipaddress.ip_address(address) in TAILSCALE_RANGE else ""
                print(f"                    http://{address}:{args.port}{label}")
        else:
            print("  Couldn't find a network address. Are you connected to WiFi or a hotspot?")
        print(f"  Might also work: http://{socket.gethostname()}:{args.port}")

    print("\n  Press Ctrl+C to stop.\n", flush=True)

    uvicorn.run("main:app", host=host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
