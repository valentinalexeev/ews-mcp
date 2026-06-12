"""Meta / introspection tools.

``whoami`` is the cheapest possible orchestrator first call: it confirms
identity and connectivity, reports the active Exchange backend / auth mode,
whether sending is enabled, and surfaces the EWS-on-Exchange-Online
retirement advisory (on-premises Exchange is unaffected). Read-only.
"""

from typing import Any, Dict, Optional

from .base import BaseTool
from ..utils import format_success_response


class WhoamiTool(BaseTool):
    """Identity, backend, capability and health self-report (read-only)."""

    # Local/state-only (or self-error-handling) tool: usable while the
    # background Exchange warmup is still connecting.
    requires_ews = False

    side_effect_class = "read"

    def __init__(self, ews_client, tools_registry: Optional[Dict[str, "BaseTool"]] = None):
        super().__init__(ews_client)
        # Shared reference to the live registry so ``tool_count`` reflects
        # every registered tool at call time (whoami is wired in after the
        # registration loop, like the approval executor).
        self._tools = tools_registry if tools_registry is not None else {}

    def get_schema(self) -> Dict[str, Any]:
        return {
            "name": "whoami",
            "description": (
                "Identity, backend, capabilities and health for this EWS-MCP "
                "server: primary mailbox, Exchange auth mode, whether sending "
                "is enabled, a live connection probe with the negotiated "
                "server version, and the EWS retirement advisory. Read-only — "
                "the recommended first call."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "target_mailbox": {
                        "type": "string",
                        "description": (
                            "Probe a delegated/impersonated mailbox instead of "
                            "the primary one (requires impersonation enabled)."
                        ),
                    },
                    "probe_connection": {
                        "type": "boolean",
                        "default": True,
                        "description": (
                            "When true (default) open/verify the Exchange "
                            "connection and report the negotiated server "
                            "version. Set false for a pure-config answer with "
                            "no network call."
                        ),
                    },
                },
                "required": [],
            },
        }

    async def execute(self, **kwargs) -> Dict[str, Any]:
        cfg = self.ews_client.config
        target_mailbox = kwargs.get("target_mailbox")
        probe = bool(kwargs.get("probe_connection", True))

        server = (cfg.ews_server_url or "").strip()
        server_l = server.lower()
        is_exchange_online = "office365.com" in server_l or "outlook.com" in server_l

        connection: Dict[str, Any] = {"probed": probe}
        manager = getattr(self.ews_client, "connection_manager", None)
        if manager is not None:
            # Background warmup/heartbeat state — no network call needed.
            connection["managed"] = manager.status()
        if probe:
            try:
                account = self.get_account(target_mailbox)
                version = getattr(account, "version", None)
                connection["ok"] = True
                connection["server_version"] = (
                    getattr(version, "fullname", None) if version else None
                )
                connection["api_version"] = (
                    getattr(version, "api_version", None) if version else None
                )
            except Exception as e:  # whoami must never hard-fail
                connection["ok"] = False
                connection["error"] = f"{type(e).__name__}: {e}"

        if is_exchange_online:
            ews_sunset = {
                "applies": True,
                "backend": "exchange_online",
                "allowlist_required_after": "2026-10-01",
                "removed_after": "2027-04-01",
                "note": (
                    "EWS on Exchange Online is being retired. Ensure "
                    "EWSEnabled=True and this app is on the tenant EWS Allow "
                    "List before 2026-10-01; plan a Microsoft Graph migration "
                    "before the 2027-04-01 hard removal."
                ),
            }
        else:
            ews_sunset = {
                "applies": False,
                "backend": "on_premises",
                "note": (
                    "On-premises Exchange Server is not affected by the "
                    "Exchange Online EWS retirement; EWS remains supported."
                ),
            }

        return format_success_response(
            f"EWS-MCP {cfg.api_version} — {cfg.ews_email}",
            mailbox=self.get_mailbox_info(target_mailbox),
            primary_mailbox=cfg.ews_email,
            backend="ews",
            server=server or None,
            autodiscover=cfg.ews_autodiscover,
            auth_type=cfg.ews_auth_type,
            impersonation={
                "enabled": cfg.ews_impersonation_enabled,
                "type": cfg.ews_impersonation_type,
            },
            transport=cfg.mcp_transport,
            send_enabled=getattr(cfg, "send_enabled", True),
            timezone=cfg.timezone,
            server_name=cfg.mcp_server_name,
            version=cfg.api_version,
            tool_count=(len(self._tools) or None),
            connection=connection,
            ews_sunset=ews_sunset,
        )
