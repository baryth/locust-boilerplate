"""
Cleanup utility for the Topics/Subscriptions API.

Purpose: NOT a load test. This is a one-shot teardown script that, for every
credential pair in the CSV file, authenticates, fetches that account's
current subscriptions, and unsubscribes ALL of them concurrently.

Use this between load test runs (or on demand) to clear all accounts back
to a clean/empty state, so a fresh Locust run doesn't start with leftover
subscriptions from a previous run.

--- EDIT THESE BEFORE RUNNING ---
"""

import asyncio
import csv
import sys

import aiohttp

# ---- CONFIG: edit to match your real API ----
BASE_URL = "REPLACE_WITH_BASE_URL"      # e.g. "https://api.example.com" (no trailing slash)
AUTH_URL = "REPLACE_WITH_AUTH_URI"      # same token endpoint used in locustfile.py

CREDENTIALS_FILE = "credentials.csv"    # CSV with header: client_id,client_secret
AUTH_SCOPE = "123"                      # scope is shared across all credential pairs
AUTH_GRANT_TYPE = "client_credentials"

SUBSCRIPTIONS_ENDPOINT = "/subscriptions"
DELETE_PATH = "/subscriptions/{id}/unsubscribe"

# How many accounts to process at the same time. Keep this reasonable --
# this script is meant to clean up state, not itself act as a load test.
MAX_CONCURRENT_ACCOUNTS = 10

# How many DELETE calls to fire at once *within* a single account.
MAX_CONCURRENT_DELETES_PER_ACCOUNT = 5

REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)


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


async def authenticate(session, client_id, client_secret):
    """Client-credentials grant, form-data, returns a bearer token or None."""
    payload = {
        "grant_type": AUTH_GRANT_TYPE,
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": AUTH_SCOPE,
    }
    async with session.get(AUTH_URL, data=payload) as resp:
        if resp.status != 200:
            print(f"  [auth] {client_id}: unexpected status {resp.status}")
            return None
        try:
            data = await resp.json()
        except aiohttp.ContentTypeError:
            print(f"  [auth] {client_id}: invalid JSON response")
            return None
        token = data.get("access_token")
        if not token:
            print(f"  [auth] {client_id}: no access_token in response")
            return None
        return token


async def fetch_subscription_ids(session, headers, client_id):
    """GET /subscriptions for this account. Returns (sub_ids, get_ok) --
    get_ok is False on any failure so callers can distinguish "genuinely
    has zero subscriptions" from "couldn't check, don't trust that '0'"."""
    url = BASE_URL + SUBSCRIPTIONS_ENDPOINT
    async with session.get(url, headers=headers) as resp:
        if resp.status != 200:
            print(f"  [get] {client_id}: unexpected status {resp.status}")
            return [], False
        try:
            data = await resp.json()
        except aiohttp.ContentTypeError:
            print(f"  [get] {client_id}: invalid JSON response")
            return [], False
        items = data if isinstance(data, list) else data.get("subscriptions", [])
        sub_ids = [item.get("id") for item in items if item.get("id") is not None]
        return sub_ids, True


async def unsubscribe_one(session, headers, client_id, sub_id, delete_sem):
    """Delete one subscription. Returns True on success, False on failure."""
    async with delete_sem:
        url = BASE_URL + DELETE_PATH.format(id=sub_id)
        try:
            async with session.delete(url, headers=headers) as resp:
                if resp.status in (200, 204):
                    return True
                print(f"  [delete] {client_id}: id={sub_id} unexpected status {resp.status}")
                return False
        except aiohttp.ClientError as exc:
            print(f"  [delete] {client_id}: id={sub_id} request error: {exc}")
            return False


async def clear_account(session, client_id, client_secret, account_sem, delete_sem):
    """Run one cleanup pass for a single account. Returns a result dict:
    {"auth_failed": bool, "get_failed": bool, "found": int, "deleted": int, "delete_failed": int}
    """
    async with account_sem:
        token = await authenticate(session, client_id, client_secret)
        if not token:
            return {"auth_failed": True, "get_failed": False, "found": 0, "deleted": 0, "delete_failed": 0}

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        sub_ids, get_ok = await fetch_subscription_ids(session, headers, client_id)
        if not get_ok:
            # Couldn't even determine what this account has -- do NOT treat
            # this as "nothing to unsubscribe". The outer loop needs to
            # know this account still needs a retry pass.
            return {"auth_failed": False, "get_failed": True, "found": 0, "deleted": 0, "delete_failed": 0}

        if not sub_ids:
            print(f"  [ok] {client_id}: nothing to unsubscribe")
            return {"auth_failed": False, "get_failed": False, "found": 0, "deleted": 0, "delete_failed": 0}

        print(f"  [ok] {client_id}: unsubscribing {len(sub_ids)} subscription(s)")
        outcomes = await asyncio.gather(*[
            unsubscribe_one(session, headers, client_id, sub_id, delete_sem)
            for sub_id in sub_ids
        ])
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


async def run_pass(session, credentials, account_sem, delete_sem, pass_num):
    print(f"\n--- Pass {pass_num} ---")
    account_results = await asyncio.gather(*[
        clear_account(session, client_id, client_secret, account_sem, delete_sem)
        for client_id, client_secret in credentials
    ])

    totals = {"auth_failed": 0, "get_failed": 0, "found": 0, "deleted": 0, "delete_failed": 0}
    for r in account_results:
        for key in totals:
            totals[key] += r[key]

    print(f"Pass {pass_num} summary: found={totals['found']} deleted={totals['deleted']} "
          f"delete_failed={totals['delete_failed']} get_failed={totals['get_failed']} "
          f"auth_failed={totals['auth_failed']}")
    return totals


async def main():
    credentials = load_credentials(CREDENTIALS_FILE)
    print(f"Loaded {len(credentials)} credential pair(s) from {CREDENTIALS_FILE}")

    account_sem = asyncio.Semaphore(MAX_CONCURRENT_ACCOUNTS)
    delete_sem = asyncio.Semaphore(MAX_CONCURRENT_DELETES_PER_ACCOUNT)

    grand_totals = {"auth_failed": 0, "get_failed": 0, "found": 0, "deleted": 0, "delete_failed": 0}
    pass_num = 0
    clean_pass_achieved = False

    async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
        while pass_num < MAX_PASSES:
            pass_num += 1
            totals = await run_pass(session, credentials, account_sem, delete_sem, pass_num)
            for key in grand_totals:
                grand_totals[key] += totals[key]

            # Stop only once a full pass found nothing left to do AND every
            # account's GET actually succeeded -- a get_failed account
            # might still be hiding subscriptions we never got to check.
            if totals["found"] == 0 and totals["get_failed"] == 0:
                clean_pass_achieved = True
                break

            if pass_num < MAX_PASSES:
                await asyncio.sleep(PASS_DELAY_SECONDS)

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
    asyncio.run(main())
