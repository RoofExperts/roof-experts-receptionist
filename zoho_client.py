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

    # ── CRM ─────────────────────────────────────────────────────────────────────────

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

    # ── Desk KB ─────────────────────────────────────────────────────────────────────

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
