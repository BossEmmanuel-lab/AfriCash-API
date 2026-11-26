import json
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from django.conf import settings


def get_explorer_tx_url(tx_hash: Optional[str]) -> Optional[str]:
    if not tx_hash:
        return None
    base = getattr(
        settings,
        'STELLAR_EXPLORER_BASE_URL',
        'https://stellar.expert/explorer/testnet/tx',
    ).rstrip('/')
    return f"{base}/{tx_hash}"


def build_exchange_explorer_links(exchange) -> Dict[str, Optional[str]]:
    return {
        "lock": get_explorer_tx_url(exchange.lock_tx_hash),
        "release": get_explorer_tx_url(exchange.release_tx_hash),
        "refund": get_explorer_tx_url(exchange.refund_tx_hash),
        "dispute": get_explorer_tx_url(exchange.dispute_tx_hash),
        "resolution": get_explorer_tx_url(exchange.resolution_tx_hash),
    }


def verify_horizon_transaction(tx_hash: str, timeout: float = 5.0) -> Dict[str, Any]:
    """Query Stellar Horizon to verify a transaction hash on Stellar Testnet."""
    explorer_url = get_explorer_tx_url(tx_hash)
    horizon_url = getattr(
        settings, 'STELLAR_HORIZON_URL', 'https://horizon-testnet.stellar.org'
    ).rstrip('/')
    url = f"{horizon_url}/transactions/{tx_hash}"

    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "AfriCash-Backend/2.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode('utf-8'))
            return {
                "tx_hash": tx_hash,
                "verified": True,
                "successful": bool(payload.get("successful", False)),
                "ledger": payload.get("ledger"),
                "created_at": payload.get("created_at"),
                "source_account": payload.get("source_account"),
                "explorer_url": explorer_url,
                "error": None,
            }
    except urllib.error.HTTPError as exc:
        error_msg = "Transaction not found on Stellar Horizon." if exc.code == 404 else f"Horizon HTTP {exc.code}"
        return {
            "tx_hash": tx_hash,
            "verified": False,
            "successful": False,
            "ledger": None,
            "created_at": None,
            "source_account": None,
            "explorer_url": explorer_url,
            "error": error_msg,
        }
    except Exception as exc:
        return {
            "tx_hash": tx_hash,
            "verified": False,
            "successful": False,
            "ledger": None,
            "created_at": None,
            "source_account": None,
            "explorer_url": explorer_url,
            "error": str(exc),
        }


def verify_exchange_transactions(exchange) -> Dict[str, Any]:
    """Verify all recorded Stellar Testnet transaction hashes for an exchange."""
    tx_map = {
        "lock": exchange.lock_tx_hash,
        "release": exchange.release_tx_hash,
        "refund": exchange.refund_tx_hash,
        "dispute": exchange.dispute_tx_hash,
        "resolution": exchange.resolution_tx_hash,
    }

    results: Dict[str, Any] = {}
    recorded_count = 0
    verified_count = 0

    for label, tx_hash in tx_map.items():
        if not tx_hash:
            results[label] = None
            continue
        recorded_count += 1
        res = verify_horizon_transaction(tx_hash)
        if res["verified"] and res["successful"]:
            verified_count += 1
        results[label] = res

    return {
        "exchange_id": exchange.exchange_id,
        "status": exchange.status,
        "transaction_type": exchange.transaction_type,
        "escrow_contract_id": exchange.escrow_contract_id,
        "recorded_transactions": recorded_count,
        "verified_transactions": verified_count,
        "all_verified": recorded_count > 0 and recorded_count == verified_count,
        "transactions": results,
        "explorer_links": build_exchange_explorer_links(exchange),
    }
