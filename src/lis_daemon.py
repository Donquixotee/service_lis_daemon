# -*- coding: utf-8 -*-
"""LIS Daemon — Async TCP server for receiving ASTM results.

Listens on one port per configured machine. When a Mindray machine
connects and sends an ASTM message:
1. Receive full frame (ENQ → STX…ETX → EOT)
2. ACK each frame
3. Parse with ASTMParser
4. Call Odoo action_receive_result() via OdooClient for each R record
"""

import asyncio
import logging
from datetime import datetime, timezone

from .astm_parser import ASTMParser, ENQ, STX, ETX, ETB, EOT, ACK, NAK, CR, LF

logger = logging.getLogger(__name__)


class LisDaemon:
    """Async TCP server that receives ASTM results from lab machines."""

    def __init__(self, odoo_client, parser, config):
        """
        Args:
            odoo_client: OdooClient instance
            parser: ASTMParser instance
            config: dict from daemon.yml 'daemon' section
        """
        self.odoo_client = odoo_client
        self.parser = parser
        self.config = config
        self.machines = {}  # port → machine dict
        self.servers = []   # asyncio.Server instances
        self._running = False

    async def start(self):
        """Load machines from Odoo and start TCP listeners."""
        logger.info("Starting LIS daemon...")

        # Authenticate with Odoo
        self.odoo_client.authenticate()

        # Load machine config
        await self._load_machines()

        if not self.machines:
            logger.warning("No active machines found in Odoo — daemon has nothing to listen on")
            return

        # Start one TCP server per machine port
        for port, machine in self.machines.items():
            server = await asyncio.start_server(
                lambda r, w, m=machine: self._handle_connection(r, w, m),
                "0.0.0.0",
                port,
            )
            self.servers.append(server)
            logger.info(
                "Listening on port %d for machine '%s' (%s)",
                port, machine["name"], machine["machine_type"],
            )

        self._running = True
        logger.info("LIS daemon started — listening on %d ports", len(self.servers))

        # Start background task to periodically refresh machine config
        asyncio.create_task(self._refresh_loop())

    async def _load_machines(self):
        """Load active machines from Odoo."""
        try:
            machine_list = self.odoo_client.load_machines()
            self.machines = {}
            for m in machine_list:
                port = m.get("port_receive")
                if port:
                    self.machines[port] = m
                    logger.info(
                        "  Machine: %s (id=%s, type=%s, port=%d)",
                        m["name"], m["id"], m["machine_type"], port,
                    )
        except Exception as e:
            logger.error("Failed to load machines from Odoo: %s", e)

    async def _refresh_loop(self):
        """Periodically reload machine config from Odoo."""
        interval = self.config.get("machine_refresh_interval", 300)
        while self._running:
            await asyncio.sleep(interval)
            try:
                await self._load_machines()
            except Exception as e:
                logger.error("Machine refresh failed: %s", e)

    async def _handle_connection(self, reader, writer, machine):
        """Handle a single TCP connection from a machine.

        Implements ASTM E1394 receive protocol:
        - Wait for ENQ → send ACK
        - Receive STX frames → send ACK for each
        - On EOT → parse complete message → process results
        """
        addr = writer.get_extra_info("peername")
        machine_id = machine["id"]
        machine_name = machine["name"]
        logger.info("Connection from %s for machine '%s'", addr, machine_name)

        # Update machine status to online
        try:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            self.odoo_client.set_machine_status(machine_id, "online", last_seen=now)
        except Exception as e:
            logger.warning("Failed to update machine status: %s", e)

        buffer = bytearray()
        timeout = self.config.get("tcp", {}).get("receive_timeout", 30)
        buf_size = self.config.get("tcp", {}).get("buffer_size", 4096)

        try:
            while True:
                try:
                    data = await asyncio.wait_for(reader.read(buf_size), timeout=timeout)
                except asyncio.TimeoutError:
                    logger.warning("Timeout receiving from %s — closing connection", addr)
                    break

                if not data:
                    logger.info("Connection closed by %s", addr)
                    break

                # Process byte by byte for ASTM protocol
                for byte in data:
                    b = bytes([byte])

                    if b == ENQ:
                        # Machine wants to send — ACK it
                        writer.write(ACK)
                        await writer.drain()
                        logger.debug("ENQ received from %s — sent ACK", addr)
                        buffer = bytearray()  # reset buffer

                    elif b == EOT:
                        # End of transmission — process the buffer
                        logger.debug("EOT received from %s — processing %d bytes", addr, len(buffer))
                        if buffer:
                            await self._process_message(bytes(buffer), machine)
                        buffer = bytearray()

                    elif b == STX:
                        # Start of frame — begin collecting
                        buffer.append(byte)

                    elif b in (ETX, ETB):
                        # End of frame — ACK it
                        buffer.append(byte)
                        # Read checksum (2 bytes) + CR + LF
                        try:
                            trailing = await asyncio.wait_for(reader.read(4), timeout=5)
                            buffer.extend(trailing)
                        except asyncio.TimeoutError:
                            pass
                        writer.write(ACK)
                        await writer.drain()
                        logger.debug("Frame received (%d bytes) — sent ACK", len(buffer))

                    else:
                        buffer.append(byte)

        except Exception as e:
            logger.error("Error handling connection from %s: %s", addr, e, exc_info=True)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            logger.info("Connection closed: %s", addr)
            # Mark machine offline on disconnect
            try:
                self.odoo_client.set_machine_status(machine_id, "offline")
            except Exception as e:
                logger.warning("Failed to update machine offline status: %s", e)


    async def _process_message(self, raw_bytes, machine):
        """Parse ASTM message and send results to Odoo.

        Args:
            raw_bytes: bytes - complete ASTM transmission
            machine: dict - machine record from Odoo
        """
        machine_id = machine["id"]
        machine_name = machine["name"]

        try:
            parsed = self.parser.parse_message(raw_bytes)
        except Exception as e:
            logger.error("ASTM parse error for machine '%s': %s", machine_name, e, exc_info=True)
            # Still try to log the raw message
            try:
                self.odoo_client.send_result(
                    machine_id=machine_id,
                    sample_barcode="",
                    test_code="",
                    value="",
                    raw_message=raw_bytes.decode("ascii", errors="replace"),
                )
            except Exception:
                pass
            return

        # Get sample barcode from first O record
        sample_barcode = ""
        if parsed["orders"]:
            sample_barcode = parsed["orders"][0].get("sample_id", "")

        raw_text = parsed.get("raw", raw_bytes.decode("ascii", errors="replace"))

        # Process each R (Result) record
        if not parsed["results"]:
            logger.warning(
                "No R records found in message from machine '%s' (sample=%s)",
                machine_name, sample_barcode,
            )
            return

        for result in parsed["results"]:
            test_code = result.get("test_code", "")
            value = result.get("value", "")
            unit = result.get("unit", "")
            flags = result.get("flags", "")
            ref_range = result.get("ref_range", "")

            logger.info(
                "Result: %s/%s = %s %s [%s] (machine=%s)",
                sample_barcode, test_code, value, unit, flags, machine_name,
            )

            try:
                response = self.odoo_client.send_result(
                    machine_id=machine_id,
                    sample_barcode=sample_barcode,
                    test_code=test_code,
                    value=value,
                    unit=unit or None,
                    flags=flags or None,
                    ref_range=ref_range or None,
                    raw_message=raw_text,
                )
                logger.info(
                    "Odoo response for %s/%s: status=%s, log_id=%s",
                    sample_barcode,
                    test_code,
                    response.get("status"),
                    response.get("log_id"),
                )
            except Exception as e:
                logger.error(
                    "Failed to send result to Odoo (%s/%s): %s",
                    sample_barcode, test_code, e,
                    exc_info=True,
                )

    async def stop(self):
        """Stop all TCP servers."""
        self._running = False
        for server in self.servers:
            server.close()
            await server.wait_closed()
        logger.info("LIS daemon stopped")
