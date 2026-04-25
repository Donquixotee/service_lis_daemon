#!/usr/bin/env python3
"""
Mindray lab machine simulator — sends ASTM E1394 results over TCP.

Usage:
    python machine_simulator.py --host 192.168.1.4 --port 5001 \\
        --barcode LS001 \\
        --results "GLU:5.2:mmol/L:N" "CREA:88:umol/L:N" "WBC:7.5:G/L:N"

Result format: CODE:VALUE:UNIT:FLAG
  FLAG: N=normal  H=high  L=low  A=abnormal  HH=very high  LL=very low

Test scenarios:
    Normal:          --results "GLU:5.2:mmol/L:N"
    High value:      --results "GLU:12.0:mmol/L:H"
    Very high:       --results "GLU:25.0:mmol/L:HH"
    Multiple tests:  --results "GLU:5.2:mmol/L:N" "CREA:88:umol/L:N" "WBC:7.5:G/L:N"
    Unknown barcode: --barcode LS999 --results "GLU:5.2:mmol/L:N"
    Unknown code:    --results "XYZ:5.2:mmol/L:N"
    Partial only:    --results "GLU:5.2:mmol/L:N"   (if test has 3 critearea → partial)
"""

import argparse
import socket
import sys
import time

# ASTM control characters
ENQ = b"\x05"
ACK = b"\x06"
NAK = b"\x15"
STX = b"\x02"
ETX = b"\x03"
EOT = b"\x04"
CR  = b"\x0d"
LF  = b"\x0a"


def lrc(data: bytes) -> bytes:
    """Compute ASTM LRC checksum (XOR of all bytes mod 256, formatted as 2 hex chars)."""
    checksum = 0
    for b in data:
        checksum = (checksum + b) % 256
    return f"{checksum:02X}".encode("ascii")


def build_frame(seq: int, record: str) -> bytes:
    """
    Build one ASTM frame:
      STX + seq_digit + record + CR + ETX + checksum + CR + LF
    """
    seq_byte = str(seq % 8).encode("ascii")
    body = seq_byte + record.encode("ascii") + CR
    checksum = lrc(body)
    return STX + body + ETX + checksum + CR + LF


def build_transmission(barcode: str, results: list[tuple]) -> list[bytes]:
    """
    Build the full list of ASTM frames for one transmission.

    Args:
        barcode: sample barcode, e.g. 'LS001'
        results: list of (code, value, unit, flag) tuples

    Returns:
        list of bytes frames, preceded by ENQ and followed by EOT
    """
    test_codes = "\\".join(f"^^^{code}" for code, *_ in results)

    records = [
        f"H|\\^&|||BS-240Pro-Sim|||||||P|1",
        f"P|1||SIM001|PATIENT^SIMULATED",
        f"O|1|{barcode}|||{test_codes}|R",
    ]
    for i, (code, value, unit, flag) in enumerate(results, start=1):
        records.append(f"R|{i}|^^^{code}|{value}|{unit}||{flag}||F")
    records.append("L|1|N")

    return [build_frame(i + 1, rec) for i, rec in enumerate(records)]


def send_transmission(host: str, port: int, barcode: str, results: list[tuple],
                      timeout: int = 10, verbose: bool = True) -> bool:
    """
    Connect to the daemon and send one complete ASTM transmission.

    Returns True on success.
    """
    def log(msg):
        if verbose:
            print(msg)

    frames = build_transmission(barcode, results)

    log(f"\n{'='*60}")
    log(f"Connecting to {host}:{port} ...")

    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            log("Connected.")

            # --- Initiation ---
            log("→ ENQ")
            sock.sendall(ENQ)

            resp = sock.recv(1)
            if resp == ACK:
                log("← ACK  (ready to receive frames)")
            elif resp == NAK:
                log("← NAK  (machine busy — aborting)")
                sock.sendall(EOT)
                return False
            else:
                log(f"← unexpected byte: {resp!r}  (continuing anyway)")

            # --- Data transfer ---
            for i, frame in enumerate(frames):
                # Extract printable record content for display
                content = frame[2:-5].decode("ascii", errors="replace").strip()
                log(f"→ Frame {i+1}: {content}")
                sock.sendall(frame)
                time.sleep(0.05)

                try:
                    ack = sock.recv(1)
                    if ack == ACK:
                        log(f"  ← ACK")
                    elif ack == NAK:
                        log(f"  ← NAK (frame rejected)")
                    # Some machines don't ACK individual frames — continue either way
                except socket.timeout:
                    log(f"  (no ACK within timeout — continuing)")

            # --- Termination ---
            log("→ EOT")
            sock.sendall(EOT)
            log("Transmission complete.")
            return True

    except ConnectionRefusedError:
        print(f"ERROR: Connection refused — is the daemon running on {host}:{port}?", file=sys.stderr)
        return False
    except socket.timeout:
        print(f"ERROR: Timed out connecting to {host}:{port}", file=sys.stderr)
        return False
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return False


def parse_result_arg(s: str) -> tuple:
    """Parse 'CODE:VALUE:UNIT:FLAG' into a 4-tuple."""
    parts = s.split(":")
    if len(parts) != 4:
        raise argparse.ArgumentTypeError(
            f"Invalid result format '{s}'. Expected CODE:VALUE:UNIT:FLAG  e.g. GLU:5.2:mmol/L:N"
        )
    return tuple(parts)


def main():
    parser = argparse.ArgumentParser(
        description="Simulate a Mindray lab machine sending ASTM results to the LIS daemon.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host",    default="192.168.1.4", help="Daemon host IP (default: 192.168.1.4)")
    parser.add_argument("--port",    default=5001, type=int,  help="Daemon port (default: 5001)")
    parser.add_argument("--barcode", default="LS001",         help="Sample barcode — must exist in Odoo (default: LS001)")
    parser.add_argument("--results", nargs="+", type=parse_result_arg, required=True,
                        metavar="CODE:VALUE:UNIT:FLAG",
                        help="One or more results to send. Format: CODE:VALUE:UNIT:FLAG")
    parser.add_argument("--timeout", default=10, type=int,   help="TCP connection timeout in seconds (default: 10)")
    parser.add_argument("--repeat",  default=1,  type=int,   help="Send N times (for duplicate test, use --repeat 2)")
    parser.add_argument("--delay",   default=1.0, type=float, help="Seconds between repeats (default: 1.0)")
    parser.add_argument("--quiet",   action="store_true",    help="Suppress verbose output")

    args = parser.parse_args()

    print(f"Simulator: {len(args.results)} result(s) for barcode {args.barcode}")
    for code, value, unit, flag in args.results:
        flag_note = {"H": " ⚠ HIGH", "L": " ⚠ LOW", "A": " ⚠ ABNORMAL",
                     "HH": " 🔴 VERY HIGH", "LL": " 🔴 VERY LOW"}.get(flag.upper(), "")
        print(f"  {code} = {value} {unit}  [{flag}{flag_note}]")

    success_count = 0
    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"\n--- Run {i+1}/{args.repeat} ---")
        ok = send_transmission(
            host=args.host,
            port=args.port,
            barcode=args.barcode,
            results=args.results,
            timeout=args.timeout,
            verbose=not args.quiet,
        )
        if ok:
            success_count += 1
        if i < args.repeat - 1:
            time.sleep(args.delay)

    print(f"\nDone: {success_count}/{args.repeat} transmission(s) succeeded.")
    sys.exit(0 if success_count == args.repeat else 1)


if __name__ == "__main__":
    main()
