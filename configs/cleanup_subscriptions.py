"""
Cleanup utility for the Topics/Subscriptions API.

Purpose: NOT a load test. This is a one-shot teardown script that, for every
credential pair in the CSV file, authenticates, fetches that account's
current subscriptions, and unsubscribes ALL of them concurrently.

Use this between load test runs (or on demand) to clear all accounts back
to a clean/empty state, so a fresh Locust run doesn't start with leftover
subscriptions from a previous run.

Concurrency is done with a thread pool (accounts in parallel, and deletes
within each account in parallel), so this uses the same `requests` +
requests-pkcs12 client-cert setup as the load test.

--- EDIT THESE BEFORE RUNNING ---
"""

import csv
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import requests
from dotenv import load_dotenv
from requests_pkcs12 import Pkcs12Adapter

# ---- CONFIG: edit to match your real API ----
BASE_URL = "REPLACE_WITH_BASE_URL"      # e.g. "https://api.example.com" (no trailing slash)
AUTH_URL = "REPLACE_WITH_AUTH_URI"      # same token endpoint used in locustfile.py

CREDENTIALS_FILE = "credentials.csv"    # CSV with header: client_id,client_secret
AUTH_SCOPE = "123"                      # scope is shared across all credential pairs
AUTH_GRANT_TYPE = "client_credentials"

# Client certificate (mutual TLS) -- same setup as the load test. The API now
# requires a .p12 client cert, mounted on the session in main() (see below).
CLIENT_CERT_FILE = "cert.p12"  # path to your .p12 bundle
load_dotenv()
# Same env var the load test uses, so you only set the passphrase once --
# PowerShell:  $env:CLIENT_CERT_PASSWORD = "your-pass"
CLIENT_CERT_PASSWORD = os.environ.get("CLIENT_CERT_PASSWORD")

SUBSCRIPTIONS_ENDPOINT = "/subscriptions"
DELETE_PATH = "/subscriptions/{id}/unsubscribe"

# How many accounts to process at the same time. Keep this reasonable --
# this script is meant to clean up state, not itself act as a load test.
MAX_CONCURRENT_ACCOUNTS = 10

# How many DELETE calls to fire at once *within* a single account.
MAX_CONCURRENT_DELETES_PER_ACCOUNT = 5

REQUEST_TIMEOUT = 30  # seconds, per request


def load_credentials(path):
    """Load (client_id, client_secret) pairs from a CSV file with a header
    row: client_id,client_secret"""
    creds = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            creds.append((row["client_id"], row["client_secret"]))
    if not creds:
        raise ValueError(f"No credentials found in {path}")
    return creds


def authenticate(session, client_id, client_secret):
    """Client-credentials grant, form-data, returns a bearer token or None."""
    payload = {
        "grant_type": AUTH_GRANT_TYPE,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": AUTH_SCOPE,
    }
    resp = session.get(AUTH_URL, data=payload, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        print(f"  [auth] {client_id}: unexpected status {resp.status_code} "
              f"({resp.request.method} {AUTH_URL}) -- body: {resp.text[:500]}")
        return None
    try:
        data = resp.json()
    except ValueError:
        print(f"  [auth] {client_id}: invalid JSON response")
        return None
    token = data.get("access_token")
    if not token:
        print(f"  [auth] {client_id}: no access_token in response")
        return None
    return token


def fetch_subscription_ids(session, headers, client_id):
 
    """GET /subscriptions for this account. Returns (sub_ids, get_ok) --
    get_ok is False on any failure so callers can distinguish "genuinely
    has zero subscriptions" from "couldn't check, don't trust that '0'"."""
    url = BASE_URL + SUBSCRIPTIONS_ENDPOINT
    resp = session.get(url, headers=headers, timeout=REQUEST_TIMEOUT)
    if resp.status_code != 200:
        print(f"  [get] {client_id}: unexpected status {resp.status_code} "
              f"(GET {url}) -- body: {resp.text[:500]}")
        return [], False
    try:
        data = resp.json()
    except ValueError:
        print(f"  [get] {client_id}: invalid JSON response")
        return [], False
    items = data if isinstance(data, list) else data.get("subscriptions", [])
    sub_ids = [item.get("id") for item in items if item.get("id") is not None]
    return sub_ids, True


def unsubscribe_one(session, headers, client_id, sub_id):
    """Delete one subscription. Returns True on success, False on failure."""
    url = BASE_URL + DELETE_PATH.format(id=sub_id)
    try:
        resp = session.delete(url, headers=headers, timeout=REQUEST_TIMEOUT)
        if resp.status_code in (200, 204):
            return True
        print(f"  [delete] {client_id}: id={sub_id} unexpected status {resp.status_code} "
              f"(DELETE {url}) -- body: {resp.text[:500]}")
        return False
    except requests.RequestException as exc:
        print(f"  [delete] {client_id}: id={sub_id} request error: {exc}")
        return False


def clear_account(session, client_id, client_secret):
    """Run one cleanup pass for a single account. Returns a result dict:
    {"auth_failed": bool, "get_failed": bool, "found": int, "deleted": int, "delete_failed": int}
    """
    token = authenticate(session, client_id, client_secret)
    if not token:
        return {"auth_failed": True, "get_failed": False, "found": 0, "deleted": 0, "delete_failed": 0}

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    sub_ids, get_ok = fetch_subscription_ids(session, headers, client_id)
    if not get_ok:
        # Couldn't even determine what this account has -- do NOT treat this
        # as "nothing to unsubscribe". The outer loop needs to know this
        # account still needs a retry pass.
        return {"auth_failed": False, "get_failed": True, "found": 0, "deleted": 0, "delete_failed": 0}

    if not sub_ids:
        print(f"  [ok] {client_id}: nothing to unsubscribe")
        return {"auth_failed": False, "get_failed": False, "found": 0, "deleted": 0, "delete_failed": 0}

    print(f"  [ok] {client_id}: unsubscribing {len(sub_ids)} subscription(s)")
    # Fire this account's deletes in parallel, capped by the pool size.
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_DELETES_PER_ACCOUNT) as pool:
        outcomes = list(pool.map(
            lambda sid: unsubscribe_one(session, headers, client_id, sid),
            sub_ids,
        ))
    deleted = sum(1 for ok in outcomes if ok)
    delete_failed = len(outcomes) - deleted
    return {
        "auth_failed": False, "get_failed": False,
        "found": len(sub_ids), "deleted": deleted, "delete_failed": delete_failed,
    }


# Safety cap on passes, in case something keeps genuinely failing forever
# (e.g. a persistent 500 on GET, or deletes that always fail) -- without
# this, a permanently broken account would loop the script forever.
MAX_PASSES = 10

# Brief pause between passes so a fast retry doesn't immediately re-hit
# whatever just failed (rate limiting, transient errors, etc.)
PASS_DELAY_SECONDS = 3


def run_pass(session, credentials, pass_num):
    print(f"\n--- Pass {pass_num} ---")
    # Process accounts in parallel, capped by the pool size.
    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_ACCOUNTS) as pool:
        account_results = list(pool.map(
            lambda cred: clear_account(session, cred[0], cred[1]),
            credentials,
        ))

    totals = {"auth_failed": 0, "get_failed": 0, "found": 0, "deleted": 0, "delete_failed": 0}
    for r in account_results:
        for key in totals:
            totals[key] += r[key]

    print(f"Pass {pass_num} summary: found={totals['found']} deleted={totals['deleted']} "
          f"delete_failed={totals['delete_failed']} get_failed={totals['get_failed']} "
          f"auth_failed={totals['auth_failed']}")
    return totals


def make_session():
    """A requests session that presents our .p12 client cert on API calls.

    Same logic as locustfile2.py's _mount_client_cert: refuse to run without
    a passphrase, then mount the Pkcs12Adapter so every request under BASE_URL
    does the mutual-TLS handshake with our client certificate. (The auth call
    goes to AUTH_URL, a different host, so it uses the default adapter -- just
    like the load test mounts the cert only on the API host.)"""
    if not CLIENT_CERT_PASSWORD:
        raise ValueError(
            "CLIENT_CERT_PASSWORD is not set -- export the .p12 passphrase "
            "before running (see CONFIG at the top of this file)."
        )
    session = requests.Session()
    session.mount(
        BASE_URL,
        Pkcs12Adapter(
            pkcs12_filename=CLIENT_CERT_FILE,
            pkcs12_password=CLIENT_CERT_PASSWORD,
            # Size the connection pool for the concurrency we drive below,
            # so parallel deletes don't fight over too few connections.
            pool_connections=MAX_CONCURRENT_ACCOUNTS,
            pool_maxsize=MAX_CONCURRENT_ACCOUNTS * MAX_CONCURRENT_DELETES_PER_ACCOUNT,
        ),
    )
    return session


def main():
    credentials = load_credentials(CREDENTIALS_FILE)
    print(f"Loaded {len(credentials)} credential pair(s) from {CREDENTIALS_FILE}")

    # One session, client cert mounted once, reused for every request/pass.
    session = make_session()

    grand_totals = {"auth_failed": 0, "get_failed": 0, "found": 0, "deleted": 0, "delete_failed": 0}
    pass_num = 0
    clean_pass_achieved = False

    with session:
        while pass_num < MAX_PASSES:
            pass_num += 1
            totals = run_pass(session, credentials, pass_num)
            for key in grand_totals:
                grand_totals[key] += totals[key]

            # Stop only once a full pass found nothing left to do AND every
            # account's GET actually succeeded -- a get_failed account might
            # still be hiding subscriptions we never got to check.
            if totals["found"] == 0 and totals["get_failed"] == 0:
                clean_pass_achieved = True
                break

            if pass_num < MAX_PASSES:
                time.sleep(PASS_DELAY_SECONDS)

    print("\n--- Final Summary ---")
    print(f"Passes run:                {pass_num}")
    print(f"Clean pass achieved:       {clean_pass_achieved}")
    print(f"Accounts per pass:         {len(credentials)}")
    print(f"Total auth failures:       {grand_totals['auth_failed']}")
    print(f"Total subscriptions found: {grand_totals['found']}")
    print(f"Total deleted:             {grand_totals['deleted']}")
    print(f"Total delete failures:     {grand_totals['delete_failed']}")
    print(f"Total get failures:        {grand_totals['get_failed']}")

    if not clean_pass_achieved:
        print(f"\nWARNING: hit MAX_PASSES ({MAX_PASSES}) without a fully clean pass. "
              f"Some accounts likely still have subscriptions or unresolved errors.")
        sys.exit(1)

    if grand_totals["delete_failed"] or grand_totals["auth_failed"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
