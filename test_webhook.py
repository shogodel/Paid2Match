#!/usr/bin/env python3
"""Test script for Stripe webhook."""
import json
import requests
import sys

WEBHOOK_URL = "https://paid2match.work/bounties/stripe-webhook"

TEST_BOUNTY_ID = "40d54945-084c-499b-9782-443191b018ae"

def run_webhook_dev_mode():
    """Test webhook in development mode (no signature required)."""
    payload = {
        "id": "evt_test_123",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_session_123",
                "payment_intent": "pi_test_123",
                "metadata": {
                    "bounty_id": TEST_BOUNTY_ID,
                    "bounty_amount": "1000",
                    "transaction_fee": "30"
                }
            }
        }
    }
    
    print(f"Testing webhook at {WEBHOOK_URL}")
    print(f"Payload: {json.dumps(payload, indent=2)}")
    
    try:
        response = requests.post(
            WEBHOOK_URL,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        print(f"\nResponse status: {response.status_code}")
        print(f"Response body: {response.text}")
        return response.status_code == 200
    except Exception as e:
        print(f"Error: {e}")
        return False


def run_webhook_with_signature():
    """Test webhook with proper Stripe signature (production mode)."""
    import hmac
    import hashlib
    import time
    
    payload = json.dumps({
        "id": "evt_test_456",
        "type": "checkout.session.completed",
        "data": {
            "object": {
                "id": "cs_test_session_456",
                "payment_intent": "pi_test_456",
                "metadata": {
                    "bounty_id": TEST_BOUNTY_ID,
                    "bounty_amount": "5000",
                    "transaction_fee": "150"
                }
            }
        }
    })
    
    timestamp = int(time.time())
    webhook_secret = "whsec_test_secret"
    
    signed_payload = f"{timestamp}.{payload}"
    signature = hmac.new(
        webhook_secret.encode(),
        signed_payload.encode(),
        hashlib.sha256
    ).hexdigest()
    
    header = f"t={timestamp},{signature}"
    
    print(f"Testing webhook with signature")
    print(f"Header: {header[:50]}...")
    
    try:
        response = requests.post(
            WEBHOOK_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Stripe-Signature": header
            },
            timeout=10
        )
        print(f"\nResponse status: {response.status_code}")
        print(f"Response body: {response.text}")
        return response.status_code == 200
    except Exception as e:
        print(f"Error: {e}")
        return False


if __name__ == "__main__":
    print("=" * 50)
    print("TEST 1: Dev mode (no signature)")
    print("=" * 50)
    run_webhook_dev_mode()
    
    print("\n" + "=" * 50)
    print("TEST 2: With signature")
    print("=" * 50)
    run_webhook_with_signature()