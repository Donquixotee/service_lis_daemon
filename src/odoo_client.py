# -*- coding: utf-8 -*-
"""Odoo XML-RPC client for the LIS daemon.

Uses Python's stdlib xmlrpc.client, which passes interactive=False during
authenticate — required for Odoo 17 API key auth to work. The JSON-RPC
/web/session/authenticate endpoint forces interactive=True and rejects API keys.
"""

import logging
import time
import xmlrpc.client

logger = logging.getLogger(__name__)


class OdooClient:
    """XML-RPC client for Odoo 17+."""

    def __init__(self, url, db, login, api_key=None, password=None,
                 max_retries=3, retry_delay=2, timeout=30):
        self.url = url.rstrip("/")
        self.db = db
        self.login = login
        self.api_key = api_key
        self.password = password
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.timeout = timeout

        self._uid = None
        self._common = xmlrpc.client.ServerProxy("%s/xmlrpc/2/common" % self.url, allow_none=True)
        self._models = xmlrpc.client.ServerProxy("%s/xmlrpc/2/object" % self.url, allow_none=True)

    @property
    def auth_password(self):
        return self.api_key or self.password

    def authenticate(self):
        """Authenticate via XML-RPC (interactive=False — required for API key auth)."""
        logger.info(
            "Authenticating with Odoo at %s (db=%s, user=%s)",
            self.url, self.db, self.login,
        )
        try:
            uid = self._common.authenticate(self.db, self.login, self.auth_password, {"interactive": False})
        except Exception as e:
            raise OdooRPCError("XML-RPC authentication failed: %s" % e)

        if not uid:
            raise OdooRPCError(
                "Authentication failed — check login (%s) and API key/password" % self.login
            )
        self._uid = uid
        logger.info("Authenticated as UID=%s", self._uid)
        return self._uid

    def call(self, model, method, args=None, kwargs=None):
        """Call an Odoo ORM method with automatic retry."""
        if not self._uid:
            self.authenticate()

        args = args or []
        kwargs = kwargs or {}

        for attempt in range(1, self.max_retries + 1):
            try:
                result = self._models.execute_kw(
                    self.db,
                    self._uid,
                    self.auth_password,
                    model,
                    method,
                    args,
                    kwargs,
                )
                return result
            except xmlrpc.client.Fault as e:
                error_msg = e.faultString or ""
                if "Session expired" in error_msg or "Access Denied" in error_msg:
                    logger.warning("Auth error, re-authenticating: %s", error_msg)
                    self._uid = None
                    self.authenticate()
                    if attempt < self.max_retries:
                        continue
                raise OdooRPCError("XML-RPC fault: %s" % e.faultString)
            except (ConnectionError, TimeoutError, OSError) as e:
                logger.warning(
                    "Odoo RPC attempt %d/%d failed (transient): %s",
                    attempt, self.max_retries, e,
                )
                if attempt < self.max_retries:
                    time.sleep(self.retry_delay * attempt)
                else:
                    raise

    def load_machines(self):
        """Load all active dnd.lis.machine records from Odoo."""
        result = self.call(
            "dnd.lis.machine",
            "search_read",
            args=[[("active", "=", True)]],
            kwargs={"fields": ["id", "name", "machine_type", "ip_address", "port_receive"]},
        )
        logger.info("Loaded %d active machines from Odoo", len(result))
        return result

    def send_result(self, machine_id, sample_barcode, test_code, value,
                    unit=None, flags=None, ref_range=None, raw_message=None):
        """Call action_receive_result on acs.laboratory.request."""
        return self.call(
            "acs.laboratory.request",
            "action_receive_result",
            args=[],
            kwargs={
                "machine_id": machine_id,
                "sample_barcode": sample_barcode,
                "test_code": test_code,
                "value": value,
                "unit": unit,
                "flags": flags,
                "ref_range": ref_range,
                "raw_message": raw_message,
            },
        )

    def create_error_log(self, raw_message, error_message, machine_id=None, sample_barcode=None):
        """Create a dnd.lis.result.log record with status='error'. Never raises."""
        try:
            vals = {
                "status": "error",
                "raw_message": raw_message or "",
                "error_message": str(error_message),
            }
            if machine_id:
                vals["machine_id"] = machine_id
            if sample_barcode:
                vals["sample_barcode"] = sample_barcode
            log_id = self.call("dnd.lis.result.log", "create", args=[vals])
            logger.info("Error log created: id=%s", log_id)
        except Exception as e:
            logger.error("Failed to create error log in Odoo: %s", e)

    def set_machine_status(self, machine_id, status, last_seen=None):
        """Update machine status in Odoo."""
        vals = {"status": status}
        if last_seen:
            vals["last_seen"] = last_seen
        self.call(
            "dnd.lis.machine",
            "write",
            args=[[machine_id], vals],
        )


class OdooRPCError(Exception):
    """Error from Odoo RPC call."""

    def __init__(self, message, error_data=None):
        super().__init__(message)
        self.error_data = error_data or {}
