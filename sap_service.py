from __future__ import annotations

import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()


class SAPService:
    """Client for SAP Business Partner reads and explicitly enabled creates."""
    def __init__(self) -> None:
        self.mode = os.getenv("SAP_MODE", "sandbox").strip().lower()
        self.base_url = os.getenv("SAP_BASE_URL", "").strip().rstrip("/")
        self.api_key = os.getenv("SAP_API_KEY", "").strip()
        self.verify_ssl = os.getenv("SAP_VERIFY_SSL", "true").strip().lower() == "true"
        self.write_enabled = os.getenv("SAP_WRITE_ENABLED", "false").strip().lower() == "true"
        self.timeout = int(os.getenv("SAP_TIMEOUT_SECONDS", "30"))

    def configuration_status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "base_url_configured": bool(self.base_url),
            "api_key_configured": bool(self.api_key),
            "verify_ssl": self.verify_ssl,
            "write_enabled": self.write_enabled,
        }

    def _validate(self) -> None:
        if not self.base_url:
            raise ValueError("SAP_BASE_URL is missing from the .env file.")
        if not self.api_key:
            raise ValueError("SAP_API_KEY is missing from the .env file.")

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "APIKey": self.api_key}

    def _write_headers(self) -> dict[str, str]:
        return {**self._headers(), "Content-Type": "application/json"}

    @staticmethod
    def _extract_results(data: dict[str, Any]) -> list[dict[str, Any]]:
        results = data.get("d", {}).get("results")
        if isinstance(results, list):
            return results
        value = data.get("value")
        return value if isinstance(value, list) else []

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._validate()
        response = requests.get(
            f"{self.base_url}/{path.lstrip('/')}",
            headers=self._headers(),
            params=params,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        if not response.ok:
            raise RuntimeError(
                f"SAP request failed with HTTP {response.status_code}: "
                f"{response.text[:700]}"
            )
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError("SAP returned a non-JSON response.") from exc

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate()
        if not self.write_enabled:
            raise PermissionError(
                "SAP writes are disabled. Set SAP_WRITE_ENABLED=true in .env "
                "and restart the application."
            )
        response = requests.post(
            f"{self.base_url}/{path.lstrip('/')}",
            headers=self._write_headers(),
            json=payload,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        if not response.ok:
            raise RuntimeError(
                f"SAP write failed with HTTP {response.status_code}: "
                f"{response.text[:700]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError("SAP returned a non-JSON response after the write.") from exc

    def _patch(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        self._validate()
        if not self.write_enabled:
            raise PermissionError(
                "SAP writes are disabled. Set SAP_WRITE_ENABLED=true in .env "
                "and restart the application."
            )
        response = requests.patch(
            f"{self.base_url}/{path.lstrip('/')}",
            headers={**self._write_headers(), "If-Match": "*"},
            json=payload,
            timeout=self.timeout,
            verify=self.verify_ssl,
        )
        if not response.ok:
            raise RuntimeError(
                f"SAP update failed with HTTP {response.status_code}: "
                f"{response.text[:700]}"
            )
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError("SAP returned a non-JSON response after the update.") from exc

    def test_connection(self) -> dict[str, Any]:
        data = self._get(
            "A_BusinessPartner",
            {"$top": 1, "$format": "json", "$select": "BusinessPartner"},
        )
        records = self._extract_results(data)
        return {
            "ok": True,
            "status_code": 200,
            "message": "Connected successfully to the SAP sandbox.",
            "records_received": len(records),
        }

    def list_business_partners(self, top: int = 10) -> dict[str, Any]:
        limit = max(1, min(int(top), 50))
        data = self._get(
            "A_BusinessPartner",
            {
                "$top": limit,
                "$format": "json",
                "$select": (
                    "BusinessPartner,BusinessPartnerFullName,"
                    "BusinessPartnerCategory,Customer,Supplier"
                ),
            },
        )
        records = self._extract_results(data)
        return {"ok": True, "count": len(records), "business_partners": records}

    def search_business_partners(self, name: str, top: int = 10) -> dict[str, Any]:
        search_name = name.strip()
        if not search_name:
            raise ValueError("name cannot be empty")
        limit = max(1, min(int(top), 50))
        safe_name = search_name.replace("'", "''")
        data = self._get(
            "A_BusinessPartner",
            {
                "$top": limit,
                "$format": "json",
                "$filter": f"substringof('{safe_name}',BusinessPartnerFullName)",
                "$select": (
                    "BusinessPartner,BusinessPartnerFullName,"
                    "BusinessPartnerCategory,Customer,Supplier"
                ),
            },
        )
        records = self._extract_results(data)
        return {
            "ok": True,
            "query": search_name,
            "count": len(records),
            "business_partners": records,
        }

    def get_business_partner(self, business_partner_id: str) -> dict[str, Any]:
        partner_id = business_partner_id.strip()
        if not partner_id:
            raise ValueError("business_partner_id cannot be empty")
        safe_id = partner_id.replace("'", "''")
        data = self._get(
            f"A_BusinessPartner('{safe_id}')",
            {"$format": "json"},
        )
        return {"ok": True, "business_partner": data.get("d", data)}

    def list_email_addresses(self, top: int = 10) -> dict[str, Any]:
        limit = max(1, min(int(top), 50))
        data = self._get(
            "A_AddressEmailAddress",
            {"$top": limit, "$format": "json"},
        )
        records = self._extract_results(data)
        return {"ok": True, "count": len(records), "email_addresses": records}

    def list_sales_orders(self, top: int = 10) -> dict[str, Any]:
        """
        List sales orders via the standard S/4HANA A_SalesOrder OData service.

        NOTE: this service (API_SALES_ORDER_SRV) is not always enabled on
        the free SAP Business Accelerator Hub sandbox tenant used elsewhere
        in this file for A_BusinessPartner. If the sandbox key does not have
        it activated, this call will raise a clear HTTP error rather than
        failing silently -- check the error against your API catalog.
        """
        limit = max(1, min(int(top), 50))
        data = self._get(
            "A_SalesOrder",
            {
                "$top": limit,
                "$format": "json",
                "$select": (
                    "SalesOrder,SalesOrderType,SalesOrganization,"
                    "SoldToParty,TotalNetAmount,TransactionCurrency,"
                    "SalesOrderDate"
                ),
            },
        )
        records = self._extract_results(data)
        return {"ok": True, "count": len(records), "sales_orders": records}

    def get_sales_order(self, sales_order_id: str) -> dict[str, Any]:
        """Read one sales order by ID."""
        order_id = sales_order_id.strip()
        if not order_id:
            raise ValueError("sales_order_id cannot be empty")
        safe_id = order_id.replace("'", "''")
        data = self._get(f"A_SalesOrder('{safe_id}')", {"$format": "json"})
        return {"ok": True, "sales_order": data.get("d", data)}

    def list_invoices(self, top: int = 10) -> dict[str, Any]:
        """
        List billing documents (invoices) via API_BILLING_DOCUMENT_SRV's
        A_BillingDocument entity set. Same sandbox-activation caveat as
        list_sales_orders applies.
        """
        limit = max(1, min(int(top), 50))
        data = self._get(
            "A_BillingDocument",
            {
                "$top": limit,
                "$format": "json",
                "$select": (
                    "BillingDocument,BillingDocumentType,BillingDocumentDate,"
                    "SoldToParty,TotalNetAmount,TransactionCurrency"
                ),
            },
        )
        records = self._extract_results(data)
        return {"ok": True, "count": len(records), "invoices": records}

    def get_invoice(self, billing_document_id: str) -> dict[str, Any]:
        """Read one billing document (invoice) by ID."""
        invoice_id = billing_document_id.strip()
        if not invoice_id:
            raise ValueError("billing_document_id cannot be empty")
        safe_id = invoice_id.replace("'", "''")
        data = self._get(f"A_BillingDocument('{safe_id}')", {"$format": "json"})
        return {"ok": True, "invoice": data.get("d", data)}

    def list_products(self, top: int = 10) -> dict[str, Any]:
        """
        List products via API_PRODUCT_SRV's A_Product entity set. Same
        sandbox-activation caveat as list_sales_orders applies.
        """
        limit = max(1, min(int(top), 50))
        data = self._get(
            "A_Product",
            {
                "$top": limit,
                "$format": "json",
                "$select": "Product,ProductType,BaseUnit,ProductGroup",
            },
        )
        records = self._extract_results(data)
        return {"ok": True, "count": len(records), "products": records}

    def get_product(self, product_id: str) -> dict[str, Any]:
        """Read one product by ID."""
        prod_id = product_id.strip()
        if not prod_id:
            raise ValueError("product_id cannot be empty")
        safe_id = prod_id.replace("'", "''")
        data = self._get(f"A_Product('{safe_id}')", {"$format": "json"})
        return {"ok": True, "product": data.get("d", data)}

    # def create_business_partner(
    #     self,
    #     category: str,
    #     organization_name: str = "",
    #     first_name: str = "",
    #     last_name: str = "",
    #     search_term: str = "",
    #     business_partner_id: str = "",
    # ) -> dict[str, Any]:
    #     """Create a person (1) or organization (2) business partner."""
    #     normalized_category = str(category).strip()
    #     if normalized_category not in {"1", "2"}:
    #         raise ValueError("category must be '1' (Person) or '2' (Organization)")

    #     payload: dict[str, Any] = {"BusinessPartnerCategory": normalized_category}
    #     if normalized_category == "1":
    #         clean_first_name = first_name.strip()
    #         clean_last_name = last_name.strip()
    #         if not clean_last_name:
    #             raise ValueError("last_name is required for a person")
    #         payload.update({"FirstName": clean_first_name, "LastName": clean_last_name})
    #     else:
    #         clean_organization_name = organization_name.strip()
    #         if not clean_organization_name:
    #             raise ValueError("organization_name is required for an organization")
    #         payload["OrganizationBPName1"] = clean_organization_name

    #     if search_term.strip():
    #         payload["SearchTerm1"] = search_term.strip()
    #     if business_partner_id.strip():
    #         payload["BusinessPartner"] = business_partner_id.strip()

    #     data = self._post("A_BusinessPartner", payload)
    #     partner = data.get("d", data)
    #     return {
    #         "ok": True,
    #         "message": "SAP business partner created successfully.",
    #         "business_partner": partner,
    #     }

    # def update_business_partner(
    #     self,
    #     business_partner_id: str,
    #     organization_name: str | None = None,
    #     first_name: str | None = None,
    #     last_name: str | None = None,
    #     search_term: str | None = None,
    # ) -> dict[str, Any]:
    #     """Update selected name/search fields on an existing business partner."""
    #     partner_id = business_partner_id.strip()
    #     if not partner_id:
    #         raise ValueError("business_partner_id cannot be empty")

    #     field_values = {
    #         "OrganizationBPName1": organization_name,
    #         "FirstName": first_name,
    #         "LastName": last_name,
    #         "SearchTerm1": search_term,
    #     }
    #     payload = {
    #         sap_field: value.strip()
    #         for sap_field, value in field_values.items()
    #         if value is not None
    #     }
    #     if not payload:
    #         raise ValueError("Provide at least one field to update")

    #     safe_id = partner_id.replace("'", "''")
    #     data = self._patch(f"A_BusinessPartner('{safe_id}')", payload)
    #     partner = data.get("d", data) if data else {"BusinessPartner": partner_id, **payload}
    #     return {
    #         "ok": True,
    #         "message": "SAP business partner updated successfully.",
    #         "business_partner": partner,
    #     }