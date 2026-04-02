import os
import requests
from dotenv import load_dotenv

load_dotenv()


class ZohoClient:
    """
    Handles both Zoho CRM (lead creation) and Zoho Desk (KB article search).
    Uses the same OAuth refresh token — just needs both CRM and Desk.kb.READ scopes.
    """

    CRM_BASE_URL = "https://www.zohoapis.com/crm/v2"
    DESK_BASE_URL = "https://desk.zoho.com/api/v1"
    TOKEN_URL = "https://accounts.zoho.com/oauth/v2/token"

    def __init__(self):
        self.client_id = os.getenv("ZOHO_CLIENT_ID")
        self.client_secret = os.getenv("ZOHO_CLIENT_SECRET")
        self.refresh_token = os.getenv("ZOHO_REFRESH_TOKEN")
        self.org_id = os.getenv("ZOHO_ORG_ID")
        self.desk_org_id = os.getenv("ZOHO_DESK_ORG_ID") or os.getenv("ZOHO_ORG_ID")
        self._access_token = None

    def _refresh_access_token(self) -> str:
        resp = requests.post(self.TOKEN_URL, params={
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        })
        resp.raise_for_status()
        self._access_token = resp.json()["access_token"]
        return self._access_token

    def _token(self) -> str:
        return self._access_token or self._refresh_access_token()

    def _crm_headers(self) -> dict:
        return {"Authorization": f"Zoho-oauthtoken {self._token()}"}

    def _desk_headers(self) -> dict:
        return {
            "Authorization": f"Zoho-oauthtoken {self._token()}",
            "orgId": str(self.desk_org_id),
        }

    def _retry_on_401(self, fn):
        """Call fn(). If 401, refresh token and retry once."""
        result = fn()
        if hasattr(result, "status_code") and result.status_code == 401:
            self._access_token = None
            result = fn()
        return result

    # ── CRM ───────────────────────────────────────────────────────────────────

    def create_lead(self, data: dict) -> dict:
        """Create a lead in Zoho CRM. Returns the created record details dict."""
        try:
            resp = self._retry_on_401(lambda: requests.post(
                f"{self.CRM_BASE_URL}/Leads",
                headers=self._crm_headers(),
                json={"data": [data]},
            ))
            resp.raise_for_status()
            return resp.json().get("data", [{}])[0].get("details", {})
        except Exception as e:
            print(f"[ZohoClient] create_lead failed: {e}")
            return {}

    # ── CRM Search (Bids / Deals) ──────────────────────────────────────────────

    def search_bids(self, query: str) -> list:
        """
        Search the custom Bids module (CustomModule2) using COQL.
        Searches by Project_Name, Received_From (GC company), City, or Street.
        Returns list of matching bid records with key fields.
        """
        # Sanitize query for COQL — escape single quotes
        safe_q = query.replace("'", "\\'").strip()
        if not safe_q:
            return []

        coql = (
            "SELECT Project_Name, Received_From, Bid_Owner, Bid_Status, "
            "Bid_Due_Date, Bid_Sent_Date, Expected_Project_Start, "
            "Street, City, State, Zip, "
            "Pre_Contact_Name, Company, PC_Email, Phone "
            "FROM CustomModule2 "
            f"WHERE Project_Name like '%{safe_q}%' "
            f"OR Received_From like '%{safe_q}%' "
            f"OR City like '%{safe_q}%' "
            f"OR Street like '%{safe_q}%' "
            "LIMIT 5"
        )
        try:
            resp = self._retry_on_401(lambda: requests.post(
                f"{self.CRM_BASE_URL.replace('/v2', '/v7')}/coql",
                headers={**self._crm_headers(), "Content-Type": "application/json"},
                json={"select_query": coql},
            ))
            if resp.status_code != 200:
                print(f"[ZohoClient] COQL bids search returned {resp.status_code}: {resp.text[:200]}")
                return []

            records = resp.json().get("data", [])
            results = []
            for r in records:
                owner = r.get("Bid_Owner") or {}
                results.append({
                    "project_name": r.get("Project_Name", ""),
                    "received_from": r.get("Received_From", ""),
                    "bid_owner": owner.get("name", "") if isinstance(owner, dict) else str(owner),
                    "bid_status": r.get("Bid_Status", ""),
                    "bid_due_date": r.get("Bid_Due_Date", ""),
                    "city": r.get("City", ""),
                    "state": r.get("State", ""),
                    "street": r.get("Street", ""),
                })
            return results
        except Exception as e:
            print(f"[ZohoClient] search_bids failed: {e}")
            return []

    def search_deals(self, query: str) -> list:
        """
        Fallback search in the Deals module using COQL.
        Returns list of matching deal records.
        """
        safe_q = query.replace("'", "\\'").strip()
        if not safe_q:
            return []

        coql = (
            "SELECT Deal_Name, Stage, Owner, Amount, Closing_Date "
            "FROM Deals "
            f"WHERE Deal_Name like '%{safe_q}%' "
            "LIMIT 5"
        )
        try:
            resp = self._retry_on_401(lambda: requests.post(
                f"{self.CRM_BASE_URL.replace('/v2', '/v7')}/coql",
                headers={**self._crm_headers(), "Content-Type": "application/json"},
                json={"select_query": coql},
            ))
            if resp.status_code != 200:
                print(f"[ZohoClient] COQL deals search returned {resp.status_code}: {resp.text[:200]}")
                return []

            records = resp.json().get("data", [])
            results = []
            for r in records:
                owner = r.get("Owner") or {}
                results.append({
                    "deal_name": r.get("Deal_Name", ""),
                    "stage": r.get("Stage", ""),
                    "owner": owner.get("name", "") if isinstance(owner, dict) else str(owner),
                })
            return results
        except Exception as e:
            print(f"[ZohoClient] search_deals failed: {e}")
            return []

    # ── Desk KB ───────────────────────────────────────────────────────────────

    def search_kb_articles(self, query: str, limit: int = 3) -> list:
        """
        Search Zoho Desk published KB articles by keyword.
        Endpoint: GET /api/v1/articles/search?_all={query}&status=published
        Required OAuth scope: Desk.kb.READ
        Returns list of dicts: [{"title": str, "summary": str}, ...]
        """
        try:
            resp = self._retry_on_401(lambda: requests.get(
                f"{self.DESK_BASE_URL}/articles/search",
                headers=self._desk_headers(),
                params={
                    "_all": query,
                    "status": "published",
                    "limit": limit,
                    "from": 0,
                },
            ))
            if resp.status_code != 200:
                print(f"[ZohoClient] KB search returned {resp.status_code}: {resp.text[:200]}")
                return []

            articles = resp.json().get("data", [])
            return [
                {
                    "title": a.get("title", ""),
                    "summary": a.get("summary", a.get("answer", "")),
                }
                for a in articles
            ]
        except Exception as e:
            print(f"[ZohoClient] search_kb_articles failed: {e}")
            return []
