# -*- coding: utf-8 -*-
"""LIS Daemon — Entry Point.

Loads configuration from environment variables and daemon.yml,
initializes the Odoo client and ASTM parser, then starts the
async TCP server.
"""

import asyncio
import logging
import os
import signal
import sys

import yaml

from .odoo_client import OdooClient
from .hl7_parser import HL7Parser
from .lis_daemon import LisDaemon

logger = logging.getLogger("lis_daemon")


def load_config(config_path):
    """Load daemon configuration from YAML file."""
    if not os.path.exists(config_path):
        logger.error("Config file not found: %s", config_path)
        sys.exit(1)

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    logger.info("Loaded config from %s", config_path)
    return config


def setup_logging(log_level):
    """Configure structured logging."""
    level = getattr(logging, log_level.upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Reduce noise from requests library
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main():
    """Main entry point."""
    # --- Load environment ---
    odoo_url = os.environ.get("ODOO_URL", "http://localhost:8069")
    odoo_db = os.environ.get("ODOO_DB", "")
    odoo_login = os.environ.get("ODOO_USER_LOGIN", "")
    odoo_api_key = os.environ.get("ODOO_API_KEY", "")
    odoo_password = os.environ.get("ODOO_PASSWORD", "")
    config_path = os.environ.get("LIS_CONFIG_PATH", "config/daemon.yml")
    log_level = os.environ.get("LIS_LOG_LEVEL", "INFO")

    # --- Setup logging ---
    setup_logging(log_level)

    # --- Validate required env vars ---
    if not odoo_db:
        logger.error("ODOO_DB not set")
        sys.exit(1)
    if not odoo_login:
        logger.error("ODOO_USER_LOGIN not set")
        sys.exit(1)
    if not odoo_api_key and not odoo_password:
        logger.error("Either ODOO_API_KEY or ODOO_PASSWORD must be set")
        sys.exit(1)

    # --- Load config ---
    config = load_config(config_path)
    daemon_config = config.get("daemon", {})
    hl7_config = config.get("hl7", {})

    # --- Initialize components ---
    rpc_config = daemon_config.get("odoo_rpc", {})
    odoo_client = OdooClient(
        url=odoo_url,
        db=odoo_db,
        login=odoo_login,
        api_key=odoo_api_key or None,
        password=odoo_password or None,
        max_retries=rpc_config.get("max_retries", 3),
        retry_delay=rpc_config.get("retry_delay", 2),
        timeout=rpc_config.get("timeout", 30),
    )

    parser = HL7Parser(hl7_config)
    daemon = LisDaemon(odoo_client, parser, daemon_config)

    # --- Run async event loop ---
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Graceful shutdown on SIGTERM/SIGINT
    def shutdown(sig):
        logger.info("Received signal %s — shutting down", sig.name)
        loop.create_task(daemon.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, shutdown, sig)

    try:
        loop.run_until_complete(daemon.start())
        if daemon.servers:
            logger.info("LIS daemon running. Press Ctrl+C to stop.")
            loop.run_forever()
        else:
            logger.error("No servers started — exiting")
            sys.exit(1)
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt — shutting down")
    finally:
        loop.run_until_complete(daemon.stop())
        loop.close()


if __name__ == "__main__":
    main()
