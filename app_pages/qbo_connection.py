"""QuickBooks Online connection block for the Finance › Cashflow page.

Moved verbatim from app.py (v2.67.211 page body) so both the current
Viktor-backed page and the legacy in-app forecast (CASHFLOW_LEGACY_PAGE=1)
share it. Returns (qbo_oauth, qbo_client, connection_info) for callers that
need to know whether QBO is connected.
"""
from __future__ import annotations

import streamlit as st

import db


def render_qbo_connection():
    """Render the ':satellite: QuickBooks Online connection' section."""
    _qbo_info = None
    current_user_profile = st.session_state.get("current_user_profile") or {}
    try:
        import qbo_oauth as _qbo_oauth
        import qbo_client as _qbo_client
    except Exception as _exc:  # noqa: BLE001
        _qbo_oauth = None
        _qbo_client = None
        st.error(f"QBO modules unavailable: {_exc}")

    if _qbo_oauth is not None:
        st.subheader("📡 QuickBooks Online connection")

        _cf_role = (current_user_profile or {}).get("role") or "sales"
        _cf_name = ((current_user_profile or {}).get("display_name")
                       or st.session_state.get("current_user", ""))
        _cf_is_super = db.is_super_admin(_cf_name, _cf_role)

        try:
            _qbo_info = _qbo_oauth.connection_info()
        except Exception as _exc:  # noqa: BLE001
            _qbo_info = None
            st.warning(f"Could not read QBO connection state: {_exc}")

        if _qbo_info:
            _env = _qbo_info.get("environment") or "sandbox"
            st.success(
                f":white_check_mark: Connected to QuickBooks Online "
                f"(**{_env}**) — company realm `"
                f"{_qbo_info.get('realm_id')}`.")
            _days_left = _qbo_info.get("refresh_days_left")
            _meta_bits = []
            if _qbo_info.get("connected_by"):
                _meta_bits.append(
                    f"connected by {_qbo_info['connected_by']}")
            if _qbo_info.get("connected_at"):
                _meta_bits.append(
                    f"on {str(_qbo_info['connected_at'])[:16]}")
            if _days_left is not None:
                _meta_bits.append(
                    f"re-auth needed in ~{_days_left} days")
            if _meta_bits:
                st.caption(" · ".join(_meta_bits))
            if _days_left is not None and _days_left <= 14:
                st.warning(
                    ":warning: The QBO refresh token expires in "
                    f"~{_days_left} days. Click Disconnect then "
                    "reconnect before then to avoid an outage.")

            # Live connection check — confirms the token actually
            # works, not just that a row exists.
            if st.button(":arrows_counterclockwise: Test connection",
                          key="_qbo_test"):
                try:
                    _ci = _qbo_client.company_info()
                    _cn = (_ci.get("CompanyName")
                            or _ci.get("LegalName") or "(unknown)")
                    st.success(
                        f":white_check_mark: QBO API reachable — "
                        f"company: **{_cn}**")
                except Exception as _exc:  # noqa: BLE001
                    st.error(f":x: QBO API call failed: {_exc}")

            if _cf_is_super:
                if st.button(":electric_plug: Disconnect QuickBooks",
                              key="_qbo_disconnect",
                              help="Revoke the stored tokens. The "
                                    "Cashflow page goes dark until "
                                    "reconnected."):
                    try:
                        _qbo_oauth.disconnect()
                        st.success("QuickBooks Online disconnected.")
                        st.rerun()
                    except Exception as _exc:  # noqa: BLE001
                        st.error(f"Disconnect failed: {_exc}")
            else:
                st.caption(
                    "Only a super-admin can disconnect QuickBooks.")
        else:
            # Not connected — show the connect button (super-admin
            # only; this is a company-wide financial connection).
            if not _qbo_oauth.is_configured():
                st.error(
                    ":x: QuickBooks OAuth is not configured. An "
                    "admin must set these env vars in the "
                    "`cin7-shared` group: `QBO_CLIENT_ID`, "
                    "`QBO_CLIENT_SECRET`, `QBO_REDIRECT_URI`, "
                    "`QBO_ENVIRONMENT` (sandbox|production), and "
                    "`QBO_TOKEN_ENCRYPTION_KEY` (or reuse "
                    "`SLACK_USER_TOKEN_ENCRYPTION_KEY`).")
            elif not _cf_is_super:
                st.info(
                    "QuickBooks Online is not connected yet. Ask a "
                    "super-admin to connect it from this page.")
            else:
                try:
                    import secrets as _secrets
                    _qbo_state = _secrets.token_urlsafe(24)
                    st.session_state["_qbo_oauth_state"] = _qbo_state
                    _qbo_auth_url = _qbo_oauth.build_authorize_url(
                        _qbo_state)
                    st.markdown(
                        f"[🔗 **Connect QuickBooks Online** "
                        f"(opens Intuit to authorise)]"
                        f"({_qbo_auth_url})")
                    st.caption(
                        f"Environment: **{_qbo_oauth.environment()}**. "
                        "The Intuit app's Redirect URI must match "
                        "`QBO_REDIRECT_URI`. Tokens are encrypted "
                        "at rest.")
                except Exception as _exc:  # noqa: BLE001
                    st.error(f"Connect button unavailable: {_exc}")
    return _qbo_oauth, _qbo_client, _qbo_info
